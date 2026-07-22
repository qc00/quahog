import datetime as dt
import sys
import time

import pytest

import quahog
from quahog import LAST_DUMP, Minute

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


def _wait_minutes(s, n=1, timeout=5.0):
    deadline = time.monotonic() + timeout
    while len(s.minutes) < n and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(s.minutes) >= n, "interactive command was not captured"


def test_interactive_command_captured(sh):
    sh.sendline("echo typed-interactively")
    _wait_minutes(sh)
    m = sh.minutes[0]
    assert isinstance(m, Minute)
    assert m.command == "echo typed-interactively"
    assert m.returncode == 0
    assert isinstance(m.when, dt.datetime)
    # The slices resolve into the session-lifetime streams.
    assert sh.text[m.text].strip() == "typed-interactively"
    assert "typed-interactively" in sh.raw[m.raw]


def test_interactive_failure_captured(sh):
    sh.sendline("false")
    _wait_minutes(sh)
    assert sh.minutes[0].returncode == 1


def test_slices_survive_multiple_commands(sh):
    sh.sendline("echo first-cmd")
    _wait_minutes(sh, 1)
    sh.sendline("echo second-cmd")
    _wait_minutes(sh, 2)
    first, second = sh.minutes[0], sh.minutes[1]
    assert sh.text[first.text].strip() == "first-cmd"
    assert sh.text[second.text].strip() == "second-cmd"


def test_programmatic_run_not_minuted(sh):
    sh.run("echo programmatic")
    time.sleep(0.3)
    assert not sh.minutes


def test_minuting_toggle(sh):
    sh.minuting = False
    sh.sendline("echo not-minuted")
    time.sleep(0.8)
    assert not sh.minutes


def test_inject_not_minuted(sh):
    sh.inject()
    time.sleep(0.8)
    assert not sh.minutes


def test_interactive_after_run_gets_own_command(sh):
    """Regression: run() must not leak its E-marker text into the next
    interactive command's capture."""
    sh.run("echo from-run")
    sh.sendline("echo from-typing")
    _wait_minutes(sh)
    m = sh.minutes[0]
    assert m.command == "echo from-typing"
    assert sh.text[m.text].strip() == "from-typing"


def test_interactive_after_fork_not_filtered(sh):
    """Regression: a fork()'s __qua_fork run used to leave its command text
    behind, making the next interactive command look like quahog plumbing."""
    f = sh.fork("echo fork-noise")
    f.wait(15)
    f.close()
    sh.sendline("echo visible-again")
    _wait_minutes(sh)
    assert sh.minutes[0].command == "echo visible-again"


def test_transcript_updates(ip, sh):
    """Every cell that displays the session is a live view whose primary
    output's text/plain is kept in sync as interactive commands complete
    (PLAN.md §4/§5)."""
    sh._ipython_display_()
    widget, _handle, _notes, _header = sh._state.views[-1]
    sh.sendline("echo into-transcript")
    _wait_minutes(sh)
    time.sleep(0.2)
    assert "into-transcript" in widget._text


def test_stale_view_pruned_when_comm_closes(ip, sh):
    """A view whose frontend counterpart is gone (its cell was re-executed,
    its output cleared, ...) must not stay in Session._views forever --
    otherwise it keeps fanning PTY output to a dead comm indefinitely, and
    is a standing extra input source. The frontend signals this itself via
    comm_close; nothing here guesses at *why* the view went away."""
    sh._ipython_display_()
    sh._ipython_display_()
    assert len(sh._state.views) == 2
    first_widget = sh._state.views[0][0]
    second_widget = sh._state.views[1][0]

    first_widget.comm.handle_close({})
    assert len(sh._state.views) == 1
    assert sh._state.views[0][0] is second_widget

    second_widget.comm.handle_close({})
    assert len(sh._state.views) == 0


def test_run_refuses_during_interactive(sh):
    sh.sendline("sleep 0.6")
    time.sleep(0.3)  # C marker fired, D not yet
    with pytest.raises(RuntimeError, match="busy"):
        sh.run("echo nope")
    time.sleep(0.8)


# ------------------------------------------------------------------ dumping


def _type(sh, cmd, n):
    sh.sendline(cmd)
    _wait_minutes(sh, n)


@pytest.fixture()
def ip():
    from IPython.testing.globalipapp import get_ipython, start_ipython

    ipy = start_ipython() or get_ipython()
    ipy.run_line_magic("load_ext", "quahog")
    return ipy


def _dumped_text(ip, sh, **kwargs):
    ip.payload_manager.clear_payload()
    result = sh.dump_minutes_as_cell(**kwargs)
    assert result is None
    payloads = ip.payload_manager.read_payload()
    ip.payload_manager.clear_payload()
    sni = [p for p in payloads if p.get("source") == "set_next_input"]
    if not sni:
        return None
    assert sni[0]["replace"] is False
    return sni[0]["text"]


def test_dump_default_since_last_dump(ip, sh):
    _type(sh, "echo one", 1)
    _type(sh, "echo two", 2)
    text = _dumped_text(ip, sh)
    assert text == "%qua echo one\n%qua echo two"
    assert sh.last_dump == 2
    # Nothing new: empty dump, no payload.
    assert _dumped_text(ip, sh) is None
    _type(sh, "echo three", 3)
    assert _dumped_text(ip, sh) == "%qua echo three"
    assert sh.last_dump == 3


def test_dump_prefix_modes(ip, sh):
    _type(sh, "echo a", 1)
    _type(sh, "echo b", 2)
    assert _dumped_text(ip, sh, since=0, prefix_per_cmd=False) == ("%%qua\necho a\necho b")
    assert _dumped_text(ip, sh, since=0, prefix_per_cmd=None) == "echo a\necho b"


def test_dump_named_session_prefix(ip, sh):
    other = quahog.bash()  # becomes quahog.default
    try:
        _type(sh, "echo mine", 1)
        text = _dumped_text(ip, sh, since=0)
        assert text == f"%qua -s {sh.name} echo mine"
        text = _dumped_text(ip, sh, since=0, prefix_per_cmd=False)
        assert text.startswith(f"%%qua {sh.name}\n")
    finally:
        other.close()
        quahog.sessions.pop(other.name, None)


def test_dump_index_and_datetime_bounds(ip, sh):
    _type(sh, "echo early", 1)
    cut = dt.datetime.now()
    time.sleep(0.05)
    _type(sh, "echo late", 2)
    assert _dumped_text(ip, sh, since=1) == "%qua echo late"
    assert _dumped_text(ip, sh, since=0, until=1) == "%qua echo early"
    assert _dumped_text(ip, sh, since=cut) == "%qua echo late"
    assert _dumped_text(ip, sh, since=0, until=cut) == "%qua echo early"


def test_dump_writes_payload_via_ipython(ip, sh):
    sh.sendline("echo dump-me")
    _wait_minutes(sh)
    text = _dumped_text(ip, sh)
    assert text == "%qua echo dump-me"
