"""Interceptor API (PLAN.md §6): plugins that match known commands and act
around them.

An interceptor is any object with::

    match(argv, session) -> bool      # required: does this command concern me?
    before(ctx)                       # optional: the command is starting
    on_output(ctx, text)              # optional: an output chunk during the command
    on_input(ctx, data)               # optional: keystrokes during the command
    after(ctx) -> str | None          # optional: command finished; a returned
                                      # string is appended to the command's
                                      # cell output (e.g. a vim diff)

``ctx`` exposes the same recorder controls as the widget toolbar — and that is
the whole password story: a password interceptor is just a hook that suppresses
input recording, released in ``after()`` or when the prompt is answered. There
is no dedicated password API.

Third-party interceptors ship via the ``quahog.interceptors`` entry-point group
(a class or an instance); ``register()`` adds one for this kernel's lifetime.
"""

from __future__ import annotations

from typing import Any, List, Optional


class Ctx:
    """Per-command context handed to every hook of a matched interceptor."""

    def __init__(self, session, argv: List[str], command: str) -> None:
        self.session = session
        self.argv = list(argv)
        self.command = command
        self.state: dict = {}
        self._holds = 0

    # -- recorder controls (the same surface as the toolbar) ---------------
    def suppress_input(self) -> None:
        """Record subsequent keystrokes as an ``[input suppressed]`` placeholder."""
        self.session._recorder.suppress()
        self._holds += 1

    def release_input(self) -> None:
        if self._holds > 0:
            self.session._recorder.release()
            self._holds -= 1

    def pause_recording(self) -> None:
        self.session._recorder.set_enabled(False)

    def resume_recording(self) -> None:
        self.session._recorder.set_enabled(True)

    def _release_all(self) -> None:
        """Session calls this when the command ends: leaked holds are bugs."""
        while self._holds > 0:
            self.release_input()


_registry: Optional[List[Any]] = None


def all_interceptors() -> List[Any]:
    """Shipped interceptors plus everything on the entry-point group."""
    global _registry
    if _registry is None:
        from .builtins import BUILTINS

        _registry = [cls() for cls in BUILTINS]
        for ep in _entry_points():
            try:
                obj = ep.load()
                _registry.append(obj() if isinstance(obj, type) else obj)
            except Exception:
                continue
    return _registry


def register(interceptor: Any) -> None:
    """Add an interceptor for this kernel's lifetime (entry points are the
    durable mechanism)."""
    all_interceptors().append(interceptor)


def _entry_points():
    try:
        from importlib.metadata import entry_points

        return list(entry_points(group="quahog.interceptors"))
    except Exception:
        return []
