"""Colours, fonts and the Fusion palette — light and dark.

The Qt port of ``lcsc/theme.py``. The palette *names* carry meaning and are not
interchangeable; they are copied across deliberately, because the distinctions
were arrived at by looking at real screens:

``ok`` / ``low`` / ``bad`` / ``unknown``
    How healthy a stock figure is. ``unknown`` is "nobody answered" and ``bad``
    is "confirmed none" — conflating those two shows in-stock parts as dead.

``jlc`` / ``retail``
    Which inventory a number came from. JLC assembly and LCSC retail are
    separate warehouses that routinely disagree by orders of magnitude, so they
    keep distinct hues everywhere they appear.

``standard``
    The advisory amber for a part that pushes the board into Standard-mode
    pricing. Deliberately *not* ``bad``: nothing is broken, it just costs more.
    Those two shared a red once, which made a pricing note indistinguishable
    from a failure.

Two things differ from the wx original, both on purpose.

**The style is forced to Fusion.** wxWidgets wraps native controls, so a layout
tuned on macOS is wrong on Windows and no amount of better code fixes it.
Fusion draws its own widgets identically on both, which is what makes a
screenshot taken here evidence about Windows — the reason for the migration.

**The appearance is explicit, not sniffed.** :func:`resolve_mode` reads
``LCSC_THEME`` before asking the platform, so a screenshot is reproducible.
``qt_probe.py`` renders both, and CI diffs both.
"""

from __future__ import annotations

from enum import Enum
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

#: (light, dark) RGB pairs, lifted from ``lcsc/theme.py`` unchanged. The dark
#: variants are desaturated and raised in luminance; saturated hues vibrate
#: against dark greys.
_PALETTE: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    # Status
    "ok": ((14, 116, 60), (94, 214, 138)),
    "low": ((140, 92, 0), (240, 190, 90)),
    "bad": ((176, 20, 40), (255, 130, 130)),
    "unknown": ((130, 130, 130), (140, 140, 148)),
    # Inventory identity
    "jlc": ((0, 105, 122), (90, 200, 220)),
    "retail": ((106, 58, 178), (198, 158, 255)),
    # Cost advisory
    "standard": ((166, 90, 12), (240, 160, 96)),
    # Chrome
    "muted": ((110, 110, 116), (150, 150, 158)),
    "rule": ((208, 208, 214), (72, 72, 80)),
}

#: Window/base/text colours for the two Fusion palettes. Stated rather than
#: inherited so both platforms and both appearances land on the same pixels.
_CHROME = {
    "light": {
        "window": (240, 240, 240),
        "base": (255, 255, 255),
        "alternate": (247, 247, 249),
        "text": (26, 26, 28),
        "disabled": (150, 150, 152),
        "highlight": (48, 116, 202),
        "highlight_text": (255, 255, 255),
        "tooltip": (255, 255, 225),
        "card": (232, 232, 234),
    },
    "dark": {
        "window": (48, 49, 53),
        "base": (36, 37, 40),
        "alternate": (43, 44, 48),
        "text": (228, 228, 232),
        "disabled": (128, 128, 132),
        "highlight": (74, 144, 226),
        "highlight_text": (16, 16, 18),
        "tooltip": (58, 59, 63),
        "card": (58, 59, 64),
    },
}

#: Below this many pieces a part is "low" rather than "in stock".
LOW_STOCK_THRESHOLD = 100

#: Base UI point size. Explicit, because Fusion inherits the platform default
#: otherwise and macOS and Windows disagree — which would make the screenshots
#: differ for a reason that has nothing to do with the layout.
BASE_FONT_POINT_SIZE = 10
MONO_FONT_POINT_SIZE = 9


class Mode(str, Enum):
    """Which appearance to render."""

    LIGHT = "light"
    DARK = "dark"


_mode = Mode.LIGHT


def resolve_mode(requested: str | None = None) -> Mode:
    """Decide which appearance to use.

    ``LCSC_THEME=dark`` wins over the platform, so a probe run is reproducible
    on a machine whose desktop is set the other way.
    """
    candidate = (requested or os.environ.get("LCSC_THEME") or "").strip().lower()
    if candidate in ("light", "dark"):
        return Mode(candidate)
    app = QApplication.instance()
    if app is not None:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return Mode.DARK
    return Mode.LIGHT


def mode() -> Mode:
    """Return the appearance currently applied."""
    return _mode


def is_dark() -> bool:
    """Report whether the dark appearance is in force."""
    return _mode is Mode.DARK


def colour(name: str) -> QColor:
    """Return the palette entry ``name`` for the current appearance."""
    light, dark = _PALETTE.get(name, _PALETTE["muted"])
    return QColor(*(dark if is_dark() else light))


def chrome(name: str) -> QColor:
    """Return one of the window/base/text chrome colours."""
    return QColor(*_CHROME[_mode.value][name])


