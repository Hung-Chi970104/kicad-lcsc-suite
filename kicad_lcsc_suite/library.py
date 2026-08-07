"""Handle the JLCPCB parts database."""

import contextlib
import csv
from enum import Enum
import logging
import os
from pathlib import Path
import sqlite3
from threading import Lock, Thread
import time
from typing import NamedTuple, Optional

import requests  # pylint: disable=import-error

from .dblib import DEFAULT_LIBRARY, LIBRARY_CONFIGS
from .events import (
    DownloadCompletedEvent,
    DownloadProgressEvent,
    DownloadStartedEvent,
    MessageEvent,
    post,
)
from .sqlite_helpers import dict_factory
from .unzip_parts import unzip_parts

#: Plugin root. Duplicated from ``helpers.PLUGIN_PATH`` rather than imported:
#: ``helpers`` pulls in wx, and this module has to be importable from the Qt
#: app's interpreter, which has none. Both resolve to this directory.
PLUGIN_PATH = Path(__file__).resolve().parent


class PartsDatabaseInfo(NamedTuple):
    """Information about the parts database."""

    last_update: str
    size: int
    part_count: int


class LibraryState(Enum):
    """The various states of the library."""

    INITIALIZED = 0
    DOWNLOAD_RUNNING = 2


#: How long a cached part-detail row is considered fresh. Stock is the only
#: field that really moves, and a day-old figure is still a useful signal —
#: whereas a *missing* one leaves the part list blank. Stale rows are served
#: immediately and refreshed in the background, which is what makes the plugin
#: work offline: the last thing we learned about a part is always available.
PART_CACHE_TTL_SECONDS = 24 * 60 * 60

#: Column order of the part-detail cache. Mirrors
#: ``lcsc.details.DETAIL_FIELDS`` so a cached row and a freshly fetched one are
#: the same mapping to every consumer.
PART_CACHE_FIELDS = (
    "lcsc",
    "stock",
    "type",
    "part_no",
    "description",
    "package",
    "category",
    "price",
)


