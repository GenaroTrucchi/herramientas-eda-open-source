# ~/.entorno.sh - elegir el entorno de trabajo al entrar a WSL
#
#   entorno          -> muestra el menu y cambia de entorno (sin reiniciar WSL)
#   entorno ihp      -> cambia directo (por nombre o numero)
#
# Cada entorno es una carpeta con sus proyectos y un archivo .pdk con su PDK
# (~/.pdk_switch.sh lo aplica al entrar a la carpeta). La lista esta en
# ~/.config/entornos/lista   con el formato   nombre|descripcion|carpeta

_ENTORNOS_DIR=~/.config/entornos

_entorno_cargar() {
    _ENT_N=(); _ENT_D=(); _ENT_C=()
    local n d c
    [ -f "$_ENTORNOS_DIR/lista" ] || return 1
    while IFS='|' read -r n d c; do
        [[ -z "$n" || "$n" == \#* ]] && continue
        _ENT_N+=("$n"); _ENT_D+=("$d"); _ENT_C+=("${c/#\~/$HOME}")
    done < "$_ENTORNOS_DIR/lista"
}

entorno() {
    _entorno_cargar || { echo "[entorno] falta $_ENTORNOS_DIR/lista"; return 1; }
    local ultimo sel i k=-1 proys dir
    ultimo=$(cat "$_ENTORNOS_DIR/ultimo" 2>/dev/null)
    [[ " ${_ENT_N[*]} " == *" $ultimo "* ]] || ultimo=${_ENT_N[0]}
    sel="$1"
    if [ -z "$sel" ]; then
        echo
        echo "Elegi el entorno  (Enter = $ultimo)"
        for i in "${!_ENT_N[@]}"; do
            printf "  %d) %-7s %s\n" $((i + 1)) "${_ENT_N[$i]}" "${_ENT_D[$i]}"
            proys=$(find "${_ENT_C[$i]}" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f  ' 2>/dev/null)
            [ -n "$proys" ] && printf "             proyectos: %s\n" "$proys"
        done
        read -r -p "> " sel || sel=
        [ -z "$sel" ] && sel=$ultimo
    fi
    for i in "${!_ENT_N[@]}"; do
        [[ "$sel" == "$((i + 1))" || "$sel" == "${_ENT_N[$i]}" ]] && k=$i
    done
    [ $k -lt 0 ] && { echo "[entorno] opcion invalida: $sel"; return 1; }
    export ENTORNO=${_ENT_N[$k]}
    echo "$ENTORNO" > "$_ENTORNOS_DIR/ultimo"
    dir=${_ENT_C[$k]}
    # el PDK del entorno queda como default (fuera de las carpetas con su propio .pdk)
    [ -f "$dir/.pdk" ] && read -r PDK_DEFAULT < "$dir/.pdk"
    export PDK_DEFAULT
    _pdk_set $PDK_DEFAULT
    _PDK_FILE=; _PDK_MANUAL=; _PDK_LAST_DIR=
    if [ "$2" != "--sin-cd" ]; then
        cd "$dir" && echo "[entorno] $ENTORNO  ($dir)"
    fi
}

# Primer shell interactivo de la sesion: preguntar. Los shells hijos heredan ENTORNO.
if [ -z "$ENTORNO" ] && [[ $- == *i* ]]; then
    if [ -t 0 ] && [ -t 1 ]; then
        entorno || entorno "$(cat "$_ENTORNOS_DIR/ultimo" 2>/dev/null)"
    else
        # sin terminal (scripts): usar el ultimo elegido, sin preguntar ni cambiar de carpeta
        entorno "$(cat "$_ENTORNOS_DIR/ultimo" 2>/dev/null || echo ihp)" --sin-cd >/dev/null
    fi
fi
