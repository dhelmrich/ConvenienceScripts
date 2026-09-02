# PDF MCP Server

A FastMCP server for robust PDF ingestion, analysis, and retrieval, with a
**region-aware two-column extractor** (PyMuPDF) plus **selective OCR** and an
optional **LLM (alias-fast/Blablador) word-separation fallback**.

The server only reads local PDFs; it has no write component.

## Features

- **PyMuPDF extraction** — primary text layer extraction with per-character
  origins (`rawdict`) and word geometry.
- **Region-aware multi-column handling** — an adaptive body-band + gutter
  detector keeps two-column academic papers correctly ordered instead of
  interleaving columns into a mangled string.
- **Boilerplate filtering** — running headers, vertical watermarks, and
  full-width contiguous footers are filtered per page.
- **Per-line spacing classifier** — each body line is classified as
  *trusted_native*, *geometry_candidate*, or *ocr_or_llm_required*.
- **Selective OCR** — only ambiguous lines (or low-density / scanned pages)
  are routed to Tesseract OCR.
- **LLM word-separation fallback** — concatenated words (missing spaces in the
  text layer, e.g. `associatingeachpixel...`) are repaired via alias-fast
  (Blablador) when `BLABLADOR_TOKEN` is set. LLM fixes take priority over OCR.
- **Content-hash caching** — fast repeated access to processed documents.
- **Page-cited passages** — bounded responses with clear citations.
- **Whitespace-insensitive search/query** — matches text regardless of
  intermediate whitespace (spaces, tabs, newlines).

## How extraction works

The primary extractor is **PyMuPDF** (not pdfminer/MarkItDown). Each page goes
through:

1. **Segmentation** (`page_segment_regions`): an adaptive body-band + gutter
   detector locates the two-column body and its gutter x. Full-width front
   matter (title/abstract), headers, watermarks, and footers are separated.
2. **Per-line classification** (`classify_line_spacing`): every rawdict body
   line is inspected for explicit spaces and inter-glyph gap patterns. Lines
   with spaces are *trusted native*; justified lines with a separable bimodal
   gap histogram are candidates for gap reconstruction; lines with no spaces
   are flagged *ocr_or_llm_required*.
3. **Concatenated-word repair**: lines with no spaces and length > 20 chars are
   sent to alias-fast (Blablador) to re-insert spaces. The LLM result is kept
   and OCR is **not** run on those lines (preventing OCR from overwriting the
   LLM fix).
4. **Selective OCR**: remaining ambiguous lines are OCR'd via Tesseract.
5. **Cross-column QA gate**: native order is compared against geometric
   (y, x) order; if they disagree on a cross-column sentence, left-then-right
   reconstruction is used.

### Font ligature handling
Characters are read in the order PyMuPDF reports them (not re-sorted purely by
x-position), so font ligatures that place glyphs at overlapping x-coordinates
(e.g. `ft` rendered such that the `f` and `t` share an origin) are preserved in
the correct reading order.

## Installation

```bash
# Install the package (editable)
pip install -e .

# Or install dependencies directly
pip install fastmcp pymupdf pytesseract pillow pydantic requests pytest pytest-asyncio
```

## System Dependencies

### Tesseract OCR Setup

#### Linux (Ubuntu/Debian)
```bash
sudo apt-get update
sudo apt-get install tesseract-ocr tesseract-ocr-eng
```

#### Linux (Fedora/RHEL)
```bash
sudo dnf install tesseract tesseract-langpack-eng
```

#### macOS
```bash
brew install tesseract
```

#### Windows
Download and install Tesseract from:
https://github.com/UB-Mannheim/tesseract/wiki (choose your language packs during
installation).

### Verify Installation
```bash
tesseract --version
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PDF_MCP_CACHE_DIR` | Cache directory for processed documents | `~/.cache/pdf_mcp` |
| `TESSERACT_PATH` | Path to tesseract executable | Auto-detected |
| `BLABLADOR_TOKEN` | Token for alias-fast (Blablador) LLM calls | unset (disabled) |
| `BLABLADOR_ADVISORY` | `1` enables the advisory layout cross-check | unset |
| `BLABLADOR_MODEL` | Model alias for Blablador calls | `alias-fast` |
| `BLABLADOR_API_URL` | Blablador API base URL | `https://api.helmholtz-blablador.fz-juelich.de/v1/chat/completions` |

