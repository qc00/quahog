"""ForkHandle: a command launched by a session with fresh std streams.

fork() is the escape hatch from the PTY's merged output (PLAN.md §3): the
injected __qua_fork helper runs ``sh -c CMD < f0 > f1 2> f2 &`` on a FIFO trio
the kernel created, so stdout and stderr are genuinely separate. The handle is
deliberately subprocess.Popen-shaped. The exit status is written to a file by
the helper (the process is the shell's child, not ours, so no waitpid).
"""

from __future__ import annotations

import os
import shutil
import signal
import threading
import time
from typing import Optional

from .result import clean_text


class _Drain(threading.Thread):
    """Reads one FIFO to EOF into a buffer, so writers never block."""

    def __init__(self, path: str, label: str) -> None:
        super().__init__(name=f"quahog-fork-{label}", daemon=True)
        self._path = path
        self.buf = bytearray()

    def run(self) -> None:
        fd = os.open(self._path, os.O_RDONLY)  # blocks until the writer opens
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                self.buf += chunk
        except OSError:
            pass
        finally:
            os.close(fd)


class ForkHandle:
    """Popen-shaped handle for one forked command."""

    def __init__(self, session_name: str, command: str, dirpath: str) -> None:
        self.session_name = session_name
        self.command = command
        self.pid: Optional[int] = None
        self._dir = dirpath
        self._rc: Optional[int] = None
        self._stdin_fd: Optional[int] = None
        self._opened = threading.Event()

        self._out = _Drain(os.path.join(dirpath, "1"), "out")
        self._err = _Drain(os.path.join(dirpath, "2"), "err")

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
                pass

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

    def send(self, data, record: bool = True) -> None:
        if self._stdin_fd is None:
            raise RuntimeError("stdin not connected yet")
        os.write(self._stdin_fd, data if isinstance(data, bytes) else str(data).encode())

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
                pass
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

    def _repr_mimebundle_(self, include=None, exclude=None):
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
