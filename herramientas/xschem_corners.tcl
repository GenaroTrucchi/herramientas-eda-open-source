## xschem_corners.tcl - "Simulation -> Simular en corners..."
## Simula el esquematico abierto en 3 corners en paralelo (corners.py) y muestra
## una tabla con las mediciones pedidas. Se carga desde el xschemrc con:
##   lappend tcl_files $env(HOME)/herramientas/xschem_corners.tcl

namespace eval corners {
  variable script [file join [file dirname [file normalize [info script]]] corners.py]
  variable cfgdir [file join $::env(HOME) .xschem corners]
  variable lista {tt ff ss sf fs}
  variable c
  # valores por defecto (sky130: 1.8 V +-10 %)
  array set c {
    lib1 tt  temp1 27   par1 {vdd=1.8}
    lib2 ff  temp2 -40  par2 {vdd=1.98}
    lib3 ss  temp3 125  par3 {vdd=1.62}
    medir {}  extra {}  raw 0
  }
}

proc corners::cfgfile {} {
  variable cfgdir
  set sch [file rootname [file tail [xschem get schname 0]]]
  return [file join $cfgdir $sch.cfg]
}

proc corners::cargar {} {
  variable c
  set f [corners::cfgfile]
  if {[file exists $f]} {
    set fp [open $f r]; set d [read $fp]; close $fp
    catch {array set c $d}
  }
}

proc corners::guardar {} {
  variable c; variable cfgdir
  file mkdir $cfgdir
  set fp [open [corners::cfgfile] w]; puts $fp [array get c]; close $fp
}

proc corners::dialogo {} {
  variable c; variable lista
  corners::cargar
  set w .corners_dlg
  catch {destroy $w}
  toplevel $w
  wm title $w "Simular en corners - [file tail [xschem get schname 0]]"

  label $w.h1 -text "Corner" -font {TkDefaultFont 9 bold}
  label $w.h2 -text "Temp \[C\]" -font {TkDefaultFont 9 bold}
  label $w.h3 -text ".param a cambiar (ej: vdd=1.8)" -font {TkDefaultFont 9 bold}
  grid $w.h1 $w.h2 $w.h3 -sticky w -padx 4 -pady 2
  foreach i {1 2 3} {
    ttk::combobox $w.lib$i -values $lista -width 6 -textvariable corners::c(lib$i)
    entry $w.temp$i -width 7 -textvariable corners::c(temp$i)
    entry $w.par$i -width 30 -textvariable corners::c(par$i)
    grid $w.lib$i $w.temp$i $w.par$i -sticky we -padx 4 -pady 1
  }
  label $w.lm -text "Que medir (separado por comas; vacio = todo lo que imprime ngspice)\nej:  gbw, dcg, pm_deg=pm*180/pi" -justify left
  grid $w.lm -columnspan 3 -sticky w -padx 4 -pady {8 0}
  entry $w.medir -textvariable corners::c(medir)
  grid $w.medir -columnspan 3 -sticky we -padx 4
  label $w.le -text "Comandos extra para el .control (uno por linea, opcional). Ej. potencia de V2:\n  op\n  let potencia = -i(v2)*v(vdd)\n  print potencia" -justify left
  grid $w.le -columnspan 3 -sticky w -padx 4 -pady {8 0}
  text $w.extra -height 4 -width 60
  $w.extra insert 1.0 $c(extra)
  grid $w.extra -columnspan 3 -sticky we -padx 4
  checkbutton $w.raw -text "Guardar los .raw de cada corner (simulations/corners/...)" -variable corners::c(raw)
  grid $w.raw -columnspan 3 -sticky w -padx 4 -pady 4
  frame $w.b
  button $w.b.ok -text "Simular" -command corners::simular
  button $w.b.no -text "Cerrar" -command "destroy $w"
  pack $w.b.ok $w.b.no -side left -padx 6
  grid $w.b -columnspan 3 -pady 6
  grid columnconfigure $w 2 -weight 1
}

proc corners::simular {} {
  variable c; variable script
  set w .corners_dlg
  set c(extra) [string trim [$w.extra get 1.0 end]]
  corners::guardar

  # 1) netlist del esquematico abierto
  if {[catch {xschem netlist} e]} {
    tk_messageBox -icon error -title Corners -message "Fallo el netlist:\n$e"
    return
  }
  set netlist [file join $::netlist_dir [xschem get netlist_name fallback]]

  # 2) argumentos para corners.py
  set args [list python3 -u $script $netlist]
  foreach i {1 2 3} {
    set lib [string trim $c(lib$i)]
    if {$lib eq {}} continue
    set spec $lib
    set t [string trim $c(temp$i)]
    append spec ":$t"
    set p [string map {{ } {}} [string trim $c(par$i)]]
    if {$p ne {}} { append spec ":$p" }
    lappend args -c $spec
  }
  if {[string trim $c(medir)] ne {}} { lappend args -m $c(medir) }
  foreach l [split $c(extra) \n] {
    set l [string trim $l]
    if {$l ne {}} { lappend args -e $l }
  }
  if {$c(raw)} { lappend args --guardar-raw }

  # 3) ventana de resultados y ejecucion en segundo plano
  set r .corners_res
  if {![winfo exists $r]} {
    toplevel $r
    text $r.t -width 100 -height 24 -font TkFixedFont -yscrollcommand "$r.s set"
    scrollbar $r.s -command "$r.t yview"
    button $r.c -text Cerrar -command "destroy $r"
    pack $r.c -side bottom -pady 4
    pack $r.s -side right -fill y
    pack $r.t -side left -fill both -expand 1
  }
  wm title $r "Corners - [file tail [xschem get schname 0]]"
  raise $r
  $r.t delete 1.0 end
  set fp [open |[concat $args [list 2>@1]] r]
  fconfigure $fp -blocking 0 -buffering line
  fileevent $fp readable [list corners::leer $fp $r.t]
}

proc corners::leer {fp t} {
  if {[gets $fp line] >= 0} {
    if {[winfo exists $t]} { $t insert end "$line\n"; $t see end }
  } elseif {[eof $fp]} {
    fconfigure $fp -blocking 1
    catch {close $fp}
  }
}

proc corners::menu {} {
  set m [xschem get top_path].menubar.simulation
  if {![winfo exists $m]} { after 500 corners::menu; return }
  if {[catch {$m index "Simular en corners..."}] || [$m index "Simular en corners..."] eq "none"} {
    $m insert 0 command -label "Simular en corners..." -command corners::dialogo
    $m insert 1 separator
  }
}

if {[info exists ::has_x]} { corners::menu }
