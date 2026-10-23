# Prompt para Claude Code

Copiá esto en Claude Code (app de escritorio, pestaña Code) y completá la ruta del `.vdi`:

---

Quiero instalar en WSL (Ubuntu) el entorno de diseño de circuitos de este repositorio:
https://github.com/GenaroTrucchi/herramientas-eda-open-source

Mi VM del curso (VirtualBox) es `icdesign` y su disco está en: `C:\...\icdesign.vdi`  ← completar

1. Leé el `README.md` del repositorio y seguí la instalación. Si no tengo WSL con Ubuntu, decime
   qué tengo que hacer yo (instalar WSL requiere permisos de administrador y reiniciar).
2. Cloná el repo dentro de Ubuntu y corré `./instalar.sh --vdi "<ruta del .vdi>"`.
   Si `apt` pide mi contraseña, avisame para que la escriba yo en una terminal.
   Si no hay espacio junto al `.vdi`, usá `--tmp` con otra unidad.
3. Si algún paso falla en mi máquina, diagnosticá la causa, arreglalo y contame qué cambiaste.
   Antes de borrar o sobrescribir archivos míos, preguntame.
4. Al terminar, verificá:
   - que al abrir WSL aparece el menú de entornos (sky130 / ihp);
   - que en `~/sky130/Curso_DACI/sch` un testbench de los TPs genera el netlist sin
     "Symbol not found" y simula con ngspice sin "unknown subckt";
   - que `corners` funciona sobre ese testbench (ver `herramientas/LEEME.md`).

Consejo: desde Windows, los comandos largos hacia WSL fallan por las comillas de PowerShell;
conviene escribir un script `.sh` y ejecutarlo con `wsl -d Ubuntu -- bash /mnt/c/.../script.sh`.
Las herramientas (xschem, ngspice) están dentro del contenedor: se usan con
`distrobox enter iic-osic-tools -- bash -c 'source ~/.osic_env.sh; ...'`.
