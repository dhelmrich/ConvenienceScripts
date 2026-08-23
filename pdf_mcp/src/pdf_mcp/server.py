"""FastMCP server for PDF ingestion and analysis."""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastmcp import FastMCP

from .cache import PDFCache
from .models import DocumentPassage, PageContent, PDFMetadata, SearchHit
from .pdf_processor import PDFProcessor, PDFProcessorError
from .utils import (
    MAX_PDF_SIZE,
    PDFValidationError,
    compute_file_hash,
    parse_page_range,
    validate_file_path,
    validate_pdf_file,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# Create FastMCP server
mcp = FastMCP(
    "PDF MCP Server",
    instructions="""
    PDF MCP Server - Robust PDF ingestion and analysis.

    This server provides tools for:
    - Registering PDF documents for multi-turn analysis
    - Extracting metadata and content
    - Searching within documents
    - Querying specific pages or ranges

    The server only reads local PDFs; it has no write component.
    """,
)

# Global state
_processor: Optional[PDFProcessor] = None
_cache: Optional[PDFCache] = None
_registered_docs: Dict[str, Dict[str, Any]] = {}


def get_processor() -> PDFProcessor:
    """Get or create the PDF processor."""
    global _processor
    if _processor is None:
        tesseract_path = os.environ.get("TESSERACT_PATH")
        _processor = PDFProcessor(tesseract_path=tesseract_path)
    return _processor


def get_cache() -> PDFCache:
    """Get or create the cache."""
    global _cache
    if _cache is None:
        cache_dir = os.environ.get("PDF_MCP_CACHE_DIR")
        _cache = PDFCache(cache_dir=cache_dir)
    return _cache


def _build_citation(doc_id: str, pages: List[int]) -> str:
    """Build a citation string for a passage."""
    if len(pages) == 1:
        return f"{doc_id[:8]} (page {pages[0]})"
    elif len(pages) == 2:
        return f"{doc_id[:8]} (pages {pages[0]} and {pages[1]})"
    else:
        return f"{doc_id[:8]} (pages {pages[0]}-{pages[-1]})"


def _truncate_content(content: str, max_chars: int = 2000) -> str:
    """Truncate content while trying to preserve markdown structure."""
    if len(content) <= max_chars:
        return content

    # Find a good break point (paragraph or heading)
    truncated = content[:max_chars]
    last_break = max(
        truncated.rfind("\n\n"),
        truncated.rfind("\n#"),
        truncated.rfind("\n-"),
    )

    if last_break > max_chars // 2:
        truncated = truncated[: last_break + 1]

    return truncated.rstrip() + "\n\n...[content truncated]"


@mcp.tool()
async def pdf_register(
    file_path: str,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Register a PDF document for analysis.

    Validates the file path, computes a content hash, and processes the PDF
    using MarkItDown with Tesseract OCR fallback for scanned pages.

    Args:
        file_path: Path to the PDF file (absolute path or file:// URI)
        description: Optional description for the document

    Returns:
        Registration result with doc_id and metadata

    Raises:
        PDFValidationError: If file is invalid or inaccessible
        PDFProcessorError: If processing fails
    """
    try:
        # Validate path
        validated_path = validate_file_path(file_path)
        validate_pdf_file(validated_path)

        # Get file info
        file_stat = validated_path.stat()
        file_hash = compute_file_hash(validated_path)

        # Check cache
        cache = get_cache()
        cached = cache.get(file_hash, str(validated_path), file_stat.st_size)

        if cached:
            logger.info(f"Using cached results for {validated_path}")
            page_contents = [PageContent(**p) for p in cached["page_contents"]]
            metadata = cached["metadata"]
        else:
            # Process PDF
            processor = get_processor()
            page_contents, metadata = processor.process_pdf(validated_path)
            
            # Detect layout and add warning if multi-column
            try:
                layout_info = processor.detect_layout(validated_path)
                if layout_info.get("warning"):
                    metadata.setdefault("extraction_warnings", []).append(
                        layout_info["warning"]
                    )
            except Exception as layout_err:
                logger.warning(f"Layout detection failed: {layout_err}")

            # Cache results
            cache.put(
                file_hash=file_hash,
                file_path=str(validated_path),
                file_size=file_stat.st_size,
                total_pages=len(page_contents),
                page_contents=[pc.model_dump() for pc in page_contents],
                metadata=metadata,
            )

        # Create document ID
        doc_id = f"pdf_{file_hash[:16]}"

        # Register document
        _registered_docs[doc_id] = {
            "doc_id": doc_id,
            "file_path": str(validated_path),
            "file_hash": file_hash,
            "total_pages": len(page_contents),
            "page_contents": page_contents,
            "metadata": metadata,
            "description": description,
            "created_at": asyncio.get_event_loop().time(),
        }

        # Build response
        ocr_pages = [
            pc.page_number for pc in page_contents if pc.has_ocr
        ]
        
        # Get layout info for response
        layout_info = None
        sample_text = None
        try:
            layout_info = processor.detect_layout(validated_path)
            # Get sample text from first page for layout analysis
            if page_contents:
                sample_text = processor.get_page_text(page_contents[0])[:1000]
        except Exception:
            pass

        response = {
            "success": True,
            "doc_id": doc_id,
            "file_path": str(validated_path),
            "total_pages": len(page_contents),
            "file_size": file_stat.st_size,
            "pages_with_ocr": metadata.get("pages_with_ocr", 0),
            "ocr_page_numbers": ocr_pages,
            "layout": layout_info,
            "sample_text": sample_text,
            "warnings": metadata.get("extraction_warnings", []),
            "description": description,
        }
        
        # If multi-column detected, add guidance
        if layout_info and layout_info.get("warning"):
            response["layout_guidance"] = (
                "This PDF has a multi-column layout. Use pdf_detect_layout with "
                "sample_text to get LLM-based analysis and recommended extraction mode."
            )

        return response

    except PDFValidationError as e:
        logger.error(f"Validation error registering {file_path}: {e}")
        return {"success": False, "error": f"Invalid PDF: {e}"}
    except PDFProcessorError as e:
        logger.error(f"Processing error for {file_path}: {e}")
        return {"success": False, "error": f"Processing failed: {e}"}
    except Exception as e:
        logger.exception(f"Unexpected error registering {file_path}: {e}")
        return {"success": False, "error": f"Unexpected error: {e}"}


@mcp.tool()
async def pdf_metadata(doc_id: str) -> Dict[str, Any]:
    """
    Get metadata for a registered document.

    Args:
        doc_id: Document ID from pdf_register

    Returns:
        Document metadata including page count, file info, and extraction status
    """
    doc = _registered_docs.get(doc_id)

    if not doc:
        return {
            "success": False,
            "error": f"Document not found: {doc_id}",
            "registered_docs": list(_registered_docs.keys()),
        }

    metadata = doc["metadata"]
    pages_with_ocr = [
        i + 1
        for i, pc in enumerate(doc["page_contents"])
        if pc.has_ocr
    ]

    return {
        "success": True,
        "doc_id": doc_id,
        "file_path": doc["file_path"],
        "total_pages": doc["total_pages"],
        "title": metadata.get("title"),
        "author": metadata.get("author"),
        "pages_with_ocr": len(pages_with_ocr),
        "ocr_page_numbers": pages_with_ocr,
        "extraction_warnings": metadata.get("extraction_warnings", []),
        "description": doc.get("description"),
    }


@mcp.tool()
async def pdf_detect_layout(
    doc_id: Optional[str] = None,
    file_path: Optional[str] = None,
    verify_sentence: Optional[str] = None,
    use_alias_fast: bool = True,
) -> Dict[str, Any]:
    """
    Detect the layout structure of a PDF document.
    
    Analyzes text box positions to determine if the PDF uses single-column,
    two-column, or multi-column layout. This helps identify potential text
    extraction issues where content spanning columns may appear fragmented.
    
    DETERMINISTIC VERIFICATION: If verify_sentence is provided, the server checks
    if that exact sentence appears consecutively in the raw PDF text extraction.
    This provides deterministic confirmation of whether column-spanning text is
    preserved or fragmented.
    
    LLM ANALYSIS: If use_alias_fast=True and BLABLADOR_TOKEN is set, the server
    calls alias-fast with the RAW extracted text (not cleaned) to get LLM-based
    layout reasoning.
    
    Args:
        doc_id: Document ID from pdf_register (use for registered docs)
        file_path: Direct path to PDF file (alternative to doc_id)
        verify_sentence: Optional sentence to verify appears consecutively in text
        use_alias_fast: If True and BLABLADOR_TOKEN set, call alias-fast API
        
    Returns:
        Layout analysis result with:
        - layout_type: "single_column", "two_column", or "multi_column"
        - columns: number of detected columns
        - sentence_verification: if verify_sentence provided:
          - "found": boolean - sentence appears consecutively
          - "fragmented": boolean - sentence fragments found but broken
          - "raw_text_sample": first 500 chars of raw extraction
        - llm_estimated_columns: LLM-based column estimate (if alias-fast used)
        - llm_layout_mode: LLM-recommended layout mode
        - llm_reasoning: LLM's reasoning for layout analysis
    """
    processor = get_processor()
    
    # Determine PDF path
    pdf_path = None
    if doc_id:
        doc = _registered_docs.get(doc_id)
        if not doc:
            return {
                "success": False,
                "error": f"Document not found: {doc_id}",
                "registered_docs": list(_registered_docs.keys()),
            }
        pdf_path = Path(doc["file_path"])
    elif file_path:
        pdf_path = validate_file_path(file_path)
    else:
        return {
            "success": False,
            "error": "Either doc_id or file_path must be provided",
        }
    
    try:
        # Run programmatic layout detection with sentence verification
        layout_info = processor.detect_layout(pdf_path, verify_sentence)
        layout_info["success"] = True
        
        # Optionally call alias-fast for LLM-based analysis
        if use_alias_fast:
            alias_fast_result = _call_alias_fast_layout_analysis_raw(pdf_path)
            if alias_fast_result.get("success"):
                layout_info["alias_fast_analysis"] = alias_fast_result.get("analysis")
                if alias_fast_result.get("analysis"):
                    llm_layout = alias_fast_result["analysis"]
                    if llm_layout.get("estimated_columns"):
                        layout_info["llm_estimated_columns"] = llm_layout["estimated_columns"]
                    if llm_layout.get("layout_mode"):
                        layout_info["llm_layout_mode"] = llm_layout["layout_mode"]
                    if llm_layout.get("reasoning"):
                        layout_info["llm_reasoning"] = llm_layout["reasoning"]
                    if llm_layout.get("fragmentation_risk"):
                        layout_info["llm_fragmentation_risk"] = llm_layout["fragmentation_risk"]
        
        return layout_info
    except Exception as e:
        logger.error(f"Layout detection failed: {e}")
        return {
            "success": False,
            "error": str(e),
        }


def _call_alias_fast_layout_analysis_raw(pdf_path: Path) -> Dict[str, Any]:
    """
    Call alias-fast API for LLM-based layout analysis using RAW extracted text.
    
    Extracts page 1 text using pdftotext (preserving PDF text stream order)
    and passes it to alias-fast for analysis. This provides LLM reasoning
    about the raw, potentially mangled text extraction.
    
    Args:
        pdf_path: Path to the PDF file
        
    Returns:
        Dict with alias-fast response
    """
    import subprocess
    import tempfile
    import json
    
    try:
        # Extract raw text using pdftotext
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            tmp_path = f.name
        
        proc_result = subprocess.run(
            ['pdftotext', '-f', '1', '-l', '1', str(pdf_path), tmp_path],
            capture_output=True, text=True
        )
        
        if proc_result.returncode != 0:
            return {
                "success": False,
                "error": "Failed to extract raw text from PDF"
            }
        
        with open(tmp_path, 'r', encoding='utf-8', errors='replace') as f:
            raw_text = f.read()
        
        # Truncate if too long (alias-fast can handle substantial context)
        max_text_len = 8000
        if len(raw_text) > max_text_len:
            raw_text = raw_text[:max_text_len] + "... [truncated]"
        
        os.remove(tmp_path)
        
        # Call alias-fast API
        api_url = "https://api.blablador.fz-juelich.de/v1/chat/completions"
        api_token = os.environ.get("BLABLADOR_TOKEN")
        
        if not api_token:
            return {
                "success": False,
                "error": "BLABLADOR_TOKEN not set in environment"
            }
        
        prompt = f"""PDF Layout Analysis - RAW Text Extraction

Analyze this RAW text extracted from a PDF page 1. The text may contain column
ordering issues, fragmented sentences, or other extraction artifacts.

RAW EXTRACTED TEXT:
---
{raw_text}
---

Respond with JSON ONLY (no markdown, no explanation). Fields:
- "document_type": "academic_paper" | "report" | "book_chapter" | "other"
- "estimated_columns": integer (1, 2, or 3)
- "layout_mode": "single_column" | "two_column_parallel" | "two_column_flow" | "multi_column"
- "confidence": float 0.0-1.0
- "reasoning": "Explain layout analysis based on text patterns, fragmentation, ordering issues"
- "fragmentation_risk": "low" | "medium" | "high"
- "text_order_issue": boolean - whether text appears in wrong reading order
- "recommended_extraction": "position_order" | "logical_order" | "hybrid"

OBSERVATIONS:
- Look for mid-sentence breaks, abrupt line endings, or text that doesn't flow
- Academic papers often have 2 columns with text flowing left then right
- If sentences appear cut off or words are repeated, there may be column issues
- Headers/footers may appear at start or end of extraction"""

        payload = {
            "model": "alias-fast",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a PDF layout analysis expert. Respond with JSON only, no markdown formatting."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.1
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}"
        }
        
        response = requests.post(api_url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # Try to parse JSON from response
        try:
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r'```(?:json)?\n?', '', content)
                content = re.sub(r'```$', '', content)
            
            analysis = json.loads(content)
            return {
                "success": True,
                "analysis": analysis
            }
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse alias-fast response as JSON: {e}")
            return {
                "success": False,
                "error": f"Failed to parse LLM response: {e}",
                "raw_response": content
            }
            
    except subprocess.SubprocessError as e:
        logger.error(f"pdftotext failed: {e}")
        return {
            "success": False,
            "error": f"Text extraction failed: {e}"
        }
    except requests.RequestException as e:
        logger.error(f"alias-fast API request failed: {e}")
        return {
            "success": False,
            "error": f"API request failed: {e}"
        }
    except Exception as e:
        logger.error(f"alias-fast analysis failed: {e}")
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def pdf_get_page(
    doc_id: str,
    page_numbers: str = "all",
    include_markdown: bool = True,
    max_length: int = 5000,
) -> Dict[str, Any]:
    """
    Get content from specific pages of a registered document.

    Args:
        doc_id: Document ID from pdf_register
        page_numbers: Comma-separated page numbers or ranges (e.g., "1,3-5") or "all"
        include_markdown: Whether to include full markdown (default: True)
        max_length: Maximum total characters to return

    Returns:
        Page content with citations
    """
    doc = _registered_docs.get(doc_id)

    if not doc:
        return {
            "success": False,
            "error": f"Document not found: {doc_id}",
        }

    try:
        pages = parse_page_range(page_numbers, doc["total_pages"])
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if not pages:
        return {
            "success": False,
            "error": "No pages specified",
        }

    # Get page contents
    page_contents = []
    total_length = 0

    for page_num in pages:
        idx = page_num - 1
        if idx >= len(doc["page_contents"]):
            continue

        pc = doc["page_contents"][idx]

        # Build page text
        if include_markdown:
            content = pc.markdown
        else:
            # Extract plain text
            text = re.sub(r"```[\s\S]*?```", "", pc.markdown)
            text = re.sub(r"`[^`]+`", "", text)
            text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
            text = re.sub(r"\*([^*]+)\*", r"\1", text)
            text = re.sub(r"#+\s*", "", text)
            text = re.sub(r"^\s*[-*+]\s*", "", text, flags=re.MULTILINE)
            text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.MULTILINE)
            text = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", text)
            text = re.sub(r"\[([^\]]*)\]\([^\)]+\)", r"\1", text)
            text = re.sub(r"\|", " ", text)
            text = re.sub(r"-{2,}", "", text)
            text = re.sub(r"\s+", " ", text)
            content = text.strip()

        # Check max length
        if total_length + len(content) > max_length:
            remaining = max_length - total_length
            if remaining > 100:
                page_contents.append({
                    "page_number": page_num,
                    "content": content[:remaining] + "...[truncated]",
                    "has_ocr": pc.has_ocr,
                    "warnings": pc.warnings,
                    "truncated": True,
                })
            break

        page_contents.append({
            "page_number": page_num,
            "content": content,
            "has_ocr": pc.has_ocr,
            "warnings": pc.warnings,
            "text_length": pc.text_length,
            "truncated": False,
        })
        total_length += len(content)

    return {
        "success": True,
        "doc_id": doc_id,
        "pages": page_contents,
        "total_pages_returned": len(page_contents),
        "citation": _build_citation(doc_id, [p["page_number"] for p in page_contents]),
    }


@mcp.tool()
async def pdf_search(
    doc_id: str,
    query: str,
    page_numbers: Optional[str] = None,
    max_results: int = 20,
    context_chars: int = 100,
) -> Dict[str, Any]:
    """
    Search for text within a registered document.

    Args:
        doc_id: Document ID from pdf_register
        query: Search query (plain text)
        page_numbers: Optional page filter (e.g., "1,3-5" or "all")
        max_results: Maximum number of results to return
        context_chars: Characters of context around each match

    Returns:
        Search results with snippets and citations
    """
    doc = _registered_docs.get(doc_id)

    if not doc:
        return {
            "success": False,
            "error": f"Document not found: {doc_id}",
        }

    # Filter pages
    if page_numbers:
        try:
            filter_pages = parse_page_range(page_numbers, doc["total_pages"])
        except ValueError as e:
            return {"success": False, "error": str(e)}
    else:
        filter_pages = list(range(1, doc["total_pages"] + 1))

    # Search query
    query_lower = query.lower()
    results = []

    for page_num in filter_pages:
        idx = page_num - 1
        if idx >= len(doc["page_contents"]):
            continue

        pc = doc["page_contents"][idx]
        text = re.sub(r"```[\s\S]*?```", "", pc.markdown)
        text = re.sub(r"`[^`]+`", "", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"\*([^*]+)\*", r"\1", text)
        text = re.sub(r"#+\s*", "", text)
        text = re.sub(r"^\s*[-*+]\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", text)
        text = re.sub(r"\[([^\]]*)\]\([^\)]+\)", r"\1", text)
        text = re.sub(r"\|", " ", text)
        text = re.sub(r"-{2,}", "", text)
        text = re.sub(r"\s+", " ", text)
        text = text.strip()

        # Find matches
        text_lower = text.lower()
        start = 0
        while True:
            pos = text_lower.find(query_lower, start)
            if pos == -1:
                break

            # Get context
            context_start = max(0, pos - context_chars)
            context_end = min(len(text), pos + len(query) + context_chars)
            context_before = text[context_start:pos]
            snippet = text[pos:pos + len(query)]
            context_after = text[pos + len(query):context_end]

            results.append({
                "page_number": page_num,
                "context_before": context_before[-50:] if len(context_before) > 50 else context_before,
                "snippet": snippet,
                "context_after": context_after[:50] if len(context_after) > 50 else context_after,
                "position": pos,
            })

            start = pos + 1

            if len(results) >= max_results:
                break

        if len(results) >= max_results:
            break

    return {
        "success": True,
        "doc_id": doc_id,
        "query": query,
        "results": results[:max_results],
        "total_matches": len(results),
        "pages_searched": len(filter_pages),
    }


@mcp.tool()
async def pdf_query(
    doc_id: str,
    question: str,
    page_numbers: Optional[str] = None,
    max_pages: int = 10,
    max_chars: int = 3000,
) -> Dict[str, Any]:
    """
    Query a document for relevant passages related to a question.

    This performs keyword-based retrieval, returning bounded passages
    with page citations.

    Args:
        doc_id: Document ID from pdf_register
        question: Natural language question
        page_numbers: Optional page filter
        max_pages: Maximum pages to include in response
        max_chars: Maximum total characters

    Returns:
        Relevant passages with citations
    """
    doc = _registered_docs.get(doc_id)

    if not doc:
        return {
            "success": False,
            "error": f"Document not found: {doc_id}",
        }

    # Extract keywords from question
    keywords = re.findall(r"\b\w+\b", question.lower())
    keywords = [k for k in keywords if len(k) > 3]  # Filter short words

    if not keywords:
        keywords = [question.lower()]

    # Score pages by keyword match
    page_scores: Dict[int, int] = {}

    for page_num in range(1, doc["total_pages"] + 1):
        idx = page_num - 1
        if idx >= len(doc["page_contents"]):
            continue

        pc = doc["page_contents"][idx]
        text = re.sub(r"```[\s\S]*?```", "", pc.markdown).lower()
        text = re.sub(r"`[^`]+`", "", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"\*([^*]+)\*", r"\1", text)
        text = re.sub(r"#+\s*", "", text)
        text = re.sub(r"^\s*[-*+]\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", text)
        text = re.sub(r"\[([^\]]*)\]\([^\)]+\)", r"\1", text)

        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            page_scores[page_num] = score

    # Sort by score and filter
    if page_numbers:
        try:
            filter_pages = set(parse_page_range(page_numbers, doc["total_pages"]))
            page_scores = {k: v for k, v in page_scores.items() if k in filter_pages}
        except ValueError as e:
            return {"success": False, "error": str(e)}

    sorted_pages = sorted(page_scores.items(), key=lambda x: -x[1])
    top_pages = [p for p, _ in sorted_pages[:max_pages]]

    if not top_pages:
        return {
            "success": True,
            "doc_id": doc_id,
            "question": question,
            "passages": [],
            "message": "No relevant content found",
        }

    # Build passages
    passages = []
    total_chars = 0

    for page_num in top_pages:
        idx = page_num - 1
        pc = doc["page_contents"][idx]

        passage = DocumentPassage(
            doc_id=doc_id,
            content=pc.markdown,
            page_numbers=[page_num],
            citation=_build_citation(doc_id, [page_num]),
            source_path=doc["file_path"],
        )

        if total_chars + len(passage.content) > max_chars:
            # Truncate this passage
            remaining = max_chars - total_chars
            if remaining > 200:
                passage.content = passage.content[:remaining] + "\n...[truncated]"
                passages.append(passage)
            break

        passages.append(passage)
        total_chars += len(passage.content)

    # Format response
    formatted_passages = []
    for p in passages:
        formatted_passages.append({
            "doc_id": p.doc_id,
            "content": p.content,
            "page_numbers": p.page_numbers,
            "citation": p.citation,
            "source_path": p.source_path,
        })

    return {
        "success": True,
        "doc_id": doc_id,
        "question": question,
        "passages": formatted_passages,
        "total_passages": len(passages),
        "total_chars": total_chars,
    }


@mcp.resource("pdf://{doc_id}/content")
def get_document_content(doc_id: str) -> str:
    """Get the full content of a registered document."""
    doc = _registered_docs.get(doc_id)
    if not doc:
        raise ValueError(f"Document not found: {doc_id}")

    parts = []
    for i, pc in enumerate(doc["page_contents"], start=1):
        parts.append(f"<!-- page {i} -->")
        parts.append(pc.markdown)

    return "\n\n".join(parts)


@mcp.resource("pdf://{doc_id}/metadata")
def get_document_metadata_resource(doc_id: str) -> str:
    """Get metadata of a registered document."""
    doc = _registered_docs.get(doc_id)
    if not doc:
        raise ValueError(f"Document not found: {doc_id}")

    metadata = doc["metadata"]
    return f"""
Document: {doc['file_path']}
Doc ID: {doc_id}
Total Pages: {doc['total_pages']}
Title: {metadata.get('title', 'N/A')}
Author: {metadata.get('author', 'N/A')}
Pages with OCR: {metadata.get('pages_with_ocr', 0)}
"""


def run_server(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
):
    """Run the MCP server.

    Args:
        transport: ``"stdio"`` (default, for local clients like Jan/Claude) or
            ``"http"`` (Streamable HTTP, for a WSL server reached from the
            Windows host over the local network/IP).
        host: Bind address for http transport (use ``0.0.0.0`` to be reachable
            from other machines, e.g. the Windows host when running in WSL).
        port: Port for http transport.
    """
    logger.info("Starting PDF MCP Server...")

    processor = get_processor()
    logger.info(f"Tesseract available: {processor.tesseract_available}")

    cache = get_cache()
    logger.info(f"Cache directory: {cache.cache_dir}")

    if transport == "http":
        logger.info(f"HTTP transport on http://{host}:{port}/mcp")
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PDF MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio for local clients (default) or http for remote access",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port")
    args = parser.parse_args()

    run_server(args.transport, args.host, args.port)
