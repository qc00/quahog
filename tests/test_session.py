import sys

import pytest

import quahog
from quahog import TimeoutExpired

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


def test_echo(sh):
    r = sh.run("echo hello")
    assert r.text.strip() == "hello"
    assert r.returncode == 0
    assert r.ok


def test_exit_codes(sh):
    assert sh.run("true").returncode == 0
    assert sh.run("false").returncode == 1
    assert sh.run("exit_code_is() { return 42; }; exit_code_is").returncode == 42


def test_color_raw_vs_text(sh):
    r = sh.run("printf '\\033[31mred\\033[0m\\n'")
    assert r.text.strip() == "red"
    assert "\x1b[31m" in r.raw


def test_state_persists_between_runs(sh):
    sh.run("X=42")
    assert sh.run("echo $X").text.strip() == "42"


def test_cwd_tracking(sh):
    sh.run("cd /tmp")
    assert sh.run("pwd").text.strip() in ("/tmp", "/private/tmp")
    assert sh.cwd in ("/tmp", "/private/tmp")


def test_multiline_output(sh):
    r = sh.run("printf 'a\\nb\\nc\\n'")
    assert r.text.splitlines() == ["a", "b", "c"]


def test_no_wait_then_wait(sh):
    r = sh.run("sleep 0.4; echo done", wait=False)
    assert not r.done
    r.wait(10)
    assert "done" in r.text
    assert r.returncode == 0


def test_busy_refusal(sh):
    r = sh.run("sleep 0.5", wait=False)
    with pytest.raises(RuntimeError, match="busy"):
        sh.run("echo nope")
    r.wait(10)


def test_timeout_keeps_command_running(sh):
    with pytest.raises(TimeoutExpired) as ei:
        sh.run("sleep 1; echo late", timeout=0.2)
    partial = ei.value.result
    partial.wait(10)
    assert "late" in partial.text


def test_popen_face(sh):
    assert isinstance(sh.pid, int)
    assert sh.poll() is None
    sh.stdin.write("echo via_stdin\r")
    # stdin writes are raw keystrokes, not run(): just check the shell echoed it
    import time

    time.sleep(0.5)
    assert sh.returncode is None


def test_session_exit(sh):
    sh.sendline("exit 7")
    assert sh.wait(10) == 7
    assert sh.poll() == 7
    with pytest.raises(RuntimeError, match="exited"):
        sh.run("echo nope")


def test_unicode(sh):
    r = sh.run("echo 'héllo wörld ✓'")
    assert r.text.strip() == "héllo wörld ✓"


def test_registry_and_default(sh):
    s = sh
    assert quahog.sessions[s.name] is s
    assert quahog.default is s


def test_duplicate_terminal_reply_is_dropped(sh, monkeypatch):
    """A recognized terminal-capability reply (CPR, DA2, DECRPM, OSC color,
    focus report -- never something a human types) is deduped within a short
    window regardless of source, including repeats from the very same view:
    a full-screen app can retry its own query if a reply doesn't arrive in
    time (plausible over the browser/kernel round trip), and if replies then
    show up for every attempt the PTY gets the same answer repeated, which
    some apps can't handle (regression: vim's welcome screen replaced by a
    stray "y" -- the tail of a DECRPM reply landing three times in ~15ms,
    all from a single attached view)."""
    writes = []
    monkeypatch.setattr(sh, "_write", lambda data: writes.append(data))

    class FakeView:
        pass

    a, b = FakeView(), FakeView()

    sh._input(b"\x1b[?12;2$y", source=a)
    sh._input(b"\x1b[?12;2$y", source=b)  # same reply, different view: dropped
    sh._input(b"\x1b[?12;2$y", source=a)  # same view retrying: also dropped
    assert writes == [b"\x1b[?12;2$y"]


def test_duplicate_non_reply_escape_from_different_view_is_dropped(sh, monkeypatch):
    """An unrecognized escape sequence (e.g. an arrow key) repeated
    byte-for-byte from a genuinely different view within the window is still
    treated as suspect and dropped -- but a repeat from the *same* view is a
    real keystroke (someone holding an arrow key) and must go through every
    time, not just once."""
    writes = []
    monkeypatch.setattr(sh, "_write", lambda data: writes.append(data))

    class FakeView:
        pass

    a, b = FakeView(), FakeView()

    sh._input(b"\x1b[A", source=a)
    sh._input(b"\x1b[A", source=b)  # same bytes, different view: dropped
    assert writes == [b"\x1b[A"]

    sh._input(b"\x1b[A", source=a)  # same view repeating: a real repeat, not deduped
    assert writes == [b"\x1b[A", b"\x1b[A"]

    # Scoped to escape sequences: a plain repeated character (e.g. two
    # people coincidentally pressing the same key in two cells) is never
    # affected, even across views.
    sh._input(b"a", source=a)
    sh._input(b"a", source=b)
    assert writes[-2:] == [b"a", b"a"]


def test_reinject_after_exec(sh):
    sh.sendline("exec bash --norc")
    import time

    time.sleep(0.8)
    sh.inject()
    assert sh.wait_integrated(15)
    r = sh.run("echo back")
    assert r.text.strip() == "back"
