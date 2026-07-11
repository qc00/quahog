"""Recording (PLAN.md §6): asciicast v2 sidecars with password hygiene.

The one hard invariant: **interactively typed passwords never reach disk.**
Three mechanisms cooperate:

- a *delayed-flush tail*: the writer keeps the last few seconds of events in
  memory before flushing, so the ⌫ erase button can rewrite recent input
  events in place (into ``[input suppressed]`` placeholders) regardless of
  what was or wasn't echoed;
- *automatic suppression*, narrow and mechanism-level: input is replaced by a
  placeholder only when the local PTY's termios reports ECHO off, or when a
  password interceptor suppressed input recording for a matched command;
- an *echo classifier* (verbatim / masked / none) that drives the widget's
  flashing ⌫ affordance — a prompt, not an action.

Recordings go to a visible, committable folder next to the notebook:
``deploy.ipynb`` → ``deploy.quahog/<session>-<ts>.cast`` (notebook path via
VS Code's ``__vsc_ipynb_file__``, the ``JPY_SESSION_NAME`` env, else a
``console.quahog`` folder in the cwd).
"""

from __future__ import annotations

import codecs
import datetime as _dt
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Union

from .result import clean_text

PLACEHOLDER = "[input suppressed]"
TAIL_SECONDS = 3.0


# ------------------------------------------------------------ sidecar folder
def notebook_path() -> Optional[Path]:
    """Best-effort path of the notebook this kernel is serving."""
    try:
        from IPython import get_ipython

        ip = get_ipython()
        if ip is not None:
            p = ip.user_ns.get("__vsc_ipynb_file__")
            if p:
                return Path(p)
    except Exception:
        pass
    p = os.environ.get("JPY_SESSION_NAME", "")
    if p.endswith(".ipynb"):
        q = Path(p)
        return q if q.is_absolute() else Path.cwd() / q
    return None


def sidecar_dir() -> Path:
    """``<notebook>.quahog/`` next to the notebook; cwd fallback otherwise."""
    nb = notebook_path()
    if nb is not None:
        return nb.parent / (nb.stem + ".quahog")
    return Path.cwd() / "console.quahog"


