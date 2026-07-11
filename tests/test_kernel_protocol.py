"""Protocol-level verification of minuting against a real ipykernel.

Frontends consume exactly two mechanisms (PLAN.md §5):
  - update_display_data messages keeping each displaying cell's single
    output (widget view + text/plain console log) in sync,
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
    """display_data messages that are a console cell's single output: the
    widget-view mimetype plus a transient display_id (PLAN.md §4)."""
    return [
        m
        for m in displays
        if "application/vnd.jupyter.widget-view+json" in m["content"]["data"]
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
