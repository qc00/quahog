from __future__ import annotations

import os
from pathlib import Path

# Fixed-width length header: the helper reads the tty in raw mode, where a
# blocking line read is awkward, so a constant 10-digit prefix lets it grab the
# count with a single fixed read before slurping exactly that many bytes.
LEN_WIDTH = 10


def resolve_upload(path: str, base_dir: Path) -> bytes:
    """Bytes to stream for a ``quacat`` request: the file at ``path``,
    resolved relative to ``base_dir`` if not already absolute."""
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = base_dir / p
    return p.read_bytes()


def framed_upload(data: bytes) -> bytes:
    """A length-prefixed frame ``quacat`` reads: a fixed ``LEN_WIDTH``-digit
    byte count followed by the raw bytes."""
    return f"{len(data):0{LEN_WIDTH}d}".encode() + data
