import pathlib

import anywidget
import traitlets

_STATIC = pathlib.Path(__file__).parent / "static"


class ConsoleView(anywidget.AnyWidget):
    _esm = _STATIC / "index.js"
    _css = _STATIC / "widget.css"

    session_name = traitlets.Unicode("").tag(sync=True)
