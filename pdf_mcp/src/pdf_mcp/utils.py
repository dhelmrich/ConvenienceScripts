"""Utility functions for path validation."""

import logging
import os
import re
import sys
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# Maximum PDF size: 100 MB
MAX_PDF_SIZE = 100 * 1024 * 1024

# Minimum text threshold per page (characters)
MIN_TEXT_THRESHOLD = 50


class PDFValidationError(Exception):
    """Raised when a PDF fails validation."""

    pass


def _wsl_translate(path_str: str) -> str:
    """
    Translate a Windows-style path to the equivalent WSL path.

    This server relies on Tesseract/poppler binaries that are only available
    under WSL, while the AI agent may run on the Windows host and hand us paths
    like ``C:\\Users\\me\\doc.pdf``, ``C:/Users/me/doc.pdf`` or
    ``file:///C:/Users/me/doc.pdf``. Incorrectly mapping those to WSL means the
    file will not be found. Translating the drive letter to ``/mnt/<drive>/...``
    makes the file resolvable from inside WSL.

    - ``C:\\Users\\me\\doc.pdf`` -> ``/mnt/c/Users/me/doc.pdf``
    - ``C:/Users/me/doc.pdf``     -> ``/mnt/c/Users/me/doc.pdf``

    Non-Windows paths are returned unchanged; UNC paths are returned unchanged
    (best effort). On non-Linux hosts the path is returned unchanged.
    """
    if not sys.platform.startswith("linux"):
        return path_str

    # Normalize backslashes so both separators are handled uniformly.
    norm = path_str.replace("\\", "/")

    # Windows drive-letter form, e.g. "C:/Users/me/doc.pdf".
    drive_match = re.match(r"^([A-Za-z]):(/.*)?$", norm)
    if drive_match:
        drive = drive_match.group(1).lower()
        rest = drive_match.group(2) or ""
        return f"/mnt/{drive}{rest}"

    return path_str


def validate_file_path(path_str: str, label: str = "File") -> Path:
    """
    Validate that a local path (or file:// URI) points to a real, readable file.

    This server only reads PDFs; it has no write component, so no directory
    sandbox is required. The path is resolved and confirmed to exist. Windows
    paths (``C:\\...`` or ``file:///C:/...``) are transparently translated to
    their WSL equivalents (``/mnt/c/...``) so files reachable from the Windows
    host are found from inside WSL.

    Args:
        path_str: The path string to validate (can be file:// URI or plain path)
        label: Human-readable label for error messages (e.g. "PDF file")

    Returns:
        Resolved and validated Path object

    Raises:
        PDFValidationError: If the path is invalid, doesn't exist, or isn't a file
    """
    # Parse file:// URI if present
    if path_str.startswith("file://"):
        path_str = path_str[7:]
        from urllib.parse import unquote
        path_str = unquote(path_str)

    # Translate Windows paths to WSL paths (host agent -> WSL server).
    path_str = _wsl_translate(path_str)

    # Normalize path
    try:
        path = Path(path_str).expanduser().resolve()
    except Exception as e:
        raise PDFValidationError(f"Invalid {label} path: {e}")

    # Check file exists
    if not path.exists():
        raise PDFValidationError(f"{label} does not exist: {path}")

    # Check it's a regular file, and that we can read it
    if not path.is_file():
        raise PDFValidationError(f"Path is not a file: {path}")

    if not os.access(path, os.R_OK):
        raise PDFValidationError(f"{label} is not readable: {path}")

    return path


def validate_pdf_file(path: Path) -> None:
    """
    Validate that a file is a valid PDF.

    Args:
        path: Path to the file

    Raises:
        PDFValidationError: If file is not a valid PDF or exceeds size limit
    """
    # Check file size
    try:
        size = path.stat().st_size
    except OSError as e:
        raise PDFValidationError(f"Cannot access file: {e}")

    if size > MAX_PDF_SIZE:
        raise PDFValidationError(
            f"PDF exceeds maximum size of {MAX_PDF_SIZE / (1024*1024):.0f}MB "
            f"(actual: {size / (1024*1024):.1f}MB)"
        )

    if size == 0:
        raise PDFValidationError("PDF file is empty")

    # Check PDF magic number
    try:
        with open(path, "rb") as f:
            header = f.read(8)
            if not header.startswith(b"%PDF-"):
                raise PDFValidationError(
                    f"File is not a valid PDF (invalid header: {header[:20]})"
                )
    except IOError as e:
        raise PDFValidationError(f"Cannot read file: {e}")


def parse_page_range(page_spec: str, total_pages: int) -> List[int]:
    """
    Parse a page range specification into a list of page numbers.

    Args:
        page_spec: Page range string (e.g., "1-5", "1,3,5", "all")
        total_pages: Total number of pages in the document

    Returns:
        List of 1-indexed page numbers

    Raises:
        ValueError: If page range is invalid
    """
    if page_spec.lower() == "all":
        return list(range(1, total_pages + 1))

    pages = []
    for part in page_spec.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-")
                start = int(start.strip())
                end = int(end.strip())
                if start > end:
                    start, end = end, start
                pages.extend(range(start, end + 1))
            except ValueError:
                raise ValueError(f"Invalid page range: {part}")
        else:
            try:
                pages.append(int(part))
            except ValueError:
                raise ValueError(f"Invalid page number: {part}")

    # Validate page numbers
    for p in pages:
        if p < 1 or p > total_pages:
            raise ValueError(f"Page {p} out of range (1-{total_pages})")

    return sorted(set(pages))


def compute_file_hash(path: Path) -> str:
    """
    Compute a hash of file content for caching.

    Args:
        path: Path to file

    Returns:
        Hex digest of file hash
    """
    import hashlib

    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        # Read in chunks for large files
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
