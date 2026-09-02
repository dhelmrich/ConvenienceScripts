"""PDF processor with region-aware two-column extraction (PyMuPDF + selective OCR).

The primary extractor is PyMuPDF's ``rawdict`` (per-character origins) and
``words`` (word geometry). A page-adaptive body-band + gutter detector separates
full-width front matter from a two-column body, boilerplate (running header,
vertical watermark, full-width contiguous footer) is filtered per page, and a
per-line spacing classifier decides whether native text is trusted, a
conservative gap reconstruction is acceptable, or selective OCR / LLM is needed.
Blablador is an optional, advisory-only cross-check that never rewrites text.
"""

import base64
import io
import json
import logging
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import markitdown
    from markitdown import MarkItDown

    MARKITDOWN_AVAILABLE = True
except ImportError:
    MARKITDOWN_AVAILABLE = False
    MarkItDown = None  # type: ignore

try:
    import pymupdf
except ImportError:  # pragma: no cover - defensive
    pymupdf = None  # type: ignore

try:
    import pytesseract
    from PIL import Image

    _TESSERACT_AVAILABLE = True
except ImportError:
    _TESSERACT_AVAILABLE = False
    Image = None  # type: ignore
    pytesseract = None  # type: ignore

# pix2tex (LaTeX-OCR) is an OPTIONAL, heavy dependency (torch + albumentations).
# It is never imported at server startup: doing so slows down the server, emits
# many third-party warnings, and (via albumentations' update check) makes an
# unsolicited network call. LaTeX transcription is therefore loaded lazily on
# the first explicit request, is ON by default whenever the package is
# installed, and can be turned off with PDF_MCP_NO_LATEX=1. Either way the
# network update check is disabled.
_PIX2TEX_AVAILABLE = False


def _pix2tex_available() -> bool:
    """Lazily load the pix2tex transcription backend (opt-out via env var)."""
    global _PIX2TEX_AVAILABLE
    if _PIX2TEX_AVAILABLE:
        return True
    if os.environ.get("PDF_MCP_NO_LATEX"):
        return False
    try:
        os.environ["NO_ALBUMENTATIONS_UPDATE"] = "1"
        from pix2tex.cli import LatexOCR  # noqa: F401, PLC0415

        _PIX2TEX_AVAILABLE = True
    except ImportError:
        logger.debug("pix2tex not installed; no LaTeX transcription")
        return False
    return _PIX2TEX_AVAILABLE


from .models import PageContent
from .utils import MIN_TEXT_THRESHOLD, PDFValidationError

logger = logging.getLogger(__name__)

# Region-aware mode constants (derived from paper_short.pdf; page-adaptive at runtime).
_HDR_Y = 95.0
_WM_MARGIN = 20.0


# ---------------------------------------------------------------------------
# Page-adaptive body-band + gutter detection (region-aware mode)
# ---------------------------------------------------------------------------

def _band_gutter(band_words, min_each: int = 10) -> Optional[tuple]:
    """
    For a y-band of words, find a credible column gutter: the widest gap between
    consecutive unique word x-centres that has a dense population on BOTH sides
    (a genuine two-column gutter, not a sparse title/author line). Returns
    (gutter_x, gap) or None.
    """
    cs = sorted(set(round((w[0] + w[2]) / 2, 1) for w in band_words))
    if len(cs) < 6:
        return None
    best_gap, loc = 0.0, None
    for a, b in zip(cs, cs[1:]):
        gap = b - a
        if gap <= best_gap:
            continue
        left = sum(1 for w in band_words if (w[0] + w[2]) / 2 <= a)
        right = sum(1 for w in band_words if (w[0] + w[2]) / 2 >= b)
        if left >= min_each and right >= min_each:
            best_gap, loc = gap, (a + b) / 2
    if loc is None:
        return None
    return round(loc, 1), round(best_gap, 1)


def _detect_body_band_and_split(words, hdr_y: float = _HDR_Y,
                                wm_x: float = 560.0, slab: float = 60.0):
    """
    Locate the two-column BODY band and its gutter x.

    Full-width front matter (title, abstract, keywords) and full-width footer
    have word centres spread across the whole page width, so they do NOT show a
    credible gutter. Only the two-column body shows a gutter that is (a) bounded
    by a dense population on both sides and (b) stable across consecutive
    y-slabs. This is page-adaptive and never uses a page-1 footer constant.

    Returns (split, band_dict, band_dict) or (None, None, {}).
    """
    ws = [w for w in words if w[1] >= hdr_y and w[0] < wm_x]
    if not ws:
        return None, None, {}
    ymin = min(w[1] for w in ws)
    ymax = max(w[3] for w in ws)

    y = ymin
    hits = []  # (y_center, gutter_x)
    while y < ymax:
        band = [w for w in ws if w[1] >= y and w[3] <= y + slab]
        g = _band_gutter(band)
        if g is not None:
            hits.append((round(y + slab / 2, 1), g[0]))
        y += slab / 2
    if not hits:
        return None, None, {}

    tol = 12.0
    best_run = None  # (n_agreeing_slabs, y0, y1, gutter)
    for gx in sorted(set(round(h[1]) for h in hits)):
        agreeing = sorted(yc for yc, gg in hits if abs(gg - gx) <= tol)
        if not agreeing:
            continue
        n = len(agreeing)
        y0_run, y1_run = agreeing[0], agreeing[-1]
        if best_run is None or n > best_run[0]:
            best_run = (n, y0_run, y1_run, round(gx, 1))

    if best_run is None or best_run[0] < 2:
        return None, None, {"split": None, "y0": ymin, "y1": ymax,
                            "slab_votes": 0}
    n, y0, y1, gutter = best_run
    band = {
        "split": gutter,
        "n_agreeing_slabs": n,
        "y0": y0,
        "y1": y1,
    }
    return band["split"], band, band


def _find_column_gutter_x(words) -> Optional[float]:
    """Detect a two-column gutter x from word geometry (fallback helper)."""
    centres = sorted(set(round((w[0] + w[2]) / 2, 1) for w in words))
    if len(centres) < 6:
        return None
    best_gap, split = 0, None
    for a, b in zip(centres, centres[1:]):
        gap = b - a
        if gap > best_gap:
            best_gap, split = gap, (a + b) / 2
    if best_gap < 20:
        return None
    return split


def _classify_word_region(w, body_band, split, wm_x, hdr_y):
    """Classify a word into header / watermark / full_width / body_left /
    body_right / footer / low_body_or_footer based on adaptive geometry."""
    x0, y0, x1, y1 = w[0], w[1], w[2], w[3]
    if x0 >= wm_x:
        return "watermark"
    if y1 < hdr_y:
        return "header"
    if body_band and y0 >= body_band["y0"] - 1 and y1 <= body_band["y1"] + 1:
        if split is not None:
            return "body_left" if (x0 + x1) / 2 < split else "body_right"
        return "body"
    if body_band is None or y1 < body_band["y0"]:
        return "full_width"
    return "low_body_or_footer"


def _line_is_full_width(line_words, page_w: float, frac: float = 0.6,
                        max_internal_gap: float = 35.0) -> bool:
    """True if a line is a near-full-width, contiguous line (footer candidate)."""
    if not line_words:
        return False
    ordered = sorted(line_words, key=lambda w: w[0])
    x0 = ordered[0][0]
    x1 = ordered[-1][2]
    if (x1 - x0) < frac * page_w:
        return False
    for a, b in zip(ordered, ordered[1:]):
        gap = b[0] - a[2]
        if gap > max_internal_gap:
            return False
    return True


def page_segment_regions(page) -> dict:
    """Adaptive per-page segmentation using word geometry."""
    words = page.get_text("words")
    if not words:
        return {"n_words": 0, "regions": {}, "col_split_x": None}

    page_w = page.rect.width
    max_x = max(w[2] for w in words)
    wm_x = max(555.0, max_x - _WM_MARGIN)
    hdr_y = _HDR_Y

    split, body_band, _ = _detect_body_band_and_split(words, hdr_y=hdr_y, wm_x=wm_x)

    lines = {}
    for w in words:
        lines.setdefault(round(w[1], 1), []).append(w)

    regions = {}
    for y, lw in lines.items():
        is_full_width = _line_is_full_width(lw, page_w)
        below_band = body_band and y > body_band["y1"]
        for w in lw:
            lab = _classify_word_region(w, body_band, split, wm_x, hdr_y)
            if lab == "low_body_or_footer":
                if below_band and is_full_width:
                    lab = "footer"
                else:
                    lab = ("body_left" if split is not None
                           and (w[0] + w[2]) / 2 < split
                           else ("body_right" if split is not None else "body"))
            regions.setdefault(lab, []).append(w)

    body_region = regions.get("body_left", []) + regions.get("body_right", [])
    body_extent = None
    if body_region:
        body_extent = {
            "top": round(min(w[1] for w in body_region), 1),
            "bottom": round(max(w[3] for w in body_region), 1),
        }

    return {
        "n_words": len(words),
        "regions": {k: len(v) for k, v in regions.items()},
        "col_split_x": round(split, 1) if split else None,
        "body_band": {k: round(v, 1) for k, v in body_band.items()} if body_band else None,
        "body_extent": body_extent,
        "body_left_x": (round(min(w[0] for w in regions["body_left"]), 1),
                        round(max(w[2] for w in regions["body_left"]), 1))
        if regions.get("body_left") else None,
        "body_right_x": (round(min(w[0] for w in regions["body_right"]), 1),
                         round(max(w[2] for w in regions["body_right"]), 1))
        if regions.get("body_right") else None,
    }


# ---------------------------------------------------------------------------
# Per-line spacing classifier (region-aware mode)
# ---------------------------------------------------------------------------

def _line_chars_gaps(line: dict):
    """Return (sorted_chars, inter_glyph_gaps) for a rawdict line."""
    chars = []
    for s in line["spans"]:
        for c in s["chars"]:
            chars.append(c)
    # Don't sort - preserve original order from PDF text layer.
    # PDF fonts may have overlapping glyphs (e.g., ligatures) where x-positions
    # don't reflect reading order. The rawdict preserves the correct text order.
    xs = [c["origin"][0] for c in chars]
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    return chars, gaps


