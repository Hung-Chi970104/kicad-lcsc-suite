"""Import LCSC parts into a KiCad library.

Wraps the ``easyeda2kicad`` converters and adds the piece they do not do:
registering the resulting library in KiCad's ``sym-lib-table`` and
``fp-lib-table`` so the symbol and footprint are immediately usable without
the user touching Preferences → Manage Libraries.

Everything lands in one library triplet::

    <root>/<LIB>.kicad_sym     symbols
    <root>/<LIB>.pretty/       footprints
    <root>/<LIB>.3dshapes/     3D models (.wrl + .step)

Project mode puts ``<root>`` next to the board and registers the library in
the project lib-tables using ``${KIPRJMOD}``, which keeps a design portable
between the two machines this project is built on. Global mode puts it under
the KiCad user config directory and registers it globally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import shutil
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_LIB_NAME = "LCSC"


def is_inside(path: str, parent: str) -> bool:
    r"""Report whether ``path`` is at or below ``parent``.

    Uses ``normcase`` + ``normpath`` so this is correct on Windows, where
    paths are case-insensitive and may mix separators — a plain
    ``str.startswith`` gets ``C:\\Users`` vs ``c:/users`` wrong.
    """
    if not path or not parent:
        return False
    try:
        path_n = os.path.normcase(os.path.abspath(path))
        parent_n = os.path.normcase(os.path.abspath(parent))
    except (OSError, ValueError):
        return False
    if path_n == parent_n:
        return True
    return path_n.startswith(parent_n.rstrip(os.sep) + os.sep)


@dataclass
class ImportResult:
    """Outcome of importing one LCSC part."""

    lcsc: str
    symbol_name: str = ""
    footprint_name: str = ""
    model_name: str = ""
    symbol_written: bool = False
    footprint_written: bool = False
    model_written: bool = False
    skipped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing errored and at least one artifact was produced."""
        return not self.errors and (
            self.symbol_written or self.footprint_written or self.model_written
        )

    def describe(self) -> str:
        """Human-readable one-liner for the log box."""
        if self.errors:
            return f"{self.lcsc}: FAILED — {'; '.join(self.errors)}"
        made = []
        if self.symbol_written:
            made.append(f"symbol {self.symbol_name}")
        if self.footprint_written:
            made.append(f"footprint {self.footprint_name}")
        if self.model_written:
            made.append(f"3D {self.model_name}")
        if not made:
            return f"{self.lcsc}: nothing to do ({', '.join(self.skipped) or 'already present'})"
        return f"{self.lcsc}: imported {', '.join(made)}"


