"""Handles the generation of the Gerber files, the BOM and the POS file."""

import csv
from importlib import import_module
import logging
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from pcbnew import (  # pylint: disable=import-error
    EXCELLON_WRITER,
    PCB_PLOT_PARAMS,
    PCB_VIA,
    PLOT_CONTROLLER,
    PLOT_FORMAT_GERBER,
    VECTOR2I,
    ZONE_FILLER,
    B_Cu,
    B_Mask,
    B_Paste,
    B_SilkS,
    Edge_Cuts,
    F_Cu,
    F_Mask,
    F_Paste,
    F_SilkS,
    Refresh,
    wxPoint,
)

from . import fab_rules
from .fab_rules import split_bom_designators  # noqa: F401 - re-exported
from .footprint_helpers import get_is_dnp

# Compatibility hack for V6 / V7 / V7.99
try:
    from pcbnew import DRILL_MARKS_NO_DRILL_SHAPE  # pylint: disable=import-error

    NO_DRILL_SHAPE = DRILL_MARKS_NO_DRILL_SHAPE
except ImportError:
    NO_DRILL_SHAPE = PCB_PLOT_PARAMS.NO_DRILL_SHAPE

# The 2048-character BOM row limit and the chunker that respects it moved to
# fab_rules, along with the rest of what both halves of the migration have to
# agree on. Imported above under its old name so existing callers and tests do
# not care that it moved.
_BOM_DESIGNATOR_MAX_LEN = fab_rules.BOM_DESIGNATOR_MAX_LEN


