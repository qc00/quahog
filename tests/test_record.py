"""Recording (PLAN.md §6): .cast sidecars, delayed-flush tail, suppression."""

import json
import sys
import time

import pytest

import quahog
from quahog.record import PLACEHOLDER, CastWriter, EchoClassifier, Recorder, sidecar_dir

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


def _events(path):
    lines = path.read_text().splitlines()
    return json.loads(lines[0]), [json.loads(ln) for ln in lines[1:]]


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def rsh(tmp_path, monkeypatch, shell_env, sh):
    shell_env["JPY_SESSION_NAME"] = None
    monkeypatch.chdir(tmp_path)
    sh.record(True)
    return sh


# ------------------------------------------------------------------ sessions


def test_cast_sidecar_and_events(rsh, tmp_path):
    r = rsh.run("echo hello-cast")
    assert r.ok
    path = rsh.cast_path
    assert path is not None
    assert path.parent == tmp_path / "console.quahog"
    assert path.suffix == ".cast"
    rsh.close()

    header, events = _events(path)
    assert header["version"] == 2
    assert header["width"] and header["height"]
    out = "".join(e[2] for e in events if e[1] == "o")
    ins = [e[2] for e in events if e[1] == "i"]
    assert "hello-cast" in out
    assert any("echo hello-cast" in i for i in ins)
    # timestamps stay monotonic
    times = [e[0] for e in events]
    assert times == sorted(times)


def test_echo_off_auto_suppression(rsh):
    """termios ECHO off (a local password prompt) auto-replaces input with a
    placeholder even when the caller didn't say record=False."""
    r = rsh.run("read -s x; echo len:${#x}", wait=False)
    assert _wait(lambda: rsh._echo_on() is False)
    rsh.send("topsecret\r")
    r.wait(10)
    assert "len:9" in r.text
    rsh.close()

    _, events = _events(rsh.cast_path)
    assert "topsecret" not in json.dumps(events)
    assert any(e[1] == "i" and e[2] == PLACEHOLDER for e in events)


def test_stdin_raw_records_by_default(rsh):
    """.raw is the byte layer, not a recording bypass: it records like any
    other input unless the caller asks for record=False (PLAN.md §3)."""
    rsh.stdin.raw.write(b"echo raw-layer\r")
    assert _wait(lambda: "raw-layer" in rsh.text)
    rsh.close()

    _, events = _events(rsh.cast_path)
    ins = [e[2] for e in events if e[1] == "i"]
    assert any("echo raw-layer" in i for i in ins)


def test_stdin_raw_and_record_false(rsh):
    """The sanctioned secret-feeding paths leave only a placeholder."""
    r = rsh.run("read -s a; read -s b; echo lens:${#a}:${#b}", wait=False)
    # Each read toggles local echo off independently; wait for it before every
    # send, or a real (briefly echo-on) gap between the two reads can echo
    # the keystrokes to the screen — an output-side leak outside recording's
    # control (PLAN.md §6: "output-side secrets ... out of scope").
    assert _wait(lambda: rsh._echo_on() is False)
    rsh.send("first-secret\r", record=False)
    assert _wait(lambda: rsh._echo_on() is False)
    rsh.stdin.raw.write(b"second-secret\r", record=False)
    r.wait(10)
    assert "lens:12:13" in r.text
    rsh.close()

    _, events = _events(rsh.cast_path)
    dump = json.dumps(events)
    assert "first-secret" not in dump
    assert "second-secret" not in dump


def test_erase_rewrites_tail(rsh):
    """⌫ redacts the most recent keystrokes in place, echoed or not."""
    rsh.send("Z")
    rsh.send("Q")
    assert rsh.erase(2) == 2
    rsh.send("\x15")  # ctrl-u: clear the shell's line buffer
    rsh.close()

    _, events = _events(rsh.cast_path)
    ins = [e[2] for e in events if e[1] == "i"]
    assert "Z" not in ins and "Q" not in ins
    assert ins.count(PLACEHOLDER) >= 2
    times = [e[0] for e in events]
    assert times == sorted(times)  # erase never reorders


def test_pause_and_resume(rsh):
    rsh.record(False)
    assert not rsh.recording
    rsh.run("echo while-paused")
    rsh.record(True)
    assert rsh.recording
    rsh.run("echo after-resume")
    rsh.close()

    _, events = _events(rsh.cast_path)
    dump = json.dumps(events)
    assert "while-paused" not in dump
    assert "after-resume" in dump


