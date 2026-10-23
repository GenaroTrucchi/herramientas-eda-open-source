## xschem_barrido.tcl - "Simulation -> Barrido parametrico..."
## Barre parametros (.param o W/L/mult/valor de instancias) en varios corners con
## barrido.py y deja los resultados en CSV + Excel. Se carga desde el xschemrc con:
##   lappend tcl_files $env(HOME)/herramientas/xschem_barrido.tcl

namespace eval barrido {
  variable script [file join [file dirname [file normalize [info script]]] barrido.py]
  variable cfgdir [file join $::env(HOME) .xschem barridos]
  variable c
  variable resdir {}
  variable fp {}
  variable conteo {}
  array set c {
    params {}
    corners "tt:27:vdd=1.8\nff:-40:vdd=1.98\nss:125:vdd=1.62"
    medir {}  extra {}  procesos 16
  }
}

proc barrido::cfgfile {} {
  variable cfgdir
  return [file join $cfgdir [file rootname [file tail [xschem get schname 0]]].cfg]
}

proc barrido::cargar {} {
  variable c
  set f [barrido::cfgfile]
  if {[file exists $f]} {
    set fp [open $f r]; set d [read $fp]; close $fp
    catch {array set c $d}
  }
}

proc barrido::guardar {} {
  variable c; variable cfgdir
  file mkdir $cfgdir
  set fp [open [barrido::cfgfile] w]; puts $fp [array get c]; close $fp
}

proc barrido::lineas {txt} {
  set out {}
  foreach l [split $txt \n] {
    set l [string trim $l]
    if {$l ne {} && [string index $l 0] ne "#"} { lappend out $l }
  }
  return $out
}

proc barrido::dialogo {} {
  variable c
  barrido::cargar
  set w .barrido_dlg
  catch {destroy $w}
  toplevel $w
  wm title $w "Barrido parametrico - [file tail [xschem get schname 0]]"

  label $w.lp -justify left -text "Parametros a barrer (uno por linea; se simulan TODAS las combinaciones)\n\
  w = 1 2 4 8                     -> .param, lista de valores\n\
  l = 0.15:0.05:0.5               -> inicio:paso:fin\n\
  iref = log(10u, 1m, 7)          -> 7 puntos logaritmicos  (lin(a, b, n) = lineales)\n\
  M1,M2@W = 1:1:10                -> atributo de instancias: W, L, mult, nf, valor\n\
  opamp.M1,opamp.M2@L = 0.15 0.3  -> instancias dentro de un subcircuito"
  grid $w.lp -sticky w -padx 6 -pady {6 0}
  text $w.params -height 5 -width 70
  $w.params insert 1.0 $c(params)
  grid $w.params -sticky we -padx 6

  label $w.lc -justify left -text "Corners (uno por linea):  seccion:temperatura:param=valor   (secciones: tt ff ss sf fs)"
  grid $w.lc -sticky w -padx 6 -pady {8 0}
  text $w.corners -height 4 -width 70
  $w.corners insert 1.0 $c(corners)
  grid $w.corners -sticky we -padx 6

  label $w.lm -justify left -text "Que medir, separado por comas (vacio = todo lo que imprime ngspice). Ej:\n\
  gbw, dcg, pm_deg=pm*180/pi              -> meas / print del testbench y cuentas\n\
  gm1=M1\[gm\], id1=M1\[id\], gmid=M1\[gm\]/M1\[id\]   -> punto de operacion (gm id gds vds vgs vth vdsat cgg...)\n\
  vout=v(vout), pot=-i(vdd)*v(vdd)        -> tensiones y corrientes del punto de operacion"
  grid $w.lm -sticky w -padx 6 -pady {8 0}
  entry $w.medir -textvariable barrido::c(medir)
  grid $w.medir -sticky we -padx 6

  label $w.le -justify left -text "Comandos extra para el .control (opcional, uno por linea)"
  grid $w.le -sticky w -padx 6 -pady {8 0}
  text $w.extra -height 3 -width 70
  $w.extra insert 1.0 $c(extra)
  grid $w.extra -sticky we -padx 6

  frame $w.p
  label $w.p.l -text "Simulaciones en paralelo:"
  entry $w.p.e -width 4 -textvariable barrido::c(procesos)
  label $w.p.n -textvariable ::barrido::conteo -fg #1f5fa8
  pack $w.p.l $w.p.e -side left
  pack $w.p.n -side left -padx 12
  grid $w.p -sticky w -padx 6 -pady 6

  frame $w.b
  button $w.b.cnt -text "Contar simulaciones" -command barrido::contar
  button $w.b.ok -text "Barrer" -command barrido::correr
  button $w.b.no -text "Cerrar" -command "destroy $w"
  pack $w.b.cnt $w.b.ok $w.b.no -side left -padx 6
  grid $w.b -pady 6
  grid columnconfigure $w 0 -weight 1
  set ::barrido::conteo {}
}

proc barrido::leer_dialogo {} {
  variable c
  set w .barrido_dlg
  set c(params)  [string trim [$w.params get 1.0 end]]
  set c(corners) [string trim [$w.corners get 1.0 end]]
  set c(extra)   [string trim [$w.extra get 1.0 end]]
  barrido::guardar
  set args {}
  foreach l [barrido::lineas $c(params)]  { lappend args -p $l }
  foreach l [barrido::lineas $c(corners)] { lappend args -c [string map {{ } {}} $l] }
  if {[string trim $c(medir)] ne {}} { lappend args -m $c(medir) }
  foreach l [barrido::lineas $c(extra)] { lappend args -e $l }
  if {[string is integer -strict $c(procesos)]} { lappend args -j $c(procesos) }
  return $args
}

