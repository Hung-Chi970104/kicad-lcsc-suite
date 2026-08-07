"""Contains the Action Plugin."""

import os

from pcbnew import ActionPlugin, GetBoard  # pylint: disable=import-error

from .mainwindow import JLCPCBTools, find_open_main_window


class JLCPCBPlugin(ActionPlugin):
    """JLCPCBPlugin instance of ActionPlugin."""

    def defaults(self):
        """Define defaults."""
        # pylint: disable=attribute-defined-outside-init
        # Named distinctly from upstream "JLCPCB Tools" so both can be
        # installed side by side without two identical toolbar entries.
        self.name = "LCSC Suite"
        self.category = "Fabrication data generation"
        self.description = (
            "Search LCSC/JLCPCB with parametric filters, compare assembly vs "
            "retail stock, import symbols/footprints/3D models, and generate "
            "JLCPCB Gerber, Excellon, BOM and CPL files"
        )
        self.show_toolbar_button = True
        path, _ = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self._pcbnew_frame = None

    def Run(self):
        """Open the main window, or raise the one that is already open.

        KiCad happily calls this again while the dialog is up. A second
        instance is not merely untidy: both windows open the same project
        database and the same board, so assignments made in one are invisible
        to — and can be overwritten by — the other.
        """
        existing = find_open_main_window()
        if existing is not None and self._reuse(existing):
            return

        dialog = JLCPCBTools(None)
        dialog.Center()
        dialog.Show()

    def _reuse(self, window):
        """Bring an already-open window to the front; False if it cannot be."""
        try:
            board_file = GetBoard().GetFileName()
            if getattr(window, "board_file", board_file) != board_file:
                # A different board is loaded now, so that window's project
                # database, part list and fabrication paths all point at the
                # wrong project. Replace it.
                window.Close()
                return False
            if window.IsIconized():
                window.Iconize(False)
            if not window.IsShown():
                window.Show()
            window.Raise()
        except RuntimeError:
            # Window was destroyed between finding it and touching it.
            return False
        return True
