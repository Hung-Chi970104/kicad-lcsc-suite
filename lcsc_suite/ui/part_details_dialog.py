"""Part Details — everything JLC knows about one assigned part (§5.6).

Opened from the main window's `Part details` button, for a part that already has
a number. That is the difference between this and the Explorer's detail pane:
the pane helps you *choose* a part and is arranged around the two stock figures,
while this answers "what exactly did I put on this board" and is arranged around
identity — the component code, the full name, the assembly process, the minimum
order quantity and both price ladders.

The record comes from JLC's assembly endpoint through
:meth:`lcsc_suite.search_source.LiveSource.assembly_detail`, so the probe and the
tests get the captured payloads and cannot reach the wire. It is fetched on a
worker: the wx dialog already used a thread for this, and for the same reason —
a synchronous lookup freezes the window for as long as the endpoint takes, which
on a rate-limited host is the ten-second timeout.

Two things the wx dialog needed and this does not:

* **A liveness check at the top of the callback.** ``wx.CallAfter`` raises on the
  worker thread once the dialog is gone; a Qt signal to a destroyed receiver is
  simply not delivered.
* **`Destroy()` on close.** A modeless ``wx.Dialog`` hides rather than destroys
  by default, so the wx version had to override ``EVT_CLOSE`` or accumulate
  hidden dialogs. ``WA_DeleteOnClose`` is one attribute.
"""

from __future__ import annotations

import logging
import os
from typing import Optional
import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..shared import lcsc_api as api
from .explorer.tasks import Pool
from .icons import icon

log = logging.getLogger(__name__)

#: The wx dialog is 1000x800. Narrower here: the property table is two columns
#: of text and the photo is 200px, so the rest was empty.
DEFAULT_SIZE = (780, 640)

PHOTO_PX = 220

#: ``(payload key, label)`` in the order §5.6 lists them, which is identity
#: first and commercial terms last. The wx dialog's set, reordered: it leads
#: with the component code, and the first thing anyone checks is which part on
#: the board this is.
FIELDS: tuple[tuple[str, str], ...] = (
    ("componentDesignator", "Designator"),
    ("componentCode", "Component Code"),
    ("componentName", "Full Name"),
    ("componentBrandEn", "Brand"),
    ("describe", "Description"),
    ("componentModelEn", "Model"),
    ("componentSpecificationEn", "Specification"),
    ("firstTypeNameEn", "Primary Category"),
    ("secondTypeNameEn", "Secondary Category"),
    ("assemblyProcess", "Assembly Process"),
    ("matchedPartDetail", "Details"),
    ("stockCount", "Stock"),
    ("leastNumber", "Minimal Quantity"),
    ("leastNumberPrice", "Minimum price"),
)

#: How the assembly endpoint spells the library type, and how people do.
LIBRARY_TYPES = {"base": "Basic", "expand": "Extended"}

LOADING_TEXT = "Loading part details…"


def price_rows(detail: dict) -> list[tuple[str, str]]:
    """Render both price ladders as ``(label, price)`` rows.

    Two ladders, kept apart and labelled by source, for the reason the whole
    fork exists: JLC assembly and LCSC retail are different inventories at
    different prices, and a single "price" row would have to pick one.

    ``endNumber == -1`` is the open-ended top band — "this price from here up" —
    which is why it reads ``>N`` rather than ``N--1``.
    """
    rows: list[tuple[str, str]] = []
    for key, source in (("jlcPrices", "JLC"), ("prices", "LCSC")):
        for band in detail.get(key) or []:
            if not isinstance(band, dict):
                continue
            start = band.get("startNumber")
            end = band.get("endNumber")
            span = f">{start}" if end == -1 else f"{start}-{end}"
            rows.append((f"{source} Price for {span}", str(band.get("productPrice"))))
    return rows


def photo_urls(detail: dict) -> list[str]:
    """Return JLC file-service photo URLs from an assembly record.

    JLC's file service rather than LCSC's CDN, and deliberately: LCSC 403s whole
    networks and takes its image CDN down with them, which is the same reason
    the Explorer's photo tile prefers these. Derived from the payload already in
    hand rather than through ``api.assembly_photo_urls``, which would fetch the
    same record a second time.
    """
    urls: list[str] = []
    for entry in detail.get("imageList") or []:
        if not isinstance(entry, dict):
            continue
        url = api.jlc_image_url(entry.get("productBigImageAccessId"))
        if url and url not in urls:
            urls.append(url)
    fallback = api.jlc_image_url(detail.get("productBigImageAccessId"))
    if fallback and fallback not in urls:
        urls.append(fallback)
    return urls


