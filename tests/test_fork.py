import os
import sys
import time
import types

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


def test_fork_timeout_cleans_up_forkdir(sh, monkeypatch):
    """If the FIFOs never connect within the wait window, fork() must still
    remove the temp directory it created -- the except-Exception branch
    above it already did this; the separate `_opened.wait(10)` timeout
    branch used to skip it entirely, leaking a directory on every such
    failure."""
    import quahog.session as session_mod

    class FakeResult:
        text = "12345\n"

    monkeypatch.setattr(sh, "run", lambda *a, **kw: FakeResult())

    real_init = session_mod.ForkHandle.__init__

    def patched_init(self, *a, **kw):
        real_init(self, *a, **kw)
        self._opened = types.SimpleNamespace(wait=lambda timeout=None: False)

    monkeypatch.setattr(session_mod.ForkHandle, "__init__", patched_init)

    created = {}
    real_mkdtemp = session_mod.tempfile.mkdtemp

    def spy_mkdtemp(*a, **kw):
        path = real_mkdtemp(*a, **kw)
        created["path"] = path
        return path

    monkeypatch.setattr(session_mod.tempfile, "mkdtemp", spy_mkdtemp)

    with pytest.raises(RuntimeError, match="never connected"):
        sh.fork("true")

    assert "path" in created
    assert not os.path.exists(created["path"])
