"""Unit tests for PDF MCP Server.

Consolidated, focused suite covering: text PDFs, scanned PDFs, invalid paths,
caching, page citations, and search/query behavior.
"""

from pathlib import Path

import base64

import pytest

from pdf_mcp.cache import PDFCache
from pdf_mcp.pdf_processor import PDFProcessor
from pdf_mcp.server import (
    fetch_formula,
    pdf_formulas,
    pdf_get_page,
    pdf_has_formula,
    pdf_metadata,
    pdf_query,
    pdf_register,
    pdf_search,
)
from pdf_mcp.utils import (
    MAX_PDF_SIZE,
    PDFValidationError,
    _wsl_translate,
    compute_file_hash,
    parse_page_range,
    validate_file_path,
    validate_pdf_file,
)

from conftest import TEST_DATA


@pytest.fixture
def text_pdf():
    """Path to the two-column PDF with a text layer."""
    return TEST_DATA / "paper.pdf"


@pytest.fixture
def scanned_pdf():
    """Path to the scanned PDF (no text layer)."""
    return TEST_DATA / "paper_image.pdf"


@pytest.fixture
def radiative_pdf():
    """Path to the two-column scientific paper with a partial text layer.

    A 12-page paper on radiative transfer in plant leaves (Berdnik &
    Mukhamedyarov) whose equations and symbols require selective OCR.
    """
    return TEST_DATA / "radiative_transfer_leaves.pdf"


@pytest.fixture
def temp_cache_dir(tmp_path):
    """Temporary cache directory."""
    return str(tmp_path / "cache")


@pytest.fixture
async def registered_text_doc(text_pdf):
    """Register the text PDF once and yield (doc_id, result)."""
    result = await pdf_register(str(text_pdf))
    assert result["success"] is True, result
    return result["doc_id"], result


class TestPathValidation:
    """Path validation and helpers."""

    def test_validate_valid_path_and_uri(self, text_pdf):
        assert validate_file_path(str(text_pdf)) == text_pdf.resolve()
        assert validate_file_path(f"file://{text_pdf}") == text_pdf.resolve()

    @pytest.mark.parametrize(
        ("win", "expected"),
        [
            (r"C:\Users\me\doc.pdf", "/mnt/c/Users/me/doc.pdf"),
            ("C:/Users/me/doc.pdf", "/mnt/c/Users/me/doc.pdf"),
            (r"D:\data\paper.pdf", "/mnt/d/data/paper.pdf"),
            ("/home/user/doc.pdf", "/home/user/doc.pdf"),
            ("/mnt/c/Users/me/doc.pdf", "/mnt/c/Users/me/doc.pdf"),
        ],
    )
    def test_wsl_translate(self, win, expected):
        assert _wsl_translate(win) == expected

    def test_validate_missing_or_not_file(self, tmp_path):
        with pytest.raises(PDFValidationError, match="does not exist"):
            validate_file_path(str(TEST_DATA / "missing.pdf"))
        with pytest.raises(PDFValidationError, match="not a file"):
            validate_file_path(str(tmp_path))

    @pytest.mark.parametrize("content", [b"Not a PDF", b""])
    def test_validate_bad_pdf(self, tmp_path, content):
        f = tmp_path / "bad.pdf"
        f.write_bytes(content)
        with pytest.raises(PDFValidationError):
            validate_pdf_file(f)

    def test_parse_page_range_all_forms(self):
        assert parse_page_range("all", 5) == [1, 2, 3, 4, 5]
        assert parse_page_range("3", 5) == [3]
        assert parse_page_range("1-3", 5) == [1, 2, 3]
        assert parse_page_range("1,3,5", 5) == [1, 3, 5]
        with pytest.raises(ValueError):
            parse_page_range("abc", 5)
        with pytest.raises(ValueError, match="out of range"):
            parse_page_range("99", 5)

    def test_file_hash_stable(self, text_pdf):
        assert compute_file_hash(text_pdf) == compute_file_hash(text_pdf)
        assert len(compute_file_hash(text_pdf)) == 64


