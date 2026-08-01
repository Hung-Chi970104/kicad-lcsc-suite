"""Contains the Action Plugin."""

import os

from pcbnew import ActionPlugin  # pylint: disable=import-error

from .mainwindow import JLCPCBTools


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
        """Overwrite Run."""
        dialog = JLCPCBTools(None)
        dialog.Center()
        dialog.Show()
