#### PDK_SWITCH: en carpetas sin xschemrc propio, carga el PDK activo ($PDK)
if {[info exists env(PDK_ROOT)] && [info exists env(PDK)] && [file exists $env(PDK_ROOT)/$env(PDK)/libs.tech/xschem/xschemrc]} {
  source $env(PDK_ROOT)/$env(PDK)/libs.tech/xschem/xschemrc
}
