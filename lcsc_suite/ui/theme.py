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

``brand``
    The EasyAssembly indigo, and the **only** hue that appears in the chrome.
    Every other entry above is a claim about a part, so the accent had to come
    from somewhere the data never goes — otherwise a branded toolbar teaches
    users a colour that means nothing in the one place they need to read it.
    Nothing in the part table is ever drawn in it.

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
    # Match highlighting. Deliberately *not* the "standard" amber, which the two
    # would otherwise share: a standard-mode trigger colours the whole row while
    # a match tints runs inside one cell, so they co-occur, and one amber for
    # both would read as a single meaning. Teal is unused elsewhere in the table.
    "match": ((0, 112, 116), (110, 214, 214)),
    # Brand. The one hue the *chrome* is allowed to be, and the reason it is
    # indigo rather than anything nearer the data: every other entry above
    # already means something in the part table, so a brand drawn in teal would
    # read as "this came from JLC" and one in purple as "this is retail stock".
    # Indigo is the widest gap left on the wheel, and it never appears in a cell.
    "brand": ((59, 91, 219), (124, 143, 245)),
    # Chrome
    "muted": ((108, 112, 124), (148, 152, 164)),
    "rule": ((223, 225, 231), (62, 64, 72)),
}

#: Match-highlight colour when the row is selected. One value for both
#: appearances, because the selection fill is the same strong blue in both and
#: the theme's teal disappears against it.
_MATCH_ON_SELECTION = (255, 215, 64)