> **Note:** the LLM word-separation fallback and layout analysis are only active
> when `BLABLADOR_TOKEN` is set. Without it, concatenated-word lines fall back
> to OCR instead.

## Usage

### Transport modes

| Transport | When to use | Start command |
|-----------|-------------|---------------|
| `stdio` | Default. Local client (Jan/Claude) runs the server as a child process | `python -m pdf_mcp.server` |
| `http` | WSL server reached from a Windows-host client over the network | `python -m pdf_mcp.server --transport http --host 0.0.0.0 --port 8560` |

### Start the Server (STDIO)

```bash
# Direct Python execution
python -m pdf_mcp.server

# Or using the package entry point
pdf-mcp-server

# Or using FastMCP CLI
fastmcp run src/pdf_mcp/server.py:mcp
```

### Start the Server (HTTP / Streamable HTTP)

```bash
python -m pdf_mcp.server --transport http --host 0.0.0.0 --port 8560
```

Endpoint: `http://<host>:<port>/mcp`.
`--host 0.0.0.0` makes the server reachable from other machines (e.g. the
Windows host when the server runs inside WSL).

### Running in WSL, using from Windows

Because this server needs Tesseract (available only inside WSL), run it in WSL
and let the Windows-host client reach it over HTTP. To find the WSL IP:

```bash
hostname -I     # e.g. 172.21.101.128
```

**Important — default WSL2 (NAT mode):** that IP is a NAT address and is
**not directly reachable from Windows**. Two working options:

**Option A — port proxy through Windows (recommended for NAT):**
Forward a Windows localhost port to the WSL server. From an elevated Windows
terminal:

```bat
netsh interface portproxy add v4tov4 listenport=8560 listenaddress=127.0.0.1 connectport=8560 connectaddress=172.21.101.128
```

Then connect to `http://127.0.0.1:8560/mcp` from Windows. This rule lasts only
until Windows reboots.

**Option B — mirrored networking (WSL ≥ 2.0):**
If `%USERPROFILE%\.wslconfig` contains `networkingMode=mirrored`, WSL shares the
Windows host IP, so Windows can reach the server directly at the host LAN IP with
no proxy.

## MCP Tools

### `pdf_register(file_path, description?)`
Register a PDF document for analysis. Validates the path, computes a content
hash, and processes the PDF (with layout detection). Returns a `doc_id` for
subsequent calls.

```json
{
  "name": "pdf_register",
  "arguments": {
    "file_path": "file:///home/user/documents/report.pdf",
    "description": "Q4 financial report"
  }
}
```

Returns: `success`, `doc_id`, `total_pages`, `pages_with_ocr`,
`ocr_page_numbers`, `layout`, `sample_text`, `warnings`.

### `pdf_metadata(doc_id)`
Get document metadata (page count, title, author, OCR pages, warnings).

```json
{ "name": "pdf_metadata", "arguments": { "doc_id": "pdf_a1b2c3d4e5f6g7h8" } }
```

### `pdf_extract_figures(pdf_path)`
Extract figure regions from a PDF using **structural cues only**. This tool
intentionally underdetects figures, relying on:
- Embedded image XObjects
- Marked-content sequences tagged as `/Figure`

It does NOT use captions, OCR, or layout models. Missing figures are expected
to be recovered via user-assisted refinement (e.g., `guided_seek_single_box`).

```json
{
  "name": "pdf_extract_figures",
  "arguments": { "pdf_path": "file:///home/user/documents/report.pdf" }
}
```

Returns: `success`, `figures` (list with page, bbox_pdf, origin, source_pdf),
`total`, `message`.

### `guided_seek_single_box(pdf_path, page_number, instruction?, dpi?)`
Open a PDF page in a GUI window and let the user draw a **single bounding box**.
This is a **vision-native** tool for user-assisted figure/region selection. The
user sees the rendered PDF page and draws a rectangle around the region they
want the model to inspect. The tool returns the cropped region as a PNG image,
which the vision-capable LLM can then analyze.

