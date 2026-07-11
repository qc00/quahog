"""Shipped interceptors (PLAN.md §6): editor diffs, pagers, password hygiene."""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path


def _base(argv0: str) -> str:
    return argv0.rsplit("/", 1)[-1]


class EditorDiffInterceptor:
    """``vim``/``nano``/``vi <file>``: snapshot before, unified diff as cell
    output after.

    Local sessions read the file straight from disk (resolved against the
    session's cwd from OSC 7); the injected ``__qua_snapshot`` helper is what
    makes the same trick work remotely (milestone 4).
    """

    EDITORS = {"vim", "vi", "nvim", "nano"}

    def match(self, argv, session) -> bool:
        return _base(argv[0]) in self.EDITORS and self._target(argv) is not None

    def before(self, ctx) -> None:
        path = self._resolve(ctx)
        ctx.state["path"] = path
        ctx.state["before"] = self._read(path)

    def after(self, ctx):
        path = ctx.state.get("path")
        if path is None:
            return None
        before = ctx.state.get("before", "")
        after = self._read(path)
        if before == after:
            return None
        diff = difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"{path.name} (before)",
            tofile=f"{path.name} (after)",
            lineterm="",
        )
        return "\n".join(diff)

    @staticmethod
    def _target(argv):
        for arg in reversed(argv[1:]):
            if not arg.startswith("-") and not arg.startswith("+"):
                return arg
        return None

    def _resolve(self, ctx) -> Path:
        p = Path(os.path.expanduser(self._target(ctx.argv)))
        if not p.is_absolute():
            p = Path(ctx.session.cwd or os.getcwd()) / p
        return p

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(errors="replace")
        except OSError:
            return ""  # not there yet — a freshly created file diffs from ""


class PagerInterceptor:
    """``less``/``man``/``more``: deliberately no cell effect — the screenshot
    button covers "what did I look at"."""

    PAGERS = {"less", "man", "more"}

    def match(self, argv, session) -> bool:
        return _base(argv[0]) in self.PAGERS

    def after(self, ctx):
        return None


class PasswordInterceptor:
    """``sudo``/``su``/``ssh``/``passwd``: pre-arm recording suppression for
    known password prompts, released when the prompt is answered.

    This is where the prompt-detection regexes live — scoped to a matched
    command instead of running globally, so suppression never surprises the
    user elsewhere. It covers *remote* prompts, which the local termios ECHO
    check cannot see.
    """

    COMMANDS = {"sudo", "su", "ssh", "passwd"}
    PROMPT_RE = re.compile(
        r"(?:password|passphrase|pin|passwort|mot de passe|contraseña|senha|密码|パスワード)"
        r"[^\n]{0,60}[::]\s*$",
        re.IGNORECASE,
    )

    def match(self, argv, session) -> bool:
        return _base(argv[0]) in self.COMMANDS

    def before(self, ctx) -> None:
        ctx.state["tail"] = ""
        ctx.state["armed"] = False

    def on_output(self, ctx, text: str) -> None:
        from ..result import clean_text

        tail = (ctx.state.get("tail", "") + clean_text(text))[-256:]
        ctx.state["tail"] = tail
        if not ctx.state.get("armed") and self.PROMPT_RE.search(tail.rstrip(" ")):
            ctx.suppress_input()
            ctx.state["armed"] = True

    def on_input(self, ctx, data) -> None:
        if isinstance(data, str):
            data = data.encode()
        if ctx.state.get("armed") and (b"\r" in data or b"\n" in data):
            # Enter answers the prompt: stop suppressing so the rest of the
            # (possibly long-lived, e.g. ssh) command records normally.
            ctx.release_input()
            ctx.state["armed"] = False
            ctx.state["tail"] = ""

    def after(self, ctx):
        if ctx.state.get("armed"):
            ctx.release_input()
            ctx.state["armed"] = False
        return None


BUILTINS = [EditorDiffInterceptor, PagerInterceptor, PasswordInterceptor]