def test_record_starts_lazily(tmp_path, monkeypatch, sh):
    monkeypatch.delenv("JPY_SESSION_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert sh.cast_path is None and not sh.recording
    sh.record(True)
    assert sh.recording and sh.cast_path is not None
    sh.run("echo lazy-start")
    sh.close()
    _, events = _events(sh.cast_path)
    assert "lazy-start" in json.dumps(events)


def test_fork_gets_own_cast(rsh, tmp_path):
    f = rsh.fork("echo fork-out; echo fork-err >&2")
    assert f.wait(15) == 0
    assert f.cast_path is not None
    assert f.cast_path.parent == tmp_path / "console.quahog"
    assert _wait(lambda: "fork-out" in f.cast_path.read_text() or True)
    f.close()
    _, events = _events(f.cast_path)
    out = "".join(e[2] for e in events if e[1] == "o")
    assert "fork-out" in out and "fork-err" in out


# ---------------------------------------------------------------- unit level


def test_sidecar_dir_from_jpy_session_name(tmp_path, monkeypatch):
    monkeypatch.setenv("JPY_SESSION_NAME", str(tmp_path / "deploy.ipynb"))
    assert sidecar_dir() == tmp_path / "deploy.quahog"


def test_sidecar_dir_cwd_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("JPY_SESSION_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert sidecar_dir() == tmp_path / "console.quahog"


def test_castwriter_erase_and_close(tmp_path):
    w = CastWriter(tmp_path / "t.cast", 80, 24, tail_seconds=60)
    w.append("o", "hello")
    w.append("i", "s3cret")
    assert w.erase_inputs() == 1
    assert w.erase_inputs() == 0  # placeholders are not re-erased
    w.close()
    _, events = _events(tmp_path / "t.cast")
    assert [e[1] for e in events] == ["o", "i"]
    assert events[1][2] == PLACEHOLDER
    assert "s3cret" not in json.dumps(events)


def test_castwriter_delayed_flush(tmp_path):
    w = CastWriter(tmp_path / "t.cast", 80, 24, tail_seconds=0.1)
    w.append("o", "old-event")
    time.sleep(0.25)
    w.append("o", "fresh-event")  # pushes the first one past the tail window
    text = (tmp_path / "t.cast").read_text()
    assert "old-event" in text
    assert "fresh-event" not in text  # still in the in-memory tail
    w.close()
    assert "fresh-event" in (tmp_path / "t.cast").read_text()


def test_echo_classifier_three_way():
    seen = []
    c = EchoClassifier(seen.append)
    c.input("a")
    c.output("a")  # verbatim echo
    c.input("b")
    c.output("*")  # masked echo
    c.input("c")
    time.sleep(EchoClassifier.WINDOW + 0.15)  # nothing came back
    c.input("d", unechoed=True)  # termios already said ECHO off
    c.input("\r")  # control keys are not classified
    c.output("noise")
    assert seen == ["verbatim", "masked", "none", "none"]


def test_echo_classifier_close_cancels_pending_timer():
    """A keystroke classified right as the session closes must not leave a
    live daemon timer behind, firing into a closed recorder afterward."""
    seen = []
    c = EchoClassifier(seen.append)
    c.input("a")  # arms the WINDOW-second expiry timer, no output yet
    assert c._timer is not None
    c.close()
    assert c._timer is None
    assert c._pending is None
    time.sleep(EchoClassifier.WINDOW + 0.15)
    assert seen == []  # the cancelled timer never fired


def test_recorder_close_reflects_in_recording_state(tmp_path):
    """`.recording` must go False once closed -- the writer object is kept
    around on purpose (so cast_path still works for post-mortem inspection),
    so `recording` has to be driven by the enabled flag, not by whether a
    writer object merely exists."""
    r = Recorder("s")
    r.start(24, 80, path=tmp_path / "t.cast")
    assert r.recording
    r.close()
    assert not r.recording
    assert r.cast_path == tmp_path / "t.cast"  # still inspectable after close


def test_recorder_close_is_idempotent(tmp_path):
    r = Recorder("s")
    r.start(24, 80, path=tmp_path / "t.cast")
    r.close()
    r.close()  # must not raise
