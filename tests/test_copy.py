"""quahog cat / tar / download — file copy over the PTY (PLAN.md §7).

The transfer rides the interactive byte stream, so the same path serves a real
remote host; here the "far side" is the local session, which drives the framing
end to end: length-framed binary-exact upload, base64-framed download, and a tar
stream. Also unit-covers the pure framing helpers.
"""

import os
import sys
import tempfile

import pytest

import quahog
from quahog import copy as qcopy

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


# ------------------------------------------------------------- unit: framing
def test_framed_upload_is_length_prefixed():
    frame = qcopy.framed_upload(b"abc")
    assert frame == b"0000000003abc"
    assert qcopy.framed_upload(b"") == b"0000000000"


def test_resolve_upload_relative_to_base(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"payload")
    assert qcopy.resolve_upload("cat", "f.txt", tmp_path) == b"payload"


def test_download_box_mimebundle():
    box = qcopy.DownloadBox("log.txt", b"hi there", None)
    bundle = box._repr_mimebundle_()
    assert "8 bytes" in bundle["text/plain"]
    assert "log.txt" in bundle["text/html"]
    assert "data:application/octet-stream;base64," in bundle["text/html"]


# ------------------------------------------------------- integration: PTY
@pytest.fixture()
def workdir():
    d = tempfile.mkdtemp(prefix="quacopy-")
    yield d


@pytest.fixture()
def sh(workdir, monkeypatch):
    # Land downloads under the temp workdir, not a console.quahog/ in the repo.
    monkeypatch.setattr(qcopy, "download_dir", lambda: __import__("pathlib").Path(workdir) / "downloads")
    s = quahog.bash(inherit_rc=False, cwd=workdir)
    yield s
    s.close()
    quahog.sessions.pop(s.name, None)


def test_upload_binary_exact(sh, workdir):
    data = bytes(range(256)) * 40  # every byte value, 10240 bytes
    src = os.path.join(workdir, "src.bin")
    dest = os.path.join(workdir, "dest.bin")
    with open(src, "wb") as f:
        f.write(data)
    r = sh.run(f"quahog cat {src} > {dest}", timeout=20)
    assert r.returncode == 0
    with open(dest, "rb") as f:
        assert f.read() == data


def test_upload_empty_file(sh, workdir):
    src = os.path.join(workdir, "empty")
    dest = os.path.join(workdir, "empty.out")
    open(src, "wb").close()
    sh.run(f"quahog cat {src} > {dest}", timeout=20)
    assert os.path.getsize(dest) == 0


def test_download_binary_exact(sh, workdir):
    data = bytes(range(256)) * 40
    src = os.path.join(workdir, "remote.bin")
    with open(src, "wb") as f:
        f.write(data)
    box = sh.download(src, name="fetched.bin")
    assert box.data == data
    assert box.name == "fetched.bin"
    assert box.path is not None and os.path.exists(box.path)
    with open(box.path, "rb") as f:
        assert f.read() == data
    # And it's recorded on the session for later reference.
    assert sh.downloads[-1] is box


def test_download_leaves_console_clean(sh, workdir):
    # The base64 payload must be diverted, not shown as command output.
    src = os.path.join(workdir, "secret.txt")
    with open(src, "w") as f:
        f.write("plain-content\n")
    before = len(sh.text)
    sh.download(src, name="secret.txt")
    tail = sh.text[before:]
    assert "==" not in tail  # no base64 padding leaked into the console


def test_tar_roundtrip(sh, workdir):
    tree = os.path.join(workdir, "tree")
    os.makedirs(tree)
    with open(os.path.join(tree, "a.txt"), "w") as f:
        f.write("alpha")
    with open(os.path.join(tree, "b.txt"), "w") as f:
        f.write("beta")
    dest = os.path.join(workdir, "out")
    os.makedirs(dest)
    r = sh.run(f"quahog tar {tree} | (cd {dest} && tar x)", timeout=20)
    assert r.returncode == 0
    assert open(os.path.join(dest, "tree", "a.txt")).read() == "alpha"
    assert open(os.path.join(dest, "tree", "b.txt")).read() == "beta"


def test_upload_twin_method(sh, workdir):
    src = os.path.join(workdir, "u.txt")
    dest = os.path.join(workdir, "u.out")
    with open(src, "w") as f:
        f.write("uploaded via method\n")
    sh.upload(src, dest)
    assert open(dest).read() == "uploaded via method\n"
