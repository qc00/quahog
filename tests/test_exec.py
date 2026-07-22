import shutil
import sys

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="unix PTY only"),
    pytest.mark.skipif(shutil.which("perl") is None, reason="exec filter needs perl on the host"),
]


def test_exec_captures_separate_stdout_and_stderr(sh):
    es = sh.exec("echo hello-exec; echo to-err 1>&2; exit 5")
    assert es.wait(15) == 5
    assert es.returncode == 5
    assert es.ok is False
    assert "hello-exec" in es.stdout
    assert "to-err" in es.stderr
    assert "to-err" not in es.stdout
    assert "hello-exec" not in es.stderr


def test_exec_output_is_raw_not_stripped(sh):
    # No far-end escape-stripping any more: base64 carries the bytes exactly.
    es = sh.exec(r"printf '\033[31mRED\033[0m\n'")
    es.wait(15)
    assert "\x1b[31m" in es.stdout


def test_exec_binary_output_intact(sh):
    es = sh.exec(r"printf '\000\001\377ok'")
    es.wait(15)
    assert es.stdout_raw == b"\x00\x01\xffok"


def test_exec_has_no_tty(sh):
    # __qua_exec now runs the command over plain pipes (IPC::Open3), not a pty.
    es = sh.exec("test -t 1 || echo NOTATTY")
    es.wait(15)
    assert "NOTATTY" in es.stdout


def test_exec_stdin_gets_eof(sh):
    # open3 closes the child's stdin immediately -- there is no stdin path.
    es = sh.exec("cat; echo done-$?")
    es.wait(15)
    assert "done-0" in es.stdout


def test_exec_does_not_pollute_parent_console(sh):
    before = len(sh.text)
    # The output string is *computed*, so it appears nowhere in the (echoed)
    # command line — only genuine leakage of exec output would surface it.
    es = sh.exec("echo val-$((6 * 7))")
    es.wait(15)
    assert es.stdout.strip() == "val-42"
    # Default mirror=False: exec output is its own; nothing leaks into the
    # session's console text.
    assert "val-42" not in sh.text[before:]


def test_exec_mirror_folds_into_parent(sh):
    es = sh.exec("echo mirrored-output", mirror=True)
    es.wait(15)
    assert "mirrored-output" in sh.text


def test_exec_does_not_block_session(sh):
    es = sh.exec("sleep 0.6; echo bg-late")
    # The session stays usable while the exec runs concurrently.
    r = sh.run("echo meanwhile")
    assert r.text.strip() == "meanwhile"
    assert es.wait(15) == 0
    assert "bg-late" in es.stdout


def test_exec_back_to_back(sh):
    for i in range(3):
        es = sh.exec(f"echo run-{i}")
        assert es.wait(15) == 0
        assert f"run-{i}" in es.stdout


def test_exec_large_stderr_does_not_hang(sh):
    # IO::Select on the far end must drain whichever pipe is ready rather
    # than blocking on one while the other fills up.
    cmd = "head -c 200000 /dev/zero | tr '\\0' 'e' 1>&2; echo done-out"
    es = sh.exec(cmd)
    assert es.wait(20) == 0
    assert len(es.stderr_raw) == 200000
    assert "done-out" in es.stdout


def test_exec_display_repr(sh):
    es = sh.exec("echo shown; exit 2")
    es.wait(15)
    plain = es._repr_mimebundle_()["text/plain"]
    assert "# exec" in plain
    assert "shown" in plain
    assert "[exit 2]" in plain
    js = es._repr_mimebundle_()["application/vnd.quahog.output+json"]
    assert js["exec"] is True and js["returncode"] == 2
