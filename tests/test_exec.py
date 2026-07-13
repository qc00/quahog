"""exec(): a command as its own object over the session's own PTY (PLAN.md §3).

These exercise the OSC 2607 O/X demux against real bash + socat: escape-stripped
tagged output, exit codes, tty preservation, background interleaving, stdin
ownership, and the mirror flag.
"""

import shutil
import sys

import pytest

import quahog

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only"),
    pytest.mark.skipif(shutil.which("socat") is None, reason="exec needs socat on the host"),
    pytest.mark.skipif(shutil.which("perl") is None, reason="exec filter needs perl on the host"),
]


@pytest.fixture()
def sh():
    s = quahog.bash(inherit_rc=False)
    yield s
    s.close()
    quahog.sessions.pop(s.name, None)


def test_exec_captures_output_and_exit(sh):
    es = sh.exec("echo hello-exec; echo to-err 1>&2; exit 5")
    assert es.wait(15) == 5
    assert es.returncode == 5
    assert es.ok is False
    assert "hello-exec" in es.text
    assert "to-err" in es.text  # a PTY merges stdout+stderr


def test_exec_strips_escape_sequences(sh):
    # The far-end filter strips ANSI so the handle holds clean text (PLAN.md §3).
    es = sh.exec(r"printf '\033[31mRED\033[0m and \033]0;title\007plain\n'")
    es.wait(15)
    assert es.text.strip() == "RED and plain"
    assert "\x1b" not in es.text


def test_exec_is_a_real_tty(sh):
    es = sh.exec("test -t 1 && echo ISATTY")
    es.wait(15)
    assert "ISATTY" in es.text


def test_exec_does_not_pollute_parent_console(sh):
    before = len(sh.text)
    # The output string is *computed*, so it appears nowhere in the (echoed)
    # command line — only genuine leakage of exec output would surface it.
    es = sh.exec("echo val-$((6 * 7))")
    es.wait(15)
    assert es.text.strip() == "val-42"
    # Default mirror=False: exec output is its own; nothing leaks into the
    # session's console text.
    assert "val-42" not in sh.text[before:]


def test_exec_mirror_folds_into_parent(sh):
    es = sh.exec("echo mirrored-output", mirror=True)
    es.wait(15)
    assert "mirrored-output" in sh.text


def test_exec_background_does_not_block_session(sh):
    es = sh.exec("sleep 0.6; echo bg-late", background=True)
    # The session stays usable while the backgrounded exec runs.
    r = sh.run("echo meanwhile")
    assert r.text.strip() == "meanwhile"
    assert es.wait(15) == 0
    assert "bg-late" in es.text


def test_exec_foreground_owns_stdin(sh):
    es = sh.exec("read x; echo got:$x")
    # session.sendline is refused while a foreground exec owns stdin...
    with pytest.raises(RuntimeError, match="foreground exec"):
        sh.sendline("via-session")
    # ...feed it via the handle instead.
    es.stdin.write("via-handle\n")
    es.wait(15)
    assert "got:via-handle" in es.text
    # Once it finishes, the session is free again.
    assert sh.run("echo free-again").text.strip() == "free-again"


def test_exec_background_cannot_feed_stdin(sh):
    es = sh.exec("sleep 0.3; echo done", background=True)
    with pytest.raises(RuntimeError, match="controlling tty"):
        es.stdin.write("nope\n")
    es.wait(15)


def test_exec_back_to_back(sh):
    for i in range(3):
        es = sh.exec(f"echo run-{i}")
        assert es.wait(15) == 0
        assert f"run-{i}" in es.text


def test_exec_display_repr(sh):
    es = sh.exec("echo shown; exit 2")
    es.wait(15)
    plain = es._repr_mimebundle_()["text/plain"]
    assert "# exec" in plain
    assert "shown" in plain
    assert "[exit 2]" in plain
    js = es._repr_mimebundle_()["application/vnd.quahog.output+json"]
    assert js["exec"] is True and js["returncode"] == 2