class TestCache:
    """Content-hash caching."""

    def test_cache_roundtrip_and_invalidation(self, temp_cache_dir):
        cache = PDFCache(cache_dir=temp_cache_dir)
        page = [{"page_number": 1, "markdown": "test", "text_length": 4,
                 "has_ocr": False, "warnings": []}]

        # miss
        assert cache.get("abc", "/f.pdf", 1000) is None

        # put + get
        cache.put("abc", "/f.pdf", 1000, 1, page, {})
        assert cache.get("abc", "/f.pdf", 1000)["total_pages"] == 1

        # stale when file size changes
        assert cache.get("abc", "/f.pdf", 2000) is None

    def test_cache_stats(self, temp_cache_dir):
        stats = PDFCache(cache_dir=temp_cache_dir).stats()
        assert {"entries", "size_mb", "cache_dir"} <= set(stats)


class TestPDFProcessor:
    """Processing of text and scanned PDFs."""

    def test_process_text_pdf(self, text_pdf):
        pages, meta = PDFProcessor().process_pdf(text_pdf)
        assert len(pages) == 16
        assert [p.page_number for p in pages] == list(range(1, 17))
        assert all(p.text_length > 0 for p in pages)
        assert meta["pages_with_ocr"] == 0
        assert meta["title"]
        assert meta["title"].startswith("VRoot")

    def test_two_column_layout_not_mangled(self, text_pdf):
        """Regression: two-column pages must not concatenate words/spaces.

        pdfplumber's naive extract interleaves columns into one string with no
        spaces (e.g. "Thisarticledescribes..."). Layout-aware extraction keeps
        the two columns separate and intact.
        """
        pages, _ = PDFProcessor().process_pdf(text_pdf)
        page1 = pages[0].markdown

        # Known sentences from the abstract arrive intact and spaced.
        assert "This article describes an immersive virtual reality" in page1
        assert (
            "Historically, it was not possible to\naccess root systems except "
            "by using difﬁcult excavation processes." in page1
        )

        # A tell-tale sign of the mangled concatenation is absent.
        assert "access rootsystems" not in page1
        assert "Thisarticledescribes" not in page1

    def test_process_scanned_pdf(self, scanned_pdf):
        """Scanned pages have no text layer, so OCR (or a warning) is used."""
        pages, meta = PDFProcessor().process_pdf(scanned_pdf)
        assert len(pages) == 16
        assert all(p.page_number >= 1 for p in pages)
        assert meta["pages_with_ocr"] > 0 or meta["extraction_warnings"]

    def test_extract_structural_elements(self):
        elements = PDFProcessor().extract_structural_elements(
            "# H\n\n- x\n\n| A | B |\n---\n| 1 | 2 |\n\nFigure 1: cap\n\n$x$"
        )
        assert "H" in elements["headings"]
        assert elements["tables"]
        assert any("Figure 1" in f for f in elements["figures"])
        assert elements["equations"]


class TestServerTools:
    """MCP tools end-to-end on the real PDF."""

    @pytest.mark.asyncio
    async def test_register_and_metadata(self, registered_text_doc):
        doc_id, result = registered_text_doc
        assert result["total_pages"] == 16

        meta = await pdf_metadata(doc_id)
        assert meta["success"]
        assert meta["total_pages"] == 16
        assert meta["title"].startswith("VRoot")

    @pytest.mark.asyncio
    async def test_register_invalid_inputs(self, tmp_path):
        missing = await pdf_register(str(TEST_DATA / "missing.pdf"))
        assert missing["success"] is False

        not_pdf = await pdf_register("/etc/hosts")
        assert not_pdf["success"] is False

        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"Not a PDF")
        assert (await pdf_register(str(bad)))["success"] is False

    @pytest.mark.asyncio
    async def test_get_page_with_citations(self, registered_text_doc):
        doc_id, _ = registered_text_doc

        single = await pdf_get_page(doc_id, "1")
        assert single["success"]
        assert single["pages"][0]["page_number"] == 1
        assert single["citation"]

        rng = await pdf_get_page(doc_id, "1-3")
        assert rng["success"]
        assert rng["total_pages_returned"] <= 3

        invalid = await pdf_get_page(doc_id, "999")
        assert invalid["success"] is False

    @pytest.mark.asyncio
    async def test_search_found_and_not_found(self, registered_text_doc):
        doc_id, _ = registered_text_doc

        hit = await pdf_search(doc_id, "Historically")
        assert hit["success"]
        assert hit["total_matches"] > 0

        miss = await pdf_search(doc_id, "xyznonexistent12345")
        assert miss["success"]
        assert miss["total_matches"] == 0

    @pytest.mark.asyncio
    async def test_query_citations_and_multiturn(self, registered_text_doc):
        doc_id, _ = registered_text_doc

        result = await pdf_query(doc_id, "What are the root system findings?")
        assert result["success"]
        # Multi-turn through the doc_id: all tools work against the same doc.
        assert (await pdf_metadata(doc_id))["success"]
        assert (await pdf_get_page(doc_id, "1"))["success"]
        assert (await pdf_search(doc_id, "Virtual reality"))["success"]

        if result["passages"]:
            passage = result["passages"][0]
            assert passage["doc_id"] == doc_id
            assert "page_numbers" in passage
            assert "citation" in passage


