from __future__ import annotations

import codecs
import re
import threading
import time
from collections import deque
from typing import Deque, Dict, List, NamedTuple, Optional, Tuple, TYPE_CHECKING

from .osc import StreamParser
from .sub_sessions import CommandResult, clean_text

if TYPE_CHECKING:
    from .screen import ScreenMirror
    from .session import ActiveInterceptor, Minute, View
    from .sub_sessions import ExecSession
    from .widget import ConsoleView


class RecentInput(NamedTuple):
    """One recent keystroke/reply, kept briefly to dedup retried or
    cross-view terminal-capability replies (see ``is_duplicate_input``)."""

    time: float
    source: Optional["ConsoleView"]
    data: bytes


class _Ring:
    """Bounded scrollback of raw PTY bytes, replayed to newly attached views."""

    def __init__(self, cap: int = 2_000_000) -> None:
        self._chunks: Deque[bytes] = deque()
        self.size: int = 0
        self._cap: int = cap

    def append(self, b: bytes) -> None:
        self._chunks.append(b)
        self.size += len(b)
        while self.size > self._cap and self._chunks:
            self.size -= len(self._chunks.popleft())

    def dump(self) -> bytes:
        return b"".join(self._chunks)


# Known terminal-reply shapes xterm.js auto-generates on the app's behalf:
# focus in/out, CPR (cursor position report), DA1/DA2 (device attributes),
# DECRPM (report mode), OSC 10/11/4 (color) replies. These are never something
# a human types — real key sequences (arrows, function keys) don't match — so
# it's safe to dedup them even from the *same* source, unlike a general
# escape-sequence dedup would be.
_TERMINAL_REPLY_RE = re.compile(
    rb"^\x1b(?:"
    rb"\[[OI]"
    rb"|\[\d+;\d+R"
    rb"|\[[>?]?\d*(?:;\d+)*c"
    rb"|\[\??\d+;\d+\$y"
    rb"|\](?:1[01]|4);[^\x07\x1b]*(?:\x07|\x1b\\)"
    rb")$"
)


