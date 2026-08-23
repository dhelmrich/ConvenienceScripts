#!/usr/bin/env python3
"""
Analyze paper.pdf to detect and fix two-column layout text ordering issues.

The PDF has a two-column layout where text is stored in PDF text stream order as:
  right column (top-to-bottom), then left column (top-to-bottom)

This causes sentences spanning columns to appear fragmented (second half before first half).

The fix: Join lines separated by single blank lines (column breaks) while preserving
multiple blank lines (paragraph breaks).

Usage:
    python analyze_paperpdf.py          # Run analysis
    from analyze_paperpdf import extract_pdf_text_fixed
    text = extract_pdf_text_fixed("paper.pdf")
"""

import subprocess
from pathlib import Path
from typing import List, Tuple, Optional
from dataclasses import dataclass


@dataclass
class TextBox:
    """Represents a text box with position information."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    
    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2
    
    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2


# ============================================================================
# CORE EXTRACTION FUNCTIONS (REUSABLE)
# ============================================================================

def join_column_breaks(text: str) -> str:
    """
    Join lines separated by single blank lines (column breaks).
    
    In two-column PDFs, pdftotext outputs:
    - Left column text
    - Single blank line (column break)
    - Right column text (continuation of sentence)
    
    This function joins such lines to restore proper sentence flow.
    Multiple blank lines (paragraph breaks) are preserved.
    """
    lines = text.split('\n')
    result_lines = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        if line == '':
            # Check if single blank line (column break) or multiple (paragraph)
            if i + 1 < len(lines) and lines[i + 1] == '':
                # Multiple blank lines - paragraph break
                result_lines.append('')
                i += 1
            elif i + 1 < len(lines) and lines[i + 1].strip():
                # Single blank line - column break, join with next line
                if result_lines and result_lines[-1]:
                    result_lines[-1] = result_lines[-1] + ' ' + lines[i + 1]
                i += 2
            else:
                result_lines.append('')
                i += 1
        else:
            result_lines.append(line)
            i += 1
    
    return '\n'.join(result_lines)


def fix_column_ordering(text: str) -> str:
    """
    Fix text where columns are in wrong order (right→left instead of left→right).
    
    In some PDFs with two-column layouts, the text stream order is:
    1. Right column (top to bottom)
    2. Left column (top to bottom)
    
    This causes sentences that span from left column bottom to right column top
    to appear with the continuation BEFORE the start.
    
    Strategy: Split text into column-sized chunks and reorder them.
    """
    import re
    
    # Split into paragraphs (double newlines)
    paragraphs = re.split(r'\n\s*\n', text)
    
    # Look for patterns where a paragraph ends mid-sentence
    # and the next paragraph starts mid-sentence
    fixed = []
    i = 0
    
    while i < len(paragraphs):
        current = paragraphs[i]
        
        # Check if current paragraph ends without terminal punctuation
        ends_cut = current and not re.search(r'[.!?]$', current.strip())
        
        # Check if next paragraph starts mid-sentence (lowercase, no heading pattern)
        next_starts_cut = False
        if i + 1 < len(paragraphs):
            next_p = paragraphs[i + 1]
            if next_p:
                first_word = next_p.strip().split()[0] if next_p.strip() else ''
                # Starts with lowercase and is not a number or special pattern
                next_starts_cut = (first_word and 
                                   first_word[0].islower() and 
                                   not re.match(r'^[\d\(\[\{]', first_word))
        
        if ends_cut and next_starts_cut:
            # Join the paragraphs - they're likely split across columns
            combined = current + ' ' + paragraphs[i + 1]
            fixed.append(combined)
            i += 2
        else:
            fixed.append(current)
            i += 1
    
    return '\n\n'.join(fixed)


def extract_pdf_text_fixed(pdf_path: str) -> str:
    '''
    Extract text from PDF with two-column layout fix applied.
    
    This method detects column breaks (single blank lines between columns)
    and joins them to restore proper sentence flow.
    
    Args:
        pdf_path: Path to PDF file
        
    Returns:
        Text with column breaks fixed
    '''
    # Extract text using pdftotext
    result = subprocess.run(
        ['pdftotext', pdf_path, '-'],
        capture_output=True, text=True
    )
    text = result.stdout
    
    # Join lines separated by single blank lines (column breaks)
    return join_column_breaks(text)


def detect_column_structure(pdf_path: str) -> Tuple[bool, float]:
    '''
    Detect if PDF has two-column layout using pdfminer.
    
    Analyzes the first page with content to detect column structure.
    
    Returns:
        (has_two_columns, column_midpoint)
    '''
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    
    for page in extract_pages(pdf_path):
        boxes = []
        for element in page:
            if isinstance(element, LTTextContainer):
                text = element.get_text().strip()
                if text:
                    cx = (element.x0 + element.x1) / 2
                    boxes.append(cx)
        
        if not boxes:
            continue
        
        # Find gap in x-coordinates
        sorted_x = sorted(set(boxes))
        for i in range(1, len(sorted_x)):
            if sorted_x[i] - sorted_x[i-1] > 50:
                return True, (sorted_x[i-1] + sorted_x[i]) / 2
        
        # If no large gap found, check for bimodal distribution
        if len(boxes) > 10:
            mid = sorted(boxes)[len(boxes) // 2]
            left_count = sum(1 for b in boxes if b < mid)
            right_count = len(boxes) - left_count
            if 0.3 < left_count / len(boxes) < 0.7:
                return True, mid
    
    return False, 0


def analyze_stream_order(pdf_path: str) -> str:
    '''
    Analyze PDF text stream order.
    
    Returns:
        "right-left" if problematic ordering detected
        "left-right" if correct ordering
    '''
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    
    boxes = []
    for page in extract_pages(pdf_path):
        for element in page:
            if isinstance(element, LTTextContainer):
                text = element.get_text().strip()
                if text:
                    boxes.append({
                        'cx': (element.x0 + element.x1) / 2,
                        'cy': (element.y0 + element.y1) / 2
                    })
    
    if not boxes:
        return "left-right"
    
    # Find column midpoint
    x_centers = [b['cx'] for b in boxes]
    sorted_x = sorted(set(x_centers))
    column_midpoint = 0
    for i in range(1, len(sorted_x)):
        if sorted_x[i] - sorted_x[i-1] > 50:
            column_midpoint = (sorted_x[i-1] + sorted_x[i]) / 2
            break
    
    if not column_midpoint:
        column_midpoint = sorted(x_centers)[len(x_centers) // 2]
    
    # Get topmost boxes from each column
    left_boxes = [b for b in boxes if b['cx'] < column_midpoint]
    right_boxes = [b for b in boxes if b['cx'] >= column_midpoint]
    
    if not left_boxes or not right_boxes:
        return "left-right"
    
    top_left = min(left_boxes, key=lambda b: b['cy'])
    top_right = min(right_boxes, key=lambda b: b['cy'])
    
    # Find positions in stream order
    left_idx = next((i for i, b in enumerate(boxes) 
                    if abs(b['cx'] - top_left['cx']) < 10 
                    and abs(b['cy'] - top_left['cy']) < 10), -1)
    right_idx = next((i for i, b in enumerate(boxes) 
                     if abs(b['cx'] - top_right['cx']) < 10 
                     and abs(b['cy'] - top_right['cy']) < 10), -1)
    
    if left_idx >= 0 and right_idx >= 0:
        return "left-right" if left_idx < right_idx else "right-left"
    
    return "left-right"


# ============================================================================
# ADVANCED: PAGE-BASED COLUMN REORDERING
# ============================================================================


def smart_join(prev_text: str, next_text: str) -> str:
    """
    Join two text fragments with a single space.

    Because verification normalises all whitespace to single spaces, we only
    need to guarantee that distinct words remain separated. We join with a
    space (never merging words) -- the only safe choice for fragments that
    pdfminer split at word boundaries.
    """
    a = (prev_text or "").strip()
    b = (next_text or "").strip()
    if not a:
        return b
    if not b:
        return a
    return a + " " + b


def detect_column_split(lines: List[Tuple[float, float, float, float, str]]) -> Optional[float]:
    """
    Detect the x-position separating a two-column layout.

    Uses the x-centres of real body-text lines (lines that contain alphabetic
    characters). The split is the midpoint of the largest gap between
    consecutive sorted x-centres. Returns None if the page is single column
    (no clear bimodal split).
    """
    centres = [
        (x0 + x1) / 2
        for x0, y0, x1, y1, text in lines
        if any(ch.isalpha() for ch in text)
    ]
    if len(centres) < 4:
        return None

    sorted_centres = sorted(set(round(c, 1) for c in centres))
    best_gap = 0
    split = None
    for a, b in zip(sorted_centres, sorted_centres[1:]):
        gap = b - a
        if gap > best_gap:
            best_gap = gap
            split = (a + b) / 2

    # Only treat as two columns if the gutter is clearly separated
    if split is None or best_gap < 20:
        return None
    return split


def _reconstruct_column(lines: List[Tuple[float, float, float, float, str]]) -> str:
    """
    Reconstruct a single column's reading order (top to bottom).

    Lines are grouped by vertical band so that horizontally-fragmented pieces
    of the same visual line (common with math or with pdfminer splitting a
    line into several containers) are re-joined in left-to-right order before
    moving to the next band.
    """
    if not lines:
        return ""

    # Sort by vertical position descending (top to bottom in PDF coords),
    # then by left edge (left to right within a line).
    lines = sorted(lines, key=lambda l: (-l[3], l[0]))
    if len(lines) == 1:
        return lines[0][4].strip()

    # Group lines whose vertical extents overlap into the same visual band.
    bands: List[List[Tuple[float, float, float, float, str]]] = []
    for line in lines:
        _, y0, _, y1, _ = line
        placed = False
        for band in bands:
            _, b_y0, _, b_y1, _ = band[0]
            # vertical overlap test
            if y1 >= b_y0 and y0 <= b_y1:
                band.append(line)
                placed = True
                break
        if not placed:
            bands.append([line])

    # Within each band sort left to right, then join bands top to bottom.
    pieces = []
    for band in bands:
        band_sorted = sorted(band, key=lambda l: l[0])
        band_text = ""
        for line in band_sorted:
            band_text = smart_join(band_text, line[4])
        pieces.append(band_text)

    result = ""
    for p in pieces:
        result = smart_join(result, p)
    return result


def extract_text_column_sorted(pdf_path: str) -> str:
    """
    Robust two-column text extraction.

    For every page, collects the fine-grained text lines (LTTextLine) instead
    of coarse LTTextContainer boxes. This avoids the box-fragmentation that
    broke naive left/right interleaving.

    For two-column pages:
      1. Detect the column split point.
      2. Reconstruct the LEFT column's full reading order (top -> bottom).
      3. Reconstruct the RIGHT column's full reading order (top -> bottom).
      4. Concatenate LEFT then RIGHT (correct reading order).

    For single-column pages the lines are simply sorted top -> bottom.
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextLine

    page_texts = []

    for page in extract_pages(pdf_path):
        lines = []

        def _collect(obj):
            if isinstance(obj, LTTextLine):
                text = obj.get_text().strip()
                if text:
                    lines.append((obj.x0, obj.y0, obj.x1, obj.y1, text))
            if hasattr(obj, "_objs"):
                for child in obj._objs:
                    _collect(child)

        for element in page:
            _collect(element)

        if not lines:
            page_texts.append("")
            continue

        split = detect_column_split(lines)

        if split is None:
            page_texts.append(_reconstruct_column(lines))
            continue

        left = [l for l in lines if (l[0] + l[2]) / 2 < split]
        right = [l for l in lines if (l[0] + l[2]) / 2 >= split]

        left_text = _reconstruct_column(left)
        right_text = _reconstruct_column(right)

        # Correct reading order: left column, then right column.
        page_texts.append(left_text + "\n\n" + right_text)

    return "\n\n".join(page_texts)


