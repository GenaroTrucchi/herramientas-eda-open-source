#!/bin/bash
# importar_vm.sh - Importa el PDK sky130 (2021) y el curso DACI desde el disco de la
# maquina virtual "icdesign" (VirtualBox) a WSL, sin arrancar la VM.
#
# Uso:
#   importar_vm.sh "C:\ruta\a\icdesign.vdi" [--tmp CARPETA]
#   importar_vm.sh --img PARTICION.img            (si la particion ya esta extraida)
#
#   --tmp CARPETA     donde extraer la particion de Linux (~32 GB temporales).
#                     Por defecto, la misma carpeta del .vdi.
#   --usuario-vm U    usuario de la VM (default: designer)
#
# Resultado:
#   ~/pdks/sky130A_2021/   PDK, simbolos xschem y modelos ngspice de la VM
#   ~/sky130/Curso_DACI/   sch/ (TPs), mag/, escritorio_vm/, vm_config/
set -euo pipefail

VDI="" IMG="" TMPD="" USR_VM=designer
while [ $# -gt 0 ]; do
    case "$1" in
        --tmp) TMPD=$2; shift 2 ;;
        --img) IMG=$2; shift 2 ;;
        --usuario-vm) USR_VM=$2; shift 2 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) VDI=$1; shift ;;
    esac
done

