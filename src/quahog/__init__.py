"""quahog — interactive console sessions, captured in Jupyter notebooks.

    import quahog as q
    h = q.bash()          # spawn; display(h) embeds the live console
    r = h.run("ls -la")   # programmatic command; r.text / r.raw / r.returncode
    %qua make test        # magic sugar over run() on the default session
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import interceptors  # noqa: F401
from . import utils
from .fork import ForkSession  # noqa: F401
from .copy import DownloadBox  # noqa: F401
from .minutes import Note  # noqa: F401
from .sub_sessions import CommandResult, ExecSession, MultiResult, clean_text  # noqa: F401
from .session import (  # noqa: F401
    LAST_DUMP,
    Minute,
    Session,
    TimeoutExpired,
    spawn_bash,
    spawn_zsh,
)

logger = logging.getLogger(__name__)
log_exception_min = utils.LogExceptionMinimal(logger.debug)

__version__ = "0.4.0"
__all__ = [
    "bash",
    "zsh",
    "sessions",
    "default",
    "Session",
    "CommandResult",
    "MultiResult",
    "ForkSession",
    "ExecSession",
    "DownloadBox",
    "Minute",
    "Note",
    "LAST_DUMP",
    "TimeoutExpired",
    "interceptors",
]


class _Registry(dict):
    """Name -> Session. Shown as a small table in notebooks."""

    def _repr_mimebundle_(self, include: Any = None, exclude: Any = None) -> Dict[str, str]:
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
    env: Optional[Dict[str, str]] = None,
    name: Optional[str] = None,
    inherit_rc: bool = True,
    record: bool = False,
) -> Session:
    """Spawn a local interactive bash with quahog shell integration."""
    return _register(spawn_bash(name or _next_name("bash"), cwd=cwd, env=env, inherit_rc=inherit_rc, record=record))


def zsh(
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    name: Optional[str] = None,
    inherit_rc: bool = True,
    record: bool = False,
) -> Session:
    """Spawn a local interactive zsh with quahog shell integration."""
    return _register(spawn_zsh(name or _next_name("zsh"), cwd=cwd, env=env, inherit_rc=inherit_rc, record=record))


def load_ipython_extension(ip: Any) -> None:
    from .magics import load_ipython_extension as _load

    _load(ip)


def _auto_register_magics() -> None:
    with log_exception_min:
        from IPython import get_ipython

        ip = get_ipython()
        if ip is not None:
            load_ipython_extension(ip)


_auto_register_magics()