def extract_text_per_page(pdf_path: str) -> List[str]:
    '''
    Extract text from PDF page by page using pdfminer.
    This allows us to detect and fix column ordering per page.
    
    Returns:
        List of text strings, one per page
    '''
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    
    page_texts = []
    
    for page in extract_pages(pdf_path):
        boxes = []
        for element in page:
            if isinstance(element, LTTextContainer):
                text = element.get_text().strip()
                if text:
                    boxes.append(TextBox(
                        text=text,
                        x0=element.x0,
                        y0=element.y0,
                        x1=element.x1,
                        y1=element.y1
                    ))
        
        if not boxes:
            page_texts.append("")
            continue
        
        # Simple column detection
        x_centers = [b.center_x for b in boxes]
        sorted_x = sorted(set(x_centers))
        column_midpoint = 0
        for i in range(1, len(sorted_x)):
            if sorted_x[i] - sorted_x[i-1] > 50:
                column_midpoint = (sorted_x[i-1] + sorted_x[i]) / 2
                break
        
        if not column_midpoint:
            column_midpoint = sorted(x_centers)[len(x_centers) // 2]
        
        left_boxes = [b for b in boxes if b.center_x < column_midpoint]
        right_boxes = [b for b in boxes if b.center_x >= column_midpoint]
        
        if left_boxes and right_boxes:
            # Check stream order
            top_left = min(left_boxes, key=lambda b: b.center_y)
            top_right = min(right_boxes, key=lambda b: b.center_y)
            left_idx = next((i for i, b in enumerate(boxes) 
                           if abs(b.center_x - top_left.center_x) < 10 
                           and abs(b.center_y - top_left.center_y) < 10), -1)
            right_idx = next((i for i, b in enumerate(boxes) 
                            if abs(b.center_x - top_right.center_x) < 10 
                            and abs(b.center_y - top_right.center_y) < 10), -1)
            
            if left_idx >= 0 and right_idx >= 0 and right_idx < left_idx:
                # Right column first - reorder
                left_sorted = sorted(left_boxes, key=lambda b: -b.center_y)
                right_sorted = sorted(right_boxes, key=lambda b: -b.center_y)
                all_boxes = []
                i, j = 0, 0
                while i < len(left_sorted) or j < len(right_sorted):
                    if i < len(left_sorted):
                        all_boxes.append(left_sorted[i])
                        i += 1
                    if j < len(right_sorted):
                        all_boxes.append(right_sorted[j])
                        j += 1
                page_text = " ".join(b.text for b in all_boxes)
            else:
                page_text = " ".join(b.text for b in boxes)
        else:
            page_text = " ".join(b.text for b in boxes)
        
        page_texts.append(page_text)
    
    return page_texts


def extract_text_fixed_with_pages(pdf_path: str) -> str:
    '''
    Extract text from PDF with column ordering fix applied per page.
    
    Args:
        pdf_path: Path to PDF file
        
    Returns:
        Text with column ordering fixed
    '''
    page_texts = extract_text_per_page(pdf_path)
    return "\n\n".join(page_texts)


# ============================================================================
# ANALYSIS SCRIPT (MAIN)
# ============================================================================

PDF_PATH = Path(__file__).parent / "test_data" / "paper.pdf"

# Target sentences to verify
TARGET_SENTENCES = [
    "Ideally, non-destructive observations of RSAs are used in experiments to allow repeated measurements, such as is possible using rhizotrons for statistical descriptions of roots [6].",
    "We computed the accuracy based on root matching to ensure that the correct identification of roots is rewarded and to measure extraction differences that contribute to differences in root length.",
]


def run_pdftotext(pdf_path: Path, use_layout: bool = False) -> str:
    """Extract text from PDF using pdftotext."""
    cmd = ["pdftotext"]
    if use_layout:
        cmd.append("-layout")
    cmd.extend([str(pdf_path), "-"])
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout


def extract_text_boxes_with_pdfminer(pdf_path: Path) -> List[TextBox]:
    """Extract text boxes with position information using pdfminer."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    
    boxes = []
    for page in extract_pages(str(pdf_path)):
        for element in page:
            if isinstance(element, LTTextContainer):
                text = element.get_text().strip()
                if not text:
                    continue
                boxes.append(TextBox(
                    text=text,
                    x0=element.x0,
                    y0=element.y0,
                    x1=element.x1,
                    y1=element.y1
                ))
    return boxes


def _normalize_ligatures(text: str) -> str:
    """
    Normalise Unicode ligatures used by the PDF font.

    The PDF stores 'ﬁ' (U+FB01) for 'fi' and 'ﬂ' (U+FB02) for 'fl'. Normalising
    these to plain 'fi'/'fl' lets us compare against plain-ASCII target
    sentences regardless of which form the PDF happens to use.
    """
    return text.replace("\ufb01", "fi").replace("\ufb02", "fl")


def verify_sentences(text: str, target_sentences: List[str]) -> dict:
    """Verify if target sentences appear correctly in the text."""
    results = {}
    normalized_text = _normalize_ligatures(" ".join(text.split()))

    for sentence in target_sentences:
        normalized_sentence = _normalize_ligatures(" ".join(sentence.split()))

        if normalized_sentence in normalized_text:
            results[sentence] = {
                "found": True,
                "position": normalized_text.find(normalized_sentence)
            }
        else:
            results[sentence] = {
                "found": False,
                "position": None
            }

    return results


def extract_sentence_context(text: str, sentence: str, context_chars: int = 100) -> str:
    """Extract context around a sentence in the text."""
    normalized_text = " ".join(text.split())
    normalized_sentence = " ".join(sentence.split())
    
    idx = normalized_text.find(normalized_sentence)
    if idx < 0:
        words = normalized_sentence.split()
        for i in range(len(words)):
            partial = " ".join(words[i:])
            idx = normalized_text.find(partial)
            if idx >= 0:
                break
    
    if idx >= 0:
        start = max(0, idx - context_chars)
        end = min(len(normalized_text), idx + len(normalized_sentence) + context_chars)
        return normalized_text[start:end]
    return None


def main():
    print("=" * 70)
    print("Paper.pdf Column Layout Analysis")
    print("=" * 70)
    print()
    
    # Step 1: Extract raw text
    print("Step 1: Extracting text from paper.pdf...")
    raw_text = run_pdftotext(PDF_PATH, use_layout=False)
    print(f"  Extracted {len(raw_text)} characters")
    print()
    
    # Step 2: Extract with layout preservation
    print("Step 2: Extracting text with -layout flag...")
    layout_text = run_pdftotext(PDF_PATH, use_layout=True)
    print(f"  Extracted {len(layout_text)} characters")
    print()
    
    # Step 3: Extract text boxes with positions
    print("Step 3: Extracting text boxes with pdfminer...")
    boxes = extract_text_boxes_with_pdfminer(PDF_PATH)
    print(f"  Found {len(boxes)} text boxes")
    print()
    
    # Step 4: Detect column structure
    print("Step 4: Detecting column structure...")
    has_two_columns, column_midpoint = detect_column_structure(str(PDF_PATH))
    
    if has_two_columns:
        left_count = sum(1 for b in boxes if b.center_x < column_midpoint)
        right_count = len(boxes) - left_count
        print(f"  Two-column layout detected!")
        print(f"  Column midpoint: x = {column_midpoint:.1f}")
        print(f"  Left column: {left_count} boxes, Right column: {right_count} boxes")
    else:
        print("  No clear two-column structure detected")
        print()
        return
    print()
    
    # Step 5: Analyze text stream order
    print("Step 5: Analyzing text stream order...")
    stream_order = analyze_stream_order(str(PDF_PATH))
    print(f"  Text stream order: {stream_order}")
    
    if stream_order == "right-left":
        print("  ⚠️  Right column appears first in PDF text stream!")
        print("  This causes sentences spanning columns to be fragmented.")
    print()
    
    # Step 6: Apply column break fix
    print("Step 6: Applying column break fix (method 1: join column breaks)...")
    print("  Method: Join lines separated by single blank lines")
    fixed_text = extract_pdf_text_fixed(str(PDF_PATH))
    print(f"  Fixed text: {len(fixed_text)} characters")
    print()
    
    # Step 6b: Apply page-based column reordering
    print("Step 6b: Applying page-based column reordering (method 2)...")
    print("  Method: Extract per page, reorder columns within each page")
    fixed_text_pages = extract_text_fixed_with_pages(str(PDF_PATH))
    print(f"  Fixed text (pages): {len(fixed_text_pages)} characters")
    print()

    # Step 6c: Apply robust column-sorted reconstruction (method 3)
    print("Step 6c: Applying robust column-sorted reconstruction (method 3)...")
    print("  Method: LTTextLine-level, reconstruct each column top->bottom, then left+right")
    fixed_text_colsorted = extract_text_column_sorted(str(PDF_PATH))
    print(f"  Fixed text (column-sorted): {len(fixed_text_colsorted)} characters")
    print()
    
    # Step 7: Verify target sentences
    print("Step 7: Verifying target sentences...")
    print()
    
    for i, sentence in enumerate(TARGET_SENTENCES, 1):
        print(f"--- Target Sentence {i} ---")
        print(f"Expected: {sentence[:80]}...")
        print()
        
        # Check raw text
        raw_results = verify_sentences(raw_text, [sentence])
        raw_result = raw_results[sentence]
        
        print("Raw text (pdftotext without -layout):")
        if raw_result["found"]:
            print(f"  ✓ Found consecutively")
            context = extract_sentence_context(raw_text, sentence)
            if context:
                print(f"  Context: ...{context}...")
        else:
            print(f"  ✗ Not found consecutively")
        print()
        
        # Check fixed text (column breaks joined)
        fixed_results = verify_sentences(fixed_text, [sentence])
        fixed_result = fixed_results[sentence]
        
        print("Fixed text (method 1: join column breaks):")
        if fixed_result["found"]:
            print(f"  ✓ Found consecutively")
            context = extract_sentence_context(fixed_text, sentence)
            if context:
                print(f"  Context: ...{context}...")
        else:
            print(f"  ✗ Not found consecutively")
        print()
        
        # Check fixed text (page-based reordering)
        page_results = verify_sentences(fixed_text_pages, [sentence])
        page_result = page_results[sentence]
        
        print("Fixed text (method 2: page-based column reordering):")
        if page_result["found"]:
            print(f"  ✓ Found consecutively")
            context = extract_sentence_context(fixed_text_pages, sentence)
            if context:
                print(f"  Context: ...{context}...")
        else:
            print(f"  ✗ Not found consecutively")
        print()

        # Check fixed text (robust column-sorted reconstruction)
        colsorted_results = verify_sentences(fixed_text_colsorted, [sentence])
        colsorted_result = colsorted_results[sentence]

        print("Fixed text (method 3: robust column-sorted reconstruction):")
        if colsorted_result["found"]:
            print(f"  ✓ Found consecutively")
            context = extract_sentence_context(fixed_text_colsorted, sentence)
            if context:
                print(f"  Context: ...{context}...")
        else:
            print(f"  ✗ Not found consecutively")
        print()
        print("-" * 70)
        print()
    
    # Summary
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    
    raw_fixed = verify_sentences(raw_text, TARGET_SENTENCES)
    fixed_fixed = verify_sentences(fixed_text, TARGET_SENTENCES)
    page_fixed = verify_sentences(fixed_text_pages, TARGET_SENTENCES)
    colsorted_fixed = verify_sentences(fixed_text_colsorted, TARGET_SENTENCES)
    
    raw_count = sum(1 for r in raw_fixed.values() if r["found"])
    fixed_count = sum(1 for r in fixed_fixed.values() if r["found"])
    page_count = sum(1 for r in page_fixed.values() if r["found"])
    colsorted_count = sum(1 for r in colsorted_fixed.values() if r["found"])
    
    print(f"Target sentences found consecutively:")
    print(f"  Raw text:            {raw_count}/{len(TARGET_SENTENCES)}")
    print(f"  Fixed (method 1):    {fixed_count}/{len(TARGET_SENTENCES)}")
    print(f"  Fixed (method 2):    {page_count}/{len(TARGET_SENTENCES)}")
    print(f"  Fixed (method 3):    {colsorted_count}/{len(TARGET_SENTENCES)}")
    print()
    
    if colsorted_count >= fixed_count and colsorted_count >= page_count and \
       colsorted_count >= raw_count and colsorted_count == len(TARGET_SENTENCES):
        print("✓ Robust column-sorted reconstruction found ALL target sentences!")
    elif colsorted_count > raw_count:
        print("✓ Robust column-sorted reconstruction improved text extraction!")
    elif page_count == raw_count and raw_count == len(TARGET_SENTENCES):
        print("✓ All target sentences found in all versions")
    else:
        print("Note: Some sentences may span page boundaries or have other issues")
    
    # Output the derived extraction method
    print()
    print("=" * 70)
    print("Reusable Functions (already available as module imports)")
    print("=" * 70)
    print()
    print("  from analyze_paperpdf import:")
    print("    - extract_text_column_sorted(pdf_path)   # BEST: robust column-sorted reconstruction")
    print("    - extract_pdf_text_fixed(pdf_path)       # Joins column breaks")
    print("    - detect_column_structure(pdf_path)      # Returns (bool, float)")
    print("    - analyze_stream_order(pdf_path)         # Returns 'left-right' or 'right-left'")
    print("    - extract_text_fixed_with_pages(pdf_path) # Advanced method")
    print()
    print("Example usage:")
    print("  text = extract_text_column_sorted('paper.pdf')")
    print("  text = extract_pdf_text_fixed('paper.pdf')")
    print("  has_cols, midpoint = detect_column_structure('paper.pdf')")
    print("  order = analyze_stream_order('paper.pdf')")
    print()


if __name__ == "__main__":
    main()