win2lin() {   # C:\algo -> /mnt/c/algo
    case "$1" in
        [A-Za-z]:\\*|[A-Za-z]:/*) wslpath -u "$1" ;;
        *) echo "$1" ;;
    esac
}

PDK=$HOME/pdks/sky130A_2021
CURSO=$HOME/sky130/Curso_DACI
H=/home/$USR_VM

if [ -e "$CURSO" ]; then
    echo "Ya existe $CURSO: movelo o borralo antes de importar (no se sobrescribe)."
    exit 1
fi
command -v debugfs >/dev/null || { echo "Falta debugfs:  sudo apt install e2fsprogs"; exit 1; }
command -v python3 >/dev/null || { echo "Falta python3:  sudo apt install python3"; exit 1; }

# ---- 1) particion de Linux del disco virtual ----------------------------------------
BORRAR_IMG=0
if [ -z "$IMG" ]; then
    [ -n "$VDI" ] || { sed -n '2,16p' "$0"; exit 1; }
    VDI=$(win2lin "$VDI")
    [ -f "$VDI" ] || { echo "No existe el disco virtual: $VDI"; exit 1; }
    Z=$(command -v 7zz || command -v 7z || true)
    [ -n "$Z" ] || { echo "Falta 7-Zip:  sudo apt install 7zip"; exit 1; }
    TMPD=$(win2lin "${TMPD:-$(dirname "$VDI")}")
    # la particion mas grande del disco es la de Linux (ext4)
    read -r PART SIZE < <("$Z" l -slt "$VDI" | awk '
        /^Path = /{p=substr($0,8)} /^Size = /{s=$3+0; if (p ~ /\.img$/ && s > max) {max=s; best=p}}
        END{print best, max}')
    [ -n "$PART" ] || { echo "No encontre una particion de Linux en $VDI"; exit 1; }
    AVAIL=$(df -B1 --output=avail "$TMPD" | tail -1)
    if [ "$AVAIL" -lt $((SIZE + 1000000000)) ]; then
        echo "Hacen falta ~$((SIZE / 1024**3 + 1)) GB libres en $TMPD (hay $((AVAIL / 1024**3)) GB)."
        echo "Usa --tmp con otra carpeta/disco, ej: --tmp /mnt/d"
        exit 1
    fi
    mkdir -p "$TMPD/vm_extraida"
    echo "Extrayendo la particion de Linux de la VM ($((SIZE / 1024**3)) GB, tarda unos minutos)..."
    "$Z" e "$VDI" "$PART" -o"$TMPD/vm_extraida" -y -bsp0 -bso0
    IMG="$TMPD/vm_extraida/$PART"
    BORRAR_IMG=1
fi
IMG=$(win2lin "$IMG")
[ -f "$IMG" ] || { echo "No existe $IMG"; exit 1; }

# debugfs lee el ext4 sin montarlo (-c: solo lectura, tolera que la VM no se apago limpia)
d()  { debugfs -c -R "$1" "$IMG" 2>/dev/null; }
rd() { mkdir -p "$2"; debugfs -c -R "rdump \"$1\" \"$2\"" "$IMG" 2>&1 \
         | grep -viE 'debugfs [0-9]|catastrophic|checksum|ownership|^\s*$' || true; }
d "ls $H/skywater" | grep -q pdk || { echo "No encuentro $H/skywater/pdk en el disco: es la VM icdesign? (--usuario-vm)"; exit 1; }

mkdir -p "$HOME/.cache"
ST=$(mktemp -d "$HOME/.cache/importar_vm.XXXX")
trap 'rm -rf "$ST"' EXIT

# ---- 2) PDK ---------------------------------------------------------------------------
if [ -d "$PDK/pdk" ]; then
    echo "Ya existe $PDK: se reutiliza."
else
    echo "Copiando el PDK sky130 2021 (~3.5 GB)..."
    rd "$H/skywater/pdk" "$ST/pdk"
    rd "$H/skywater/xschem_sky130" "$ST/pdk"
    rd "$H/skywater/skywater-pdk/libraries/sky130_fd_pr_ngspice/latest" "$ST/modelos"
    mkdir -p "$PDK/skywater-pdk/libraries/sky130_fd_pr_ngspice"
    mv "$ST/pdk/pdk" "$PDK/pdk"
    mv "$ST/pdk/xschem_sky130" "$PDK/xschem_sky130"
    mv "$ST/modelos/latest" "$PDK/skywater-pdk/libraries/sky130_fd_pr_ngspice/latest"
    chmod -R u+w "$PDK"
    # el PDK se compilo en la VM con rutas absolutas a /home/<usuario>/skywater/pdk
    grep -rlF "$H/skywater/pdk" "$PDK/pdk" | while IFS= read -r f; do
        sed -i "s#$H/skywater/pdk#$PDK/pdk#g" "$f"
    done
fi

# ---- 3) curso ---------------------------------------------------------------------------
echo "Copiando el curso (icdesign/sch, icdesign/mag, Escritorio)..."
mkdir -p "$CURSO/vm_config"
rd "$H/icdesign/sch" "$CURSO"
rd "$H/icdesign/mag" "$CURSO"
if d "ls $H/Escritorio" | grep -q .; then
    rd "$H/Escritorio" "$ST/esc"
    mv "$ST/esc/Escritorio" "$CURSO/escritorio_vm"
fi
for f in xschemrc .bashrc; do rd "$H/$f" "$CURSO/vm_config"; done
chmod -R u+w "$CURSO"
cp "$CURSO/sch/xschemrc" "$CURSO/vm_config/xschemrc_sch_original"

# ---- 4) adaptar rutas y el xschemrc al entorno nuevo -------------------------------------
python3 - "$USR_VM" "$CURSO" "$PDK" <<'PY'
import os, re, sys
usr, curso, pdk = sys.argv[1:4]
home_vm = f'/home/{usr}'
# todos los archivos de texto del curso (esquematicos, simbolos, netlists, magicrc, notas...;
# hay TPs con nombres sin extension, ej. "tp2_3bsch"): rutas de la VM -> rutas nuevas
reps = [('~/skywater/', '~/pdks/sky130A_2021/'), (f'{home_vm}/skywater', pdk),
        ('~/icdesign/', '~/sky130/Curso_DACI/'), (f'{home_vm}/icdesign', curso),
        (f'{home_vm}/Escritorio', os.path.join(curso, 'escritorio_vm'))]
n = 0
for root, dirs, files in os.walk(curso):
    dirs[:] = [x for x in dirs if x not in ('.svn', 'vm_config')]
    for fn in files:
        p = os.path.join(root, fn)
        if fn == 'xschemrc':
            continue                                   # se adapta aparte, abajo
        if fn.endswith(('.raw', '.gds', '.png', '.pdf')) or os.path.getsize(p) > 20_000_000:
            continue
        with open(p, 'rb') as f:
            raw = f.read()
        if b'\0' in raw[:8192]:
            continue                                   # binario
        t = raw.decode('utf-8', errors='surrogateescape')
        t2 = t
        for a, b in reps:
            t2 = t2.replace(a, b)
        if t2 != t:
            with open(p, 'w', errors='surrogateescape') as f:
                f.write(t2)
            n += 1
print(f"  rutas actualizadas en {n} archivos")

# xschemrc del curso (pensado para el xschem 2021 de la VM)
p = os.path.join(curso, 'sch', 'xschemrc')
out = []
with open(p, errors='surrogateescape') as f:
    lineas = f.read().split('\n')
for l in lineas:
    l = l.replace('/usr/local/share/doc/xschem', '${XSCHEM_SHAREDIR}/../doc/xschem')
    if re.match(r'\s*(append XSCHEM_LIBRARY_PATH|lappend tcl_files|set SKYWATER_|set netlist_dir)', l):
        for a, b in [('~/skywater', '$env(HOME)/pdks/sky130A_2021'),
                     (f'{home_vm}/skywater', '$env(HOME)/pdks/sky130A_2021'),
                     ('$env(HOME)/skywater', '$env(HOME)/pdks/sky130A_2021'),
                     ('~/icdesign/sch/', '$env(HOME)/sky130/Curso_DACI/sch'),
                     ('~/icdesign/sch', '$env(HOME)/sky130/Curso_DACI/sch'),
                     (f'{home_vm}/icdesign', '$env(HOME)/sky130/Curso_DACI'),
                     ('$env(HOME)/icdesign', '$env(HOME)/sky130/Curso_DACI')]:
            l = l.replace(a, b)
    out.append(l)
    s = l.strip()
    if s == 'append XSCHEM_LIBRARY_PATH :${XSCHEM_SHAREDIR}/xschem_library':
        out.append('append XSCHEM_LIBRARY_PATH :${XSCHEM_SHAREDIR}/xschem_library/devices   ;# xschem nuevo: simbolos basicos en devices/')
    if s == 'append XSCHEM_LIBRARY_PATH :$env(HOME)/sky130/Curso_DACI/sch':
        out.append('# subcarpetas de los TPs (el xschem viejo buscaba simbolos en la carpeta del esquematico)')
        out.append('foreach _d [lsort [glob -nocomplain -type d $env(HOME)/sky130/Curso_DACI/sch/*]] '
                   '{ if {[file tail $_d] ne "simulations"} { append XSCHEM_LIBRARY_PATH :$_d } }')
out += [
    '',
    '#### --- agregado por herramientas-eda-open-source (importar_vm.sh) ---',
    '#### El xschem nuevo ya no trae ngspice::annotate (lo usan los botones "Backannotate"):',
    '#### lo reemplaza el comando interno xschem annotate_op',
    'namespace eval ngspice {}',
    'if {[info commands ngspice::annotate] eq {}} {',
    '  proc ngspice::annotate {args} { xschem annotate_op {*}$args }',
    '}',
    '#### menus Simulation -> "Simular en corners..." / "Barrido parametrico..."',
    'foreach _f {xschem_corners.tcl xschem_barrido.tcl} {',
    '  if {[file exists $env(HOME)/herramientas/$_f]} { lappend tcl_files $env(HOME)/herramientas/$_f }',
    '}',
    '',
]
with open(p, 'w', errors='surrogateescape') as f:
    f.write('\n'.join(out))
print("  xschemrc del curso adaptado al xschem nuevo")
PY

echo "sky130A sky130_fd_sc_hd root=~/pdks/sky130A_2021/pdk/skywater130" > "$CURSO/.pdk"

if [ "$BORRAR_IMG" = 1 ]; then
    rm -f "$IMG"; rmdir "$(dirname "$IMG")" 2>/dev/null || true
    echo "Particion temporal borrada."
fi
echo
echo "Listo:"
echo "  PDK:   $PDK"
echo "  Curso: $CURSO  ($(find "$CURSO/sch" -name '*.sch' | wc -l) esquematicos)"