class Library:
    """A storage class to get data from a sqlite database and write it back."""

    def __init__(self, parent, allow_network=True):
        self.logger = logging.getLogger(__name__)
        self.parent = parent
        #: Whether construction may reach the network. Only ``check_library``'s
        #: one-off corrections seed does, and only on a machine that has never
        #: had a global corrections database. The wx plugin always could, so
        #: this defaults to the behaviour it has always had; the Qt app turns it
        #: off until the phase that owns the Corrections dialog.
        self.allow_network = allow_network
        self.datadir = ""
        self.selected_library = DEFAULT_LIBRARY
        self.partsdb_file = ""
        self.rotationsdb_file = ""
        self.localcorrectionsdb_file = ""
        self.globalcorrectionsdb_file = ""
        self.correctionsdb_file = ""
        self.mappingsdb_file = ""
        self.partcachedb_file = ""
        self.has_bulk_database = False
        self.state = None
        self.download_lock = Lock()
        self.category_map = {}

        self.refresh_library_config()

        self.logger.debug("partsdb_file %s", self.partsdb_file)
        self.logger.debug("sqlite.sqlite_version %s", sqlite3.sqlite_version)

    def _resolve_data_directory(self):
        """Resolve the directory where global database files are stored."""
        configured = self.parent.settings.get("library", {}).get("data_path", "")
        if isinstance(configured, str) and configured.strip():
            return os.path.abspath(os.path.expanduser(configured.strip()))
        return os.path.join(PLUGIN_PATH, "jlcpcb")

    def refresh_library_config(self):
        """Refresh library configuration from settings."""
        self.datadir = self._resolve_data_directory()

        # Get selected library from settings, default to all-parts
        selected_library = self.parent.settings.get("library", {}).get(
            "selected_library", DEFAULT_LIBRARY
        )
        if selected_library not in LIBRARY_CONFIGS:
            selected_library = DEFAULT_LIBRARY

        self.selected_library = selected_library
        library_config = LIBRARY_CONFIGS[selected_library]
        self.partsdb_file = os.path.join(self.datadir, library_config.name)
        self.rotationsdb_file = os.path.join(self.datadir, "rotations.db")
        self.localcorrectionsdb_file = os.path.join(
            self.parent.project_path, "jlcpcb", "project.db"
        )
        self.globalcorrectionsdb_file = os.path.join(self.datadir, "corrections.db")
        self.correctionsdb_file = (
            self.globalcorrectionsdb_file
            if self.uses_global_correction_database()
            else self.localcorrectionsdb_file
        )
        self.mappingsdb_file = os.path.join(self.datadir, "mappings.db")
        self.partcachedb_file = os.path.join(self.datadir, "partcache.db")
        self.category_map = {}

        self.setup()
        self.check_library()
        self.create_part_cache_table()

        self.logger.debug(
            "Library configuration refreshed. Selected: %s, Data directory: %s, Database: %s",
            self.selected_library,
            self.datadir,
            self.partsdb_file,
        )

    def setup(self):
        """Check if folders and database exist, setup if not."""
        if not os.path.isdir(self.datadir):
            self.logger.info(
                "Data directory '%s' does not exist and will be created.", self.datadir
            )
            Path(self.datadir).mkdir(parents=True, exist_ok=True)
        else:
            self.logger.info("Data directory '%s' exists, not creating", self.datadir)

    def check_library(self):
        """Check which databases exist and set up the small ones that don't.

        The bulk parts database is **optional**. Per-part details now come from
        the API with a local cache behind them (see ``lcsc/details.py``), so an
        absent parts DB is a missing offline catalogue, not a broken install —
        it must not block start-up or trigger a three-quarter-gigabyte download
        nobody asked for.

        The one thing here that *can* reach the network is the corrections seed,
        and only when no global corrections database exists yet. It is gated on
        ``allow_network`` so a caller that has no business making requests — a
        test, a screenshot probe — cannot silently make one.
        """
        self.has_bulk_database = (
            os.path.isfile(self.partsdb_file) and os.path.getsize(self.partsdb_file) > 0
        )
        self.state = LibraryState.INITIALIZED
        corrections_file_missing = not os.path.isfile(self.correctionsdb_file)
        if corrections_file_missing or os.path.getsize(self.correctionsdb_file) == 0:
            self.create_correction_table()
            self.migrate_corrections()
            if (
                self.allow_network
                and corrections_file_missing
                and self.correctionsdb_file == self.globalcorrectionsdb_file
            ):
                db_path = self.globalcorrectionsdb_file
                Thread(
                    target=self.fetch_remote_corrections,
                    args=(db_path,),
                    daemon=True,
                ).start()
        if (
            not os.path.isfile(self.mappingsdb_file)
            or os.path.getsize(self.mappingsdb_file) == 0
        ):
            self.create_mapping_table()
            self.migrate_mappings()

    def uses_global_correction_database(self):
        """Check if there is a board specific corrections database or not.

        Returns True if the global database is used.
        """

        try:
            with (
                contextlib.closing(
                    sqlite3.connect(self.localcorrectionsdb_file)
                ) as ldb,
                ldb as lcur,
            ):
                result = lcur.execute(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='correction')"
                ).fetchone()
                if not result:
                    return True

                return result[0] != 1
        except sqlite3.OperationalError:
            return True

        return True

    def switch_to_global_correction_database(self, use_global):
        """Switches to global or board local database."""

        currently_using_global = (
            self.correctionsdb_file == self.globalcorrectionsdb_file
        )
        if currently_using_global == use_global:
            return

        if use_global:
            try:
                with (
                    contextlib.closing(
                        sqlite3.connect(self.localcorrectionsdb_file)
                    ) as con,
                    con as cur,
                ):
                    cur.execute("DROP TABLE IF EXISTS correction")
                    cur.commit()
                self.correctionsdb_file = self.globalcorrectionsdb_file
            except OSError:
                self.logger.warning("Failed to remove board local corrections file.")
        else:
            global_corrections = self.get_all_correction_data()
            self.correctionsdb_file = self.localcorrectionsdb_file
            self.create_correction_table()
            for regex, rotation, offset in global_corrections:
                self.insert_correction_data(regex, rotation, offset)

    def delete_parts_table(self):
        """Delete the parts table."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            cur.execute("DROP TABLE IF EXISTS parts")
            cur.commit()

    def create_meta_table(self):
        """Create the meta table."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS meta ('filename', 'size', 'partcount', 'date', 'last_update')"
            )
            cur.commit()

    def create_correction_table(self):
        """Create the correction table."""
        self.logger.debug("Create SQLite table for corrections")
        with (
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                "CREATE TABLE IF NOT EXISTS correction ('regex', 'rotation', 'offset_x', 'offset_y')"
            )
            cur.commit()

    def get_correction_data(self, regex, db_path=None):
        """Get the correction data by its regex."""
        target = db_path if db_path is not None else self.correctionsdb_file
        with (
            contextlib.closing(sqlite3.connect(target)) as con,
            con as cur,
        ):
            return cur.execute(
                f"SELECT * FROM correction WHERE regex = '{regex}'"
            ).fetchone()

    def delete_correction_data(self, regex):
        """Delete a correction from the database."""
        with (
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as con,
            con as cur,
        ):
            cur.execute(f"DELETE FROM correction WHERE regex = '{regex}'")
            cur.commit()

    def update_correction_data(self, regex, rotation, offset):
        """Update a correction in the database."""
        with (
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                f"UPDATE correction SET rotation = '{rotation}', offset_x = '{offset[0]}', offset_y = '{offset[1]}' WHERE regex = '{regex}'"
            )
            cur.commit()

    def insert_correction_data(self, regex, rotation, offset, db_path=None):
        """Insert a correction into the database."""
        target = db_path if db_path is not None else self.correctionsdb_file
        with (
            contextlib.closing(sqlite3.connect(target)) as con,
            con as cur,
        ):
            cur.execute(
                "INSERT INTO correction VALUES (?, ?, ?, ?)",
                (regex, rotation, offset[0], offset[1]),
            )
            cur.commit()

    def get_all_correction_data(self):
        """Get all corrections from the database."""
        with (
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as con,
            con as cur,
        ):
            try:
                result = cur.execute(
                    "SELECT * FROM correction ORDER BY regex ASC"
                ).fetchall()
                return [(c[0], int(c[1]), (float(c[2]), float(c[3]))) for c in result]
            except sqlite3.OperationalError:
                return []

    def create_mapping_table(self):
        """Create the mapping table."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                "CREATE TABLE IF NOT EXISTS mapping ('footprint', 'value', 'LCSC')"
            )
            cur.commit()

    def get_mapping_data(self, footprint, value):
        """Get the mapping data by its regex."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            return cur.execute(
                f"SELECT * FROM mapping WHERE footprint = '{footprint}' AND value = '{value}'"
            ).fetchone()

    def delete_mapping_data(self, footprint, value):
        """Delete a mapping from the database."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                f"DELETE FROM mapping WHERE footprint = '{footprint}' AND value = '{value}'"
            )
            cur.commit()

    def update_mapping_data(self, footprint, value, LCSC):
        """Update a mapping in the database."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                f"UPDATE mapping SET LCSC = '{LCSC}' WHERE footprint = '{footprint}' AND value = '{value}'"
            )
            cur.commit()

    def insert_mapping_data(self, footprint, value, LCSC):
        """Insert a mapping into the database."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            cur.execute(
                "INSERT INTO mapping VALUES (?, ?, ?)",
                (footprint, value, LCSC),
            )
            cur.commit()

    def get_all_mapping_data(self):
        """Get all mapping from the database."""
        with (
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as con,
            con as cur,
        ):
            return [
                list(c)
                for c in cur.execute(
                    "SELECT * FROM mapping ORDER BY footprint ASC"
                ).fetchall()
            ]

    def create_parts_table(self, columns):
        """Create the parts table."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            cols = ",".join([f" '{c}'" for c in columns])
            cur.execute(f"CREATE TABLE IF NOT EXISTS parts ({cols})")
            cur.commit()

    def get_part_details(self, number: str) -> dict:
        """Resolve one part's details, cheapest source first, without blocking.

        Called once per assigned part while the footprint list is being built,
        on the UI thread, so it must not touch the network. The ordering is:

        1. the local API cache, however old — a stale figure beats a blank row,
           and serving it unconditionally is what makes an offline session
           behave;
        2. the bulk parts database, when the user has downloaded one;
        3. nothing, which the caller must read as "not looked up yet".

        Refreshing stale and missing entries is the background refresher's job
        (``mainwindow.start_part_detail_refresh``), not this method's.
        """
        cached = self.get_cached_part_details(number)
        if cached:
            return cached
        return self.get_bulk_part_details(number)

    def get_bulk_part_details(self, number: str) -> dict:
        """Get part details from the downloaded parts DB using FTS5 querying."""
        if not self.has_bulk_database:
            return {}
        try:
            with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con:
                con.row_factory = dict_factory
                cur = con.cursor()
                query = """SELECT "LCSC Part" AS lcsc, "Stock" AS stock, "Library Type" AS type,
                    "MFR.Part" as part_no, "Description" as description, "Package" as package,
                    "First Category" as category, "Price" as price
                    FROM parts WHERE parts MATCH :number"""
                cur.execute(query, {"number": number})
                return next((n for n in cur.fetchall() if n["lcsc"] == number), {})
        except sqlite3.Error as exc:
            # A half-written download leaves a file that exists but has no
            # `parts` table. That is a missing catalogue, not a crash.
            self.logger.debug("Bulk part lookup for %s failed: %s", number, exc)
            return {}

    # ------------------------------------------------------------------
    # API part-detail cache
    # ------------------------------------------------------------------

    def create_part_cache_table(self):
        """Create the API part-detail cache table if it does not exist."""
        columns = ",".join(
            f"'{field}'" + (" NOT NULL PRIMARY KEY" if field == "lcsc" else "")
            for field in PART_CACHE_FIELDS
        )
        with (
            contextlib.closing(sqlite3.connect(self.partcachedb_file)) as con,
            con as cur,
        ):
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS part_cache ({columns}, fetched_at NUMERIC)"
            )
            cur.commit()

    def get_cached_part_details(self, number: str) -> dict:
        """Return the cached details for ``number``, ignoring age.

        Age is deliberately not a filter here — see
        :func:`get_part_details`. ``fetched_at`` is dropped from the result so
        a cached row is indistinguishable from a bulk-database one.
        """
        if not number:
            return {}
        try:
            with contextlib.closing(sqlite3.connect(self.partcachedb_file)) as con:
                con.row_factory = dict_factory
                row = (
                    con.cursor()
                    .execute(
                        "SELECT * FROM part_cache WHERE lcsc = :lcsc",
                        {"lcsc": number},
                    )
                    .fetchone()
                )
        except sqlite3.Error as exc:
            self.logger.debug("Part cache read for %s failed: %s", number, exc)
            return {}
        if not row:
            return {}
        row.pop("fetched_at", None)
        return row

    def set_cached_part_details(self, details: dict):
        """Upsert one part's details into the cache, stamped as fetched now."""
        lcsc = str(details.get("lcsc") or "")
        if not lcsc:
            return
        row = {field: details.get(field, "") for field in PART_CACHE_FIELDS}
        row["lcsc"] = lcsc
        row["fetched_at"] = int(time.time())
        placeholders = ",".join(f":{field}" for field in PART_CACHE_FIELDS)
        assignments = ",".join(
            f"{field} = :{field}" for field in PART_CACHE_FIELDS if field != "lcsc"
        )
        try:
            with (
                contextlib.closing(sqlite3.connect(self.partcachedb_file)) as con,
                con as cur,
            ):
                cur.execute(
                    f"INSERT INTO part_cache ({','.join(PART_CACHE_FIELDS)}, fetched_at) "
                    f"VALUES ({placeholders}, :fetched_at) "
                    f"ON CONFLICT(lcsc) DO UPDATE SET {assignments}, "
                    "fetched_at = :fetched_at",
                    row,
                )
                cur.commit()
        except sqlite3.Error as exc:
            self.logger.debug("Part cache write for %s failed: %s", lcsc, exc)

    def get_part_numbers_needing_refresh(self, numbers) -> list:
        """Return which of ``numbers`` are absent from the cache or stale."""
        wanted = [str(number) for number in dict.fromkeys(numbers) if number]
        if not wanted:
            return []
        cutoff = int(time.time()) - PART_CACHE_TTL_SECONDS
        try:
            with contextlib.closing(sqlite3.connect(self.partcachedb_file)) as con:
                # The f-string only injects ?-placeholders; the LCSC numbers
                # themselves are bound, never interpolated.
                placeholders = ",".join("?" for _ in wanted)
                rows = (
                    con.cursor()
                    .execute(
                        f"SELECT lcsc FROM part_cache WHERE lcsc IN ({placeholders}) "
                        "AND fetched_at IS NOT NULL AND fetched_at >= ?",
                        [*wanted, cutoff],
                    )
                    .fetchall()
                )
        except sqlite3.Error as exc:
            self.logger.debug("Part cache staleness query failed: %s", exc)
            return wanted
        fresh = {row[0] for row in rows}
        return [number for number in wanted if number not in fresh]

    def update(self):
        """Update the sqlite parts database from the JLCPCB CSV."""
        with self.download_lock:
            if self.state == LibraryState.DOWNLOAD_RUNNING:
                self.logger.info(
                    "Download already running, ignoring duplicate request."
                )
                return
            self.state = LibraryState.DOWNLOAD_RUNNING
        try:
            Thread(target=self._download_wrapper).start()
        except Exception:
            with self.download_lock:
                self.state = LibraryState.INITIALIZED
            raise

    def _download_wrapper(self):
        """Run the download worker with guaranteed state cleanup."""
        try:
            self.download()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.logger.exception("Unexpected error while downloading parts database")
            post(
                self.parent,
                MessageEvent(
                    title="Download Error",
                    text=f"Unexpected error while downloading parts database: {exc}",
                    style="error",
                ),
            )
        finally:
            with self.download_lock:
                self.state = LibraryState.INITIALIZED

    def download(self):
        """Actual worker thread that downloads and imports the parts data."""
        start = time.time()
        post(self.parent, DownloadStartedEvent())

        # Get library configuration for selected library
        library_config = LIBRARY_CONFIGS[self.selected_library]

        # Define basic variables
        url_stub = "https://bouni.github.io/kicad-jlcpcb-tools/"
        cnt_file = library_config.chunk_file_name
        progress_file = os.path.join(self.datadir, f"{library_config.name}.progress")
        chunk_file_stub = library_config.name.replace(".db", ".db.zip.")
        completed_chunks = set()

        self.logger.debug("Starting download of JLCPCB parts database...")
        self.logger.debug(
            "Using library: %s (basefile %s)",
            self.selected_library,
            chunk_file_stub,
        )

        # Check if there is a progress file
        if os.path.exists(progress_file):
            with open(progress_file) as f:
                # Read completed chunk indices from the progress file
                completed_chunks = {int(line.strip()) for line in f.readlines()}

        # Get the total number of chunks to download
        try:
            r = requests.get(
                url_stub + cnt_file, allow_redirects=True, stream=True, timeout=300
            )
            if r.status_code != requests.codes.ok:
                post(
                    self.parent,
                    MessageEvent(
                        title="HTTP GET Error",
                        text=f"Failed to fetch count of database parts, error code {r.status_code}\n"
                        + "URL was:\n"
                        f"'{url_stub + cnt_file}'",
                        style="error",
                    ),
                )
                self.state = LibraryState.INITIALIZED
                return

            total_chunks = int(r.text)
        except Exception as e:
            post(
                self.parent,
                MessageEvent(
                    title="Download Error",
                    text=f"Failed to fetch database chunk count, {e}",
                    style="error",
                ),
            )
            self.state = LibraryState.INITIALIZED
            return

        # Re-download incomplete or missing chunks
        for i in range(total_chunks):
            chunk_index = i + 1
            chunk_file = chunk_file_stub + f"{chunk_index:03}"
            chunk_path = os.path.join(self.datadir, chunk_file)

            # Check if the chunk is logged as completed but the file might be incomplete
            if chunk_index in completed_chunks:
                if os.path.exists(chunk_path):
                    # Validate the size of the chunk file
                    try:
                        expected_size = int(
                            requests.head(
                                url_stub + chunk_file, timeout=300
                            ).headers.get("Content-Length", 0)
                        )
                        actual_size = os.path.getsize(chunk_path)
                        if actual_size == expected_size:
                            self.logger.debug(
                                "Skipping already downloaded and validated chunk %d.",
                                chunk_index,
                            )
                            continue
                        else:
                            self.logger.warning(
                                "Chunk %d is incomplete, re-downloading.", chunk_index
                            )
                    except Exception as e:
                        self.logger.warning(
                            "Unable to validate chunk %d, re-downloading. Error: %s",
                            chunk_index,
                            e,
                        )
                else:
                    self.logger.warning(
                        "Chunk %d marked as completed but file is missing, re-downloading.",
                        chunk_index,
                    )

            # Download the chunk
            try:
                with open(chunk_path, "wb") as f:
                    r = requests.get(
                        url_stub + chunk_file,
                        allow_redirects=True,
                        stream=True,
                        timeout=300,
                    )
                    if r.status_code != requests.codes.ok:
                        post(
                            self.parent,
                            MessageEvent(
                                title="Download Error",
                                text=f"Failed to download chunk {chunk_index}, error code {r.status_code}\n"
                                + "URL was:\n"
                                f"'{url_stub + chunk_file}'",
                                style="error",
                            ),
                        )
                        self.state = LibraryState.INITIALIZED
                        return

                    size = int(r.headers.get("Content-Length", 0))
                    self.logger.debug(
                        "Downloading chunk %d/%d (%.2f MB)",
                        chunk_index,
                        total_chunks,
                        size / 1024 / 1024,
                    )
                    for data in r.iter_content(chunk_size=4096):
                        f.write(data)
                        progress = f.tell() / size * 100
                        post(self.parent, DownloadProgressEvent(value=progress))
                    self.logger.debug("Chunk %d downloaded successfully.", chunk_index)

                # Update progress file after successful download
                with open(progress_file, "a") as f:
                    f.write(f"{chunk_index}\n")

            except Exception as e:
                post(
                    self.parent,
                    MessageEvent(
                        title="Download Error",
                        text=f"Failed to download chunk {chunk_index}, {e}",
                        style="error",
                    ),
                )
                self.state = LibraryState.INITIALIZED
                return

        # Delete progress file to indicate the download is complete
        if os.path.exists(progress_file):
            os.remove(progress_file)

        # Combine and extract downloaded files
        self.logger.debug("Combining and extracting zip part files...")
        try:
            unzip_parts(self.parent, self.datadir, library_config.name + ".zip")
        except Exception as e:
            post(
                self.parent,
                MessageEvent(
                    title="Extract Error",
                    text=f"Failed to combine and extract the JLCPCB database, {e}",
                    style="error",
                ),
            )
            self.state = LibraryState.INITIALIZED
            return

        # Check if the database file was successfully extracted
        if not os.path.exists(self.partsdb_file):
            post(
                self.parent,
                MessageEvent(
                    title="Download Error",
                    text="Failed to extract the database file from the downloaded zip.",
                    style="error",
                ),
            )
            self.state = LibraryState.INITIALIZED
            return

        post(self.parent, DownloadCompletedEvent())
        end = time.time()
        post(
            self.parent,
            MessageEvent(
                title="Success",
                text=f"Successfully downloaded and imported the JLCPCB database in {end - start:.2f} seconds!",
                style="info",
            ),
        )
        self.state = LibraryState.INITIALIZED

    def create_tables(self, headers):
        """Create all tables."""
        self.create_meta_table()
        self.delete_parts_table()
        self.create_parts_table(headers)
        self.create_correction_table()
        self.create_mapping_table()

    @property
    def categories(self):
        """The primary categories in the database.

        Caching the relatively small set of category and subcategory maps
        gives a noticeable speed improvement over repeatedly reading the
        information from the on-disk database.
        """
        if not self.category_map:
            self.category_map.setdefault("", [])

            # Populate the cache.
            with (
                contextlib.closing(sqlite3.connect(self.partsdb_file)) as con,
                con as cur,
            ):
                for row in cur.execute(
                    'SELECT * from categories ORDER BY UPPER("First Category"), UPPER("Second Category")'
                ):
                    self.category_map.setdefault(row[0], []).append(row[1])
        tmp = list(self.category_map.keys())
        tmp.insert(0, "All")
        return tmp

    def get_subcategories(self, category):
        """Get the subcategories associated with the given category."""
        return self.category_map[category]

    def migrate_corrections_from_rotation(self):
        """Migrate existing rotations from rotation db to correction db."""
        if not os.path.exists(self.rotationsdb_file):
            return
        with (
            contextlib.closing(sqlite3.connect(self.rotationsdb_file)) as rdb,
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as cdb,
            rdb as rcur,
            cdb as ccur,
        ):
            try:
                result = rcur.execute(
                    "SELECT * FROM rotation ORDER BY regex ASC"
                ).fetchall()
                if not result:
                    return
                for r in result:
                    ccur.execute(
                        "INSERT INTO correction VALUES (?, ?, 0, 0)",
                        (r[0], r[1]),
                    )
                    ccur.commit()
                self.logger.debug(
                    "Migrated %d rotations to corrections database.", len(result)
                )
                os.remove(self.rotationsdb_file)
                self.logger.debug("Deleted rotations database.")
            except sqlite3.OperationalError:
                return
            except OSError:
                return

    def migrate_corrections_from_parts(self):
        """Migrate existing rotations from parts db to correction db."""
        with (
            contextlib.closing(sqlite3.connect(self.partsdb_file)) as pdb,
            contextlib.closing(sqlite3.connect(self.correctionsdb_file)) as rdb,
            pdb as pcur,
            rdb as rcur,
        ):
            try:
                result = pcur.execute(
                    "SELECT * FROM rotation ORDER BY regex ASC"
                ).fetchall()
                if not result:
                    return
                for r in result:
                    rcur.execute(
                        "INSERT INTO correction VALUES (?, ?, 0, 0)",
                        (r[0], r[1]),
                    )
                    rcur.commit()
                self.logger.debug(
                    "Migrated %d rotations to separate database.", len(result)
                )
                pcur.execute("DROP TABLE IF EXISTS rotation")
                pcur.commit()
                self.logger.debug("Droped rotations table from parts database.")
            except sqlite3.OperationalError:
                return

    def migrate_corrections(self):
        """Migrate existing rotations from old rotation db and parts db to correction db."""
        self.migrate_corrections_from_rotation()
        self.migrate_corrections_from_parts()

    def fetch_remote_corrections(self, db_path=None):
        """Download rotation corrections from Matthew Lai's JLCKicadTools repo."""
        target = db_path if db_path is not None else self.correctionsdb_file
        url = "https://raw.githubusercontent.com/matthewlai/JLCKicadTools/master/jlc_kicad_tools/cpl_rotations_db.csv"
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            corrections = csv.reader(r.text.splitlines(), delimiter=",", quotechar='"')
            next(corrections)
            for row in corrections:
                if len(row) < 2:
                    continue
                if not self.get_correction_data(row[0], db_path=target):
                    offset = (row[2], row[3]) if len(row) >= 4 else (0, 0)
                    self.insert_correction_data(row[0], row[1], offset, db_path=target)
            self.logger.info("Downloaded corrections to %s.", target)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.logger.debug("Failed to download corrections to %s: %s", target, exc)

    def migrate_mappings(self):
        """Migrate existing mappings from parts db to mappings db."""
        with (
            contextlib.closing(sqlite3.connect(self.partsdb_file)) as pdb,
            contextlib.closing(sqlite3.connect(self.mappingsdb_file)) as mdb,
            pdb as pcur,
            mdb as mcur,
        ):
            try:
                result = pcur.execute(
                    "SELECT * FROM mapping ORDER BY footprint ASC"
                ).fetchall()
                if not result:
                    return
                for r in result:
                    mcur.execute(
                        "INSERT INTO mapping VALUES (?, ?)",
                        (r[0], r[1]),
                    )
                    mcur.commit()
                self.logger.debug(
                    "Migrated %d mappings to sepetrate database.", len(result)
                )
                pcur.execute("DROP TABLE IF EXISTS mapping")
                pcur.commit()
                self.logger.debug("Droped mappings table from parts database.")
            except sqlite3.OperationalError:
                return

    def get_parts_db_info(self) -> Optional[PartsDatabaseInfo]:  # noqa: UP045
        """Retrieve the database information."""
        with contextlib.closing(sqlite3.connect(self.partsdb_file)) as con, con as cur:
            try:
                meta = cur.execute(
                    "SELECT last_update, size, partcount FROM meta"
                ).fetchone()
                if meta:
                    return PartsDatabaseInfo(meta[0], meta[1], meta[2])
                return None
            except sqlite3.OperationalError:
                return None
