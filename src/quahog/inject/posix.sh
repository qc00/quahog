# quahog shell integration for bash -- OSC 133 command markers plus a private
# OSC 5522 channel (E = typed command text, for minuting and interceptors).
# Works on bash 3.2+ (no PS0): the pre-exec hook is a DEBUG trap, guarded so it
# fires once per prompt cycle, and it preserves $? for PROMPT_COMMAND.
# E is emitted twice per command: at DEBUG-trap time from BASH_COMMAND (early,
# so password/editor interceptors can pre-arm before the command runs) and at
# precmd time from history (accurate; supersedes the early guess).
# The __qua_pc case guard keeps an empty Enter -- where the only command in the
# prompt cycle is PROMPT_COMMAND itself -- from minuting a phantom command.
# Every line below is a complete statement so the file can also be replayed as
# a single semicolon-joined line by reinject(). Plain source only: no eval, no
# base64 (PLAN.md section 7).
__qua_pe() { local r=$?; if [ -n "$__qua_p" ]; then case "$BASH_COMMAND" in __qua_pc*) return $r;; esac; __qua_p=; __qua_r=1; printf '\033]5522;E;%s\007' "${BASH_COMMAND//$'\n'/ }"; printf '\033]133;C\007'; fi; return $r; }
__qua_pc() { __qua_e=$?; if [ -n "$__qua_r" ]; then __qua_r=; __qua_c=$(HISTTIMEFORMAT= builtin history 1); __qua_c=${__qua_c#"${__qua_c%%[![:space:]]*}"}; __qua_n=${__qua_c%%[![:digit:]*]*}; __qua_c=${__qua_c#"$__qua_n"}; __qua_c=${__qua_c#"${__qua_c%%[![:space:]]*}"}; printf '\033]5522;E;%s\007' "${__qua_c//$'\n'/ }"; printf '\033]133;D;%d\007' "$__qua_e"; fi; printf '\033]7;file://%s%s\007' "${HOSTNAME:-}" "$PWD"; printf '\033]133;A\007'; __qua_p=1; }
__qua_fork() { ( ( sh -c "$2" <"$1/0" >"$1/1" 2>"$1/2"; printf '%s' "$?" >"$1/rc" ) & printf '%d\n' "$!" ); }
__qua_snapshot() { cat -- "$1" 2>/dev/null; }
trap '__qua_pe' DEBUG
PROMPT_COMMAND="__qua_pc${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
PS1="${PS1}\[\033]133;B\007\]"
set +H
