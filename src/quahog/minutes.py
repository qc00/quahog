"""Minuting support: the anchor-cell transcript.

The cell that displayed a session holds a transcript display handle; every
interactive command's {command, output, exit} is appended there as text via
update_display_data, so the durable record never depends on cell creation.

Cell creation itself is pull-based — Session.dump_minutes_as_cell() — after
VS Code field testing (2026-07-11) showed set_next_input payloads written
between executions never render, and only the last payload per execution is
honored. See PLAN.md §5.
"""

from __future__ import annotations

from typing import Any, List, Optional


class Note:
    """A plain-text block in the anchor transcript that isn't a command:
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
    """Accumulated interactive commands for one anchor-cell era."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        self.blocks: List[Any] = []  # anything with a _plain() method
        # Set when the session records: the committed cell then references the
        # .cast sidecar so replay tooling can find it (PLAN.md §6).
        self.cast: Optional[str] = None

    def append(self, block: Any) -> None:
        self.blocks.append(block)

    def _plain(self) -> str:
        lines = [f"[recording: {self.cast}]"] if self.cast else []
        lines.extend(b._plain() for b in self.blocks)
        return "\n".join(lines)

    def _repr_mimebundle_(self, include=None, exclude=None):
        return {"text/plain": self._plain()}

    def __repr__(self) -> str:
        return self._plain()
