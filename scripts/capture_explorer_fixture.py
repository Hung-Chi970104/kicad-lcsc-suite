#!/usr/bin/env python3
"""Capture the LCSC Explorer's search fixture from the live endpoints. ONE SHOT.

**Run this by hand, once, and then leave it alone.** It is committed so the
fixture's provenance is auditable and so it can be re-captured deliberately if
the payload shape ever changes — not because it is part of any workflow. It
refuses to start without ``--capture-once`` for exactly that reason.

Why the fixture exists at all: ``.github/workflows/qt-screens.yml`` asserts that
the committed PNGs match what renders, and a results grid built from live search
results renders differently every run — the stock figures are the most volatile
values on the screen and three columns show them. That gate would fail
permanently. See the migration plan's Phase 4 concerns.

The rules this follows, all of them from the standing reachability note:

* **Capture once, never on a loop.** ``wmsc.lcsc.com`` refuses this network more
  often than not and EasyEDA's CloudFront bans it outright on a burst. A script
  that retries is the thing that costs the next session its access, so every
  request here is paced and every phase gives up after a short run of failures
  rather than pushing through them.
* **Capture raw.** Payloads are stored exactly as the endpoints returned them,
  keyed by the URL ``api.py`` would request them under. Nothing is parsed on the
  way in, so a later change to how ``api.py`` reads a field needs no re-capture,
  and the fixture cannot encode our *guess* at a shape — which is the mistake
  trap 4 punished.
* **Capture the thumbnails.** The grid's rows are 108px tall and empty without
  them. One id per search row, from JLC's file service, so this costs image
  bytes and no extra JSON lookups.

    .venv/bin/python scripts/capture_explorer_fixture.py --capture-once

Writes ``lcsc_suite/fixtures/explorer/payloads.json.gz`` plus ``images/*.jpg``.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from typing import Any, Optional
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lcsc_suite.shared import lcsc_api as api  # noqa: E402

#: What §5.2's reference screenshots searched for, down to the result count line
#: they show. A wide result set with a full parametric spread is the point: the
#: facet panel has nothing to display if every hit shares every attribute.
KEYWORD = "10nF 0402"
PAGE_SIZE = 100

FIXTURE_DIR = os.path.join(_ROOT, "lcsc_suite", "fixtures", "explorer")

#: Per-phase pacing, in seconds between requests. These are deliberately slow.
#: The whole capture takes a couple of minutes and happens once in the project's
#: life; a ban costs days.
PACE_ASSEMBLY = 0.25
PACE_RETAIL = 0.5
PACE_IMAGE = 0.15
PACE_EASYEDA = 1.0

#: Consecutive failures after which a phase stops. A host that has refused this
#: many in a row has decided something about us, and the next request will not
#: change its mind — it will only make the refusal last longer.
GIVE_UP_AFTER = 5

#: Full-size product photos are ~65 kB each and the photo viewer shows one at a
#: time, so only the first few rows get one.
BIG_PHOTO_ROWS = 3

#: EasyEDA is the retail *fallback* — only consulted when wmsc.lcsc.com stays
#: silent. A handful of parts is enough to exercise that path, and a handful is
#: also all its rate limiter will tolerate.
EASYEDA_ROWS = 6


def fetch_json(url: str, headers: dict, data: Optional[bytes] = None) -> Any:
    """GET or POST ``url`` and return parsed JSON, or ``None`` on any failure.

    Deliberately *not* ``api._get_json``: that one caches, consults the host
    breaker and flattens every failure to ``{}``. Here a failure has to be
    visible so the pacing logic can stop, and the payload has to arrive
    unparsed.
    """
    try:
        req = urllib.request.Request(url=url, headers=headers, data=data)  # noqa: S310
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=30, context=api.ssl_context()
        ) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        print(f"    failed: {exc}")
        return None


def fetch_bytes(url: str) -> Optional[bytes]:
    """Download ``url``'s bytes, or ``None`` on any failure."""
    try:
        req = urllib.request.Request(url=url, headers=api.HEADERS)  # noqa: S310
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=30, context=api.ssl_context()
        ) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"    failed: {exc}")
        return None


def capture_search() -> tuple[str, Any]:
    """POST the keyword search and return ``(cache_key, raw_payload)``.

    The key is the one ``api._jlc_search_direct`` computes for its own cache, so
    priming a cache with it is enough to make ``api.jlc_search`` answer from the
    fixture with its real parser doing the work.
    """
    payload = {"keyword": KEYWORD, "currentPage": 1, "pageSize": PAGE_SIZE}
    key = "jlc_search:" + json.dumps(payload, sort_keys=True)
    print(f"search: {KEYWORD!r} page 1, {PAGE_SIZE} per page")
    raw = fetch_json(
        api.JLC_SEARCH_API,
        api.JLC_SEARCH_HEADERS,
        json.dumps(payload).encode("utf-8"),
    )
    return key, raw


def paced(
    label: str,
    items: list,
    pace: float,
    handler,
) -> int:
    """Run ``handler`` over ``items``, paced, stopping after a run of failures.

    Returns the number of successes. Prints one line per item so a capture that
    is being refused is obvious while it happens rather than afterwards.
    """
    consecutive = 0
    kept = 0
    for index, item in enumerate(items):
        if index:
            time.sleep(pace)
        ok = handler(item)
        if ok:
            kept += 1
            consecutive = 0
        else:
            consecutive += 1
            if consecutive >= GIVE_UP_AFTER:
                print(
                    f"  {label}: {GIVE_UP_AFTER} refusals in a row — stopping "
                    f"here with {kept}/{len(items)}. This is not an error; it "
                    f"is the rule about not pushing through a refusal."
                )
                break
    print(f"  {label}: kept {kept}/{len(items)}")
    return kept


