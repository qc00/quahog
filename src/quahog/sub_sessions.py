"""CommandResult/MultiResult (run()'s durable, display()-able records) and
ExecSession (exec()'s own live handle, PLAN.md §3).

Design rule from PLAN.md §1: the live console is ephemeral, text is canonical.
The repr always includes text/plain (that is what lands in the .ipynb JSON and
what git and LLMs see) plus a quahog mimetype carrying the ANSI-preserved raw
stream for rich client-side re-rendering later.

ExecSession is unlike fork() (which needs kernel-local FIFOs, so it is
local-only): exec() rides whatever PTY the session is currently sitting in —
including a remote host reached by navigating there interactively — so it
needs nothing on the far end but ``socat`` (to give the command a pty, keeping
``isatty()`` true) and a small escape-stripping filter (``__qua_xf``, injected).

A PTY is one merged stream and the kernel can't tell one program's bytes from
another's on it, so exec's output is *tagged*: the far-end filter strips escape
sequences from the command's pty output (which both yields clean text and
guarantees the payload can't contain the BEL/ST that would break the frame — so
no base64) and wraps each chunk as ``OSC 2607;QUA;O;<id>;<clean text>``;
completion emits ``OSC 2607;QUA;X;<id>;<rc>``. This per-chunk self-identifying
tagging — not the OSC 133 C/D time-brackets run() relies on — is what makes
``background=True`` work: a backgrounded job's output interleaves on the one tty
with the live shell, and only self-identifying chunks can be attributed to it.

One honest limit (PLAN.md §11): the captured output is escape-stripped clean
text, so full-screen/binary fidelity lives in the console or a screenshot(), not
here.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from .session import Session

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
        # Set by Session.exec() when this result tracks a foreground exec
        # (PLAN.md §3): lets the OSC 133;D handler complete es.wait() once the
        # shell is back at its prompt.
        self._exec: Optional["ExecSession"] = None

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
        # A PTY merges stdout and stderr by nature; fork() is the API for
        # genuinely separate streams.
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

    def _repr_mimebundle_(
        self, include: Optional[Iterable[str]] = None, exclude: Optional[Iterable[str]] = None
    ) -> Dict[str, Any]:
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

    def __iter__(self) -> Iterator[CommandResult]:
        return iter(self.results)

    def __getitem__(self, i: int) -> CommandResult:
        return self.results[i]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def _repr_mimebundle_(
        self, include: Optional[Iterable[str]] = None, exclude: Optional[Iterable[str]] = None
    ) -> Dict[str, Any]:
        return {
            "text/plain": "\n".join(r._plain() for r in self.results),
            "application/vnd.quahog.output+json": [
                r._repr_mimebundle_()["application/vnd.quahog.output+json"]
                for r in self.results
            ],
        }

    def __repr__(self) -> str:
        return "\n".join(r._plain() for r in self.results)


class _ExecStdin:
    """sys.stdin-shaped input for a *foreground* exec: the bytes go to the
    session PTY, which socat is reading, so they reach the command. A
    backgrounded exec can't read the controlling tty (a documented gap,
    PLAN.md §3), so writing raises."""

    class _Raw:
        """Byte layer: es.stdin.raw.write(b"\\x03")."""

        def __init__(self, es: "ExecSession") -> None:
            self._es = es

        def write(self, data: bytes, record: bool = True) -> int:
            self._es._feed(data, record)
            return len(data)

        def flush(self) -> None:
            pass

    def __init__(self, es: "ExecSession") -> None:
        self._es = es
        self.raw = _ExecStdin._Raw(es)

    def write(self, data: str, record: bool = True) -> int:
        self._es._feed(data.encode(), record)
        return len(data)

    def flush(self) -> None:
        pass


class ExecSession:
    """A command running over the session's PTY, with its own captured
    ``text``/``raw``/``returncode`` and stdin. Deliberately session-shaped."""

    def __init__(self, session: "Session", eid: str, command: str, background: bool, mirror: bool) -> None:
        self.session_name = session.name
        self.command = command
        self.eid = eid
        self.background = background
        self.mirror = mirror
        self.pid: Optional[int] = None
        self.returncode: Optional[int] = None
        self._session = session
        self._parts: List[str] = []
        self._done = threading.Event()
        self.stdin = _ExecStdin(self)

    # -- feeding (called from the reader thread on O/X tokens) --------------
    def _on_output(self, text: str) -> None:
        self._parts.append(text)

    def _feed(self, data: bytes, record: bool) -> None:
        if self.background:
            raise RuntimeError("cannot feed stdin to a backgrounded exec (it has no controlling tty)")
        if self._done.is_set():
            raise RuntimeError("exec already finished")
        self._session._input(data, record=record)

    # -- content -------------------------------------------------------------
    @property
    def raw(self) -> str:
        """The command's escape-stripped output as the far-end filter framed it."""
        return "".join(self._parts)

    @property
    def text(self) -> str:
        """Clean text form of the captured output."""
        return clean_text(self.raw)

    @property
    def stdout(self) -> str:
        # exec rides one merged PTY stream; fork() is the API for split streams.
        return self.text

    # -- status --------------------------------------------------------------
    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def poll(self) -> Optional[int]:
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if not self._done.wait(timeout):
            raise TimeoutError(f"exec still running after {timeout}s: {self.command!r}")
        return self.returncode

    def send(self, data: Union[bytes, str], record: bool = True) -> None:
        self.stdin.write(data if isinstance(data, str) else data.decode("utf-8", "replace"), record=record)

    def sendline(self, line: str = "", record: bool = True) -> None:
        self.send(line + "\n", record=record)

    # -- display -------------------------------------------------------------
    def _plain(self) -> str:
        head = f"$ {self.command}  # exec"
        if self.background:
            head += " &"
        rc = self.poll()
        body = self.text.rstrip("\n")
        parts = [head]
        if body:
            parts.append(body)
        if rc is None:
            parts.append("… running")
        elif rc != 0:
            parts.append(f"[exit {rc}]")
        return "\n".join(parts)

    def _repr_mimebundle_(
        self, include: Optional[Iterable[str]] = None, exclude: Optional[Iterable[str]] = None
    ) -> Dict[str, Any]:
        return {
            "text/plain": self._plain(),
            "application/vnd.quahog.output+json": {
                "session": self.session_name,
                "command": self.command,
                "exec": True,
                "background": self.background,
                "output": self.raw,
                "returncode": self.returncode,
            },
        }

    def __repr__(self) -> str:
        return self._plain()
