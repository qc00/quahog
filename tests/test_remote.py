"""Remote navigation over the PTY (PLAN.md §7): reach a shell that dropped the
integration and restore it with inject(), then operate normally.

A real remote host can't source a local hooks file — quahog always probes the
shell kind and types the matching snippet fresh. Here we simulate the hop
with a nested ``exec bash --norc`` that loses the integration — the same
failure mode as landing on a bare remote shell.
"""

import shutil
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


def test_handshake_confirms_integration(sh):
    # The hooks emit OSC 2607;QUA;I on source, confirming the inject.
    assert sh.integrated


def test_run_after_reinject(sh):
    sh.run("echo integrated")  # baseline
    sh.sendline("exec bash --norc --noprofile")
    time.sleep(0.8)
    # Integration is gone: no markers. Re-inject.
    sh.inject()
    assert sh.wait_integrated(15), "handshake never confirmed the inject"
    r = sh.run("echo after-reinject", timeout=15)
    assert r.text.strip() == "after-reinject"
    assert r.returncode == 0


@pytest.mark.skipif(shutil.which("perl") is None, reason="exec needs perl")
def test_exec_after_reinject(sh):
    sh.sendline("exec bash --norc --noprofile")
    time.sleep(0.8)
    sh.inject()
    assert sh.wait_integrated(15)
    es = sh.exec("echo exec-remote; exit 4")
    assert es.wait(15) == 4
    assert "exec-remote" in es.stdout


def test_reinject_not_minuted(sh):
    # The inject plumbing must never show up as a user command (PLAN.md §5).
    # Minuting tracks *interactively typed* commands (sendline), not run().
    sh.sendline("exec bash --norc --noprofile")
    time.sleep(0.8)
    sh.inject()
    assert sh.wait_integrated(15)
    sh.sendline("echo real-command")
    time.sleep(1.0)
    commands = [m.command for m in sh.minutes]
    assert "echo real-command" in commands
    assert not any("__qua_" in c for c in commands)
    assert not any(";133;" in c or "]133" in c for c in commands)