**Use this when:**
- `pdf_extract_figures` missed a figure and you want to recover it
- You need to inspect a specific region that automated detection didn't find
- You want to manually select a table, figure, or other visual element

**The tool:**
1. Renders the specified page at the given DPI (default: 150)
2. Opens a window showing the page
3. Lets the user draw one rectangle
4. Returns the cropped image along with PDF and image coordinates

```json
{
  "name": "guided_seek_single_box",
  "arguments": {
    "pdf_path": "file:///home/user/documents/report.pdf",
    "page_number": 3,
    "instruction": "Draw a rectangle around the figure you want the model to inspect.",
    "dpi": 150
  }
}
```

Returns: `success`, `page`, `bbox_pdf` (PDF points, bottom-left origin),
`bbox_image` (pixels, top-left origin), `crop_image_png` (base64),
`full_page_image_png` (base64, for context), `instruction`, `dpi`.
If cancelled by the user, returns `success: false` with `cancelled: true`.

**Requirements:** This tool requires a GUI environment (tkinter). It will fail
in headless environments.

### `pdf_get_page(doc_id, page_numbers, include_markdown?, max_length?)`
Get content from specific pages or ranges. Returns per-page content with a
citation string.

```json
{
  "name": "pdf_get_page",
  "arguments": { "doc_id": "pdf_a1b2c3d4e5f6g7h8", "page_numbers": "1-5" }
}
```

### `pdf_search(doc_id, query, page_numbers?, max_results?, context_chars?)`
Search for text within a document. **Whitespace-insensitive**: the query is
matched regardless of spaces/tabs/newlines between words. Returns matches with
page, position, snippet, and context.

```json
{
  "name": "pdf_search",
  "arguments": { "doc_id": "pdf_a1b2c3d4e5f6g7h8", "query": "machine learning" }
}
```

### `pdf_query(doc_id, question, page_numbers?, max_pages?, max_chars?)`
Keyword-based retrieval for natural-language questions. Extracts keywords from
the question and returns relevant passages plus a `keyword_occurrences` map
(page, position, matched text, context) for each keyword.

```json
{
  "name": "pdf_query",
  "arguments": { "doc_id": "pdf_a1b2c3d4e5f6g7h8", "question": "What are the main findings?" }
}
```

## MCP Resources

### `pdf://{doc_id}/content`
Full content of a registered document (all pages, page markers included).

### `pdf://{doc_id}/metadata`
Plain-text metadata summary for a registered document.

## Usage recommendations

1. **Register once, query many times.** Call `pdf_register` once and reuse the
   returned `doc_id` for all metadata/search/query/get_page calls. Results are
   cached by content hash, so re-registering an unchanged file is fast.

2. **Prefer `pdf_search` for exact phrases.** It is whitespace-insensitive, so
   a phrase split across a line break or column boundary still matches.

3. **Use `pdf_query` for topic discovery.** It extracts keywords and returns
   occurrence locations, which is useful for locating where a topic is discussed.

4. **Set `BLABLADOR_TOKEN` to fix concatenated words.** Two-column PDFs
   sometimes ship with a text layer missing spaces (e.g.
   `associatingeachpixelwithaclasslabelsuchasorgantypeand/or`). With the token
   set, the server re-inserts spaces via alias-fast; otherwise those lines fall
   back to OCR, which is less reliable.

5. **For scanned PDFs, ensure Tesseract is installed.** Pages with no text layer
   are OCR'd automatically. Verify with `tesseract --version`.

6. **Use `pdf_detect_layout` with a `verify_sentence`** to deterministically
   confirm whether a column-spanning sentence is preserved, before trusting the
   extracted text for that document.

7. **Clear the cache after upgrading.** `pdf_register --clear-cache` (or
   `rm -rf ~/.cache/pdf_mcp/*`) forces reprocessing so results reflect the
   current code.

## Jan MCP Configuration

Add this to your Jan configuration:

