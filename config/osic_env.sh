if [ -d "/foss/pdks" ]; then
    export PATH=/headless/.local/bin:/foss/tools/bin:/foss/tools/sak:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/foss/tools/kactus2:/foss/tools/klayout:/foss/tools/osic-multitool:$PATH
    export PDK_ROOT_SYS=/foss/pdks
    # si viene de un shell padre (ej. terminal abierta desde xschem) se respeta lo heredado
    export PDK_ROOT="${PDK_ROOT:-$PDK_ROOT_SYS}"
    export SHELL=/bin/bash
    # PDK fuera de las carpetas con archivo .pdk (lo define el entorno elegido)
    export PDK_DEFAULT="${PDK_DEFAULT:-ihp-sg13g2}"
    # PDK, PDKPATH, STD_CELL_LIBRARY, SPICE_USERINIT_DIR y KLAYOUT_PATH
    # los define ~/.pdk_switch.sh segun la carpeta en la que estes
    [ -f ~/.pdk_switch.sh ] && source ~/.pdk_switch.sh
    # menu para elegir el entorno (sky130 / ihp) al entrar a WSL
    [ -f ~/.entorno.sh ] && source ~/.entorno.sh
fi