class SessionState:
    """SessionState: all of a Session's cross-thread mutable state behind one lock.

    Access patterns:

    - the reader loop takes ``with state.lock:`` once and drives the whole token
    batch as a single critical section.
    - everyone else uses the locked helper methods, which return **snapshots**
    (``text()`` a ``str``, ``views_snapshot()`` a list copy). All blocking I/O —
    xterm writes, ``display``/``update_display_data``, ``.cast`` appends — happens
    on those snapshots, *outside* the lock, so the lock never spans a blocking
    send and a single reentrant lock suffices.

    ``Session`` is a façade over this, delegating all shared state here.
    """

    _DEDUP_WINDOW = 0.15  # seconds

    def __init__(self, mirror: Optional["ScreenMirror"] = None) -> None:
        self.lock = threading.RLock()
        self._ring = _Ring()
        self.mirror = mirror
        self.parser = StreamParser()

        # Command capturing
        self.active: Optional[CommandResult] = None
        self.icap: Optional[CommandResult] = None
        self.icap_marks: Optional[Tuple[int, int]] = None  # (raw_start, text_start)
        self.typed_cmd: Optional[str] = None
        self.altscreen: bool = False
        self.iactive: List["ActiveInterceptor"] = []  # for the running command

        self.execs: Dict[str, "ExecSession"] = {}
        self.fg_exec: Optional[str] = None

        # In-progress "quahog download" (PLAN.md §7): dl_active gates the base64
        # payload between its Ds/De markers away from the console text.
        self.dl_active: bool = False
        self.dl_name: Optional[str] = None
        self.dl_parts: List[str] = []

        self.minutes: List["Minute"] = []
        self.views: List["View"] = []

        self._recent_input: List[RecentInput] = []

        # Session-lifetime streams. ``raw`` is the decoded data stream with
        # markers stripped but escapes kept; ``text`` is its cleaned form,
        # materialized up to the last command boundary. Minute slices index
        # into these (unbounded by design).
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._raw_parts: List[str] = []
        self._raw_len = 0
        self._unflushed: List[str] = []
        self._text_parts: List[str] = []
        self._text_len = 0

    # -- scrollback ----------------------------------------------------------
    def append_output(self, data: bytes) -> None:
        with self.lock:
            self._ring.append(data)

    def dump_scrollback(self) -> bytes:
        with self.lock:
            return self._ring.dump()

    def ring_size(self) -> int:
        with self.lock:
            return self._ring.size

    # -- screen (pyte mirror) ------------------------------------------------
    def feed_screen(self, data: bytes) -> Optional[bool]:
        """Feed the mirror; on an alt-screen transition update ``altscreen``
        and, when *leaving*, append the exit marker to the clean-text stream.

        A full-screen app's cursor-addressed screen updates are excluded from
        the clean-text log while active (clean_text() strips escape sequences
        but has no notion of 2D cursor positioning, so naively "cleaning" a
        TUI's output produces unreadable run-on garbage, not readable text) —
        leave a marker instead of silently jumping. h.screenshot() is the tool
        for "what was on screen" (PLAN.md §6). Returns the new alt-screen state
        if it changed, else None."""
        with self.lock:
            if self.mirror is None:
                return None
            alt = self.mirror.feed(data)
            if alt is not None:
                self.altscreen = alt
                if not alt:
                    marker = "[full-screen app exited — see h.screenshot()]\n"
                    self._text_parts.append(marker)
                    self._text_len += len(marker)
            return alt

    def resize_screen(self, rows: int, cols: int) -> None:
        with self.lock:
            if self.mirror is not None:
                self.mirror.resize(rows, cols)

    def screen_snapshot(self) -> Optional[str]:
        """The current screen as text, or None if pyte is unavailable."""
        with self.lock:
            if self.mirror is None:
                return None
            return self.mirror.snapshot()

    # -- lifetime streams ----------------------------------------------------
    def ingest(self, b: bytes) -> None:
        with self.lock:
            s = self._decoder.decode(b)
            if s:
                self._raw_parts.append(s)
                self._raw_len += len(s)
                self._unflushed.append(s)

    @property
    def raw_len(self) -> int:
        return self._raw_len

    def text_boundary(self) -> int:
        """Materialize the clean-text stream up to 'now'; return its length.
        Called at command boundaries (C/D), which sit at line edges, so
        carriage-return overlays never straddle the flush point in practice."""
        with self.lock:
            if self._unflushed:
                cleaned = clean_text("".join(self._unflushed))
                self._unflushed.clear()
                self._text_parts.append(cleaned)
                self._text_len += len(cleaned)
            return self._text_len

    def append_external_text(self, s: str) -> None:
        """Fold text that never came off this PTY (a mirror=True exec's output,
        PLAN.md §3) into the lifetime streams so the console log shows it."""
        with self.lock:
            self._raw_parts.append(s)
            self._raw_len += len(s)
            self._text_parts.append(s)
            self._text_len += len(s)

    def raw(self) -> str:
        """The session's data stream since start: escapes kept, markers
        stripped. Minute.raw slices index into this."""
        with self.lock:
            return "".join(self._raw_parts)

    def text(self) -> str:
        """Clean-text form of ``raw``. Minute.text slices index into this."""
        with self.lock:
            done = "".join(self._text_parts)
            tail = clean_text("".join(self._unflushed)) if self._unflushed else ""
        return done + tail

    # -- minuting ------------------------------------------------------------
    def append_minute(self, minute: "Minute") -> None:
        with self.lock:
            self.minutes.append(minute)

    def minutes_snapshot(self) -> List["Minute"]:
        with self.lock:
            return list(self.minutes)

    # -- interceptors --------------------------------------------------------
    def iactive_snapshot(self) -> List["ActiveInterceptor"]:
        with self.lock:
            return list(self.iactive)

    # -- views ---------------------------------------------------------------
    def add_view(self, view: "View") -> None:
        with self.lock:
            self.views.append(view)

    def prune_view(self, widget: "ConsoleView") -> None:
        with self.lock:
            self.views = [v for v in self.views if v.widget is not widget]

    def views_snapshot(self) -> List["View"]:
        with self.lock:
            return list(self.views)

    def has_views(self) -> bool:
        with self.lock:
            return bool(self.views)

    # -- input dedup ---------------------------------------------------------
    def is_duplicate_input(self, data: bytes, source: Optional["ConsoleView"]) -> bool:
        """A full-screen app querying the terminal for its own capabilities can
        retry if a reply doesn't arrive within its own short timeout — plausible
        over the browser/kernel round trip a widget's input takes. If replies
        then arrive for every attempt, the PTY receives the same reply repeated,
        which some apps can't handle gracefully (observed: vim's welcome screen
        replaced by a stray "y" — the tail of a DECRPM reply landing three times
        in ~15ms, all from one attached view). Concurrently-attached views
        (PLAN.md §4) compound the same risk: each independently answers the same
        query, so N views means N replies to what should be one.

        Recognized reply shapes are deduped within a short window regardless of
        source; anything else only dedups across genuinely different sources,
        since two distinct views sending identical bytes within a couple hundred
        ms otherwise essentially never happens by coincidence (a plain repeated
        character is content no reply ever takes, so it's never touched)."""
        with self.lock:
            now = time.monotonic()
            self._recent_input = [e for e in self._recent_input if now - e.time < self._DEDUP_WINDOW]
            is_reply = bool(_TERMINAL_REPLY_RE.match(data))
            if not is_reply and not data.startswith(b"\x1b"):
                self._recent_input.append(RecentInput(now, source, data))
                return False
            for e in self._recent_input:
                if e.data == data and (is_reply or e.source is not source):
                    return True
            self._recent_input.append(RecentInput(now, source, data))
            return False
