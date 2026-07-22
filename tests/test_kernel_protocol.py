import shutil
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


def _stdin_states(iopub):
    """The stdin-state widget messages, in order: what the toolbar's badge is
    driven by (PLAN.md §3)."""
    states = []
    for m in iopub:
        if m["msg_type"] != "comm_msg":
            continue
        content = m["content"].get("data", {}).get("content") or {}
        if content.get("type") == "stdin-state":
            states.append(content.get("state"))
    return states


def test_stdin_state_wire_protocol(kernel):
    """The toolbar indicates when typing no longer reaches the shell. The
    frontend can't work this out for itself — exit is a kernel-side fact — so
    it is pushed as its own message on every transition, and answered on a
    new view's ready handshake. exec() rides pipes on the far end and never
    borrows stdin, so "open" is the only state short of "closed"."""
    kc = kernel
    execute(
        kc,
        "import shutil, time, tempfile\nimport quahog as q\n"
        "s = q.bash(env={'HISTFILE': tempfile.mktemp()})\ns._ipython_display_()\n",
        settle=0.5,
    )

    # A view attaching mid-session is told the current state, not left blank.
    _, iopub = execute(
        kc,
        "w = s._state.views[-1][0]\ns._on_widget_msg(w, {'type': 'ready'}, [])\ntime.sleep(0.3)\n",
        settle=0.5,
    )
    assert _stdin_states(iopub) == ["open"]

    if shutil.which("perl"):
        # exec() runs concurrently and never takes stdin away from the shell.
        _, iopub = execute(kc, "es = s.exec('echo concurrent')\nes.wait(15)\ntime.sleep(0.3)\n", settle=0.5)
        assert _stdin_states(iopub) == [] or set(_stdin_states(iopub)) == {"open"}

    # Exit closes stdin for good.
    _, iopub = execute(kc, "s.close()\ntime.sleep(0.3)\n", settle=1.0)
    assert _stdin_states(iopub)[-1] == "closed"


def test_minuting_wire_protocol(kernel):
    kc = kernel
    # A session display is a single output: the live widget and the console
    # log's text/plain together, under one display_id.
    _, iopub = execute(
        kc,
        "import tempfile\nimport quahog as q\n"
        "h = q.bash(env={'HISTFILE': tempfile.mktemp()})\n"
        "h._ipython_display_()\n",
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
    # h stays open -- test_screenshot_direct_call_wire_protocol reuses it.


def test_screenshot_direct_call_wire_protocol(kernel):
    """h.screenshot() called directly in a cell is just a returned Note: it
    auto-displays as that cell's own execute_result, and — unlike the
    toolbar's camera button — does not get published onto any *other* cell
    that happens to display the same session (PLAN.md §6)."""
    kc = kernel

    reply, iopub = execute(kc, "h._ipython_display_()", settle=0.5)
    other_id = reply["parent_header"]["msg_id"]

    reply, iopub = execute(kc, "h.screenshot()", settle=0.5)
    results = [m for m in iopub if m["msg_type"] == "execute_result"]
    assert results, "screenshot()'s return value did not auto-display"
    assert "[screen" in results[0]["content"]["data"].get("text/plain", "")

    stray = [
        m
        for m in _displays(iopub)
        if not m["content"].get("transient", {}).get("display_id") and m["parent_header"].get("msg_id") == other_id
    ]
    assert not stray, f"screenshot() leaked into another cell's output: {stray!r}"


def test_screenshot_toolbar_click_wire_protocol(kernel):
    """A toolbar click (simulated the way the frontend actually triggers it:
    a widget message carrying the specific view that was clicked) publishes
    the screenshot only onto that *one* view's cell — not every cell
    displaying the session, and each click is its own new, separate output
    rather than an update to a shared slot (PLAN.md §6)."""
    kc = kernel

    reply1, iopub1 = execute(kc, "h._ipython_display_()", settle=0.5)
    id1 = reply1["parent_header"]["msg_id"]
    reply2, iopub2 = execute(kc, "h._ipython_display_()", settle=0.5)
    id2 = reply2["parent_header"]["msg_id"]
    assert id1 != id2

    execute(kc, "1 + 1")  # shifts the kernel's ambient "current" context

    def _new_outputs(iopub):
        return [m for m in _displays(iopub) if not m["content"].get("transient", {}).get("display_id")]

    # Click on the SECOND view specifically.
    _, iopub = execute(
        kc,
        "widget2 = h._state.views[-1][0]\n"
        "h._on_widget_msg(widget2, {'type': 'screenshot'}, [])\n"
        "import time; time.sleep(0.5)\n",
        settle=1.0,
    )
    news = _new_outputs(iopub)
    assert len(news) == 1, f"expected exactly one new output, got: {news!r}"
    assert news[0]["parent_header"]["msg_id"] == id2, "screenshot landed on the wrong (unclicked) cell"
    assert news[0]["parent_header"]["msg_id"] != id1
    assert "[screen" in news[0]["content"]["data"].get("text/plain", "")
    first_click_id = news[0]["header"]["msg_id"]

    # Clicking again produces a second, distinct output on the same view.
    _, iopub = execute(
        kc,
        "h._on_widget_msg(widget2, {'type': 'screenshot'}, [])\nimport time; time.sleep(0.5)\n",
        settle=1.0,
    )
    news2 = _new_outputs(iopub)
    assert len(news2) == 1
    assert news2[0]["parent_header"]["msg_id"] == id2
    assert (
        news2[0]["header"]["msg_id"] != first_click_id
    ), "two clicks must be two distinct output messages, not the same one repeated"
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
