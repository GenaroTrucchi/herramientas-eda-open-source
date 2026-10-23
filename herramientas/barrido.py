#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
barrido.py - Barrido parametrico multi-corner con ngspice -> CSV + Excel.

Simula TODAS las combinaciones de los parametros que indiques, en cada corner,
en paralelo, y guarda una tabla con las mediciones pedidas.

Uso:
  barrido NETLIST.spice -p "w = 1 2 4 8" -p "iref = log(50u, 400u, 4)" \
          -c tt:27:vdd=1.8 -c ff:-40:vdd=1.98 -c ss:125:vdd=1.62 \
          -m "gm1=XM1[gm], id1=XM1[id], gmid=XM1[gm]/XM1[id], vout=v(net1)"

  -p  parametro a barrer (se puede repetir). Formato  OBJETIVO = VALORES
        OBJETIVO:  nombre de un .param            ej.  w = ...
                   instancias@atributo            ej.  XM1,XM2@W = ...   (W, L, mult, nf, valor)
                   (en subcircuitos: opamp.XM1@L;  M1 tambien sirve por XM1)
        VALORES:   lista                          1 2 4 8      (acepta 10u, 2.2p, 1k...)
                   inicio:paso:fin                0.15:0.05:0.5
                   lin(a, b, n) / log(a, b, n)    n puntos lineales / logaritmicos
  -c  corner  SECCION[:TEMP][:param=valor,...]   (igual que corners.py)
  -m  que medir, separado por comas (vacio = todo lo que imprime ngspice):
        nombre             algo que ngspice imprime como "nombre = valor" (meas, print)
        nombre=expresion   cuenta con esos valores, ej. pm_deg=pm*180/pi
        XM1[gm]            parametro de punto de operacion de un transistor (gm, id, gds,
                           vds, vgs, vth, vdsat, cgg...), se agrega 'op' automaticamente
        v(nodo), i(fuente) tension / corriente en el punto de operacion
  -e  comando extra para el .control (se puede repetir)
  -j  simulaciones en paralelo (default: nucleos - 2)
  --contar   solo informa cuantas simulaciones serian

Resultados en <carpeta del netlist>/barridos/<testbench>_<fecha>/:
  resultados.csv   una fila por (combinacion, corner)
  resultados.xlsx  hojas: todos | una por corner | resumen (min/max entre corners) | config
