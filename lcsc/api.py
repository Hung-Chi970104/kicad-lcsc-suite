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
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

JLC_ASSEMBLY_DETAIL = (
    "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={}"
)
LCSC_RETAIL_DETAIL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={}"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": _UA, "Accept": "application/json, text/plain, */*"}

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


def clear_cache() -> None:
    """Drop every cached response — used by the explorer's Refresh button."""
    _cache.clear()


def _get_json(url: str, timeout: int = 25) -> Dict[str, Any]:
    """GET a URL and parse JSON, returning ``{}`` on any failure."""
    cached = _cache.get(url)
    if cached is not None:
        return cached
    try:
        req = urllib.request.Request(url=url, headers=HEADERS)  # noqa: S310
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=timeout, context=ssl_context()
        ) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.debug("GET %s failed: %r", url, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
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
    report.ok = bool(assembly or retail)
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
        domestic = retail.get("domesticStockVO")
        if isinstance(domestic, dict):
            report.retail_domestic = _as_int(domestic.get("total"))
        overseas = retail.get("overseasStockVO")
        if isinstance(overseas, dict):
            report.retail_overseas = _as_int(overseas.get("total"))

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
    """
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


def build_facets(hits: List[SearchHit]) -> Dict[str, List[str]]:
    """Build LCSC-style filter facets from a result set.

    Returns attribute name -> sorted distinct values, keeping only attributes
    that actually discriminate (present on several hits, more than one value).
    """
    buckets: Dict[str, Dict[str, int]] = {}
    for hit in hits:
        for name, value in hit.attributes.items():
            buckets.setdefault(name, {})
            buckets[name][value] = buckets[name].get(value, 0) + 1

    facets: Dict[str, List[str]] = {}
    for name, values in buckets.items():
        if len(values) < 2:
            continue
        facets[name] = sorted(values, key=lambda v: (-values[v], _sort_key(v)))
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


def filter_hits(hits: List[SearchHit], selected: Dict[str, str]) -> List[SearchHit]:
    """Apply selected facet values (AND across attributes) to ``hits``."""
    if not selected:
        return hits
    out = []
    for hit in hits:
        if all(hit.attributes.get(name) == value for name, value in selected.items()):
            out.append(hit)
    return out
