# herramientas-eda-open-source

Entorno de diseño de circuitos integrados analógicos **open source en WSL** (Windows 11),
pensado para quienes vienen de la VM `icdesign` del curso DACI y quieren pasar a WSL.

- **Herramientas**: xschem, ngspice, magic, klayout, netgen... del contenedor
  [IIC-OSIC-TOOLS](https://github.com/iic-jku/IIC-OSIC-TOOLS) (versión 2026.08), vía distrobox.
- **Menú de entornos** al abrir WSL: `sky130` (el PDK de 2021 de la VM del curso) o `ihp` (IHP SG13G2).
- **PDK por carpeta**: cada proyecto usa su PDK automáticamente (archivo `.pdk`).
- **Importa el curso desde la VM** sin arrancarla: PDK, TPs y layouts, con las rutas y la
  configuración de xschem adaptadas al xschem nuevo.
- **Herramientas de simulación** (menú *Simulation* de xschem o terminal):
  - **corners**: el testbench en varios corners (TT/FF/SS, temperatura, VDD) en paralelo, en una tabla.
  - **barrido**: barre parámetros (`.param`, W/L/mult de instancias) × corners → **Excel / CSV**.
    Mide también el punto de operación (`gm1=M1[gm]`, `v(nodo)`...).
  - **optimizar**: busca W/L/C/I que cumplan especificaciones en todos los corners con el mínimo consumo.

```
Elegi el entorno  (Enter = sky130)
  1) sky130  Skywater 130 - PDK 2021 de la VM del curso
             proyectos: Curso_DACI
  2) ihp     IHP SG13G2 - PDK del contenedor iic-osic-tools
```

## Instalación

Necesitás Windows 11 (con WSLg para las ventanas gráficas), ~25 GB libres para el contenedor y,
para importar el curso, ~35 GB temporales y el archivo `.vdi` de la VM `icdesign`.

1. **WSL con Ubuntu** (PowerShell como administrador, si todavía no lo tenés):
   ```powershell
   wsl --install -d Ubuntu
   ```
   Reiniciá, abrí "Ubuntu" y creá tu usuario.

2. **En Ubuntu**:
   ```bash
   sudo apt update && sudo apt install -y git
   git clone https://github.com/GenaroTrucchi/herramientas-eda-open-source.git
   cd herramientas-eda-open-source
   ./instalar.sh --vdi "C:\Users\TU_USUARIO\VirtualBox VMs\icdesign\icdesign.vdi"
   ```
   La ruta del `.vdi` la ves en VirtualBox → la VM → Configuración → Almacenamiento.
   La VM tiene que estar **apagada**. Si en el disco del `.vdi` no hay ~35 GB libres:
   `--tmp /mnt/d` (otra unidad).
   El instalador baja el contenedor (~15 GB, tarda) y te pide la contraseña de Ubuntu para `apt`.

3. **Cerrá y abrí WSL**: aparece el menú. Elegí `sky130` y probá:
   ```bash
   cd ~/sky130/Curso_DACI/sch && xschem_design
   ```

Si algo falla en tu máquina, en `PROMPT_CLAUDE.md` hay un prompt para pedirle a Claude Code
que lo instale y resuelva lo que haga falta.

## Uso

| Comando | Qué hace |
|---|---|
| `entorno` | cambiar de entorno sin cerrar WSL (`entorno ihp`, `entorno 1`) |
| `pdk` | ver el PDK activo |
| `pdk-init <pdk>` | marcar la carpeta actual con otro PDK (crea `.pdk`) |
| `xschem_design` / `magic_design` | xschem y magic del curso |
| `corners`, `barrido`, `optimizar` | ver [`herramientas/LEEME.md`](herramientas/LEEME.md) |

En xschem: **Simulation → Simular en corners...** y **Simulation → Barrido parametrico...**.

**Proyectos nuevos**: creá la carpeta dentro de `~/sky130` o `~/ihp` y usa el PDK de ese entorno.

## Estructura que deja

```
~/sky130/            entorno Skywater 130 (PDK 2021)       -> .pdk
    Curso_DACI/      sch/ (TPs), mag/, escritorio_vm/, optimizacion/, vm_config/
~/ihp/               entorno IHP SG13G2 (PDK del contenedor) -> .pdk
~/pdks/sky130A_2021/ PDK, símbolos xschem y modelos ngspice de la VM
~/herramientas/      corners.py, barrido.py, optimizar.py (+ menús de xschem)
~/.osic_env.sh  ~/.pdk_switch.sh  ~/.entorno.sh  ~/.config/entornos/
```

Para agregar un entorno: una línea en `~/.config/entornos/lista` (`nombre|descripción|carpeta`)
y un `.pdk` en esa carpeta. Formato de `.pdk`: `<pdk> [<stdcell>] [root=<carpeta>]`.

## Qué arregla el importador de la VM

El curso se armó con el xschem y el PDK de 2021. Con el xschem nuevo del contenedor fallan
algunas cosas que el importador corrige solo:

- rutas `~/skywater`, `~/icdesign`, `/home/designer` → nuevas ubicaciones (esquemáticos, magicrc y 43 archivos del PDK);
- símbolos básicos (`vsource.sym`, `res.sym`...) que ahora están en `xschem_library/devices`;
- símbolos de la carpeta del TP (ej. `opamp.sym`), que el xschem viejo encontraba solo;
- el botón **Backannotate** (`ngspice::annotate` ya no existe: se redirige a `xschem annotate_op`);
- los ejemplos de xschem que estaban en `/usr/local/share/doc/xschem`.

## Notas

- **Velocidad**: `corners`, `barrido` y `optimizar` cargan solo los modelos que usa el circuito
  (validado contra la librería completa) y corren ngspice en paralelo: miles de simulaciones por minuto.
- No incluye ningún PDK ni material del curso: el PDK sky130 2021 y los TPs salen de **tu** VM.
- Créditos: IIC-OSIC-TOOLS (JKU, Apache-2.0), xschem (GPL-2.0), ngspice, PDK SkyWater SKY130
  y IHP SG13G2 (Apache-2.0), distrobox, podman.

## Licencia

MIT, ver [LICENSE](LICENSE).
