"""Kernel-side screen state (PLAN.md §4/§6): a pyte mirror of the terminal.

Every PTY byte is fed through a pyte screen so ``Session.screenshot()`` works
even with no view attached, and the alt-screen switch (CSI ?1049/?1047/?47)
is tracked to mark full-screen interactive apps: while it is active run()
refuses, minuting pauses, and the widget shows a badge.

pyte's base ``Screen`` doesn't itself implement the alternate-screen buffer
swap that a real terminal does for private mode 1049/1047/47 — it just keeps
drawing into the one buffer regardless. Left alone, that means a screenshot
taken after quitting a full-screen app (``less``, ``vim``) shows whatever
that app last drew, not the shell prompt underneath. ``ScreenMirror`` does
the swap itself: entering alt-screen stashes the current buffer and clears
it for the app to draw on; leaving restores exactly what was stashed.
"""

from __future__ import annotations

import copy
import re
from typing import Optional

# Longest sequence is 8 bytes; the carry keeps a suffix of the previous chunk
# so a switch split across reads is still seen. Re-scanning the carried suffix
# is safe: order is preserved and the last match wins.
_ALT_RE = re.compile(rb"\x1b\[\?(?:1049|1047|47)([hl])")
_CARRY = 12


class ScreenMirror:
    """What's on screen right now, maintained from the raw byte stream."""

    def __init__(self, rows: int, cols: int) -> None:
        import pyte

        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        self._carry = b""
        self.altscreen = False
        self._saved_buffer = None
        self._saved_cursor = None

    def feed(self, data: bytes) -> Optional[bool]:
        """Ingest PTY output; returns the new alt-screen state if it changed."""
        try:
            self._stream.feed(data)
        except Exception:
            pass  # pyte hiccups must never kill the tap
        buf = self._carry + data
        self._carry = buf[-_CARRY:]
        state = self.altscreen
        for m in _ALT_RE.finditer(buf):
            state = m.group(1) == b"h"
        if state != self.altscreen:
            self.altscreen = state
            self._swap_buffer(state)
            return state
        return None

    def _swap_buffer(self, entering_altscreen: bool) -> None:
        try:
            if entering_altscreen:
                self._saved_buffer = copy.deepcopy(self._screen.buffer)
                self._saved_cursor = copy.copy(self._screen.cursor)
                self._screen.buffer.clear()
                self._screen.cursor_position()
            elif self._saved_buffer is not None:
                self._screen.buffer.clear()
                self._screen.buffer.update(self._saved_buffer)
                self._screen.cursor = self._saved_cursor
                self._saved_buffer = None
                self._saved_cursor = None
        except Exception:
            pass  # a mirror hiccup must never kill the tap

    def resize(self, rows: int, cols: int) -> None:
        try:
            self._screen.resize(rows, cols)
        except Exception:
            pass

    def snapshot(self) -> str:
        """The current screen as plain text, trailing blanks trimmed."""
        lines = [ln.rstrip() for ln in self._screen.display]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)
