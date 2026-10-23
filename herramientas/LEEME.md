# Herramientas de simulación del curso

| Herramienta | Desde xschem | Desde la terminal | Para qué |
|---|---|---|---|
| Corners | Simulation → *Simular en corners...* | `corners` | 3 corners en paralelo → tabla |
| Barrido | Simulation → *Barrido parametrico...* | `barrido` | todas las combinaciones de parámetros × corners → CSV + Excel |
| Optimizador | (todavía no) | `optimizar archivo.yaml` | busca W/L/C/I que cumplan specs con mínimo consumo |

## Barrido paramétrico

En la ventana (o con `-p` en la terminal), un parámetro por línea:

```
w = 1 2 4 8                      .param con lista de valores (acepta 10u, 2.2p, 1k...)
l = 0.15:0.05:0.5                inicio:paso:fin
iref = log(10u, 1m, 7)           7 puntos logarítmicos   (lin(a, b, n) = lineales)
M1,M2@W = 1:1:10                 atributo de instancias: W, L, mult, nf, valor
opamp.M1,opamp.M2@L = 0.15 0.3   instancias dentro de un subcircuito
```

Corners, uno por línea: `tt:27:vdd=1.8` (sección : temperatura : .param a cambiar).

Qué medir (separado por comas, vacío = todo lo que imprime ngspice):

- `gbw, dcg`: lo que el testbench imprime con `meas` / `print`
- `pm_deg=pm*180/pi`: cuentas con esos valores
- `gm1=M1[gm], gmid=M1[gm]/M1[id]`: punto de operación de un transistor (gm, id, gds, vds, vgs, vth, vdsat, cgg...)
- `vout=v(vout), pot=-i(vdd)*v(vdd)`: tensiones y corrientes del punto de operación

Resultados en `simulations/barridos/<testbench>_<fecha>/`:

- `resultados.xlsx`: hojas *todos* (una fila por combinación y corner), una por corner,
  *resumen* (mínimo y máximo de cada medición entre corners) y *config*. Usá este en Excel.
- `resultados.csv`: lo mismo con punto decimal, para Python/pandas/Octave
  (el Excel en español lo lee mal: usá el `.xlsx`).

Cancelar a mitad de camino guarda lo que ya se simuló.

---

# Optimizador de dimensionamiento (W / L / mult / C / corrientes)

Busca la configuración de **menor consumo** (u otro objetivo) que **cumpla todas las
especificaciones en todos los corners** (TT, FF, SS...), corriendo ngspice miles de
veces en paralelo.

## Uso rápido

```bash
cd ~/sky130/Curso_DACI/optimizacion
optimizar mi_diseno.yaml --evaluar     # 1) ver cómo está el diseño actual en cada corner
optimizar mi_diseno.yaml               # 2) optimizar
optimizar mi_diseno.yaml --aplicar resultados/<carpeta>   # 3) escribir el resultado en los .sch
```

Para un diseño nuevo: copiá `ejemplo_TP_FINAL.yaml`, cambiá testbench, instancias,
rangos y especificaciones. El diseño actual del esquemático siempre entra como punto
de partida, así que el resultado nunca es peor que lo que ya tenías.

## Qué hace

1. Netlistea tus testbenches con xschem (una sola vez).
2. Para cada diseño candidato cambia en el netlist los W, L, mult, C y `.param`
   que declaraste, y lo simula en cada corner. Por corner cambia la sección del `.lib`,
   la temperatura y los `.param`, por ejemplo VDD.
3. Lee lo que ngspice imprime como `nombre = valor` (los `meas` y `print` de tu `.control`),
   además de la potencia y la saturación que agrega solo.
4. **Evolución diferencial** con reglas de factibilidad:
   - un diseño que cumple todo siempre le gana a uno que no cumple;
   - entre dos que no cumplen, gana el que viola menos (violación normalizada);
   - entre dos que cumplen, gana el de mejor objetivo **en el peor corner**.
5. Se detiene al llegar a `max_evaluaciones` o tras `generaciones_sin_mejora`.

**Velocidad:** leer la librería completa de sky130 le lleva a ngspice ~1,3 s por simulación,
y la simulación en sí son centésimas. Por eso, al arrancar, el optimizador arma por corner
una librería **recortada** con solo los dispositivos que usa tu circuito, simula el diseño
actual con las dos y la usa únicamente si todas las mediciones dan idénticas. Así una
simulación pasa de ~1,3 s a ~0,07 s. Los netlists de `netlists_mejor/` usan siempre la
librería completa. Se desactiva con `recortar_modelos: false`.

## Resultados (`resultados/<fecha>/`)

