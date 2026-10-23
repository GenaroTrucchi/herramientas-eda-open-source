#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
optimizar.py - Dimensionamiento automatico multi-corner (W, L, mult, nf, C, R, .param) con ngspice.

Busca el diseno de menor consumo (o el objetivo que definas) que cumpla TODAS las
especificaciones en TODOS los corners (ej. TT, FF, SS), variando los parametros que
declares en un archivo YAML. Algoritmo: Evolucion Diferencial con reglas de
factibilidad de Deb (un diseno que cumple siempre le gana a uno que no cumple;
entre dos que no cumplen gana el que viola menos; entre dos que cumplen gana el
de mejor objetivo en el peor corner).

Uso:
  optimizar config.yaml              optimiza
  optimizar config.yaml --evaluar    solo simula el diseno actual en todos los corners
  optimizar config.yaml --aplicar resultados/<carpeta>
                                     escribe el mejor diseno en los .sch (deja .bak)

Ver LEEME.md para el formato del YAML.
"""
import argparse
import csv
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import yaml

# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
_SI = {'t': 1e12, 'g': 1e9, 'meg': 1e6, 'k': 1e3, 'm': 1e-3, 'u': 1e-6,
       'n': 1e-9, 'p': 1e-12, 'f': 1e-15, 'a': 1e-18}
_NUM_RE = re.compile(r'^([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)(meg|[tgkmunpfa])?', re.I)


def parse_si(s):
    """'14.3p' -> 1.43e-11, '95u' -> 9.5e-05, 50e6 -> 5e7."""
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().strip("'\"").lower().replace('µ', 'u')
    m = _NUM_RE.match(t)
    if not m:
        raise ValueError(f"valor no numerico: {s!r}")
    v = float(m.group(1))
    if m.group(2):
        v *= _SI[m.group(2).lower()]
    return v


def try_si(s):
    try:
        return parse_si(s)
    except (ValueError, TypeError):
        return None


def fmt_num(v, entero=False):
    if entero:
        return str(int(round(v)))
    return f"{v:.6g}"


def fmt_eng(v):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return str(v)
    if v == 0:
        return "0"
    for suf, mul in (('G', 1e9), ('M', 1e6), ('k', 1e3), ('', 1), ('m', 1e-3),
                     ('u', 1e-6), ('n', 1e-9), ('p', 1e-12), ('f', 1e-15)):
        if abs(v) >= mul:
            return f"{v / mul:.4g}{suf}"
    return f"{v:.3e}"


_SAFE = {k: getattr(math, k) for k in ('sqrt', 'log', 'log10', 'exp', 'pi', 'sin', 'cos',
                                       'tan', 'atan', 'atan2', 'floor', 'ceil')}
_SAFE.update({'abs': abs, 'min': min, 'max': max, 'db': lambda x: 20 * math.log10(abs(x))})


def eval_expr(expr, env):
    return float(eval(str(expr).lower(), {'__builtins__': {}}, {**_SAFE, **env}))


# ----------------------------------------------------------------------------
# Netlist SPICE editable
# ----------------------------------------------------------------------------
class Netlist:
    """Netlist con lineas logicas (une continuaciones '+') y contexto de subcircuito."""

    def __init__(self, text, base_dir):
        self.base_dir = base_dir
        self.groups = []          # lista de listas de lineas fisicas
        for raw in text.splitlines():
            if raw.startswith('+') and self.groups:
                self.groups[-1].append(raw)
            else:
                self.groups.append([raw])
        self.logical = [' '.join([g[0]] + [l[1:].strip() for l in g[1:]]) for g in self.groups]
        self.ctx = []             # subckt (minusculas) de cada linea logica, '' = top
        self.subckts = {}         # nombre -> indice de .subckt
        self.sch_of = {}          # subckt ('' = top) -> ruta .sch (comentarios de xschem)
        cur, last_sch = '', None
        for i, l in enumerate(self.logical):
            s = l.strip()
            low = s.lower()
            m = re.match(r'\*\*\s*sch_path:\s*(\S+)', s)
            if m:
                last_sch = m.group(1)
                if '' not in self.sch_of:
                    self.sch_of[''] = last_sch
            if low.startswith('.subckt'):
                cur = s.split()[1].lower()
                self.subckts[cur] = i
                if last_sch:
                    self.sch_of[cur] = last_sch
            self.ctx.append(cur)
            if low.startswith('.ends'):
                cur = ''
        self.edited = {}          # indice -> linea nueva
        self._prepend_extra = []

    # -- busqueda -----------------------------------------------------------
    def find_instances(self, ref):
        """'opamp.XM1' o 'XM1' -> lista de indices de linea."""
        if '.' in ref:
            sub, name = ref.lower().split('.', 1)
        else:
            sub, name = None, ref.lower()
        out = []
        for i, l in enumerate(self.logical):
            t = l.split()
            if not t or t[0].startswith(('*', '.', '+')):
                continue
            if t[0].lower() == name and (sub is None or self.ctx[i] == sub):
                out.append(i)
        return out

    def find_param(self, name):
        rx = re.compile(r'(?<![\w.])' + re.escape(name) + r'\s*=\s*', re.I)
        return [i for i, l in enumerate(self.logical)
                if l.strip().lower().startswith('.param') and rx.search(l)]

    def line(self, i):
        return self.edited.get(i, self.logical[i])

    # -- lectura/escritura de atributos ------------------------------------
    @staticmethod
    def _attr_rx(attr):
        return re.compile(r'(?<=\s)(' + re.escape(attr) + r')\s*=\s*(\'[^\']*\'|\S+)', re.I)

    def get_attr(self, i, attr):
        l = self.line(i)
        if attr == 'valor':
            t = l.split()
            return t[3] if len(t) > 3 else None
        m = self._attr_rx(attr).search(l)
        return m.group(2) if m else None

    def set_attr(self, i, attr, val):
        l = self.line(i)
        if attr == 'valor':
            t = l.split()
            t[3] = val
            l = ' '.join(t)
        else:
            names = [attr]
            if attr.lower() == 'mult':        # xschem pone mult= y m= ; mantener ambos iguales
                names.append('m')
            for a in names:
                rx = self._attr_rx(a)
                if rx.search(l):
                    l = rx.sub(lambda m: f"{m.group(1)}={val}", l)
                elif a == attr:
                    l = l + f" {a}={val}"
                elif attr.lower() == 'mult':
                    l = l + f" m={val}"
        self.edited[i] = l

    def get_param(self, i, name):
        m = re.search(r'(?<![\w.])' + re.escape(name) + r'\s*=\s*(\S+)', self.line(i), re.I)
        return m.group(1) if m else None

    def set_param(self, i, name, val):
        rx = re.compile(r'((?<![\w.])' + re.escape(name) + r'\s*=\s*)(\S+)', re.I)
        self.edited[i] = rx.sub(lambda m: m.group(1) + val, self.line(i))

    # -- corners -------------------------------------------------------------
    def set_lib_corner(self, patron, corner):
        n = 0
        for i, l in enumerate(self.logical):
            s = self.line(i).strip()
            if s.lower().startswith('.lib') and patron.lower() in s.lower():
                t = s.split()
                if len(t) >= 3:
                    t[-1] = corner
                    self.edited[i] = ' '.join(t)
                    n += 1
        return n

    def set_temp(self, temp):
        done = False
        for i, l in enumerate(self.logical):
            s = self.line(i)
            low = s.strip().lower()
            if low.startswith('.temp'):
                self.edited[i] = f".temp {temp}"
                done = True
            elif low.startswith('.option') and re.search(r'\btemp\s*=', low):
                self.edited[i] = re.sub(r'(\btemp\s*=\s*)\S+', lambda m: m.group(1) + str(temp), s, flags=re.I)
                done = True
        if not done:
            self._prepend_extra.append(f".temp {temp}")

    # -- render --------------------------------------------------------------
    def render(self, quitar=(), inyectar=(), prepend=()):
        out, in_ctrl = [], False
        ctrl_end = max((i for i, l in enumerate(self.logical) if l.strip().lower() == '.endc'), default=None)
        end_idx = max((i for i, l in enumerate(self.logical) if l.strip().lower() == '.end'), default=len(self.logical))
        for i, g in enumerate(self.groups):
            low = self.logical[i].strip().lower()
            if i == end_idx and prepend:
                out.extend(prepend)
            if ctrl_end is None and i == end_idx and inyectar:
                out.extend(['.control', 'run', *inyectar, '.endc'])
            if low == '.control':
                in_ctrl = True
            if in_ctrl and low.split() and low.split()[0] in quitar:
                continue
            if i == ctrl_end:
                out.extend(inyectar)
                in_ctrl = False
            if low == '.endc':
                in_ctrl = False
            if i in self.edited:
                out.append(self.edited[i])
            else:
                # rutas relativas de .include/.lib -> absolutas
                s = g[0]
                m = re.match(r'(\s*\.(?:include|inc|lib)\s+)(["\']?)([^\s"\']+)(.*)$', s, re.I)
                if m and not m.group(3).startswith(('/', '~', '$')):
                    s = m.group(1) + m.group(2) + os.path.normpath(os.path.join(self.base_dir, m.group(3))) + m.group(4)
                out.append(s)
                out.extend(g[1:])
        return '\n'.join(out) + '\n'

    def clone(self):
        c = Netlist.__new__(Netlist)
        c.__dict__.update(self.__dict__)
        c.edited = dict(self.edited)
        c._prepend_extra = []
        return c

    # -- jerarquia de transistores --------------------------------------------
    def model_of(self, i):
        # sacar valores entre comillas/llaves (ej. ad='W * 0.29') y tomar lo previo al primer k=v
        s = re.sub(r"'[^']*'|\{[^}]*\}|\"[^\"]*\"", "_", self.logical[i])
        toks = []
        for x in s.split()[1:]:
            if '=' in x:
                break
            toks.append(x)
        return toks[-1].lower() if toks else ''

    def walk_devices(self, patron_dispositivo, sub='', prefix=''):
        """Devuelve [(path 'x1.xm1', modelo, subckt, nombre)] de transistores en la jerarquia."""
        rx = re.compile(patron_dispositivo, re.I)
        res = []
        for i, l in enumerate(self.logical):
            if self.ctx[i] != sub:
                continue
            t = l.split()
            if not t or not t[0][0].lower() in 'xm':
                continue
            if self.logical[i].strip().lower().startswith(('.subckt', '.ends')):
                continue
            model = self.model_of(i)
            path = (prefix + '.' if prefix else '') + t[0].lower()
            if model in self.subckts and model != sub:
                res += self.walk_devices(patron_dispositivo, model, path)
            elif rx.search(model):
                res.append((path, model, sub, t[0]))
        return res


# ----------------------------------------------------------------------------
# Recorte de librerias de modelos (acelera mucho: ngspice tarda ~1 s solo en
# leer la libreria completa de sky130; el circuito usa 3 o 4 dispositivos)
# ----------------------------------------------------------------------------
_ID_RE = re.compile(r'[A-Za-z_][\w]*(?:\.\w+)*')


def _ids(text):
    out = []
    for x in _ID_RE.findall(text):
        x = x.lower()
        out.append(x)
        if '.' in x:
            out.append(x.split('.', 1)[0])
    return out


_ASSIGN_RE = re.compile(r'(?<![\w.<>!=])([A-Za-z_]\w*)\s*=(?!=)')


def _leer_logico(path):
    out = []
    with open(path, errors='replace') as f:
        for raw in f:
            # comentarios en linea ('$' o ';'): ngspice los ignora, y si se dejan aparecen
            # asignaciones falsas (ej. "... $ for L>=4 and W=0.42" -> .param W = 0.42)
            s = re.sub(r'(^|\s)[$;].*$', '', raw.rstrip('\n'))
            st = s.strip()
            if not st or st.startswith('*'):
                continue
            if st.startswith('+') and out:
                out[-1] += ' ' + st[1:].strip()
            else:
                out.append(st)
    return out


def _aplanar(path, seccion, out, prof=0):
    if prof > 30:
        raise RuntimeError("includes demasiado anidados")
    lines = _leer_logico(path)
    if seccion is not None:
        sel, dentro = [], False
        for l in lines:
            t = l.split()
            if t[0].lower() == '.lib' and len(t) == 2 and t[1].lower() == seccion.lower():
                dentro = True
                continue
            if dentro and t[0].lower() == '.endl':
                break
            if dentro:
                sel.append(l)
        if not dentro:
            raise RuntimeError(f"no encontre la seccion '{seccion}' en {path}")
        lines = sel
    base = os.path.dirname(path)

    def res(p):
        p = os.path.expanduser(p.strip('"\''))
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))
    for l in lines:
        t = l.split()
        k = t[0].lower()
        if k in ('.include', '.inc') and len(t) >= 2:
            _aplanar(res(t[1]), None, out, prof + 1)
        elif k == '.lib' and len(t) >= 3:
            _aplanar(res(t[1]), t[2], out, prof + 1)
        elif k in ('.lib', '.endl', '.end'):
            continue
        else:
            out.append(l)


def recortar_lib(lib_path, seccion, raices):
    """Aplana '.lib lib_path seccion' y deja solo subckts/modelos/params alcanzables desde 'raices'."""
    flat = []
    _aplanar(lib_path, seccion, flat)
    items = []          # (tipo, nombre, texto)
    i = 0
    while i < len(flat):
        l = flat[i]
        t = l.split()
        k = t[0].lower()
        if k == '.subckt':
            j = i
            depth = 0
            while j < len(flat):
                kk = flat[j].split()[0].lower()
                depth += kk == '.subckt'
                depth -= kk == '.ends'
                if depth == 0:
                    break
                j += 1
            items.append(('subckt', t[1].lower(), '\n'.join(flat[i:j + 1])))
            i = j + 1
            continue
        if k == '.model':
            items.append(('model', t[1].lower(), l))
        elif k == '.param':
            body = l[len(t[0]):]
            ms = [m for m in _ASSIGN_RE.finditer(body) if body[:m.start()].count("'") % 2 == 0]
            for a, m in enumerate(ms):
                fin = ms[a + 1].start() if a + 1 < len(ms) else len(body)
                items.append(('param', m.group(1).lower(), f".param {m.group(1)} = {body[m.end():fin].strip()}"))
        else:
            items.append(('otro', '', l))
        i += 1
    por_nombre = {}
    modelos = []
    for idx, (tp, nm, tx) in enumerate(items):
        if tp in ('subckt', 'param'):
            por_nombre.setdefault(nm, []).append(idx)
        elif tp == 'model':
            modelos.append((nm, idx))
    keep = set(idx for idx, it in enumerate(items) if it[0] == 'otro')
    pend = [r.lower() for r in raices]
    for idx in list(keep):
        pend += _ids(items[idx][2])
    vistos = set()
    while pend:
        n = pend.pop()
        if n in vistos:
            continue
        vistos.add(n)
        nuevos = list(por_nombre.get(n, []))
        nuevos += [idx for nm, idx in modelos if nm == n or nm.startswith(n + '.')]
        for idx in nuevos:
            if idx not in keep:
                keep.add(idx)
                pend += _ids(items[idx][2])
    out = [f"* recortado de {lib_path} seccion {seccion} por optimizar.py"]
    out += [items[idx][2] for idx in sorted(keep)]
    return '\n'.join(out) + '\n', len(items), len(keep)


# ----------------------------------------------------------------------------
# Simulacion (corre en procesos hijos)
# ----------------------------------------------------------------------------
_OUT_RE = re.compile(r'^\s*([A-Za-z_][\w]*)\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$')


def parse_output(out):
    vals = {}
    for line in out.splitlines():
        m = _OUT_RE.match(line)
        if m:
            vals[m.group(1).lower()] = float(m.group(2))
    return vals


# muchas simulaciones en paralelo: cada ngspice con un solo hilo y SIN espera activa de OpenMP.
# Sin OMP_WAIT_POLICY=PASSIVE, con 8+ procesos los hilos "girando" se roban la CPU y cada
# simulacion pasa de 0.06 s a varios segundos (medido: 3 -> 135 simulaciones/s con 18 procesos).
_ENV_1HILO = dict(os.environ, OMP_NUM_THREADS='1', OMP_WAIT_POLICY='PASSIVE', GOMP_SPINCOUNT='0',
                  OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')


def run_ngspice(job):
    text, tmpbase, spiceinit, timeout = job
    d = tempfile.mkdtemp(prefix='opt_', dir=tmpbase)
    try:
        with open(os.path.join(d, 'tb.spice'), 'w') as f:
            f.write(text)
        with open(os.path.join(d, '.spiceinit'), 'w') as f:
            f.write(spiceinit)
        try:
            p = subprocess.run(['ngspice', '-b', 'tb.spice'], cwd=d, capture_output=True,
                               text=True, timeout=timeout, errors='replace', env=_ENV_1HILO)
            out = p.stdout + '\n' + p.stderr
        except subprocess.TimeoutExpired:
            out = 'TIMEOUT'
    finally:
        shutil.rmtree(d, ignore_errors=True)
    errs = [l.strip() for l in out.splitlines() if re.search(r'error|timeout|singular|not converge', l, re.I)]
    return parse_output(out), errs[:5]


# ----------------------------------------------------------------------------
# Problema
# ----------------------------------------------------------------------------
class Problema:
    def __init__(self, cfg_path, outdir=None):
        self.cfg_path = os.path.abspath(cfg_path)
        self.cfg_dir = os.path.dirname(self.cfg_path)
        with open(cfg_path) as f:
            self.cfg = yaml.safe_load(f)
        c = self.cfg
        alg = c.get('algoritmo', {}) or {}
        self.alg = {
            'poblacion': int(alg.get('poblacion', 0)),
            'max_evaluaciones': int(alg.get('max_evaluaciones', 2000)),
            'generaciones_sin_mejora': int(alg.get('generaciones_sin_mejora', 30)),
            'procesos': int(alg.get('procesos', max(1, (os.cpu_count() or 2) - 2))),
            'semilla': alg.get('semilla', None),
            # None = automatico: 20x lo que tarda el diseno actual (minimo 5 s)
            'timeout_sim': float(alg['timeout_sim']) if alg.get('timeout_sim') else None,
            'recortar_modelos': bool(alg.get('recortar_modelos', True)),
        }
        self.recortar = False          # se activa despues de validar (ver validar_recorte)
        self._recortes = {}
        self.quitar = set(x.lower() for x in c.get('quitar_comandos',
                                                     ['plot', 'write', 'wrdata', 'hardcopy', 'gnuplot', 'wrs2p']))
        self.outdir = outdir
        self._load_testbenches()
        self._load_params()
        self._load_corners()
        self._load_specs()

    def p(self, rel):
        return os.path.normpath(os.path.join(self.cfg_dir, os.path.expanduser(str(rel))))

    # -- testbenches ----------------------------------------------------------
    def _load_testbenches(self):
        self.tbs = []
        for tb in self.cfg['testbenches']:
            nombre = tb['nombre'].lower()
            if 'netlist' in tb:
                path = self.p(tb['netlist'])
                base = os.path.dirname(path)
            else:
                path, base = self._netlistear(nombre, tb)
            with open(path) as f:
                text = f.read()
            if 'IS MISSING' in text:
                raise SystemExit(f"[{nombre}] el netlist tiene simbolos faltantes ('IS MISSING'): {path}")
            si = tb.get('spiceinit')
            cand = [self.p(si)] if si else [os.path.join(base, '.spiceinit'), os.path.expanduser('~/.spiceinit')]
            spiceinit = ''
            for cpath in cand:
                if os.path.isfile(cpath):
                    with open(cpath) as f:
                        spiceinit = f.read()
                    break
            # cada simulacion corre en su propio proceso: 1 hilo por ngspice
            spiceinit = re.sub(r'(?m)^\s*set\s+numthreads\s*=.*$', '', spiceinit) + '\nset numthreads=1\n'
            self.tbs.append({'nombre': nombre, 'net': Netlist(text, base), 'spiceinit': spiceinit,
                             'path': path, 'extra': [str(x) for x in tb.get('control_extra', [])]})

    def _netlistear(self, nombre, tb):
        sch = self.p(tb['esquematico'])
        rc = self.p(tb['xschemrc']) if tb.get('xschemrc') else None
        outd = os.path.join(self.outdir or tempfile.mkdtemp(), 'netlists')
        os.makedirs(outd, exist_ok=True)
        cmd = ['xschem', '--preinit', 'proc option args {}']
        if rc:
            cmd += ['--rcfile', rc]
        cmd += ['-n', '-q', '-x', '-o', outd, '-N', f'{nombre}.spice', sch]
        cwd = os.path.dirname(rc) if rc else os.path.dirname(sch)
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        path = os.path.join(outd, f'{nombre}.spice')
        miss = [l for l in (p.stdout + p.stderr).splitlines() if 'Symbol not found' in l]
        if not os.path.isfile(path) or miss:
            raise SystemExit(f"[{nombre}] fallo el netlist de {sch}:\n" + '\n'.join(miss[:10] or [p.stderr[-2000:]]))
        # rutas relativas del testbench: relativas a la carpeta de simulacion de xschem
        base = os.path.join(cwd, 'simulations') if os.path.isdir(os.path.join(cwd, 'simulations')) else os.path.dirname(sch)
        return path, base

    # -- parametros -------------------------------------------------------------
    def _load_params(self):
        self.params, self.derivados = [], []
        for nombre, d in (self.cfg.get('parametros') or {}).items():
            prm = {'nombre': nombre.lower(), 'nombre_orig': nombre, 'targets': []}
            if 'param' in d:
                pname = d['param']
                for tb in self.tbs:
                    for i in tb['net'].find_param(pname):
                        prm['targets'].append((tb['nombre'], 'param', i, pname))
                if not prm['targets']:
                    raise SystemExit(f"parametro {nombre}: no encontre '.param {pname}' en ningun testbench")
            else:
                attr = d.get('atributo', 'W')
                for ref in d['instancias']:
                    found = False
                    for tb in self.tbs:
                        for i in tb['net'].find_instances(ref):
                            prm['targets'].append((tb['nombre'], 'inst', i, attr))
                            found = True
                    if not found:
                        raise SystemExit(f"parametro {nombre}: no encontre la instancia '{ref}' en los netlists")
                prm['instancias'] = list(d['instancias'])
                prm['atributo'] = attr
            prm['entero'] = d.get('tipo', '') in ('int', 'entero')
            if 'expr' in d:
                prm['expr'] = str(d['expr'])
                self.derivados.append(prm)
                continue
            prm['min'], prm['max'] = parse_si(d['min']), parse_si(d['max'])
            if prm['min'] >= prm['max']:
                raise SystemExit(f"parametro {nombre}: min >= max")
            esc = d.get('escala', 'lin' if prm['entero'] else 'log')
            prm['log'] = esc == 'log' and prm['min'] > 0
            prm['paso'] = parse_si(d['paso']) if 'paso' in d else None
            # valor inicial (el del netlist actual)
            tbn, kind, i, a = prm['targets'][0]
            net = self.tb(tbn)['net']
            raw = net.get_param(i, a) if kind == 'param' else net.get_attr(i, a)
            v0 = try_si(raw) if raw is not None else None
            prm['inicial'] = v0
            self.params.append(prm)
        if not self.params:
            raise SystemExit("no hay parametros para optimizar")
        self.dim = len(self.params)

    def tb(self, nombre):
        return next(t for t in self.tbs if t['nombre'] == nombre)

    def decode(self, u):
        vals = {}
        for k, prm in enumerate(self.params):
            x = min(1.0, max(0.0, float(u[k])))
            if prm['log']:
                v = math.exp(math.log(prm['min']) + x * (math.log(prm['max']) - math.log(prm['min'])))
            else:
                v = prm['min'] + x * (prm['max'] - prm['min'])
            if prm['paso']:
                v = round(v / prm['paso']) * prm['paso']
            if prm['entero']:
                v = round(v)
            vals[prm['nombre']] = min(prm['max'], max(prm['min'], v))
        for prm in self.derivados:
            v = eval_expr(prm['expr'], vals)
            vals[prm['nombre']] = round(v) if prm['entero'] else v
        return vals

    def encode(self, vals):
        u = np.empty(self.dim)
        for k, prm in enumerate(self.params):
            v = vals.get(prm['nombre'])
            if v is None:
                u[k] = 0.5
                continue
            v = min(prm['max'], max(prm['min'], v))
            if prm['log']:
                u[k] = (math.log(v) - math.log(prm['min'])) / (math.log(prm['max']) - math.log(prm['min']))
            else:
                u[k] = (v - prm['min']) / (prm['max'] - prm['min'])
        return u

    def seed_vals(self):
        return {p['nombre']: p['inicial'] for p in self.params if p['inicial'] is not None}

    # -- corners ----------------------------------------------------------------
    def _load_corners(self):
        cc = self.cfg.get('corners') or {}
        self.lib_patron = cc.get('lib_patron')
        self.corners = []
        for c in cc.get('lista', [{'nombre': 'nominal'}]):
            self.corners.append({'nombre': str(c['nombre']).lower(), 'lib': c.get('lib'),
                                 'temp': c.get('temp'), 'params': c.get('params') or {}})
        if self.lib_patron:
            for tb in self.tbs:
                if not any(l.strip().lower().startswith('.lib') and self.lib_patron.lower() in l.lower()
                           for l in tb['net'].logical):
                    print(f"  aviso: [{tb['nombre']}] no tiene una linea .lib con '{self.lib_patron}'")

    # -- especificaciones y objetivo ------------------------------------------
    def _load_specs(self):
        c = self.cfg
        self.medidas = {k.lower(): str(v) for k, v in (c.get('mediciones') or {}).items()}
        self.specs = []
        for expr, d in (c.get('especificaciones') or {}).items():
            d = d or {}
            self.specs.append({'expr': str(expr).lower(),
                               'min': try_si(d['min']) if 'min' in d else None,
                               'max': try_si(d['max']) if 'max' in d else None,
                               'corners': [x.lower() for x in d['corners']] if d.get('corners') else None})
        ob = c.get('objetivo') or {'expr': 'potencia', 'sentido': 'min'}
        self.obj_expr = str(ob['expr']).lower()
        self.obj_sign = 1.0 if str(ob.get('sentido', 'min')).lower().startswith('min') else -1.0

        # potencia y saturacion -> lineas que se inyectan al final del .control
        self.inject = {tb['nombre']: list(tb['extra']) for tb in self.tbs}
        pot = c.get('potencia')
        if pot:
            tbn = pot.get('testbench', self.tbs[0]['nombre']).lower()
            net = self.tb(tbn)['net']
            terms = []
            for src in pot['fuentes']:
                idx = net.find_instances(src)
                if not idx:
                    raise SystemExit(f"potencia: no encontre la fuente {src}")
                t = net.logical[idx[0]].split()
                n1, n2 = t[1].lower(), t[2].lower()
                v = lambda n: '0' if n in ('0', 'gnd') else f'v({n})'
                terms.append(f"(-i({t[0].lower()})*({v(n1)}-{v(n2)}))")
            self.inject[tbn] += ['op', f"let potencia = {' + '.join(terms)}", 'print potencia']
        self.sat = []
        sat = c.get('saturacion')
        if sat:
            tbn = sat.get('testbench', self.tbs[0]['nombre']).lower()
            net = self.tb(tbn)['net']
            plantilla = sat.get('plantilla', '@m.{path}.m{modelo}[{p}]')
            patron = sat.get('patron_dispositivo', r'fet|mos')
            excl = [x.lower() for x in sat.get('excluir', [])]
            margen = parse_si(sat.get('margen', 0.05))
            lines = [] if any(l == 'op' for l in self.inject[tbn]) else ['op']
            for path, modelo, sub, name in net.walk_devices(patron):
                if path in excl or name.lower() in excl or f"{sub}.{name.lower()}" in excl:
                    continue
                var = 'sat_' + path.replace('.', '_')
                vds = plantilla.format(path=path, modelo=modelo, p='vds')
                vdsat = plantilla.format(path=path, modelo=modelo, p='vdsat')
                lines += [f"let {var} = abs({vds}) - abs({vdsat})", f"print {var}"]
                self.sat.append(var)
                self.specs.append({'expr': var, 'min': margen, 'max': None, 'corners': None, 'auto': True})
            self.inject[tbn] += lines

    # -- construir netlists de un candidato -------------------------------------
    def build(self, vals, corner, recortar=None):
        texts = {}
        for tb in self.tbs:
            net = tb['net'].clone()
            for prm in self.params + self.derivados:
                v = vals[prm['nombre']]
                for tbn, kind, i, a in prm['targets']:
                    if tbn != tb['nombre']:
                        continue
                    if kind == 'param':
                        net.set_param(i, a, fmt_num(v, prm['entero']))
                    else:
                        net.set_attr(i, a, fmt_num(v, prm['entero']))
            if corner['lib'] and self.lib_patron:
                net.set_lib_corner(self.lib_patron, corner['lib'])
            if corner['temp'] is not None:
                net.set_temp(corner['temp'])
            for pn, pv in corner['params'].items():
                idx = net.find_param(pn)
                if not idx:
                    net._prepend_extra.append(f".param {pn}={pv}")
                for i in idx:
                    net.set_param(i, pn, str(pv))
            txt = net.render(quitar=self.quitar, inyectar=self.inject[tb['nombre']],
                             prepend=net._prepend_extra)
            if recortar if recortar is not None else self.recortar:
                txt = self._usar_recorte(tb['nombre'], txt)
            texts[tb['nombre']] = txt
        return texts

    _LIB_RE = re.compile(r'(?mi)^\s*\.lib\s+(["\']?)(\S+?)\1\s+(\S+)\s*$')

    def _usar_recorte(self, tbn, txt):
        """Reemplaza cada '.lib archivo seccion' por un .include de la version recortada."""
        def rep(m):
            path, sec = os.path.expanduser(m.group(2)), m.group(3)
            key = (tbn, path, sec.lower())
            if key not in self._recortes:
                body, n_all, n_keep = recortar_lib(path, sec, _ids(txt))
                d = os.path.join(self.outdir, 'modelos_recortados')
                os.makedirs(d, exist_ok=True)
                f = os.path.join(d, f"{tbn}_{os.path.basename(path)}_{sec}.spice")
                with open(f, 'w') as fh:
                    fh.write(body)
                self._recortes[key] = (f, n_all, n_keep)
            return f".include {self._recortes[key][0]}"
        return self._LIB_RE.sub(rep, txt)

    def validar_recorte(self, vals, tmpbase):
        """Simula el diseno con la libreria completa y la recortada; si todo da igual, activa el recorte."""
        if not self.alg['recortar_modelos']:
            return
        to = self.alg['timeout_sim'] or 300.0
        jobs, t = [], {}
        try:
            for c in self.corners:
                full, trim = self.build(vals, c, recortar=False), self.build(vals, c, recortar=True)
                for tbn in full:
                    if full[tbn] == trim[tbn]:
                        continue
                    sp = self.tb(tbn)['spiceinit']
                    jobs.append((c['nombre'], tbn, (full[tbn], tmpbase, sp, to), (trim[tbn], tmpbase, sp, to)))
        except Exception as e:
            print(f"  recorte de modelos desactivado ({e})")
            return
        if not jobs:
            return
        n_med, dif = 0, []
        t0 = time.time()
        for cn, tbn, jf, jt in jobs:
            a, _ = run_ngspice(jf)
            t1 = time.time()
            b, _ = run_ngspice(jt)
            t[cn] = (t1 - t0, time.time() - t1)
            t0 = time.time()
            n_med += len(a)
            for k in set(a) | set(b):
                va, vb = a.get(k), b.get(k)
                if va is None or vb is None or abs(va - vb) > 1e-6 * max(abs(va), abs(vb), 1e-30):
                    dif.append(f"{cn}/{tbn}/{k}: {va} vs {vb}")
        if dif or n_med == 0:
            print(f"  recorte de modelos DESACTIVADO: los resultados difieren ({'; '.join(dif[:3]) or 'sin mediciones'})")
            return
        self.recortar = True
        n_all = max(v[1] for v in self._recortes.values())
        n_keep = max(v[2] for v in self._recortes.values())
        tf = sum(x[0] for x in t.values()) / len(t)
        tr = sum(x[1] for x in t.values()) / len(t)
        print(f"  Libreria de modelos recortada ({n_all} -> {n_keep} elementos), validada: {n_med} mediciones "
              f"identicas. Simulacion {tf:.2f}s -> {tr:.2f}s")

    def area(self, vals):
        """Area activa aprox. sum(W*L*mult) de los transistores del primer testbench [um2]."""
        net = self.tbs[0]['net'].clone()
        for prm in self.params + self.derivados:
            for tbn, kind, i, a in prm['targets']:
                if tbn == self.tbs[0]['nombre'] and kind == 'inst':
                    net.set_attr(i, a, fmt_num(vals[prm['nombre']], prm['entero']))
        tot = 0.0
        for path, modelo, sub, name in net.walk_devices(r'fet|mos'):
            for i in net.find_instances(f"{sub}.{name}" if sub else name):
                w, l = try_si(net.get_attr(i, 'W')), try_si(net.get_attr(i, 'L'))
                m = try_si(net.get_attr(i, 'mult') or net.get_attr(i, 'm') or 1) or 1
                if w and l:
                    tot += w * l * m
                break
        return tot

    # -- evaluar resultados ------------------------------------------------------
    def score(self, vals, sims):
        """sims[corner][tb] = dict medidas. Devuelve (violacion, objetivo, detalle)."""
        viol, objs, det = 0.0, [], {}
        base_env = dict(vals)
        base_env['area'] = self.area(vals)
        for c in self.corners:
            env = dict(base_env)
            for tb in self.tbs:
                for k, v in sims[c['nombre']].get(tb['nombre'], {}).items():
                    env.setdefault(k, v)
                    env[f"{tb['nombre']}_{k}"] = v
            for k, e in self.medidas.items():
                try:
                    env[k] = eval_expr(e, env)
                except Exception:
                    pass
            d = {'_env': env, 'specs': []}
            for s in self.specs:
                if s['corners'] and c['nombre'] not in s['corners']:
                    continue
                try:
                    v = eval_expr(s['expr'], env)
                    if math.isnan(v):
                        raise ValueError
                except Exception:
                    v = None
                if v is None:
                    vi = 10.0
                else:
                    vi = 0.0
                    if s['min'] is not None and v < s['min']:
                        vi += (s['min'] - v) / (abs(s['min']) or 1.0)
                    if s['max'] is not None and v > s['max']:
                        vi += (v - s['max']) / (abs(s['max']) or 1.0)
                viol += vi
                d['specs'].append((s, v, vi))
            try:
                o = eval_expr(self.obj_expr, env)
            except Exception:
                o = float('inf') if self.obj_sign > 0 else float('-inf')
            d['objetivo'] = o
            objs.append(o)
            det[c['nombre']] = d
        # peor caso entre corners
        obj = max(objs) if self.obj_sign > 0 else min(objs)
        return viol, obj, det


def mejor(a, b, sign):
    """Reglas de Deb. a, b = (violacion, objetivo)."""
    va, oa = a
    vb, ob = b
    if va == 0 and vb == 0:
        return sign * oa <= sign * ob
    if va == 0 or vb == 0:
        return va == 0
    return va <= vb


# ----------------------------------------------------------------------------
# Optimizador
# ----------------------------------------------------------------------------
class Optimizador:
    def __init__(self, prob):
        self.pr = prob
        self.cache = {}
        self.n_eval = 0
        self.hist = []
        self.t0 = time.time()
        self.tmpbase = '/dev/shm' if os.path.isdir('/dev/shm') else None
        self.pool = ProcessPoolExecutor(max_workers=prob.alg['procesos'])
        self.csv_f = None

    def key(self, vals):
        return tuple(round(vals[p['nombre']], 9) for p in self.pr.params)

    def evaluate(self, vals_list, gen=0):
        pr = self.pr
        todo, jobs, meta = [], [], []
        for vals in vals_list:
            k = self.key(vals)
            if k in self.cache or k in todo:
                continue
            todo.append(k)
            for c in pr.corners:
                texts = pr.build(vals, c)
                for tbn, txt in texts.items():
                    jobs.append((txt, self.tmpbase, pr.tb(tbn)['spiceinit'], pr.alg['timeout_sim']))
                    meta.append((k, c['nombre'], tbn))
        if jobs:
            results = list(self.pool.map(run_ngspice, jobs, chunksize=1))
            sims = {}
            for (k, cn, tbn), (vals_out, errs) in zip(meta, results):
                sims.setdefault(k, {}).setdefault(cn, {})[tbn] = vals_out
                if errs:
                    sims[k].setdefault('_errs', []).extend(f"[{cn}/{tbn}] {e}" for e in errs[:2])
            for vals in vals_list:
                k = self.key(vals)
                if k in sims and k not in self.cache:
                    viol, obj, det = pr.score(vals, sims[k])
                    self.n_eval += 1
                    rec = {'id': self.n_eval, 'gen': gen, 'vals': dict(vals), 'viol': viol, 'obj': obj,
                           'det': det, 'errs': sims[k].get('_errs', [])}
                    self.cache[k] = rec
                    self._log(rec)
        return [self.cache[self.key(v)] for v in vals_list]

    def _log(self, rec):
        pr = self.pr
        if self.csv_f is None:
            return
        row = {'id': rec['id'], 'gen': rec['gen'], 'cumple': int(rec['viol'] == 0),
               'violacion': f"{rec['viol']:.4g}", 'objetivo_peor_corner': f"{rec['obj']:.6g}"}
        for p in pr.params + pr.derivados:
            row[p['nombre_orig']] = fmt_num(rec['vals'][p['nombre']], p['entero'])
        for cn, d in rec['det'].items():
            for s, v, vi in d['specs']:
                if s.get('auto'):
                    continue
                row[f"{cn}:{s['expr']}"] = '' if v is None else f"{v:.5g}"
            row[f"{cn}:objetivo"] = f"{d['objetivo']:.5g}"
        if self.csv_w is None:
            self.csv_cols = list(row.keys())
            self.csv_w = csv.DictWriter(self.csv_f, fieldnames=self.csv_cols, extrasaction='ignore')
            self.csv_w.writeheader()
        self.csv_w.writerow(row)
        self.csv_f.flush()

    def run(self, outdir):
        pr, a = self.pr, self.pr.alg
        self.csv_f = open(os.path.join(outdir, 'historial.csv'), 'w', newline='')
        self.csv_w = None
        rng = np.random.default_rng(a['semilla'])
        D = pr.dim
        NP = a['poblacion'] or int(min(60, max(16, 8 * D)))
        # poblacion inicial: hipercubo latino + diseno actual
        pop = np.empty((NP, D))
        for j in range(D):
            pop[:, j] = (rng.permutation(NP) + rng.random(NP)) / NP
        seed = pr.seed_vals()
        if seed:
            pop[0] = pr.encode(seed)
        recs = self.evaluate([pr.decode(u) for u in pop], gen=0)
        best_i = self._best_index(recs)
        hist_best = [(self.n_eval, recs[best_i]['viol'], recs[best_i]['obj'])]
        self._print_gen(0, recs, best_i)
        sin_mejora, gen = 0, 0
        while self.n_eval < a['max_evaluaciones'] and sin_mejora < a['generaciones_sin_mejora']:
            gen += 1
            trials = np.empty_like(pop)
            for i in range(NP):
                r1, r2 = rng.choice([j for j in range(NP) if j != i], 2, replace=False)
                F = rng.uniform(0.4, 0.9)
                CR = rng.uniform(0.6, 1.0)
                # DE/current-to-best/1/bin
                v = pop[i] + F * (pop[best_i] - pop[i]) + F * (pop[r1] - pop[r2])
                mask = rng.random(D) < CR
                mask[rng.integers(D)] = True
                t = np.where(mask, v, pop[i])
                # fuera de rango -> punto medio entre el padre y el borde
                low, high = t < 0, t > 1
                t[low] = pop[i][low] / 2
                t[high] = (pop[i][high] + 1) / 2
                trials[i] = t
            trecs = self.evaluate([pr.decode(u) for u in trials], gen=gen)
            prev = (recs[best_i]['viol'], recs[best_i]['obj'])
            for i in range(NP):
                if mejor((trecs[i]['viol'], trecs[i]['obj']), (recs[i]['viol'], recs[i]['obj']), pr.obj_sign):
                    pop[i], recs[i] = trials[i], trecs[i]
            best_i = self._best_index(recs)
            cur = (recs[best_i]['viol'], recs[best_i]['obj'])
            mejoro = (cur[0] < prev[0] - 1e-12) or (cur[0] == 0 and prev[0] > 0) or \
                     (cur[0] == 0 and pr.obj_sign * cur[1] < pr.obj_sign * prev[1] * (1 - 1e-4) - 1e-30)
            sin_mejora = 0 if mejoro else sin_mejora + 1
            hist_best.append((self.n_eval, cur[0], cur[1]))
            self._print_gen(gen, recs, best_i)
        self.csv_f.close()
        self.pool.shutdown()
        return recs[best_i], hist_best

    def _best_index(self, recs):
        bi = 0
        for i in range(1, len(recs)):
            if not mejor((recs[bi]['viol'], recs[bi]['obj']), (recs[i]['viol'], recs[i]['obj']), self.pr.obj_sign):
                bi = i
        return bi

    def _print_gen(self, gen, recs, bi):
        b = recs[bi]
        nf = sum(1 for r in recs if r['viol'] == 0)
        estado = f"CUMPLE  objetivo={fmt_eng(b['obj'])}" if b['viol'] == 0 else f"no cumple (violacion={b['viol']:.3g})"
        print(f"gen {gen:3d} | sims {self.n_eval * len(self.pr.corners) * len(self.pr.tbs):6d} | "
              f"disenos {self.n_eval:5d} | factibles en poblacion {nf:3d}/{len(recs)} | mejor: {estado} | "
              f"{time.time() - self.t0:6.0f}s", flush=True)


# ----------------------------------------------------------------------------
# Reportes
# ----------------------------------------------------------------------------
def reporte(pr, rec, titulo, inicial=None):
    L = [f"=== {titulo} ===", ""]
    L.append("Parametros:")
    for p in pr.params + pr.derivados:
        v = rec['vals'][p['nombre']]
        ini = ''
        if inicial is not None and p['nombre'] in inicial['vals']:
            ini = f"   (inicial: {fmt_num(inicial['vals'][p['nombre']], p['entero'])})"
        rango = f"[{fmt_num(p['min'])} .. {fmt_num(p['max'])}]" if 'min' in p else f"= {p['expr']}"
        L.append(f"  {p['nombre_orig']:<16} = {fmt_num(v, p['entero']):>12}   {rango}{ini}")
    L.append("")
    L.append(f"Area activa aprox (sum W*L*mult): {pr.area(rec['vals']):.1f} um2")
    L.append("")
    cn = [c['nombre'] for c in pr.corners]
    L.append(f"{'especificacion':<26}{'limite':<22}" + ''.join(f"{c:>14}" for c in cn))
    rows = {}
    for c in cn:
        for s, v, vi in rec['det'][c]['specs']:
            rows.setdefault(s['expr'], (s, {}))[1][c] = (v, vi)
    sat_fail = []
    for expr, (s, byc) in rows.items():
        if s.get('auto'):
            for c, (v, vi) in byc.items():
                if vi > 0:
                    sat_fail.append(f"{expr} en {c}: {v if v is None else round(v, 4)}")
            continue
        lim = ' y '.join(x for x in [f">= {fmt_eng(s['min'])}" if s['min'] is not None else '',
                                     f"<= {fmt_eng(s['max'])}" if s['max'] is not None else ''] if x)
        cells = ''
        for c in cn:
            if c not in byc:
                cells += f"{'-':>14}"
                continue
            v, vi = byc[c]
            cells += f"{('FALLA ' if vi > 0 else '') + ('sin dato' if v is None else fmt_eng(v)):>14}"
        L.append(f"{expr:<26}{lim:<22}{cells}")
    if pr.sat:
        n_mal = {c: sum(1 for s, v, vi in rec['det'][c]['specs'] if s.get('auto') and vi > 0) for c in cn}
        L.append(f"{'saturacion (' + str(len(pr.sat)) + ' transist.)':<26}{'|vds|-|vdsat| >= margen':<22}" +
                 ''.join(f"{('OK' if n_mal[c] == 0 else f'FALLAN {n_mal[c]}'):>14}" for c in cn))
    L.append(f"{'OBJETIVO: ' + pr.obj_expr:<26}{'min' if pr.obj_sign > 0 else 'max':<22}" +
             ''.join(f"{fmt_eng(rec['det'][c]['objetivo']):>14}" for c in cn))
    L.append("")
    L.append("RESULTADO: " + ("CUMPLE todas las especificaciones en todos los corners"
                              if rec['viol'] == 0 else f"NO cumple (violacion total {rec['viol']:.3g})"))
    if sat_fail:
        L.append("Transistores fuera de saturacion: " + '; '.join(sat_fail[:12]))
    if rec.get('errs'):
        L.append("Mensajes de ngspice: " + ' | '.join(rec['errs'][:6]))
    L.append("")
    L.append("Valores para cargar en el esquematico:")
    for p in pr.params + pr.derivados:
        v = fmt_num(rec['vals'][p['nombre']], p['entero'])
        if 'instancias' in p:
            L.append(f"  {', '.join(p['instancias']):<30} {p['atributo']}={v}")
        else:
            L.append(f"  .param {p['targets'][0][3]} = {v}")
    return '\n'.join(L)


def grafico(hist, pr, path):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return
    ev = [h[0] for h in hist]
    fig, ax = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    ax[0].plot(ev, [h[1] for h in hist], color='#c0392b')
    ax[0].set_ylabel('violacion (0 = cumple)')
    ax[0].set_yscale('symlog', linthresh=1e-3)
    ok = [(e, o) for e, v, o in hist if v == 0]
    if ok:
        ax[1].plot([e for e, _ in ok], [o for _, o in ok], color='#2471a3')
    ax[1].set_ylabel(f'objetivo ({pr.obj_expr}) peor corner')
    ax[1].set_xlabel('disenos evaluados')
    for a in ax:
        a.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)


def guardar_netlists(pr, rec, outdir):
    d = os.path.join(outdir, 'netlists_mejor')
    os.makedirs(d, exist_ok=True)
    for c in pr.corners:
        for tbn, txt in pr.build(rec['vals'], c, recortar=False).items():
            with open(os.path.join(d, f"{tbn}_{c['nombre']}.spice"), 'w') as f:
                f.write(txt)


# ----------------------------------------------------------------------------
# Aplicar al esquematico
# ----------------------------------------------------------------------------
def _sch_blocks(text):
    """Devuelve [(ini, fin)] de los bloques de atributos {..} de lineas 'C {sym} x y r f {attrs}'."""
    res = []
    for m in re.finditer(r'(?m)^C \{[^}]*\} \S+ \S+ \S+ \S+ \{', text):
        i, depth = m.end(), 1
        while i < len(text) and depth:
            ch = text[i]
            if ch == '\\':
                i += 2
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            i += 1
        res.append((m.end(), i - 1))
    return res


def aplicar(pr, resdir):
    with open(os.path.join(resdir, 'mejor.yaml')) as f:
        vals = {k.lower(): v for k, v in yaml.safe_load(f)['parametros'].items()}
    cambios = {}   # sch -> lista de (tipo, nombre, attr, valor)
    for p in pr.params + pr.derivados:
        v = fmt_num(vals[p['nombre']], p['entero'])
        tbn, kind, i, a = p['targets'][0]
        net = pr.tb(tbn)['net']
        if kind == 'param':
            sch = net.sch_of.get('')
            for t in pr.tbs:      # el .param puede estar en cualquier testbench
                for tt, kk, ii, aa in p['targets']:
                    if tt == t['nombre'] and t['net'].sch_of.get(''):
                        cambios.setdefault(t['net'].sch_of[''], []).append(('param', aa, None, v))
            continue
        for ref in p['instancias']:
            idx = net.find_instances(ref)
            sub = net.ctx[idx[0]]
            sch = net.sch_of.get(sub)
            name = net.logical[idx[0]].split()[0]
            attr = {'valor': 'value'}.get(p['atributo'], p['atributo'])
            cambios.setdefault(sch, []).append(('inst', name, attr, v))
    for sch, lst in cambios.items():
        if not sch or not os.path.isfile(sch):
            print(f"  no encuentro el .sch ({sch}); cambios: {lst}")
            continue
        with open(sch) as f:
            text = f.read()
        orig = text
        for kind, name, attr, v in lst:
            if kind == 'param':
                rx = re.compile(r'(\.param\s+' + re.escape(name) + r'\s*=\s*)(\S+)', re.I)
                text, n = rx.subn(lambda m: m.group(1) + v, text)
                print(f"  {os.path.basename(sch)}: .param {name} = {v}" + ('' if n else '  (NO ENCONTRADO)'))
                continue
            ok = False
            for ini, fin in reversed(_sch_blocks(text)):
                blk = text[ini:fin]
                nm = re.search(r'(?:^|\s)name=(\S+)', blk)
                if not nm or nm.group(1).lower() not in (name.lower(), name.lower()[1:]):
                    continue
                rx = re.compile(r'((?:^|\s)' + re.escape(attr) + r'=)(\S+)', re.I)
                if rx.search(blk):
                    blk = rx.sub(lambda m: m.group(1) + v, blk, count=1)
                else:
                    blk = blk.rstrip() + f"\n{attr}={v}"
                text = text[:ini] + blk + text[fin:]
                ok = True
                break
            print(f"  {os.path.basename(sch)}: {name} {attr}={v}" + ('' if ok else '  (NO ENCONTRADO)'))
        if text != orig:
            bak = sch + time.strftime('.%Y%m%d_%H%M%S.bak')
            shutil.copy2(sch, bak)
            with open(sch, 'w') as f:
                f.write(text)
            print(f"  -> guardado {sch}  (backup: {os.path.basename(bak)})")


# ----------------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(line_buffering=True)     # ver el progreso aunque la salida vaya a un archivo
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('config')
    ap.add_argument('--evaluar', action='store_true', help='solo evaluar el diseno actual')
    ap.add_argument('--aplicar', metavar='CARPETA_RESULTADO', help='escribir el mejor diseno en los .sch')
    args = ap.parse_args()

    cfg_dir = os.path.dirname(os.path.abspath(args.config))
    stamp = time.strftime('%Y%m%d_%H%M%S')
    outdir = os.path.join(cfg_dir, 'resultados', ('eval_' if args.evaluar else '') + stamp)
    if args.aplicar:
        outdir = tempfile.mkdtemp()
    os.makedirs(outdir, exist_ok=True)

    pr = Problema(args.config, outdir)
    print(f"Testbenches: {', '.join(t['nombre'] for t in pr.tbs)} | corners: "
          f"{', '.join(c['nombre'] for c in pr.corners)} | parametros: {pr.dim}"
          f"{' (+' + str(len(pr.derivados)) + ' derivados)' if pr.derivados else ''} | "
          f"especificaciones: {len([s for s in pr.specs if not s.get('auto')])}"
          f"{' + saturacion de ' + str(len(pr.sat)) + ' transistores' if pr.sat else ''}")

    if args.aplicar:
        aplicar(pr, os.path.abspath(args.aplicar))
        shutil.rmtree(outdir, ignore_errors=True)
        return

    opt = Optimizador(pr)
    seed = pr.seed_vals()
    faltan = [p['nombre_orig'] for p in pr.params if p['inicial'] is None]
    if faltan:
        print(f"  aviso: sin valor inicial legible para {faltan} (se usa el medio del rango)")
    vals0 = pr.decode(pr.encode(seed))
    for p in pr.params:    # el diseno actual tal cual (sin redondear al paso)
        if p['inicial'] is not None:
            vals0[p['nombre']] = p['inicial']
    pr.validar_recorte(vals0, opt.tmpbase)
    print("Simulando el diseno actual...")
    opt.csv_f = None
    auto_to = pr.alg['timeout_sim'] is None
    if auto_to:
        pr.alg['timeout_sim'] = 300.0
    t0 = time.time()
    ini = opt.evaluate([vals0])[0]
    if auto_to:
        # cada corner/testbench corre en paralelo: el tiempo total ~ una simulacion
        pr.alg['timeout_sim'] = max(5.0, 20 * (time.time() - t0))
    print(f"  limite por simulacion: {pr.alg['timeout_sim']:.0f} s (las que tarden mas cuentan como 'no cumple')")
    txt0 = reporte(pr, ini, 'DISENO ACTUAL')
    print('\n' + txt0 + '\n')
    with open(os.path.join(outdir, 'reporte_inicial.txt'), 'w') as f:
        f.write(txt0 + '\n')
    if args.evaluar:
        guardar_netlists(pr, ini, outdir)
        opt.pool.shutdown()
        print(f"Resultados en {outdir}")
        return

    print(f"Optimizando con {pr.alg['procesos']} procesos en paralelo "
          f"(max {pr.alg['max_evaluaciones']} disenos)...")
    best, hist = opt.run(outdir)
    if not mejor((best['viol'], best['obj']), (ini['viol'], ini['obj']), pr.obj_sign):
        print("Aviso: el diseno inicial resulto mejor que lo encontrado; se reporta el inicial.")
        best = ini
    txt = reporte(pr, best, 'MEJOR DISENO ENCONTRADO', inicial=ini)
    print('\n' + txt)
    with open(os.path.join(outdir, 'reporte.txt'), 'w') as f:
        f.write(txt + '\n')
    with open(os.path.join(outdir, 'mejor.yaml'), 'w') as f:
        yaml.safe_dump({'cumple': best['viol'] == 0, 'violacion': float(best['viol']),
                        'objetivo_peor_corner': float(best['obj']),
                        'parametros': {p['nombre_orig']: (int(best['vals'][p['nombre']]) if p['entero']
                                                          else float(f"{best['vals'][p['nombre']]:.6g}"))
                                       for p in pr.params + pr.derivados}},
                       f, sort_keys=False, allow_unicode=True)
    guardar_netlists(pr, best, outdir)
    grafico(hist, pr, os.path.join(outdir, 'convergencia.png'))
    print(f"\nResultados en {outdir}")
    print(f"Para escribirlo en el esquematico:  optimizar {args.config} --aplicar {outdir}")


if __name__ == '__main__':
    main()