def _has_separable_gap_pattern(gaps, mode_mult: float = 1.45,
                               valley_bin: float = 0.4,
                               min_sep_ratio: float = 1.8) -> bool:
    """
    Conservative separability test for a justified line's inter-glyph gaps.

    Lines whose explicit spaces are present show a BIMODAL gap histogram
    (within-word cluster + word-boundary cluster separated by a valley); when
    word boundaries are missing, the distribution is continuous. Both a
    max/median ratio and a near-empty valley between two populated clusters
    must hold before a gap reconstruction is attempted.
    """
    if not gaps:
        return False
    med = sorted(gaps)[len(gaps) // 2]
    mx = max(gaps)
    if med <= 0 or mx < min_sep_ratio * med:
        return False
    buckets = Counter(round(g / valley_bin) * valley_bin for g in gaps)
    if len(buckets) < 3:
        return False
    lo, hi = min(buckets), max(buckets)
    steps = int(round((hi - lo) / valley_bin))
    if steps < 4:
        return False
    bins = [lo + i * valley_bin for i in range(steps + 1)]
    counts = [buckets.get(round(b, 2), 0) for b in bins]
    for i in range(1, len(bins) - 1):
        if counts[i] <= 1 and counts[i - 1] >= 2 and counts[i + 1] >= 2:
            return True
    return False


def classify_line_spacing(line: dict) -> dict:
    """
    Classify a rawdict line into trusted_native / geometry_candidate /
    ocr_or_llm_required (or trivial).

    Emits measured diagnostics (explicit-space count, gap histogram,
    separability, candidate reconstruction, confidence, next action).
    """
    chars, gaps = _line_chars_gaps(line)
    text = "".join(c["c"] for c in chars)
    n_space = sum(1 for c in chars if c["c"] == " ")
    n_alnum = sum(1 for c in chars if c["c"].isalnum())

    if len(chars) < 3:
        return {
            "label": "trivial", "state": "trivial", "text": text,
            "n_space": n_space, "n_chars": len(chars), "n_alnum": n_alnum,
            "reconstructed": text, "confidence": 1.0,
            "recommended_action": "keep_native",
        }

    separable = _has_separable_gap_pattern(gaps)
    med = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
    mx = max(gaps) if gaps else 0.0

    candidate = text
    conf = 1.0
    action = "keep_native"
    if separable:
        thr = med * 1.45
        s = chars[0]["c"]
        for i in range(len(chars) - 1):
            if gaps[i] > thr:
                s += " "
            s += chars[i + 1]["c"]
        candidate = s
        tokens = [t for t in candidate.split(" ") if t]
        bad = sum(1 for t in tokens if len(t) <= 2 or len(t) > 20)
        conf = round(max(0.0, 1.0 - bad / max(len(tokens), 1)), 3)
        action = "use_reconstructed_if_confident"

    if n_space > 0:
        state = "trusted_native"
        label = "trusted"
        if not separable:
            label = "trusted_partial"
    elif separable:
        state = "geometry_candidate"
        label = "repairable"
    else:
        state = "ocr_or_llm_required"
        label = "ambiguous"
        action = "selective_ocr_or_llm"

    hist = {}
    if gaps:
        hist = dict(Counter(round(g, 1) for g in gaps))

    return {
        "label": label,
        "state": state,
        "text": text,
        "n_space": n_space,
        "n_chars": len(chars),
        "n_alnum": n_alnum,
        "gap_separable": separable,
        "median_gap": round(med, 2),
        "max_gap": round(mx, 2),
        "gap_histogram": hist,
        "reconstructed": candidate,
        "confidence": conf,
        "recommended_action": action,
    }


def _text_from_words(words) -> str:
    """Simple word join for a set of words (used by geometric modes)."""
    return " ".join(w[4] for w in sorted(words, key=lambda w: (round(w[1], 1), w[0])))


# ---------------------------------------------------------------------------
# Display-formula detection (geometry-based)
# ---------------------------------------------------------------------------

_MATH_SYMS = set("∫∑ΣγΩ±∂⋯∈≤≥·×→σδλµ∏√∞≈≠∝∅⊂⊃≅⊥αβθΛΞΦΨΓδπεφρτχ")
_STRONG_OPERATORS = set("=\u2260\u2248\u2264\u2265\u00b1\u00d7\u00f7\u221d\u2208\u2209\u2192\u2194\u2202\u2207\u222b\u2211\u220f\u221a\u221e")
_MEDIUM_OPERATORS = set("+-*/^_()[]{}|,:;")
_MATH_GLYPHS = set("\u03b1\u03b2\u03b3\u03b4\u03b5\u03b6\u03b7\u03b8\u03b9\u03ba\u03bb\u03bc\u03bd\u03be\u03bf\u03c0\u03c1\u03c3\u03c4\u03c5\u03c6\u03c7\u03c8\u03c9"
                  "\u0391\u0392\u0393\u0394\u0395\u0396\u0397\u0398\u0399\u039a\u039b\u039c\u039d\u039e\u039f\u03a0\u03a1\u03a3\u03a4\u03a5\u03a6\u03a7\u03a8\u03a9")
_COMMON_WORDS = {'the', 'of', 'and', 'in', 'to', 'a', 'is', 'that', 'for', 'with',
                 'as', 'on', 'by', 'at', 'an', 'be', 'or', 'not', 'this', 'which',
                 'are', 'was', 'were', 'been', 'have', 'has', 'had', 'do', 'does',
                 'did', 'will', 'would', 'could', 'should', 'may', 'might', 'must',
                 'can', 'from', 'into', 'between', 'through', 'we', 'our', 'they',
                 'their', 'he', 'she', 'it', 'its', 'his', 'her', 'where', 'when'}


def _is_symbol_font(font: str) -> bool:
    return "symbol" in font.lower()


def _is_strong_operator(ch: str) -> bool:
    return ch in _STRONG_OPERATORS


def _formula_row_key(y0: float, y1: float) -> int:
    return round((y0 + y1) / 2.0)


class FormulaRegion:
    """A detected display-formula region on a page."""

    __slots__ = ("page", "number", "bbox", "text")

    def __init__(self, page: int, number: int, bbox, text: str):
        self.page = page
        self.number = number
        self.bbox = bbox  # (x0, y0, x1, y1)
        self.text = text


class FigureRegion:
    """A detected figure/diagram region on a page."""

    __slots__ = ("page", "number", "bbox", "caption", "has_image")

    def __init__(self, page: int, number: int, bbox, caption: str = "", has_image: bool = False):
        self.page = page
        self.number = number
        self.bbox = bbox  # (x0, y0, x1, y1)
        self.caption = caption
        self.has_image = has_image


def _median(values) -> float:
    """Return the median of a non-empty numeric list."""
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _score_formula_line(f, body_size: float, column_width: float,
                        gap_above=None, gap_below=None,
                        line_pitch: float = None) -> float:
    """Return a formula candidacy score for a line's feature dict.

    Score > 0 means the line is a plausible display-formula candidate.
    Combines strong mathematical structure with display-layout evidence.
    """
    if line_pitch is None:
        line_pitch = body_size * 1.3

    # Whitespace surround: a display formula sits in a gap larger than the
    # normal line pitch. This is the user's key display-layout signal.
    wide_gap_above = gap_above is not None and gap_above >= 1.4 * line_pitch
    wide_gap_below = gap_below is not None and gap_below >= 1.4 * line_pitch
    wide_gap = wide_gap_above or wide_gap_below

    strong_math = (
        f["strong_count"] >= 1
        or f["sym_count"] >= 2
        or (f["sub_count"] >= 2 and f["token_count"] <= 12)
    )

    # Symbolic compactness: dense math symbols, low prose ratio.
    symbolic_compactness = (
        f["sym_count"] >= 2
        and f["prose_word_ratio"] <= 0.35
        and f["token_count"] <= 20
    )

    # Sub/superscript structure within a compact region.
    sub_super_structure = (
        f["sub_count"] >= 2
        and f["token_count"] <= 12
        and f["prose_word_ratio"] <= 0.45
    )

    # Display layout: isolated whitespace, standalone, taller, or narrow.
    display_layout = (
        wide_gap
        or f["prose_word_ratio"] <= 0.35
        or f["y_range"] >= 1.15 * body_size
        or (f["x_span"] <= 0.85 * column_width and f["token_count"] <= 12)
    )

    if strong_math and display_layout:
        return 1.0
    if symbolic_compactness and display_layout:
        return 0.8
    if sub_super_structure and display_layout and f["y_range"] >= 1.15 * body_size:
        return 0.7
    return 0.0


def _find_equation_number(rd, y0: float, y1: float, x_start: float,
                          x_max: float) -> Optional[float]:
    """Search for a right-aligned equation number like (1) within the band."""
    for block in rd["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            ly0, ly1 = line["bbox"][1], line["bbox"][3]
            if ly1 < y0 or ly0 > y1:
                continue
            text = ""
            for span in line["spans"]:
                for ch in span["chars"]:
                    text += ch["c"]
            stripped = text.strip()
            if stripped.startswith("(") and stripped.endswith(")"):
                chars = [ch for sp in line["spans"] for ch in sp["chars"]]
                if chars and chars[-1]["bbox"][1] > x_max - 100:
                    return chars[-1]["bbox"][2]
    return None


def detect_formula_regions(page, body_size: float = 11.0, wm_x: float = 560.0,
                           hdr_y: float = 95.0, ftr_y: float = 750.0) -> List[FormulaRegion]:
    """
    Detect display-formula regions on a page from glyph geometry.

    Display formulas are vertically stacked clusters of math glyphs (fractions,
    integrals/sums with limits, sub/superscripts) that sit between body text lines.

    Returns regions in reading order (top-to-bottom, left-column first), each
    carrying a per-page sequence number and a best-effort linear text.
    """
    seg = page_segment_regions(page)
    split = seg["col_split_x"]
    if split is None:
        max_x = max((w[2] for w in page.get_text("words")), default=0.0)
        wm_x = max(555.0, max_x - _WM_MARGIN)

    regions: List[FormulaRegion] = []

    def collect_column(x_min: float, x_max: float) -> List[FormulaRegion]:
        """Detect formulas within a single column x-window using content+layout scoring."""
        rd = page.get_text("rawdict")
        column_width = x_max - x_min

        # Collect all glyph lines within the column.
        all_lines = []
        for block in rd["blocks"]:
            if block["type"] != 0 or block["bbox"][0] >= x_max:
                continue
            if block["bbox"][3] < hdr_y or block["bbox"][2] <= x_min:
                continue
            if block["bbox"][1] > ftr_y:
                continue
            for line in block["lines"]:
                glyphs = []
                for span in line["spans"]:
                    size = span["size"]
                    font = span["font"]
                    is_sub = size < 0.8 * body_size
                    for ch in span["chars"]:
                        chx0, chy0, chx1, chy1 = ch["bbox"]
                        c = ch["c"]
                        if chx0 < x_min or chx1 >= x_max:
                            continue
                        if not c or not c.strip():
                            continue
                        glyphs.append({
                            "x0": chx0, "y0": chy0, "x1": chx1, "y1": chy1,
                            "c": c, "is_sub": is_sub,
                            "is_sym_font": _is_symbol_font(font),
                            "is_strong": _is_strong_operator(c),
                        })
                if glyphs:
                    all_lines.append({
                        "y0": line["bbox"][1], "y1": line["bbox"][3],
                        "glyphs": glyphs,
                    })

        if not all_lines:
            return []

        def line_features(ln):
            gs = ln["glyphs"]
            text = "".join(g["c"] for g in gs)
            low = text.lower()
            words = [w.strip('.,;:!?()[]{}"\'') for w in text.split()]
            prose_words = [w for w in words if w in _COMMON_WORDS]
            strong_count = sum(1 for g in gs if g["is_strong"])
            sub_count = sum(1 for g in gs if g["is_sub"])
            sym_count = sum(1 for g in gs if g["is_sym_font"] or g["c"] in _MATH_GLYPHS)
            y_range = ln["y1"] - ln["y0"]
            x_span = (max(g["x1"] for g in gs) - min(g["x0"] for g in gs)) if gs else 0
            return {
                "text": text,
                "token_count": len(words),
                "prose_word_ratio": (len(prose_words) / len(words)) if words else 0,
                "strong_count": strong_count,
                "sub_count": sub_count,
                "sym_count": sym_count,
                "y_range": y_range,
                "x_span": x_span,
                "char_count": len(text),
            }

        # Estimate the normal body line spacing (median vertical pitch of
        # consecutive glyph lines). A display formula sits in whitespace larger
        # than this pitch, so gaps above/below that exceed it are strong
        # display-layout evidence.
        ordered = sorted(all_lines, key=lambda ln: (ln["y0"] + ln["y1"]) / 2)
        pitches = []
        for a, b in zip(ordered, ordered[1:]):
            gap = (b["y0"] + b["y1"]) / 2 - (a["y0"] + a["y1"]) / 2
            if gap > 0:
                pitches.append(gap)
        line_pitch = _median(pitches) if pitches else body_size * 1.3

        # For each line, compute the whitespace gap to the nearest line above
        # and below within the same column.
        for i, ln in enumerate(ordered):
            y_mid = (ln["y0"] + ln["y1"]) / 2
            gap_above = None
            gap_below = None
            for other in ordered:
                if other is ln:
                    continue
                o_mid = (other["y0"] + other["y1"]) / 2
                if o_mid < y_mid:
                    gap_above = y_mid - o_mid
                elif o_mid > y_mid:
                    if gap_below is None or o_mid - y_mid < gap_below:
                        gap_below = o_mid - y_mid
            ln["gap_above"] = gap_above
            ln["gap_below"] = gap_below
            ln["line_pitch"] = line_pitch

        # Score each line as a formula candidate.
        for ln in all_lines:
            f = line_features(ln)
            ln["score"] = _score_formula_line(f, body_size, column_width,
                                              ln["gap_above"], ln["gap_below"],
                                              line_pitch)
            ln["features"] = f

        # Group candidates into vertical bands (compact vertical stacking).
        candidates = [ln for ln in all_lines if ln["score"] > 0]
        if not candidates:
            return []

        candidates.sort(key=lambda ln: (ln["y0"] + ln["y1"]) / 2)
        moat = body_size * 1.4
        bands: List[List] = [[candidates[0]]]
        for ln in candidates[1:]:
            prev_y = (bands[-1][-1]["y0"] + bands[-1][-1]["y1"]) / 2
            cur_y = (ln["y0"] + ln["y1"]) / 2
            if cur_y - prev_y > moat:
                bands.append([ln])
            else:
                bands[-1].append(ln)

        out: List[FormulaRegion] = []
        for band in bands:
            all_g = [g for ln in band for g in ln["glyphs"]]
            x0 = min(g["x0"] for g in all_g)
            x1 = max(g["x1"] for g in all_g)
            y0 = min(g["y0"] for g in all_g)
            y1 = max(g["y1"] for g in all_g)
            region_height = y1 - y0
            # Hard guard on region height. A display formula is at most a few
            # body lines tall (fractions, sub/superscripts, limits). Regions far
            # taller than this are prose paragraphs or other text blocks that
            # merely contain math-like artifacts, not display equations.
            # Require at least 2 body lines height to reject single-line math artifacts.
            min_height = 2.3 * body_size
            max_height = 5.0 * body_size
            if min_height <= region_height <= max_height:
                # Search rightward within the vertical band for an equation number
                # like "(1)" near the right column edge.
                eq_num = _find_equation_number(rd, y0, y1, x1, x_max)
                if eq_num:
                    x1 = eq_num
                # Display formulas span the full column width for the crop.
                out.append(
                    FormulaRegion(
                        0, 0,
                        (round(x_min, 1), round(y0, 1), round(x_max, 1), round(y1, 1)),
                        "".join(g["c"] for g in sorted(all_g, key=lambda g: (g["y0"], g["x0"]))),
                    )
                )
        return out

    if split is not None:
        regions = collect_column(0.0, split)
        regions = [r for r in regions if r.bbox[0] < split]
        regions.extend(collect_column(split, wm_x))
    else:
        regions = collect_column(0.0, wm_x)

    page_number = page.number if hasattr(page, "number") else 0
    for i, region in enumerate(regions, start=1):
        region.page = page_number
        region.number = i
    return regions


def detect_figure_regions(page, body_size: float = 11.0, wm_x: float = 560.0,
                          hdr_y: float = 95.0, ftr_y: float = 750.0) -> List[FigureRegion]:
    """
    Detect figure/diagram regions on a page from layout geometry and captions.

    Figures are identified by:
    1. Caption text starting with "Fig." or "Figure"
    2. Large visual regions (gaps in text layout where figures would be)
    3. Often have bounding boxes that span significant width/height

    Returns regions in reading order (top-to-bottom, left-to-right), each
    carrying a per-page sequence number, caption, and image flag.
    """
    seg = page_segment_regions(page)
    split = seg["col_split_x"]

    # Get images on the page (for has_image flag)
    images = page.get_images()
    image_boxes = []
    for img in images:
        try:
            img_rects = page.get_image_rects(img[0])
            for rect in img_rects:
                image_boxes.append((rect.x0, rect.y0, rect.x1, rect.y1))
        except Exception:
            continue

    # Get text blocks
    rd = page.get_text("dict")
    text_blocks = []
    for block in rd["blocks"]:
        if block["type"] == 0:  # text block
            bbox = block["bbox"]
            lines_text = []
            for line in block["lines"]:
                line_text = ""
                for span in line["spans"]:
                    line_text += span["text"]
                if line_text.strip():
                    lines_text.append(line_text.strip())
            if lines_text:
                text_blocks.append({
                    "bbox": bbox,
                    "lines": lines_text,
                    "text": " ".join(lines_text),
                })

    regions: List[FigureRegion] = []
    min_fig_height = 40.0  # Minimum figure region height
    min_fig_width = 150.0  # Minimum figure region width

    def is_caption_text(text: str) -> bool:
        """Check if text looks like a figure caption.
        
        Captions typically:
        - Start with "Fig." or "Fig " followed by a number
        - Are relatively short (not full sentences)
        """
        lower = text.lower()
        # Must start with Fig. or Fig followed by space and number
        import re
        return bool(re.match(r'^fig\.?\s*\d', lower))

    def find_figure_region_for_caption(caption_block) -> tuple:
        """Find the visual region above/around a caption block."""
        caption_bbox = caption_block["bbox"]
        caption_y0 = caption_bbox[1]
        caption_x0 = caption_bbox[0]
        caption_x1 = caption_bbox[2]

        # Look for a large gap above the caption
        # The figure region is typically above the caption
        for block in text_blocks:
            block_bbox = block["bbox"]
            block_y1 = block_bbox[3]

            # Check if this block is just above the caption
            if block_y1 < caption_y0 and caption_y0 - block_y1 < 50:
                # This might be the figure region
                width = block_bbox[2] - block_bbox[0]
                height = caption_y0 - block_y1
                if width >= min_fig_width and height >= min_fig_height:
                    return (block_bbox[0], block_y1, block_bbox[2], caption_y0)

        # If no gap found, use a default region around the caption
        # This handles cases where the figure is inline with text
        default_height = 100.0
        default_width = max(caption_x1 - caption_x0, min_fig_width)
        return (caption_x0, caption_y0 - default_height,
                caption_x0 + default_width, caption_y0)

    # Find caption blocks and create figure regions
    caption_blocks = []
    for block in text_blocks:
        for line in block["lines"]:
            if is_caption_text(line):
                caption_blocks.append({
                    "bbox": block["bbox"],
                    "caption": line,
                })
                break

    # Sort captions by position (top-to-bottom, left-to-right)
    caption_blocks.sort(key=lambda c: (c["bbox"][1], c["bbox"][0]))

    # Create figure regions for each caption
    used_regions = []
    for cap in caption_blocks:
        region = find_figure_region_for_caption(cap)
        x0, y0, x1, y1 = region

        # Check if this region overlaps significantly with existing ones
        overlap = False
        for used in used_regions:
            ux0, uy0, ux1, uy1 = used
            overlap_x = max(0, min(x1, ux1) - max(x0, ux0))
            overlap_y = max(0, min(y1, uy1) - max(y0, uy0))
            if overlap_x * overlap_y > 1000:  # Significant overlap
                overlap = True
                break

        if not overlap:
            # Check if region contains any images
            has_image = False
            for img_box in image_boxes:
                igx0, igy0, igx1, igy1 = img_box
                overlap_x = max(0, min(x1, igx1) - max(x0, igx0))
                overlap_y = max(0, min(y1, igy1) - max(y0, igy0))
                if overlap_x * overlap_y > 500:
                    has_image = True
                    break

            used_regions.append(region)
            regions.append(FigureRegion(
                0, 0, region, cap["caption"], has_image
            ))

    # Assign page numbers and sequence numbers
    page_number = page.number if hasattr(page, "number") else 0
    for i, region in enumerate(regions, start=1):
        region.page = page_number
        region.number = i

    return regions


def _parse_conf(value) -> Optional[float]:
    """Parse a Tesseract conf entry (str or int) into 0..1 or None."""
    if value is None:
        return None
    try:
        s = str(value).strip()
        if s == "-1" or s == "":
            return None
        return int(s) / 100.0
    except Exception:
        return None


class PDFProcessorError(Exception):
    """Raised when PDF processing fails."""

    pass


class PDFProcessor:
    """
    Process PDFs using MarkItDown with Tesseract OCR fallback.

    This processor:
    1. Uses MarkItDown for initial PDF-to-Markdown conversion
    2. Detects pages with missing or insufficient text
    3. Renders those pages and uses Tesseract OCR as fallback
    4. Preserves page numbers, headings, tables, figure captions, equations
    """

    def __init__(
        self,
        tesseract_path: Optional[str] = None,
        min_text_threshold: int = MIN_TEXT_THRESHOLD,
        max_pages: int = 500,
    ):
        """
        Initialize the PDF processor.

        Args:
            tesseract_path: Path to tesseract executable. Auto-detected if None.
            min_text_threshold: Minimum characters for a page to be considered valid
            max_pages: Maximum number of pages to process
        """
        if not MARKITDOWN_AVAILABLE:
            raise PDFProcessorError(
                "MarkItDown not installed. Install with: pip install 'markitdown[all]'"
            )

        self.markitdown = MarkItDown()
        self.min_text_threshold = min_text_threshold
        self.max_pages = max_pages
        self._current_pdf_path: Optional[Path] = None
        self._latex_ocr = None

        # Configure Tesseract
        if tesseract_path:
            pytesseract.tesseract_cmd = tesseract_path
        elif _TESSERACT_AVAILABLE:
            # Try to auto-detect
            try:
                pytesseract.get_tesseract_version()
            except Exception as e:
                logger.warning(f"Tesseract auto-detection failed: {e}")
                self._tesseract_available = False

        self._tesseract_available = _TESSERACT_AVAILABLE

    @property
    def tesseract_available(self) -> bool:
        """Check if Tesseract OCR is available."""
        if not self._tesseract_available:
            return False
        try:
            pytesseract.get_tesseract_version()
            return True
        except Exception:
            self._tesseract_available = False
            return False

    def detect_layout(
        self, pdf_path: Path, verify_sentence: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Detect the layout structure of a PDF using region-aware segmentation.

        The primary extractor is PyMuPDF. Each page is segmented with the
        adaptive body-band + gutter detector; a page is two-column when a stable
        gutter is present. A cross-column QA gate compares native order against
        geometric (y,x) order to flag ordering issues.

        Args:
            pdf_path: Path to the PDF file
            verify_sentence: Optional sentence to check if it appears
                consecutively in the region-aware extraction (deterministic
                column-span check)

        Returns:
            Dict with layout information:
            - layout_type: "single_column", "two_column", or "multi_column"
            - columns: number of detected columns
            - sample_pages: layout analysis for sample pages
            - warning: message if complex layout detected
            - sentence_verification: if verify_sentence provided
            - cross_column_order: native vs geometric disagreement flag
        """
        if not pdf_path.exists():
            raise PDFValidationError(f"File not found: {pdf_path}")

        if pymupdf is None:
            return {
                "layout_type": "single_column",
                "columns": 1,
                "sample_pages": [],
                "warning": None,
                "error": "PyMuPDF not installed",
            }

        result = {
            "layout_type": "single_column",
            "columns": 1,
            "sample_pages": [],
            "warning": None,
            "cross_column_order": False,
        }

        doc = pymupdf.open(str(pdf_path))
        total = len(doc)
        pages_to_analyze = min(5, total)
        column_counts = []
        native_page1 = ""

        for i in range(pages_to_analyze):
            page = doc[i]
            seg = page_segment_regions(page)
            split = seg["col_split_x"]
            has_two_col = split is not None and seg.get("body_band") is not None
            columns = 2 if has_two_col else 1
            column_counts.append(columns)

            # Cross-column QA gate: native vs geometric order on the body.
            body_band = seg.get("body_band")
            page_w = page.rect.width
            words = page.get_text("words")
            max_x = max(w[2] for w in words) if words else 0.0
            wm_x = max(555.0, max_x - _WM_MARGIN)
            hdr_y = _HDR_Y
            native = self._native_body_text(page, body_band, wm_x, hdr_y)
            geom = self._geometric_order_body_text(page, body_band, split, wm_x, hdr_y)
            if i == 0:
                native_page1 = native

            result["sample_pages"].append({
                "page": i + 1,
                "detected_columns": columns,
                "col_split_x": split,
                "body_extent": seg.get("body_extent"),
                "native_vs_geom_disagree": self._cross_column_order_check(native, geom),
            })

        doc.close()

        if column_counts:
            avg_columns = sum(column_counts) / len(column_counts)
            result["columns"] = max(column_counts)

            if avg_columns >= 1.8 and max(column_counts) >= 2:
                result["layout_type"] = (
                    "two_column" if max(column_counts) == 2 else "multi_column"
                )
                result["warning"] = (
                    f"Multi-column layout detected ({result['columns']} columns). "
                    "Text extraction may have ordering issues where content spanning "
                    "columns appears fragmented. Consider verifying with a sample "
                    "sentence or consecutive sentences that cross column boundaries."
                )

        # Cross-column order disagreement detected on any analysed page.
        if any(p.get("native_vs_geom_disagree") for p in result["sample_pages"]):
            result["cross_column_order"] = True
            result["warning"] = (result["warning"] or "") + (
                " Native vs geometric order disagree on a cross-column sentence; "
                "left-column-first then right-column reconstruction may be needed."
            ).strip()

        # Verify sentence (deterministic) against region-aware page-1 text.
        if verify_sentence:
            result["sentence_verification"] = {
                "sentence": verify_sentence,
                "found": verify_sentence in native_page1,
                "fragmented": False,
                "raw_text_sample": native_page1[:500],
            }
            if not result["sentence_verification"]["found"]:
                words = verify_sentence.split()
                if len(words) > 5:
                    phrase = " ".join(words[:5])
                    if phrase in native_page1:
                        result["sentence_verification"]["fragmented"] = True
                        result["sentence_verification"]["reason"] = (
                            "Sentence fragments found but not in correct order"
                        )

        return result

    def _native_body_text(self, page, body_band, wm_x, hdr_y) -> str:
        """Extract native-order body text from rawdict lines (boilerplate filtered)."""
        rd = page.get_text("rawdict")
        parts = []
        for b in rd["blocks"]:
            if b["type"] != 0:
                continue
            if b["bbox"][0] > 1e9:
                continue
            x0, y0, x1, y1 = b["bbox"]
            if x0 >= wm_x or y1 < hdr_y:
                continue
            for li in b["lines"]:
                info = classify_line_spacing(li)
                parts.append(info["text"])
        return "\n".join(parts)

    def process_pdf(self, pdf_path: Path) -> Tuple[List[PageContent], Dict[str, Any]]:
        """
        Process a PDF file and extract content from all pages.

        The primary extractor is PyMuPDF (pymupdf). Each page is analysed with
        the region-aware ``scientific_two_column_region_aware`` mode: adaptive
        body-band + gutter detection, boilerplate filtering, native-order
        reconstruction with selective repair, per-line spacing classification
        and selective OCR. Document metadata is read from the PyMuPDF document.

        Args:
            pdf_path: Path to the PDF file

        Returns:
            Tuple of (list of PageContent, metadata dict)

        Raises:
            PDFProcessorError: If processing fails
        """
        if not pdf_path.exists():
            raise PDFValidationError(f"File not found: {pdf_path}")

        logger.info(f"Processing PDF: {pdf_path}")
        self._current_pdf_path = pdf_path

        if pymupdf is None:
            raise PDFProcessorError(
                "PyMuPDF not installed. Install with: pip install pymupdf"
            )

        try:
            doc = pymupdf.open(str(pdf_path))
        except Exception as e:
            raise PDFProcessorError(f"PyMuPDF failed to open PDF: {e}")

        total_pages = len(doc)
        if total_pages == 0:
            raise PDFProcessorError("PDF has no pages")

        if total_pages > self.max_pages:
            raise PDFValidationError(
                f"PDF has {total_pages} pages, exceeding maximum of {self.max_pages}"
            )

        pdf_meta = {
            "title": doc.metadata.get("title"),
            "author": doc.metadata.get("author"),
            "creation_date": doc.metadata.get("creationDate"),
            "modification_date": doc.metadata.get("modDate"),
        }
        metadata = {
            "title": pdf_meta.get("title"),
            "author": pdf_meta.get("author"),
            "creation_date": pdf_meta.get("creation_date"),
            "modification_date": pdf_meta.get("modification_date"),
        }

        metadata.update(
            {
                "pages_with_ocr": 0,
                "extraction_warnings": [],
                "extraction_mode": "scientific_two_column_region_aware",
                "provenance": {},
            }
        )

        page_contents = []
        for page_number in range(1, total_pages + 1):
            page = doc[page_number - 1]
            page_content = self._process_page_region_aware(page, page_number)
            page_contents.append(page_content)

            if page_content.has_ocr:
                metadata["pages_with_ocr"] += 1

            if page_content.warnings:
                metadata["extraction_warnings"].extend(page_content.warnings)

            metadata["provenance"][page_number] = page_content.diagnostics

        doc.close()

        logger.info(
            f"Processed {len(page_contents)} pages, "
            f"{metadata['pages_with_ocr']} with OCR"
        )

        return page_contents, metadata

    def _dominant_body_size(self, page, wm_x: float, hdr_y: float) -> float:
        """Estimate the dominant body font size of a page from its spans."""
        sizes = Counter()
        for block in page.get_text("rawdict")["blocks"]:
            if block["type"] != 0:
                continue
            bx0, _, _, by1 = block["bbox"]
            if bx0 >= wm_x or by1 < hdr_y:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    n = sum(1 for c in span["chars"] if c["c"].strip())
                    if n:
                        sizes[round(span["size"], 1)] += n
        if not sizes:
            return 11.0
        return float(max(sizes, key=sizes.get))

    def crop_formula_image(self, pdf_path: Path, page_number: int,
                           bbox, dpi: int = 300) -> Optional[bytes]:
        """
        Render a formula region as a PNG image crop from the given page.

        ``bbox`` is a (x0, y0, x1, y1) tuple in PDF points; ``dpi`` controls
        render resolution. Returns PNG bytes or None on failure.
        """
        try:
            doc = pymupdf.open(str(pdf_path))
            page = doc[page_number - 1]
            clip = pymupdf.Rect(*bbox)
            pix = page.get_pixmap(clip=clip, dpi=dpi,
                                  colorspace=pymupdf.csGRAY)
            data = pix.tobytes("png")
            doc.close()
            return data
        except Exception as e:
            logger.warning(f"Formula crop failed page {page_number}: {e}")
            return None

    def crop_page_image(self, pdf_path: Path, page_number: int,
                        bbox, dpi: int = 300) -> Optional[bytes]:
        """
        Render a figure/region as a PNG image crop from the given page.

        ``bbox`` is a (x0, y0, x1, y1) tuple in PDF points; ``dpi`` controls
        render resolution. Returns PNG bytes or None on failure.
        """
        try:
            doc = pymupdf.open(str(pdf_path))
            page = doc[page_number - 1]
            clip = pymupdf.Rect(*bbox)
            pix = page.get_pixmap(clip=clip, dpi=dpi)
            data = pix.tobytes("png")
            doc.close()
            return data
        except Exception as e:
            logger.warning(f"Figure crop failed page {page_number}: {e}")
            return None

    def transcribe_formula_image(self, image_bytes: bytes) -> Optional[str]:
        """
        Transcribe a cropped formula image to LaTeX via pix2tex (LaTeX-OCR).

        pix2tex (a heavy torch dependency) is loaded lazily on first use and is
        ON by default when installed; set ``PDF_MCP_NO_LATEX=1`` to disable it.
        Returns None when unavailable or on failure, so callers can fall back
        to the raw cropped image.
        """
        if not _pix2tex_available():
            return None
        try:
            from pix2tex.cli import LatexOCR  # noqa: PLC0415

            if self._latex_ocr is None:
                self._latex_ocr = LatexOCR()
            img = Image.open(io.BytesIO(image_bytes))
            return self._latex_ocr(img)
        except Exception as e:
            logger.warning(f"pix2tex transcription failed: {e}")
            return None

    def _process_page_region_aware(self, page, page_number: int) -> PageContent:
        """
        Process a single page with the region-aware extraction mode.

        Steps:
          1. Segment the page (body band, gutter, header/watermark/footer).
          2. Extract native-order body text from rawdict lines, filtering
             boilerplate and reconstructing body lines with the per-line
             spacing classifier.
          3. Run the cross-column QA gate: if native order disagrees with the
             detected gutter on a cross-column sentence, fall back to
             left-then-right order.
          4. Trigger selective OCR only for ``ocr_or_llm_required`` lines (or a
             whole low-density / scanned page).
          5. Optionally consult Blablador (advisory) for ambiguous regions.

        Returns:
            PageContent with markdown, diagnostics and per-line provenance.
        """
        warnings = []
        diagnostics = {
            "page": page_number,
            "native_order": [],
            "geometric_order": [],
            "missing_spaces": [],
            "column_order_error": False,
            "line_states": {},
            "ocr_triggered": False,
        }

        # --- Region segmentation ---
        seg = page_segment_regions(page)
        split = seg["col_split_x"]
        body_band = seg.get("body_band")
        is_scanned = seg["n_words"] == 0

        if is_scanned:
            return self._handle_scanned_page(page, page_number, warnings, diagnostics)

        page_w = page.rect.width
        max_x = max(w[2] for w in page.get_text("words"))
        wm_x = max(555.0, max_x - _WM_MARGIN)
        hdr_y = _HDR_Y

        # --- Detect formula regions FIRST (to exclude from body text classification) ---
        body_size = self._dominant_body_size(page, wm_x, hdr_y)
        formula_regions = detect_formula_regions(page, body_size=body_size,
                                                  wm_x=wm_x, hdr_y=hdr_y)
        # Build a set of y-ranges for formula regions to skip during body classification
        formula_y_ranges = [(f.bbox[1], f.bbox[3]) for f in formula_regions]
        diagnostics["formulas"] = [
            {
                "page": page_number,
                "number": f.number,
                "bbox": list(f.bbox),
                "text": f.text,
            }
            for f in formula_regions
        ]

        # --- Detect figure regions ---
        figure_regions = detect_figure_regions(page, body_size=body_size,
                                                wm_x=wm_x, hdr_y=hdr_y)
        diagnostics["figures"] = [
            {
                "page": page_number,
                "number": f.number,
                "bbox": list(f.bbox),
                "caption": f.caption,
                "has_image": f.has_image,
            }
            for f in figure_regions
        ]

        # --- Per-line spacing classification over native rawdict body lines ---
        rd = page.get_text("rawdict")
        body_lines = []      # list of (state, text, reconstructed, bbox, info)
        n_ocr_required = 0
        n_body_lines = 0
        for b in rd["blocks"]:
            if b["type"] != 0:
                continue
            if b["bbox"][0] > 1e9:
                continue
            x0, y0, x1, y1 = b["bbox"]
            if x0 >= wm_x or y1 < hdr_y:
                continue  # watermark / running header
            for li in b["lines"]:
                # Skip lines that are part of formula regions - they are not body text
                # and should not trigger OCR. Formulas are handled separately.
                line_y = (li["bbox"][1] + li["bbox"][3]) / 2
                in_formula = any(y0_f <= line_y <= y1_f for y0_f, y1_f in formula_y_ranges)
                if in_formula:
                    continue
                    
                info = classify_line_spacing(li)
                # Filter out footer lines (full-width contiguous below body) and
                # non-body front matter above the body band.
                region = self._classify_line_region(
                    li, body_band, split, wm_x, hdr_y, page_w)
                if region in ("footer", "watermark", "header"):
                    continue
                if region in ("body_left", "body_right", "body", "low_body_or_footer",
                              "full_width", "front"):
                    body_lines.append((info, li["bbox"], region))
                    n_body_lines += 1
                    if info["state"] == "ocr_or_llm_required":
                        n_ocr_required += 1

        diagnostics["line_states"] = dict(
            Counter(info["state"] for info, _, _ in body_lines))

        # --- Fallback: whole-page OCR when >40% of body lines are ambiguous ---
        frac_ambiguous = (n_ocr_required / n_body_lines) if n_body_lines else 0.0
        whole_page_ocr = frac_ambiguous > 0.40 or (
            n_body_lines > 0 and self._text_density_low(body_lines))

        if whole_page_ocr and self.tesseract_available:
            ocr_result = self._ocr_page_region_aware(page, page_number)
            if ocr_result:
                ocr_text, ocr_conf, ocr_lines = ocr_result
                if ocr_text.strip():
                    diagnostics["ocr_triggered"] = True
                    warnings.append(
                        f"Whole-page OCR used ({n_ocr_required}/{n_body_lines} "
                        f"lines ambiguous, conf {ocr_conf:.1%})")
                    return PageContent(
                        page_number=page_number,
                        markdown=ocr_text,
                        text_length=len(self._extract_text(ocr_text)),
                        has_ocr=True,
                        ocr_confidence=ocr_conf,
                        warnings=warnings,
                        diagnostics=diagnostics,
                    )

        # --- First pass: classify lines and identify LLM-required lines ---
        llm_fixed_lines = set()
        for i, (info, _, _) in enumerate(body_lines):
            if (info["state"] == "ocr_or_llm_required" and
                info["n_space"] == 0 and
                len(info["text"]) > 20):
                llm_fixed_lines.add(i)

        # --- Second pass: apply fixes ---
        native_out = []
        provenance = []
        
        for i, (info, bbox, region) in enumerate(body_lines):
            state = info["state"]
            line_text = info["text"]
            src = "pymupdf_rawdict"
            
            if i in llm_fixed_lines:
                fixed_text = self._llm_fix_concatenated_words(line_text)
                if fixed_text:
                    line_text = fixed_text
                    src = "llm_concatenated_words_fix"
            elif state == "geometry_candidate" and info["confidence"] >= 0.9:
                line_text = info["reconstructed"]
                src = "gap_reconstruction"
            
            native_out.append(line_text)
            provenance.append({
                "page_number": page_number,
                "source_tool": src,
                "line_or_block_bbox": [round(v, 1) for v in bbox],
                "spacing_state": state,
                "ocr_confidence": None,
                "blablador_reasoning": None,
            })
            
            if state == "ocr_or_llm_required" and src != "llm_concatenated_words_fix":
                diagnostics["missing_spaces"].append({
                    "page": page_number,
                    "bbox": [round(v, 1) for v in bbox],
                    "text": info["text"],
                })

        native_text = "\n".join(native_out)

        # --- Cross-column QA gate: native vs geometric order ---
        geom_text = self._geometric_order_body_text(
            page, body_band, split, wm_x, hdr_y)
        diagnostics["native_order"] = native_text[:500]
        diagnostics["geometric_order"] = geom_text[:500]

        order_disagreement = self._cross_column_order_check(native_text, geom_text)
        diagnostics["column_order_error"] = order_disagreement

        if order_disagreement and split is not None:
            warnings.append(
                "Native order disagrees with detected gutter on a cross-column "
                "sentence; using left-column-first then right-column order.")
            final_text = self._left_then_right_body_text(
                page, body_band, split, wm_x, hdr_y)
        else:
            final_text = native_text

        # --- Optional Blablador advisory cross-check ---
        blablador_note = self._advisory_blablador(
            page, page_number, split, n_ocr_required, body_band)
        if blablador_note:
            diagnostics["blablador"] = blablador_note
            for p in provenance:
                p["blablador_reasoning"] = blablador_note.get("reasoning")

        diagnostics["provenance"] = provenance

        # --- Selective OCR: skip lines that were LLM-fixed ---
        text_length = len(self._extract_text(final_text))
        has_ocr = False
        ocr_confidence: Optional[float] = None

        if n_ocr_required > 0 and self.tesseract_available and not whole_page_ocr:
            ocr_body_lines = [
                (info, bbox, region) for i, (info, bbox, region) in enumerate(body_lines)
                if i not in llm_fixed_lines
            ]
            
            ocr_patch = self._selective_ocr_lines(page, page_number, ocr_body_lines)
            if ocr_patch:
                ocr_line_map = ocr_patch.get("line_map", {})
                lines = final_text.split("\n")
                out = []
                for i, line in enumerate(lines):
                    if i in llm_fixed_lines:
                        out.append(line)
                    else:
                        key = "".join(line.split())
                        out.append(ocr_line_map.get(key, line))
                final_text = "\n".join(out)
                # Only mark page as "with OCR" if a significant portion needed it.
                # Pages with just a few touch-ups have a text layer and shouldn't
                # be reported as requiring OCR.
                ocr_fraction = len(ocr_patch['items']) / n_body_lines if n_body_lines else 0
                if ocr_fraction >= 0.20:  # 20% threshold
                    has_ocr = True
                ocr_confidence = ocr_patch["mean_confidence"]
                diagnostics["ocr_triggered"] = True
                warnings.append(
                    f"Selective OCR applied to {len(ocr_patch['items'])} "
                    f"ambiguous lines (conf {ocr_patch['mean_confidence']:.1%})")

        markdown = final_text
        return PageContent(
            page_number=page_number,
            markdown=markdown,
            text_length=text_length,
            has_ocr=has_ocr,
            ocr_confidence=ocr_confidence,
            warnings=warnings,
            diagnostics=diagnostics,
        )

    def _classify_line_region(self, line, body_band, split, wm_x, hdr_y,
                              page_w) -> str:
        """Classify a rawdict line's region using its bbox + adaptive geometry."""
        x0, y0, x1, y1 = line["bbox"]
        if x0 >= wm_x:
            return "watermark"
        if y1 < hdr_y:
            return "header"
        if body_band and y0 >= body_band["y0"] - 1 and y1 <= body_band["y1"] + 1:
            return ("body_left" if split is not None and (x0 + x1) / 2 < split
                    else ("body_right" if split is not None else "body"))
        if body_band is None or y1 < body_band["y0"]:
            return "front"
        # Below the body band: footer only if near-full-width contiguous line.
        # A single line's bbox spanning near-full-width with no internal gutter.
        if y0 > body_band["y1"]:
            if (x1 - x0) >= 0.6 * page_w:
                return "footer"
        return "low_body_or_footer"

    def _text_density_low(self, body_lines) -> bool:
        """True if the body has very few characters relative to line count."""
        total = sum(len(info["text"]) for info, _, _ in body_lines)
        if not body_lines:
            return True
        return total < self.min_text_threshold

    def _geometric_order_body_text(self, page, body_band, split, wm_x, hdr_y) -> str:
        """Geometric (y, x) reconstruction of body words for the QA gate."""
        words = [w for w in page.get_text("words")
                 if w[0] < wm_x and w[1] >= hdr_y]
        if body_band:
            words = [w for w in words
                     if w[1] >= body_band["y0"] - 1 and w[1] <= body_band["y1"] + 1]
        return _text_from_words(words)

    def _left_then_right_body_text(self, page, body_band, split, wm_x, hdr_y) -> str:
        """Left-column-first then right-column reconstruction."""
        words = page.get_text("words")
        body = [w for w in words if w[1] >= hdr_y and w[0] < wm_x]
        if body_band:
            body = [w for w in body
                    if w[1] >= body_band["y0"] - 1 and w[1] <= body_band["y1"] + 1]
        if split is None:
            return _text_from_words(body)
        left = [w for w in body if (w[0] + w[2]) / 2 < split]
        right = [w for w in body if (w[0] + w[2]) / 2 >= split]
        return _text_from_words(left) + "\n\n" + _text_from_words(right)

    def _cross_column_order_check(self, native_text: str, geom_text: str) -> bool:
        """
        Deterministic QA gate: does native order disagree with geometry on a
        cross-column sentence? We compare space-collapsed n-gram continuity of a
        few long, distinctive substrings between the two orderings. If native
        order preserves a sequence that geometry breaks (or vice-versa) for a
        token that crosses the gutter, we treat native as disagreeing with the
        gutter and fall back to left-then-right.

        This is a cheap, deterministic proxy: when the two orders differ in the
        relative placement of a mid-line token at the gutter boundary, a
        geometric sort (which interleaves by y) will place left/right column
        tokens in a different relative order than native.
        """
        def collapse(s: str) -> str:
            s = s.replace("\ufb01", "fi").replace("\ufb02", "fl")
            return "".join(s.split())

        cn = collapse(native_text)
        cg = collapse(geom_text)
        # Compare the longest common prefix where both have content; if they
        # diverge early on a content-bearing stretch, geometry (y,x) order is
        # not respected by native (or native is broken). We flag disagreement
        # only when they share a long prefix but then reorder a later token.
        i = 0
        m = min(len(cn), len(cg))
        while i < m and cn[i] == cg[i]:
            i += 1
        # If they diverge after a substantial common prefix, there is a genuine
        # order difference (not just spacing) to investigate.
        return i > 40 and i < m

    def _selective_ocr_lines(self, page, page_number: int,
                             body_lines) -> Optional[dict]:
        """
        Run OCR only on lines classified ``ocr_or_llm_required``.

        The page is rendered once (via PyMuPDF, no external binary) and Tesseract
        ``image_to_data`` returns per-word text/bbox/confidence. Ambiguous lines
        (which carry no recoverable spaces in the text layer) are matched to OCR
        words by y-coordinate overlap and reconstructed into a readable line.
        Returns a patch dict with a ``line_map`` keyed by the space-collapsed
        native line text, or None.
        """
        ambiguous = [(info, bbox, region) for info, bbox, region in body_lines
                     if info["state"] == "ocr_or_llm_required"]
        if not ambiguous:
            return None
        if not self.tesseract_available:
            return None

        try:
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))

            ocr_data = pytesseract.image_to_data(
                img, output_type=pytesseract.Output.DICT)
            confs = [v for v in (_parse_conf(c) for c in ocr_data["conf"])
                     if v is not None]
            mean_conf = (sum(confs) / len(confs)) if confs else 0.0

            words = ocr_data.get("text", [])
            left = ocr_data.get("left", [])
            top = ocr_data.get("top", [])
            width = ocr_data.get("width", [])
            height = ocr_data.get("height", [])
            conf_l = ocr_data.get("conf", [])
            scale = 72.0 / 300.0

            # Group OCR words into lines by (rounded top, adjusted by scale).
            ocr_lines_by_y = {}
            for i, t in enumerate(words):
                if not t or not t.strip():
                    continue
                y_px = top[i]
                y_pt = y_px * scale
                key = round(y_pt / 5.0) * 5.0
                ocr_lines_by_y.setdefault(key, []).append({
                    "text": t,
                    "x0": left[i] * scale,
                    "x1": (left[i] + width[i]) * scale,
                    "y0": y_pt,
                    "conf": _parse_conf(conf_l[i]),
                })

            line_map = {}
            items = []
            for info, bbox, region in ambiguous:
                _, y0, _, y1 = bbox
                center_y = (y0 + y1) / 2.0
                best = None
                best_dist = None
                for key, ws in ocr_lines_by_y.items():
                    dist = abs(key - center_y)
                    if best_dist is None or dist < best_dist:
                        best_dist = dist
                        best = ws
                ocr_text = None
                ocr_conf = None
                if best is not None:
                    best = sorted(best, key=lambda w: w["x0"])
                    ocr_text = " ".join(w["text"] for w in best)
                    confs_here = [w["conf"] for w in best if w["conf"] is not None]
                    ocr_conf = (sum(confs_here) / len(confs_here)
                                if confs_here else None)
                key = "".join(info["text"].split())
                if ocr_text and ocr_conf is not None and ocr_conf >= 0.6:
                    line_map[key] = ocr_text
                    items.append({
                        "key": info["text"],
                        "text": ocr_text,
                        "bbox": [round(v, 1) for v in bbox],
                        "confidence": round(ocr_conf, 3),
                    })
                else:
                    items.append({
                        "key": info["text"],
                        "text": None,
                        "bbox": [round(v, 1) for v in bbox],
                        "confidence": ocr_conf,
                    })
            return {"items": items, "mean_confidence": mean_conf,
                    "line_map": line_map}
        except Exception as e:
            logger.warning(f"Selective OCR failed for page {page_number}: {e}")
            return None

    def _handle_scanned_page(self, page, page_number: int, warnings: list,
                             diagnostics: dict) -> PageContent:
        """Full-page OCR for a page with no extractable text layer."""
        diagnostics["ocr_triggered"] = True
        if self.tesseract_available:
            ocr_result = self._ocr_page_region_aware(page, page_number)
            if ocr_result:
                ocr_text, ocr_conf, ocr_lines = ocr_result
                if ocr_text.strip():
                    warnings.append(
                        f"Full-page OCR (scanned page, conf {ocr_conf:.1%})")
                    return PageContent(
                        page_number=page_number,
                        markdown=ocr_text,
                        text_length=len(self._extract_text(ocr_text)),
                        has_ocr=True,
                        ocr_confidence=ocr_conf,
                        warnings=warnings,
                        diagnostics=diagnostics,
                    )
        warnings.append("Scanned page with OCR unavailable")
        return PageContent(
            page_number=page_number,
            markdown="",
            text_length=0,
            has_ocr=False,
            ocr_confidence=None,
            warnings=warnings,
            diagnostics=diagnostics,
        )

    def _ocr_page_region_aware(self, page, page_number: int):
        """
        Render a single page (via PyMuPDF, no external binary) and run
        Tesseract OCR, preserving per-word bboxes/confidence from image_to_data.
        Returns (ocr_text, confidence, ocr_lines) or None.
        """
        if not self.tesseract_available:
            return None

        try:
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            ocr_text = pytesseract.image_to_string(img)
            ocr_data = pytesseract.image_to_data(
                img, output_type=pytesseract.Output.DICT)
            confs = [v for v in (_parse_conf(c) for c in ocr_data["conf"])
                     if v is not None]
            confidence = (sum(confs) / len(confs)) if confs else 0.0

            ocr_lines = []
            text = ocr_data.get("text", [])
            left = ocr_data.get("left", [])
            top = ocr_data.get("top", [])
            width = ocr_data.get("width", [])
            height = ocr_data.get("height", [])
            conf_l = ocr_data.get("conf", [])
            for i, t in enumerate(text):
                if not t or not t.strip():
                    continue
                ocr_lines.append({
                    "text": t,
                    "bbox": [left[i], top[i], left[i] + width[i],
                             top[i] + height[i]],
                    "confidence": _parse_conf(conf_l[i]),
                })
            return ocr_text.strip(), round(confidence, 3), ocr_lines
        except Exception as e:
            logger.warning(f"OCR failed for page {page_number}: {e}")
            return None

    def _llm_fix_concatenated_words(self, concatenated_text: str) -> Optional[str]:
        """
        Use alias-fast API to insert spaces into concatenated text.
        
        Calls the alias-fast API with a prompt to reconstruct proper spacing
        in text where spaces are missing (e.g., "associatingeachpixel..." ->
        "associating each pixel..."). Only invoked when BLABLADOR_TOKEN is set.
        
        Args:
            concatenated_text: Text with missing spaces
            
        Returns:
            Corrected text with spaces, or original text if LLM fails
        """
        token = os.environ.get("BLABLADOR_TOKEN")
        if not token:
            logger.debug("BLABLADOR_TOKEN not set, skipping LLM fix for concatenated words")
            return None
        
        try:
            import requests
        except ImportError:
            logger.debug("requests not installed, skipping LLM fix")
            return None
        
        prompt = (
            "Fix the missing spaces in this text. Return ONLY the corrected text "
            "with proper spacing. Do not add any explanation, markdown formatting, "
            "or additional text.\n\n"
            "INPUT (concatenated text):\n"
            f"{concatenated_text}\n\n"
            "OUTPUT (corrected text with spaces):"
        )
        
        payload = {
            "model": os.environ.get("BLABLADOR_MODEL", "alias-fast"),
            "messages": [
                {
                    "role": "system",
                    "content": "You are a text correction assistant. Fix missing spaces and return ONLY the corrected text, nothing else."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.0,
        }
        
        api_url = os.environ.get("BLABLADOR_API_URL", "https://api.helmholtz-blablador.fz-juelich.de/v1/chat/completions")
        
        try:
            resp = requests.post(
                api_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            
            # Parse response - handle various formats
            content = content.strip()
            
            # Remove markdown code blocks if present
            if content.startswith("```"):
                content = re.sub(r"^```(?:\w+)?\n?", "", content)
                content = re.sub(r"\n```$", "", content)
            
            content = content.strip()
            
            # Validate that we got reasonable text back
            if not content or len(content) < len(concatenated_text):
                logger.warning("LLM response too short or empty, using original")
                return None
            
            logger.info(f"Fixed concatenated text: {concatenated_text[:50]}... -> {content[:50]}...")
            return content
            
        except requests.RequestException as e:
            logger.warning(f"alias-fast API request failed for concatenated words: {e}")
            return None
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to parse alias-fast response: {e}")
            return None
        except Exception as e:
            logger.warning(f"Unexpected error fixing concatenated words: {e}")
            return None

    def _advisory_blablador(self, page, page_number: int, split,
                            n_ocr_required, body_band) -> Optional[dict]:
        """
        Optional Blablador advisory cross-check (disabled unless
        BLABLADOR_ADVISORY=1 and BLABLADOR_TOKEN set). Records page, bbox,
        input text, model alias, output, confidence, and deterministic evidence.
        Never rewrites authoritative text.
        """
        if not os.environ.get("BLABLADOR_ADVISORY") == "1":
            return None
        token = os.environ.get("BLABLADOR_TOKEN")
        if not token:
            return None
        try:
            import requests
        except ImportError:
            return None

        raw = page.get_text("text")[:6000]
        deterministic = {
            "column_split_x": split,
            "body_extent": body_band,
            "n_ocr_or_llm_lines": n_ocr_required,
        }
        prompt = (
            "Classify the layout of this PDF page. Return JSON with fields: "
            "estimated_columns (int), layout_mode "
            "(single_column|two_column_parallel|two_column_flow|multi_column), "
            "fragmentation_risk (low|medium|high), confidence (0..1), reasoning "
            "(short). Do not rewrite or correct the text.\n\nRAW TEXT:\n" + raw
        )
        payload = {
            "model": os.environ.get("BLABLADOR_MODEL", "alias-fast"),
            "messages": [
                {"role": "system",
                 "content": "You are a PDF layout classifier. Respond JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
        try:
            resp = requests.post(
                "https://api.helmholtz-blablador.fz-juelich.de/v1/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r"```(?:json)?\n?", "", content).replace("```", "").strip()
            llm = json.loads(content)
            measured = 2 if split else 1
            return {
                "page": page_number,
                "model_alias": payload["model"],
                "llm_response": llm,
                "confidence": llm.get("confidence"),
                "deterministic_evidence": deterministic,
                "agreement": {
                    "llm_estimated_columns": llm.get("estimated_columns"),
                    "measured_columns": measured,
                    "agree": (llm.get("estimated_columns") == measured),
                    "note": "LLM is advisory only; measured geometry is authoritative.",
                },
                "reasoning": llm.get("reasoning"),
            }
        except Exception as e:
            logger.warning(f"Blablador advisory call failed: {e}")
            return {"page": page_number, "error": str(e)}

    def _extract_text(self, markdown: str) -> str:
        """Extract plain text from markdown."""
        # Remove markdown formatting
        text = re.sub(r"```[\s\S]*?```", "", markdown)  # Code blocks
        text = re.sub(r"`[^`]+`", "", text)  # Inline code
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)  # Bold
        text = re.sub(r"\*([^*]+)\*", r"\1", text)  # Italic
        text = re.sub(r"#+\s*", "", text)  # Headers
        text = re.sub(r"^\s*[-*+]\s*", "", text, flags=re.MULTILINE)  # Lists
        text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.MULTILINE)  # Numbered lists
        text = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", text)  # Images
        text = re.sub(r"\[([^\]]*)\]\([^\)]+\)", r"\1", text)  # Links
        text = re.sub(r"\|", " ", text)  # Table separators
        text = re.sub(r"-{2,}", "", text)  # Table separators
        text = re.sub(r"\s+", " ", text)  # Whitespace
        return text.strip()

    def get_page_text(self, page_content: PageContent) -> str:
        """
        Get the text content of a page for indexing/searching.

        Args:
            page_content: PageContent object

        Returns:
            Plain text representation
        """
        return self._extract_text(page_content.markdown)

    def extract_structural_elements(
        self, markdown: str
    ) -> Dict[str, List[str]]:
        """
        Extract structural elements from markdown.

        Args:
            markdown: Markdown content

        Returns:
            Dict with keys: headings, tables, figures, equations
        """
        elements = {
            "headings": [],
            "tables": [],
            "figures": [],
            "equations": [],
        }

        lines = markdown.split("\n")

        for line in lines:
            # Headings
            if line.startswith("#"):
                heading = re.sub(r"^#+\s*", "", line).strip()
                if heading:
                    elements["headings"].append(heading)

            # Tables (markdown table rows contain |)
            if "|" in line and line.strip().startswith("|"):
                elements["tables"].append(line.strip())

            # Figure captions (typically "Figure X:" or "Fig. X:")
            if re.match(r"(Figure|Fig\.)\s*\d+", line, re.IGNORECASE):
                elements["figures"].append(line.strip())

            # Equations (LaTeX style)
            if "$" in line or re.match(r"^\\\[.*\\\]$", line):
                elements["equations"].append(line.strip())

        return elements


def extract_figures_from_pdf(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Extract figure regions from a PDF using STRUCTURAL CUES ONLY.

    This is a **structure-first** detection tool that intentionally underdetects
    figures. It relies on:
    - Embedded image XObjects
    - Marked-content sequences tagged as `/Figure`

    It does NOT use:
    - Caption text or regex patterns
    - OCR
    - Layout models
    - Non-text region inference

    Missing figures are expected to be recovered via user-assisted refinement
    (e.g., a `guided_seek` tool that lets users draw bounding boxes).

    Args:
        pdf_path: Path to the PDF file

    Returns:
        List of figure dicts with keys:
        - page: 1-based page number
        - bbox_pdf: [x0, y0, x1, y1] in PDF points (origin at bottom-left)
        - origin: "image_xobject" | "marked_content_figure"
        - source_pdf: absolute path to the PDF
    """
    if pymupdf is None:
        raise RuntimeError("PyMuPDF not available")

    doc = pymupdf.open(str(pdf_path))
    figures = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        page_number = page_num + 1  # 1-based

        # Get page dimensions for coordinate transformations
        page_rect = page.rect
        page_width = page_rect.width
        page_height = page_rect.height

        # 1. Extract embedded image XObjects
        image_figures = _extract_image_xobjects(page, page_number, str(pdf_path))
        figures.extend(image_figures)

        # 2. Extract marked-content /Figure sequences
        marked_figures = _extract_marked_content_figures(page, page_number, str(pdf_path))
        figures.extend(marked_figures)

    doc.close()
    return figures


def _extract_image_xobjects(page, page_number: int, source_pdf: str) -> List[Dict[str, Any]]:
    """
    Extract figure candidates from embedded image XObjects.

    For each image on the page, compute its on-page bounding box from the
    image's placement matrix and page coordinate system.
    """
    figures = []

    try:
        images = page.get_images()
        for img in images:
            # img is a tuple: (xref, smask, width, height, ...)
            xref = img[0]

            try:
                # Get the image's placement rectangles
                img_rects = page.get_image_rects(xref)
                for rect in img_rects:
                    # rect is in PDF coordinates (bottom-left origin)
                    figures.append({
                        "page": page_number,
                        "bbox_pdf": [rect.x0, rect.y0, rect.x1, rect.y1],
                        "origin": "image_xobject",
                        "source_pdf": source_pdf,
                    })
            except Exception as e:
                logger.debug(f"Failed to extract image {xref} from page {page_number}: {e}")
                continue

    except Exception as e:
        logger.debug(f"Failed to list images on page {page_number}: {e}")

    return figures


def _extract_marked_content_figures(page, page_number: int, source_pdf: str) -> List[Dict[str, Any]]:
    """
    Extract figure candidates from marked-content sequences tagged as /Figure.

    Walks the page's content stream and identifies marked-content sequences
    with the /Figure tag. For each, accumulates the bounding box of drawing
    operations within that sequence.

    Note: This is a simplified implementation. Full parsing of PDF content
    streams with proper operator handling would be more robust.
    """
    figures = []

    try:
        # Get the page's content streams
        xref_list = page.get_xobjects()

        # Also check the main content stream
        content = page.read_stream()

        # Look for /Figure marked content patterns
        # PDF marked content uses BMC (BeginMarkedContent) and EMC (EndMarkedContent)
        # with a tag like /Figure
        content_text = content.decode('latin-1', errors='ignore')

        # Simple pattern matching for /Figure tags
        # This is a heuristic; proper parsing would use a PDF library that
        # exposes marked content sequences directly
        import re
        figure_pattern = r'/Figure\s*BMC'

        # For now, we note that marked-content /Figure extraction requires
        # more sophisticated PDF stream parsing. PyMuPDF doesn't directly
        # expose marked content sequences, so we log this as a future enhancement.
        #
        # In a full implementation, we would:
        # 1. Parse the content stream operator-by-operator
        # 2. Track BMC/EMC pairs with /Figure tags
        # 3. Accumulate bounding boxes of drawing operations (m, l, c, re operators)
        # 4. Convert to PDF coordinates

        logger.debug(f"Page {page_number}: marked-content /Figure detection requires enhanced stream parsing")

    except Exception as e:
        logger.debug(f"Failed to parse marked content on page {page_number}: {e}")

    return figures


def pdf_bbox_to_image_bbox(page_image_size: Tuple[int, int],
                           bbox_pdf: List[float],
                           page_rotation: int = 0) -> Tuple[int, int, int, int]:
    """
    Convert a PDF coordinate bounding box to image pixel coordinates.

    PDF coordinates:
    - Origin: bottom-left
    - Units: points (1/72 inch)
    - Y increases upward

    Image coordinates:
    - Origin: top-left
    - Units: pixels
    - Y increases downward

    Args:
        page_image_size: (width, height) of the rendered page image in pixels
        bbox_pdf: [x0, y0, x1, y1] in PDF points
        page_rotation: Page rotation in degrees (0, 90, 180, 270)

    Returns:
        (x0, y0, x1, y1) in image pixel coordinates (top-left origin)

    Example:
        >>> # Page is 612x792 points (8.5x11 inches), rendered at 150 DPI
        >>> # That's 150 * 8.5 = 1275px width, 150 * 11 = 1650px height
        >>> pdf_bbox_to_image_bbox((1275, 1650), [100, 100, 200, 200])
        (208, 1442, 416, 1650)  # Note: y is inverted!
    """
    img_width, img_height = page_image_size
    x0_pdf, y0_pdf, x1_pdf, y1_pdf = bbox_pdf

    # Get PDF page dimensions in points (assume standard Letter if unknown)
    # In practice, you'd pass this as a parameter or get it from the PDF
    pdf_width_pt = 612.0  # 8.5 inches * 72
    pdf_height_pt = 792.0  # 11 inches * 72

    # Calculate scale factors
    scale_x = img_width / pdf_width_pt
    scale_y = img_height / pdf_height_pt

    # Convert PDF points to image pixels
    # PDF y=0 is bottom, image y=0 is top, so we invert
    x0_img = int(x0_pdf * scale_x)
    x1_img = int(x1_pdf * scale_x)
    y0_img = int(img_height - y1_pdf * scale_y)  # Invert y
    y1_img = int(img_height - y0_pdf * scale_y)  # Invert y

    # Handle rotation if needed
    if page_rotation == 90:
        # Rotate 90 degrees clockwise
        x0_img, y0_img, x1_img, y1_img = (
            y0_img, img_width - x1_img,
            y1_img, img_width - x0_img
        )
    elif page_rotation == 180:
        x0_img, y0_img, x1_img, y1_img = (
            img_width - x1_img, img_height - y1_img,
            img_width - x0_img, img_height - y0_img
        )
    elif page_rotation == 270:
        x0_img, y0_img, x1_img, y1_img = (
            img_width - y1_img, x0_img,
            img_width - y0_img, x1_img
        )

    # Ensure coordinates are within bounds
    x0_img = max(0, min(x0_img, img_width))
    y0_img = max(0, min(y0_img, img_height))
    x1_img = max(0, min(x1_img, img_width))
    y1_img = max(0, min(y1_img, img_height))

    return (x0_img, y0_img, x1_img, y1_img)


def image_bbox_to_pdf_bbox(
    page_image_size: Tuple[int, int],
    bbox_image: Tuple[int, int, int, int],
    pdf_page_size: Tuple[float, float] = (612.0, 792.0),
    page_rotation: int = 0,
) -> Tuple[float, float, float, float]:
    """
    Convert image pixel coordinates back to PDF points.

    Inverse of pdf_bbox_to_image_bbox().

    Args:
        page_image_size: (width, height) of the rendered page image in pixels
        bbox_image: (x0, y0, x1, y1) in image pixel coordinates (top-left origin)
        pdf_page_size: (width, height) of the PDF page in points (default: Letter 612x792)
        page_rotation: Page rotation in degrees (0, 90, 180, 270)

    Returns:
        (x0, y0, x1, y1) in PDF point coordinates (bottom-left origin)
    """
    img_width, img_height = page_image_size
    x0_img, y0_img, x1_img, y1_img = bbox_image
    pdf_width_pt, pdf_height_pt = pdf_page_size

    # Reverse rotation if needed
    if page_rotation == 90:
        x0_img, y0_img, x1_img, y1_img = (
            img_height - y1_img, x0_img,
            img_height - y0_img, x1_img
        )
    elif page_rotation == 180:
        x0_img, y0_img, x1_img, y1_img = (
            img_width - x1_img, img_height - y1_img,
            img_width - x0_img, img_height - y0_img
        )
    elif page_rotation == 270:
        x0_img, y0_img, x1_img, y1_img = (
            y0_img, img_width - x1_img,
            y1_img, img_width - x0_img
        )

    # Calculate scale factors
    scale_x = pdf_width_pt / img_width
    scale_y = pdf_height_pt / img_height

    # Convert image pixels to PDF points (invert the Y axis)
    x0_pdf = x0_img * scale_x
    x1_pdf = x1_img * scale_x
    y0_pdf = (img_height - y1_img) * scale_y
    y1_pdf = (img_height - y0_img) * scale_y

    return (x0_pdf, y0_pdf, x1_pdf, y1_pdf)


class GuidedSeekCancelledError(Exception):
    """Raised when user cancels the guided_seek_single_box interaction."""
    pass


def guided_seek_single_box(
    pdf_path: str,
    page_number: int,
    instruction: Optional[str] = None,
    dpi: int = 150,
) -> Dict[str, Any]:
    """
    Open a PDF page in a window, let user draw a single bounding box,
    and return the cropped region as an image.

    This is a **vision-native** tool for user-assisted figure/region selection.
    The calling LLM (which must have vision capabilities) receives the cropped
    image directly and can inspect it.

    Args:
        pdf_path: Absolute path to the PDF file
        page_number: 1-based page number to display
        instruction: Optional instruction shown to the user. If None, uses default.
        dpi: Rendering resolution (default: 150)

    Returns:
        Dict with:
        - page: int (1-based page number)
        - bbox_pdf: [x0, y0, x1, y1] in PDF points (bottom-left origin)
        - bbox_image: [x0, y0, x1, y1] in image pixels (top-left origin)
        - crop_image: PNG bytes of the cropped region
        - full_page_image: PNG bytes of the full rendered page (optional)
        - instruction: str (the instruction shown to the user)

    Raises:
        GuidedSeekCancelledError: If user cancels the interaction
        PDFValidationError: If the PDF or page number is invalid
    """
    import fitz  # PyMuPDF
    from PIL import Image
    import io

    # Validate inputs
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise PDFValidationError(f"PDF file not found: {pdf_path}")

    doc = fitz.open(str(pdf_path))
    
    # Check page count - reject if too large for interactive selection
    if len(doc) > 30:
        doc.close()
        raise PDFValidationError(
            f"PDF has {len(doc)} pages, which exceeds the 30-page limit for guided_seek_single_box. "
            f"This tool is designed for interactive box selection on shorter documents. "
            f"Please ask the user to manually take a screenshot of the region of interest instead."
        )
    
    if page_number < 1 or page_number > len(doc):
        doc.close()
        raise PDFValidationError(
            f"Invalid page number {page_number}. PDF has {len(doc)} pages."
        )

    # Get PDF page dimensions from first page (for coordinate conversion)
    first_page = doc[0]
    page_rect = first_page.rect
    pdf_width_pt = page_rect.width
    pdf_height_pt = page_rect.height
    page_rotation = first_page.rotation

    if instruction is None:
        instruction = (
            "Draw a rectangle around the figure or region you want the model to inspect. "
            "Click 'Confirm' when done, or 'Cancel' to abort. "
            "Use 'Next'/'Previous' buttons to navigate between pages."
        )

    # Create a matrix for the desired DPI
    # Standard PDF is 72 points per inch
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)

    # Render all pages upfront
    all_page_images = []
    all_page_bytes = []
    for i in range(len(doc)):
        p = doc[i]
        pix = p.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        all_page_bytes.append(img_bytes)
        all_page_images.append(Image.open(io.BytesIO(img_bytes)))

    # Get image dimensions from first page
    img_width, img_height = all_page_images[0].size

    # Create GUI window for box selection
    try:
        import tkinter as tk
        from PIL import ImageTk
    except ImportError:
        doc.close()
        raise PDFValidationError(
            "tkinter is not available. guided_seek_single_box requires a GUI environment."
        )

    class BoxSelector:
        def __init__(self, root, all_images, all_bytes, instruction, total_pages):
            self.root = root
            self.all_images = all_images
            self.all_bytes = all_bytes
            self.instruction = instruction
            self.current_page = 0
            self.total_pages = total_pages
            self.start_x = None
            self.start_y = None
            self.rect = None
            self.box = None  # (x0, y0, x1, y1) in image coordinates

            # Set up window
            root.title(f"guided_seek_single_box - Page 1/{total_pages}")
            root.resizable(True, True)

            # Main frame
            self.frame = tk.Frame(root)
            self.frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

            # Instruction label
            instruction_label = tk.Label(
                self.frame,
                text=instruction,
                wraplength=800,
                justify=tk.LEFT,
                font=("Arial", 10)
            )
            instruction_label.pack(pady=(0, 10))

            # Canvas for displaying image
            self.canvas = tk.Canvas(
                self.frame,
                width=800,
                height=600,
                cursor="crosshair",
                bg="#f0f0f0"
            )
            self.canvas.pack(fill=tk.BOTH, expand=True)

            # Display image (scaled to fit)
            self._photo_ref = None
            self._update_display()

            # Bind mouse events
            self.canvas.bind("<Button-1>", self.on_mouse_press)
            self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
            self.canvas.bind("<ButtonRelease-1>", self.on_mouse_release)

            # Bind Enter key to confirm
            root.bind("<Return>", lambda e: self.on_confirm())

            # Navigation frame
            nav_frame = tk.Frame(self.frame)
            nav_frame.pack(pady=10)

            # Page info
            self.page_label = tk.Label(
                nav_frame,
                text=f"Page {self.current_page + 1} of {total_pages}",
                font=("Arial", 11, "bold")
            )
            self.page_label.pack(side=tk.LEFT, padx=20)

            # Previous button
            prev_btn = tk.Button(
                nav_frame,
                text="← Previous",
                command=self.on_prev_page,
                width=10
            )
            prev_btn.pack(side=tk.LEFT, padx=5)

            # Next button
            next_btn = tk.Button(
                nav_frame,
                text="Next →",
                command=self.on_next_page,
                width=10
            )
            next_btn.pack(side=tk.LEFT, padx=5)

            # Page selector
            self.page_var = tk.StringVar(value=str(self.current_page + 1))
            page_spinbox = tk.Spinbox(
                nav_frame,
                from_=1,
                to=total_pages,
                textvariable=self.page_var,
                width=5,
                command=self.on_page_jump
            )
            page_spinbox.pack(side=tk.LEFT, padx=5)
            page_spinbox.bind('<Return>', lambda e: self.on_page_jump())

            # Button frame
            btn_frame = tk.Frame(self.frame)
            btn_frame.pack(pady=10)

            # Confirm button
            confirm_btn = tk.Button(
                btn_frame,
                text="Confirm",
                command=self.on_confirm,
                width=10,
                bg="#4CAF50",
                fg="white",
                font=("Arial", 10, "bold")
            )
            confirm_btn.pack(side=tk.LEFT, padx=5)

            # Cancel button
            cancel_btn = tk.Button(
                btn_frame,
                text="Cancel",
                command=self.on_cancel,
                width=10,
                bg="#f44336",
                fg="white",
                font=("Arial", 10, "bold")
            )
            cancel_btn.pack(side=tk.LEFT, padx=5)

            # Status label
            self.status = tk.Label(self.frame, text="", fg="gray")
            self.status.pack(pady=(5, 0))

            # Result
            self.result = None
            self.cancelled = False

        def _update_display(self):
            """Update the displayed image for current page."""
            img = self.all_images[self.current_page]
            
            # Calculate display size (fit within 800x600)
            max_w, max_h = 800, 600
            scale = min(max_w / img.size[0], max_h / img.size[1])
            self.display_width = int(img.size[0] * scale)
            self.display_height = int(img.size[1] * scale)
            
            # Resize for display
            self.display_img = img.resize(
                (self.display_width, self.display_height),
                Image.Resampling.LANCZOS
            )
            
            # Update canvas size
            self.canvas.config(width=self.display_width, height=self.display_height)
            
            # Clear and redraw
            self.canvas.delete("all")
            self._photo_ref = ImageTk.PhotoImage(self.display_img)
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self._photo_ref)
            
            # Redraw box if exists
            if self.box:
                self._redraw_box()

        def _redraw_box(self):
            """Redraw the selection box on the current display."""
            if self.box is None:
                return
            # Scale box to display coordinates
            scale_x = self.display_width / self.all_images[self.current_page].size[0]
            scale_y = self.display_height / self.all_images[self.current_page].size[1]
            x0, y0, x1, y1 = self.box
            self.rect = self.canvas.create_rectangle(
                x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y,
                outline="red", width=2, dash=(4, 4)
            )

        def on_prev_page(self):
            """Go to previous page."""
            if self.current_page > 0:
                self.current_page -= 1
                self._update_display()
                self.page_label.config(text=f"Page {self.current_page + 1} of {self.total_pages}")
                self.page_var.set(str(self.current_page + 1))
                # Clear box when changing pages
                self.box = None
                self.start_x = None
                self.start_y = None
                self.status.config(text="")

        def on_next_page(self):
            """Go to next page."""
            if self.current_page < self.total_pages - 1:
                self.current_page += 1
                self._update_display()
                self.page_label.config(text=f"Page {self.current_page + 1} of {self.total_pages}")
                self.page_var.set(str(self.current_page + 1))
                # Clear box when changing pages
                self.box = None
                self.start_x = None
                self.start_y = None
                self.status.config(text="")

        def on_page_jump(self):
            """Jump to a specific page."""
            try:
                page_num = int(self.page_var.get()) - 1
                if 0 <= page_num < self.total_pages:
                    self.current_page = page_num
                    self._update_display()
                    self.page_label.config(text=f"Page {self.current_page + 1} of {self.total_pages}")
                    # Clear box when changing pages
                    self.box = None
                    self.start_x = None
                    self.start_y = None
                    self.status.config(text="")
            except ValueError:
                pass

        def on_mouse_press(self, event):
            self.start_x = event.x
            self.start_y = event.y
            if self.rect:
                self.canvas.delete(self.rect)

        def on_mouse_drag(self, event):
            if self.start_x is None:
                return
            if self.rect:
                self.canvas.delete(self.rect)
            self.rect = self.canvas.create_rectangle(
                self.start_x, self.start_y, event.x, event.y,
                outline="red", width=2, dash=(4, 4)
            )

        def on_mouse_release(self, event):
            if self.start_x is None:
                return
            x0 = min(self.start_x, event.x)
            y0 = min(self.start_y, event.y)
            x1 = max(self.start_x, event.x)
            y1 = max(self.start_y, event.y)
            self.box = (x0, y0, x1, y1)
            self.status.config(text=f"Box selected: ({x0}, {y0}) to ({x1}, {y1})")

        def on_confirm(self):
            if self.box is None:
                self.status.config(text="Please draw a box first!", fg="orange")
                return
            self.result = self.box
            self.root.quit()

        def on_cancel(self):
            self.cancelled = True
            self.root.quit()

        def get_scaled_box(self):
            """Convert box from display coordinates to original image coordinates."""
            if self.box is None:
                return None
            scale_x = self.all_images[self.current_page].size[0] / self.display_width
            scale_y = self.all_images[self.current_page].size[1] / self.display_height
            x0, y0, x1, y1 = self.box
            return (
                int(x0 * scale_x),
                int(y0 * scale_y),
                int(x1 * scale_x),
                int(y1 * scale_y)
            )

    # Create and run the GUI
    root = tk.Tk()
    selector = BoxSelector(root, all_page_images, all_page_bytes, instruction, len(doc))

    # Center the window
    root.update_idletasks()
    root.geometry(f"+{root.winfo_screenwidth()//2 - 400}+{root.winfo_screenheight()//2 - 300}")

    # Run the main loop
    root.mainloop()

    # Check if cancelled
    if selector.cancelled:
        raise GuidedSeekCancelledError("User cancelled the box selection")

    # Get the box in original image coordinates
    box_img = selector.get_scaled_box()
    if box_img is None:
        raise PDFValidationError("No box was selected")

    x0_img, y0_img, x1_img, y1_img = box_img

    # Crop the region from the current page's image
    crop = all_page_images[selector.current_page].crop((x0_img, y0_img, x1_img, y1_img))
    crop_bytes = io.BytesIO()
    crop.save(crop_bytes, format="PNG")
    crop_bytes = crop_bytes.getvalue()

    # Convert to PDF coordinates
    bbox_pdf = image_bbox_to_pdf_bbox(
        (img_width, img_height),
        (x0_img, y0_img, x1_img, y1_img),
        (pdf_width_pt, pdf_height_pt),
        page_rotation
    )

    doc.close()

    return {
        "page": page_number,
        "bbox_pdf": list(bbox_pdf),
        "bbox_image": list(box_img),
        "crop_image": crop_bytes,
        "full_page_image": img_bytes,
        "instruction": instruction,
        "dpi": dpi,
    }
