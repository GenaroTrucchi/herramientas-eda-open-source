#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
corners.py - Simula un netlist en varios corners en paralelo y muestra una tabla
con las mediciones pedidas.

Uso:
  corners NETLIST.spice -c tt:27:vdd=1.8 -c ff:-40:vdd=1.98 -c ss:125:vdd=1.62 \
          -m "gbw, dcg, pm_deg=pm*180/pi"

  -c  corner como  SECCION[:TEMP][:param=valor,param=valor]
        SECCION = la seccion de la libreria de modelos (tt, ff, ss, sf, fs...)
        TEMP    = temperatura en grados C (opcional)
        params  = .param a cambiar, ej. vdd=1.98 (opcional)
  -m  que mostrar, separado por comas. Cada item es:
        nombre          algo que ngspice imprime como "nombre = valor" (meas, print)
        nombre=expr     una cuenta con esos valores, ej. pm_deg=pm*180/pi
      Si no se pone -m, muestra todo lo que ngspice imprime.
  -e  comando extra para el .control (se puede repetir), ej.
        -e "meas ac f3db when vdb(vout)=-3"

Las simulaciones corren en carpetas temporales; el netlist original no se toca.
"""
import argparse
import csv
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from optimizar import Netlist, parse_output, eval_expr, fmt_eng, _ENV_1HILO  # noqa: E402


def parse_corner(s):
    partes = s.split(':')
    c = {'lib': partes[0].strip(), 'temp': None, 'params': {}}
    for p in partes[1:]:
        p = p.strip()
        if not p:
            continue
        if '=' in p:
            for kv in p.split(','):
                k, v = kv.split('=', 1)
                c['params'][k.strip()] = v.strip()
        else:
            c['temp'] = float(p)
    nombre = c['lib'].upper()
    if c['temp'] is not None:
        nombre += f" {c['temp']:g}C"
    for k, v in c['params'].items():
        nombre += f" {k}={v}"
    c['nombre'] = nombre
    return c


def parse_pedidos(s):
    out = []
    for item in [x.strip() for x in (s or '').split(',') if x.strip()]:
        if '=' in item:
            n, e = item.split('=', 1)
            out.append((n.strip(), e.strip()))
        else:
            out.append((item, item))
    return out


def correr(job):
    texto, spiceinit, timeout, guardar_en = job
    d = tempfile.mkdtemp(prefix='corner_', dir='/dev/shm' if os.path.isdir('/dev/shm') else None)
    t0 = time.time()
    try:
        with open(os.path.join(d, 'tb.spice'), 'w') as f:
            f.write(texto)
        with open(os.path.join(d, '.spiceinit'), 'w') as f:
            f.write(spiceinit)
        try:
            p = subprocess.run(['ngspice', '-b', 'tb.spice'], cwd=d, capture_output=True, text=True,
                               timeout=timeout, errors='replace', env=_ENV_1HILO)
            out = p.stdout + '\n' + p.stderr
        except subprocess.TimeoutExpired:
            out = f'TIMEOUT: la simulacion tardo mas de {timeout:g} s'
        if guardar_en:
            os.makedirs(guardar_en, exist_ok=True)
            for f in os.listdir(d):
                if f.endswith('.raw'):
                    shutil.copy(os.path.join(d, f), os.path.join(guardar_en, f))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    errs = []
    for l in out.splitlines():
        if re.search(r'(?i)\berror\b|could not|unknown subckt|TIMEOUT|singular|no convergence', l):
            l = l.strip()
            if l and l not in errs:
                errs.append(l)
    return parse_output(out), errs, time.time() - t0


def main():
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('netlist')
    ap.add_argument('-c', '--corner', action='append', required=True, help='SECCION[:TEMP][:param=v,...]')
    ap.add_argument('-m', '--medir', default='', help='mediciones separadas por coma (vacio = todas)')
    ap.add_argument('-e', '--extra', action='append', default=[], help='comando extra para el .control')
    ap.add_argument('--lib-patron', default='sky130', help="linea .lib donde se cambia el corner (default: 'sky130')")
    ap.add_argument('--guardar-raw', action='store_true', help='copiar los .raw de cada corner a <netlist_dir>/corners/')
    ap.add_argument('--timeout', type=float, default=300)
    args = ap.parse_args()

    path = os.path.abspath(os.path.expanduser(args.netlist))
    if not os.path.isfile(path):
        sys.exit(f"No existe el netlist: {path}\n(hace Netlist en xschem primero)")
    base = os.path.dirname(path)
    with open(path) as f:
        texto = f.read()
    if 'IS MISSING' in texto:
        sys.exit("El netlist tiene simbolos faltantes ('IS MISSING'). Revisa el esquematico.")
    net = Netlist(texto, base)
    nombre_tb = os.path.splitext(os.path.basename(path))[0]

    spiceinit = ''
    for cand in (os.path.join(base, '.spiceinit'), os.path.expanduser('~/.spiceinit')):
        if os.path.isfile(cand):
            with open(cand) as f:
                spiceinit = f.read()
            break
    spiceinit = re.sub(r'(?m)^\s*set\s+numthreads\s*=.*$', '', spiceinit) + '\nset numthreads=1\n'

    corners = [parse_corner(c) for c in args.corner]
    pedidos = parse_pedidos(args.medir)
    quitar = {'plot', 'hardcopy', 'gnuplot'} | (set() if args.guardar_raw else {'write', 'wrdata'})

    jobs, avisos = [], []
    for c in corners:
        n = net.clone()
        if n.set_lib_corner(args.lib_patron, c['lib']) == 0:
            avisos.append(f"no encontre una linea .lib con '{args.lib_patron}': el corner {c['lib']} no se aplico")
        if c['temp'] is not None:
            n.set_temp(c['temp'])
        for k, v in c['params'].items():
            idx = n.find_param(k)
            if not idx:
                avisos.append(f"[{c['nombre']}] el testbench no tiene '.param {k}': se agrego")
                n._prepend_extra.append(f".param {k}={v}")
            for i in idx:
                n.set_param(i, k, v)
        txt = n.render(quitar=quitar, inyectar=args.extra, prepend=n._prepend_extra)
        dest = os.path.join(base, 'corners', nombre_tb, re.sub(r'[^\w.=-]+', '_', c['nombre'])) if args.guardar_raw else None
        jobs.append((txt, spiceinit, args.timeout, dest))

    print(f"Simulando {nombre_tb} en {len(corners)} corners en paralelo: {', '.join(c['nombre'] for c in corners)}")
    for a in sorted(set(avisos)):
        print("  aviso:", a)
    t0 = time.time()
    with ThreadPoolExecutor(len(jobs)) as ex:
        res = list(ex.map(correr, jobs))
    print(f"Listo en {time.time() - t0:.1f} s\n")

    # tabla
    filas = []
    if pedidos:
        for nom, expr in pedidos:
            vals = []
            for (meds, errs, _), c in zip(res, corners):
                try:
                    v = eval_expr(expr, meds)
                    vals.append(None if math.isnan(v) else v)
                except Exception:
                    vals.append(None)
            filas.append((nom, vals))
    else:
        nombres = []
        for meds, _, _ in res:
            for k in meds:
                if k not in nombres:
                    nombres.append(k)
        filas = [(k, [meds.get(k) for meds, _, _ in res]) for k in nombres]

    ancho0 = max([len('medicion')] + [len(f[0]) for f in filas])
    anchos = [max(12, len(c['nombre'])) for c in corners]
    print(f"{'medicion':<{ancho0}}  " + '  '.join(f"{c['nombre']:>{w}}" for c, w in zip(corners, anchos)))
    print('-' * (ancho0 + 2 + sum(w + 2 for w in anchos)))
    for nom, vals in filas:
        print(f"{nom:<{ancho0}}  " + '  '.join(f"{('---' if v is None else fmt_eng(v)):>{w}}" for v, w in zip(vals, anchos)))
    if not filas:
        print("(ngspice no imprimio ningun valor 'nombre = valor': agrega meas/print al .control o con -e)")

    hubo_err = False
    for (meds, errs, dt), c in zip(res, corners):
        if errs:
            if not hubo_err:
                print("\nMensajes de ngspice:")
                hubo_err = True
            for e in errs[:6]:
                print(f"  [{c['nombre']}] {e}")
    faltan = [nom for nom, vals in filas if any(v is None for v in vals)]
    if faltan and pedidos:
        disp = sorted(set(k for meds, _, _ in res for k in meds))
        print(f"\n'---' = no disponible. Valores que si imprimio ngspice: {', '.join(disp) or '(ninguno)'}")

    # guardar
    out_csv = os.path.join(base, f"corners_{nombre_tb}.csv")
    with open(out_csv, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['medicion'] + [c['nombre'] for c in corners])
        for nom, vals in filas:
            w.writerow([nom] + ['' if v is None else f"{v:.6g}" for v in vals])
    print(f"\nTabla guardada en {out_csv}")
    if args.guardar_raw:
        print(f"Archivos .raw de cada corner en {os.path.join(base, 'corners', nombre_tb)}/")


if __name__ == '__main__':
    main()
