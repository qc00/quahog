import shutil
import sys
import time

import pytest

import quahog

pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("zsh"), reason="needs zsh")


@pytest.fixture()
def zs():
    s = quahog.zsh(inherit_rc=False)
    yield s
    s.close()
    quahog.sessions.pop(s.name, None)


def test_zsh_run(zs):
    r = zs.run("echo from-zsh")
    assert r.text.strip() == "from-zsh"
    assert r.returncode == 0
    assert zs.run("false").returncode == 1


def test_zsh_interactive_minuted(zs):
    zs.sendline("echo zsh-typed")
    deadline = time.monotonic() + 5
    while not zs.minutes and time.monotonic() < deadline:
        time.sleep(0.05)
    assert zs.minutes, "zsh interactive command not captured"
    m = zs.minutes[0]
    assert m.command == "echo zsh-typed"
    assert zs.text[m.text].strip() == "zsh-typed"
    assert zs.dump_minutes_as_cell() is None
    assert zs.last_dump == 1


def test_zsh_fork(zs):
    f = zs.fork("echo zf-out; echo zf-err 1>&2")
    try:
        assert f.wait(15) == 0
        assert f.stdout.strip() == "zf-out"
        assert f.stderr.strip() == "zf-err"
    finally:
        f.close()


@pytest.mark.skipif(not shutil.which("socat") or not shutil.which("perl"), reason="exec needs socat+perl")
def test_zsh_exec(zs):
    es = zs.exec("echo zsh-exec; exit 3")
    assert es.wait(15) == 3
    assert "zsh-exec" in es.text


def test_zsh_copy_roundtrip(zs, tmp_path, monkeypatch):
    from quahog import copy as qcopy

    monkeypatch.setattr(qcopy, "download_dir", lambda: tmp_path / "downloads")
    data = bytes(range(256)) * 8
    src = tmp_path / "s.bin"
    src.write_bytes(data)
    box = zs.download(str(src), name="z.bin")
    assert box.data == data
    dest = tmp_path / "o.bin"
    zs.upload(str(src), str(dest))
    assert dest.read_bytes() == data
