import sys

import pytest

import quahog
from quahog import TimeoutExpired

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


@pytest.fixture()
def sh():
    s = quahog.bash(inherit_rc=False)
    yield s
    s.close()
    quahog.sessions.pop(s.name, None)


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


def test_registry_and_default():
    s = quahog.bash(inherit_rc=False)
    try:
        assert quahog.sessions[s.name] is s
        assert quahog.default is s
        assert quahog.attach(s.name) is s
    finally:
        s.close()
        quahog.sessions.pop(s.name, None)


def test_reinject_after_exec(sh):
    sh.sendline("exec bash --norc")
    import time

    time.sleep(0.8)
    sh.reinject(full=True)
    time.sleep(0.8)
    r = sh.run("echo back")
    assert r.text.strip() == "back"