| archivo | contenido |
|---|---|
| `reporte_inicial.txt` | el diseño actual, corner por corner |
| `reporte.txt` | el mejor diseño: parámetros, tabla por corner, valores para el esquemático |
| `mejor.yaml` | los parámetros óptimos (los usa `--aplicar`) |
| `historial.csv` | **todos** los diseños simulados (abrible en Excel/pandas) |
| `convergencia.png` | evolución de la violación y del objetivo |
| `netlists_mejor/` | netlists finales por testbench y corner, para revisar a mano |

`--aplicar` modifica los `.sch` (subcircuito y `.param` del testbench) y deja un `.bak`
con fecha al lado de cada archivo.

## Formato del YAML

```yaml
testbenches:                       # uno o varios
  - nombre: ol
    esquematico: ../sch/TP_FINAL/tb_opamp_openloop.sch
    xschemrc: ../sch/xschemrc
    # netlist: ruta.spice          # alternativa: un netlist ya hecho
    # control_extra: ["meas ac f3db when vdb(vout)=-3"]   # comandos extra en el .control

corners:
  lib_patron: sky130.lib           # (IHP: cornerMOSlv.lib, con lib: mos_tt / mos_ff / mos_ss)
  lista:
    - {nombre: tt, lib: TT, temp: 27,  params: {vdd: 1.8}}
    - {nombre: ff, lib: FF, temp: -40, params: {vdd: 1.98}}
    - {nombre: ss, lib: SS, temp: 125, params: {vdd: 1.62}}

parametros:
  W_dif: {instancias: [opamp.XM1, opamp.XM2], atributo: W, min: 2, max: 100, paso: 0.01}
  m_dif: {instancias: [opamp.XM1, opamp.XM2], atributo: mult, min: 1, max: 40, tipo: int}
  Cc:    {instancias: [opamp.C1], atributo: valor, min: 0.5p, max: 20p}
  iref:  {param: iref, min: 5u, max: 150u}
  W_M8:  {instancias: [opamp.XM8], atributo: W, expr: "w_dif/2"}     # derivado (no se optimiza)
```

- `instancias`: el nombre **en el netlist**. El `M1` dentro de `opamp.sch` es `opamp.XM1`.
  Todas las instancias de una misma lista quedan con el mismo valor (útil para apareamiento y espejos).
- `escala`: `log` (por defecto, conviene para W, L, C, I) o `lin`. `paso`: grilla (0.01 µm, por ejemplo).
- Unidades SPICE aceptadas: `5u`, `20p`, `50e6`, `2meg`...

```yaml
mediciones:                        # métricas derivadas (Python, sin mayúsculas)
  pm_deg: "pm*180/pi"
  fom: "gbw*20e-12/potencia"

potencia:   {testbench: ol, fuentes: [V2]}           # -> métrica 'potencia' [W]
saturacion: {testbench: ol, margen: 0.05, excluir: [x1.xm9]}
#   -> exige |vds|-|vdsat| >= margen en cada transistor y en cada corner.
#      Excluí los que trabajan en triodo a propósito (transistores-resistencia).

especificaciones:                  # se cumplen en TODOS los corners...
  dcg:    {min: 50}
  gbw:    {min: 50e6}
  pm_deg: {min: 60, max: 90}
  area:   {max: 15000}             # 'area' = sum(W*L*mult) en um2, siempre disponible
  sr:     {min: 10e6, corners: [ss]}   # ...o solo en algunos

objetivo: {expr: potencia, sentido: min}     # o {expr: fom, sentido: max}

algoritmo:
  max_evaluaciones: 6000           # diseños (cada uno = 1 simulación por corner y testbench)
  generaciones_sin_mejora: 40
  procesos: 18                     # simulaciones en paralelo
  semilla: 1                       # misma semilla -> mismo resultado
  # poblacion: 60                  # por defecto 8 x n_parametros (entre 16 y 60)
  # timeout_sim: 10                # s; por defecto 20x lo que tarda el diseño actual (mín. 5 s).
  #                                # Una simulación que no converge a tiempo cuenta como "no cumple"
  # recortar_modelos: true
```

Nombres disponibles en las expresiones: lo que imprime ngspice (en minúscula), también
como `<testbench>_<nombre>` si hay varios testbenches; `potencia`, `area`, `sat_x1_xm1`...,
los parámetros (`w_dif`, `iref`...) y `sqrt, log10, db, abs, min, max, pi`.

## Consejos

- Empezá con `--evaluar` y revisá que las mediciones salgan bien en los 3 corners.
- Si un `meas` falla (por ejemplo, la ganancia nunca cruza 0 dB), ese diseño cuenta como
  "no cumple". El optimizador lo descarta solo.
- Menos parámetros convergen más rápido: fijá lo que ya sabés y agrupá los transistores
  apareados.
- Para cerrar el diseño, corré Monte Carlo o un barrido de temperatura sobre el resultado.
