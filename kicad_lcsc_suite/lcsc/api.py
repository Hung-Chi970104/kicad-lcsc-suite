"""Unified access to the JLCPCB and LCSC endpoints.

Three distinct sources, deliberately kept separate because they do not agree:

* **JLC assembly**  ``cart.jlcpcb.com`` — what the SMT assembly service can
  actually place on a board. Carries library type (Basic/Preferred/Extended),
  minimum purchase quantity and the attrition ("loss") count.
* **LCSC retail**   ``wmsc.lcsc.com`` — what you can buy loose as a component
  order, split into domestic/overseas warehouses, plus the real parametric
  attribute list that drives LCSC's filter sidebar.
* **JLC search**    the parts-library keyword search (via the vendored
  easyeda2kicad client). Returns parametric attributes in bulk, which the
  per-part detail endpoints cannot do.

A part can have huge assembly stock and zero retail stock, or the reverse.
Never collapse the two into one "stock" number — :class:`StockReport` keeps
both and explains the difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import ssl
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

JLC_ASSEMBLY_DETAIL = (
    "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={}"
)
LCSC_RETAIL_DETAIL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={}"

#: EasyEDA's product record. Its ``szlcsc`` block carries LCSC *retail* stock,
#: price and minimum buy — the same figures :data:`LCSC_RETAIL_DETAIL` serves,
#: from a host that is reachable when ``lcsc.com`` is not. Used as the retail
#: fallback; see :func:`retail_snapshot`.
EASYEDA_PRODUCT = "https://easyeda.com/api/products/{}/components?version=6.4.19.5"

#: JLC's file service. Product photos are addressed by an opaque "access id"
#: that the search and assembly-detail payloads hand out, not by a URL — see
#: :func:`jlc_image_url`.
JLC_FILE_DOWNLOAD = "https://jlcpcb.com/api/file/downloadByFileSystemAccessId/{}"

#: The parts-library keyword search. The vendored easyeda2kicad client calls
#: the same endpoint but discards the image access ids on the way out, so this
#: module posts to it directly and keeps the client as a fallback.
JLC_SEARCH_API = (
    "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/"
    "selectSmtComponentList"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": _UA, "Accept": "application/json, text/plain, */*"}

#: EasyEDA sits behind CloudFront, which 403s a request whose User-Agent does
#: not look like a browser — a short ``Mozilla/5.0`` is not enough, the full
#: string is. It also rate-limits, which is what :class:`_HostBreaker` is for.
EASYEDA_HEADERS = dict(HEADERS, Referer="https://easyeda.com/")

JLC_SEARCH_HEADERS = dict(
    HEADERS,
    **{
        "Content-Type": "application/json",
        "Origin": "https://jlcpcb.com",
        "Referer": "https://jlcpcb.com/parts",
    },
)

# Stock moves, so cached values must expire. Parametric attributes do not
# really move, but they ride along in the same payload; a few minutes is a
# reasonable compromise between UI snappiness and freshness.
CACHE_TTL_SECONDS = 300

_ssl_context: Optional[ssl.SSLContext] = None
_ssl_lock = threading.Lock()


#: CA bundles shipped by common Linux distributions, tried when neither
#: certifi nor the interpreter's default trust store yields any certificates.
_CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Alpine, Arch
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL, CentOS
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/ssl/cert.pem",  # Alpine, FreeBSD, macOS via Homebrew
)


def _context_has_certs(context: ssl.SSLContext) -> bool:
    """Report whether ``context`` actually loaded some trust anchors."""
    try:
        return bool(context.get_ca_certs())
    except Exception:  # noqa: BLE001 - not all builds implement this
        return True


def ssl_context() -> ssl.SSLContext:
    """Return a validating SSL context that works on every supported platform.

    KiCad's bundled macOS Python has no populated system trust store, so a
    plain ``urlopen`` raises CERTIFICATE_VERIFY_FAILED. Distro Pythons on
    Linux usually do have one, and Windows loads from the cert store. Try, in
    order: an explicitly configured bundle, certifi, the interpreter default,
    and finally well-known distribution bundles.

    Verification is never disabled — if no trust anchors can be found the
    default context is returned and requests fail loudly rather than
    silently talking to an unverified peer.
    """
    global _ssl_context  # noqa: PLW0603 - process-wide cached singleton
    with _ssl_lock:
        if _ssl_context is not None:
            return _ssl_context

        # 1. Explicit override, honouring the usual environment variables.
        for var in ("LCSC_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            path = os.environ.get(var)
            if path and os.path.isfile(path):
                try:
                    _ssl_context = ssl.create_default_context(cafile=path)
                    logger.debug("Using CA bundle from %s: %s", var, path)
                    return _ssl_context
                except Exception:  # noqa: BLE001
                    logger.debug("CA bundle from %s unusable: %s", var, path)

        # 2. certifi, which KiCad bundles on macOS and Windows.
        try:
            # Deferred: certifi is optional, and absence is a handled path.
            import certifi  # noqa: PLC0415  # pylint: disable=import-error

            _ssl_context = ssl.create_default_context(cafile=certifi.where())
            logger.debug("Using certifi CA bundle at %s", certifi.where())
            return _ssl_context
        except Exception:  # noqa: BLE001 - certifi absent or broken
            logger.debug("certifi unavailable")

        # 3. The interpreter default — correct on Linux and Windows.
        default = ssl.create_default_context()
        if _context_has_certs(default):
            logger.debug("Using the interpreter default CA store")
            _ssl_context = default
            return _ssl_context

        # 4. Distribution bundles, for a stripped Python with no default store.
        for path in _CA_BUNDLE_CANDIDATES:
            if os.path.isfile(path):
                try:
                    _ssl_context = ssl.create_default_context(cafile=path)
                    logger.debug("Using system CA bundle at %s", path)
                    return _ssl_context
                except Exception:  # noqa: BLE001
                    continue

        logger.warning(
            "No CA trust store found — HTTPS requests to LCSC/JLCPCB will "
            "fail. Install certifi, or point LCSC_CA_BUNDLE at a CA bundle."
        )
        _ssl_context = default
        return _ssl_context


class _TTLCache:
    """Small thread-safe TTL cache."""

    def __init__(self, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._data: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            stamp, value = hit
            if (time.time() - stamp) > self._ttl:
                self._data.pop(key, None)
                return None
            return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_cache = _TTLCache()


class _HostBreaker:
    """Stops hammering a host that is refusing everything.

    A blocked or rate-limited storefront answers instantly with a 403, so
    nothing times out and nothing looks slow — the explorer just quietly fires
    one doomed request per row. Filling a 120-row grid against a host that has
    geo-blocked the user costs 120 round trips to learn the same fact 120
    times, and on a rate-limiter it is what turns a soft throttle into a hard
    ban.

    So consecutive hard failures trip the host open for a cooldown, during
    which its requests fail locally at zero cost. One success closes it again,
    which is what makes this self-healing: whatever the block was — a WAF
    rule, a rate limit, a flight through an airport's captive portal — the
    feature comes back on its own once the host does.
    """

    def __init__(self, threshold: int = 3, cooldown: float = 600.0) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._lock = threading.Lock()
        self._failures: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}

    @staticmethod
    def _host(url: str) -> str:
        return urllib.parse.urlsplit(url).netloc

    def blocked(self, url: str) -> bool:
        """Report whether requests to ``url``'s host should be skipped."""
        host = self._host(url)
        with self._lock:
            until = self._open_until.get(host)
            if until is None:
                return False
            if time.time() >= until:
                # Cooldown elapsed: let exactly one request through to probe.
                self._open_until.pop(host, None)
                self._failures[host] = 0
                return False
            return True

    def record_failure(self, url: str) -> None:
        """Count a hard failure, tripping the host open at the threshold."""
        host = self._host(url)
        with self._lock:
            count = self._failures.get(host, 0) + 1
            self._failures[host] = count
            if count >= self._threshold and host not in self._open_until:
                self._open_until[host] = time.time() + self._cooldown
                logger.warning(
                    "%s refused %d requests in a row — pausing it for %d minutes. "
                    "Affected data will show as '?' until it recovers.",
                    host,
                    count,
                    int(self._cooldown // 60),
                )

    def record_success(self, url: str) -> None:
        """Clear a host's failure history."""
        host = self._host(url)
        with self._lock:
            if self._failures.get(host) or host in self._open_until:
                self._failures[host] = 0
                self._open_until.pop(host, None)

    def reset(self) -> None:
        """Forget every host's state — the Refresh button's job."""
        with self._lock:
            self._failures.clear()
            self._open_until.clear()


_breaker = _HostBreaker()


def host_blocked(url_or_host: str) -> bool:
    """Report whether ``url_or_host`` is currently tripped open.

    Lets the UI say "unreachable" rather than "no stock" for a whole column.
    """
    if "//" not in url_or_host:
        url_or_host = "https://" + url_or_host
    return _breaker.blocked(url_or_host)


def clear_cache() -> None:
    """Drop cached API responses — used by the explorer's Refresh button.

    Also re-arms every tripped host: Refresh is the user saying "try again",
    and the most likely reason they pressed it is that they just fixed their
    connection.

    Product photos are not dropped: they are cached by URL and an image URL is
    immutable, so re-downloading them would cost bandwidth and change nothing.
    """
    _cache.clear()
    _breaker.reset()


def _get_json(
    url: str,
    timeout: int = 25,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
) -> Dict[str, Any]:
    """GET (or POST, given ``data``) a URL and parse JSON, ``{}`` on failure."""
    cached = _cache.get(url) if data is None else None
    if cached is not None:
        return cached
    if _breaker.blocked(url):
        logger.debug("Skipping %s: host is in cooldown", url)
        return {}
    try:
        req = urllib.request.Request(  # noqa: S310
            url=url, headers=headers or HEADERS, data=data
        )
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=timeout, context=ssl_context()
        ) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        # A refusal (403/429) and a parse failure both mean "this host is not
        # answering usefully"; only the breaker cares about the difference
        # between one of them and thirty.
        logger.debug("Fetch %s failed: %r", url, exc)
        _breaker.record_failure(url)
        return {}
    _breaker.record_success(url)
    if not isinstance(payload, dict):
        return {}
    if data is None:
        _cache.put(url, payload)
    return payload