class Fabrication:
    """Contains all functionality to generate the JLCPCB production files."""

    def __init__(self, parent, board):
        self.parent = parent
        self.logger = logging.getLogger(__name__)
        self.board = board
        self.corrections = []
        self.path, self.filename = os.path.split(self.board.GetFileName())
        self.create_folders()

    def create_folders(self):
        """Create output folders if they not already exist."""
        self.outputdir = os.path.join(self.path, "jlcpcb", "production_files")
        Path(self.outputdir).mkdir(parents=True, exist_ok=True)
        self.gerberdir = os.path.join(self.path, "jlcpcb", "gerber")
        Path(self.gerberdir).mkdir(parents=True, exist_ok=True)

    def get_gerber_zip_path(self):
        """Return the full path to the generated Gerber ZIP file."""
        return os.path.join(self.outputdir, f"GERBER-{Path(self.filename).stem}.zip")

    def get_cpl_csv_path(self):
        """Return the full path to the generated CPL CSV file."""
        return os.path.join(self.outputdir, f"CPL-{Path(self.filename).stem}.csv")

    def get_bom_csv_path(self):
        """Return the full path to the generated BOM CSV file."""
        return os.path.join(self.outputdir, f"BOM-{Path(self.filename).stem}.csv")

    def get_artifact_paths(self):
        """Return all generated production artifact paths."""
        return {
            "gerber_zip": self.get_gerber_zip_path(),
            "cpl_csv": self.get_cpl_csv_path(),
            "bom_csv": self.get_bom_csv_path(),
        }

    def fill_zones(self):
        """Refill copper zones following user prompt."""
        if self.parent.settings.get("gerber", {}).get("fill_zones", True):
            filler = ZONE_FILLER(self.board)
            zones = self.board.Zones()
            filler.Fill(zones)
            Refresh()

    def _find_correction(self, value):
        """Return (rotation, offset) for the first correction matching value."""
        return fab_rules.find_correction(self.corrections, value)

    @staticmethod
    def _correction_names(footprint):
        """Return the three names a correction may match, in priority order."""
        return (
            str(footprint.GetReference()),
            str(footprint.GetValue()),
            str(footprint.GetFPID().GetLibItemName()),
        )

    @staticmethod
    def _orientation(footprint):
        """Return the footprint's angle in degrees, across KiCad versions."""
        original = footprint.GetOrientation()
        # `.AsDegrees()` added in KiCAD 6.99
        try:
            return original.AsDegrees()
        except AttributeError:
            # we need to divide by 10 to get 180 out of 1800 for example.
            # This might be a bug in 5.99 / 6.0 RC
            return original / 10

    def fix_rotation(self, footprint):
        """Fix the rotation of footprints in order to be correct for JLCPCB."""
        names = self._correction_names(footprint)
        bottom = footprint.GetLayer() != 0
        match = fab_rules.match_for(self.corrections, names)
        if match:
            self.logger.info(
                "Fixed rotation of %s (%s / %s) on %s Layer by %d degrees",
                names[0],
                names[1],
                names[2],
                "Bottom" if bottom else "Top",
                match[0],
            )
        return fab_rules.corrected_rotation(
            self._orientation(footprint), bottom, names, self.corrections
        )

    def fix_position(self, footprint, position):
        """Fix the position of footprints in order to be correct for JLCPCB."""
        names = self._correction_names(footprint)
        bottom = footprint.GetLayer() != 0
        match = fab_rules.match_for(self.corrections, names)
        if match and (match[1][0] != 0 or match[1][1] != 0):
            self.logger.info(
                "Fixed position of %s (%s / %s) on %s Layer by %f/%f",
                names[0],
                names[1],
                names[2],
                "Bottom" if bottom else "Top",
                match[1][0],
                match[1][1],
            )
        x, y = fab_rules.corrected_position(
            position.x,
            position.y,
            self._orientation(footprint),
            bottom,
            names,
            self.corrections,
        )
        return wxPoint(x, y)

    def get_position(self, footprint):
        """Calculate position based on center of bounding box."""
        try:
            pads = footprint.Pads()
            bbox = pads[0].GetBoundingBox()
            for pad in pads:
                bbox.Merge(pad.GetBoundingBox())
            return bbox.GetCenter()
        except:
            self.logger.info(
                "WARNING footprint %s: original position used", footprint.GetReference()
            )
            return footprint.GetPosition()

    def generate_geber(self, layer_count=None):
        """Generate Gerber files."""
        # inspired by https://github.com/KiCad/kicad-source-mirror/blob/master/demos/python_scripts_examples/gen_gerber_and_drill_files_board.py

        pctl = PLOT_CONTROLLER(self.board)
        popt = pctl.GetPlotOptions()

        # https://github.com/KiCad/kicad-source-mirror/blob/master/pcbnew/pcb_plot_params.h
        popt.SetOutputDirectory(self.gerberdir)

        # Plot format to Gerber
        # https://github.com/KiCad/kicad-source-mirror/blob/master/include/plotter.h#L67-L78
        popt.SetFormat(1)

        # General Options
        popt.SetPlotValue(
            self.parent.settings.get("gerber", {}).get("plot_values", True)
        )
        popt.SetPlotReference(
            self.parent.settings.get("gerber", {}).get("plot_references", True)
        )

        popt.SetSketchPadsOnFabLayers(False)

        # Gerber Options
        popt.SetUseGerberProtelExtensions(False)

        popt.SetCreateGerberJobFile(False)

        popt.SetSubtractMaskFromSilk(
            self.parent.settings.get("gerber", {}).get("subtract_mask_from_silk", True)
        )

        popt.SetUseAuxOrigin(True)

        # Tented vias or not, selcted by user in settings
        # Only possible via settings in KiCAD < 8.99
        # In KiCAD 8.99 this must be set in the layer settings of KiCAD
        if hasattr(PCB_VIA, "SetPlotViaOnMaskLayer"):
            popt.SetPlotViaOnMaskLayer(
                not self.parent.settings.get("gerber", {}).get("tented_vias", True)
            )

        popt.SetUseGerberX2format(True)

        popt.SetIncludeGerberNetlistInfo(True)

        popt.SetDisableGerberMacros(False)

        popt.SetDrillMarksType(NO_DRILL_SHAPE)

        popt.SetPlotFrameRef(False)

        # delete all existing files in the output directory first
        for f in os.listdir(self.gerberdir):
            os.remove(os.path.join(self.gerberdir, f))

        # if no layer_count is given, get the layer count from the board
        if not layer_count:
            layer_count = self.board.GetCopperLayerCount()

        plot_plan_top = [
            ("CuTop", F_Cu, "Top layer"),
            ("SilkTop", F_SilkS, "Silk top"),
            ("MaskTop", F_Mask, "Mask top"),
            ("PasteTop", F_Paste, "Paste top"),
        ]
        plot_plan_bottom = [
            ("CuBottom", B_Cu, "Bottom layer"),
            ("SilkBottom", B_SilkS, "Silk bottom"),
            ("MaskBottom", B_Mask, "Mask bottom"),
            ("EdgeCuts", Edge_Cuts, "Edges"),
            ("PasteBottom", B_Paste, "Paste bottom"),
        ]

        plot_plan = []

        # Single sided PCB
        if layer_count == 1:
            plot_plan = plot_plan_top + plot_plan_bottom[-2:]
        # Double sided PCB
        elif layer_count == 2:
            plot_plan = plot_plan_top + plot_plan_bottom
        # Everything with inner layers
        else:
            plot_plan = (
                plot_plan_top
                + [
                    (
                        f"CuIn{layer}",
                        getattr(import_module("pcbnew"), f"In{layer}_Cu"),
                        f"Inner layer {layer}",
                    )
                    for layer in range(1, layer_count - 1)
                ]
                + plot_plan_bottom
            )

        # Add all JLC prefixed layers - layers must have "JLC_" in their name
        jlc_layers_to_plot = []
        enabled_layer_ids = list(self.board.GetEnabledLayers().Seq())
        for enabled_layer_id in enabled_layer_ids:
            layer_name_string = str(self.board.GetLayerName(enabled_layer_id)).upper()
            if "JLC_" in layer_name_string:
                plotter_info = (layer_name_string, enabled_layer_id, layer_name_string)
                jlc_layers_to_plot.append(plotter_info)
        plot_plan += jlc_layers_to_plot

        for layer_info in plot_plan:
            if layer_info[1] <= B_Cu:
                popt.SetSkipPlotNPTH_Pads(True)
            else:
                popt.SetSkipPlotNPTH_Pads(False)
            pctl.SetLayer(layer_info[1])
            pctl.OpenPlotfile(layer_info[0], PLOT_FORMAT_GERBER, layer_info[2])
            if pctl.PlotLayer() is False:
                self.logger.error("Error plotting %s", layer_info[2])
            self.logger.info("Successfully plotted %s", layer_info[2])
        pctl.ClosePlot()

    def generate_excellon(self):
        """Generate Excellon files."""
        drlwriter = EXCELLON_WRITER(self.board)
        mirror = False
        minimalHeader = False
        offset = self.board.GetDesignSettings().GetAuxOrigin()
        mergeNPTH = False
        drlwriter.SetOptions(mirror, minimalHeader, offset, mergeNPTH)
        drlwriter.SetFormat(False)
        genDrl = True
        genMap = True
        drlwriter.CreateDrillandMapFilesSet(self.gerberdir, genDrl, genMap)
        self.logger.info("Finished generating Excellon files")

    def zip_gerber_excellon(self):
        """Zip Gerber and Excellon files, ready for upload to JLCPCB."""
        zip_path = self.get_gerber_zip_path()
        with ZipFile(
            zip_path,
            "w",
            compression=ZIP_DEFLATED,
            compresslevel=9,
        ) as zipfile:
            for folderName, _, filenames in os.walk(self.gerberdir):
                for filename in filenames:
                    if not filename.endswith(("gbr", "drl", "pdf")):
                        continue
                    filePath = os.path.join(folderName, filename)
                    zipfile.write(filePath, os.path.basename(filePath))
        self.logger.info("Finished generating ZIP file %s", zip_path)

    def generate_cpl(self):
        """Generate placement file (CPL)."""
        cpl_path = self.get_cpl_csv_path()
        self.corrections = self.parent.library.get_all_correction_data()
        aux_orgin = self.board.GetDesignSettings().GetAuxOrigin()
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        with open(cpl_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(fab_rules.CPL_HEADER)
            footprints = sorted(self.board.Footprints(), key=lambda x: x.GetReference())
            for fp in footprints:
                if get_is_dnp(fp):
                    self.logger.info(
                        "Component %s has 'Do not place' enabled: removing from CPL",
                        fp.GetReference(),
                    )
                    continue
                part = self.parent.store.get_part(fp.GetReference())
                if not part:  # No matching part in the database, continue
                    continue
                if part["exclude_from_pos"] == 1:
                    continue
                if not add_without_lcsc and not part["lcsc"]:
                    continue
                try:  # Kicad <= 8.0
                    position = self.get_position(fp) - aux_orgin
                except TypeError:  # Kicad 8.99
                    x1, y1 = self.get_position(fp)
                    x2, y2 = aux_orgin
                    position = VECTOR2I(x1 - x2, y1 - y2)
                position = self.fix_position(fp, position)
                writer.writerow(
                    fab_rules.cpl_row(
                        part["reference"],
                        part["value"],
                        part["footprint"],
                        position.x,
                        position.y,
                        self.fix_rotation(fp),
                        fp.GetLayer() != 0,
                    )
                )
        self.logger.info("Finished generating CPL file %s", cpl_path)

    def generate_bom(self):
        """Generate BOM file."""
        bom_path = self.get_bom_csv_path()
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        footprints = {fp.GetReference(): fp for fp in self.board.Footprints()}

        def is_dnp(reference):
            fp = footprints.get(reference)
            return bool(fp) and get_is_dnp(fp)

        def report(reason, subject):
            if reason == "dnp":
                self.logger.info(
                    "Component %s has 'Do not place' enabled: removing from BOM",
                    subject,
                )
            else:
                self.logger.info(
                    "Component group %s has no LCSC number assigned and the setting Add parts without LCSC is disabled: removing from BOM",
                    subject,
                )

        with open(bom_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(fab_rules.BOM_HEADER)
            writer.writerows(
                fab_rules.bom_rows(
                    self.parent.store.read_bom_parts(),
                    is_dnp=is_dnp,
                    add_without_lcsc=add_without_lcsc,
                    on_skip=report,
                )
            )
        self.logger.info("Finished generating BOM file %s", bom_path)

    def get_part_consistency_warnings(self) -> str:
        """Check the plausibility of the parts, there should be just one value per LCSC number.

        Returns an empty sting if all parts are ok, otherwise a otherwise a overview of parts that share a LCSC number but have different values.
        """
        return fab_rules.consistency_warnings(self.parent.store.read_bom_parts())
