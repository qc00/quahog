from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import Session

# Shell-agnostic: __qua_exec (pipe-based exec, base64-framed, separate
# stdout/stderr) and quacat (upload request + raw read).
_SHARED = r"""
__qua_exec() { perl -MIPC::Open3 -MIO::Select -MMIME::Base64 -MSymbol=gensym -e '$|=1; my ($id,$cmd)=@ARGV; my $e=gensym; my $pid=open3(my $i,my $o,$e,"sh","-c",$cmd); close $i; my $s=IO::Select->new($o,$e); while (my @r=$s->can_read){ for my $fh (@r){ my $n=sysread($fh,my $b,4096); if(!$n){$s->remove($fh);next;} syswrite(STDOUT,"\e]2607;QUA;O;$id;".(($fh==$o)?1:2).";".encode_base64($b,"")."\a"); } } waitpid($pid,0); syswrite(STDOUT,"\e]2607;QUA;X;$id;".($?>>8)."\a");' "$1" "$2"; }
quacat() { local s; s=$(stty -g </dev/tty); stty raw -echo </dev/tty; printf '\033]2607;QUA;U;%s\007' "$1" >/dev/tty; local n; n=$(dd bs=1 count=10 </dev/tty 2>/dev/null); head -c "$((10#$n))" </dev/tty; stty "$s" </dev/tty; }
"""

# bash: no first-class preexec/precmd, so a DEBUG trap fakes one, gated to
# once per prompt cycle; PROMPT_COMMAND fakes precmd.
_BASH = r"""
__qua_preexec() { local r=$?; if [ -n "$__qua_at_prompt" ]; then case "$BASH_COMMAND" in __qua_precmd*) return $r;; esac; __qua_at_prompt=; __qua_running=1; printf '\033]2607;QUA;E;%s\007' "${BASH_COMMAND//$'\n'/ }"; printf '\033]133;C\007'; fi; return $r; }
__qua_precmd() { local e=$?; if [ -n "$__qua_running" ]; then __qua_running=; printf '\033]133;D;%d\007' "$e"; fi; case "$PS1" in *'\033]133;B\007'*) ;; *) PS1="$PS1"'\[\033]133;B\007\]';; esac; printf '\033]7;file://%s\007' "$PWD"; printf '\033]133;A\007'; __qua_at_prompt=1; }
trap '__qua_preexec' DEBUG
PROMPT_COMMAND="__qua_precmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
"""

# zsh: first-class preexec_functions/precmd_functions arrays.
_ZSH = r"""
__qua_preexec() { __qua_running=1; printf '\033]2607;QUA;E;%s\007' "${1//$'\n'/ }"; printf '\033]133;C\007'; }
__qua_precmd() { local e=$?; if [[ -n $__qua_running ]]; then __qua_running=; printf '\033]133;D;%d\007' $e; fi; [[ $PS1 == *$'\e]133;B\a'* ]] || PS1="${PS1}%{"$'\e]133;B\a'"%}"; printf '\033]7;file://%s\007' "$PWD"; printf '\033]133;A\007'; }
typeset -ga precmd_functions preexec_functions
precmd_functions+=(__qua_precmd)
preexec_functions+=(__qua_preexec)
"""

_HANDSHAKE = "printf '\\033]2607;QUA;I\\007'"

_PROBE = "printf '\\033]2607;QUA;P;%s\\007' \"${ZSH_VERSION:-}\""


def _join(*blocks: str) -> str:
    lines = []
    for block in blocks:
        lines.extend(line for line in block.strip("\n").splitlines() if line.strip())
    return " ; ".join(lines)


def _snippet(zsh: bool) -> str:
    return _join(_SHARED, _ZSH if zsh else _BASH, _HANDSHAKE)


def inject(session: "Session") -> None:
    """Put quahog's shell integration into whatever shell the PTY currently sits
    in — a freshly spawned local shell, or a remote host reached by a
    hand-typed ssh, su, or ``exec zsh`` (PLAN.md §7).

    Fire-and-forget once the probe goes out: the shell answers with its own
    kind (OSC 2607;QUA;P), and the reader loop assembles and types the
    matching snippet from there (see ``send_hooks``). The one thing worth
    waiting for here is the shell being ready to read at all — a freshly
    spawned or just hopped-into shell throws away anything queued before its
    line editor starts.
    """
    session.wait_until_quiet()
    session._integrated.clear()
    session.type_plumbing(_PROBE)


def send_hooks(session: "Session", zsh: bool) -> None:
    """Type the snippet matching the shell the probe identified.

    Called off the reader thread once the probe's ``P`` answer arrives,
    after ``st.lock`` is released — typing several KB into the PTY needs the
    reader thread to keep draining the shell's echo, or both ends wedge.
    """
    session.wait_until_quiet()
    session.type_plumbing(_snippet(zsh))