def normalize_lcsc(value: str) -> str:
    """Normalise user input into a canonical ``C1234`` LCSC id."""
    text = (value or "").strip().upper()
    if text and not text.startswith("C"):
        text = "C" + text
    return text


# ---------------------------------------------------------------------------
# JLC assembly
# ---------------------------------------------------------------------------


def jlc_assembly_detail(lcsc: str) -> Dict[str, Any]:
    """Return the JLC SMT-assembly record for ``lcsc`` (``{}`` if unknown)."""
    lcsc = normalize_lcsc(lcsc)
    if not lcsc:
        return {}
    payload = _get_json(JLC_ASSEMBLY_DETAIL.format(urllib.parse.quote(lcsc)))
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# LCSC retail
# ---------------------------------------------------------------------------


def lcsc_retail_detail(lcsc: str) -> Dict[str, Any]:
    """Return the LCSC retail record for ``lcsc`` (``{}`` if unknown)."""
    lcsc = normalize_lcsc(lcsc)
    if not lcsc:
        return {}
    payload = _get_json(LCSC_RETAIL_DETAIL.format(urllib.parse.quote(lcsc)))
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def easyeda_product(lcsc: str) -> Dict[str, Any]:
    """Return EasyEDA's product record for ``lcsc`` (``{}`` if unknown)."""
    lcsc = normalize_lcsc(lcsc)
    if not lcsc:
        return {}
    payload = _get_json(
        EASYEDA_PRODUCT.format(urllib.parse.quote(lcsc)), headers=EASYEDA_HEADERS
    )
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def easyeda_retail(lcsc: str) -> Dict[str, Any]:
    """Return LCSC retail figures for ``lcsc`` as seen by EasyEDA.

    EasyEDA embeds a ``szlcsc`` block — LCSC's own pre-rename identity — in
    every product record, carrying live retail stock, unit price and minimum
    buy. It is the same warehouse :func:`lcsc_retail_detail` reports on, which
    is what makes it a legitimate stand-in when ``lcsc.com`` will not answer.

    What it does *not* carry is the parametric attribute list, the price
    ladder or the domestic/overseas split, so this is a narrower answer than
    the retail endpoint's — enough for the grid's stock column, not enough to
    fill the detail pane on its own.
    """
    product = easyeda_product(lcsc)
    block = product.get("szlcsc") if product else None
    if not isinstance(block, dict):
        return {}
    return {
        "stock": _as_int(block.get("stock")),
        "price": _as_float(block.get("price")),
        "min_buy": _as_int(block.get("min")),
        "url": str(block.get("url") or ""),
    }