class LcscImporter:
    """Downloads LCSC parts and writes them into a KiCad library."""

    def __init__(
        self,
        root: str,
        lib_name: str = DEFAULT_LIB_NAME,
        project_relative: bool = True,
    ) -> None:
        self.root = Path(root)
        self.lib_name = lib_name or DEFAULT_LIB_NAME
        self.project_relative = project_relative

    # -- paths ----------------------------------------------------------

    @property
    def symbol_lib_path(self) -> Path:
        """Path of the ``.kicad_sym`` file."""
        return self.root / f"{self.lib_name}.kicad_sym"

    @property
    def footprint_lib_path(self) -> Path:
        """Path of the ``.pretty`` footprint directory."""
        return self.root / f"{self.lib_name}.pretty"

    @property
    def model_dir_path(self) -> Path:
        """Path of the ``.3dshapes`` model directory."""
        return self.root / f"{self.lib_name}.3dshapes"

    def _model_uri(self) -> str:
        """URI written into footprints for the 3D model directory."""
        if self.project_relative:
            return "${KIPRJMOD}/" + self._relative_to_project(self.model_dir_path)
        return self.model_dir_path.as_posix()

    def _relative_to_project(self, path: Path) -> str:
        """Best-effort project-relative POSIX path."""
        try:
            return path.relative_to(self.root.parent).as_posix()
        except ValueError:
            return path.as_posix()

    # -- import ---------------------------------------------------------

    def import_part(
        self,
        lcsc: str,
        overwrite: bool = False,
        with_symbol: bool = True,
        with_footprint: bool = True,
        with_model: bool = True,
    ) -> ImportResult:
        """Download ``lcsc`` from EasyEDA and write it into the library."""
        result = ImportResult(lcsc=lcsc)
        try:
            # Deferred because an import is the only thing that needs them, and
            # they are not cheap. ``easyeda2kicad`` is an installed dependency
            # (install.sh pins it); a missing one is reported, not raised.
            from easyeda2kicad.easyeda.easyeda_api import EasyedaApi  # noqa: PLC0415
            from easyeda2kicad.easyeda.easyeda_importer import (  # noqa: PLC0415
                Easyeda3dModelImporter,
                EasyedaFootprintImporter,
                EasyedaSymbolImporter,
            )
            from easyeda2kicad.kicad.export_kicad_3d_model import (  # noqa: PLC0415
                Exporter3dModelKicad,
            )
            from easyeda2kicad.kicad.export_kicad_footprint import (  # noqa: PLC0415
                ExporterFootprintKicad,
            )
            from easyeda2kicad.kicad.export_kicad_symbol import (  # noqa: PLC0415
                ExporterSymbolKicad,
            )
        except ImportError as exc:
            result.errors.append(
                f"easyeda2kicad not importable ({exc}); re-run install.sh"
            )
            return result

        api = EasyedaApi()
        try:
            cad_data = api.get_cad_data_of_component(lcsc_id=lcsc)
        except Exception as exc:  # noqa: BLE001 - network is best-effort
            result.errors.append(f"EasyEDA request failed: {exc}")
            return result
        if not cad_data:
            result.errors.append(
                "no EasyEDA CAD data for this part (many parts on LCSC have no "
                "EasyEDA symbol/footprint)"
            )
            return result

        self.root.mkdir(parents=True, exist_ok=True)

        if with_symbol:
            self._import_symbol(
                cad_data, overwrite, result, EasyedaSymbolImporter, ExporterSymbolKicad
            )
        if with_footprint:
            self._import_footprint(
                cad_data,
                overwrite,
                result,
                EasyedaFootprintImporter,
                ExporterFootprintKicad,
            )
        if with_model:
            self._import_model(
                cad_data,
                overwrite,
                result,
                api=api,
                importer_cls=Easyeda3dModelImporter,
                exporter_cls=Exporter3dModelKicad,
            )
        return result

    def _import_symbol(
        self, cad_data, overwrite, result, importer_cls, exporter_cls
    ) -> None:
        """Convert and write the schematic symbol."""
        try:
            symbol = importer_cls(easyeda_cp_cad_data=cad_data).get_symbol()
            result.symbol_name = symbol.info.name
            exporter = exporter_cls(symbol=symbol, lib_path=str(self.symbol_lib_path))
            written = exporter.save_to_lib(
                lib_path=str(self.symbol_lib_path),
                footprint_lib_name=self.lib_name,
                overwrite=overwrite,
            )
            result.symbol_written = bool(written)
            if not written:
                result.skipped.append("symbol already in library")
        except Exception as exc:  # noqa: BLE001 - report, do not crash the UI
            logger.exception("symbol import failed for %s", result.lcsc)
            result.errors.append(f"symbol: {exc}")

    def _import_footprint(
        self, cad_data, overwrite, result, importer_cls, exporter_cls
    ) -> None:
        """Convert and write the footprint."""
        try:
            footprint = importer_cls(easyeda_cp_cad_data=cad_data).get_footprint()
            result.footprint_name = footprint.info.name
            self.footprint_lib_path.mkdir(parents=True, exist_ok=True)
            target = self.footprint_lib_path / f"{footprint.info.name}.kicad_mod"
            if target.is_file() and not overwrite:
                result.skipped.append("footprint already in library")
                return
            exporter_cls(footprint=footprint).export(
                footprint_full_path=str(target),
                model_3d_path=self._model_uri(),
            )
            result.footprint_written = True
        except Exception as exc:  # noqa: BLE001 - report, do not crash the UI
            logger.exception("footprint import failed for %s", result.lcsc)
            result.errors.append(f"footprint: {exc}")

    def _import_model(
        self, cad_data, overwrite, result, *, api, importer_cls, exporter_cls
    ) -> None:
        """Download and write the 3D model, if the part has one."""
        try:
            model = importer_cls(
                easyeda_cp_cad_data=cad_data,
                download_raw_3d_model=True,
                api=api,
            ).output
            exporter = exporter_cls(model_3d=model)
            if not exporter.output:
                result.skipped.append("no 3D model available")
                return
            result.model_name = exporter.output.name
            self.model_dir_path.mkdir(parents=True, exist_ok=True)
            written = exporter.export(
                output_dir=str(self.model_dir_path), overwrite=overwrite
            )
            result.model_written = bool(written)
            if not written:
                result.skipped.append("3D model already in library")
        except Exception as exc:  # noqa: BLE001 - 3D is the least critical part
            logger.exception("3D model import failed for %s", result.lcsc)
            result.errors.append(f"3D model: {exc}")

    # -- library table registration --------------------------------------

    def register_libraries(self, project_dir: Optional[str] = None) -> List[str]:
        """Ensure the library is listed in the relevant KiCad lib-tables.

        Returns a list of human-readable actions taken. Registration is
        idempotent: an entry with the same nickname is left alone.
        """
        actions: List[str] = []
        if self.project_relative and project_dir:
            base = Path(project_dir)
        else:
            base = _kicad_global_config_dir()
            if base is None:
                actions.append(
                    "Could not locate the KiCad global config directory — "
                    "add the library manually in Preferences → Manage Libraries."
                )
                return actions

        sym_uri = self._table_uri(self.symbol_lib_path, base)
        fp_uri = self._table_uri(self.footprint_lib_path, base)

        actions += _ensure_lib_table_entry(
            base / "sym-lib-table", "sym_lib_table", self.lib_name, sym_uri
        )
        actions += _ensure_lib_table_entry(
            base / "fp-lib-table", "fp_lib_table", self.lib_name, fp_uri
        )
        return actions

    def _table_uri(self, path: Path, base: Path) -> str:
        """Build the lib-table URI for ``path`` relative to ``base``."""
        if self.project_relative:
            try:
                return "${KIPRJMOD}/" + path.relative_to(base).as_posix()
            except ValueError:
                return path.as_posix()
        return path.as_posix()