proc barrido::contar {} {
  variable script
  set args [barrido::leer_dialogo]
  if {[catch {exec python3 $script - --contar {*}$args 2>@1} r]} {
    set r [lindex [split [string trim $r] \n] end]
  }
  set ::barrido::conteo $r
  return $r
}

proc barrido::correr {} {
  variable script; variable fp; variable resdir
  set args [barrido::leer_dialogo]
  set r [barrido::contar]
  if {[regexp {= (\d+) simulaciones} $r -> n] && $n > 3000} {
    set ok [tk_messageBox -type yesno -icon question -title Barrido \
      -message "$r\n\nSon muchas simulaciones (pueden tardar varios minutos). Seguir?"]
    if {$ok ne "yes"} return
  } elseif {![regexp {simulaciones} $r]} {
    tk_messageBox -icon error -title Barrido -message $r
    return
  }
  if {[catch {xschem netlist} e]} {
    tk_messageBox -icon error -title Barrido -message "Fallo el netlist:\n$e"
    return
  }
  set netlist [file join $::netlist_dir [xschem get netlist_name fallback]]

  set rw .barrido_res
  if {![winfo exists $rw]} {
    toplevel $rw
    frame $rw.b
    button $rw.b.xls -text "Abrir Excel" -state disabled -command {barrido::abrir resultados.xlsx}
    button $rw.b.dir -text "Abrir carpeta" -state disabled -command {barrido::abrir {}}
    button $rw.b.stop -text "Cancelar" -command barrido::cancelar
    button $rw.b.close -text "Cerrar" -command "destroy $rw"
    pack $rw.b.xls $rw.b.dir $rw.b.stop $rw.b.close -side left -padx 4
    pack $rw.b -side bottom -pady 4
    text $rw.t -width 110 -height 22 -font TkFixedFont -yscrollcommand "$rw.s set"
    scrollbar $rw.s -command "$rw.t yview"
    pack $rw.s -side right -fill y
    pack $rw.t -side left -fill both -expand 1
  }
  wm title $rw "Barrido - [file tail [xschem get schname 0]]"
  raise $rw
  $rw.t delete 1.0 end
  $rw.b.xls configure -state disabled
  $rw.b.dir configure -state disabled
  $rw.b.stop configure -state normal
  set resdir {}
  set fp [open |[concat [list python3 -u $script $netlist] $args [list 2>@1]] r]
  fconfigure $fp -blocking 0
  fileevent $fp readable [list barrido::leer $fp $rw]
}

proc barrido::leer {f rw} {
  variable resdir
  if {[gets $f line] >= 0} {
    if {[regexp {^RESULTADOS_DIR=(.*)$} $line -> resdir]} {
      if {[winfo exists $rw]} {
        $rw.b.xls configure -state normal
        $rw.b.dir configure -state normal
        $rw.t insert end "Desde Windows: [barrido::ruta_windows $resdir]\n"
        $rw.t see end
      }
      return
    }
    if {[winfo exists $rw.t]} {
      # el progreso se reescribe en la misma linea
      if {[regexp {^\s+\d+/\d+ simulaciones} $line] && \
          [regexp {^\s+\d+/\d+ simulaciones} [$rw.t get "end-2l linestart" "end-2l lineend"]]} {
        $rw.t delete "end-2l linestart" "end-1l linestart"
      }
      $rw.t insert end "$line\n"
      $rw.t see end
    }
  } elseif {[eof $f]} {
    fconfigure $f -blocking 1
    catch {close $f}
    if {[winfo exists $rw]} { $rw.b.stop configure -state disabled }
  }
}

proc barrido::cancelar {} {
  variable fp
  if {$fp ne {}} { catch {exec kill -TERM {*}[pid $fp]} }
}

proc barrido::ruta_windows {p} {
  set distro Ubuntu
  if {[info exists ::env(WSL_DISTRO_NAME)] && $::env(WSL_DISTRO_NAME) ne {}} { set distro $::env(WSL_DISTRO_NAME) }
  return "\\\\wsl.localhost\\$distro[string map {/ \\} $p]"
}

## Abre en Windows (Excel / Explorador). Dentro del contenedor de distrobox los .exe
## de Windows no corren directo: se lanzan a traves del /init de WSL del host.
proc barrido::comando_windows {} {
  set exe /mnt/c/Windows/explorer.exe
  if {![file exists $exe]} { return {} }
  foreach init {/run/host/init /init} {
    if {[file executable $init]} { return [list $init $exe] }
  }
  return [list $exe]
}

proc barrido::abrir {archivo} {
  variable resdir
  if {$resdir eq {}} return
  set p [expr {$archivo eq {} ? $resdir : [file join $resdir $archivo]}]
  set cmd [barrido::comando_windows]
  if {$cmd ne {}} {
    catch {exec {*}$cmd [barrido::ruta_windows $p] &}
  } else {
    catch {exec xdg-open $p &}
  }
}

proc barrido::menu {} {
  set m [xschem get top_path].menubar.simulation
  if {![winfo exists $m]} { after 500 barrido::menu; return }
  if {![catch {$m index "Barrido parametrico..."}]} return
  set pos 0
  if {![catch {$m index "Simular en corners..."} i] && $i ne "none"} { set pos [expr {$i + 1}] }
  $m insert $pos command -label "Barrido parametrico..." -command barrido::dialogo
  if {$pos == 0} { $m insert 1 separator }
}

if {[info exists ::has_x]} { barrido::menu }