def retail_stock(lcsc: str) -> Optional[int]:
    """Return the LCSC retail stock figure for ``lcsc``, from whoever answers.

    Tries the retail endpoint first because it is authoritative and carries
    the rest of the detail pane's data in the same response, then falls back
    to EasyEDA's copy of the same figure. ``None`` means nobody answered —
    which is *not* the same as zero, and callers must not render it as such.
    """
    retail = lcsc_retail_detail(lcsc)
    if retail:
        return _as_int(retail.get("stockNumber"))
    return easyeda_retail(lcsc).get("stock")


def retail_unreachable() -> bool:
    """Report whether *every* retail source is currently refusing us.

    :func:`retail_stock` answers ``None`` both for a part LCSC has never heard
    of and for a part it would not answer about at all, and the two must not be
    drawn the same way: a whole column of the latter reads as "nothing is in
    stock" when the truth is "nobody would tell us". Both hosts tripped open at
    once is that second case, and it is not hypothetical — ``wmsc.lcsc.com``
    geo-blocks outright in places, and EasyEDA's CloudFront answers a burst of
    per-row lookups with a 403 for every subsequent request.

    Callers should stop filling and say so, rather than record one ``None`` per
    row. See :class:`_HostBreaker`.
    """
    return all(host_blocked(url) for url in (LCSC_RETAIL_DETAIL, EASYEDA_PRODUCT))


