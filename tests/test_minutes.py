import sys
import time

import pytest

import quahog
from quahog.minutes import Transcript, _cell_text

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


@pytest.fixture()
def sh():
    s = quahog.bash(inherit_rc=False)
    yield s
    s.close()
    quahog.sessions.pop(s.name, None)


def _wait_minute(s, n=1, timeout=5.0):
    deadline = time.monotonic() + timeout
    while len(s._minute_q) < n and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(s._minute_q) >= n, "interactive command was not captured"


def test_interactive_command_captured(sh):
    sh.sendline("echo typed-interactively")
    _wait_minute(sh)
    r = sh._drain_minutes()[0]
    assert r.command == "echo typed-interactively"
    assert "typed-interactively" in r.text
    assert r.returncode == 0


def test_interactive_failure_captured(sh):
    sh.sendline("false")
    _wait_minute(sh)
    assert sh._drain_minutes()[0].returncode == 1


def test_programmatic_run_not_minuted(sh):
    sh.run("echo programmatic")
    time.sleep(0.3)
    assert not sh._minute_q


def test_interactive_after_run_gets_own_command(sh):
    """Regression: run() must not leak its E-marker text into the next
    interactive command's capture (found in browser testing: the minuted cell
    said '%qua <previous run command>')."""
    sh.run("echo from-run")
    sh.sendline("echo from-typing")
    _wait_minute(sh)
    r = sh._drain_minutes()[0]
    assert r.command == "echo from-typing"
    assert "from-typing" in r.text


def test_interactive_after_fork_not_filtered(sh):
    """Regression: a fork()'s __qua_fork run used to leave its command text
    behind, making the next interactive command look like quahog plumbing and
    get silently dropped."""
    f = sh.fork("echo fork-noise")
    f.wait(15)
    f.close()
    sh.sendline("echo visible-again")
    _wait_minute(sh)
    r = sh._drain_minutes()[0]
    assert r.command == "echo visible-again"


def test_minutes_toggle(sh):
    sh.minutes = False
    sh.sendline("echo not-minuted")
    time.sleep(0.8)
    assert not sh._minute_q


def test_reinject_not_minuted(sh):
    sh.reinject()
    time.sleep(0.8)
    assert not sh._minute_q


def test_transcript_updates(sh):
    updates = []

    class FakeHandle:
        def update(self, obj):
            updates.append(obj)

    sh._transcript = Transcript(sh.name)
    sh._thandle = FakeHandle()
    sh.sendline("echo into-transcript")
    _wait_minute(sh)
    time.sleep(0.2)
    assert sh._transcript.blocks
    assert "into-transcript" in repr(sh._transcript)
    assert updates, "transcript display handle was not updated"


def test_run_refuses_during_interactive(sh):
    sh.sendline("sleep 0.6")
    time.sleep(0.3)  # C marker fired, D not yet
    with pytest.raises(RuntimeError, match="busy"):
        sh.run("echo nope")
    time.sleep(0.8)


def test_cell_text_single_default():
    s = quahog.bash(inherit_rc=False)
    try:
        from quahog.result import CommandResult

        r = CommandResult(s.name, "ls -la")
        assert _cell_text([(s, r)], s) == "%qua ls -la"
        assert _cell_text([(s, r)], None) == f"%qua -s {s.name} ls -la"
        r2 = CommandResult(s.name, "pwd")
        assert _cell_text([(s, r), (s, r2)], s) == "%%qua\nls -la\npwd"
        assert (
            _cell_text([(s, r), (s, r2)], None)
            == f"%%qua {s.name}\nls -la\npwd"
        )
    finally:
        s.close()
        quahog.sessions.pop(s.name, None)


def test_payload_flush_via_ipython():
    from IPython.testing.globalipapp import start_ipython

    ip = start_ipython()
    ip.run_line_magic("load_ext", "quahog")
    s = quahog.bash(inherit_rc=False)
    try:
        s.sendline("echo minute-me")
        _wait_minute(s)
        ip.run_cell("1 + 1")  # any execution flushes the queue
        payloads = ip.payload_manager.read_payload()
        ip.payload_manager.clear_payload()
        matching = [
            p
            for p in payloads
            if p.get("source") == "set_next_input" and "minute-me" in p.get("text", "")
        ]
        assert matching, f"no set_next_input payload found in {payloads!r}"
        assert matching[0]["text"].startswith("%qua ")
        assert matching[0]["replace"] is False
    finally:
        s.close()
        quahog.sessions.pop(s.name, None)
