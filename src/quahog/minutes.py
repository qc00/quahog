"""Minuting: interactively typed commands become durable notebook content.

Two mechanisms, per PLAN.md §5:

1. Anchor-cell transcript — the cell that displayed a session holds a
   transcript display handle; every interactive command's {command, output,
   exit} is appended there as text via update_display_data. The durable record
   never depends on cell creation.

2. Cell creation — a retry convenience. Commands queue up; on the next
   execution of any cell (post_run_cell fires inside the execute request, so
   the payload rides that reply) the queue flushes via a single
   set_next_input payload creating an unexecuted %qua / %%qua cell.
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


_registered = False


def register(ip) -> None:
    global _registered
    if _registered:
        return
    ip.events.register("post_run_cell", _flush)
    _registered = True


def _flush(result=None) -> None:
    """post_run_cell hook: drain every session's minute queue into one
    set_next_input payload."""
    try:
        import quahog
        from IPython import get_ipython

        ip = get_ipython()
        if ip is None:
            return
        entries = []  # (session, CommandResult)
        for s in list(quahog.sessions.values()):
            for r in s._drain_minutes():
                entries.append((s, r))
        if not entries:
            return
        text = _cell_text(entries, quahog.default)
        ip.payload_manager.write_payload(
            {"source": "set_next_input", "text": text, "replace": False}
        )
    except Exception:
        pass  # minuting must never break cell execution


def _cell_text(entries, default) -> str:
    names = {s.name for s, _ in entries}
    if len(entries) == 1:
        s, r = entries[0]
        prefix = "%qua " if s is default else f"%qua -s {s.name} "
        return prefix + r.command
    if len(names) == 1:
        s = entries[0][0]
        head = "%%qua" if s is default else f"%%qua {s.name}"
        return head + "\n" + "\n".join(r.command for _, r in entries)
    # Mixed sessions in one flush (rare): one %qua line each.
    return "\n".join(
        (f"%qua {r.command}" if s is default else f"%qua -s {s.name} {r.command}")
        for s, r in entries
    )
