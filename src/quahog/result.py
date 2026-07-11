"""CommandResult: the durable, display()-able record of one command.

Design rule from PLAN.md §1: the live console is ephemeral, text is canonical.
The repr always includes text/plain (that is what lands in the .ipynb JSON and
what git and LLMs see) plus a quahog mimetype carrying the ANSI-preserved raw
stream for rich client-side re-rendering later.
"""

from __future__ import annotations

import re
import threading
import time
from typing import List, Optional

# CSI sequences, other single-ESC sequences, and C0 controls except \n, \t, \r.
_STRIP_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI ... final
    r"|\x1b[@-Z\\_^\]]"  # other ESC-x (OSC never reaches here; the parser eats it)
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)


def clean_text(raw: str) -> str:
    """Strip escape sequences and emulate carriage-return overwrites."""
    s = raw.replace("\r\n", "\n")
    s = _STRIP_RE.sub("", s)
    if "\r" not in s:
        return s
    lines = []
    for line in s.split("\n"):
        if "\r" in line:
            out = ""
            for seg in line.split("\r"):
                out = seg + out[len(seg) :]
            line = out
        lines.append(line)
    return "\n".join(lines)


class CommandResult:
    """Outcome of one command run in a session.

    ``raw`` is the byte-faithful output (escape sequences included, decoded as
    UTF-8); ``text`` is clean text. ``wait()`` blocks until the shell reports
    the command finished (OSC 133;D).
    """

    def __init__(self, session_name: str, command: str) -> None:
        self.session_name = session_name
        self.command = command
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.returncode: Optional[int] = None
        # Extra text blocks attached by interceptors (e.g. a vim diff); they
        # render after the output in the cell (PLAN.md §6).
        self.notes: List[str] = []
        self._buf = bytearray()
        self._capturing = False
        self._done = threading.Event()

    # -- state ---------------------------------------------------------------
    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def wait(self, timeout: Optional[float] = None) -> "CommandResult":
        if not self._done.wait(timeout):
            raise TimeoutError(
                f"command still running after {timeout}s: {self.command!r}"
            )
        return self

    def _finish(self, returncode: Optional[int]) -> None:
        self.returncode = returncode
        self.finished_at = time.time()
        self._capturing = False
        self._done.set()

    # -- content -------------------------------------------------------------
    @property
    def raw_bytes(self) -> bytes:
        return bytes(self._buf)

    @property
    def raw(self) -> str:
        return self.raw_bytes.decode("utf-8", "replace")

    @property
    def text(self) -> str:
        return clean_text(self.raw)

    @property
    def stdout(self) -> str:
        # A PTY merges stdout and stderr by nature; fork() (later milestone)
        # is the API for genuinely separate streams.
        return self.text

    # -- display -------------------------------------------------------------
    def _plain(self) -> str:
        body = self.text.rstrip("\n")
        head = f"$ {self.command}"
        parts = [head]
        if body:
            parts.append(body)
        if not self.done:
            parts.append("… running")
        elif self.returncode not in (0, None):
            parts.append(f"[exit {self.returncode}]")
        parts.extend(self.notes)
        return "\n".join(parts)

    def _repr_mimebundle_(self, include=None, exclude=None):
        return {
            "text/plain": self._plain(),
            "application/vnd.quahog.output+json": {
                "session": self.session_name,
                "command": self.command,
                "raw": self.raw,
                "returncode": self.returncode,
                "notes": list(self.notes),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            },
        }

    def __repr__(self) -> str:
        return self._plain()


class MultiResult:
    """Results of the commands of one %%qua cell, displayed as one output."""

    def __init__(self, results: List[CommandResult]) -> None:
        self.results = results

    def __iter__(self):
        return iter(self.results)

    def __getitem__(self, i):
        return self.results[i]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def _repr_mimebundle_(self, include=None, exclude=None):
        return {
            "text/plain": "\n".join(r._plain() for r in self.results),
            "application/vnd.quahog.output+json": [
                r._repr_mimebundle_()["application/vnd.quahog.output+json"]
                for r in self.results
            ],
        }

    def __repr__(self) -> str:
        return "\n".join(r._plain() for r in self.results)
