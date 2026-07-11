import pathlib

import anywidget
import traitlets

_STATIC = pathlib.Path(__file__).parent / "static"


class ConsoleView(anywidget.AnyWidget):
    _esm = _STATIC / "index.js"
    _css = _STATIC / "widget.css"

    session_name = traitlets.Unicode("").tag(sync=True)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._text = ""

    def __repr__(self) -> str:
        return self._text or f"<quahog console: {self.session_name}>"