"""
import argparse
import csv
import itertools
import math
import os
import re
import shutil
import signal
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from optimizar import Netlist, parse_si, fmt_num, eval_expr, recortar_lib, _ids  # noqa: E402
from corners import parse_corner, correr  # noqa: E402

ATTR_ENTEROS = {'mult', 'm', 'nf'}


# ----------------------------------------------------------------------------
# Definicion del barrido
# ----------------------------------------------------------------------------
def parse_valores(s):
    s = s.strip()
    m = re.fullmatch(r'(lin|log)\s*\(\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*(\d+)\s*\)', s, re.I)
    if m:
        a, b, n = parse_si(m.group(2)), parse_si(m.group(3)), int(m.group(4))
        if n < 2:
            return [a]
        if m.group(1).lower() == 'log':
            if a <= 0 or b <= 0:
                raise ValueError(f"log() necesita valores positivos: {s}")
            vals = [a * (b / a) ** (k / (n - 1)) for k in range(n)]
        else:
            vals = [a + (b - a) * k / (n - 1) for k in range(n)]
    elif re.fullmatch(r'[^:\s]+:[^:\s]+:[^:\s]+', s):
        a, p, b = (parse_si(x) for x in s.split(':'))
        if p == 0 or (b - a) / p < 0:
            raise ValueError(f"paso invalido en {s}")
        n = int(math.floor((b - a) / p + 1e-9)) + 1
        if n > 100000:
            raise ValueError(f"demasiados puntos en {s}")
        vals = [a + k * p for k in range(n)]
    else:
        vals = [parse_si(x) for x in re.split(r'[,\s]+', s) if x]
    if not vals:
        raise ValueError(f"sin valores: {s!r}")
    return [float(f"{v:.12g}") for v in vals]


def parse_barrido(linea):
    if '=' not in linea:
        raise ValueError(f"falta '=' en: {linea!r}  (formato: OBJETIVO = VALORES)")
    obj, vals = linea.split('=', 1)
    obj = re.sub(r'\s+', '', obj)
    b = {'etiqueta': obj, 'valores': parse_valores(vals)}
    if '@' in obj:
        insts, attr = obj.split('@', 1)
        attr = {'value': 'valor', 'val': 'valor'}.get(attr.lower(), attr)
        b.update(tipo='inst', instancias=[x for x in insts.split(',') if x], atributo=attr,
                 entero=attr.lower() in ATTR_ENTEROS)
    else:
        b.update(tipo='param', nombre=obj, entero=False)
    return b


def resolver(net, b):
    """Busca en el netlist las lineas que toca el barrido b."""
    if b['tipo'] == 'param':
        idx = net.find_param(b['nombre'])
        if not idx:
            raise ValueError(f"no hay '.param {b['nombre']}' en el netlist")
        b['lineas'] = idx
        return
    b['lineas'] = []
    for ref in b['instancias']:
        cands = [ref]
        if '.' in ref:
            sub, n = ref.split('.', 1)
            cands.append(f"{sub}.x{n}")
        else:
            cands.append('x' + ref)
        idx = []
        for c in cands:
            idx = net.find_instances(c)
            if idx:
                break
        if not idx:
            nombres = sorted(set((f"{net.ctx[i]}." if net.ctx[i] else '') + net.logical[i].split()[0]
                                 for i in range(len(net.logical))
                                 if net.logical[i].split() and net.logical[i].split()[0][0].lower() in 'xmcrlqd'
                                 and not net.logical[i].lstrip().startswith(('*', '.'))))
            raise ValueError(f"no encontre la instancia '{ref}'. Hay: {', '.join(nombres[:40])}")
        b['lineas'] += idx


# ----------------------------------------------------------------------------
# Mediciones (con acceso al punto de operacion)
# ----------------------------------------------------------------------------
_DISP_RE = re.compile(r'([A-Za-z_][\w.]*)\[(\w+)\]')
_VI_RE = re.compile(r'\b([vViI])\(([^()]+)\)')


def preparar_mediciones(texto_medir, net, plantilla):
    """Devuelve (pedidos [(nombre, expr_python)], lineas_control_extra)."""
    pedidos = []
    for item in [x.strip() for x in (texto_medir or '').split(',') if x.strip()]:
        if '=' in item and not re.match(r'^[^=]*[\[(]', item.split('=', 1)[0]):
            n, e = item.split('=', 1)
            pedidos.append([n.strip(), e.strip()])
        else:
            pedidos.append([item, item])
    if not pedidos:
        return [], []
    devs = None
    lets, vars_ = [], {}

    def var_para(clave, expr_ng):
        if clave not in vars_:
            v = 'op_' + re.sub(r'\W+', '_', clave.lower()).strip('_')
            vars_[clave] = v
            lets.extend([f"let {v} = {expr_ng}", f"print {v}"])
        return vars_[clave]

    for p in pedidos:
        e = p[1]

        def rep_disp(m):
            nonlocal devs
            base, prm = m.group(1).lower(), m.group(2).lower()
            if devs is None:
                devs = net.walk_devices(r'fet|mos')
            cands = [d for d in devs if d[0] in (base, 'x' + base) or d[0].endswith('.' + base)
                     or d[0].endswith('.x' + base)]
            if not cands:
                raise ValueError(f"no encontre el transistor '{m.group(1)}'. Hay: "
                                 f"{', '.join(d[0] for d in devs)}")
            if len(cands) > 1:
                raise ValueError(f"'{m.group(1)}' es ambiguo: {', '.join(d[0] for d in cands)} "
                                 f"(usa la ruta completa, ej. {cands[0][0]}[{prm}])")
            path, modelo = cands[0][0], cands[0][1]
            return var_para(f"{path}_{prm}", plantilla.format(path=path, modelo=modelo, p=prm))

        def rep_vi(m):
            return var_para(f"{m.group(1)}_{m.group(2)}", f"{m.group(1).lower()}({m.group(2).strip().lower()})")

        e = _DISP_RE.sub(rep_disp, e)
        e = _VI_RE.sub(rep_vi, e)
        p[1] = e
    extra = (['op'] + lets) if lets else []
    return [tuple(p) for p in pedidos], extra


# ----------------------------------------------------------------------------
# Excel (.xlsx) sin dependencias
# ----------------------------------------------------------------------------
def _col(n):
    s = ''
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _hoja_xml(filas, filtro=True):
    ncol = max((len(f) for f in filas), default=1)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
           'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
           f'<cols><col min="1" max="{ncol}" width="15" customWidth="1"/></cols>', '<sheetData>']
    for r, fila in enumerate(filas, 1):
        cs = []
        for c, v in enumerate(fila, 1):
            ref = f"{_col(c)}{r}"
            st = ' s="1"' if r == 1 else ''
            if v is None or v == '':
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if math.isnan(v) or math.isinf(v):
                    continue
                cs.append(f'<c r="{ref}"{st}><v>{v!r}</v></c>')
            else:
                cs.append(f'<c r="{ref}"{st} t="inlineStr"><is><t xml:space="preserve">{escape(str(v))}</t></is></c>')
        out.append(f'<row r="{r}">{"".join(cs)}</row>')
    out.append('</sheetData>')
    if filtro and len(filas) > 1:
        out.append(f'<autoFilter ref="A1:{_col(ncol)}{len(filas)}"/>')
    out.append('</worksheet>')
    return '\n'.join(out)


def escribir_xlsx(path, hojas):
    """hojas: lista de (nombre, filas[lista de listas], filtro)."""
    usados, nombres = set(), []
    for n, _, _ in hojas:
        n = re.sub(r'[\[\]:*?/\\]', '_', n)[:31] or 'hoja'
        base, k = n, 2
        while n.lower() in usados:
            n = f"{base[:28]}_{k}"
            k += 1
        usados.add(n.lower())
        nombres.append(n)
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    ct += [f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
           for i in range(1, len(hojas) + 1)]
    ct.append('</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    defs = []
    for i, (n, (_, filas, filtro)) in enumerate(zip(nombres, hojas)):
        if filtro and len(filas) > 1:
            ncol = max(len(f) for f in filas)
            nq = n.replace("'", "''")
            defs.append(f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
                        f"'{escape(nq)}'!$A$1:${_col(ncol)}${len(filas)}</definedName>")
    wb = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{ns}" xmlns:r="{rns}"><sheets>'
          + ''.join(f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(nombres, 1))
          + '</sheets>' + (f'<definedNames>{"".join(defs)}</definedNames>' if defs else '') + '</workbook>')
    wbrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                        for i in range(1, len(hojas) + 1))
              + f'<Relationship Id="rId{len(hojas) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
              '</Relationships>')
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{ns}">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/><bgColor indexed="64"/></patternFill></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    tmp = path + '.tmp'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '\n'.join(ct))
        z.writestr('_rels/.rels', rels)
        z.writestr('xl/workbook.xml', wb)
        z.writestr('xl/_rels/workbook.xml.rels', wbrels)
        z.writestr('xl/styles.xml', styles)
        for i, (_, filas, filtro) in enumerate(hojas, 1):
            z.writestr(f'xl/worksheets/sheet{i}.xml', _hoja_xml(filas, filtro))
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Recorte de modelos (mismo mecanismo que el optimizador)
# ----------------------------------------------------------------------------
_LIB_RE = re.compile(r'(?mi)^\s*\.lib\s+(["\']?)(\S+?)\1\s+(\S+)\s*$')


def usar_recorte(txt, cache, carpeta):
    def rep(m):
        path, sec = os.path.expanduser(m.group(2)), m.group(3)
        key = (path, sec.lower())
        if key not in cache:
            body, _, _ = recortar_lib(path, sec, _ids(txt))
            os.makedirs(carpeta, exist_ok=True)
            f = os.path.join(carpeta, f"{os.path.basename(path)}_{sec}.spice")
            with open(f, 'w') as fh:
                fh.write(body)
            cache[key] = f
        return f".include {cache[key]}"
    return _LIB_RE.sub(rep, txt)


def iguales(a, b):
    if set(a) != set(b) or not a:
        return False
    return all(abs(a[k] - b[k]) <= 1e-6 * max(abs(a[k]), abs(b[k]), 1e-30) for k in a)


# ----------------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('netlist')
    ap.add_argument('-p', '--param', action='append', default=[], help='OBJETIVO = VALORES')
    ap.add_argument('-c', '--corner', action='append', default=[], help='SECCION[:TEMP][:param=v,...]')
    ap.add_argument('-m', '--medir', default='')
    ap.add_argument('-e', '--extra', action='append', default=[])
    ap.add_argument('-j', '--procesos', type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument('--lib-patron', default='sky130')
    ap.add_argument('--plantilla-disp', default='@m.{path}.m{modelo}[{p}]',
                    help='vector de ngspice para XM1[gm] (default: sky130)')
    ap.add_argument('--sin-recorte', action='store_true', help='no recortar la libreria de modelos')
    ap.add_argument('--timeout', type=float, default=None, help='s por simulacion (default: automatico)')
    ap.add_argument('--contar', action='store_true')
    a = ap.parse_args()

    try:
        barridos = [parse_barrido(x) for x in a.param if x.strip()]
        corners = [parse_corner(c) for c in a.corner if c.strip()] or [parse_corner('tt')]
    except ValueError as e:
        sys.exit(f"Error: {e}")
    n_comb = math.prod(len(b['valores']) for b in barridos) if barridos else 1
    total = n_comb * len(corners)
    if a.contar:
        detalle = ' x '.join(f"{len(b['valores'])} ({b['etiqueta']})" for b in barridos) or '1'
        print(f"{detalle} = {n_comb} combinaciones x {len(corners)} corners = {total} simulaciones")
        return

    path = os.path.abspath(os.path.expanduser(a.netlist))
    if not os.path.isfile(path):
        sys.exit(f"No existe el netlist: {path}  (hace Netlist en xschem primero)")
    base = os.path.dirname(path)
    with open(path) as f:
        texto = f.read()
    if 'IS MISSING' in texto:
        sys.exit("El netlist tiene simbolos faltantes ('IS MISSING'). Revisa el esquematico.")
    net = Netlist(texto, base)
    tb = os.path.splitext(os.path.basename(path))[0]
    try:
        for b in barridos:
            resolver(net, b)
        pedidos, op_lineas = preparar_mediciones(a.medir, net, a.plantilla_disp)
    except ValueError as e:
        sys.exit(f"Error: {e}")
    inyectar = list(a.extra) + op_lineas

    spiceinit = ''
    for cand in (os.path.join(base, '.spiceinit'), os.path.expanduser('~/.spiceinit')):
        if os.path.isfile(cand):
            with open(cand) as f:
                spiceinit = f.read()
            break
    spiceinit = re.sub(r'(?m)^\s*set\s+numthreads\s*=.*$', '', spiceinit) + '\nset numthreads=1\n'

    outdir = os.path.join(base, 'barridos', f"{tb}_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(outdir, exist_ok=True)
    quitar = {'plot', 'hardcopy', 'gnuplot', 'write', 'wrdata'}
    avisos = set()
    swept_params = {b['nombre'].lower() for b in barridos if b['tipo'] == 'param'}

    def construir(combo, c):
        n = net.clone()
        if n.set_lib_corner(a.lib_patron, c['lib']) == 0:
            avisos.add(f"no hay una linea .lib con '{a.lib_patron}': el corner {c['lib']} no se aplico")
        if c['temp'] is not None:
            n.set_temp(c['temp'])
        for k, v in c['params'].items():
            if k.lower() in swept_params:
                avisos.add(f"'{k}' esta en el corner y en el barrido: manda el barrido")
                continue
            idx = n.find_param(k)
            if not idx:
                avisos.add(f"el testbench no tiene '.param {k}': se agrego")
                n._prepend_extra.append(f".param {k}={v}")
            for i in idx:
                n.set_param(i, k, v)
        for b, v in zip(barridos, combo):
            s = fmt_num(v, b['entero'])
            for i in b['lineas']:
                if b['tipo'] == 'param':
                    n.set_param(i, b['nombre'], s)
                else:
                    n.set_attr(i, b['atributo'], s)
        return n.render(quitar=quitar, inyectar=inyectar, prepend=n._prepend_extra)

    combos = list(itertools.product(*[b['valores'] for b in barridos])) if barridos else [()]
    print(f"Barrido de {tb}: {n_comb} combinaciones x {len(corners)} corners = {total} simulaciones "
          f"({a.procesos} en paralelo)")
    for b in barridos:
        print(f"  {b['etiqueta']}: {len(b['valores'])} valores [{fmt_num(b['valores'][0])} .. {fmt_num(b['valores'][-1])}]")

    # recorte de modelos: se valida con la primera combinacion en cada corner
    recorte, cache = False, {}
    timeout = a.timeout or 300.0
    t_val = []
    if not a.sin_recorte and total >= 20:
        try:
            ok = True
            for c in corners:
                full = construir(combos[0], c)
                trim = usar_recorte(full, cache, os.path.join(outdir, 'modelos_recortados'))
                if trim == full:
                    ok = False
                    break
                r1, r2 = correr((full, spiceinit, timeout, None)), correr((trim, spiceinit, timeout, None))
                t_val.append(r2[2])
                if not iguales(r1[0], r2[0]):
                    ok = False
                    break
            recorte = ok
        except Exception as e:  # noqa: BLE001
            print(f"  (recorte de modelos no disponible: {e})")
        print("  Libreria de modelos recortada y validada: simulaciones ~20x mas rapidas" if recorte
              else "  Se usa la libreria de modelos completa")
    if a.timeout is None:
        timeout = max(10.0, 20 * max(t_val)) if t_val else 120.0

    for x in sorted(avisos):
        print("  aviso:", x)

    # correr
    res = {}
    stop = {'v': False}

    def on_term(*_):
        stop['v'] = True
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    t0 = time.time()
    hechos, ult = 0, 0.0
    incompleto = False
    ex = ThreadPoolExecutor(max_workers=max(1, a.procesos))
    try:
        futs = {}
        for k, combo in enumerate(combos):
            for ci, c in enumerate(corners):
                txt = construir(combo, c)
                if recorte:
                    txt = usar_recorte(txt, cache, os.path.join(outdir, 'modelos_recortados'))
                futs[ex.submit(correr, (txt, spiceinit, timeout, None))] = (k, ci)
        for f in as_completed(futs):
            res[futs[f]] = f.result()
            hechos += 1
            ahora = time.time()
            if ahora - ult > 2 or hechos == total:
                ult = ahora
                el = ahora - t0
                falta = el / hechos * (total - hechos)
                print(f"  {hechos}/{total} simulaciones ({100 * hechos / total:.0f}%)  "
                      f"{el:.0f} s, faltan ~{falta:.0f} s")
    except KeyboardInterrupt:
        incompleto = True
        print("\nCancelado: se guardan las simulaciones terminadas...")
        ex.shutdown(wait=False, cancel_futures=True)
    else:
        ex.shutdown()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    # armar tablas
    cpar = []
    for c in corners:
        for k in c['params']:
            if k not in cpar:
                cpar.append(k)
    if pedidos:
        mnames = [n for n, _ in pedidos]
    else:
        mnames = []
        for (meds, _, _) in res.values():
            for k in meds:
                if not k.startswith('op_') and k not in mnames:
                    mnames.append(k)

    def valores_medidos(meds):
        if not pedidos:
            return [meds.get(n) for n in mnames]
        out = []
        for _, e in pedidos:
            try:
                v = eval_expr(e, meds)
                out.append(None if (math.isnan(v) or math.isinf(v)) else v)
            except Exception:  # noqa: BLE001
                out.append(None)
        return out

    head = ['caso', 'corner', 'seccion', 'temp'] + cpar + [b['etiqueta'] for b in barridos] + mnames + ['errores']
    filas, por_corner = [], {ci: [] for ci in range(len(corners))}
    n_err = 0
    for k, combo in enumerate(combos):
        for ci, c in enumerate(corners):
            if (k, ci) not in res:
                continue
            meds, errs, _ = res[(k, ci)]
            mv = valores_medidos(meds)
            if errs:
                n_err += 1
            fila = ([k + 1, c['nombre'], c['lib'], c['temp']] + [_num(c['params'].get(p)) for p in cpar]
                    + list(combo) + mv + ['; '.join(errs[:3])])
            filas.append(fila)
            por_corner[ci].append([k + 1] + list(combo) + mv + ['; '.join(errs[:3])])

    out_csv = os.path.join(outdir, 'resultados.csv')
    with open(out_csv, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(head)
        for fila in filas:
            w.writerow(['' if v is None else (f"{v:.7g}" if isinstance(v, float) else v) for v in fila])

    hojas = [('todos', [head] + filas, True)]
    hc = ['caso'] + [b['etiqueta'] for b in barridos] + mnames + ['errores']
    for ci, c in enumerate(corners):
        hojas.append((c['nombre'], [hc] + por_corner[ci], True))
    # resumen: min / max de cada medicion entre corners
    hr = ['caso'] + [b['etiqueta'] for b in barridos]
    for m in mnames:
        hr += [f"{m} min", f"{m} max"]
    hr.append('corners con error')
    resumen = []
    for k, combo in enumerate(combos):
        vals_c = [valores_medidos(res[(k, ci)][0]) for ci in range(len(corners)) if (k, ci) in res]
        if not vals_c:
            continue
        fila = [k + 1] + list(combo)
        for j in range(len(mnames)):
            col = [v[j] for v in vals_c if v[j] is not None]
            fila += [min(col) if col else None, max(col) if col else None]
        fila.append(sum(1 for ci in range(len(corners)) if (k, ci) in res and res[(k, ci)][1]))
        resumen.append(fila)
    hojas.append(('resumen (min-max corners)', [hr] + resumen, True))
    cfg = [['configuracion', ''], ['testbench', path], ['fecha', time.strftime('%Y-%m-%d %H:%M:%S')],
           ['simulaciones', f"{len(res)} de {total}" + (' (INCOMPLETO)' if incompleto else '')]]
    cfg += [['barrido', b['etiqueta'] + ' = ' + ' '.join(fmt_num(v) for v in b['valores'])] for b in barridos]
    cfg += [['corner', c['nombre']] for c in corners]
    cfg += [['medir', a.medir or '(todo lo que imprime ngspice)']] + [['comando extra', x] for x in a.extra]
    hojas.append(('config', cfg, False))
    out_xlsx = os.path.join(outdir, 'resultados.xlsx')
    escribir_xlsx(out_xlsx, hojas)
    with open(os.path.join(outdir, 'config.txt'), 'w') as f:
        f.write('\n'.join(f"{r[0]}: {r[1]}" for r in cfg[1:]) + '\n')
    shutil.rmtree(os.path.join(outdir, 'modelos_recortados'), ignore_errors=True)

    print(f"\n{'INCOMPLETO: ' if incompleto else ''}{len(res)} simulaciones en {time.time() - t0:.0f} s"
          + (f"  ({n_err} con mensajes de error de ngspice, ver columna 'errores')" if n_err else ''))
    if pedidos:
        sin_dato = [n for j, n in enumerate(mnames) if all(f[4 + len(cpar) + len(barridos) + j] is None for f in filas)]
        if sin_dato:
            disp = sorted(set(k for (meds, _, _) in res.values() for k in meds if not k.startswith('op_')))
            print(f"Sin datos para: {', '.join(sin_dato)}. ngspice si imprimio: {', '.join(disp) or '(nada)'}")
    elif not mnames:
        print("ngspice no imprimio ningun valor 'nombre = valor': indica que medir (-m) o agrega meas/print.")
    print(f"CSV:   {out_csv}")
    print(f"Excel: {out_xlsx}")
    print(f"RESULTADOS_DIR={outdir}")


def _num(s):
    """'1.8' -> 1.8 (para que Excel lo tome como numero); si no es numero, lo deja como texto."""
    if s is None:
        return None
    try:
        return parse_si(s)
    except ValueError:
        return s


if __name__ == '__main__':
    main()
