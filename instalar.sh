#!/bin/bash
# instalar.sh - Entorno de diseno analogico open source en WSL
#   iic-osic-tools (xschem, ngspice, magic, klayout...) en distrobox + menu de entornos
#   (sky130 / ihp) + PDK por carpeta + herramientas (corners, barrido, optimizador)
#   + importacion opcional del curso DACI desde la VM "icdesign".
#
# Uso (en Ubuntu de WSL, NO dentro del contenedor):
#   ./instalar.sh                              instala el entorno
#   ./instalar.sh --vdi "C:\ruta\icdesign.vdi"  ademas importa PDK sky130 2021 + curso de la VM
#
# Opciones:
#   --vdi RUTA        disco de la VM del curso (VirtualBox .vdi) para importar
#   --tmp CARPETA     donde extraer temporalmente la VM (~32 GB; default: junto al .vdi)
#   --contenedor N    nombre del contenedor (default: iic-osic-tools)
#   --imagen IMG      imagen (default: docker.io/hpretl/iic-osic-tools:2026.08)
#   --sin-contenedor  no crear el contenedor (si ya lo tenes)
#
# Es seguro correrlo de nuevo: hace backup de lo que reemplaza en ~/.backup_entorno_eda_<fecha>/
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IMAGEN=docker.io/hpretl/iic-osic-tools:2026.08
CONT=iic-osic-tools
VDI="" TMPD="" SIN_CONT=0
while [ $# -gt 0 ]; do
    case "$1" in
        --vdi) VDI=$2; shift 2 ;;
        --tmp) TMPD=$2; shift 2 ;;
        --contenedor) CONT=$2; shift 2 ;;
        --imagen) IMAGEN=$2; shift 2 ;;
        --sin-contenedor) SIN_CONT=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Opcion desconocida: $1"; exit 1 ;;
    esac
done

paso() { echo; echo "=== $* ==="; }
[ "$(id -u)" -ne 0 ] || { echo "Correlo como tu usuario normal (no con sudo)."; exit 1; }
[ -z "${CONTAINER_ID:-}" ] || { echo "Estas dentro del contenedor ($CONTAINER_ID). Salí con 'exit' o abrí otra terminal con:  wsl -d Ubuntu -e bash --norc"; exit 1; }
grep -qi microsoft /proc/version 2>/dev/null || echo "Aviso: no parece WSL; sigo igual."

TS=$(date +%Y%m%d_%H%M%S)
BK=$HOME/.backup_entorno_eda_$TS
respaldar() { for f in "$@"; do [ -e "$f" ] && { mkdir -p "$BK"; cp -a "$f" "$BK/" ; }; done; return 0; }