```json
{
  "mcpServers": {
    "pdf-server": {
      "command": "/mnt/c/work/ConvenienceScripts/pdf_mcp/.venv/bin/python",
      "args": ["-m", "pdf_mcp.server"],
      "env": { "PDF_MCP_CACHE_DIR": "/home/user/.cache/pdf_mcp" }
    }
  }
}
```

For Windows with WSL2 (adjust paths accordingly):

```json
{
  "mcpServers": {
    "pdf-server": {
      "command": "/usr/bin/python3",
      "args": ["-m", "pdf_mcp.server"],
      "cwd": "/mnt/c/work/ConvenienceScripts/pdf_mcp",
      "env": { "PDF_MCP_CACHE_DIR": "/home/user/.cache/pdf_mcp" }
    }
  }
}
```

> **Windows + WSL path handling:** the server runs in WSL and automatically
> translates Windows paths to their WSL equivalents. `C:\Users\me\doc.pdf`,
> `C:/Users/me/doc.pdf`, and `file:///C:/Users/me/doc.pdf` all resolve to
> `/mnt/c/Users/me/doc.pdf`. Point `pdf_register` at the Windows path and it
> will be found.

### Connecting a Windows client to a WSL HTTP server

If the client runs on Windows and the server runs inside WSL over HTTP,
configure the client with a `url` (connect to the already-running server):

```json
{
  "mcpServers": {
    "pdf-server": { "url": "http://127.0.0.1:8560/mcp" }
  }
}
```

- Use `http://127.0.0.1:8560/mcp` with the WSL NAT port proxy (Option A above).
- Use `http://<windows-lan-ip>:8560/mcp` with mirrored networking (Option B).

## Running Unit Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=pdf_mcp --cov-report=term-missing

# Run a specific test
pytest tests/test_server.py::TestServerTools -v
```

The tests use `tests/test_data.json` to drive expected values (page counts,
titles, full-sentence checks, and keyword occurrence counts) against real PDFs
in `test_data/`.

## Security Considerations

1. **Read-Only**: the server only reads PDFs; it has no write component.
2. **Local Use Only**: designed for local, trusted use only.
3. **No Authentication**: the server does not implement authentication.
4. **File Privileges**: the server runs with the privileges of the user
   executing it.

## Troubleshooting

### Tesseract Not Found
```
Error: Tesseract not available
```
- Verify installation: `tesseract --version`
- Set `TESSERACT_PATH` if not on PATH.

### Concatenated words not being repaired
- Confirm `BLABLADOR_TOKEN` is set. Without it, lines fall back to OCR.
- Check network access to the Blablador API.

### Permission Denied / File Not Found
```
Error: File does not exist: /mnt/c/...
```
- Confirm the Windows path maps correctly to WSL (`C:\Users\me\doc.pdf` becomes
  `/mnt/c/Users/me/doc.pdf`) and that the file exists inside WSL.
- Ensure the relevant drive is mounted in WSL.

### Cache Errors
```
Error: Failed to load cache data
```
- Clear cache: `rm -rf ~/.cache/pdf_mcp/*`
- Or set a different `PDF_MCP_CACHE_DIR`.

## Project Structure

```
pdf_mcp/
├── src/
│   └── pdf_mcp/
│       ├── __init__.py       # Package metadata
│       ├── server.py         # FastMCP server, MCP tools, and resources
│       ├── models.py         # Pydantic data models
│       ├── pdf_processor.py  # Region-aware extraction, OCR, and LLM repair
│       ├── cache.py          # Content-hash caching
│       └── utils.py          # Path validation and helpers
├── tests/
│   ├── test_server.py        # Unit tests
│   └── test_data.json        # Expected values driving the tests
├── test_data/
│   ├── paper_short.pdf           # Two-column PDF with a text layer
│   ├── paper_short_image.pdf     # Scanned PDF (no text)
│   ├── vroot_short.pdf           # Two-column PDF (VRoot)
│   └── vroot_image_short.pdf     # Scanned PDF (VRoot)
├── pyproject.toml
└── README.md
```

## License

MIT License
