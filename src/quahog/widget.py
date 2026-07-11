import pathlib

import anywidget
import traitlets

_STATIC = pathlib.Path(__file__).parent / "static"


class ConsoleView(anywidget.AnyWidget):
    _esm = _STATIC / "index.js"
    _css = _STATIC / "widget.css"

    session_name = traitlets.Unicode("").tag(sync=True)
    # Hop support: when the session is displayed elsewhere, this view freezes
    # into a static snapshot (SerializeAddon HTML) and stays that way.
    frozen = traitlets.Bool(False).tag(sync=True)
    frozen_html = traitlets.Unicode("").tag(sync=True)