def stock_state(count) -> str:
    """Classify a stock figure into a palette status name.

    ``None`` means "not fetched, or the endpoint did not answer", which is a
    different fact from a confirmed zero and must not share its colour.
    """
    if count is None or count == "":
        return "unknown"
    try:
        value = int(count)
    except (TypeError, ValueError):
        return "unknown"
    if value <= 0:
        return "bad"
    if value < LOW_STOCK_THRESHOLD:
        return "low"
    return "ok"


def stock_colour(count) -> QColor:
    """Return the colour a stock figure should be drawn in."""
    return colour(stock_state(count))


def blend(first: QColor, second: QColor, ratio: float) -> QColor:
    """Mix two colours, ``ratio`` being the weight of ``second``."""
    ratio = max(0.0, min(1.0, ratio))
    return QColor(
        int(first.red() + (second.red() - first.red()) * ratio),
        int(first.green() + (second.green() - first.green()) * ratio),
        int(first.blue() + (second.blue() - first.blue()) * ratio),
    )


def card_background() -> QColor:
    """Background for a raised panel, nudged away from the window colour."""
    return chrome("card")


def unassigned_colour() -> QColor:
    """Row colour for a BOM part with no LCSC number.

    The one actionable failure the part list can show: the part is going into
    the BOM and JLC has nothing to place. Parts *excluded* from the BOM
    (mounting holes, fiducials, test points) are fine without a number and are
    never marked.
    """
    return colour("bad")


def standard_trigger_colour() -> QColor:
    """Row colour for parts that push the board into Standard-mode pricing."""
    return colour("standard")


def base_font() -> QFont:
    """Return the application font."""
    font = QFont()
    font.setPointSize(BASE_FONT_POINT_SIZE)
    return font


def bold(font: QFont) -> QFont:
    """Return ``font`` in bold, without mutating the original."""
    out = QFont(font)
    out.setBold(True)
    return out


def scaled(font: QFont, factor: float) -> QFont:
    """Return ``font`` resized by ``factor``."""
    out = QFont(font)
    out.setPointSize(max(7, int(round(font.pointSize() * factor))))
    return out


def mono_font() -> QFont:
    """Return a fixed-pitch font for the log pane."""
    font = QFont()
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFamilies(["Menlo", "Consolas", "DejaVu Sans Mono", "Courier New"])
    font.setPointSize(MONO_FONT_POINT_SIZE)
    return font


def build_palette(target: Mode) -> QPalette:
    """Build the Fusion palette for ``target``."""
    values = _CHROME[target.value]
    palette = QPalette()

    def put(role: QPalette.ColorRole, key: str) -> None:
        palette.setColor(role, QColor(*values[key]))

    put(QPalette.ColorRole.Window, "window")
    put(QPalette.ColorRole.WindowText, "text")
    put(QPalette.ColorRole.Base, "base")
    put(QPalette.ColorRole.AlternateBase, "alternate")
    put(QPalette.ColorRole.ToolTipBase, "tooltip")
    put(QPalette.ColorRole.ToolTipText, "text")
    put(QPalette.ColorRole.Text, "text")
    put(QPalette.ColorRole.Button, "window")
    put(QPalette.ColorRole.ButtonText, "text")
    put(QPalette.ColorRole.Highlight, "highlight")
    put(QPalette.ColorRole.HighlightedText, "highlight_text")
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 80, 80))
    palette.setColor(
        QPalette.ColorRole.Link,
        QColor(*_PALETTE["jlc"][1 if target is Mode.DARK else 0]),
    )

    disabled = QColor(*values["disabled"])
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.HighlightedText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return palette


def stylesheet() -> str:
    """App-wide stylesheet: the handful of things a palette cannot express."""
    rule = colour("rule").name()
    muted = colour("muted").name()
    card = card_background().name()
    return f"""
    QToolBar {{ border: 0; padding: 2px; spacing: 2px; }}
    QToolBar::separator {{
        background: {rule};
        width: 1px;
        margin: 4px 6px;
    }}
    QToolButton {{
        padding: 3px 6px;
        border: 1px solid transparent;
        border-radius: 4px;
    }}
    QToolButton:hover {{ border-color: {rule}; }}
    QToolButton:checked {{ background: {card}; border-color: {rule}; }}
    QHeaderView::section {{
        padding: 4px 6px;
        border: 0;
        border-right: 1px solid {rule};
        border-bottom: 1px solid {rule};
    }}
    QLabel[role="status"] {{ color: {muted}; }}
    QLabel[role="section"] {{ font-weight: 600; }}
    QFrame[role="card"] {{
        background: {card};
        border: 1px solid {rule};
        border-radius: 5px;
    }}
    """


def apply(app: QApplication, requested: str | None = None) -> Mode:
    """Force Fusion, install the palette and set the base font.

    Returns the mode actually applied, so a caller (the probe) can name its
    output file after it.
    """
    global _mode  # noqa: PLW0603 - one process-wide appearance, by design
    app.setStyle("Fusion")
    _mode = resolve_mode(requested)
    app.setPalette(build_palette(_mode))
    app.setFont(base_font())
    app.setStyleSheet(stylesheet())
    return _mode
