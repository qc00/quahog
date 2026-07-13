import sys
from typing import Any, Protocol, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType


class _LoggerMethod(Protocol):
    def __call__(self, msg: str, *args: Any, **kwargs: Any) -> None: ...


class LogExceptionMinimal:
    """Context manager that logs any exception raised inside it, then swallow.

    Instances can also be called directly as a function."""

    def __init__(self, logger_method: _LoggerMethod, *, extra_message: str = ""):
        self.logger_method = logger_method
        self.extra_message = extra_message

    def __enter__(self) -> "LogExceptionMinimal":
        return self

    def log_exc(
        self,
        exc_type: "type[BaseException] | None",
        exc_value: "BaseException | None",
        trace: "TracebackType | None",
        override_logger: _LoggerMethod | None = None,
        override_msg: str = "",
    ) -> None:
        frame: Tuple[str, int, str] = ("unknown", 0, "file")
        if trace is not None:
            while trace.tb_next is not None:
                trace = trace.tb_next
            frame = (trace.tb_frame.f_code.co_filename, trace.tb_lineno, trace.tb_frame.f_code.co_name)
        (override_logger or self.logger_method)(
            "%s: %s at %s:%d (%s). %s",
            exc_type.__name__ if exc_type else "??",
            exc_value,
            frame[0],
            frame[1],
            frame[2],
            override_msg or self.extra_message,
        )

    def __call__(
        self, e: Exception | None = None, *, override_logger: _LoggerMethod | None = None, override_msg: str = ""
    ) -> None:
        if e is None:
            exc_type, exc_value, trace = sys.exc_info()
        else:
            exc_type, exc_value, trace = type(e), e, e.__traceback__
        self.log_exc(exc_type, exc_value, trace, override_logger=override_logger, override_msg=override_msg)

    def __exit__(
        self,
        exc_type: "type[BaseException] | None",
        exc_value: "BaseException | None",
        trace: "TracebackType | None",
    ) -> bool:
        if exc_type or exc_value:
            self.log_exc(exc_type, exc_value, trace)
        return True
