"""ForkSession: a command launched by a session with fresh std streams.

fork() is the escape hatch from the PTY's merged output (PLAN.md §3): the
injected __qua_fork helper runs ``sh -c CMD < f0 > f1 2> f2 &`` on a FIFO trio
the kernel created, so stdout and stderr are genuinely separate. The handle is
deliberately subprocess.Popen-shaped. The exit status is written to a file by
the helper (the process is the shell's child, not ours, so no waitpid).
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, TYPE_CHECKING, Union

from . import utils
from .sub_sessions import clean_text

if TYPE_CHECKING:
    from .record import CastWriter

logger = logging.getLogger(__name__)
log_exception_min = utils.LogExceptionMinimal(logger.debug)


class _Drain(threading.Thread):
    """Reads one FIFO to EOF into a buffer, so writers never block."""

    def __init__(
        self,
        path: str,
        label: str,
        on_data: Optional[Callable[[bytes], None]] = None,
        on_eof: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(name=f"quahog-fork-{label}", daemon=True)
        self._path = path
        self._on_data = on_data
        self._on_eof = on_eof
        self.buf = bytearray()

    def run(self) -> None:
        fd = os.open(self._path, os.O_RDONLY)  # blocks until the writer opens
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                self.buf += chunk
                if self._on_data is not None:
                    with log_exception_min:
                        self._on_data(chunk)
        except OSError:
            log_exception_min()
        finally:
            os.close(fd)
            if self._on_eof is not None:
                with log_exception_min:
                    self._on_eof()


class ForkSession:
    """Popen-shaped handle for one forked command."""

    def __init__(self, session_name: str, command: str, dirpath: str, cast: Optional["CastWriter"] = None) -> None:
        self.session_name = session_name
        self.command = command
        self.pid: Optional[int] = None
        self._dir = dirpath
        self._rc: Optional[int] = None
        self._stdin_fd: Optional[int] = None
        self._opened = threading.Event()

        self._cast = cast
        self._cast_lock = threading.Lock()
        self._cast_open_streams = 2
        tee = self._tee if cast is not None else None
        eof = self._stream_eof if cast is not None else None

        self._out = _Drain(os.path.join(dirpath, "1"), "out", on_data=tee, on_eof=eof)
        self._err = _Drain(os.path.join(dirpath, "2"), "err", on_data=tee, on_eof=eof)

        # FIFO opens block until the peer opens; the shell side opens its
        # redirects in 0,1,2 order, so we must open in the same order — and in
        # a thread, because the shell hasn't even run the command yet when the
        # handle is constructed.
        def _open() -> None:
            try:
                self._stdin_fd = os.open(os.path.join(dirpath, "0"), os.O_WRONLY)
                self._out.start()
                self._err.start()
                self._opened.set()
            except OSError:
                log_exception_min()

        threading.Thread(target=_open, name="quahog-fork-open", daemon=True).start()

    # ------------------------------------------------------------- streams
    @property
    def stdout_bytes(self) -> bytes:
        return bytes(self._out.buf)

    @property
    def stderr_bytes(self) -> bytes:
        return bytes(self._err.buf)

    @property
    def stdout(self) -> str:
        return self.stdout_bytes.decode("utf-8", "replace")

    @property
    def stderr(self) -> str:
        return self.stderr_bytes.decode("utf-8", "replace")

    def _tee(self, chunk: bytes) -> None:
        with self._cast_lock:
            if self._cast is not None:
                self._cast.append("o", chunk.decode("utf-8", "replace"))

    def _stream_eof(self) -> None:
        with self._cast_lock:
            self._cast_open_streams -= 1
            if self._cast_open_streams <= 0 and self._cast is not None:
                self._cast.close()

    @property
    def cast_path(self) -> Optional[Path]:
        return self._cast.path if self._cast is not None else None

    def send(self, data: Union[bytes, str], record: bool = True) -> None:
        if self._stdin_fd is None:
            raise RuntimeError("stdin not connected yet")
        raw = data if isinstance(data, bytes) else str(data).encode()
        with self._cast_lock:
            if self._cast is not None:
                from .record import PLACEHOLDER

                self._cast.append("i", raw.decode("utf-8", "replace") if record else PLACEHOLDER)
        os.write(self._stdin_fd, raw)

    def sendline(self, line: str = "", record: bool = True) -> None:
        self.send(line + "\n")

    def close_stdin(self) -> None:
        if self._stdin_fd is not None:
            try:
                os.close(self._stdin_fd)
            finally:
                self._stdin_fd = None

    # -------------------------------------------------------------- status
    @property
    def returncode(self) -> Optional[int]:
        return self.poll()

    def poll(self) -> Optional[int]:
        if self._rc is None:
            try:
                with open(os.path.join(self._dir, "rc")) as f:
                    text = f.read().strip()
                if text:
                    self._rc = int(text)
            except (FileNotFoundError, ValueError):
                log_exception_min()
        return self._rc

    @property
    def done(self) -> bool:
        return self.poll() is not None

    def wait(self, timeout: Optional[float] = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout

        def remaining() -> Optional[float]:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        if not self._opened.wait(remaining() if deadline else 30):
            raise TimeoutError(f"fork never started: {self.command!r}")
        for t in (self._out, self._err):
            t.join(remaining())
            if t.is_alive():
                raise TimeoutError(f"fork still running: {self.command!r}")
        # The helper writes the rc file just after the streams close.
        grace = time.monotonic() + 2
        while self.poll() is None and time.monotonic() < grace:
            time.sleep(0.02)
        rc = self.poll()
        if rc is None:
            raise RuntimeError(f"fork finished but no exit status: {self.command!r}")
        return rc

    def send_signal(self, sig: int) -> None:
        if self.pid is None:
            raise RuntimeError("pid unknown")
        os.kill(self.pid, sig)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def close(self) -> None:
        self.close_stdin()
        with self._cast_lock:
            if self._cast is not None:
                self._cast.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    # ------------------------------------------------------------- display
    def _plain(self) -> str:
        head = f"$ {self.command}  # fork, pid {self.pid}"
        rc = self.poll()
        state = "… running" if rc is None else (f"[exit {rc}]" if rc else None)
        parts = [head]
        out = clean_text(self.stdout).rstrip("\n")
        err = clean_text(self.stderr).rstrip("\n")
        if out:
            parts.append(out)
        if err:
            parts.append("--- stderr ---")
            parts.append(err)
        if state:
            parts.append(state)
        return "\n".join(parts)

    def _repr_mimebundle_(
        self, include: Optional[Iterable[str]] = None, exclude: Optional[Iterable[str]] = None
    ) -> Dict[str, Any]:
        return {
            "text/plain": self._plain(),
            "application/vnd.quahog.output+json": {
                "session": self.session_name,
                "command": self.command,
                "fork": True,
                "pid": self.pid,
                "stdout": self.stdout,
                "stderr": self.stderr,
                "returncode": self.poll(),
            },
        }

    def __repr__(self) -> str:
        return self._plain()
