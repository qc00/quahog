"""File copy over the PTY (PLAN.md §7): ``quahog cat`` / ``quahog tar`` /
``quahog download``.

The injected ``quahog`` function emits a private OSC and the kernel performs the
transfer over the same PTY channel — no scp, no ControlPath, working at any
navigation depth. Two directions, two framings:

- **upload** (local → remote, ``quahog cat FILE`` / ``quahog tar DIR``): the
  helper requests the bytes with ``OSC 2607;QUA;U;<mode>;<path>`` and then reads
  the tty raw; the kernel resolves the path relative to the notebook, sends a
  fixed 10-digit length header so the helper reads *exactly* that many bytes,
  then the raw bytes — binary-exact, no in-band EOF, no base64.

- **download** (remote → local, ``… | quahog download NAME``): a file can't be
  escape-stripped and its streaming size is unknown up front, so the helper
  brackets its stdin with ``OSC 2607;QUA;Ds;<name>`` … ``OSC 2607;QUA;De`` and
  base64-frames the bytes between them. The kernel decodes, saves the file, and
  renders a download box.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import tarfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from . import utils

logger = logging.getLogger(__name__)
log_exception_min = utils.LogExceptionMinimal(logger.debug)

# Fixed-width length header: the helper reads the tty in raw mode, where a
# blocking line read is awkward, so a constant 10-digit prefix lets it grab the
# count with a single fixed read before slurping exactly that many bytes.
LEN_WIDTH = 10


def resolve_upload(mode: str, path: str, base_dir: Path) -> bytes:
    """Bytes to stream for an upload request: the file (``cat``) or a tar of the
    directory (``tar``), with ``path`` resolved relative to ``base_dir``."""
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = base_dir / p
    if mode == "tar":
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            tf.add(str(p), arcname=p.name)
        return buf.getvalue()
    return p.read_bytes()


def framed_upload(data: bytes) -> bytes:
    """A length-prefixed frame the injected ``__qua_recv`` helper reads: a
    fixed ``LEN_WIDTH``-digit byte count followed by the raw bytes."""
    return f"{len(data):0{LEN_WIDTH}d}".encode() + data


def download_dir() -> Path:
    """Where remote→local downloads land: ``<notebook>.quahog/downloads/``,
    or ``console.quahog/downloads/`` in the cwd as a fallback."""
    from .record import sidecar_dir

    return sidecar_dir() / "downloads"


class DownloadBox:
    """The cell artifact for a completed ``quahog download`` (PLAN.md §7): the
    bytes are saved locally and offered as a data-URI link (VS Code's data-URI
    handling is weaker than Lab/NB7 — the saved-path line is the robust path)."""

    def __init__(self, name: str, data: bytes, path: Optional[Path]) -> None:
        self.name = name
        self.data = data
        self.path = path

    def _plain(self) -> str:
        where = f" → {self.path}" if self.path is not None else ""
        return f"[downloaded {self.name} — {len(self.data)} bytes{where}]"

    def _html(self) -> str:
        import html

        b64 = base64.b64encode(self.data).decode("ascii")
        name = html.escape(self.name)
        href = f"data:application/octet-stream;base64,{b64}"
        where = f" &middot; saved to <code>{html.escape(str(self.path))}</code>" if self.path is not None else ""
        return (
            f'<div class="quahog-download">📥 <a download="{name}" href="{href}">{name}</a> '
            f"({len(self.data)} bytes){where}</div>"
        )

    def _repr_mimebundle_(
        self, include: Optional[Iterable[str]] = None, exclude: Optional[Iterable[str]] = None
    ) -> Dict[str, Any]:
        return {"text/plain": self._plain(), "text/html": self._html()}

    def __repr__(self) -> str:
        return self._plain()


def save_download(name: str, data: bytes) -> Optional[Path]:
    """Write downloaded bytes next to the notebook; return the path (or None if
    it couldn't be written — the bytes are still on the DownloadBox)."""
    try:
        d = download_dir()
        d.mkdir(parents=True, exist_ok=True)
        # Keep the basename only: a remote-supplied name must never escape the
        # download directory.
        path = d / os.path.basename(name or "download")
        path.write_bytes(data)
        return path
    except OSError:
        log_exception_min()
        return None
