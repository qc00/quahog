"""Minuting support: interactive-command capture and non-literal console notes.

Every cell that displays a session is a live, independent view (PLAN.md §4);
its single output's ``text/plain`` is the session's clean console log —
``Session.text`` plus any ``Transcript`` blocks (screenshots, interceptor
notes: things that aren't literal PTY bytes) — kept in sync via
``update_display_data``. Cell *creation* is a separate, explicit concern:
``Session.dump_minutes_as_cell()``, pull-based after VS Code field testing
(2026-07-11) showed ``set_next_input`` payloads written between executions
never render, and only the last payload per execution is honored. See
PLAN.md §5.
"""

from __future__ import annotations

from typing import Any, List


class Note:
    """A plain-text console-log block that isn't literal PTY output:
    screenshots, interceptor output (vim diffs), and the like."""

    def __init__(self, text: str) -> None:
        self.text = text

    def _plain(self) -> str:
        return self.text

    def _repr_mimebundle_(self, include=None, exclude=None):
        return {"text/plain": self.text}

    def __repr__(self) -> str:
        return self.text


class Transcript:
    """Accumulated non-literal console-log blocks for one session."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        self.blocks: List[Any] = []  # anything with a _plain() method

    def append(self, block: Any) -> None:
        self.blocks.append(block)

    def _plain(self) -> str:
        return "\n".join(b._plain() for b in self.blocks)

    def __repr__(self) -> str:
        return self._plain()
