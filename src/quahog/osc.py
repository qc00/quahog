"""Incremental parser splitting a PTY byte stream into data spans and OSC events.

The tap feeds every chunk read from the PTY through a StreamParser. The parser
never alters the byte stream (the widget receives the original bytes); it just
classifies them, so the session can capture command output between the
OSC 133;C and OSC 133;D markers and exclude the markers themselves.

Only OSC sequences (ESC ] ... BEL | ESC \\) are lifted out as events. CSI/SGR
color sequences etc. stay inside "data" spans on purpose: they belong to
CommandResult.raw.
"""

from __future__ import annotations

from typing import List, Tuple, Union

# A token is either ("data", bytes) or ("osc", number_str, payload_str).
Token = Union[Tuple[str, bytes], Tuple[str, str, str]]

_ESC = 0x1B
_BEL = 0x07
# An unterminated "OSC" longer than this is assumed to be garbage, not a
# sequence split across reads, and is passed through as data.
_MAX_OSC = 8192


class StreamParser:
    def __init__(self) -> None:
        self._pend = b""

    def feed(self, chunk: bytes) -> List[Token]:
        buf = self._pend + chunk
        self._pend = b""
        out: List[Token] = []
        pos = 0
        n = len(buf)

        def data(a: int, b: int) -> None:
            if b > a:
                out.append(("data", buf[a:b]))

        span = pos
        while pos < n:
            i = buf.find(b"\x1b", pos)
            if i == -1:
                data(span, n)
                break
            if i + 1 >= n:
                # Lone ESC at the very end: might be the start of an OSC split
                # across reads.
                data(span, i)
                self._pend = buf[i:]
                break
            if buf[i + 1] != ord("]"):
                # Some other escape sequence (CSI etc.): stays in the current
                # data span; keep scanning past it.
                pos = i + 1
                continue

            data(span, i)
            # Scan for the terminator: BEL or ST (ESC \).
            j = i + 2
            end = -1
            term = 0
            while j < n:
                c = buf[j]
                if c == _BEL:
                    end, term = j, 1
                    break
                if c == _ESC:
                    if j + 1 < n:
                        if buf[j + 1] == ord("\\"):
                            end, term = j, 2
                        else:
                            j += 1
                            continue
                    break  # ESC is the last byte: unterminated for now
                j += 1

            if end == -1:
                if n - i <= _MAX_OSC:
                    self._pend = buf[i:]
                else:
                    data(i, n)  # runaway; give up and pass through
                pos = n
                break

            payload = buf[i + 2 : end].decode("utf-8", "replace")
            num, _, rest = payload.partition(";")
            out.append(("osc", num, rest))
            pos = end + term
            span = pos

        return out
