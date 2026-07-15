"""quahog MCP server — drive quahog sessions and notebook cells over the
Model Context Protocol (PLAN.md §9).

Optional feature: requires the ``mcp`` extra (``pip install quahog[mcp]`` →
fastmcp, jupyter-server). Nothing in quahog's core imports this module, so the
package works without those installed; importing ``quahog.mcp`` is what pulls
them in.

The verbs here cover only what *isn't* naturally a notebook cell — session
introspection, a convenience ``run``, recording, cell CRUD, and snapshot.
Anything that must bind a Python variable (``h = q.bash()``, ``r = h.exec(...)``,
``display(h)``, ``h.fork(...)``) is done by *writing and executing a cell*, so
the result lives in the kernel namespace for later cells and the notebook stays
the honest record. That is why there is no ``exec``/``fork``/``spawn`` tool: use
``insert_cell(..., execute=True)`` or ``write_cell`` + ``execute_cell``.

Skeleton: the tool surface and its docs are real; the bridge to the kernel and
the notebook document (:class:`Bridge`) is stubbed. Launch the stdio server with
``python -m quahog.mcp``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations


# --------------------------------------------------------------------------- #
# The bridge: between the MCP server and the two things it drives — the kernel
# (where quahog Sessions live) and the notebook document (where cells live).
#
# The server process is a jupyter-server extension, so it shares the process
# that already owns both halves:
#   * Kernel side — Sessions are objects in the kernel namespace
#     (quahog.sessions), reached over the server's existing kernel channels.
#   * Document side — Cells are entries in the notebook document, keyed by the
#     nbformat `id` (stable since 4.5). With jupyter-collaboration (RTC / ydoc)
#     installed, edits go through the shared model and show up live; without it,
#     through the file model plus a save, then an execute request.
#
# Skeleton: shapes and docstrings are settled, bodies raise NotImplementedError.
# The tools below are written against this interface, so a real implementation
# drops straight in.
# --------------------------------------------------------------------------- #


@dataclass
class SessionInfo:
    """A live quahog session, as an MCP client sees it."""

    name: str
    shell: str
    cwd: Optional[str]
    pid: int
    returncode: Optional[int]
    altscreen: bool
    recording: bool
    cell_ids: List[str] = field(default_factory=list)  # cells currently displaying it


@dataclass
class RunResult:
    """Outcome of ``run`` — one command typed and captured (like ``h.run()``)."""

    returncode: Optional[int]
    text: str
    timed_out: bool = False


@dataclass
class CellInfo:
    """A notebook cell as it currently stands in the live document."""

    id: str
    cell_type: Literal["code", "markdown", "raw"]
    source: str
    outputs: List[Any] = field(default_factory=list)  # nbformat output dicts (code cells)


class Bridge:
    """What the MCP tools call. One instance is shared by every tool. Kernel vs.
    document methods are split only for readability — a single jupyter-server
    extension implements both."""

    # ----------------------------------------------------------- kernel side
    def list_sessions(self) -> List[SessionInfo]:
        """Snapshot every session in ``quahog.sessions``."""
        raise NotImplementedError

    def read_session(
        self,
        name: str,
        kind: Literal["text", "raw", "screen", "minutes"] = "text",
        tail: Optional[int] = None,
    ) -> str:
        """Read one session's state. ``text``/``raw`` are the accumulated
        streams, ``screen`` the pyte snapshot of what's on screen now,
        ``minutes`` the typed-command log. ``tail`` limits to the last N
        lines. Raises if ``name`` is unknown."""
        raise NotImplementedError

    def run(self, session: str, command: str, timeout: Optional[float]) -> RunResult:
        """Type ``command`` into ``session`` and capture its output between the
        OSC 133 markers — the same launch-and-capture core as ``h.run()``.
        Raises if the session is busy or in alt-screen."""
        raise NotImplementedError

    def record(self, session: str, on: bool) -> bool:
        """Toggle keystroke recording; returns the new state."""
        raise NotImplementedError

    # --------------------------------------------------------- document side
    def read_cell(self, cell_id: str) -> CellInfo:
        """The live source + outputs of one cell (may be fresher than disk)."""
        raise NotImplementedError

    def write_cell(self, cell_id: str, source: str) -> None:
        """Replace a cell's source. Does not execute."""
        raise NotImplementedError

    def execute_cell(self, cell_id: str) -> CellInfo:
        """Run a cell on the notebook's kernel; return it with fresh outputs."""
        raise NotImplementedError

    def delete_cell(self, cell_id: str) -> None:
        """Remove a cell from the document."""
        raise NotImplementedError

    def insert_cell(
        self,
        source: str,
        before: Optional[str],
        after: Optional[str],
        execute: bool,
    ) -> CellInfo:
        """Insert a new code cell adjacent to an existing one. Exactly one of
        ``before``/``after`` must be given; returns the new cell (with its
        freshly minted id)."""
        raise NotImplementedError

    # -------------------------------------------------------------- snapshot
    def snapshot(self, cell_id: str) -> None:
        """Dump the current screen of the console displayed in ``cell_id`` into
        that cell's output — the per-view screenshot the toolbar camera does.
        The cell's output carries the ``ConsoleView`` widget model id; that
        maps to the live view on the kernel side. Raises if the cell holds no
        live console."""
        raise NotImplementedError


