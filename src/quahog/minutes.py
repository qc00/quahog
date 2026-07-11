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

from typing import List

from .result import CommandResult


class Transcript:
    """Accumulated interactive commands for one anchor-cell era."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        self.blocks: List[CommandResult] = []

    def append(self, result: CommandResult) -> None:
        self.blocks.append(result)

    def _repr_mimebundle_(self, include=None, exclude=None):
        text = "\n".join(r._plain() for r in self.blocks)
        return {"text/plain": text}

    def __repr__(self) -> str:
        return "\n".join(r._plain() for r in self.blocks)
