"""Remote navigation over the PTY (PLAN.md §7): reach a shell that dropped the
integration and restore it with reinject(full=True), then operate normally.

A real remote can't source quahog's local snippet file, so reinject(full=True)
re-types the whole snippet as plain shell source. Here we simulate the hop with
a nested ``exec bash --norc`` that loses the integration — the same failure
mode as landing on a bare remote shell.
"""

import shutil
import sys
import time

import pytest

import quahog

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


@pytest.fixture()
def sh():
    s = quahog.bash(inherit_rc=False)
    yield s
    s.close()
    quahog.sessions.pop(s.name, None)


def test_handshake_sets_shell_kind(sh):
    # The snippet emits OSC 2607;QUA;I on source, confirming the (re)inject.
    assert sh.shell_kind == "bash"


def test_run_after_full_reinject(sh):
    sh.run("echo integrated")  # baseline
    sh.sendline("exec bash --norc --noprofile")
    time.sleep(0.8)
    # Integration is gone: no markers. Re-type the whole snippet.
    sh.reinject(full=True)
    assert sh.wait_reinject(15), "handshake never confirmed the reinject"
    r = sh.run("echo after-reinject", timeout=15)
    assert r.text.strip() == "after-reinject"
    assert r.returncode == 0


@pytest.mark.skipif(shutil.which("socat") is None, reason="exec needs socat")
@pytest.mark.skipif(shutil.which("perl") is None, reason="exec filter needs perl")
def test_exec_after_full_reinject(sh):
    sh.sendline("exec bash --norc --noprofile")
    time.sleep(0.8)
    sh.reinject(full=True)
    assert sh.wait_reinject(15)
    es = sh.exec("echo exec-remote; exit 4")
    assert es.wait(15) == 4
    assert "exec-remote" in es.text


def test_reinject_not_minuted(sh):
    # The reinject plumbing must never show up as a user command (PLAN.md §5).
    # Minuting tracks *interactively typed* commands (sendline), not run().
    sh.sendline("exec bash --norc --noprofile")
    time.sleep(0.8)
    sh.reinject(full=True)
    assert sh.wait_reinject(15)
    sh.sendline("echo real-command")
    time.sleep(1.0)
    commands = [m.command for m in sh.minutes]
    assert "echo real-command" in commands
    assert not any("__qua_" in c for c in commands)
    assert not any(";133;" in c or "]133" in c for c in commands)
