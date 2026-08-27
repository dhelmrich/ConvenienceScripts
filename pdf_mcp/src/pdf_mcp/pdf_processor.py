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
