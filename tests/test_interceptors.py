"""Interceptors (PLAN.md §6): the API, editor diffs, and password hygiene.

The password story is verified as a behavior (input recorded as a placeholder
around a matched prompt), not by poking at internals — that is the invariant
that must hold.
"""

import json
import sys
import time

import pytest

import quahog
from quahog import interceptors
from quahog.interceptors.builtins import (
    EditorDiffInterceptor,
    PagerInterceptor,
    PasswordInterceptor,
)
from quahog.record import PLACEHOLDER

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only")


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def sh():
    s = quahog.bash(inherit_rc=False)
    yield s
    s.close()
    quahog.sessions.pop(s.name, None)


# --------------------------------------------------------------- match/hooks


def test_shipped_interceptors_present():
    kinds = {type(i).__name__ for i in interceptors.all_interceptors()}
    assert {"EditorDiffInterceptor", "PagerInterceptor", "PasswordInterceptor"} <= kinds


def test_editor_matches_only_with_file():
    itc = EditorDiffInterceptor()
    assert itc.match(["vim", "notes.txt"], None)
    assert itc.match(["/usr/bin/nano", "-w", "f"], None)
    assert not itc.match(["vim"], None)  # no file target
    assert not itc.match(["cat", "f"], None)


def test_pager_and_password_match():
    assert PagerInterceptor().match(["less", "f"], None)
    assert PagerInterceptor().match(["man", "ls"], None)
    for cmd in ("sudo", "su", "ssh", "passwd"):
        assert PasswordInterceptor().match([cmd, "x"], None)
    assert not PasswordInterceptor().match(["echo", "hi"], None)


def test_password_prompt_regex():
    rx = PasswordInterceptor.PROMPT_RE
    assert rx.search("[sudo] password for bob: ")
    assert rx.search("Enter passphrase for key '/x':")
    assert rx.search("bob@host's password:")
    assert not rx.search("the password was changed successfully")


def test_custom_interceptor_registration(sh):
    """A registered interceptor's before/after hooks fire around a matched
    interactive command, and after()'s return lands in the session's
    console-log transcript as a Note (PLAN.md §6) — it isn't literal PTY
    output, so it wouldn't otherwise appear anywhere."""
    calls = []

    class Spy:
        def match(self, argv, session):
            return argv[0] == "true"

        def before(self, ctx):
            calls.append(("before", ctx.command))

        def after(self, ctx):
            calls.append(("after", ctx.command))
            return "spy-note"

    spy = Spy()
    interceptors.register(spy)
    try:
        sh.sendline("true")
        assert _wait(lambda: len(sh.minutes) >= 1)
        time.sleep(0.2)
        assert ("before", "true") in calls
        assert ("after", "true") in calls
        block = sh._transcript.blocks[-1]
        assert block._plain() == "spy-note"
    finally:
        interceptors.all_interceptors().remove(spy)


def _editor_ctx(cwd, path):
    return interceptors.Ctx(
        type("S", (), {"cwd": str(cwd), "_recorder": None})(),
        ["vim", str(path)],
        f"vim {path}",
    )


def test_editor_diff_note(tmp_path):
    """before() snapshots, after() diffs across the command boundary —
    regardless of what actually changed the file."""
    f = tmp_path / "poem.txt"
    f.write_text("roses\n")
    itc = EditorDiffInterceptor()
    ctx = _editor_ctx(tmp_path, f)
    itc.before(ctx)
    f.write_text("roses\nviolets\n")
    note = itc.after(ctx)
    assert note is not None
    assert "+violets" in note


def test_editor_diff_new_file(tmp_path):
    """A freshly created file diffs from the empty string, not a crash."""
    f = tmp_path / "brand-new.txt"
    itc = EditorDiffInterceptor()
    ctx = _editor_ctx(tmp_path, f)
    itc.before(ctx)
    f.write_text("first line\n")
    note = itc.after(ctx)
    assert note is not None and "+first line" in note


def test_editor_diff_none_when_unchanged(tmp_path):
    f = tmp_path / "same.txt"
    f.write_text("unchanged\n")
    itc = EditorDiffInterceptor()
    ctx = _editor_ctx(tmp_path, f)
    itc.before(ctx)
    assert itc.after(ctx) is None


# ----------------------------------------------------- password behavior (§6)


def test_remote_style_prompt_suppressed(sh, tmp_path, monkeypatch):
    """A command matched by the password interceptor that prints a
    'Password:' prompt (no local termios ECHO change — the remote case, since
    a real ssh session leaves the local pty's mode untouched) still gets its
    typed answer recorded as a placeholder on the *input* side.

    The fake ``ssh`` below deliberately echoes the answer back (plain
    ``read``, not ``-s``) to prove the interceptor — not local echo
    detection — is what suppressed the input: PLAN.md §6 explicitly puts
    output-side echoing out of scope ("delete the cell if needed"), so this
    only asserts on the "i" events.
    """
    monkeypatch.delenv("JPY_SESSION_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    sh.record(True)

    # 'ssh' is matched by PasswordInterceptor.
    # (bash's DEBUG trap never fires for a bare function-definition line, so
    # this needs a trailing no-op to get the run() its C/D markers.)
    sh.run("ssh() { printf \"user@host's password: \"; read ans; echo got:${#ans}; }; :")
    r = sh.run("ssh example", wait=False)
    assert _wait(lambda: "password:" in sh.text.lower())
    time.sleep(0.2)  # let on_output fire and arm suppression
    sh.send("hunter2primary\r")
    r.wait(10)
    assert "got:14" in r.text
    sh.close()

    events = [json.loads(ln) for ln in sh.cast_path.read_text().splitlines()[1:]]
    ins = [e[2] for e in events if e[1] == "i"]
    assert not any("hunter2primary" in i for i in ins)
    assert any(i == PLACEHOLDER for i in ins)
