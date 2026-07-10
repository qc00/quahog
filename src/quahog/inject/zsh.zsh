# quahog shell integration for zsh -- emits OSC 133 command markers.
__qua_pe() { __qua_r=1; printf '\033]133;C\007'; }
__qua_pc() { local e=$?; if [[ -n $__qua_r ]]; then __qua_r=; printf '\033]133;D;%d\007' $e; fi; printf '\033]7;file://%s%s\007' "${HOST:-}" "$PWD"; printf '\033]133;A\007'; }
typeset -ga precmd_functions preexec_functions
precmd_functions+=(__qua_pc)
preexec_functions+=(__qua_pe)
PS1="${PS1}%{$(printf '\033]133;B\007')%}"
