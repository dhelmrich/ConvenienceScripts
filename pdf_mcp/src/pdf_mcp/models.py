"""Pydantic models for PDF MCP Server."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PageContent(BaseModel):
    """Content of a single PDF page."""

    page_number: int = Field(..., ge=1, description="Page number (1-indexed)")
    markdown: str = Field(..., description="Markdown representation of page content")
    text_length: int = Field(..., ge=0, description="Character count of extracted text")
    has_ocr: bool = Field(..., description="Whether OCR was used for this page")
    ocr_confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="OCR confidence score if OCR was used"
    )
    warnings: List[str] = Field(
        default_factory=list, description="Warnings about extraction issues"
    )


class PDFMetadata(BaseModel):
    """Metadata for a registered PDF document."""

    doc_id: str = Field(..., description="Unique document identifier (content hash)")
    file_path: str = Field(..., description="Absolute path to the PDF file")
    file_size: int = Field(..., ge=0, description="File size in bytes")
    total_pages: int = Field(..., ge=1, description="Total number of pages")
    title: Optional[str] = Field(None, description="PDF title from metadata")
    author: Optional[str] = Field(None, description="PDF author from metadata")
    creation_date: Optional[str] = Field(None, description="PDF creation date")
    modification_date: Optional[str] = Field(None, description="PDF modification date")
    pages_with_ocr: int = Field(
        ..., ge=0, description="Number of pages that required OCR"
    )
    extraction_warnings: List[str] = Field(
        default_factory=list, description="Global extraction warnings"
    )


class SearchHit(BaseModel):
    """A search result hit."""

    doc_id: str = Field(..., description="Document identifier")
    page_number: int = Field(..., ge=1, description="Page number where match found")
    snippet: str = Field(..., description="Text snippet containing the match")
    context_before: str = Field(
        default_factory=str, description="Text before the match"
    )
    context_after: str = Field(
        default_factory=str, description="Text after the match"
    )
    match_positions: List[int] = Field(
        default_factory=list, description="Character positions of matches"
    )


class DocumentPassage(BaseModel):
    """A bounded passage from a document with citations."""

    doc_id: str = Field(..., description="Document identifier")
    content: str = Field(..., description="Passage content in markdown")
    page_numbers: List[int] = Field(
        ..., description="Page numbers this passage spans"
    )
    citation: str = Field(
        ..., description="Citation string (e.g., 'Document (pages 1-3)')"
    )
    source_path: Optional[str] = Field(None, description="Source file path")


@dataclass
class PageAnalysis:
    """Analysis results for a single page."""

    page_number: int
    text_content: str
    has_insufficient_text: bool
    needs_ocr: bool
    headings: List[str]
    tables: List[str]
    figures: List[str]  # Figure captions
    equations: List[str]  # Detected equations
    raw_markdown: str


@dataclass
class DocumentIndex:
    """Search index for a document."""

    doc_id: str
    page_texts: Dict[int, str]  # page_number -> full text
    page_headings: Dict[int, List[str]]  # page_number -> list of headings
    word_to_pages: Dict[str, set]  # word -> set of page numbers
    created_at: float
