"""PDF processor with MarkItDown and Tesseract OCR fallback."""

import base64
import io
import logging
import re
import tempfile
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
        Detect the layout structure of a PDF.
        
        Analyzes text box positions to determine if the PDF uses a single-column
        or multi-column layout. This helps identify potential text extraction
        issues where content may span columns.
        
        Args:
            pdf_path: Path to the PDF file
            verify_sentence: Optional sentence to check if it appears consecutively
                           in the extracted text (deterministic column-span check)
        
        Returns:
            Dict with layout information:
            - layout_type: "single_column", "two_column", or "multi_column"
            - columns: number of detected columns
            - sample_pages: layout analysis for sample pages
            - warning: message if complex layout detected
            - sentence_verification: if verify_sentence provided, contains:
              - "found": boolean - whether sentence appears in extracted text
              - "fragmented": boolean - whether sentence appears but is broken
              - "raw_text_sample": first 500 chars of raw extracted text
        """
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
        import subprocess
        import tempfile
        import os
        
        if not pdf_path.exists():
            raise PDFValidationError(f"File not found: {pdf_path}")
        
        result = {
            "layout_type": "single_column",
            "columns": 1,
            "sample_pages": [],
            "warning": None
        }
        
        # Get raw text extraction using pdftotext (preserves PDF text stream order)
        raw_text = ""
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                tmp_path = f.name
            
            # Extract page 1 raw text
            proc_result = subprocess.run(
                ['pdftotext', '-f', '1', '-l', '1', str(pdf_path), tmp_path],
                capture_output=True, text=True
            )
            if proc_result.returncode == 0:
                with open(tmp_path, 'r', encoding='utf-8', errors='replace') as f:
                    raw_text = f.read()
        except Exception as e:
            logger.warning(f"Raw text extraction failed: {e}")
        finally:
            try:
                os.remove(tmp_path)
            except:
                pass
        
        # Verify sentence if provided
        if verify_sentence:
            result["sentence_verification"] = {
                "sentence": verify_sentence,
                "found": verify_sentence in raw_text,
                "fragmented": False,
                "raw_text_sample": raw_text[:500]
            }
            
            # Check if sentence is fragmented (parts found but not consecutive)
            if not result["sentence_verification"]["found"]:
                # Check if key phrases from the sentence appear
                words = verify_sentence.split()
                if len(words) > 5:
                    phrase = ' '.join(words[:5])
                    if phrase in raw_text:
                        result["sentence_verification"]["fragmented"] = True
                        result["sentence_verification"]["reason"] = (
                            "Sentence fragments found but not in correct order"
                        )
        
        # Analyze first 5 pages using pdfminer for column detection
        pages_to_analyze = min(5, len(list(extract_pages(str(pdf_path)))))
        column_counts = []
        
        for i, page in enumerate(extract_pages(str(pdf_path))):
            if i >= pages_to_analyze:
                break
                
            # Collect text boxes with x positions and widths
            boxes = []
            for obj in page:
                if isinstance(obj, LTTextContainer) and obj.get_text().strip():
                    width = obj.x1 - obj.x0
                    boxes.append((obj.x0, width))
            
            if not boxes:
                continue
            
            # Cluster x positions to detect columns
            boxes.sort()
            columns = 0
            col_centers = []
            
            for x0, width in boxes:
                center = x0 + width / 2
                belongs_to_col = False
                for col_center in col_centers:
                    if abs(center - col_center) < 100:
                        belongs_to_col = True
                        break
                
                if not belongs_to_col:
                    columns += 1
                    col_centers.append(center)
            
            column_counts.append(columns)
            
            result["sample_pages"].append({
                "page": i + 1,
                "detected_columns": columns,
                "text_box_count": len(boxes)
            })
        
        # Determine overall layout
        if column_counts:
            avg_columns = sum(column_counts) / len(column_counts)
            result["columns"] = max(column_counts)
            
            if avg_columns >= 1.8 and max(column_counts) >= 2:
                result["layout_type"] = "two_column" if max(column_counts) == 2 else "multi_column"
                result["warning"] = (
                    f"Multi-column layout detected ({result['columns']} columns). "
                    "Text extraction may have ordering issues where content spanning "
                    "columns appears fragmented. Consider verifying with a sample sentence "
                    "or consecutive sentences that cross column boundaries."
                )
        
        return result

    def process_pdf(self, pdf_path: Path) -> Tuple[List[PageContent], Dict[str, Any]]:
        """
        Process a PDF file and extract content from all pages.

        Page boundaries are derived authoritatively with pdfplumber (each page
        becomes one PageContent). MarkItDown is used for the overall PDF-to-
        markdown conversion and document metadata (title/author).

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

        # Convert PDF to markdown using MarkItDown (primary conversion).
        try:
            result = self.markitdown.convert(str(pdf_path))
            full_markdown = result.text_content
        except Exception as e:
            logger.warning(f"MarkItDown conversion failed, using pdfplumber only: {e}")
            full_markdown = ""

        metadata = {}
        if hasattr(result, "metadata"):
            metadata = {
                "title": result.metadata.get("title"),
                "author": result.metadata.get("author"),
                "creation_date": result.metadata.get("created"),
                "modification_date": result.metadata.get("modified"),
            }

        # Extract per-page content authoritatively with pdfplumber.
        try:
            page_texts, pdf_meta = self._extract_pages_with_pdfplumber(pdf_path)
        except Exception as e:
            raise PDFProcessorError(f"pdfplumber page extraction failed: {e}")

        if not page_texts:
            raise PDFProcessorError("PDF has no extractable pages")

        if len(page_texts) > self.max_pages:
            raise PDFValidationError(
                f"PDF has {len(page_texts)} pages, exceeding maximum of {self.max_pages}"
            )

        # pdfplumber is the authoritative source for PDF metadata (MarkItDown's
        # PDF converter does not populate title/author). Prefer non-empty values.
        for key in ("title", "author", "creation_date", "modification_date"):
            if pdf_meta.get(key):
                metadata[key] = pdf_meta[key]

        # Per-page content. When MarkItDown emits reliable page markers we use
        # its paginated markdown; otherwise we use pdfplumber's authoritative
        # per-page text so page numbers are accurate for citations.
        page_markdowns = self._align_markdown_to_pages(full_markdown, len(page_texts))
        if not page_markdowns:
            page_markdowns = {i + 1: t for i, t in enumerate(page_texts)}

        metadata.update(
            {
                "pages_with_ocr": 0,
                "extraction_warnings": [],
            }
        )

        # Process each page
        page_contents = []
        for i in range(1, len(page_texts) + 1):
            per_page_markdown = page_markdowns.get(i, page_texts[i - 1])
            page_content = self._process_page(i, per_page_markdown)
            page_contents.append(page_content)

            if page_content.has_ocr:
                metadata["pages_with_ocr"] += 1

            if page_content.warnings:
                metadata["extraction_warnings"].extend(page_content.warnings)

        logger.info(
            f"Processed {len(page_contents)} pages, "
            f"{metadata['pages_with_ocr']} with OCR"
        )

        return page_contents, metadata

    def _extract_pages_with_pdfplumber(
        self, pdf_path: Path
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Extract text per page and document metadata.

        pdfminer (via ``extract_pages``) performs proper layout analysis, so it
        correctly separates multi-column page layouts (e.g. two-column academic
        papers), whereas pdfplumber's ``extract_text`` interleaves columns into a
        single mangled stream. pdfminer is also what MarkItDown's PDF converter
        uses under the hood for text PDFs. pdfplumber is kept only for reliable
        page counts and document metadata.

        Returns a tuple:
          - list where index i holds page (i+1)'s layout-aware text (empty string
            for pages with no extractable text, e.g. scanned pages), and
          - a metadata dict (title, author, creation/modification dates).
        """
        import pdfplumber
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer, LTPage, LTItem

        def _collect_text_boxes(obj: Any) -> List[Tuple[float, float, str]]:
            """Collect all text boxes with their positions from a layout object.
            
            Returns list of (y0, x0, text) tuples for sorting.
            """
            boxes = []
            if isinstance(obj, LTTextContainer) and obj.get_text().strip():
                boxes.append((obj.y0, obj.x0, obj.get_text()))
            else:
                for child in getattr(obj, "__iter__", lambda: [])():
                    boxes.extend(_collect_text_boxes(child))
            return boxes

        def _layout_text_column_aware(page: LTPage) -> str:
            """Extract text from a page, handling multi-column layouts.
            
            Groups text boxes by column (x position), then reads each column
            top-to-bottom, left-to-right. Uses line-height awareness to handle
            text that flows from bottom of left column to top of right column.
            """
            boxes = _collect_text_boxes(page)
            if not boxes:
                return ""
            
            # Group by column (x0 position)
            COL_THRESHOLD = 50.0  # pixels
            columns: Dict[float, List[Tuple[float, str]]] = {}
            
            for y0, x0, text in boxes:
                col_x = None
                for col_x0 in columns.keys():
                    if abs(x0 - col_x0) < COL_THRESHOLD:
                        col_x = col_x0
                        break
                if col_x is None:
                    col_x = x0
                    columns[col_x] = []
                columns[col_x].append((y0, text))
            
            # Sort columns left-to-right, then boxes in each column top-to-bottom
            result_parts = []
            sorted_col_x = sorted(columns.keys())
            for col_x0 in sorted_col_x:
                col_boxes = sorted(columns[col_x0], key=lambda b: -b[0])
                for y0, text in col_boxes:
                    result_parts.append(text)
            
            return "".join(result_parts)

        page_texts: List[str] = []
        pdf_meta: Dict[str, Any] = {}

        with pdfplumber.open(str(pdf_path)) as pdf:
            pdf_meta = {
                "title": pdf.metadata.get("Title"),
                "author": pdf.metadata.get("Author"),
                "creation_date": pdf.metadata.get("CreationDate"),
                "modification_date": pdf.metadata.get("ModDate"),
            }

        # Use pdftotext without layout for correct reading order
        # (handles multi-column PDFs where text stream order != visual order)
        import subprocess
        import os
        
        page_count = len(list(extract_pages(str(pdf_path))))
        
        for page_num in range(1, page_count + 1):
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                tmp_path = f.name
            
            try:
                # pdftotext -f -l extracts specific page range
                result = subprocess.run(
                    ['pdftotext', '-f', str(page_num), '-l', str(page_num), str(pdf_path), tmp_path],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    with open(tmp_path, 'r', encoding='utf-8', errors='replace') as f:
                        page_text = f.read()
                    page_texts.append(page_text.strip())
                else:
                    # Fallback to layout-aware extraction
                    for page in extract_pages(str(pdf_path)):
                        text = _layout_text_column_aware(page)
                        page_texts.append(text.strip() if text else "")
                    break
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        else:
            # Successfully extracted all pages with pdftotext
            return page_texts, pdf_meta
        
        # If we got here, fallback failed partway - return what we have
        return page_texts, pdf_meta
    def _align_markdown_to_pages(
        self, full_markdown: str, page_count: int
    ) -> Dict[int, str]:
        """
        Split MarkItDown's full-document markdown into per-page markdown when it
        contains explicit page markers ("<!-- page N -->").

        Returns an empty dict when no markers are present, signalling the caller
        to fall back to pdfplumber's authoritative per-page text.
        """
        marker_pattern = re.compile(r"<!--\s*page\s+(\d+)\s*-->", re.IGNORECASE)
        matches = list(marker_pattern.finditer(full_markdown))

        if not matches:
            return {}

        pages: Dict[int, str] = {}
        for i, match in enumerate(matches):
            page_num = int(match.group(1))
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(full_markdown)
            pages[page_num] = full_markdown[start:end].strip()
        return pages

    def _process_page(
        self, page_number: int, markdown: str
    ) -> PageContent:
        """
        Process a single page, using OCR if text is insufficient.

        Args:
            page_number: Page number (1-indexed)
            markdown: Markdown content from MarkItDown

        Returns:
            PageContent object
        """
        warnings = []

        # Extract text content
        text_content = self._extract_text(markdown)
        text_length = len(text_content)

        # Check if OCR is needed
        needs_ocr = text_length < self.min_text_threshold

        # Try to detect if page is likely scanned (no text but has structure markers)
        if not needs_ocr:
            # Check for indicators of scanned/low-quality extraction
            if self._is_likely_scanned_page(markdown, text_content):
                needs_ocr = True
                warnings.append("Page appears to be scanned or low-quality extraction")

        ocr_confidence: Optional[float] = None
        ocr_succeeded = False

        if needs_ocr and self.tesseract_available:
            # Attempt OCR
            ocr_result = self._ocr_page(page_number, markdown)
            if ocr_result:
                ocr_text, ocr_confidence = ocr_result
                if len(ocr_text) > self.min_text_threshold:
                    # Preserve the OCR'd text so search/query can index it.
                    markdown = ocr_text
                    text_content = ocr_text
                    text_length = len(text_content)
                    ocr_succeeded = True
                    warnings.append(f"OCR fallback used (confidence: {ocr_confidence:.1%})")
                else:
                    warnings.append(f"OCR produced insufficient text ({len(ocr_text)} chars)")
            else:
                warnings.append("OCR failed - using extracted text")
        elif needs_ocr:
            warnings.append("Insufficient text and OCR unavailable")

        return PageContent(
            page_number=page_number,
            markdown=markdown,
            text_length=text_length,
            has_ocr=needs_ocr and self.tesseract_available,
            ocr_confidence=ocr_confidence,
            warnings=warnings,
        )

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

    def _is_likely_scanned_page(self, markdown: str, text: str) -> bool:
        """
        Detect if a page is likely scanned based on extraction quality.

        Heuristics:
        - Very short text relative to expected page content
        - High ratio of special characters to alphanumeric
        - Missing common document structure
        """
        if not text:
            return True

        # Check text density
        text_ratio = len(text) / max(len(markdown), 1)
        if text_ratio < 0.1:
            return True

        # Check for gibberish patterns (high special char ratio)
        alpha_count = sum(1 for c in text if c.isalnum())
        if len(text) > 0 and alpha_count / len(text) < 0.5:
            return True

        return False

    def _ocr_page(
        self, page_number: int, markdown: str
    ) -> Optional[Tuple[str, float]]:
        """
        Perform OCR on a page.

        This requires rendering the PDF page to an image first.
        Since MarkItDown doesn't provide page images, we use pdf2image
        if available, otherwise return None.

        Args:
            page_number: Page number (1-indexed)
            markdown: Original markdown (for context)

        Returns:
            Tuple of (ocr_text, confidence) or None
        """
        # Check for pdf2image
        try:
            from pdf2image import convert_from_path
        except ImportError:
            logger.debug("pdf2image not available for OCR rendering")
            return None

        if not self._current_pdf_path:
            return None

        try:
            # Convert PDF page to image
            images = convert_from_path(
                self._current_pdf_path,
                first_page=page_number,
                last_page=page_number,
                dpi=300,
            )
            if not images:
                return None

            image = images[0]

            # Perform OCR
            ocr_text = pytesseract.image_to_string(image)
            ocr_data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)

            # Calculate confidence
            confidences = [
                int(c) for c in ocr_data["conf"] if c and c.strip() and c != "-1"
            ]
            confidence = sum(confidences) / len(confidences) if confidences else 0.0

            return ocr_text.strip(), confidence / 100.0

        except Exception as e:
            logger.warning(f"OCR failed for page {page_number}: {e}")
            return None

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