# The process-wide bridge. A real deployment swaps this for a JupyterBridge
# constructed by the server extension with handles to the kernel manager and
# the notebook document; the tools below never reference anything else.
bridge = Bridge()


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

mcp = FastMCP(name="quahog")


# --- Session tools (kernel side) ------------------------------------------- #


@mcp.tool(annotations=ToolAnnotations(title="List sessions", readOnlyHint=True))
def list_sessions() -> List[SessionInfo]:
    """List every live quahog session in the notebook's kernel.

    Start here: the returned names are what every other session tool takes as
    its ``session`` argument, and ``cell_ids`` tells you which notebook cells
    are currently showing each console (useful for ``snapshot``).
    """
    return bridge.list_sessions()


@mcp.tool(annotations=ToolAnnotations(title="Read session", readOnlyHint=True))
def read_session(
    name: str,
    kind: Literal["text", "raw", "screen", "minutes"] = "text",
    tail: Optional[int] = None,
) -> str:
    """Read what's on a session — the cheap "look before you act" call.

    Args:
        name: Session name (from ``list_sessions``).
        kind: What to read.
            ``text`` — clean accumulated output, escapes stripped (default);
            ``raw`` — accumulated output with escape sequences intact;
            ``screen`` — a pyte snapshot of what is on the screen right now
            (the right choice for full-screen / TUI apps like ``vim``, ``htop``);
            ``minutes`` — the log of commands typed interactively into the console.
        tail: If given, return only the last ``tail`` lines.
    """
    return bridge.read_session(name, kind=kind, tail=tail)


@mcp.tool(annotations=ToolAnnotations(title="Run command", idempotentHint=False))
def run(session: str, command: str, timeout: Optional[float] = None) -> RunResult:
    """Type a command into a session and capture its output — the same
    launch-and-capture core as the ``%qua`` magic and ``h.run()``.

    Use this for the common "run something, read the result" loop. Reach for a
    cell instead (``insert_cell(execute=True)``) when you need to keep the
    result as a Python object — e.g. ``r = h.exec("make test")`` — so later
    cells can use ``r``.

    Refuses if the session is busy running another command or is inside a
    full-screen (alt-screen) app; read ``kind="screen"`` and drive the console
    directly in that case.

    Args:
        session: Session name.
        command: A single shell command line (no embedded newlines; join with
            ``&&`` or use a cell running ``%%qua``).
        timeout: Seconds to wait. ``None`` waits indefinitely; on timeout the
            command keeps running and ``timed_out`` is ``True``.
    """
    return bridge.run(session, command, timeout)


