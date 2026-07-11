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


def test_fork_separates_streams(sh):
    f = sh.fork("echo to-out; echo to-err 1>&2; exit 3")
    try:
        rc = f.wait(15)
        assert rc == 3
        assert f.returncode == 3
        assert f.stdout.strip() == "to-out"
        assert f.stderr.strip() == "to-err"
        assert isinstance(f.pid, int)
    finally:
        f.close()


def test_fork_stdin(sh):
    f = sh.fork("cat")
    try:
        f.sendline("hello fork")
        assert f.poll() is None
        f.close_stdin()
        assert f.wait(15) == 0
        assert f.stdout == "hello fork\n"
    finally:
        f.close()


def test_fork_does_not_block_session(sh):
    f = sh.fork("sleep 0.6; echo late-fork")
    try:
        # The session stays usable while the fork runs.
        r = sh.run("echo meanwhile")
        assert r.text.strip() == "meanwhile"
        f.wait(15)
        assert f.stdout.strip() == "late-fork"
    finally:
        f.close()


def test_fork_display_repr(sh):
    f = sh.fork("echo shown; exit 1")
    try:
        f.wait(15)
        plain = f._repr_mimebundle_()["text/plain"]
        assert "# fork" in plain
        assert "shown" in plain
        assert "[exit 1]" in plain
    finally:
        f.close()


def test_fork_shell_quoting(sh):
    f = sh.fork("printf '%s\\n' \"double 'single' $HOME\"")
    try:
        f.wait(15)
        assert "double 'single'" in f.stdout
    finally:
        f.close()
