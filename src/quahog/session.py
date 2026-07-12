"""Session: a live PTY with a Popen-shaped API, an OSC 133 tap, and views.

Architecture (PLAN.md §2): a reader thread drains the PTY and fans bytes out to
(a) the scrollback ring buffer, (b) the OSC parser driving run() capture, and
(c) any attached widget views. The canonical record of a command is the
CommandResult text; the widget is a pure client-side view.
"""

from __future__ import annotations

import datetime as _dt
import os
import shlex
import shutil
import signal
import tempfile
import threading
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Union

from . import interceptors as _interceptors
from .fork import ForkHandle
from .minutes import Note, Transcript
from .record import Recorder
from .result import CommandResult
from .state import SessionState

try:
    from .screen import ScreenMirror
except Exception:  # pyte missing: sessions still work, screenshots don't
    ScreenMirror = None


class _LastDump:
    """Sentinel: 'everything since the previous dump' (see Session.last_dump)."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "LAST_DUMP"


LAST_DUMP = _LastDump()


class Minute(NamedTuple):
    """One interactively typed command. ``raw`` and ``text`` are slice objects
    into the session-lifetime streams ``Session.raw`` / ``Session.text``."""

    when: _dt.datetime
    command: str
    raw: slice
    text: slice
    returncode: Optional[int] = None


_INJECT_DIR = Path(__file__).parent / "inject"


class TimeoutExpired(TimeoutError):
    """run(timeout=...) expired. The command keeps running; the partial
    CommandResult is available as .result and will complete in the background."""

    def __init__(self, result: CommandResult, timeout: float) -> None:
        super().__init__(f"command did not finish in {timeout}s (still running): " f"{result.command!r}")
        self.result = result


class _Stdin:
    """sys.stdin-shaped input: text at the top, bytes via .buffer.

    ``record=False`` (or the ``.raw`` variant) sends the bytes to the PTY but
    leaves only an ``[input suppressed]`` placeholder in the .cast — the
    sanctioned path for feeding secrets from a keyring/vault into an
    interactive prompt without them ever touching disk (PLAN.md §3).
    """

    class _Buffer:
        def __init__(self, session: "Session") -> None:
            self._s = session

        def write(self, data: bytes, record: bool = True) -> int:
            self._s._input(data, record=record)
            return len(data)

        def flush(self) -> None:
            pass

    class _Raw:
        """Unrecorded byte layer: h.stdin.raw.write(b"...")."""

        def __init__(self, session: "Session") -> None:
            self._s = session

        def write(self, data) -> int:
            data = data if isinstance(data, bytes) else str(data).encode()
            self._s._input(data, record=False)
            return len(data)

        def flush(self) -> None:
            pass

    def __init__(self, session: "Session") -> None:
        self._s = session
        self.buffer = _Stdin._Buffer(session)
        self.raw = _Stdin._Raw(session)

    def write(self, data: str, record: bool = True) -> int:
        self._s._input(data.encode(), record=record)
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
        record: bool = False,
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

        self._proc = PtyProcess.spawn(list(argv), cwd=cwd, env=full_env, dimensions=(rows, cols))
        self._rows, self._cols = rows, cols
        self._at_prompt = threading.Event()
        self._exited = threading.Event()
        self._returncode: Optional[int] = None
        self.cwd: Optional[str] = cwd
        self._kernel = self._find_kernel()
        self.stdin = _Stdin(self)
        self._transcript = Transcript(name)

        # Recording & hygiene (PLAN.md §6). The recorder exists even when not
        # recording so the toolbar and interceptors always have a target.
        self._recorder = Recorder(name, on_event=self._on_rec_event)

        # Minuting (PLAN.md §5): `minuting` gates appending; `last_dump` is the
        # pull-based dump_minutes_as_cell cursor. Both are touched only from the
        # kernel thread, so they live on the façade, not in the shared state.
        self.minuting = True
        self.last_dump = 0

        # Every piece of state shared across the reader/kernel/worker threads —
        # scrollback ring, pyte mirror, OSC parser, lifetime raw/text streams,
        # the command FSM, minutes, the view registry, the input-dedup buffer —
        # lives behind one lock in SessionState (PLAN.md §3). The mirror tracks
        # the alt-screen switch that marks full-screen apps; it needs pyte,
        # which may be absent (sessions still work, screenshots don't).
        mirror = None
        if ScreenMirror is not None:
            try:
                mirror = ScreenMirror(rows, cols)
            except Exception:
                pass  # pyte missing or misbehaving: sessions still work
        self._state = SessionState(mirror=mirror)
        if record:
            self._recorder.start(rows, cols)

        self._reader = threading.Thread(target=self._read_loop, name=f"quahog-{name}", daemon=True)
        self._reader.start()
        # Wait for the first prompt marker so run() right after construction
        # doesn't race shell startup.
        self._at_prompt.wait(self._PROMPT_WAIT)

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
        st = self._state
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            # One critical section per read (PLAN.md §3): scrollback, mirror,
            # parser and the command FSM move together; feed_screen() also
            # markers the clean-text log when a full-screen app exits.
            with st.lock:
                st.append_output(data)
                alt = st.feed_screen(data)
                for tok in st.parser.feed(data):
                    self._on_token(tok)
            self._recorder.output(data)
            if alt is not None:
                self._emit({"type": "altscreen", "on": alt}, [])
            self._emit({"type": "out"}, [data])
        self._on_exit()

    def _close_icap(self, rc: Optional[int]) -> None:
        st = self._state
        icap, st.icap = st.icap, None
        marks, st.icap_marks = st.icap_marks, None
        if not icap.command:
            icap.command = st.typed_cmd or ""
        st.typed_cmd = None
        self._ifinish(icap)
        icap._finish(rc)
        raw_s = slice(marks[0], st.raw_len) if marks else slice(0, 0)
        text_s = slice(marks[1], st.text_boundary()) if marks else slice(0, 0)
        self._record_interactive(icap, raw_s, text_s)

    def _on_token(self, tok) -> None:
        st = self._state
        if tok[0] == "data":
            if not st.altscreen:
                st.ingest(tok[1])
            target = st.active if st.active is not None else st.icap
            if target is not None and target._capturing:
                target._buf += tok[1]
            if st.iactive:
                text = tok[1].decode("utf-8", "replace")
                for itc, ctx in st.iactive:
                    fn = getattr(itc, "on_output", None)
                    if fn is not None:
                        try:
                            fn(ctx, text)
                        except Exception:
                            pass
            return
        _, num, payload = tok
        if num == "133":
            code = payload[:1]
            if code == "C":
                if st.active is not None:
                    st.active._capturing = True
                    self._istart(st.active)
                else:
                    # Interactively typed command: capture it the same way.
                    # Both shells deliver the text on 2607;QUA;E just before C
                    # (zsh from preexec; bash from the DEBUG trap); bash sends
                    # a history-accurate correction in precmd, before D.
                    st.icap = CommandResult(self.name, st.typed_cmd or "")
                    st.icap._capturing = True
                    if self._recorder.suppressed:
                        # Typed while input suppression is active (PLAN.md §5):
                        # likely a secret misread as a command — never minuted.
                        st.icap._qs_suppressed = True
                    st.icap_marks = (st.raw_len, st.text_boundary())
                    st.typed_cmd = None
                    self._istart(st.icap)
            elif code == "D":
                parts = payload.split(";")
                rc = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
                if st.active is not None:
                    active, st.active = st.active, None
                    # The E marker fired for this run() command too; drop the
                    # stash or the next *interactive* command inherits it.
                    st.typed_cmd = None
                    self._ifinish(active)
                    active._finish(rc)
                    self._refresh_views()
                elif st.icap is not None:
                    self._close_icap(rc)
            elif code in ("A", "B"):
                if code == "A" and st.icap is not None:
                    # A fresh prompt while a capture is still open: the command
                    # never produced a D (exec, exit into a nested shell, lost
                    # integration). Close it out honestly.
                    self._close_icap(None)
                self._at_prompt.set()
        elif num == "2607":
            sig, _, body = payload.partition(";")
            if sig != "QUA":
                return  # foreign use of OSC 2607 — ignore (PLAN.md §7)
            kind, _, rest = body.partition(";")
            if kind == "E":
                cmd = rest.strip()
                if st.icap is not None:
                    # bash's precmd E carries the history-accurate line; it
                    # supersedes the DEBUG-trap guess the capture opened with.
                    if cmd:
                        st.icap.command = cmd
                else:
                    st.typed_cmd = cmd
            elif kind == "I":
                self._on_handshake(rest)
        elif num == "7" and payload.startswith("file://"):
            rest = payload[len("file://") :]
            slash = rest.find("/")
            if slash != -1:
                self.cwd = rest[slash:]

    # ------------------------------------------------------ integration
    def _on_handshake(self, rest: str) -> None:
        # OSC 2607;QUA;I;<kind>;<host>;<user> — confirming a successful (re)inject.
        parts = rest.split(";")
        if parts and parts[0] in ("bash", "zsh"):
            self.shell_kind = parts[0]

    def _record_interactive(self, result: CommandResult, raw_s: slice, text_s: slice) -> None:
        command = result.command.strip()
        if not command:
            return
        if "__qua_" in command or str(_INJECT_DIR) in command:
            return  # quahog's own plumbing (reinject, fork helper) isn't minuted
        if getattr(result, "_qs_suppressed", False):
            return  # typed while input suppression was active: never minuted
        for note in result.notes:
            # Not literal PTY bytes (an interceptor's vim diff etc.), so it
            # doesn't already appear in self.text — surface it on the
            # separate notes output instead (PLAN.md §6).
            self._transcript.append(Note(note))
        if self.minuting:
            self._state.append_minute(
                Minute(
                    when=_dt.datetime.now(),
                    command=command,
                    raw=raw_s,
                    text=text_s,
                    returncode=result.returncode,
                )
            )
        self._refresh_views()
        if result.notes:
            self._refresh_notes()

    # ----------------------------------------------------------- interceptors
    def _istart(self, result: CommandResult) -> None:
        """Match interceptors (PLAN.md §6) against the command that just
        started; run their before() hooks. Called at the OSC 133;C marker."""
        self._state.iactive = []
        command = (result.command or "").strip()
        if not command or "__qua_" in command:
            return
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = command.split()
        if not argv:
            return
        for itc in _interceptors.all_interceptors():
            try:
                if not itc.match(argv, self):
                    continue
                ctx = _interceptors.Ctx(self, argv, command)
                fn = getattr(itc, "before", None)
                if fn is not None:
                    fn(ctx)
                self._state.iactive.append((itc, ctx))
            except Exception:
                pass

    def _ifinish(self, result: Optional[CommandResult]) -> None:
        """Command ended (OSC 133;D or forced close): run after() hooks,
        attach any returned text to the result, drop leaked suppressions."""
        active, self._state.iactive = self._state.iactive, []
        for itc, ctx in active:
            out = None
            try:
                fn = getattr(itc, "after", None)
                out = fn(ctx) if fn is not None else None
            except Exception:
                out = None
            ctx._release_all()
            if out and result is not None:
                result.notes.append(str(out))

    # ------------------------------------------------------ lifetime streams
    @property
    def raw(self) -> str:
        """The session's data stream since start: escapes kept, markers
        stripped. Minute.raw slices index into this."""
        return self._state.raw()

    @property
    def text(self) -> str:
        """Clean-text form of ``raw``. Minute.text slices index into this."""
        return self._state.text()

    @property
    def altscreen(self) -> bool:
        """Whether a full-screen app currently owns the alt-screen (PLAN.md §6)."""
        return self._state.altscreen

    @property
    def minutes(self) -> list:
        """Interactively typed commands, each a Minute (PLAN.md §5)."""
        return self._state.minutes_snapshot()

    def dump_minutes_as_cell(
        self,
        since: Union[int, _dt.date, _dt.datetime, _LastDump] = LAST_DUMP,
        until: Union[int, _dt.date, _dt.datetime, None] = None,
        prefix_per_cmd: Optional[bool] = True,
    ) -> None:
        """Turn tracked interactive commands into a new (unexecuted) cell.

        ``since``/``until`` select from ``self.minutes`` — by list index, by
        date/datetime, or (the default) everything since the previous dump
        (``LAST_DUMP`` sentinel; the resolved index lives in ``self.last_dump``).
        ``prefix_per_cmd``: True → one ``%qua cmd`` line each (easy to split
        into separate cells); False → a single ``%%qua`` header; None → bare
        commands.

        The cell is created via a ``set_next_input`` payload riding the
        current execution, which works in JupyterLab *and* VS Code — but
        frontends honor only one payload per execution, so call this once per
        cell.
        """
        entries = self._state.minutes_snapshot()

        def _index(bound, default: int) -> int:
            if bound is None:
                return default
            if isinstance(bound, _LastDump):
                return min(self.last_dump, len(entries))
            if isinstance(bound, int):
                return bound
            if isinstance(bound, _dt.datetime):
                dt = bound
            elif isinstance(bound, _dt.date):
                dt = _dt.datetime.combine(bound, _dt.time.min)
            else:
                raise TypeError(f"unsupported bound: {bound!r}")
            for i, m in enumerate(entries):
                if m.when >= dt:
                    return i
            return len(entries)

        start = _index(since, 0)
        end = _index(until, len(entries))
        selected = entries[start:end]
        self.last_dump = end if end >= 0 else len(entries) + end
        if not selected:
            return

        import quahog

        is_default = quahog.default is self
        commands = [m.command for m in selected]
        if prefix_per_cmd is True:
            prefix = "%qua " if is_default else f"%qua -s {self.name} "
            text = "\n".join(prefix + c for c in commands)
        elif prefix_per_cmd is False:
            head = "%%qua" if is_default else f"%%qua {self.name}"
            text = head + "\n" + "\n".join(commands)
        else:
            text = "\n".join(commands)

        try:
            from IPython import get_ipython

            ip = get_ipython()
            if ip is not None and getattr(ip, "payload_manager", None) is not None:
                ip.payload_manager.write_payload({"source": "set_next_input", "text": text, "replace": False})
        except Exception:
            pass

    def _refresh_views(self) -> None:
        """Push the current session text to every live view's primary output
        (PLAN.md §1): plain, literal console text — what a non-widget
        renderer (git diff, nbconvert, an LLM) sees for this cell."""
        views = self._state.views_snapshot()
        if not views:
            return
        text = self.text

        def _update() -> None:
            for widget, handle, _notes, _header in views:
                try:
                    widget._text = text
                    handle.update(widget)
                except Exception:
                    pass

        loop = getattr(self._kernel, "io_loop", None)
        if loop is not None:
            loop.add_callback(_update)
        else:
            _update()

    def _refresh_notes(self) -> None:
        """Push the current notes (interceptor output, the recording
        indicator — not literal PTY bytes) to every live view's second
        output (PLAN.md §6). That output starts empty/invisible; this is what
        makes it appear at all, as its own distinct block rather than buried
        in the primary output's hidden text/plain fallback. Screenshots don't
        go through here — see ``_publish_note_to``."""
        views = self._state.views_snapshot()
        if not views:
            return
        transcript = self._transcript

        def _update() -> None:
            for _widget, _handle, notes, _header in views:
                try:
                    notes.update(transcript)
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
        st = self._state
        with st.lock:
            active, st.active = st.active, None
            if st.icap is not None:
                self._close_icap(self._returncode)
            elif active is not None:
                self._ifinish(active)
        if active is not None:
            active._finish(self._returncode)
            self._refresh_views()
        self._exited.set()
        self._at_prompt.set()  # unblock anyone waiting on a dead shell
        self._recorder.close()
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
            raise ValueError("multi-line commands are not supported yet; use %%qua or join with '&&'")
        if self._exited.is_set():
            raise RuntimeError(f"session {self.name!r} has exited")
        if self.altscreen:
            raise RuntimeError(
                f"session {self.name!r} is inside a full-screen app (alt-screen); "
                "run() is disabled — interact via the console or screenshot()"
            )
        st = self._state
        with st.lock:
            if st.active is not None:
                raise RuntimeError(f"session {self.name!r} is busy running: {st.active.command!r}")
            if st.icap is not None:
                raise RuntimeError(
                    f"session {self.name!r} is busy with an interactive command: " f"{st.icap.command!r}"
                )
            result = CommandResult(self.name, command)
            st.active = result
            st.typed_cmd = None
            self._at_prompt.clear()
        self._input(command.encode() + b"\r")
        if wait:
            if not result._done.wait(timeout):
                raise TimeoutExpired(result, timeout)
        return result

    def fork(self, command: str, timeout: float = 15.0, record: Optional[bool] = None) -> ForkHandle:
        """Run ``command`` with fresh std streams and its own handle.

        The kernel creates a FIFO trio; the injected __qua_fork helper starts
        ``sh -c command`` redirected onto it in the background, so the session
        stays free and stdout/stderr are genuinely separate.

        ``record`` defaults to the parent session's recording state; a
        fork gets its own .cast file.

        Currently supports local sessions only.
        """
        cast = self._fork_cast(record)
        forkdir = tempfile.mkdtemp(prefix="quaf-")
        for n in ("0", "1", "2"):
            os.mkfifo(os.path.join(forkdir, n))
        handle = ForkHandle(self.name, command, forkdir, cast=cast)
        try:
            r = self.run(
                f"__qua_fork {shlex.quote(forkdir)} {shlex.quote(command)}",
                timeout=timeout,
            )
            handle.pid = int(r.text.strip().splitlines()[-1])
        except Exception:
            shutil.rmtree(forkdir, ignore_errors=True)
            if cast is not None:
                cast.close()
            raise
        if not handle._opened.wait(10):
            # Same cleanup as the except branch above: the command ran (we
            # got a pid) but never connected its FIFOs, so nobody else will
            # ever remove this directory. The background thread blocked in
            # os.open() on the stdin FIFO stays stuck regardless -- it's a
            # daemon thread, so it dies with the process, but nothing short
            # of a peer opening that FIFO unblocks it sooner.
            shutil.rmtree(forkdir, ignore_errors=True)
            if cast is not None:
                cast.close()
            raise RuntimeError(f"fork streams never connected: {command!r}")
        return handle

    def _fork_cast(self, record: Optional[bool]):
        if record is None:
            record = self._recorder.recording
        if not record:
            return None
        from .record import CastWriter, sidecar_dir

        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        return CastWriter(sidecar_dir() / f"{self.name}-fork-{ts}.cast", self._cols, self._rows)

    def _write(self, data: bytes) -> None:
        os.write(self._proc.fd, data)

    def _input(self, data: bytes, record: bool = True, source=None) -> None:
        """Every user/programmatic keystroke funnels through here: dedup of
        retried/cross-view terminal-capability replies (SessionState), recording
        (with termios ECHO auto-suppression), interceptor on_input hooks, then
        the PTY."""
        if self._state.is_duplicate_input(data, source):
            return
        self._recorder.input(data, record=record, echoed=self._echo_on())
        for itc, ctx in self._state.iactive_snapshot():
            fn = getattr(itc, "on_input", None)
            if fn is not None:
                try:
                    fn(ctx, data)
                except Exception:
                    pass
        self._write(data)

    def _echo_on(self) -> Optional[bool]:
        """Whether the shell is about to echo what we type: False only for
        canonical no-echo mode (ICANON on, ECHO off) — the classic
        getpass()/``read -s`` idiom used by local password prompts, including
        sudo with pwfeedback (which paints its own ``*``s).

        Readline (and any full-screen app) also clears the kernel ECHO bit,
        but only as part of switching to raw mode (ICANON off) so it can do
        its own character-by-character echo — that state is excluded (return
        None) or normal command typing would be auto-suppressed as if it were
        a secret.
        """
        try:
            import termios

            lflag = termios.tcgetattr(self._proc.fd)[3]
            if not (lflag & termios.ICANON):
                return None  # raw mode: not classifiable this way
            return bool(lflag & termios.ECHO)
        except Exception:
            return None

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
        self._input(b"\x03")

    def send(self, data, record: bool = True) -> None:
        self._input(data if isinstance(data, bytes) else str(data).encode(), record=record)

    def sendline(self, line: str = "", record: bool = True) -> None:
        self._input(line.encode() + b"\r", record=record)

    def resize(self, rows: int, cols: int) -> None:
        self._rows, self._cols = rows, cols
        self._proc.setwinsize(rows, cols)
        self._recorder.resize(rows, cols)
        self._state.resize_screen(rows, cols)

    # ------------------------------------------------------- recording (§6)
    @property
    def recording(self) -> bool:
        return self._recorder.recording

    @property
    def cast_path(self) -> Optional[Path]:
        """The asciicast v2 sidecar this session records to, if any."""
        return self._recorder.cast_path

    def record(self, on: bool = True) -> None:
        """Runtime toggle: start/resume or pause recording. The first
        ``record(True)`` opens the .cast sidecar next to the notebook."""
        if on:
            self._recorder.start(self._rows, self._cols)
        else:
            self._recorder.set_enabled(False)

    def erase(self, count: int = 1) -> int:
        """⌫: redact the most recent keystroke(s) from the recording. Works on
        anything still inside the delayed-flush tail, regardless of what was
        or wasn't echoed. Returns how many events were rewritten."""
        return self._recorder.erase(count)

    def screenshot(self) -> Note:
        """Dump the current screen as preformatted text; returns a Note.
        Call this directly in a cell to have it appear there — the return
        value auto-displays. The toolbar's camera button does *not* go
        through here: it targets just the clicking view (see
        ``_on_widget_msg``), so a screenshot never appears under a cell it
        wasn't taken in, even if the same session is displayed elsewhere too."""
        return Note(self._snapshot_text())

    def _snapshot_text(self) -> str:
        text = self._state.screen_snapshot()
        if text is None:
            raise RuntimeError("screenshots need the 'pyte' package")
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        return f"[screen {self.name} {stamp} {self._cols}×{self._rows}]\n{text}"

    def _on_rec_event(self, kind: str, **kw) -> None:
        if kind == "echo":
            # An un-echoed or masked keystroke: flash the ⌫ affordance —
            # a prompt, not an action (PLAN.md §6).
            self._emit({"type": "echo", "cls": kw.get("cls")}, [])
        elif kind == "state":
            self._emit_rec_state()
            cast = self._recorder.cast_path
            if cast is not None:
                try:
                    self._transcript.cast = os.path.relpath(cast)
                except ValueError:
                    self._transcript.cast = str(cast)
            else:
                self._transcript.cast = None
            self._refresh_notes()  # the "[recording: ...]" line changed

    def _emit_rec_state(self, widget=None) -> None:
        p = self._recorder.cast_path
        content = {
            "type": "rec-state",
            "started": p is not None,
            "recording": self._recorder.recording,
            "cast": str(p) if p is not None else "",
        }
        if widget is not None:
            self._send_to(widget, content, [])
        else:
            self._emit(content, [])

    def close(self) -> None:
        self.terminate()
        if not self._exited.wait(3):
            try:
                self.kill()
            except Exception:
                pass
        self._cleanup()

    def _cleanup(self) -> None:
        self._recorder.close()
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
        for widget, _handle, _notes, _header in self._state.views_snapshot():
            self._send_to(widget, content, buffers)

    def _new_view(self):
        from .widget import ConsoleView

        widget = ConsoleView(session_name=self.name)
        widget.on_msg(self._on_widget_msg)
        comm = getattr(widget, "comm", None)
        if comm is not None:
            # Without this, a view whose frontend counterpart is gone (its
            # cell was re-executed, its output cleared, ...) stays in
            # _views forever: every future _emit/_refresh_* still tries to
            # send to it (harmless, caught, but wasted work), and it's a
            # standing extra "source" the input dedup has to reason about.
            # A closed comm is the frontend's own signal that the view is
            # actually gone -- this is not a guess about *why* it closed.
            comm.on_close(lambda data, w=widget: self._prune_view(w))
        return widget

    def _prune_view(self, widget) -> None:
        self._state.prune_view(widget)

    def _on_widget_msg(self, widget, content, buffers) -> None:
        kind = content.get("type")
        if kind == "ready":
            scrollback = self._state.dump_scrollback()
            if scrollback:
                self._send_to(widget, {"type": "out"}, [scrollback])
            self._emit_rec_state(widget)
            if self.altscreen:
                self._send_to(widget, {"type": "altscreen", "on": True}, [])
            if self._exited.is_set():
                self._send_to(widget, {"type": "exited", "code": self._returncode}, [])
        elif kind == "stdin":
            if not self._exited.is_set():
                self._input(content.get("data", "").encode(), source=widget)
        elif kind == "pause":
            self.record(not self._recorder.recording)
        elif kind == "erase":
            self.erase()
        elif kind == "screenshot":
            try:
                self._publish_note_to(widget, self._snapshot_text())
            except Exception:
                pass
        elif kind == "resize":
            rows, cols = int(content.get("rows", 24)), int(content.get("cols", 80))
            if (rows, cols) != (self._rows, self._cols):
                try:
                    self.resize(rows, cols)
                except Exception:
                    pass

    def _ipython_display_(self) -> None:
        """Embed a live console here. Every cell that displays the session
        gets its own live, independent view (PLAN.md §4) — output fans out to
        all of them, input from any of them goes to the one PTY, like
        multiple clients attached to the same tmux session.

        Each display starts with two outputs, both kept in sync via
        update_display_data: the live widget (its text/plain fallback is the
        plain session console text), and a second, initially empty/invisible
        one for interceptor notes and the recording indicator — content that
        isn't literal PTY bytes, so it gets its own clearly separate block
        instead of being buried in the first output's hidden fallback text
        (PLAN.md §6). Screenshots are different again: a toolbar click on
        *this* view publishes a brand-new output onto just *this* cell (like
        calling display() again, via ``_publish_note_to``) — not every cell
        that happens to display the session — so it can be individually
        copied or deleted rather than folded into one shared slot. That's why
        this cell's parent header is captured here and stashed per view: a
        toolbar click is an async widget message, not a cell execution, so a
        plain display() call at that point would land wherever the kernel
        happens to be, which by the time a real click arrives is usually some
        unrelated later cell."""
        from IPython.display import display

        widget = self._new_view()
        widget._text = self.text
        handle = display(widget, display_id=True)
        notes = display(self._transcript, display_id=True)
        self._state.add_view((widget, handle, notes, self._capture_parent_header()))

    @staticmethod
    def _capture_parent_header() -> Optional[dict]:
        try:
            from IPython import get_ipython

            ip = get_ipython()
            if ip is not None:
                return dict(ip.display_pub.parent_header)
        except Exception:
            pass
        return None

    def _publish_note_to(self, widget, text: str) -> None:
        """Publish ``text`` as a brand-new, separate output on just the ONE
        cell that displays ``widget`` (PLAN.md §6) — as if ``display()`` were
        called again — so a screenshot appears only where it was actually
        taken, not on every cell that happens to display this session, and
        can be individually copied or deleted rather than folded into one
        growing update_display_data-updated slot.

        Looks up ``widget``'s parent header, captured back when that cell's
        ``_ipython_display_()`` ran — necessary because the toolbar click
        that gets us here is an async widget message, not a cell execution,
        so there's no "current cell" for a plain ``display()`` call to
        attach to; it would land wherever the kernel happens to be, which by
        the time a real click arrives is usually some unrelated later cell."""
        header = None
        for w, _handle, _notes, h in self._state.views_snapshot():
            if w is widget:
                header = h
                break
        if header is None:
            return
        mimebundle = Note(text)._repr_mimebundle_()

        def _publish() -> None:
            try:
                from IPython import get_ipython

                kernel = getattr(get_ipython(), "kernel", None)
                if kernel is None:
                    return
                content = {"data": mimebundle, "metadata": {}, "transient": {}}
                msg = kernel.session.msg("display_data", content, parent=header)
                kernel.session.send(kernel.iopub_socket, msg, ident=b"display_data")
            except Exception:
                pass

        loop = getattr(self._kernel, "io_loop", None)
        if loop is not None:
            loop.add_callback(_publish)
        else:
            _publish()

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
            lines = [ln for ln in path.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
            self.sendline(" ; ".join(lines))
        else:
            self.sendline(f". '{path}'")

    def __repr__(self) -> str:
        state = f"exited {self._returncode}" if self._exited.is_set() else ("busy" if self._state.active else "at prompt")
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
