"""Protocol-level verification of minuting against a real ipykernel.

Frontends consume exactly three mechanisms:
  - update_display_data keeping each displaying cell's primary output (the
    widget + plain session text/plain, PLAN.md §5) in sync,
  - update_display_data keeping each displaying cell's second, initially
    empty/invisible output (screenshots, interceptor notes, the recording
    indicator — not literal PTY bytes, PLAN.md §6) in sync,
  - execute_reply payloads with source=set_next_input creating %qua cells.
These tests assert on those wire messages via jupyter_client — stronger than a
pixel test, and it is the same surface VS Code consumes.
"""

import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


@pytest.fixture(scope="module")
def kernel():
    jc = pytest.importorskip("jupyter_client")
    from jupyter_client.manager import start_new_kernel

    km, kc = start_new_kernel(kernel_name="python3", startup_timeout=60)
    yield kc
    kc.stop_channels()
    km.shutdown_kernel(now=True)


def execute(kc, code, timeout=60, settle=0.0):
    """Run code; return (reply, iopub_msgs). Optionally keep draining iopub
    for `settle` seconds after idle, to catch async update_display_data."""
    msg_id = kc.execute(code)
    iopub = []
    idle = False
    deadline = time.monotonic() + timeout
    while not idle and time.monotonic() < deadline:
        try:
            msg = kc.get_iopub_msg(timeout=1)
        except Exception:
            continue
        iopub.append(msg)
        if (
            msg["msg_type"] == "status"
            and msg["content"].get("execution_state") == "idle"
            and msg["parent_header"].get("msg_id") == msg_id
        ):
            idle = True
    reply = kc.get_shell_msg(timeout=timeout)
    end = time.monotonic() + settle
    while time.monotonic() < end:
        try:
            iopub.append(kc.get_iopub_msg(timeout=0.25))
        except Exception:
            pass
    return reply, iopub


def _displays(iopub, msg_type="display_data"):
    return [m for m in iopub if m["msg_type"] == msg_type]


def _live_displays(displays):
    """display_data messages that are a console cell's primary output: the
    widget-view mimetype plus a transient display_id (PLAN.md §4)."""
    return [
        m
        for m in displays
        if "application/vnd.jupyter.widget-view+json" in m["content"]["data"]
        and m["content"].get("transient", {}).get("display_id")
    ]


def _notes_displays(displays):
    """display_data messages that are a console cell's *second* output: a
    transient display_id, but no widget-view mimetype (PLAN.md §6)."""
    return [
        m
        for m in displays
        if "application/vnd.jupyter.widget-view+json" not in m["content"]["data"]
        and m["content"].get("transient", {}).get("display_id")
    ]


def test_minuting_wire_protocol(kernel):
    kc = kernel
    # A session display is a single output: the live widget and the console
    # log's text/plain together, under one display_id.
    _, iopub = execute(
        kc,
        "import quahog as q\n" "h = q.bash(inherit_rc=False)\n" "h._ipython_display_()\n",
        settle=0.5,
    )
    anchors = _live_displays(_displays(iopub))
    assert len(anchors) == 1, f"expected exactly one display, got: {_displays(iopub)!r}"
    assert "text/plain" in anchors[0]["content"]["data"]
    display_id = anchors[0]["content"]["transient"]["display_id"]

    # Interactive command (sendline = same PTY path as typing in the widget).
    # No sleep inside the cell: the command must complete *between* executions,
    # as real typing does — otherwise the minute queue flushes on this very
    # cell's reply instead of the next one.
    _, iopub = execute(kc, "h.sendline('echo minuted-live')", settle=2.0)
    updates = [
        m
        for m in _displays(iopub, "update_display_data")
        if m["content"].get("transient", {}).get("display_id") == display_id
    ]
    assert updates, "no update_display_data arrived for the console log"
    text = updates[-1]["content"]["data"].get("text/plain", "")
    # The console log mirrors clean terminal text (PLAN.md §5), not a
    # "$ cmd / [exit]" per-command block format.
    assert "echo minuted-live" in text
    assert "minuted-live" in text

    # Explicit dump: the set_next_input payload rides the dumping cell's own
    # reply — the only payload timing every frontend (incl. VS Code) honors.
    reply, _ = execute(kc, "h.dump_minutes_as_cell()")
    payloads = reply["content"].get("payload", [])
    sni = [p for p in payloads if p.get("source") == "set_next_input"]
    assert sni, f"no set_next_input payload in reply: {payloads!r}"
    assert sni[0]["text"] == "%qua echo minuted-live"
    assert sni[0]["replace"] is False

    # A second dump with nothing new adds no payload.
    reply, _ = execute(kc, "h.dump_minutes_as_cell()")
    payloads = reply["content"].get("payload", [])
    assert not [p for p in payloads if p.get("source") == "set_next_input"]