class TestEdgeCases:
    """Error handling for invalid/oversized inputs."""

    @pytest.mark.asyncio
    async def test_oversized_document(self, tmp_path):
        big = tmp_path / "big.pdf"
        big.write_bytes(b"%PDF-1.4\n" + b"x" * (MAX_PDF_SIZE + 1))
        result = await pdf_register(str(big))
        assert result["success"] is False
        assert "exceeds maximum" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_and_non_pdf(self, tmp_path):
        empty = tmp_path / "empty.pdf"
        empty.write_bytes(b"")
        assert (await pdf_register(str(empty)))["success"] is False

        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"Not a PDF file at all")
        assert (await pdf_register(str(bad)))["success"] is False

    @pytest.mark.asyncio
    async def test_metadata_not_found(self):
        result = await pdf_metadata("nonexistent_doc")
        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestFormulaExtraction:
    """Display-formula bbox detection, indexing and fetch (radiative paper).

    The radiative-transfer paper is a dense two-column maths document whose
    equations are a 2D arrangement of glyphs, so they are exposed as croppable
    formula regions with their own numbers rather than relying on linear text.
    """

    @pytest.mark.asyncio
    async def test_register_and_fetch_display_formulas(self, radiative_pdf):
        result = await pdf_register(str(radiative_pdf))
        assert result["success"] is True
        doc_id = result["doc_id"]

        # Document-level presence of display formulas.
        has = await pdf_has_formula(doc_id)
        assert has["success"] is True
        assert has["has_formula"] is True
        assert has["total"] > 0

        # Specific formula by number.
        by_number = await pdf_has_formula(doc_id, number=1)
        assert by_number["has_formula"] is True
        assert by_number["formula"]["page"] >= 1

        # Per-page listing with bbox geometry.
        page1 = await pdf_formulas(doc_id, page=1)
        assert page1["success"] is True
        assert page1["total"] > 0
        first = page1["formulas"][0]
        assert first["page"] == 1
        assert len(first["bbox"]) == 4

        # Fetching the first formula returns a decodable PNG crop.
        fetched = await fetch_formula(doc_id, first["number"], transcribe=False)
        assert fetched["success"] is True
        assert fetched["page"] == 1
        b64 = fetched["image_png_b64"]
        assert b64[:8] == "iVBORw0K"  # PNG magic bytes
        assert len(base64.b64decode(b64)) > 1000

        # Unknown formula number is reported gracefully.
        missing = await fetch_formula(doc_id, 99999)
        assert missing["success"] is False
        assert "not found" in missing["error"]

    @pytest.mark.asyncio
    async def test_formula_helpers_unknown_document(self):
        # Unregistered document id is handled gracefully without processing.
        assert (await pdf_has_formula("nonexistent_doc"))["success"] is False
        assert (await pdf_formulas("nonexistent_doc"))["success"] is False
        assert (await fetch_formula("nonexistent_doc", 1))["success"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