@mcp.tool(annotations=ToolAnnotations(title="Toggle recording", idempotentHint=True))
def record(session: str, on: bool = True) -> bool:
    """Turn keystroke recording (the asciicast ``.cast`` sidecar) on or off for
    a session. Returns the new recording state.

    Args:
        session: Session name.
        on: ``True`` to start recording, ``False`` to stop.
    """
    return bridge.record(session, on)


# --- Notebook cell tools (document side) ----------------------------------- #
# Cell ids are the nbformat cell ids you learn by reading the .ipynb file once;
# these tools act on the LIVE document.


@mcp.tool(annotations=ToolAnnotations(title="Read cell", readOnlyHint=True))
def read_cell(cell_id: str) -> CellInfo:
    """Read a cell's live source and outputs by its nbformat id.

    The live document can be fresher than the ``.ipynb`` on disk (e.g. after a
    cell ran but before a save), so prefer this over re-reading the file when
    you need the current state of one cell.

    Args:
        cell_id: nbformat id of the cell to read.
    """
    return bridge.read_cell(cell_id)


@mcp.tool(annotations=ToolAnnotations(title="Write cell"))
def write_cell(cell_id: str, source: str) -> None:
    """Replace a cell's source. Does not execute it — follow with
    ``execute_cell`` when you want it to run.

    Args:
        cell_id: nbformat id of the cell to overwrite.
        source: New source for the cell.
    """
    bridge.write_cell(cell_id, source)


@mcp.tool(annotations=ToolAnnotations(title="Execute cell", idempotentHint=False))
def execute_cell(cell_id: str) -> CellInfo:
    """Execute a cell on the notebook's kernel and return it with its fresh
    outputs. Because the cell runs in the kernel namespace, any variables it
    binds (``h = q.bash()``, ``r = h.exec(...)``) persist for later cells and
    tools — this is the sanctioned way to use session features that return an
    object.

    Args:
        cell_id: nbformat id of the cell to execute.
    """
    return bridge.execute_cell(cell_id)


@mcp.tool(annotations=ToolAnnotations(title="Delete cell"))
def delete_cell(cell_id: str) -> None:
    """Delete a cell from the document by id.

    Args:
        cell_id: nbformat id of the cell to delete.
    """
    bridge.delete_cell(cell_id)


@mcp.tool(annotations=ToolAnnotations(title="Insert cell"))
def insert_cell(
    source: str,
    before: Optional[str] = None,
    after: Optional[str] = None,
    execute: bool = False,
) -> CellInfo:
    """Insert a new code cell next to an existing one and return it (including
    its newly assigned id).

    Set ``execute=True`` to run it immediately — the usual way to bind a
    variable in the kernel, e.g.
    ``insert_cell("r = h.exec('make test')", after=<id>, execute=True)`` then
    read ``r`` from a later cell.

    Args:
        source: Source for the new code cell.
        before: Insert immediately before this cell id.
        after: Insert immediately after this cell id.
        execute: Run the new cell right after inserting it.

    Exactly one of ``before`` / ``after`` must be given.
    """
    if (before is None) == (after is None):
        raise ValueError("pass exactly one of `before` or `after`")
    return bridge.insert_cell(source, before=before, after=after, execute=execute)


# --- Snapshot -------------------------------------------------------------- #


@mcp.tool(annotations=ToolAnnotations(title="Snapshot console cell"))
def snapshot(cell_id: str) -> None:
    """Snapshot the console displayed in a cell into that cell's own output.

    ``cell_id`` must be a cell that is already displaying a session console
    (from a ``display(h)``). The current screen is dumped as preformatted text
    into that cell — exactly the toolbar camera button, targeting only that
    view — so a full-screen app's state becomes visible and committable without
    disturbing other cells that show the same session. Read
    ``list_sessions().cell_ids`` to find cells that hold a console.

    Args:
        cell_id: nbformat id of a cell that is displaying a session console.
    """
    bridge.snapshot(cell_id)


if __name__ == "__main__":
    mcp.run()
