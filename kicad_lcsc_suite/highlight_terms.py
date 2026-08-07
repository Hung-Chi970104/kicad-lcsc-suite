"""Term expansion and span finding for match highlighting — no toolkit.

Split out of ``dataview_highlight.py`` for the same reason ``sqlite_helpers.py``
was split out of ``helpers.py``: the Qt app needs these functions and cannot
import a module that touches wx. ``dataview_highlight`` re-exports every name
here, so the wx plugin's imports are unchanged.

The split is not merely tidiness. ``dataview_highlight`` guards its wx import
with ``try/except ImportError``, which is enough when wx is genuinely absent —
but a test that installs a *partial* ``wx`` stub makes that import succeed, and
the renderer class below it then fails on the first attribute the stub lacks.
Anything importable from the Qt half has to be free of wx outright, not
conditionally.

What is encoded here is domain knowledge with a long tail: which spellings of a
component value count as the same thing. ``390R`` is ``390Ω``; ``10uF`` is
``10µF`` is ``10µ``; ``R_0402_1005Metric`` is a ``0402``. Get one wrong and the
symptom is only ever a cell that quietly never highlights.
"""

from __future__ import annotations

import re

_HIGHLIGHT_FG = (180, 120, 0)
_HIGHLIGHT_FG_SELECTED = (255, 215, 64)
_MIN_HIGHLIGHT_TERM_LENGTH = 2
_HIGHLIGHT_VALUE_SEPARATOR = "\x1f"

_FOOTPRINT_ALIAS_FORWARD = {
    "SIOC-8": "SO-8",
    "SOT-23": "TO-236",
}
_FOOTPRINT_ALIAS_MAP = dict(_FOOTPRINT_ALIAS_FORWARD)
_FOOTPRINT_ALIAS_MAP.update(
    {target: source for source, target in _FOOTPRINT_ALIAS_FORWARD.items()}
)


def normalize_highlight_terms(query: str) -> list[str]:
    """Split keyword query into normalized terms suitable for highlighting."""
    terms = []
    for raw_term in query.split():
        term = raw_term.strip().strip("%")
        if term:
            lowered = term.casefold()
            if lowered not in terms:
                terms.append(lowered)
    return terms


def find_highlight_spans(text: str, terms: list[str]) -> list[tuple[int, int]]:
    """Return merged `(start, end)` spans for all term matches in `text`."""
    if not text or not terms:
        return []

    lowered_text = text.casefold()
    spans: list[tuple[int, int]] = []

    for term in terms:
        start = 0
        while True:
            match_start = lowered_text.find(term, start)
            if match_start == -1:
                break
            match_end = match_start + len(term)
            spans.append((match_start, match_end))
            start = match_end

    if not spans:
        return []

    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def filtered_highlight_terms(query: str) -> list[str]:
    """Return normalized terms that are long enough to highlight."""
    return [
        term
        for term in normalize_highlight_terms(query)
        if len(term) >= _MIN_HIGHLIGHT_TERM_LENGTH
    ]


def encode_highlighted_value(text: str, terms: list[str]) -> str:
    """Pack display text and row-specific highlight terms into a single string."""
    packed_terms = []
    for term in terms:
        if term is None:
            continue
        cleaned = str(term).strip().strip("%")
        if cleaned:
            packed_terms.append(cleaned)

    return _HIGHLIGHT_VALUE_SEPARATOR.join(
        ["" if text is None else str(text), *packed_terms]
    )


def decode_highlighted_value(value: str) -> tuple[str, list[str]]:
    """Unpack display text and normalized highlight terms from a packed string."""
    text = "" if value is None else str(value)
    if _HIGHLIGHT_VALUE_SEPARATOR not in text:
        return text, []

    parts = text.split(_HIGHLIGHT_VALUE_SEPARATOR)
    display_text = parts[0]
    raw_terms = " ".join(parts[1:])
    return display_text, normalize_highlight_terms(raw_terms)