# ---------------------------------------------------------------------------
# Product photos
# ---------------------------------------------------------------------------


def jlc_image_url(access_id: Any) -> str:
    """Return the download URL for a JLC file-service access id.

    JLC addresses product photos by an opaque numeric id rather than a path,
    and hands those ids out in both the search results and the assembly
    detail. The bytes come back as JPEG regardless of the ``Content-Type``
    the service claims.
    """
    text = str(access_id or "").strip()
    if not text or text.lower() in ("none", "null", "0"):
        return ""
    return JLC_FILE_DOWNLOAD.format(urllib.parse.quote(text))


def assembly_photo_urls(lcsc: str, big: bool = True) -> List[str]:
    """Return every product photo URL JLC holds for ``lcsc``.

    The assembly detail's ``imageList`` is the multi-angle set — front, back,
    packaging, reel — where a search result carries only the primary shot.
    Worth one request when the user has asked to look at the photos, and
    wasteful for anything less.
    """
    key = "productBigImageAccessId" if big else "productImageAccessId"
    urls: List[str] = []
    assembly = jlc_assembly_detail(lcsc)
    for entry in assembly.get("imageList") or []:
        if not isinstance(entry, dict):
            continue
        url = jlc_image_url(entry.get(key))
        if url and url not in urls:
            urls.append(url)
    if not urls:
        # Single-photo parts populate only the top-level ids.
        fallback = jlc_image_url(
            assembly.get("productBigImageAccessId" if big else "minImageAccessId")
        )
        if fallback:
            urls.append(fallback)
    return urls


def retail_thumbnail_url(lcsc: str) -> str:
    """Return a small product photo URL for ``lcsc``, or ``""`` if it has none.

    Prefers whatever the retail endpoint already gave us — its response is in
    the shared cache if the stock fill ran — and otherwise asks JLC.

    Callers filling a whole grid should use ``SearchHit.image_id`` and only
    come here for the rows where it is empty. That is not a rare case worth
    ignoring: search results for a microcontroller are routinely half
    photo-less, and better than half of those do have pictures filed under
    the assembly record's ``imageList`` instead. It costs a request per such
    row, which is why it is the fallback and not the path.
    """
    retail = lcsc_retail_detail(lcsc)
    if retail:
        urls = retail_images(retail)
        if urls:
            return urls[0]
    urls = assembly_photo_urls(lcsc, big=False)
    return urls[0] if urls else ""


#: LCSC serves each product photo at several fixed sizes and encodes the
#: dimension in the path. The detail payload hands out the 900x900 original,
#: which is ~65 kB — far more than a preview tile needs.
_FULL_IMAGE_SIZE = "900x900"
THUMBNAIL_SIZE = "224x224"


def retail_images(retail: Dict[str, Any], size: str = THUMBNAIL_SIZE) -> List[str]:
    """Return product photo URLs from an LCSC record, resized to ``size``.

    Pass ``size=None`` to keep the full-resolution URLs.
    """
    urls: List[str] = []
    for url in retail.get("productImages") or []:
        if not isinstance(url, str) or not url:
            continue
        urls.append(url.replace(_FULL_IMAGE_SIZE, size) if size else url)
    return urls


#: Photos never change for a given URL, so they are cached for the process
#: lifetime rather than the five-minute stock TTL. Bounded so a long session
#: of browsing cannot grow without limit — but comfortably above one grid's
#: worth of thumbnails, or scrolling back to a previous search would refetch
#: every one of them.
_MAX_CACHED_IMAGES = 256
_image_cache: Dict[str, Optional[bytes]] = {}
_image_lock = threading.Lock()


def fetch_image(url: str, timeout: int = 15) -> Optional[bytes]:
    """Download an image, returning ``None`` on any failure.

    Deliberately separate from :func:`_get_json` — images are bytes, are
    cached under a different policy, and are the lowest-priority thing the
    explorer fetches. A missing photo is never worth an error dialog.
    """
    if not url:
        return None
    with _image_lock:
        if url in _image_cache:
            return _image_cache[url]
    if _breaker.blocked(url):
        return None

    data: Optional[bytes] = None
    try:
        req = urllib.request.Request(url=url, headers=HEADERS)  # noqa: S310
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=timeout, context=ssl_context()
        ) as response:
            data = response.read()
    except (urllib.error.URLError, OSError) as exc:
        logger.debug("Image fetch %s failed: %r", url, exc)
        _breaker.record_failure(url)
    else:
        _breaker.record_success(url)

    # A failure is cached too, as ``None``: a part whose photo 404s should be
    # asked about once, not once per repaint.
    with _image_lock:
        if len(_image_cache) >= _MAX_CACHED_IMAGES:
            _image_cache.clear()
        _image_cache[url] = data
    return data