def _kicad_global_config_dir() -> Optional[Path]:
    """Locate the KiCad user config directory holding the global lib-tables."""
    env = os.environ.get("KICAD_CONFIG_HOME")
    candidates: List[Path] = []
    if env:
        candidates.append(Path(env))
    home = Path.home()
    candidates += [
        home / "Library" / "Preferences" / "kicad",  # macOS
        Path(os.environ.get("APPDATA", home)) / "kicad",  # Windows
        home / ".config" / "kicad",  # Linux
    ]
    for base in candidates:
        if not base.is_dir():
            continue
        # Prefer the highest-numbered version directory that has lib-tables.
        versioned = sorted(
            (
                p
                for p in base.iterdir()
                if p.is_dir() and re.match(r"^\d+\.\d+$", p.name)
            ),
            key=lambda p: [int(x) for x in p.name.split(".")],
            reverse=True,
        )
        for candidate in versioned + [base]:
            if (candidate / "sym-lib-table").is_file() or (
                candidate / "fp-lib-table"
            ).is_file():
                return candidate
        if versioned:
            return versioned[0]
    return None


def _ensure_lib_table_entry(
    table_path: Path, table_kind: str, nickname: str, uri: str
) -> List[str]:
    """Add ``nickname`` -> ``uri`` to a KiCad lib-table if not already there."""
    actions: List[str] = []
    entry = (
        f'  (lib (name "{nickname}")(type "KiCad")(uri "{uri}")'
        f'(options "")(descr "LCSC parts imported by kicad-lcsc-suite"))\n'
    )

    if not table_path.is_file():
        table_path.parent.mkdir(parents=True, exist_ok=True)
        table_path.write_text(
            f"({table_kind}\n  (version 7)\n{entry})\n", encoding="utf-8"
        )
        actions.append(f"Created {table_path.name} with library '{nickname}'.")
        return actions

    try:
        content = table_path.read_text(encoding="utf-8")
    except OSError as exc:
        actions.append(f"Could not read {table_path}: {exc}")
        return actions

    nickname_re = re.escape(nickname)
    if re.search(rf'\(name\s+"?{nickname_re}"?\s*\)', content):
        actions.append(f"Library '{nickname}' already registered in {table_path.name}.")
        return actions

    closing = content.rfind(")")
    if closing == -1:
        actions.append(f"{table_path.name} looks malformed; left it alone.")
        return actions

    # Back up before rewriting a file KiCad owns. Copies written before the
    # rebrand are named `.lcsc-suite.bak` and are not renamed or cleaned up:
    # this is somebody's only copy of a lib table, and the one thing worse than
    # an inconsistent suffix is a restore path that goes looking for a file
    # under a name it was never saved as.
    backup = table_path.with_suffix(table_path.suffix + ".easyassembly.bak")
    try:
        shutil.copyfile(table_path, backup)
    except OSError:
        logger.debug("could not back up %s", table_path)

    updated = content[:closing] + entry + content[closing:]
    try:
        table_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        actions.append(f"Could not write {table_path}: {exc}")
        return actions
    actions.append(
        f"Registered library '{nickname}' in {table_path.name} (backup: {backup.name})."
    )
    return actions
