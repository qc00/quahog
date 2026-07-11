"""Minuting support: interactive-command capture and the notes side-output.

Every cell that displays a session is a live, independent view (PLAN.md §4)
with *two* outputs, both kept in sync via ``update_display_data``:

- the widget's own output, whose ``text/plain`` fallback is the session's
  plain, literal console text (``Session.text``) — what a non-widget
  renderer (git diff, nbconvert, an LLM reading the ``.ipynb``) sees;
- a second, initially invisible ``Transcript`` output for content that isn't
  literal PTY bytes — screenshots, interceptor notes (a vim diff), the
  recording indicator — so a screenshot reads as its own clearly-separate
  block rather than being buried inside the first output's hidden fallback
  text (invisible whenever the widget mimetype renders).

Cell *creation* is a separate, explicit concern: ``Session.dump_minutes_as_cell()``,
pull-based after VS Code field testing (2026-07-11) showed ``set_next_input``
payloads written between executions never render, and only the last payload
per execution is honored. See PLAN.md §5.
"""

from __future__ import annotations

from typing import Any, List, Optional


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
    """Accumulated non-literal console-log blocks for one session: the
    second, initially-empty output every live view carries alongside the
    widget (PLAN.md §6). An empty ``Transcript`` formats to ``""`` — real
    content (via IPython's ``update_display_data``) is what makes it appear
    at all, so nothing shows until the first note or recording toggle."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        self.blocks: List[Any] = []  # anything with a _plain() method
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
