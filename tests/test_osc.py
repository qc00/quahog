from quahog.osc import StreamParser


def feed_all(chunks):
    p = StreamParser()
    out = []
    for c in chunks:
        out.extend(p.feed(c))
    return out


def test_plain_data_passthrough():
    assert feed_all([b"hello world"]) == [("data", b"hello world")]


def test_osc_133_bel():
    toks = feed_all([b"before\x1b]133;D;0\x07after"])
    assert toks == [("data", b"before"), ("osc", "133", "D;0"), ("data", b"after")]


def test_osc_st_terminator():
    toks = feed_all([b"\x1b]133;A\x1b\\x"])
    assert toks == [("osc", "133", "A"), ("data", b"x")]


def test_split_across_chunks():
    toks = feed_all([b"ab\x1b]13", b"3;C\x07cd"])
    assert toks == [("data", b"ab"), ("osc", "133", "C"), ("data", b"cd")]


def test_split_at_esc():
    toks = feed_all([b"ab\x1b", b"]7;file://h/tmp\x07"])
    assert toks == [("data", b"ab"), ("osc", "7", "file://h/tmp")]


def test_csi_stays_data():
    toks = feed_all([b"\x1b[31mred\x1b[0m"])
    assert toks == [("data", b"\x1b[31mred\x1b[0m")]


def test_runaway_osc_passes_through():
    # Past _MAX_OSC an unterminated OSC is assumed to be garbage, not a
    # sequence split across reads, and is passed through as data. exec()'s
    # base64 chunks stay well under the cap (4KB reads inflate to ~5.5KB).
    big = b"\x1b]" + b"x" * 250_000
    toks = feed_all([big])
    assert toks == [("data", big)]