# ---- 1) paquetes del sistema -------------------------------------------------------------
paso "1/6 Paquetes del sistema"
FALTAN=()
command -v podman >/dev/null || FALTAN+=(podman)
command -v distrobox >/dev/null || FALTAN+=(distrobox)
command -v debugfs >/dev/null || FALTAN+=(e2fsprogs)
command -v python3 >/dev/null || FALTAN+=(python3)
if [ -n "$VDI" ] && ! command -v 7zz >/dev/null && ! command -v 7z >/dev/null; then FALTAN+=(7zip); fi
if [ ${#FALTAN[@]} -gt 0 ]; then
    echo "Instalando: ${FALTAN[*]}  (te va a pedir tu contrasena de Ubuntu)"
    sudo apt-get update -qq
    for p in "${FALTAN[@]}"; do
        sudo apt-get install -y "$p" || { [ "$p" = 7zip ] && sudo apt-get install -y p7zip-full; } || {
            if [ "$p" = distrobox ]; then
                echo "distrobox no esta en apt: instalando en ~/.local/bin"
                curl -fsSL https://raw.githubusercontent.com/89luca89/distrobox/main/install | sh -s -- --prefix "$HOME/.local"
                export PATH=$HOME/.local/bin:$PATH
            else
                echo "No pude instalar $p"; exit 1
            fi
        }
    done
else
    echo "ok (podman, distrobox, debugfs, python3)"
fi

# ---- 2) contenedor iic-osic-tools ------------------------------------------------------------
paso "2/6 Contenedor $CONT ($IMAGEN)"
if [ $SIN_CONT = 1 ]; then
    echo "omitido (--sin-contenedor)"
elif podman container exists "$CONT" 2>/dev/null; then
    echo "ya existe: se reutiliza"
else
    echo "Descargando la imagen (~15 GB, puede tardar bastante)..."
    podman pull "$IMAGEN"
    distrobox create --yes --name "$CONT" --image "$IMAGEN"
    echo "Inicializando el contenedor (solo la primera vez, unos minutos)..."
    distrobox enter "$CONT" -- true
fi

# ---- 3) configuracion (dotfiles) ----------------------------------------------------------------
paso "3/6 Configuracion"
respaldar ~/.osic_env.sh ~/.pdk_switch.sh ~/.entorno.sh ~/.magicrc ~/.bashrc ~/.bash_aliases ~/.xschem/xschemrc
cp "$REPO/config/osic_env.sh"  ~/.osic_env.sh
cp "$REPO/config/pdk_switch.sh" ~/.pdk_switch.sh
cp "$REPO/config/entorno.sh"    ~/.entorno.sh
cp "$REPO/config/magicrc"       ~/.magicrc
mkdir -p ~/.config/entornos ~/.xschem
[ -f ~/.config/entornos/lista ] || cp "$REPO/config/entornos.lista" ~/.config/entornos/lista
# xschemrc del usuario: carga el PDK activo en carpetas sin xschemrc propio
if [ ! -f ~/.xschem/xschemrc ]; then
    cp "$REPO/config/xschemrc_usuario.tcl" ~/.xschem/xschemrc
elif ! grep -q PDK_SWITCH ~/.xschem/xschemrc; then
    { cat "$REPO/config/xschemrc_usuario.tcl"; echo; cat ~/.xschem/xschemrc; } > ~/.xschem/xschemrc.nuevo
    mv ~/.xschem/xschemrc.nuevo ~/.xschem/xschemrc
fi
# .bashrc: entrar al contenedor al abrir WSL y cargar el entorno
[ -f ~/.bashrc ] || cp /etc/skel/.bashrc ~/.bashrc
sed -i '/^# >>> entorno-eda >>>$/,/^# <<< entorno-eda <<<$/d' ~/.bashrc
if grep -qE '^[^#]*exec distrobox enter' ~/.bashrc; then
    echo "Tu .bashrc ya entra a un contenedor por su cuenta: solo agrego la carga del entorno."
    grep -q 'source ~/.osic_env.sh' ~/.bashrc || printf '\n# >>> entorno-eda >>>\n[ -f ~/.osic_env.sh ] && source ~/.osic_env.sh\n# <<< entorno-eda <<<\n' >> ~/.bashrc
else
    cat >> ~/.bashrc <<EOF
# >>> entorno-eda >>>
# Al abrir WSL se entra al contenedor $CONT; adentro se carga el entorno (menu sky130/ihp)
if [ -z "\${CONTAINER_ID:-}" ] && command -v distrobox &>/dev/null; then
    exec distrobox enter $CONT
fi
[ -f ~/.osic_env.sh ] && source ~/.osic_env.sh
# <<< entorno-eda <<<
EOF
fi
echo "ok"

# ---- 4) herramientas --------------------------------------------------------------------------------
paso "4/6 Herramientas (corners, barrido, optimizador)"
[ -d ~/herramientas ] && respaldar ~/herramientas
mkdir -p ~/herramientas
cp "$REPO"/herramientas/{optimizar.py,corners.py,barrido.py,xschem_corners.tcl,xschem_barrido.tcl,LEEME.md} ~/herramientas/
chmod +x ~/herramientas/*.py
touch ~/.bash_aliases
sed -i '/^# >>> entorno-eda >>>$/,/^# <<< entorno-eda <<<$/d' ~/.bash_aliases
cat >> ~/.bash_aliases <<'EOF'
# >>> entorno-eda >>>
alias optimizar="python3 ~/herramientas/optimizar.py"
alias corners="python3 ~/herramientas/corners.py"
alias barrido="python3 ~/herramientas/barrido.py"
alias xschem_design="xschem --rcfile ~/sky130/Curso_DACI/sch/xschemrc"
alias magic_design="magic -rcfile ~/sky130/Curso_DACI/mag/magicrc"
# <<< entorno-eda <<<
EOF
echo "ok: ~/herramientas  (alias: optimizar, corners, barrido, xschem_design, magic_design)"

# ---- 5) carpetas de los entornos -------------------------------------------------------------------------
paso "5/6 Entornos"
mkdir -p ~/sky130 ~/ihp
[ -f ~/sky130/.pdk ] || echo "sky130A sky130_fd_sc_hd root=~/pdks/sky130A_2021/pdk/skywater130" > ~/sky130/.pdk
[ -f ~/ihp/.pdk ] || echo "ihp-sg13g2" > ~/ihp/.pdk
echo "ok: ~/sky130 (PDK sky130 2021)  y  ~/ihp (IHP SG13G2 del contenedor)"

# ---- 6) curso desde la VM ----------------------------------------------------------------------------------
paso "6/6 Curso DACI desde la VM"
if [ -n "$VDI" ]; then
    "$REPO/curso_daci/importar_vm.sh" "$VDI" ${TMPD:+--tmp "$TMPD"}
    if [ -d ~/sky130/Curso_DACI ] && [ ! -e ~/sky130/Curso_DACI/optimizacion ]; then
        mkdir -p ~/sky130/Curso_DACI/optimizacion
        cp "$REPO/herramientas/ejemplos/ejemplo_TP_FINAL.yaml" ~/sky130/Curso_DACI/optimizacion/
    fi
else
    echo "omitido. Para importarlo despues:"
    echo "  $REPO/curso_daci/importar_vm.sh \"C:\\ruta\\a\\icdesign.vdi\""
    [ -d ~/pdks/sky130A_2021 ] || echo "  (sin eso, el entorno sky130 no tiene PDK: ~/pdks/sky130A_2021 no existe)"
fi

echo
echo "================================================================"
echo " Listo. Cerra esta ventana y abri WSL de nuevo: vas a ver el menu"
echo " para elegir el entorno (sky130 / ihp)."
[ -d "$BK" ] && echo " Backup de lo reemplazado: $BK"
echo "================================================================"