def retail_parameters(retail: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Extract ``(name, value)`` parametric attributes from an LCSC record."""
    out: List[Tuple[str, str]] = []
    for param in retail.get("paramVOList") or []:
        if not isinstance(param, dict):
            continue
        name = (param.get("paramNameEn") or "").strip()
        value = (param.get("paramValueEn") or "").strip()
        if name and value:
            out.append((name, value))
    return out


def assembly_price_ladder(assembly: Dict[str, Any]) -> List[Tuple[int, float]]:
    """Extract the ``(quantity, unit_price_usd)`` ladder from a JLC assembly record.

    This is the ladder that belongs in a BOM estimate for a JLC assembly order.
    LCSC retail prices are a different number for a different transaction and
    routinely disagree — the same distinction this module keeps for stock.

    Only the band's start quantity is returned; ``endNumber`` is dropped because
    JLC's bands are contiguous and the consumer derives each upper bound from
    the next band's start.
    """
    ladder: List[Tuple[int, float]] = []
    for entry in assembly.get("prices") or []:
        if not isinstance(entry, dict):
            continue
        try:
            ladder.append((int(entry["startNumber"]), float(entry["productPrice"])))
        except (KeyError, TypeError, ValueError):
            continue
    ladder.sort(key=lambda item: item[0])
    return ladder


def retail_price_ladder(retail: Dict[str, Any]) -> List[Tuple[int, float]]:
    """Extract the ``(quantity, unit_price_usd)`` ladder from an LCSC record."""
    ladder: List[Tuple[int, float]] = []
    for entry in retail.get("productPriceList") or []:
        if not isinstance(entry, dict):
            continue
        qty = entry.get("ladder")
        price = entry.get("usdPrice", entry.get("currencyPrice"))
        try:
            ladder.append((int(qty), float(price)))
        except (TypeError, ValueError):
            continue
    ladder.sort(key=lambda item: item[0])
    return ladder


def unit_price_at(ladder: List[Tuple[int, float]], quantity: int) -> Optional[float]:
    """Return the unit price that applies at ``quantity`` for a price ladder."""
    if not ladder:
        return None
    price = ladder[0][1]
    for break_qty, break_price in ladder:
        if quantity >= break_qty:
            price = break_price
        else:
            break
    return price


# ---------------------------------------------------------------------------
# Combined stock reporting — the point of this module
# ---------------------------------------------------------------------------

#: Relative gap between the two stock figures above which we call it out.
DIVERGENCE_THRESHOLD = 0.25


@dataclass
class StockReport:
    """Reconciled availability for one part across both storefronts."""

    lcsc: str
    jlc_stock: Optional[int] = None
    retail_stock: Optional[int] = None
    library_type: str = ""
    min_purchase: Optional[int] = None
    attrition: Optional[int] = None
    retail_min_buy: Optional[int] = None
    retail_domestic: Optional[int] = None
    retail_overseas: Optional[int] = None
    model: str = ""
    manufacturer: str = ""
    package: str = ""
    datasheet: str = ""
    parameters: List[Tuple[str, str]] = field(default_factory=list)
    retail_ladder: List[Tuple[int, float]] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    ok: bool = False

    @property
    def divergent(self) -> bool:
        """True when the two stock figures disagree by more than the threshold."""
        if not self.jlc_stock or not self.retail_stock:
            return False
        worst = max(self.jlc_stock, self.retail_stock)
        return abs(self.jlc_stock - self.retail_stock) / worst > DIVERGENCE_THRESHOLD

    def summary(self) -> str:
        """One-line availability summary for a status bar."""
        jlc = "?" if self.jlc_stock is None else f"{self.jlc_stock:,}"
        retail = "?" if self.retail_stock is None else f"{self.retail_stock:,}"
        return f"JLC assembly: {jlc}   |   LCSC retail: {retail}"


def stock_report(lcsc: str, needed_qty: int = 1) -> StockReport:
    """Fetch both storefronts for ``lcsc`` and reconcile them.

    ``needed_qty`` is the number of placements on the board, used to warn when
    stock or the JLC minimum purchase quantity would block an order.
    """
    lcsc = normalize_lcsc(lcsc)
    report = StockReport(lcsc=lcsc)
    if not lcsc:
        report.warnings.append("No LCSC part number given.")
        return report

    assembly = jlc_assembly_detail(lcsc)
    retail = lcsc_retail_detail(lcsc)
    # Only worth asking when the authoritative source stayed silent — it
    # answers a strict subset of the same questions.
    fallback = {} if retail else easyeda_retail(lcsc)
    report.ok = bool(assembly or retail or fallback)
    if not report.ok:
        report.warnings.append(
            "Part not found on either JLC assembly or LCSC retail (or both "
            "endpoints are unreachable)."
        )
        return report

    if assembly:
        report.jlc_stock = _as_int(assembly.get("stockCount"))
        report.library_type = str(assembly.get("componentLibraryType") or "")
        report.min_purchase = _as_int(assembly.get("minPurchaseNum"))
        report.attrition = _as_int(assembly.get("lossNumber"))

    if retail:
        report.retail_stock = _as_int(retail.get("stockNumber"))
        report.retail_min_buy = _as_int(retail.get("minBuyNumber"))
        report.model = str(retail.get("productModel") or "")
        report.manufacturer = str(retail.get("brandNameEn") or "")
        report.package = str(retail.get("encapStandard") or "")
        report.datasheet = str(retail.get("pdfUrl") or "")
        report.parameters = retail_parameters(retail)
        report.retail_ladder = retail_price_ladder(retail)
        report.images = retail_images(retail)
        domestic = retail.get("domesticStockVO")
        if isinstance(domestic, dict):
            report.retail_domestic = _as_int(domestic.get("total"))
        overseas = retail.get("overseasStockVO")
        if isinstance(overseas, dict):
            report.retail_overseas = _as_int(overseas.get("total"))
    elif fallback:
        report.retail_stock = fallback.get("stock")
        report.retail_min_buy = fallback.get("min_buy")
        price = fallback.get("price")
        if price is not None:
            # A single figure, not a ladder — flagged as such by having one
            # band, so the estimator does not read it as a volume price.
            report.retail_ladder = [(fallback.get("min_buy") or 1, price)]

    if not report.images:
        # The retail endpoint owns the photo set, but JLC serves the same
        # product shots through its file service and is reachable more often.
        report.images = assembly_photo_urls(lcsc)

    report.warnings.extend(_build_warnings(report, needed_qty))
    return report


def _as_int(value: Any) -> Optional[int]:
    """Coerce a JSON value to int, or None when it is not numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_warnings(report: StockReport, needed_qty: int) -> List[str]:
    """Derive human-readable availability warnings from a report."""
    out: List[str] = []
    jlc, retail = report.jlc_stock, report.retail_stock

    if jlc == 0 and retail == 0:
        out.append(
            "UNAVAILABLE — zero stock on both JLC assembly and LCSC retail. "
            "Pick a different part."
        )
    elif jlc == 0 and retail:
        out.append(
            f"Assembly-blocked — LCSC retail has {retail:,} but JLC assembly has 0, "
            "so JLC cannot place this part. Buy loose and hand-solder, or "
            "substitute."
        )
    elif retail == 0 and jlc:
        out.append(
            f"Assembly-only — JLC assembly has {jlc:,} but LCSC retail has 0, "
            "so you cannot buy spares loose for rework."
        )
    elif report.divergent and jlc and retail:
        ratio = max(jlc, retail) / max(1, min(jlc, retail))
        out.append(
            f"Stock figures diverge {ratio:.0f}x (assembly {jlc:,} vs retail "
            f"{retail:,}) — they are separate inventories; trust the one "
            "matching how you will order."
        )

    if jlc is not None and 0 < jlc < needed_qty:
        out.append(
            f"JLC assembly stock ({jlc:,}) is below the {needed_qty} placements "
            "on this board."
        )
    if report.min_purchase and report.min_purchase > 1:
        out.append(
            f"JLC minimum purchase is {report.min_purchase:,} pieces — you pay "
            "for the whole reel even if the board needs a handful."
        )
    if report.attrition:
        out.append(
            f"JLC adds {report.attrition} pieces attrition loss per order for "
            "this part."
        )
    if report.library_type and report.library_type.lower() not in ("base", "basic"):
        out.append(
            "Extended part — JLC charges a per-reel feeder setup fee on top of "
            "the component cost."
        )
    return out


# ---------------------------------------------------------------------------
# Bulk parametric search — the LCSC-style filter source
# ---------------------------------------------------------------------------


@dataclass
class SearchHit:
    """One result from the JLC parts-library keyword search."""

    lcsc: str
    model: str
    brand: str
    package: str
    category: str
    description: str
    stock: Optional[int]
    library_type: str
    min_qty: Optional[int]
    reel_qty: Optional[int]
    price: Optional[float]
    datasheet: str
    attributes: Dict[str, str] = field(default_factory=dict)
    #: JLC file-service ids for the part's primary photo, small and large.
    #: Free with the search response, which is the whole point: a grid of
    #: thumbnails costs no JSON requests at all, only the image bytes.
    image_id: str = ""
    big_image_id: str = ""

    @property
    def thumbnail_url(self) -> str:
        """URL of the small (96px) product photo, or ``""``."""
        return jlc_image_url(self.image_id)

    @property
    def photo_url(self) -> str:
        """URL of the large (900px) product photo, or ``""``."""
        return jlc_image_url(self.big_image_id) or self.thumbnail_url


def jlc_search(
    keyword: str,
    page: int = 1,
    page_size: int = 100,
    part_type: Optional[str] = None,
) -> Tuple[int, List[SearchHit]]:
    """Keyword-search the JLC parts library, returning ``(total, hits)``.

    Unlike the per-part detail endpoints this returns parametric attributes in
    bulk, which is what makes client-side facet filtering possible.
    ``part_type`` is ``"base"`` for Basic or ``"expand"`` for Extended.

    Posts to the endpoint directly, falling back to the vendored easyeda2kicad
    client. Both talk to the same URL; the direct call exists because the
    client's result mapper drops the product-photo access ids, and re-deriving
    those costs one extra request per row.
    """
    total, hits = _jlc_search_direct(keyword, page, page_size, part_type)
    if hits:
        return total, hits
    return _jlc_search_vendored(keyword, page, page_size, part_type)


def _jlc_search_direct(
    keyword: str,
    page: int,
    page_size: int,
    part_type: Optional[str],
) -> Tuple[int, List[SearchHit]]:
    """Search via a direct POST, keeping every field the payload carries."""
    payload: Dict[str, Any] = {
        "keyword": keyword,
        "currentPage": page,
        "pageSize": page_size,
    }
    if part_type:
        payload["componentLibraryType"] = part_type

    # POSTs bypass the URL cache, so key this one by hand — paging back and
    # forth through a result set is a common gesture and the responses are
    # large.
    cache_key = "jlc_search:" + json.dumps(payload, sort_keys=True)
    cached = _cache.get(cache_key)
    if cached is None:
        cached = _get_json(
            JLC_SEARCH_API,
            headers=JLC_SEARCH_HEADERS,
            data=json.dumps(payload).encode("utf-8"),
        )
        if cached:
            _cache.put(cache_key, cached)

    info = (cached.get("data") or {}).get("componentPageInfo") or {}
    items = info.get("list") or []
    if not isinstance(items, list):
        return 0, []

    hits: List[SearchHit] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        attributes: Dict[str, str] = {}
        for attr in item.get("attributes") or []:
            if not isinstance(attr, dict):
                continue
            name = (attr.get("attribute_name_en") or "").strip()
            value = (attr.get("attribute_value_name") or "").strip()
            # "-" is JLC's own placeholder for "not specified"; keeping it
            # would put a useless one-value facet in the filter bar.
            if name and value and value != "-":
                attributes[name] = value
        prices = item.get("componentPrices") or []
        first = prices[0] if prices and isinstance(prices[0], dict) else {}
        hits.append(
            SearchHit(
                lcsc=str(item.get("componentCode") or ""),
                model=str(item.get("componentModelEn") or ""),
                brand=str(item.get("componentBrandEn") or ""),
                package=str(item.get("componentSpecificationEn") or ""),
                category=str(item.get("componentTypeEn") or ""),
                description=str(item.get("describe") or ""),
                stock=_as_int(item.get("stockCount")),
                # The estimator compares this to "Extended" exactly; the wire
                # format is base/expand. Same mapping the vendored client does.
                library_type="Basic"
                if item.get("componentLibraryType") == "base"
                else "Extended",
                min_qty=_as_int(item.get("minPurchaseNum")),
                reel_qty=_as_int(item.get("encapsulationNumber")),
                price=_as_float(first.get("productPrice")),
                datasheet=str(item.get("dataManualUrl") or ""),
                attributes=attributes,
                image_id=str(item.get("minImageAccessId") or ""),
                big_image_id=str(item.get("productBigImageAccessId") or ""),
            )
        )
    return _as_int(info.get("total")) or 0, hits


def _jlc_search_vendored(
    keyword: str,
    page: int,
    page_size: int,
    part_type: Optional[str],
) -> Tuple[int, List[SearchHit]]:
    """Search via the vendored easyeda2kicad client — no photo ids."""
    try:
        # Deferred: the vendored copy is only on sys.path once the plugin
        # package has been imported.
        from easyeda2kicad.easyeda.easyeda_api import EasyedaApi  # noqa: PLC0415
    except ImportError:  # pragma: no cover - only when lib/ is missing
        logger.error("Vendored easyeda2kicad not importable; is lib/ on sys.path?")
        return 0, []

    try:
        raw = EasyedaApi().search_jlcpcb_components(
            keyword=keyword,
            page=page,
            page_size=page_size,
            part_type=part_type,
        )
    except Exception as exc:  # noqa: BLE001 - network layer is best-effort
        logger.error("JLC search failed: %r", exc)
        return 0, []

    total = _as_int(raw.get("total")) or 0
    hits: List[SearchHit] = []
    for item in raw.get("results") or []:
        if not isinstance(item, dict):
            continue
        attributes: Dict[str, str] = {}
        for attr in item.get("attributes") or []:
            if isinstance(attr, dict):
                name = (attr.get("name") or "").strip()
                value = (attr.get("value") or "").strip()
                if name and value:
                    attributes[name] = value
        hits.append(
            SearchHit(
                lcsc=str(item.get("lcsc") or ""),
                model=str(item.get("model") or ""),
                brand=str(item.get("brand") or ""),
                package=str(item.get("package") or ""),
                category=str(item.get("category") or ""),
                description=str(item.get("description") or ""),
                stock=_as_int(item.get("stock")),
                library_type=str(item.get("type") or ""),
                min_qty=_as_int(item.get("min_qty")),
                reel_qty=_as_int(item.get("reel_qty")),
                price=_as_float(item.get("price")),
                datasheet=str(item.get("datasheet") or ""),
                attributes=attributes,
            )
        )
    return total, hits


def _as_float(value: Any) -> Optional[float]:
    """Coerce a JSON value to float, or None when it is not numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_facets(hits: List[SearchHit]) -> Dict[str, List[Tuple[str, int]]]:
    """Build LCSC-style filter facets from a result set.

    Returns attribute name -> ``(value, count)`` pairs, keeping only
    attributes that actually discriminate (more than one distinct value).
    Values are ordered by how many hits carry them, then naturally, so the
    useful options sit at the top of the list.

    The counts are over the **fetched** result set as a whole, and stay fixed
    while the user ticks boxes. Recomputing them against the current
    selection — what LCSC's "results remaining" does — would mean rebuilding
    the filter controls on every toggle, including the one the user has open.
    """
    buckets: Dict[str, Dict[str, int]] = {}
    for hit in hits:
        for name, value in hit.attributes.items():
            buckets.setdefault(name, {})
            buckets[name][value] = buckets[name].get(value, 0) + 1

    facets: Dict[str, List[Tuple[str, int]]] = {}
    for name, values in buckets.items():
        if len(values) < 2:
            continue
        facets[name] = [
            (value, values[value])
            for value in sorted(values, key=lambda v: (-values[v], _sort_key(v)))
        ]
    return facets


def _sort_key(value: str) -> Tuple[float, str]:
    """Sort attribute values numerically where possible, else lexically."""
    number = _leading_number(value)
    return (number if number is not None else float("inf"), value)


_SI = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}


def _leading_number(value: str) -> Optional[float]:
    """Parse a leading number with optional SI prefix out of ``value``."""
    text = value.strip().lstrip("±")
    digits = ""
    idx = 0
    while idx < len(text) and (text[idx].isdigit() or text[idx] in ".-+"):
        digits += text[idx]
        idx += 1
    if not digits:
        return None
    try:
        number = float(digits)
    except ValueError:
        return None
    if idx < len(text) and text[idx] in _SI:
        number *= _SI[text[idx]]
    return number


def filter_hits(
    hits: List[SearchHit], selected: Dict[str, Set[str]]
) -> List[SearchHit]:
    """Apply selected facet values to ``hits``.

    **OR within an attribute, AND across attributes** — the semantics every
    parts catalogue uses and the only ones that make multi-select worth
    having: ticking ±1% and ±0.5% widens the tolerance allowed, while also
    picking a package narrows the result.

    An attribute mapped to an empty set is inactive, not "match nothing".
    """
    active: Dict[str, Set[str]] = {}
    for name, values in (selected or {}).items():
        # A bare string here would iterate into single characters and match
        # nothing, silently. Cheap to absorb, miserable to debug.
        chosen = {values} if isinstance(values, str) else set(values or ())
        if chosen:
            active[name] = chosen
    if not active:
        return list(hits)

    return [
        hit
        for hit in hits
        if all(hit.attributes.get(name) in values for name, values in active.items())
    ]