def main(argv=None) -> int:
    """Capture the fixture."""
    parser = argparse.ArgumentParser(
        prog="capture_explorer_fixture.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--capture-once",
        action="store_true",
        help="required; confirms you mean to spend a live capture",
    )
    parser.add_argument("--out", default=FIXTURE_DIR, help="fixture directory")
    args = parser.parse_args(argv)

    if not args.capture_once:
        parser.error(
            "refusing to run without --capture-once. This script spends real "
            "requests against hosts that rate-limit and geo-block; the fixture "
            "it writes is already committed. Re-capture only deliberately."
        )

    images_dir = os.path.join(args.out, "images")
    os.makedirs(images_dir, exist_ok=True)

    payloads: dict[str, Any] = {}

    # --- 1. The search itself. Everything else keys off its hits. ----------
    search_key, search_raw = capture_search()
    if not search_raw:
        print(
            "\nThe search returned nothing. Nothing else can be captured "
            "without it, so this run is a no-op — the existing fixture is "
            "untouched. Try again later; do not loop."
        )
        return 1
    payloads[search_key] = search_raw

    total, hits = api.jlc_search(KEYWORD, page_size=PAGE_SIZE)
    print(f"  {len(hits)} hits, {total} total matches")
    if not hits:
        print("  parsed to zero hits — the payload shape has changed. Stopping.")
        return 1

    # --- 2. JLC assembly detail, per part. ---------------------------------
    # The friendlier of the two hosts, and the source of library type, minimum
    # purchase, attrition and the assembly price ladder that the detail pane
    # shows. Also the multi-angle imageList the photo viewer uses.
    print("\nJLC assembly detail (cart.jlcpcb.com)")

    def grab_assembly(hit) -> bool:
        url = api.JLC_ASSEMBLY_DETAIL.format(urllib.parse.quote(hit.lcsc))
        raw = fetch_json(url, api.HEADERS)
        if raw is None:
            return False
        payloads[url] = raw
        return True

    paced("assembly", hits, PACE_ASSEMBLY, grab_assembly)

    # --- 3. LCSC retail detail, per part. ----------------------------------
    # The inventory that disagrees with the one above, which is the whole reason
    # the Inventory selector exists. Slowest pacing: this is the host that
    # refuses whole networks.
    print("\nLCSC retail detail (wmsc.lcsc.com)")

    def grab_retail(hit) -> bool:
        url = api.LCSC_RETAIL_DETAIL.format(urllib.parse.quote(hit.lcsc))
        raw = fetch_json(url, api.HEADERS)
        if raw is None:
            return False
        payloads[url] = raw
        return True

    paced("retail", hits, PACE_RETAIL, grab_retail)

    # --- 4. EasyEDA, for the fallback path. --------------------------------
    print("\nEasyEDA product (the retail fallback)")

    def grab_easyeda(hit) -> bool:
        url = api.EASYEDA_PRODUCT.format(urllib.parse.quote(hit.lcsc))
        raw = fetch_json(url, api.EASYEDA_HEADERS)
        if raw is None:
            return False
        payloads[url] = raw
        return True

    paced("easyeda", hits[:EASYEDA_ROWS], PACE_EASYEDA, grab_easyeda)

    # --- 5. Thumbnails, and a few full-size photos. ------------------------
    print("\nThumbnails (JLC file service)")
    manifest: dict[str, str] = {}

    def grab_image(spec) -> bool:
        url, name = spec
        data = fetch_bytes(url)
        if not data:
            return False
        with open(os.path.join(images_dir, name), "wb") as handle:
            handle.write(data)
        manifest[url] = name
        return True

    wanted: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, hit in enumerate(hits):
        for access_id in (
            hit.image_id,
            hit.big_image_id if index < BIG_PHOTO_ROWS else "",
        ):
            url = api.jlc_image_url(access_id)
            if url and url not in seen:
                seen.add(url)
                wanted.append((url, f"{access_id}.jpg"))
    paced("images", wanted, PACE_IMAGE, grab_image)

    # --- 6. Write it out. ---------------------------------------------------
    document = {
        "keyword": KEYWORD,
        "page_size": PAGE_SIZE,
        "search_key": search_key,
        "captured_from": {
            "search": api.JLC_SEARCH_API,
            "assembly": api.JLC_ASSEMBLY_DETAIL,
            "retail": api.LCSC_RETAIL_DETAIL,
            "easyeda": api.EASYEDA_PRODUCT,
            "images": api.JLC_FILE_DOWNLOAD,
        },
        "images": manifest,
        "payloads": payloads,
    }
    # Gzipped, and not as a space micro-optimisation: the raw retail payloads
    # are ~83 kB each and the uncompressed document is 9 MB, which is not a
    # sensible thing to put in a working tree. Compressed it is 416 kB — smaller
    # than several of the committed screenshots — and *complete*, so the "capture
    # raw, never re-capture to fix a parse change" rule survives intact. Trimming
    # the payloads down to the fields we currently read would have been the other
    # way to shrink it, and it is the wrong one: that is post-processing, and a
    # fixture carrying only the fields we thought mattered is the trap again.
    #
    # ``mtime=0`` so re-serialising an unchanged capture yields an identical
    # file rather than a spurious diff.
    target = os.path.join(args.out, "payloads.json.gz")
    with gzip.GzipFile(target, "wb", compresslevel=9, mtime=0) as handle:
        handle.write(
            json.dumps(
                document, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )

    size = os.path.getsize(target)
    print(
        f"\nWrote {os.path.relpath(target, _ROOT)} "
        f"({size / 1024:.0f} kB, {len(payloads)} payloads) and "
        f"{len(manifest)} images."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
