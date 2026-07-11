"""Kernel-side screen state (PLAN.md §4/§6): screenshots and alt-screen."""

import sys
import time

import pytest

import quahog
from quahog.screen import ScreenMirror

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


# ---------------------------------------------------------------- unit level


def test_mirror_snapshot_reflects_output():
    m = ScreenMirror(24, 80)
    m.feed(b"hello world\r\n")
    assert "hello world" in m.snapshot()


def test_mirror_carriage_return_overwrite():
    m = ScreenMirror(24, 80)
    m.feed(b"aaaaa\rbb")
    assert m.snapshot().splitlines()[0] == "bbaaa"


def test_mirror_altscreen_transitions():
    m = ScreenMirror(24, 80)
    assert m.feed(b"normal text") is None
    assert m.altscreen is False
    assert m.feed(b"\x1b[?1049h") is True
    assert m.altscreen is True
    assert m.feed(b"in the app") is None  # no change, no signal
    assert m.feed(b"\x1b[?1049l") is False
    assert m.altscreen is False


def test_mirror_altscreen_split_across_reads():
    """The switch sequence split across two feeds is still detected."""
    m = ScreenMirror(24, 80)
    assert m.feed(b"\x1b[?10") is None
    assert m.feed(b"49h") is True
    assert m.altscreen is True


def test_mirror_restores_normal_screen_after_altscreen():
    """pyte's base Screen doesn't itself implement the alternate-screen
    buffer swap a real terminal does for private mode 1049/1047/47 -- left
    alone, a snapshot taken after a full-screen app quits shows whatever
    that app last drew, not what was underneath (regression: a screenshot
    taken after quitting `less` showed less's content, not the shell)."""
    m = ScreenMirror(24, 80)
    m.feed(b"shell prompt$ ")
    assert "shell prompt$" in m.snapshot()
    m.feed(b"\x1b[?1049h")  # enter alt-screen (e.g. `less` starts)
    m.feed(b"\x1b[2J\x1b[Hpager content")
    assert "pager content" in m.snapshot()
    assert "shell prompt$" not in m.snapshot()
    m.feed(b"\x1b[?1049l")  # leave alt-screen (`less` quit)
    assert "shell prompt$" in m.snapshot()
    assert "pager content" not in m.snapshot()


def test_mirror_altscreen_starts_on_blank_canvas():
    """Entering alt-screen clears the buffer first, so the full-screen app's
    own content doesn't visually mix with whatever was on the normal screen
    a moment before."""
    m = ScreenMirror(24, 80)
    m.feed(b"leftover shell text")
    m.feed(b"\x1b[?1049h")
    assert "leftover shell text" not in m.snapshot()


# ---------------------------------------------------------------- session


def test_screenshot_returns_note(sh):
    """screenshot() itself is now published as its own new output on every
    live view (PLAN.md §6, verified at the wire-protocol level in
    test_kernel_protocol.py); at the unit level, just check its content."""
    from quahog.minutes import Note

    sh.run("echo on-the-screen")
    assert _wait(lambda: "on-the-screen" in sh._mirror.snapshot())
    note = sh.screenshot()
    assert isinstance(note, Note)
    assert "on-the-screen" in note.text


def test_altscreen_blocks_run(sh):
    """While a full-screen app owns the alt-screen, run() refuses."""
    # Emulate an app entering the alt-screen without needing a real TUI.
    sh.run("printf '\\033[?1049h'")
    assert _wait(lambda: sh.altscreen is True)
    with pytest.raises(RuntimeError, match="full-screen"):
        sh.run("echo nope")
    # Leaving the alt-screen re-enables run().
    sh.send("printf '\\033[?1049l'\r")
    assert _wait(lambda: sh.altscreen is False)
    assert sh.run("echo back").text.strip() == "back"


def test_screenshot_without_pyte_errors(sh, monkeypatch):
    monkeypatch.setattr(sh, "_mirror", None)
    with pytest.raises(RuntimeError, match="pyte"):
        sh.screenshot()


def test_altscreen_output_excluded_from_text(sh):
    """A full-screen app's cursor-addressed screen updates must not be fed
    to clean_text(): it has no notion of 2D positioning, so "cleaning" them
    produces unreadable run-on garbage rather than readable text (this is
    what happened before the fix — a vim session showed up as ~2000 chars of
    mashed-together status-bar/tilde/help text in the console log). Excluded
    content is replaced by a marker; h.screenshot() covers "what was on
    screen"."""
    before = sh.text
    sh.sendline("vim")
    assert _wait(lambda: sh.altscreen is True)
    sh.sendline(":q!")
    assert _wait(lambda: sh.altscreen is False)
    added = sh.text[len(before) :]
    assert "[full-screen app exited" in added
    # None of vim's actual screen content (status line, tildes, help text)
    # leaked into the log.
    assert "VIM - Vi IMproved" not in added
    assert "~" * 5 not in added
