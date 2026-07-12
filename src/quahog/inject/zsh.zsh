# quahog shell integration for zsh -- OSC 133 command markers plus a private
# OSC 2607 channel, every payload signed QUA (E = typed command text;
# I = handshake: shell kind/host/user). PLAN.md §7.
__qua_pe() { __qua_r=1; printf '\033]2607;QUA;E;%s\007' "${1//$'\n'/ }"; printf '\033]133;C\007'; }
__qua_pc() { local e=$?; if [[ -n $__qua_r ]]; then __qua_r=; printf '\033]133;D;%d\007' $e; fi; printf '\033]7;file://%s%s\007' "${HOST:-}" "$PWD"; printf '\033]133;A\007'; }
__qua_fork() { ( ( sh -c "$2" <"$1/0" >"$1/1" 2>"$1/2"; printf '%s' "$?" >"$1/rc" ) & printf '%d\n' "$!" ); }
__qua_snapshot() { cat -- "$1" 2>/dev/null; }
__qua_hs() { printf '\033]2607;QUA;I;zsh;%s;%s\007' "${HOST:-}" "${USER:-}"; }
typeset -ga precmd_functions preexec_functions
precmd_functions+=(__qua_pc)
preexec_functions+=(__qua_pe)
PS1="${PS1}%{$(printf '\033]133;B\007')%}"
