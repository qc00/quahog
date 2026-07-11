"""Session: a live PTY with a Popen-shaped API, an OSC 133 tap, and views.

Architecture (PLAN.md §2): a reader thread drains the PTY and fans bytes out to
(a) the scrollback ring buffer, (b) the OSC parser driving run() capture, and
(c) any attached widget views. The canonical record of a command is the
CommandResult text; the widget is a pure client-side view.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

from .fork import ForkHandle
from .minutes import Transcript
from .osc import StreamParser
from .result import CommandResult

_INJECT_DIR = Path(__file__).parent / "inject"


class TimeoutExpired(TimeoutError):
    """run(timeout=...) expired. The command keeps running; the partial
    CommandResult is available as .result and will complete in the background."""

    def __init__(self, result: CommandResult, timeout: float) -> None:
        super().__init__(
            f"command did not finish in {timeout}s (still running): "
            f"{result.command!r}"
        )
        self.result = result


class _Ring:
    """Bounded scrollback of raw PTY bytes, replayed to newly attached views."""

    def __init__(self, cap: int = 2_000_000) -> None:
        self._chunks: deque = deque()
        self._size = 0
        self._cap = cap

    def append(self, b: bytes) -> None:
        self._chunks.append(b)
        self._size += len(b)
        while self._size > self._cap and self._chunks:
            self._size -= len(self._chunks.popleft())

    def dump(self) -> bytes:
        return b"".join(self._chunks)


class _Stdin:
    """sys.stdin-shaped input: text at the top, bytes via .buffer.

    The record= plumbing (PLAN.md §3) lands with the recording milestone; the
    parameter is accepted now so calling code doesn't change later.
    """

    class _Buffer:
        def __init__(self, session: "Session") -> None:
            self._s = session

        def write(self, data: bytes, record: bool = True) -> int:
            self._s._write(data)
            return len(data)

        def flush(self) -> None:
            pass

    def __init__(self, session: "Session") -> None:
        self._s = session
        self.buffer = _Stdin._Buffer(session)

    def write(self, data: str, record: bool = True) -> int:
        self._s._write(data.encode())
        return len(data)

    def flush(self) -> None:
        pass


class Session:
    """One live shell in a PTY. Popen vocabulary plus run()/display."""

    def __init__(
        self,
        argv,
        name: str,
        shell_kind: str = "bash",
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        record: bool = False,  # accepted per plan; recording lands in M3
        rows: int = 24,
        cols: int = 100,
    ) -> None:
        from ptyprocess import PtyProcess

        self.name = name
        self.shell_kind = shell_kind
        full_env = dict(os.environ)
        full_env.update(
            TERM="xterm-256color", QUAHOG="1", QUAHOG_SESSION=name, LANG=full_env.get("LANG", "en_US.UTF-8")
        )
        if env:
            full_env.update(env)

        self._proc = PtyProcess.spawn(
            list(argv), cwd=cwd, env=full_env, dimensions=(rows, cols)
        )
        self._rows, self._cols = rows, cols
        self._parser = StreamParser()
        self._ring = _Ring()
        self._lock = threading.Lock()
        self._active: Optional[CommandResult] = None
        self._at_prompt = threading.Event()
        self._exited = threading.Event()
        self._returncode: Optional[int] = None
        self.cwd: Optional[str] = cwd
        self._widget = None
        self._kernel = self._find_kernel()
        self.stdin = _Stdin(self)

        # Minuting (PLAN.md §5): interactive commands are captured between the
        # shell integration's C and D markers; the typed text arrives on the
        # private OSC 5522;E channel.
        self.minutes = True
        self._icap: Optional[CommandResult] = None
        self._typed_cmd: Optional[str] = None
        self._minute_q: deque = deque()
        self._transcript: Optional[Transcript] = None
        self._thandle = None

        self._reader = threading.Thread(
            target=self._read_loop, name=f"quahog-{name}", daemon=True
        )
        self._reader.start()
        # Wait for the first prompt marker so run() right after construction
        # doesn't race shell startup.
        self._at_prompt.wait(10)

    # ------------------------------------------------------------------ tap
    @staticmethod
    def _find_kernel():
        try:
            from IPython import get_ipython

            ip = get_ipython()
            return getattr(ip, "kernel", None)
        except Exception:
            return None

    def _read_loop(self) -> None:
        fd = self._proc.fd
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self._lock:
                self._ring.append(data)
                for tok in self._parser.feed(data):
                    self._on_token(tok)
            self._emit({"type": "out"}, [data])
        self._on_exit()

    def _on_token(self, tok) -> None:
        if tok[0] == "data":
            target = self._active if self._active is not None else self._icap
            if target is not None and target._capturing:
                target._buf += tok[1]
            return
        _, num, payload = tok
        if num == "133":
            code = payload[:1]
            if code == "C":
                if self._active is not None:
                    self._active._capturing = True
                else:
                    # Interactively typed command: capture it the same way.
                    # zsh's preexec delivered the text on 5522;E already; bash
                    # delivers it in precmd, just before D.
                    self._icap = CommandResult(self.name, self._typed_cmd or "")
                    self._icap._capturing = True
                    self._typed_cmd = None
            elif code == "D":
                parts = payload.split(";")
                rc = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
                if self._active is not None:
                    active, self._active = self._active, None
                    # The E marker fired for this run() command too; drop the
                    # stash or the next *interactive* command inherits it.
                    self._typed_cmd = None
                    active._finish(rc)
                elif self._icap is not None:
                    icap, self._icap = self._icap, None
                    if not icap.command:
                        icap.command = self._typed_cmd or ""
                    self._typed_cmd = None
                    icap._finish(rc)
                    self._record_interactive(icap)
            elif code in ("A", "B"):
                if code == "A" and self._icap is not None:
                    # A fresh prompt while a capture is still open: the command
                    # never produced a D (exec, exit into a nested shell, lost
                    # integration). Close it out honestly.
                    icap, self._icap = self._icap, None
                    icap._finish(None)
                    self._record_interactive(icap)
                self._at_prompt.set()
        elif num == "5522":
            kind, _, rest = payload.partition(";")
            if kind == "E":
                if self._icap is not None and not self._icap.command:
                    self._icap.command = rest.strip()
                else:
                    self._typed_cmd = rest.strip()
        elif num == "7" and payload.startswith("file://"):
            rest = payload[len("file://") :]
            slash = rest.find("/")
            if slash != -1:
                self.cwd = rest[slash:]

    def _record_interactive(self, result: CommandResult) -> None:
        command = result.command.strip()
        if not command:
            return
        if "__qua_" in command or str(_INJECT_DIR) in command:
            return  # quahog's own plumbing (reinject, fork helper) isn't minuted
        transcript = self._transcript
        if transcript is not None:
            transcript.append(result)
            self._update_transcript()
        if self.minutes:
            self._minute_q.append(result)

    def _drain_minutes(self) -> List[CommandResult]:
        out = []
        while self._minute_q:
            out.append(self._minute_q.popleft())
        return out

    def _update_transcript(self) -> None:
        handle, transcript = self._thandle, self._transcript
        if handle is None or transcript is None:
            return

        def _update() -> None:
            try:
                handle.update(transcript)
            except Exception:
                pass

        loop = getattr(self._kernel, "io_loop", None)
        if loop is not None:
            loop.add_callback(_update)
        else:
            _update()

    def _on_exit(self) -> None:
        try:
            self._proc.wait()
        except Exception:
            pass
        self._returncode = self._proc.exitstatus
        if self._returncode is None and self._proc.signalstatus is not None:
            self._returncode = -self._proc.signalstatus
        with self._lock:
            active, self._active = self._active, None
        if active is not None:
            active._finish(self._returncode)
        self._exited.set()
        self._at_prompt.set()  # unblock anyone waiting on a dead shell
        self._emit({"type": "exited", "code": self._returncode}, [])

    # ------------------------------------------------------------- commands
    def run(
        self,
        command: str,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Type ``command`` into the shell; capture output between the shell
        integration's C and D markers. Returns a CommandResult."""
        command = command.strip()
        if not command:
            raise ValueError("empty command")
        if "\n" in command:
            raise ValueError(
                "multi-line commands are not supported yet; use %%qua or join with '&&'"
            )
        if self._exited.is_set():
            raise RuntimeError(f"session {self.name!r} has exited")
        with self._lock:
            if self._active is not None:
                raise RuntimeError(
                    f"session {self.name!r} is busy running: {self._active.command!r}"
                )
            if self._icap is not None:
                raise RuntimeError(
                    f"session {self.name!r} is busy with an interactive command: "
                    f"{self._icap.command!r}"
                )
            result = CommandResult(self.name, command)
            self._active = result
            self._typed_cmd = None
            self._at_prompt.clear()
        self._write(command.encode() + b"\r")
        if wait:
            if not result._done.wait(timeout):
                raise TimeoutExpired(result, timeout)
        return result

    def fork(self, command: str, timeout: float = 15.0) -> ForkHandle:
        """Run ``command`` with fresh std streams and its own handle.

        The kernel creates a FIFO trio; the injected __qua_fork helper starts
        ``sh -c command`` redirected onto it in the background, so the session
        stays free and stdout/stderr are genuinely separate.
        """
        forkdir = tempfile.mkdtemp(prefix="quaf-")
        for n in ("0", "1", "2"):
            os.mkfifo(os.path.join(forkdir, n))
        handle = ForkHandle(self.name, command, forkdir)
        try:
            r = self.run(
                f"__qua_fork {shlex.quote(forkdir)} {shlex.quote(command)}",
                timeout=timeout,
            )
            handle.pid = int(r.text.strip().splitlines()[-1])
        except Exception:
            shutil.rmtree(forkdir, ignore_errors=True)
            raise
        if not handle._opened.wait(10):
            raise RuntimeError(f"fork streams never connected: {command!r}")
        return handle

    def _write(self, data: bytes) -> None:
        os.write(self._proc.fd, data)

    # ----------------------------------------------------------- Popen face
    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._returncode

    def poll(self) -> Optional[int]:
        if not self._proc.isalive():
            self._exited.wait(5)
        return self._returncode

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if not self._exited.wait(timeout):
            raise TimeoutError(f"session {self.name!r} still running")
        return self._returncode

    def send_signal(self, sig: int) -> None:
        self._proc.kill(sig)

    def terminate(self) -> None:
        try:
            self._proc.terminate()
        except Exception:
            pass

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def interrupt(self) -> None:
        """Ctrl-C through the PTY (goes to the foreground process group)."""
        self._write(b"\x03")

    def send(self, data, record: bool = True) -> None:
        self._write(data if isinstance(data, bytes) else str(data).encode())

    def sendline(self, line: str = "", record: bool = True) -> None:
        self._write(line.encode() + b"\r")

    def resize(self, rows: int, cols: int) -> None:
        self._rows, self._cols = rows, cols
        self._proc.setwinsize(rows, cols)

    def close(self) -> None:
        self.terminate()
        if not self._exited.wait(3):
            try:
                self.kill()
            except Exception:
                pass
        self._cleanup()

    def _cleanup(self) -> None:
        tmp = getattr(self, "_tmpdir", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    # -------------------------------------------------------------- widgets
    def _send_to(self, widget, content: dict, buffers: list) -> None:
        if widget is None:
            return

        def _send() -> None:
            try:
                widget.send(content, buffers=buffers)
            except Exception:
                pass

        loop = getattr(self._kernel, "io_loop", None)
        if loop is not None:
            loop.add_callback(_send)
        else:
            _send()

    def _emit(self, content: dict, buffers: list) -> None:
        self._send_to(self._widget, content, buffers)

    def _view(self):
        from .widget import ConsoleView

        if self._widget is None:
            self._widget = ConsoleView(session_name=self.name)
            self._widget.on_msg(self._on_widget_msg)
        return self._widget

    def _on_widget_msg(self, widget, content, buffers) -> None:
        if widget is not self._widget:
            return  # a hopped-away (frozen) view; ignore its messages
        kind = content.get("type")
        if kind == "ready":
            scrollback = self._ring.dump()
            if scrollback:
                self._emit({"type": "out"}, [scrollback])
            if self._exited.is_set():
                self._emit({"type": "exited", "code": self._returncode}, [])
        elif kind == "stdin":
            if not self._exited.is_set():
                self._write(content.get("data", "").encode())
        elif kind == "resize":
            rows, cols = int(content.get("rows", 24)), int(content.get("cols", 80))
            if (rows, cols) != (self._rows, self._cols):
                try:
                    self.resize(rows, cols)
                except Exception:
                    pass

    def _ipython_display_(self) -> None:
        """Embed the live console here. If it was displayed elsewhere before,
        this is a hop (PLAN.md §4): the old embedded view freezes into a
        static snapshot and this cell becomes the anchor — new transcript
        lines and the live view both target it."""
        from IPython.display import display

        old = self._widget
        if old is not None:
            self._send_to(old, {"type": "freeze"}, [])
            self._widget = None
        display(self._view())
        self._transcript = Transcript(self.name)
        self._thandle = display(self._transcript, display_id=True)

    # ------------------------------------------------------------ integration
    def reinject(self, full: bool = False) -> None:
        """Re-type the shell integration after su / exec zsh / a nested shell.

        ``full=True`` types the whole snippet (needed when the new shell can't
        read local files, e.g. remote — later milestone); the default sources
        the snippet file, which any local shell can do.
        """
        path = _INJECT_DIR / ("zsh.zsh" if self.shell_kind == "zsh" else "posix.sh")
        if full:
            # The snippet contains '![' character classes, which a shell with
            # history expansion still enabled would mangle at read time — so
            # disable it first, as its own line. The trailing marker comment
            # keeps the guard line out of the minutes.
            if self.shell_kind == "zsh":
                self.sendline("setopt no_bang_hist # __qua_reinject")
            else:
                self.sendline("set +H # __qua_reinject")
            lines = [
                ln
                for ln in path.read_text().splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")
            ]
            self.sendline(" ; ".join(lines))
        else:
            self.sendline(f". '{path}'")

    def __repr__(self) -> str:
        state = (
            f"exited {self._returncode}"
            if self._exited.is_set()
            else ("busy" if self._active else "at prompt")
        )
        return f"<quahog.Session {self.name} ({self.shell_kind}, pid {self.pid}, {state})>"


# ----------------------------------------------------------------- factories

def _rcfile_bash(tmpdir: str, inherit_rc: bool) -> str:
    path = os.path.join(tmpdir, "bashrc")
    lines = []
    if inherit_rc:
        lines.append('[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"')
    else:
        # Isolated session: keep the in-memory history (minuting needs it) but
        # never read or write the user's ~/.bash_history.
        lines.append("HISTFILE=")
    lines.append(f". '{_INJECT_DIR / 'posix.sh'}'")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def spawn_bash(
    name: str,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    inherit_rc: bool = True,
    record: bool = False,
) -> Session:
    bash = shutil.which("bash") or "/bin/bash"
    tmpdir = tempfile.mkdtemp(prefix="quahog-")
    rcfile = _rcfile_bash(tmpdir, inherit_rc)
    s = Session(
        [bash, "--noprofile", "--rcfile", rcfile, "-i"],
        name=name,
        shell_kind="bash",
        cwd=cwd,
        env=env,
        record=record,
    )
    s._tmpdir = tmpdir
    return s


def spawn_zsh(
    name: str,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    inherit_rc: bool = True,
    record: bool = False,
) -> Session:
    zsh = shutil.which("zsh") or "/bin/zsh"
    tmpdir = tempfile.mkdtemp(prefix="quahog-")
    zdot = os.path.join(tmpdir, "zdot")
    os.makedirs(zdot, exist_ok=True)
    with open(os.path.join(zdot, ".zshrc"), "w") as f:
        if inherit_rc:
            f.write('[ -f "$HOME/.zshrc" ] && ZDOTDIR="$HOME" . "$HOME/.zshrc"\n')
        f.write(f". '{_INJECT_DIR / 'zsh.zsh'}'\n")
    s = Session(
        [zsh, "-i"],
        name=name,
        shell_kind="zsh",
        cwd=cwd,
        env=dict(env or {}, ZDOTDIR=zdot),
        record=record,
    )
    s._tmpdir = tmpdir
    return s
