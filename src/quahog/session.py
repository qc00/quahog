"""Session: a live PTY with a Popen-shaped API, an OSC 133 tap, and views.

Architecture (PLAN.md §2): a reader thread drains the PTY and fans bytes out to
(a) the scrollback ring buffer, (b) the OSC parser driving run() capture, and
(c) any attached widget views. The canonical record of a command is the
CommandResult text; the widget is a pure client-side view.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import logging
import os
import shlex
import shutil
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, TYPE_CHECKING, Union

from . import copy as _copy
from . import interceptors as _interceptors
from . import utils
from .fork import ForkSession
from .interceptors import Ctx, Interceptor
from .minutes import Note, Transcript
from .osc import Token
from .record import Recorder
from .state import SessionState
from .sub_sessions import CommandResult, ExecSession

if TYPE_CHECKING:
    from IPython.core.display_functions import DisplayHandle

    from .record import CastWriter
    from .widget import ConsoleView

logger = logging.getLogger(__name__)
log_exception_min = utils.LogExceptionMinimal(logger.debug)

try:
    from .screen import ScreenMirror
except Exception:  # pyte missing: sessions still work, screenshots don't
    log_exception_min()
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


class ActiveInterceptor(NamedTuple):
    """One interceptor matched against the currently-running command, plus
    its per-command ``Ctx`` (``SessionState.iactive``)."""

    interceptor: Interceptor
    ctx: Ctx


class View(NamedTuple):
    """One live display of a session (``SessionState.views``): the widget, its
    ``display()`` handle, the second notes-output handle, and the parent cell
    header captured for ``_publish_note_to`` (PLAN.md §6)."""

    widget: "ConsoleView"
    handle: "DisplayHandle"
    notes: Transcript
    header: Optional[Dict[str, Any]]


_INJECT_DIR = Path(__file__).parent / "inject"


class TimeoutExpired(TimeoutError):
    """run(timeout=...) expired. The command keeps running; the partial
    CommandResult is available as .result and will complete in the background."""

    def __init__(self, result: CommandResult, timeout: float) -> None:
        super().__init__(f"command did not finish in {timeout}s (still running): " f"{result.command!r}")
        self.result = result


class _Stdin:
    """sys.stdin-shaped input: text at the top, bytes via .raw.

    ``.raw`` is the input-side counterpart of ``Session.raw``: bytes reach the
    PTY untouched, control characters and escape sequences included.

    Recording is orthogonal to the layer — both take ``record=False``, which
    sends the bytes to the PTY but leaves only an ``[input suppressed]``
    placeholder in the .cast, the sanctioned path for feeding secrets from a
    keyring/vault into an interactive prompt without them ever touching disk
    (PLAN.md §3).
    """

    class _Raw:
        """Byte layer: h.stdin.raw.write(b"\\x03")."""

        def __init__(self, session: "Session") -> None:
            self._s = session

        def write(self, data: bytes, record: bool = True) -> int:
            self._s._check_fg_exec()
            self._s._input(data, record=record)
            return len(data)

        def flush(self) -> None:
            pass

    def __init__(self, session: "Session") -> None:
        self._s = session
        self.raw = _Stdin._Raw(session)

    def write(self, data: str, record: bool = True) -> int:
        self._s._check_fg_exec()
        self._s._input(data.encode(), record=record)
        return len(data)

    def flush(self) -> None:
        pass


class Session:
    """One live shell in a PTY. Popen vocabulary plus run()/display."""

    _PROMPT_WAIT = 10.0

    def __init__(
        self,
        argv: List[str],
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
        self._integrated = threading.Event()  # set on each OSC 2607;QUA;I handshake
        self._exited = threading.Event()
        self._returncode: Optional[int] = None
        self.cwd: Optional[str] = cwd
        self._kernel = self._find_kernel()
        self.stdin = _Stdin(self)
        self._transcript = Transcript(name)

        self._recorder = Recorder(name, on_event=self._on_rec_event)

        # Minuting (PLAN.md §5): `minuting` gates appending; `last_dump` is the
        # pull-based dump_minutes_as_cell cursor. Both are touched only from the
        # kernel thread, so they live on the façade, not in the shared state.
        self.minuting = True
        self.last_dump = 0

        # exec()/copy (PLAN.md §3, §7): a per-session id source for exec tags,
        # and the list of completed remote->local downloads.
        self._exec_ids: Iterator[int] = itertools.count(1)
        self.downloads: List[_copy.DownloadBox] = []

        # The pyte mirror needs pyte, which may be absent (sessions still
        # work, screenshots don't).
        mirror = None
        if ScreenMirror is not None:
            with log_exception_min:
                mirror = ScreenMirror(rows, cols)
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
    def _find_kernel() -> Any:
        try:
            from IPython import get_ipython

            ip = get_ipython()
            return getattr(ip, "kernel", None)
        except Exception:
            log_exception_min()
            return None

    def _read_loop(self) -> None:
        fd = self._proc.fd
        st = self._state
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                log_exception_min()
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

    def _on_token(self, tok: Token) -> None:
        st = self._state
        if tok[0] == "data":
            if st.dl_active:
                # Between OSC 2607 Ds and De the command's base64-framed bytes
                # arrive as ordinary PTY data (PLAN.md §7); divert them from the
                # console text and the active capture — they are not output.
                st.dl_parts.append(tok[1].decode("latin-1"))
                return
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
                        with log_exception_min:
                            fn(ctx, text)
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
                    ex = active._exec
                    if ex is not None:
                        # Foreground exec: the shell is back at its prompt, so
                        # the session is free and es.wait() can complete now
                        # (rc already set by the X tag).
                        self._clear_fg_exec(ex.eid)
                        ex._done.set()
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
            elif kind == "O":
                # exec output chunk: OSC 2607;QUA;O;<id>;<clean text> (PLAN.md §3).
                eid, _, text = rest.partition(";")
                es = st.execs.get(eid)
                if es is not None:
                    es._on_output(text)
                    if es.mirror:
                        st.append_external_text(text)
                        self._refresh_views()
            elif kind == "X":
                # exec completion: OSC 2607;QUA;X;<id>;<rc>. rc lands here; a
                # foreground exec's _done waits for its shell prompt (D handler).
                eid, _, rcs = rest.partition(";")
                es = st.execs.pop(eid, None)
                if es is not None:
                    es.returncode = int(rcs) if rcs.lstrip("-").isdigit() else None
                    if es.background:
                        es._done.set()
                        self._clear_fg_exec(eid)
            elif kind == "U":
                # upload request (local -> remote): kernel streams the bytes.
                mode, _, path = rest.partition(";")
                self._handle_upload(mode, path)
            elif kind == "Ds":
                st.dl_active = True
                st.dl_name = rest
                st.dl_parts = []
            elif kind == "De":
                self._finish_download()
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
        self._integrated.set()

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
            log_exception_min()
            argv = command.split()
        if not argv:
            return
        for itc in _interceptors.all_interceptors():
            with log_exception_min:
                if not itc.match(argv, self):
                    continue
                ctx = Ctx(self, argv, command)
                fn = getattr(itc, "before", None)
                if fn is not None:
                    fn(ctx)
                self._state.iactive.append(ActiveInterceptor(itc, ctx))

    def _ifinish(self, result: Optional[CommandResult]) -> None:
        """Command ended (OSC 133;D or forced close): run after() hooks,
        attach any returned text to the result, drop leaked suppressions."""
        active, self._state.iactive = self._state.iactive, []
        for itc, ctx in active:
            out = None
            with log_exception_min:
                fn = getattr(itc, "after", None)
                out = fn(ctx) if fn is not None else None
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
    def minutes(self) -> List[Minute]:
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

        with log_exception_min:
            from IPython import get_ipython

            ip = get_ipython()
            if ip is not None and getattr(ip, "payload_manager", None) is not None:
                ip.payload_manager.write_payload({"source": "set_next_input", "text": text, "replace": False})

    def _refresh_views(self) -> None:
        """Push the current session text to every live view's primary output
        (PLAN.md §1): plain, literal console text — what a non-widget
        renderer (git diff, nbconvert, an LLM) sees for this cell."""
        views = self._state.views_snapshot()
        if not views:
            return
        text = self.text

        def _update() -> None:
            for view in views:
                with log_exception_min:
                    view.widget._text = text
                    view.handle.update(view.widget)

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
            for view in views:
                with log_exception_min:
                    view.notes.update(transcript)

        loop = getattr(self._kernel, "io_loop", None)
        if loop is not None:
            loop.add_callback(_update)
        else:
            _update()

    def _on_exit(self) -> None:
        with log_exception_min:
            self._proc.wait()
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
        self._emit_stdin_state()

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

    def fork(self, command: str, timeout: float = 15.0, record: Optional[bool] = None) -> ForkSession:
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
        handle = ForkSession(self.name, command, forkdir, cast=cast)
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

    def _fork_cast(self, record: Optional[bool]) -> Optional["CastWriter"]:
        if record is None:
            record = self._recorder.recording
        if not record:
            return None
        from .record import CastWriter, sidecar_dir

        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        return CastWriter(sidecar_dir() / f"{self.name}-fork-{ts}.cast", self._cols, self._rows)

    # ---------------------------------------------------------------- exec (§3)
    def exec(self, command: str, background: bool = False, mirror: bool = False) -> ExecSession:
        """Run ``command`` on the session's *own* PTY as its own object.

        Unlike fork(), exec rides the current PTY, so it works at any navigation
        depth (local or a remote host you've navigated to) needing only ``socat``
        and the injected ``__qua_xf`` filter on the far end. Output is captured
        as escape-stripped clean text, demuxed from the shared stream by OSC
        2607 O/X tags — which is what lets ``background=True`` interleave safely
        with the live shell.

        A *foreground* exec (default) owns stdin until it finishes:
        ``session.send``/``sendline`` raise meanwhile — feed it via
        ``handle.stdin``. ``mirror=True`` also folds the exec's output into this
        session's console/streams (default off — isolation is the point).
        """
        command = command.strip()
        if not command:
            raise ValueError("empty command")
        if "\n" in command:
            raise ValueError("multi-line commands are not supported; join with '&&'")
        if self._exited.is_set():
            raise RuntimeError(f"session {self.name!r} has exited")
        st = self._state
        eid = f"e{next(self._exec_ids)}"
        es = ExecSession(self, eid, command, background=background, mirror=mirror)
        mode = "0" if background else "1"
        core = f"__qua_exec {eid} {mode} {shlex.quote(command)}"
        # Background: wrap in a subshell that backgrounds internally — ``( … & )``
        # is the standard way to launch a job without the interactive shell's
        # ``[1] <pid>`` monitor-mode notice bleeding into the next command's
        # captured output. The orphaned job keeps the PTY fd and streams its
        # OSC 2607 O/X tags from there.
        line = f"( {core} & )" if background else core
        with st.lock:
            if st.fg_exec is not None:
                raise RuntimeError(f"session {self.name!r} is busy with a foreground exec")
            # Launching either kind means typing a command, so the shell must be
            # at a prompt — refuse mid run()/interactive command in both cases.
            # (Once a background job is launched the shell returns to its prompt,
            # so further commands, including more background execs, are fine.)
            if st.active is not None or st.icap is not None:
                raise RuntimeError(f"session {self.name!r} is busy")
            st.execs[eid] = es
            self._at_prompt.clear()
            if not background:
                # Drive the foreground exec through the command FSM: rc arrives
                # on the OSC 2607 X tag, but the session is only free again once
                # the shell returns to its prompt (OSC 133 D). Linking es to the
                # tracking result lets the D handler complete es.wait() then, so
                # a back-to-back exec/run doesn't race the finishing prompt.
                st.fg_exec = eid
                result = CommandResult(self.name, line)
                result._exec = es
                st.active = result
                st.typed_cmd = None
        self._input(line.encode() + b"\r")
        if background:
            # The ``( … & )`` launch subshell exits at once, returning the shell
            # to its prompt; wait for that so the handle is returned only when
            # the session is free again (the orphaned job keeps streaming O/X).
            self._at_prompt.wait(self._PROMPT_WAIT)
        else:
            self._emit_stdin_state()  # stdin is the exec's until its prompt returns
        return es

    # ------------------------------------------------------------ copy (§7)
    def _copy_base_dir(self) -> Path:
        """Where ``quahog cat``/``tar`` paths resolve: the notebook's folder,
        else the kernel cwd."""
        from .record import notebook_path

        nb = notebook_path()
        return nb.parent if nb is not None else Path.cwd()

    def _handle_upload(self, mode: str, path: str) -> None:
        """A ``quahog cat``/``tar`` request (OSC 2607;QUA;U): resolve the local
        path and stream a length-framed copy into the PTY, on a worker thread so
        the (blocking) send never stalls the reader that must keep draining the
        helper's acknowledgements."""

        def _worker() -> None:
            try:
                data = _copy.resolve_upload(mode, path, self._copy_base_dir())
                frame = _copy.framed_upload(data)
            except OSError:
                log_exception_min()
                # The helper is waiting for a fixed-width length header; send a
                # zero-length frame so ``head -c 0`` returns and it doesn't hang.
                frame = _copy.framed_upload(b"")
            self._write_all(frame)

        threading.Thread(target=_worker, name=f"quahog-upload-{self.name}", daemon=True).start()

    def _finish_download(self) -> None:
        """End of a ``quahog download`` (OSC 2607;QUA;De): decode the buffered
        base64, save the file, and surface a download box. Runs the decode/save
        off the reader thread."""
        st = self._state
        name = st.dl_name or "download"
        b64 = "".join(st.dl_parts)
        st.dl_active = False
        st.dl_name = None
        st.dl_parts = []

        def _worker() -> None:
            import base64

            try:
                data = base64.b64decode("".join(b64.split()))
            except Exception:
                log_exception_min()
                return
            path = _copy.save_download(name, data)
            box = _copy.DownloadBox(name, data, path)
            self.downloads.append(box)
            self._transcript.append(box)
            self._refresh_notes()

        threading.Thread(target=_worker, name=f"quahog-download-{self.name}", daemon=True).start()

    def upload(self, local: str, remote: str) -> CommandResult:
        """Programmatic ``quahog cat``: copy a local file to ``remote`` on the
        far side (PLAN.md §7). Typing ``quahog cat`` in the console is the
        primary interface; this is its twin."""
        return self.run(f"quahog cat {shlex.quote(local)} > {shlex.quote(remote)}")

    def download(self, remote: str, name: Optional[str] = None) -> "_copy.DownloadBox":
        """Programmatic ``quahog download``: bring a remote file to the local
        kernel and return the resulting download box."""
        name = name or os.path.basename(remote)
        before = len(self.downloads)
        self.run(f"cat {shlex.quote(remote)} | quahog download {shlex.quote(name)}")
        # The De marker triggers an off-thread decode/save; wait for the box.
        deadline = time.monotonic() + 10
        while len(self.downloads) == before and time.monotonic() < deadline:
            time.sleep(0.02)
        if len(self.downloads) == before:
            raise RuntimeError(f"download did not complete: {remote!r}")
        return self.downloads[-1]

    def _write(self, data: bytes) -> None:
        os.write(self._proc.fd, data)

    def _write_all(self, data: bytes) -> None:
        """os.write to a PTY master can write only part of a large buffer; a
        length-framed upload (PLAN.md §7) must land every byte or the helper's
        ``head -c`` blocks forever waiting for the rest."""
        fd = self._proc.fd
        mv = memoryview(data)
        while mv:
            try:
                n = os.write(fd, mv)
            except BlockingIOError:
                log_exception_min()
                time.sleep(0.005)
                continue
            except OSError:
                log_exception_min()
                break
            mv = mv[n:]

    def _input(self, data: bytes, record: bool = True, source: Optional["ConsoleView"] = None) -> None:
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
                with log_exception_min:
                    fn(ctx, data)
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
            log_exception_min()
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
        with log_exception_min:
            self._proc.terminate()

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def interrupt(self) -> None:
        """Ctrl-C through the PTY (goes to the foreground process group)."""
        self._input(b"\x03")

    def _check_fg_exec(self) -> None:
        """A foreground exec owns stdin (PLAN.md §3): steer programmatic input to
        its handle rather than letting it race the running command."""
        if self._state.fg_exec is not None:
            raise RuntimeError(
                f"session {self.name!r} has a foreground exec running; "
                "feed it via the exec handle's .stdin/.sendline instead"
            )

    def send(self, data: Union[bytes, str], record: bool = True) -> None:
        self._check_fg_exec()
        self._input(data if isinstance(data, bytes) else str(data).encode(), record=record)

    def sendline(self, line: str = "", record: bool = True) -> None:
        self._check_fg_exec()
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
                    log_exception_min()
                    self._transcript.cast = str(cast)
            else:
                self._transcript.cast = None
            self._refresh_notes()  # the "[recording: ...]" line changed

    def _emit_rec_state(self, widget: Optional["ConsoleView"] = None) -> None:
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

    def _emit_stdin_state(self, widget: Optional["ConsoleView"] = None) -> None:
        """Tell views whether what they type still reaches the shell: the
        session has exited (the PTY is gone, keystrokes are dropped), or a
        foreground exec owns stdin (PLAN.md §3) — typing there feeds the exec,
        not the shell, since exec rides this same PTY."""
        if self._exited.is_set():
            state = "closed"
        elif self._state.fg_exec is not None:
            state = "exec"
        else:
            state = "open"
        content = {"type": "stdin-state", "state": state}
        if widget is not None:
            self._send_to(widget, content, [])
        else:
            self._emit(content, [])

    def _clear_fg_exec(self, eid: str) -> None:
        """Release stdin if ``eid`` is the exec holding it, telling the views."""
        if self._state.fg_exec == eid:
            self._state.fg_exec = None
            self._emit_stdin_state()

    def close(self) -> None:
        self.terminate()
        if not self._exited.wait(3):
            with log_exception_min:
                self.kill()
        self._cleanup()

    def _cleanup(self) -> None:
        self._recorder.close()
        tmp = getattr(self, "_tmpdir", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    # -------------------------------------------------------------- widgets
    def _send_to(self, widget: Optional["ConsoleView"], content: Dict[str, Any], buffers: List[bytes]) -> None:
        if widget is None:
            return

        def _send() -> None:
            with log_exception_min:
                widget.send(content, buffers=buffers)

        loop = getattr(self._kernel, "io_loop", None)
        if loop is not None:
            loop.add_callback(_send)
        else:
            _send()

    def _emit(self, content: Dict[str, Any], buffers: List[bytes]) -> None:
        for view in self._state.views_snapshot():
            self._send_to(view.widget, content, buffers)

    def _new_view(self) -> "ConsoleView":
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

    def _prune_view(self, widget: "ConsoleView") -> None:
        self._state.prune_view(widget)

    def _on_widget_msg(self, widget: "ConsoleView", content: Dict[str, Any], buffers: List[bytes]) -> None:
        kind = content.get("type")
        if kind == "ready":
            scrollback = self._state.dump_scrollback()
            if scrollback:
                self._send_to(widget, {"type": "out"}, [scrollback])
            self._emit_rec_state(widget)
            self._emit_stdin_state(widget)
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
            with log_exception_min:
                self._publish_note_to(widget, self._snapshot_text())
        elif kind == "resize":
            rows, cols = int(content.get("rows", 24)), int(content.get("cols", 80))
            if (rows, cols) != (self._rows, self._cols):
                with log_exception_min:
                    self.resize(rows, cols)

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
        self._state.add_view(View(widget, handle, notes, self._capture_parent_header()))

    @staticmethod
    def _capture_parent_header() -> Optional[Dict[str, Any]]:
        with log_exception_min:
            from IPython import get_ipython

            ip = get_ipython()
            if ip is not None:
                return dict(ip.display_pub.parent_header)
        return None

    def _publish_note_to(self, widget: "ConsoleView", text: str) -> None:
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
        for view in self._state.views_snapshot():
            if view.widget is widget:
                header = view.header
                break
        if header is None:
            return
        mimebundle = Note(text)._repr_mimebundle_()

        def _publish() -> None:
            with log_exception_min:
                from IPython import get_ipython

                kernel = getattr(get_ipython(), "kernel", None)
                if kernel is None:
                    return
                content = {"data": mimebundle, "metadata": {}, "transient": {}}
                msg = kernel.session.msg("display_data", content, parent=header)
                kernel.session.send(kernel.iopub_socket, msg, ident=b"display_data")

        loop = getattr(self._kernel, "io_loop", None)
        if loop is not None:
            loop.add_callback(_publish)
        else:
            _publish()

    # ------------------------------------------------------------ integration
    @property
    def integrated(self) -> bool:
        """Whether an integration handshake has been seen at least once."""
        return self._integrated.is_set()

    def reinject(self, full: bool = False) -> None:
        """Re-type the shell integration after su / exec zsh / a nested shell,
        or a hand-typed ssh onto a remote host (PLAN.md §7).

        ``full=True`` types the whole snippet as plain shell source — needed
        when the new shell can't read quahog's local file (i.e. anything
        remote); the default sources the snippet file, which any local shell
        can do. Fire-and-forget; ``wait_reinject()`` blocks until the snippet's
        handshake confirms it took.
        """
        self._integrated.clear()
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

    def wait_reinject(self, timeout: float = 10.0) -> bool:
        """Block until the integration handshake (OSC 2607;QUA;I) arrives after
        a reinject — i.e. markers are flowing again — and the shell has reached
        a clean prompt (so any command left open across the navigation, which
        never emitted its D, is closed out). Returns False on timeout."""
        if not self._integrated.wait(timeout):
            return False
        # The handshake fires while the snippet sources; the first prompt after
        # it is what closes a hop-orphaned capture. Wait for that quiet prompt.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state.lock:
                if self._state.icap is None and self._state.active is None:
                    return True
            time.sleep(0.02)
        return True

    def __repr__(self) -> str:
        state = (
            f"exited {self._returncode}" if self._exited.is_set() else ("busy" if self._state.active else "at prompt")
        )
        return f"<quahog.Session {self.name} ({self.shell_kind}, pid {self.pid}, {state})>"


# ----------------------------------------------------------------- factories


def _rcfile_bash(tmpdir: str, inherit_rc: bool) -> str:
    path = os.path.join(tmpdir, "bashrc")
    lines: List[str] = []
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
