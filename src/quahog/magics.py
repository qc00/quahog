"""%qua / %%qua magics.

    %qua ls -la                    run in the default session
    %qua -s prod -t 300 make test  explicit session and timeout
    %qua -b tail -f app.log        don't wait (background); result completes later

    %%qua [session]
    step one
    step two

Interactively captured commands (minuting, milestone 2) will be written back
as %qua cells, so this is also the replay format.
"""

from __future__ import annotations

from IPython.core.magic import Magics, line_magic, cell_magic, magics_class

from .result import MultiResult


def _resolve_session(name):
    import quahog

    if name:
        try:
            return quahog.sessions[name]
        except KeyError:
            raise NameError(
                f"no session named {name!r}; have: {list(quahog.sessions) or 'none'}"
            ) from None
    if quahog.default is None or quahog.default._exited.is_set():
        quahog.bash()  # auto-start; %qua should just work
    return quahog.default


@magics_class
class QuahogMagics(Magics):
    def _parse(self, line):
        opts, arg = self.parse_options(line, "s:t:b", mode="string")
        session = _resolve_session(opts.get("s"))
        timeout = float(opts["t"]) if "t" in opts else None
        wait = "b" not in opts
        return session, timeout, wait, arg

    @line_magic("qua")
    def qua(self, line):
        session, timeout, wait, command = self._parse(line)
        if not command.strip():
            return session
        return session.run(command, wait=wait, timeout=timeout)

    @cell_magic("qua")
    def qua_cell(self, line, cell):
        session, timeout, wait, _ = self._parse(line)
        results = []
        for raw in cell.splitlines():
            command = raw.strip()
            if not command or command.startswith("#"):
                continue
            results.append(session.run(command, wait=wait, timeout=timeout))
        return MultiResult(results)


def load_ipython_extension(ip) -> None:
    ip.register_magics(QuahogMagics(ip))
