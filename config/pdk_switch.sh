# ~/.pdk_switch.sh — selección de PDK por proyecto (IIC-OSIC-TOOLS)
#
# Uso:
#   pdk                 -> muestra el PDK activo y los disponibles
#   pdk sky130A         -> cambia a mano en esta terminal
#   pdk-init sky130A    -> marca la carpeta actual como proyecto sky130A (crea .pdk)
#
# Al hacer `cd` a una carpeta que tenga (ella o algún padre) un archivo `.pdk`,
# el PDK cambia solo. Fuera de un proyecto vuelve a PDK_DEFAULT.
# Formato de .pdk: una línea "<pdk> [<stdcell-lib>] [root=<dir>]"
#   ej: "sky130A"                                   (PDK del contenedor, /foss/pdks)
#       "sky130A root=~/pdks/sky130A_2021/pdk/skywater130"   (PDK propio, ej. el de la VM)

: "${PDK_DEFAULT:=ihp-sg13g2}"
: "${PDK_ROOT_SYS:=/foss/pdks}"

_pdk_set() {
    local name="$1" lib="" root="" t
    shift
    for t in "$@"; do
        case "$t" in root=*) root="${t#root=}" ;; *) lib="$t" ;; esac
    done
    root="${root/#\~/$HOME}"
    export PDK_ROOT="${root:-$PDK_ROOT_SYS}"
    if [ ! -d "$PDK_ROOT/$name" ]; then
        echo "[pdk] No existe $PDK_ROOT/$name" >&2
        return 1
    fi
    export PDK="$name"
    export PDKPATH="$PDK_ROOT/$PDK"
    export SPICE_USERINIT_DIR="$PDKPATH/libs.tech/ngspice"
    export KLAYOUT_PATH="/headless/.klayout:$PDKPATH/libs.tech/klayout"
    [ -d "$PDKPATH/libs.tech/klayout/tech" ] && KLAYOUT_PATH="$KLAYOUT_PATH:$PDKPATH/libs.tech/klayout/tech"
    unset GF_PDK_OPTION
    if [ -z "$lib" ]; then
        case "$PDK" in
            sky130A|sky130B)        lib=sky130_fd_sc_hd ;;
            ihp-sg13g2)             lib=sg13g2_stdcell ;;
            ihp-sg13cmos5l)         lib=sg13cmos5l_stdcell ;;
            gf180mcuC)              lib=gf180mcu_fd_sc_mcu7t5v0; export GF_PDK_OPTION=C ;;
            gf180mcuD)              lib=gf180mcu_fd_sc_mcu7t5v0; export GF_PDK_OPTION=D ;;
        esac
    fi
    export STD_CELL_LIBRARY="$lib"
}

pdk() {
    if [ $# -eq 0 ]; then
        echo "PDK activo: $PDK  (stdcell: $STD_CELL_LIBRARY)  en $PDK_ROOT"
        [ -n "$_PDK_FILE" ] && echo "Definido por: $_PDK_FILE" || echo "Definido por: default/manual"
        echo "Disponibles en $PDK_ROOT_SYS: $(ls "$PDK_ROOT_SYS" | grep -vE 'ciel|versions' | tr '\n' ' ')"
        return
    fi
    _pdk_set "$@" && _PDK_MANUAL=1 && echo "[pdk] $PDK ($STD_CELL_LIBRARY)"
}

pdk-init() {
    [ -z "$1" ] && { echo "Uso: pdk-init <pdk> [<stdcell-lib>]"; return 1; }
    echo "$*" > .pdk
    # que git no lo marque como archivo nuevo
    local gd; gd=$(git rev-parse --git-dir 2>/dev/null) && \
        { grep -qxF ".pdk" "$gd/info/exclude" 2>/dev/null || echo ".pdk" >> "$gd/info/exclude"; }
    _PDK_LAST_DIR=; _pdk_auto
}

# Busca .pdk desde $PWD hacia arriba
_pdk_find() {
    local d="$PWD"
    while [ -n "$d" ] && [ "$d" != "/" ]; do
        [ -f "$d/.pdk" ] && { echo "$d/.pdk"; return; }
        d="${d%/*}"
    done
}

_pdk_auto() {
    [ "$PWD" = "$_PDK_LAST_DIR" ] && return
    _PDK_LAST_DIR="$PWD"
    local f; f=$(_pdk_find)
    if [ -n "$f" ]; then
        [ "$f" = "$_PDK_FILE" ] && [ -z "$_PDK_MANUAL" ] && return
        local line; read -r line < "$f"
        _pdk_set $line && echo "[pdk] $PDK ($STD_CELL_LIBRARY) en $PDK_ROOT  <- ${f%/.pdk}"
        _PDK_FILE="$f"; _PDK_MANUAL=
    elif [ -n "$_PDK_FILE" ]; then
        _pdk_set $PDK_DEFAULT && echo "[pdk] $PDK (default del entorno)"
        _PDK_FILE=; _PDK_MANUAL=
    fi
}

# Estado inicial + hook en cada prompt
# (PDK_DEFAULT puede ser una linea completa de .pdk, ej. "sky130A root=~/pdks/...": sin comillas a proposito)
[ -z "$PDK" ] && _pdk_set $PDK_DEFAULT
case "$PROMPT_COMMAND" in *_pdk_auto*) ;; *) PROMPT_COMMAND="_pdk_auto${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;; esac
case "$PS1" in *'$PDK'*) ;; *) PS1='\[\e[1;35m\][${ENTORNO:+$ENTORNO | }$PDK]\[\e[0m\] '"$PS1" ;; esac