class PartDetailsDialog(QDialog):
    """Everything JLC's assembly record says about one part."""

    def __init__(
        self,
        parent=None,
        source=None,
        lcsc: str = "",
        references=None,
        project_path: str = "",
    ) -> None:
        super().__init__(parent)
        self.source = source
        self.lcsc = api.normalize_lcsc(lcsc)
        self.references: list[str] = list(references or [])
        self.project_path = project_path
        self.detail: dict = {}
        self.datasheet_url = ""
        self.page_url = ""

        self.setObjectName("part-details-dialog")
        self.setWindowTitle(self._title())
        self.resize(*DEFAULT_SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self._pool = Pool("lcsc-part-details", 1, self._on_fetched)
        self._build()
        self.start_fetch()

    def _title(self) -> str:
        """Name the window after the part and the rows it was opened from.

        The board references matter here in a way they do not in the Explorer:
        this dialog is reached from a selection, and the part it describes is
        one that is already placed. ``componentDesignator`` in the table below
        is a different thing — JLC's category letter — and would be mistaken for
        this if the window did not say which is which.
        """
        if not self.references:
            return f"Part details — {self.lcsc}"
        shown = ", ".join(self.references[:4])
        if len(self.references) > 4:
            shown += f", +{len(self.references) - 4} more"
        return f"Part details — {self.lcsc} ({shown})"

    # -- construction ---------------------------------------------------------

    def _build(self) -> None:
        """Assemble the property table and the right-hand column."""
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)
        root.addWidget(self._build_table(), 1)
        root.addWidget(self._build_side(), 0)

    def _build_table(self) -> QTableWidget:
        """Build the two-column property list."""
        table = QTableWidget(0, 2, self)
        table.setObjectName("part-details-table")
        table.setHorizontalHeaderLabels(["Property", "Value"])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table = table
        return table

    def _build_side(self) -> QWidget:
        """Build the photo and the three link buttons."""
        panel = QWidget(self)
        panel.setFixedWidth(PHOTO_PX + 20)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.photo = QLabel(panel)
        self.photo.setObjectName("part-details-photo")
        self.photo.setFixedSize(PHOTO_PX, PHOTO_PX)
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo.setProperty("role", "card")
        self.photo.setFrameShape(QLabel.Shape.StyledPanel)
        self.photo.setText("…")
        layout.addWidget(self.photo, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

        self.download_button = QPushButton(
            icon("mdi-cloud-download-outline.png"), "Download Datasheet", panel
        )
        self.download_button.clicked.connect(self.download_datasheet)
        layout.addWidget(self.download_button)

        self.open_datasheet_button = QPushButton(
            icon("mdi-file-document-outline.png"), "Open Datasheet", panel
        )
        self.open_datasheet_button.clicked.connect(self.open_datasheet)
        layout.addWidget(self.open_datasheet_button)

        self.open_page_button = QPushButton(
            icon("mdi-earth.png"), "Open LCSC page", panel
        )
        self.open_page_button.clicked.connect(self.open_page)
        layout.addWidget(self.open_page_button)

        close = QPushButton("Close", panel)
        close.clicked.connect(self.reject)
        layout.addWidget(close)

        self._set_links_enabled(False)
        return panel

    def _set_links_enabled(self, enabled: bool) -> None:
        """Enable the three link buttons only when there is a URL behind them."""
        self.download_button.setEnabled(enabled and bool(self.datasheet_url))
        self.open_datasheet_button.setEnabled(enabled and bool(self.datasheet_url))
        self.open_page_button.setEnabled(enabled and bool(self.page_url))

    # -- fetching -------------------------------------------------------------

    def start_fetch(self) -> None:
        """Show the placeholder row and queue the lookup."""
        self.set_rows([("Status", LOADING_TEXT)])
        if self.source is None or not self.lcsc:
            self.set_rows([("Status", "No LCSC number to look up.")])
            return
        source, number = self.source, self.lcsc

        def work():
            detail = source.assembly_detail(number)
            image = None
            for url in photo_urls(detail):
                image = source.image(url)
                if image:
                    break
            return detail, image

        self._pool.start(0, number, work)

    def _on_fetched(self, _token, _key, result) -> None:
        """Fill the dialog in from the worker's result."""
        if result is None:
            self.set_rows([("Status", "Could not read the part record.")])
            return
        detail, image = result
        self.detail = detail or {}
        if not self.detail:
            self.set_rows(
                [
                    ("Status", f"Nothing found for {self.lcsc}."),
                    (
                        "Hint",
                        "Either the number is wrong, or JLC's assembly endpoint "
                        "is refusing us — the two look the same from here.",
                    ),
                ]
            )
            return
        self.datasheet_url = str(self.detail.get("dataManualUrl") or "")
        self.page_url = str(self.detail.get("lcscGoodsUrl") or "")
        self.set_rows(self.build_rows(self.detail))
        self.show_photo(image)
        self._set_links_enabled(True)

    @staticmethod
    def build_rows(detail: dict) -> list[tuple[str, str]]:
        """Turn an assembly record into the property rows, in §5.6's order."""
        rows: list[tuple[str, str]] = []
        library_type = LIBRARY_TYPES.get(str(detail.get("componentLibraryType") or ""))
        if library_type:
            rows.append(("Type", library_type))
        for key, label in FIELDS:
            value = detail.get(key)
            # Falsy rather than None, as the wx dialog does: a blank description
            # and a zero minimum quantity are both "the endpoint had nothing to
            # say", and a row of empty space is worse than no row.
            if value:
                rows.append((label, str(value)))
        rows.extend(price_rows(detail))
        for attribute in detail.get("attributes") or []:
            if not isinstance(attribute, dict):
                continue
            name = attribute.get("attribute_name_en")
            if name:
                rows.append((str(name), str(attribute.get("attribute_value_name"))))
        return rows

    def set_rows(self, rows) -> None:
        """Replace the property table's contents."""
        rows = list(rows)
        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for index, (name, value) in enumerate(rows):
            self.table.setItem(index, 0, QTableWidgetItem(str(name)))
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            self.table.setItem(index, 1, item)

    def show_photo(self, data: Optional[bytes]) -> None:
        """Render the product photo, or say there is none."""
        if not data:
            self.photo.setText("No photo")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.photo.setText("No photo")
            return
        self.photo.setPixmap(
            pixmap.scaled(
                PHOTO_PX,
                PHOTO_PX,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # -- the three links ------------------------------------------------------

    def open_page(self) -> None:
        """Open the part's LCSC product page in a browser."""
        if self.page_url:
            log.info("Opening %s", self.page_url)
            webbrowser.open(self.page_url)

    def open_datasheet(self) -> None:
        """Open the datasheet PDF in a browser."""
        if self.datasheet_url:
            log.info("Opening %s", self.datasheet_url)
            webbrowser.open(self.datasheet_url)

    def datasheet_directory(self) -> str:
        """Where a downloaded datasheet is written.

        Beside the project, as the wx dialog does, so datasheets travel with the
        board rather than accumulating in one global folder nobody prunes.
        """
        return os.path.join(self.project_path or os.path.expanduser("~"), "datasheets")

    def download_datasheet(self) -> None:
        """Save the datasheet next to the project.

        Through ``source.image`` — which is ``api.fetch_image``, a cached binary
        GET behind the host breaker. The name is about its usual payload rather
        than its behaviour; a PDF is binary too, and going this way means the
        download honours the same breaker and the same offline guarantee as
        everything else the window fetches.
        """
        if not self.datasheet_url or self.source is None:
            return
        directory = self.datasheet_directory()
        filename = self.datasheet_url.rsplit("/", maxsplit=1)[-1] or f"{self.lcsc}.pdf"
        target = os.path.join(directory, filename)
        data = self.source.image(self.datasheet_url)
        if not data:
            QMessageBox.warning(
                self,
                "Download datasheet",
                "The datasheet could not be downloaded.\n\n"
                "LCSC blocks whole networks at a time, so this may be nothing to "
                "do with the part. Open Datasheet still works in a browser.",
            )
            return
        try:
            os.makedirs(directory, exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            QMessageBox.warning(
                self, "Download datasheet", f"Could not write the file.\n\n{exc}"
            )
            return
        log.info("Saved the datasheet to %s", target)
        QMessageBox.information(self, "Download datasheet", f"Saved to\n{target}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Drop queued work on the way out."""
        self._pool.clear()
        super().closeEvent(event)


__all__ = [
    "DEFAULT_SIZE",
    "FIELDS",
    "LOADING_TEXT",
    "PartDetailsDialog",
    "photo_urls",
    "price_rows",
]
