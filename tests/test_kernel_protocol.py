"""Protocol-level verification of minuting against a real ipykernel.

Frontends consume exactly two mechanisms (PLAN.md §5):
  - update_display_data messages carrying the anchor-cell transcript,
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


def test_minuting_wire_protocol(kernel):
    kc = kernel
    # Anchor display: widget view + transcript display (with display_id).
    _, iopub = execute(
        kc,
        "import quahog as q\n"
        "h = q.bash(inherit_rc=False)\n"
        "h._ipython_display_()\n",
        settle=0.5,
    )
    displays = _displays(iopub)
    widget_views = [
        m for m in displays
        if "application/vnd.jupyter.widget-view+json" in m["content"]["data"]
    ]
    transcripts = [
        m for m in displays
        if m["content"].get("transient", {}).get("display_id")
        and "application/vnd.jupyter.widget-view+json" not in m["content"]["data"]
    ]
    assert widget_views, "console widget view was not displayed"
    assert transcripts, "transcript display (with display_id) was not emitted"
    display_id = transcripts[-1]["content"]["transient"]["display_id"]

    # Interactive command (sendline = same PTY path as typing in the widget).
    # No sleep inside the cell: the command must complete *between* executions,
    # as real typing does — otherwise the minute queue flushes on this very
    # cell's reply instead of the next one.
    _, iopub = execute(kc, "h.sendline('echo minuted-live')", settle=2.0)
    updates = [
        m for m in _displays(iopub, "update_display_data")
        if m["content"].get("transient", {}).get("display_id") == display_id
    ]
    assert updates, "no update_display_data arrived for the transcript"
    text = updates[-1]["content"]["data"].get("text/plain", "")
    assert "$ echo minuted-live" in text
    assert "minuted-live" in text

    # Next execution flushes the minute queue as a set_next_input payload.
    reply, _ = execute(kc, "1 + 1")
    payloads = reply["content"].get("payload", [])
    sni = [p for p in payloads if p.get("source") == "set_next_input"]
    assert sni, f"no set_next_input payload in reply: {payloads!r}"
    assert sni[0]["text"] == "%qua echo minuted-live"
    assert sni[0]["replace"] is False


def test_hop_new_transcript_wire_protocol(kernel):
    kc = kernel
    # Hop: display again → fresh transcript display_id.
    _, iopub = execute(kc, "h._ipython_display_()", settle=0.5)
    transcripts = [
        m for m in _displays(iopub)
        if m["content"].get("transient", {}).get("display_id")
        and "application/vnd.jupyter.widget-view+json" not in m["content"]["data"]
    ]
    assert transcripts, "hop did not emit a fresh transcript display"
    new_id = transcripts[-1]["content"]["transient"]["display_id"]

    _, iopub = execute(
        kc,
        "h.sendline('echo after-hop')\nimport time; time.sleep(1.2)\n",
        settle=1.5,
    )
    updates = [
        m for m in _displays(iopub, "update_display_data")
        if m["content"].get("transient", {}).get("display_id") == new_id
    ]
    assert updates, "no transcript update for the hopped anchor"
    text = updates[-1]["content"]["data"].get("text/plain", "")
    assert "$ echo after-hop" in text, f"wrong transcript text: {text!r}"
    execute(kc, "h.close()")
