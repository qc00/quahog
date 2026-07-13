import logging
from typing import Any, Dict, List, Optional, Protocol, TYPE_CHECKING

from .. import utils

if TYPE_CHECKING:
    from importlib.metadata import EntryPoint

    from ..session import Session

logger = logging.getLogger(__name__)
log_exception_min = utils.LogExceptionMinimal(logger.debug)


class Interceptor(Protocol):
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

    def match(self, argv: List[str], session: "Session") -> bool: ...


class Ctx:
    """Per-command context handed to every hook of a matched interceptor."""

    def __init__(self, session: "Session", argv: List[str], command: str) -> None:
        self.session = session
        self.argv = list(argv)
        self.command = command
        self.state: Dict[str, Any] = {}
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


_registry: Optional[List[Interceptor]] = None


def all_interceptors() -> List[Interceptor]:
    """Shipped interceptors plus everything on the entry-point group."""
    global _registry
    if _registry is None:
        from .builtins import BUILTINS

        _registry = [cls() for cls in BUILTINS]
        for ep in _entry_points():
            with log_exception_min:
                obj = ep.load()
                _registry.append(obj() if isinstance(obj, type) else obj)
    return _registry


def register(interceptor: Interceptor) -> None:
    """Add an interceptor for this kernel's lifetime (entry points are the
    durable mechanism)."""
    all_interceptors().append(interceptor)


def _entry_points() -> List["EntryPoint"]:
    try:
        from importlib.metadata import entry_points

        return list(entry_points(group="quahog.interceptors"))
    except Exception:
        log_exception_min()
        return []