def expand_value(reference: str, value: str) -> list[str]:
    """Return value variants used for highlight matching.

    For resistor references (`R*`), include ohm-symbol equivalents so terms like
    `390R` can also match `390Ω`, and `10K` can also match `10KΩ`.
    For capacitor references (`C*`), include `u`/`µ` interchangeable variants
    and optional `F`-suffixed forms.
    """
    raw = "" if value is None else str(value).strip()
    if not raw:
        return []

    variants = [raw]
    ref = "" if reference is None else str(reference).strip()
    upper_ref = ref.upper()

    if upper_ref.startswith("R"):
        if raw.endswith("Ω"):
            base = raw[:-1]
            if base and base[-1] in "RrOoKkMm":
                variants.append(base)
        else:
            last = raw[-1]
            if last in "RrOo":
                variants.append(f"{raw[:-1]}Ω")
            elif last in "KkMm":
                variants.append(f"{raw}Ω")

    if upper_ref.startswith("C"):
        has_micro = "µ" in raw or "u" in raw or "U" in raw
        if has_micro:
            swapped = raw.replace("µ", "u") if "µ" in raw else re.sub(r"[uU]", "µ", raw)

            if raw.endswith("F"):
                variants.extend([raw, swapped, raw[:-1], swapped[:-1]])
            else:
                variants.extend([raw, swapped, f"{raw}F", f"{swapped}F"])

    deduped = []
    for variant in variants:
        if variant not in deduped:
            deduped.append(variant)
    return deduped


def simplify_footprint_name(footprint: str) -> str:
    """Extract a short package token such as `0603` from a KiCad footprint name."""
    if not footprint:
        return ""

    match = re.search(r"_([0-9]{4})_\d+Metric\b", footprint)
    if match:
        return match.group(1)

    footprint_name = str(footprint).split(":")[-1]
    return (
        footprint_name.rsplit("_", maxsplit=1)[-1]
        if "_" in footprint_name
        else footprint_name
    )


def expand_footprint(reference: str, footprint: str) -> list[str]:
    """Return footprint variants used for highlight matching.

    Includes simplified package tokens, selected alias mappings, and optional
    designator-specific mappings.
    """
    raw = "" if footprint is None else str(footprint).strip()
    if not raw:
        return []

    footprint_name = raw.split(":")[-1]
    upper_name = footprint_name.upper()
    variants: list[str] = []

    simplified = simplify_footprint_name(footprint_name)
    if simplified:
        variants.append(simplified)

    # Common package aliases used by parts databases and footprints.
    for source, target in _FOOTPRINT_ALIAS_MAP.items():
        if source in upper_name:
            variants.append(target)

    # Capacitor-specific mapping: CP_Elec_6.3x7.7 -> SMD,D6.3
    ref = "" if reference is None else str(reference).strip()
    if ref.upper().startswith("C"):
        match = re.search(
            r"CP_ELEC_([0-9]+(?:\.[0-9]+)?)X[0-9]+(?:\.[0-9]+)?",
            upper_name,
        )
        if match:
            variants.append(f"SMD,D{match.group(1)}")

    deduped = []
    for variant in variants:
        if variant and variant not in deduped:
            deduped.append(variant)
    return deduped


class HighlightQueryCache:
    """Cache normalized query terms and highlight spans for one active query."""

    def __init__(self):
        self._query = ""
        self._terms: list[str] = []
        self._span_cache: dict[str, list[tuple[int, int]]] = {}

    def prepare(self, query: str):
        """Prepare cache state for a query, resetting cached spans on change."""
        if query != self._query:
            self._query = query
            self._terms = filtered_highlight_terms(query)
            self._span_cache.clear()

    def clear(self):
        """Clear all cached query and span data."""
        self._query = ""
        self._terms = []
        self._span_cache.clear()

    def get_terms(self) -> list[str]:
        """Return terms prepared for the active query."""
        return self._terms

    def get_spans(self, text: str) -> list[tuple[int, int]]:
        """Return cached spans for text, computing and storing on cache miss."""
        spans = self._span_cache.get(text)
        if spans is None:
            spans = find_highlight_spans(text, self._terms)
            self._span_cache[text] = spans
        return spans
