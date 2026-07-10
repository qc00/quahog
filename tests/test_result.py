from quahog.result import clean_text, CommandResult


def test_strip_colors():
    assert clean_text("\x1b[31mred\x1b[0m\n") == "red\n"


def test_crlf():
    assert clean_text("a\r\nb\r\n") == "a\nb\n"


def test_carriage_return_overwrite():
    assert clean_text("12%\r45%\r100%\n") == "100%\n"
    # A shorter segment overlays only its own width, like a real terminal.
    assert clean_text("progress\rdone\n") == "doneress\n"


def test_overlay_shorter_segment():
    # A shorter segment overwrites only its own width, like a real terminal.
    assert clean_text("abcdef\rXY\n") == "XYcdef\n"


def test_plain_repr_includes_command_and_exit():
    r = CommandResult("bash1", "false")
    r._buf += b""
    r._finish(1)
    assert "$ false" in repr(r)
    assert "[exit 1]" in repr(r)


def test_mimebundle_has_text_plain_and_raw():
    r = CommandResult("bash1", "echo hi")
    r._buf += b"\x1b[32mhi\x1b[0m\r\n"
    r._finish(0)
    bundle = r._repr_mimebundle_()
    assert "hi" in bundle["text/plain"]
    payload = bundle["application/vnd.quahog.output+json"]
    assert payload["returncode"] == 0
    assert "\x1b[32m" in payload["raw"]
    assert r.text == "hi\n"