#: Window/base/text colours for the two Fusion palettes. Stated rather than
#: inherited so both platforms and both appearances land on the same pixels.
#:
#: The greys carry a slight blue cast (the channels rise B > G > R) rather than
#: being neutral. That is deliberate and it is the whole trick behind the
#: chrome: a dead-neutral grey next to a saturated indigo reads as *dirty*,
#: because the eye takes the accent as the white point. Two or three points of
#: blue is below the threshold of looking "blue" and above the threshold of
#: looking wrong.
_CHROME = {
    "light": {
        "window": (243, 244, 246),
        "base": (255, 255, 255),
        "alternate": (248, 249, 251),
        "text": (24, 26, 32),
        "disabled": (156, 160, 170),
        "highlight": (59, 91, 219),
        "highlight_text": (255, 255, 255),
        # Dark tooltips in both appearances. The pale yellow Fusion inherits
        # from Windows 95 is the single most dated thing the old palette had,
        # and it is the one surface that never sits next to the data.
        "tooltip": (36, 38, 46),
        "tooltip_text": (244, 245, 248),
        "card": (236, 238, 242),
    },
    "dark": {
        # Deeper than the old (48,49,53). The status hues above are all
        # high-luminance in dark mode, so the further the chrome drops the more
        # the data separates from it — the table is the point of this window.
        "window": (32, 34, 40),
        "base": (25, 27, 32),
        "alternate": (30, 32, 38),
        "text": (226, 228, 235),
        "disabled": (118, 122, 132),
        "highlight": (88, 110, 232),
        # White, not the old near-black. The selection fill is a saturated
        # indigo in both appearances now, and dark text on it was unreadable.
        "highlight_text": (255, 255, 255),
        "tooltip": (58, 61, 71),
        "tooltip_text": (236, 237, 242),
        "card": (42, 45, 53),
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


def brand() -> QColor:
    """Return the accent — the only hue the chrome is allowed to use.

    Kept behind a function rather than exported as a constant because it is
    appearance-dependent like everything else here: the light indigo would
    disappear into a dark window and the dark one glares on white.
    """
    return colour("brand")


def brand_rgb() -> tuple[int, int, int]:
    """Return the brand indigo as fixed RGB, ignoring the current appearance.

    For artwork that leaves this process — KiCad's toolbar, the PCM listing, a
    desktop icon — where there is no palette to follow and one file has to serve
    every background. The light variant, because what supplies its contrast is
    the tile it fills, not the window behind it.
    """
    return _PALETTE["brand"][0]


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


def highlight_ink(selected: bool = False) -> QColor:
    """Colour for the matched runs inside the LCSC Params cell.

    A selected row is filled with a strong blue that the theme's teal vanishes
    into, so the selected case gets its own high-contrast value rather than a
    tint of the same hue.
    """
    if selected:
        return QColor(*_MATCH_ON_SELECTION)
    return colour("match")


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
    put(QPalette.ColorRole.ToolTipText, "tooltip_text")
    put(QPalette.ColorRole.Text, "text")
    put(QPalette.ColorRole.Button, "window")
    put(QPalette.ColorRole.ButtonText, "text")
    put(QPalette.ColorRole.Highlight, "highlight")
    put(QPalette.ColorRole.HighlightedText, "highlight_text")
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 80, 80))
    # Links are chrome, so they take the brand accent. They used to take the
    # JLC teal, which made every hyperlink look like a claim about stock.
    palette.setColor(
        QPalette.ColorRole.Link,
        QColor(*_PALETTE["brand"][1 if target is Mode.DARK else 0]),
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
    """App-wide stylesheet: the handful of things a palette cannot express.

    Three rules govern everything below, and breaking one of them is how a
    restyle turns into a regression:

    **Nothing here states a font family or a text size in pixels.** The layout
    gate (``scripts/compare_geometry.py``) only holds because the app pins a
    point size and lets each platform pick the face. A ``font-size: 11px`` in a
    stylesheet would silently opt that widget out of the arrangement.

    **No sub-controls on spin boxes or combo boxes.** The moment a stylesheet
    touches ``QSpinBox::up-button`` Qt stops drawing the Fusion arrow and
    expects an image instead, so the control loses its arrows on every platform
    at once. The two spin boxes get a border and nothing else.

    **Padding is symmetric and stated in whole pixels.** Half-pixel padding
    rounds differently at fractional device-pixel ratios, which is a
    Windows-only wobble that no screenshot taken here would show.
    """
    rule = colour("rule").name()
    muted = colour("muted").name()
    card = card_background().name()
    accent = brand().name()
    base = chrome("base").name()
    window = chrome("window").name()
    text = chrome("text").name()
    # Hover and pressed are derived rather than stated so they track the two
    # appearances automatically: a fixed grey that reads as "raised" on the
    # light window reads as "sunken" on the dark one.
    hover = blend(chrome("window"), chrome("text"), 0.08).name()
    pressed = blend(chrome("window"), chrome("text"), 0.16).name()
    scroll = blend(chrome("window"), chrome("text"), 0.22).name()
    scroll_hover = blend(chrome("window"), chrome("text"), 0.38).name()
    return f"""
    QToolBar {{
        border: 0;
        border-bottom: 1px solid {rule};
        padding: 3px 4px;
        spacing: 2px;
    }}
    QToolBar::separator {{
        background: {rule};
        width: 1px;
        margin: 6px 8px;
    }}
    QToolButton {{
        padding: 4px 7px;
        border: 1px solid transparent;
        border-radius: 5px;
    }}
    QToolButton:hover {{ background: {hover}; }}
    QToolButton:pressed {{ background: {pressed}; }}
    QToolButton:checked {{ background: {card}; border-color: {rule}; }}

    /* The identity bar. A flat strip, separated by one hairline and nothing
       else — a gradient or a drop shadow here would be the "overly fancy" this
       design is trying not to be. */
    QFrame#identity-bar {{
        background: {base};
        border: 0;
        border-bottom: 1px solid {rule};
    }}
    QLabel#identity-wordmark {{
        color: {text};
        font-weight: 600;
    }}
    QLabel#identity-context {{ color: {muted}; }}

    QHeaderView::section {{
        background: {window};
        padding: 5px 6px;
        border: 0;
        border-bottom: 1px solid {rule};
    }}
    QHeaderView::section:hover {{ background: {hover}; }}
    QTableView {{
        border: 1px solid {rule};
        border-radius: 5px;
        gridline-color: transparent;
    }}
    QTableView::item:focus {{ outline: none; }}

    QPlainTextEdit, QTextEdit, QListView, QTreeView {{
        border: 1px solid {rule};
        border-radius: 5px;
    }}
    QLineEdit {{
        border: 1px solid {rule};
        border-radius: 5px;
        padding: 3px 6px;
        background: {base};
    }}
    QLineEdit:focus {{ border-color: {accent}; }}
    /* Border only. See the docstring: sub-control rules cost the arrows. */
    QSpinBox, QDoubleSpinBox {{
        border: 1px solid {rule};
        border-radius: 5px;
    }}

    QPushButton {{
        padding: 4px 12px;
        /* 56 + the 12px padding either side = the 80px Fusion enforces on its
           own. Styling a QPushButton at all discards that minimum, and what it
           looks like when it goes is a 45px `OK` sitting next to a
           `Show Details…` three times its width — which reads as a broken
           dialog rather than as a restyled one. */
        min-width: 56px;
        border: 1px solid {rule};
        border-radius: 5px;
        background: {card};
    }}
    QPushButton:hover {{ background: {hover}; }}
    QPushButton:pressed {{ background: {pressed}; }}
    QPushButton:default {{ border-color: {accent}; }}
    QPushButton:disabled {{ color: {muted}; background: transparent; }}

    /* Thin, no arrows, handle only on the track it needs. Fusion's stepper
       arrows are the other half of the dated look the tooltips had. */
    QScrollBar:vertical {{
        background: transparent;
        width: 11px;
        margin: 0;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 11px;
        margin: 0;
    }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
        background: {scroll};
        border-radius: 4px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{ min-height: 28px; }}
    QScrollBar::handle:horizontal {{ min-width: 28px; }}
    QScrollBar::handle:hover {{ background: {scroll_hover}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        height: 0; width: 0; border: 0; background: none;
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:hover {{ background: {rule}; }}

    QProgressBar {{
        border: 0;
        border-radius: 3px;
        background: {card};
    }}
    QProgressBar::chunk {{ background: {accent}; border-radius: 3px; }}

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