def test_screenshot_second_output_wire_protocol(kernel):
    """A display starts with two outputs — the primary widget, and a second,
    initially empty/invisible one for interceptor notes and the recording
    indicator (PLAN.md §6). An earlier revision merged screenshots into that
    same second slot too, silently breaking their "add a new, individually
    copyable/deletable output" behavior: nothing was visibly wrong (the slot
    still updated), so it went unnoticed until manual testing."""
    kc = kernel

    _, iopub = execute(kc, "h._ipython_display_()", settle=0.5)
    displays = _displays(iopub)
    anchors = _live_displays(displays)
    notes = _notes_displays(displays)
    assert len(anchors) == 1
    assert len(notes) == 1, f"expected exactly one second (notes) output, got: {displays!r}"
    # Invisible until something is actually noted.
    assert notes[0]["content"]["data"].get("text/plain", "") == ""
    # h stays open -- test_screenshot_separate_output_wire_protocol reuses it.


def test_screenshot_separate_output_wire_protocol(kernel):
    """Each screenshot is published as its own brand-new output (PLAN.md §6)
    — as if display() were called again — not merged into a single updated
    slot, so each one can be individually copied or deleted. Attribution to
    the right cell must survive an unrelated cell running in between, since a
    real screenshot is usually triggered by a toolbar click at an arbitrary
    later time, not synchronously from the anchor cell itself."""
    kc = kernel

    reply, iopub = execute(kc, "h._ipython_display_()", settle=0.5)
    anchor_id = reply["parent_header"]["msg_id"]
    assert len(_live_displays(_displays(iopub))) == 1

    execute(kc, "1 + 1")  # shifts the kernel's ambient "current" context

    # screenshot() fans out to every live view (PLAN.md §4 convention) -- this
    # shared kernel already has other views from earlier tests in this file,
    # so filter to messages attributed to *this* anchor specifically.
    def _new_outputs_for_anchor(iopub):
        return [
            m
            for m in _displays(iopub)
            if not m["content"].get("transient", {}).get("display_id")
            and m["parent_header"].get("msg_id") == anchor_id
        ]

    _, iopub1 = execute(kc, "h.screenshot()", settle=1.0)
    shots1 = _new_outputs_for_anchor(iopub1)
    assert len(shots1) == 1, f"expected exactly one new output for this anchor, got: {shots1!r}"
    assert "[screen" in shots1[0]["content"]["data"].get("text/plain", "")

    _, iopub2 = execute(kc, "h.screenshot()", settle=1.0)
    shots2 = _new_outputs_for_anchor(iopub2)
    assert len(shots2) == 1
    assert shots2[0]["header"]["msg_id"] != shots1[0]["header"]["msg_id"], (
        "two screenshots must be two distinct output messages, not the same one repeated"
    )
    # h stays open -- test_concurrent_displays_wire_protocol reuses it.


def test_concurrent_displays_wire_protocol(kernel):
    """There is no hop: displaying the same session again opens a second,
    independent live view rather than replacing the first, and a later
    interactive command keeps *every* live view's output in sync (PLAN.md
    §4) — the same fan-out the tap already does for pop-out views."""
    kc = kernel

    _, iopub = execute(kc, "h._ipython_display_()", settle=0.5)
    anchors = _live_displays(_displays(iopub))
    assert len(anchors) == 1
    second_id = anchors[0]["content"]["transient"]["display_id"]

    _, iopub = execute(kc, "h._ipython_display_()", settle=0.5)
    anchors = _live_displays(_displays(iopub))
    assert len(anchors) == 1
    third_id = anchors[0]["content"]["transient"]["display_id"]
    assert third_id != second_id, "each display() call should get its own live view"

    _, iopub = execute(
        kc,
        "h.sendline('echo both-live')\nimport time; time.sleep(1.2)\n",
        settle=1.5,
    )
    updates = _displays(iopub, "update_display_data")
    updated_ids = {m["content"].get("transient", {}).get("display_id") for m in updates}
    assert {second_id, third_id} <= updated_ids, "not every live view was kept in sync"
    for m in updates:
        assert "both-live" in m["content"]["data"].get("text/plain", "")

    execute(kc, "h.close()")
