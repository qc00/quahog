"""quahog — interactive console sessions, captured in Jupyter notebooks.

    import quahog as q
    h = q.bash()          # spawn; display(h) embeds the live console
    r = h.run("ls -la")   # programmatic command; r.text / r.raw / r.returncode
    %qua make test        # magic sugar over run() on the default session
"""

from __future__ import annotations

from typing import Optional

from . import interceptors  # noqa: F401
from .fork import ForkHandle  # noqa: F401
from .minutes import Note  # noqa: F401
from .result import CommandResult, MultiResult, clean_text  # noqa: F401
from .session import (  # noqa: F401
    LAST_DUMP,
    Minute,
    Session,
    TimeoutExpired,
    spawn_bash,
    spawn_zsh,
)

__version__ = "0.4.0"
__all__ = [
    "bash",
    "zsh",
    "sessions",
    "default",
    "Session",
    "CommandResult",
    "MultiResult",
    "ForkHandle",
    "Minute",
    "Note",
    "LAST_DUMP",
    "TimeoutExpired",
    "interceptors",
]


class _Registry(dict):
    """Name -> Session. Shown as a small table in notebooks."""

    def _repr_mimebundle_(self, include=None, exclude=None):
        lines = [f"{name}: {s!r}" for name, s in self.items()] or ["(no sessions)"]
        return {"text/plain": "\n".join(lines)}


sessions: _Registry = _Registry()
default: Optional[Session] = None


def _register(session: Session) -> Session:
    global default
    sessions[session.name] = session
    default = session
    return session


def _next_name(base: str) -> str:
    i = 1
    while f"{base}{i}" in sessions:
        i += 1
    return f"{base}{i}"


def bash(
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    name: Optional[str] = None,
    inherit_rc: bool = True,
    record: bool = False,
) -> Session:
    """Spawn a local interactive bash with quahog shell integration."""
    return _register(
        spawn_bash(name or _next_name("bash"), cwd=cwd, env=env, inherit_rc=inherit_rc, record=record)
    )


def zsh(
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    name: Optional[str] = None,
    inherit_rc: bool = True,
    record: bool = False,
) -> Session:
    """Spawn a local interactive zsh with quahog shell integration."""
    return _register(
        spawn_zsh(name or _next_name("zsh"), cwd=cwd, env=env, inherit_rc=inherit_rc, record=record)
    )


def load_ipython_extension(ip) -> None:
    from .magics import load_ipython_extension as _load

    _load(ip)


def _auto_register_magics() -> None:
    try:
        from IPython import get_ipython

        ip = get_ipython()
        if ip is not None:
            load_ipython_extension(ip)
    except Exception:
        pass


_auto_register_magics()
