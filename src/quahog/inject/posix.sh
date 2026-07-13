# quahog shell integration for bash -- OSC 133 command markers plus a private
# OSC 2607 channel, every payload signed QUA (E = typed command text, for
# minuting and interceptors; I = handshake: shell kind/host/user;
# O/X = exec output/exit; U/Ds/De = file copy). PLAN.md §7.
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
__qua_pe() { local r=$?; if [ -n "$__qua_p" ]; then case "$BASH_COMMAND" in __qua_pc*) return $r;; esac; __qua_p=; __qua_r=1; printf '\033]2607;QUA;E;%s\007' "${BASH_COMMAND//$'\n'/ }"; printf '\033]133;C\007'; fi; return $r; }
__qua_pc() { __qua_e=$?; if [ -n "$__qua_r" ]; then __qua_r=; __qua_c=$(HISTTIMEFORMAT= builtin history 1); __qua_c=${__qua_c#"${__qua_c%%[![:space:]]*}"}; __qua_n=${__qua_c%%[![:digit:]*]*}; __qua_c=${__qua_c#"$__qua_n"}; __qua_c=${__qua_c#"${__qua_c%%[![:space:]]*}"}; printf '\033]2607;QUA;E;%s\007' "${__qua_c//$'\n'/ }"; printf '\033]133;D;%d\007' "$__qua_e"; fi; printf '\033]7;file://%s%s\007' "${HOSTNAME:-}" "$PWD"; printf '\033]133;A\007'; __qua_p=1; }
__qua_fork() { ( ( sh -c "$2" <"$1/0" >"$1/1" 2>"$1/2"; printf '%s' "$?" >"$1/rc" ) & printf '%d\n' "$!" ); }
__qua_snapshot() { cat -- "$1" 2>/dev/null; }
__qua_hs() { printf '\033]2607;QUA;I;bash;%s;%s\007' "${HOSTNAME:-}" "${USER:-}"; }
__qua_xf() { perl -e '$|=1; my $id=$ARGV[0]; my $rc="0"; my $b; while (sysread(STDIN,$b,65536) > 0) { while ($b =~ s/\x01RC(-?\d+)\x01//) { $rc=$1; } $b =~ s/\e\][^\a\e]*(?:\a|\e\\)//g; $b =~ s/\e[\[\]\(][0-9;?]*[ -\/]*[@-~]//g; $b =~ s/\e[@-Z\\_^]//g; $b =~ s/[\x00-\x08\x0b-\x1f\x7f]//g; syswrite(STDOUT, "\e]2607;QUA;O;$id;$b\a") if length $b; } syswrite(STDOUT, "\e]2607;QUA;X;$id;$rc\a");' "$1"; }
__qua_exec() { local id="$1" mode="$2"; export __qua_c="( $3 ); printf '\001RC%d\001' \$?"; if [ "$mode" = 1 ]; then socat - 'SYSTEM:eval \"$__qua_c\",pty,stderr,setsid' 2>/dev/null | __qua_xf "$id"; else socat -u 'SYSTEM:eval \"$__qua_c\",pty,stderr,setsid' - 2>/dev/null | __qua_xf "$id"; fi; unset __qua_c; }
__qua_recv() { local s; s=$(stty -g </dev/tty 2>/dev/null); stty raw -echo </dev/tty 2>/dev/null; printf '\033]2607;QUA;U;%s\007' "$1" >/dev/tty; local n; n=$(dd bs=1 count=10 </dev/tty 2>/dev/null); head -c "$((10#$n))" </dev/tty 2>/dev/null; stty "$s" </dev/tty 2>/dev/null; }
__qua_send() { { printf '\033]2607;QUA;Ds;%s\007' "$1"; base64 | tr -d '\n'; printf '\033]2607;QUA;De\007'; } >/dev/tty; }
quahog() { case "$1" in cat) __qua_recv "cat;$2";; tar) __qua_recv "tar;$2";; download) __qua_send "$2";; *) printf 'quahog: usage: quahog {cat FILE|tar DIR|download NAME}\n' >&2; return 2;; esac; }
trap '__qua_pe' DEBUG
PROMPT_COMMAND="__qua_pc${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
PS1="${PS1}\[\033]133;B\007\]"
set +H
__qua_hs
