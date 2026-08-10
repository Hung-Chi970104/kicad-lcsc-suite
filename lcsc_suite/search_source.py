"""Where the Explorer's data comes from — the live endpoints, or a fixture.

Everything the Explorer knows about the outside world arrives through one of
these objects. There are two, and which one is in use decides whether the window
touches the network at all:

:class:`LiveSource`
    Delegates straight to ``lcsc/api.py``. What the app runs.

:class:`FixtureSource`
    Replays a captured set of raw payloads. What ``qt_probe.py`` and the tests
    run, and they must never reach the wire.

This follows the precedent Phase 2 set with ``Library(allow_network=False)``: the
seam is a parameter on the way in, defaulting to the live behaviour, rather than
a flag consulted deep inside. The reason it exists is narrower than "CI has no
network" — CI plainly has one, it installs PySide6 over it. It is that
``qt-screens.yml`` asserts the committed PNGs match what renders, and a grid
built from live search results renders differently every run: stock figures are
the most volatile numbers on the screen and three columns show them. That gate
would fail permanently. See the migration plan's Phase 4 concerns.

**The fixture replays raw payloads through api.py's own parsers.** It does not
carry ``SearchHit`` objects or any other post-processed shape. That is the whole
point of it: a fixture built from the shapes we *think* the API returns is a
stand-in more permissive than the thing it stands for, which is exactly what
trap 4 punished. Here, a change to how ``api.py`` reads a field changes what the
fixture produces, because it is the same code doing the reading.

The offline guarantee is structural rather than a matter of timing. ``api.py``'s
two fetch functions — ``_get_json`` and ``fetch_image`` — both consult the cache
first and the host breaker second, *before* any socket is opened. So priming the
cache with the captured payloads and installing a breaker that reports every
host tripped open leaves no path to the network: a captured URL is served from
the fixture, and an uncaptured one takes the module's own "nobody answered"
branch and renders as ``?``. Neither spelling required editing ``api.py``, which
is copied, not edited.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from typing import Any, Optional

from .shared import lcsc_api as api, lcsc_details as details

log = logging.getLogger(__name__)

#: The captured fixture, written by ``scripts/capture_explorer_fixture.py``.
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
EXPLORER_FIXTURE = os.path.join(FIXTURE_DIR, "explorer", "payloads.json.gz")


class _NeverExpires:
    """A stand-in for ``api._TTLCache`` that holds its entries forever.

    The real one expires after five minutes so stock figures stay fresh. A
    fixture has no freshness to lose, and an expiry would mean a probe run that
    took longer than the TTL started reaching for the network halfway through —
    an offline guarantee that depends on the wall clock is not one.
    """

    def __init__(self, payloads: dict[str, Any]) -> None:
        self._data = dict(payloads)

    def get(self, key: str) -> Optional[Any]:
        """Return the captured payload for ``key``, or ``None``."""
        return self._data.get(key)

    def put(self, key: str, value: Any) -> None:
        """Accept a computed entry — ``jlc_search`` writes its own back."""
        self._data[key] = value

    def clear(self) -> None:
        """Do nothing.

        ``api.clear_cache()`` is the Refresh button, and dropping the fixture
        would leave the window with nothing to show and no way to get it back.
        Refresh against a fixture honestly means "re-query and get the same
        answer", which is what keeping the entries produces.
        """


class _AllHostsBlocked:
    """A stand-in for ``api._HostBreaker`` that refuses every host.

    Quacks like the real one in the four ways ``api.py`` uses it. This is the
    offline guarantee: both fetch functions check ``blocked()`` after the cache
    and before the socket, so anything the capture missed fails locally, at zero
    cost, down the same path a geo-blocked host takes in production.
    """

    def blocked(self, url: str) -> bool:
        """Report every host as tripped open."""
        del url
        return True

    def record_failure(self, url: str) -> None:
        """Ignore — nothing here can fail over the wire."""

    def record_success(self, url: str) -> None:
        """Ignore — nothing here can succeed over the wire."""

    def reset(self) -> None:
        """Ignore. Re-arming a host that does not exist changes nothing."""


class LiveSource:
    """The real endpoints. Every call goes to ``lcsc/api.py`` unchanged."""

    #: Whether callers should describe themselves as working from a fixture.
    offline = False

    def search(self, keyword: str, page_size: int = 100, part_type=None):
        """Keyword-search the JLC parts library, returning ``(total, hits)``."""
        return api.jlc_search(keyword, page_size=page_size, part_type=part_type)

    def retail_stock(self, lcsc: str) -> Optional[int]:
        """Return LCSC retail stock, or ``None`` when nobody answered."""
        return api.retail_stock(lcsc)

    def retail_unreachable(self) -> bool:
        """Report whether every retail source is currently refusing us."""
        return api.retail_unreachable()

    def stock_report(self, lcsc: str, needed_qty: int = 1):
        """Reconcile both storefronts for one part."""
        return api.stock_report(lcsc, needed_qty=needed_qty)

    def assembly_detail(self, lcsc: str) -> dict:
        """Return JLC's raw assembly record for one part.

        The Explorer works from :meth:`stock_report`, which is the reconciled
        view of both storefronts. The Part Details dialog (§5.6) wants the
        record itself: the fields it lists — the component code, the full name,
        the assembly process, the minimum quantity and its price, and both price
        ladders — are on this payload and are deliberately *not* on
        ``StockReport``, which carries availability rather than identity.

        One request, not two: ``stock_report`` has already asked for this under
        the same cache key if the Explorer ran, and the dialog's photo ids are
        on here as well.
        """
        return api.jlc_assembly_detail(lcsc)

    def image(self, url: str) -> Optional[bytes]:
        """Download an image, or ``None``."""
        return api.fetch_image(url)

    def part_details(self, lcsc: str) -> dict:
        """Resolve one part's Type / Stock / Params / price, or ``{}``.

        The main window's background cache fill (§5.1's three API columns) gets
        its answers here. ``lcsc/details.py`` is what does the resolving; going
        through the source means the fill obeys the same offline seam as
        everything else the app fetches, rather than being the one path that
        reaches the wire from a window built against a fixture.
        """
        return details.fetch_details(lcsc)

    def cad_data(self, lcsc: str) -> dict:
        """Return EasyEDA's CAD record, which the SVG previews render from.

        The same payload ``EasyedaApi.get_cad_data_of_component`` returns: that
        method is ``easyeda_product``'s ``result`` under a different URL
        spelling. Going through ``api.py`` instead of that client means
        one transport, one cache and one host breaker for the whole window.
        """
        return api.easyeda_product(lcsc)

    def clear_cache(self) -> None:
        """Drop cached responses and re-arm every host — the Refresh button."""
        api.clear_cache()


class FixtureSource(LiveSource):
    """Replays a captured payload set, and cannot reach the network.

    Subclasses :class:`LiveSource` deliberately: every method is inherited, so
    the fixture exercises the same call into ``api.py`` that the app makes, and
    a method added to the live source cannot be silently missing here. The only
    thing this constructor changes is what ``api.py`` finds when it looks in its
    cache — and what it finds when it asks whether the host is reachable.
    """

    offline = True

    def __init__(self, path: str = EXPLORER_FIXTURE) -> None:
        self.path = path
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            document = json.load(handle)
        self.keyword: str = document.get("keyword", "")
        self.page_size: int = int(document.get("page_size", 100))
        self._payloads: dict[str, Any] = document.get("payloads", {})
        self._images: dict[str, str] = document.get("images", {})
        self._images_dir = os.path.join(os.path.dirname(path), "images")
        self.install()

    def install(self) -> None:
        """Point ``api.py``'s cache and host breaker at this fixture.

        Module-level assignment rather than an edit to ``api.py``: the rule is
        that the network layer is copied, not edited, and swapping what two of
        its module globals refer to is something a caller may do.
        """
        api._cache = _NeverExpires(self._payloads)  # noqa: SLF001
        api._breaker = _AllHostsBlocked()  # noqa: SLF001
        api._image_cache = self._load_images()  # noqa: SLF001

    def _load_images(self) -> dict[str, Optional[bytes]]:
        """Read the captured thumbnails into ``api``'s image cache.

        Read eagerly rather than on demand. The whole set is a megabyte or so,
        it is read once per process, and the alternative — a lazy loader in the
        cache's place — would put file I/O on whichever thread happened to ask
        first, which for the grid fill is a worker thread.
        """
        images: dict[str, Optional[bytes]] = {}
        for url, name in self._images.items():
            try:
                with open(os.path.join(self._images_dir, name), "rb") as handle:
                    images[url] = handle.read()
            except OSError:
                # A missing file is "this part has no photo", which is a state
                # the grid already draws. It is not worth failing a screenshot.
                log.debug("fixture image %s missing", name)
                images[url] = None
        return images

    def search(self, keyword: str, page_size: int = 100, part_type=None):
        """Answer from the capture only, never through the fallback client.

        The one place the offline guarantee had a hole, and a test found it.
        ``api.jlc_search`` posts to JLC directly and, **if that yields nothing**,
        falls back to the ``easyeda2kicad`` client — which carries its
        own transport and so never passes the host breaker at all. A keyword the
        capture does not hold therefore went straight out to the network, from a
        source whose whole contract is that it cannot.

        Calling the direct half by name fixes it and costs no duplication:
        ``_jlc_search_direct`` computes the same cache key the capture is primed
        under, so a captured search is served by the real parser and an
        uncaptured one returns ``(0, [])`` — which the window already renders as
        "No parts found", the same as any other unanswered request.
        """
        return api._jlc_search_direct(keyword, 1, page_size, part_type)  # noqa: SLF001

    def retail_unreachable(self) -> bool:
        """Report whether the capture holds no retail figures at all.

        Overridden rather than inherited, and the reason is worth recording
        because the first version of this class got it wrong and a screenshot
        caught it. ``api.retail_unreachable()`` asks the host breaker, and the
        breaker here refuses *everything* — so the live implementation reported
        both retail sources down, the Explorer skipped the fill it was about to
        run entirely, and every row in the LCSC retail column rendered ``…``
        under a status line reading "both lcsc.com and easyeda.com are refusing
        requests".

        The breaker is still right to refuse everything: it is the transport
        block, and an uncaptured URL genuinely is unreachable from here. But
        "can this process open a socket" and "does this source have retail data"
        are two different questions, and only the second one is what the window
        is asking. The capture has all 100 rows, so the answer is no.
        """
        return not any("wmsc.lcsc.com" in key for key in self._payloads)

    def part_details(self, lcsc: str) -> dict:
        """Answer nothing, because the capture cannot answer this one safely.

        The same hole :meth:`search` documents, one level up.
        ``details.fetch_details`` calls ``api.jlc_search``, whose
        ``easyeda2kicad`` fallback carries its own transport and so never passes
        the host breaker — so a number the capture does not hold would go
        straight out to the network from a source whose whole contract is that
        it cannot.

        Returning ``{}`` costs the fixture nothing it had: the probe seeds the
        part cache from ``fixtures/part_details.json`` through
        ``parts.open_fixture_library``, so the three API columns in the
        committed screenshots are filled before this pass ever runs — and a pass
        that finds them fresh queues no lookups at all.
        """
        del lcsc
        return {}

    def clear_cache(self) -> None:
        """Re-install the fixture, the way Refresh re-queries the endpoints."""
        self.install()

    def hits(self):
        """Return the captured result set — the search, parsed. For tests."""
        return self.search(self.keyword, page_size=self.page_size)


def build_source(offline: bool = False, fixture: str = EXPLORER_FIXTURE):
    """Return the source the Explorer should use.

    ``offline`` is the probe's and the tests' switch. It defaults to the live
    behaviour so that forgetting it cannot silently give a user a window full of
    somebody else's capacitors.
    """
    if offline:
        return FixtureSource(fixture)
    return LiveSource()


__all__ = [
    "EXPLORER_FIXTURE",
    "FixtureSource",
    "LiveSource",
    "build_source",
]
