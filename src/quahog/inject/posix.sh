# quahog shell integration for bash -- emits OSC 133 command markers.
# Works on bash 3.2+ (no PS0): the pre-exec hook is a DEBUG trap, guarded so it
# fires once per prompt cycle, and it preserves $? for PROMPT_COMMAND.
# Every line below is a complete statement so the file can also be replayed as
# a single semicolon-joined line by reinject(). Plain source only: no eval, no
# base64 (PLAN.md section 7).
__qua_pe() { local r=$?; if [ -n "$__qua_p" ]; then __qua_p=; __qua_r=1; printf '\033]133;C\007'; fi; return $r; }
__qua_pc() { __qua_e=$?; if [ -n "$__qua_r" ]; then __qua_r=; printf '\033]133;D;%d\007' "$__qua_e"; fi; printf '\033]7;file://%s%s\007' "${HOSTNAME:-}" "$PWD"; printf '\033]133;A\007'; __qua_p=1; }
trap '__qua_pe' DEBUG
PROMPT_COMMAND="__qua_pc${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
PS1="${PS1}\[\033]133;B\007\]"
set +H