# ------------------------------------------------------------------- writer
class CastWriter:
    """asciicast v2 file with a delayed-flush tail.

    Events younger than ``tail_seconds`` stay in memory; within that window
    ``erase_inputs()`` rewrites input events in place — nothing is reordered
    and timestamps stay monotonic (they are assigned at append time and never
    changed).
    """

    def __init__(self, path: Union[str, Path], cols: int, rows: int, tail_seconds: float = TAIL_SECONDS) -> None:
        self.path = Path(path)
        self._tail_seconds = tail_seconds
        self._t0 = time.time()
        self._tail: List[list] = []  # [t, code, data] — mutable for erase
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "w")
        header = {
            "version": 2,
            "width": cols,
            "height": rows,
            "timestamp": int(self._t0),
            "env": {"TERM": "xterm-256color", "SHELL": os.environ.get("SHELL", "")},
        }
        self._f.write(json.dumps(header) + "\n")
        self._f.flush()

    def append(self, code: str, data: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._tail.append([round(time.time() - self._t0, 6), code, data])
            self._flush_old()
            self._schedule()

    def erase_inputs(self, count: int = 1) -> int:
        """Redact the last ``count`` real input events still in the tail."""
        with self._lock:
            n = 0
            for ev in reversed(self._tail):
                if n >= count:
                    break
                if ev[1] == "i" and ev[2] != PLACEHOLDER:
                    ev[2] = PLACEHOLDER
                    n += 1
            return n

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            for ev in self._tail:
                self._f.write(json.dumps(ev) + "\n")
            self._tail.clear()
            self._f.close()

    # -- internal --------------------------------------------------------
    def _flush_old(self) -> None:
        cutoff = time.time() - self._t0 - self._tail_seconds
        wrote = False
        while self._tail and self._tail[0][0] <= cutoff:
            self._f.write(json.dumps(self._tail.pop(0)) + "\n")
            wrote = True
        if wrote:
            self._f.flush()

    def _schedule(self) -> None:
        if self._timer is None and self._tail:
            self._timer = threading.Timer(self._tail_seconds + 0.2, self._tick)
            self._timer.daemon = True
            self._timer.start()

    def _tick(self) -> None:
        with self._lock:
            self._timer = None
            if self._closed:
                return
            self._flush_old()
            self._schedule()


# --------------------------------------------------------- echo classifier
class EchoClassifier:
    """Three-way keystroke echo classification: verbatim / masked / none.

    Pairs each printable keystroke with the output that follows within a
    short window. TUIs that consume input without echoing classify as
    "none" — the consequence is only a flashed affordance, never an action.
    """

    WINDOW = 0.25
    MASKS = frozenset("*•●")

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._cb = callback
        self._lock = threading.Lock()
        self._pending: Optional[tuple] = None
        self._timer: Optional[threading.Timer] = None

    def input(self, text: str, unechoed: bool = False) -> None:
        if len(text) != 1 or not text.isprintable():
            return  # control keys, escape sequences, pastes: not classifiable
        if unechoed:
            self._cb("none")  # termios already says no echo will come
            return
        with self._lock:
            self._cancel()
            self._pending = (time.time(), text)
            self._timer = threading.Timer(self.WINDOW, self._expire)
            self._timer.daemon = True
            self._timer.start()

    def output(self, text: str) -> None:
        with self._lock:
            if self._pending is None:
                return
            t0, typed = self._pending
            visible = clean_text(text)
            if not visible.strip("\r\n"):
                return  # cursor noise only; keep waiting for the echo
            if typed in visible:
                cls = "verbatim"
            elif self.MASKS & set(visible):
                cls = "masked"
            else:
                cls = "none"
            self._pending = None
            self._cancel()
        self._cb(cls)

    def _expire(self) -> None:
        with self._lock:
            if self._pending is None:
                return
            self._pending = None
            self._timer = None
        self._cb("none")

    def _cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


# ----------------------------------------------------------------- recorder
class Recorder:
    """Per-session recording state: the .cast writer, the pause toggle, the
    suppression holds, and the echo classifier.

    Exists on every session (recording or not) so the toolbar and the
    interceptor ctx always have something to talk to; all methods no-op
    while not recording.
    """

    def __init__(self, session_name: str, on_event: Optional[Callable] = None) -> None:
        self.session_name = session_name
        self._on_event = on_event or (lambda kind, **kw: None)
        self._writer: Optional[CastWriter] = None
        self._enabled = False
        self._suppress = 0
        self._sup_run = False  # a placeholder was already written for this suppressed burst
        self._lock = threading.RLock()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._classifier = EchoClassifier(self._on_echo)

    # -- state -------------------------------------------------------------
    @property
    def cast_path(self) -> Optional[Path]:
        return self._writer.path if self._writer is not None else None

    @property
    def recording(self) -> bool:
        return self._writer is not None and self._enabled

    @property
    def suppressed(self) -> bool:
        return self._suppress > 0

    def start(self, rows: int, cols: int, path: Union[str, Path, None] = None) -> None:
        """Open the .cast file (first call) and enable recording."""
        with self._lock:
            if self._writer is None:
                if path is None:
                    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                    path = sidecar_dir() / f"{self.session_name}-{ts}.cast"
                self._writer = CastWriter(path, cols, rows)
            self._enabled = True
        self._on_event("state")

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self._enabled = bool(on) and self._writer is not None
        self._on_event("state")

    def suppress(self) -> None:
        """Hold input suppression (interceptor ctx / toolbar); refcounted."""
        with self._lock:
            self._suppress += 1

    def release(self) -> None:
        with self._lock:
            self._suppress = max(0, self._suppress - 1)

    # -- traffic -------------------------------------------------------------
    def input(self, data: Union[bytes, str], record: bool = True, echoed: Optional[bool] = None) -> None:
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        with self._lock:
            if not self.recording:
                self._sup_run = False
                return
            sup = (not record) or self._suppress > 0 or echoed is False
            if sup:
                # One placeholder per suppressed burst: neither the bytes nor
                # the keystroke count reach disk.
                if not self._sup_run:
                    self._writer.append("i", PLACEHOLDER)
                    self._sup_run = True
            else:
                self._sup_run = False
                self._writer.append("i", text)
        self._classifier.input(text, unechoed=(echoed is False))

    def output(self, data: bytes) -> None:
        text = self._decoder.decode(data)
        if not text or not self.recording:
            return
        self._writer.append("o", text)
        self._classifier.output(text)

    def resize(self, rows: int, cols: int) -> None:
        if self.recording:
            self._writer.append("r", f"{cols}x{rows}")

    def erase(self, count: int = 1) -> int:
        """⌫: redact the most recent keystroke(s) still in the flush tail."""
        if self._writer is None:
            return 0
        return self._writer.erase_inputs(count)

    def close(self) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.close()

    # -- internal --------------------------------------------------------
    def _on_echo(self, cls: str) -> None:
        if self.recording and cls in ("masked", "none"):
            self._on_event("echo", cls=cls)
