"""The injection probe (plan/10-injection-and-hooks.md): the probe detects
bash vs zsh and only that shell's block is typed; injection plumbing never
appears in h.minutes; a precmd that reassigns PS1 doesn't kill integration.
"""

import shutil
import sys
import time

import pytest

import quahog

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


def test_bash_probe_sends_only_bash_block(sh):
    # Runtime-state checks, not a grep of h.raw: the injected line is much
    # wider than the PTY's columns, and long-line soft-wrap can insert
    # terminal padding bytes into the echoed text, making substring matches
    # against h.raw unreliable.
    assert sh.integrated
    r = sh.run("typeset -p precmd_functions >/dev/null 2>&1 && echo has-zsh-array || echo no-zsh-array")
    assert "no-zsh-array" in r.text  # zsh-only wiring never typed into bash
    r = sh.run("trap -p DEBUG")
    assert "__qua_preexec" in r.text  # bash's own block did go over


def test_zsh_probe_sends_only_zsh_block():
    s = quahog.zsh()
    try:
        assert s.integrated
        r = s.run("trap -p DEBUG 2>/dev/null; echo done")
        assert "__qua_preexec" not in r.text  # bash-only wiring never typed into zsh
        r = s.run("typeset -p precmd_functions")
        assert "__qua_precmd" in r.text  # zsh's own block did go over
    finally:
        s.close()
        quahog.sessions.pop(s.name, None)


def test_ps1_marker_reappended_when_missing(sh):
    """A prompt theme reassigning PS1 from its own precmd would strip the
    OSC 133;B marker quahog appends; __qua_precmd re-checks and re-appends it
    every cycle rather than assuming it survives from source time. Since the
    marker is an OSC token (stripped from h.raw/h.text before it ever gets
    there, like any other quahog marker), it's verified on the shell side."""
    sh.run("PS1='fresh> '")  # simulate a theme wiping the marker
    sh.run("true")  # one full prompt cycle with the wiped PS1
    check_cmd = "case \"$PS1\" in *'\\033]133;B\\007'*) echo has-marker;; *) echo no-marker;; esac"
    r = sh.run(check_cmd)
    assert "has-marker" in r.text


def test_injection_lines_absent_from_minutes(sh):
    sh.sendline("echo real-command")
    deadline = time.monotonic() + 5
    while not sh.minutes and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sh.minutes, "interactive command was not captured"
    commands = [m.command for m in sh.minutes]
    assert commands == ["echo real-command"]
    assert not any("__qua_" in c for c in commands)
