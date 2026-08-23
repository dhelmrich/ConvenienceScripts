# PDF MCP Server

A FastMCP server for robust PDF ingestion and analysis with OCR fallback.

## Features

- **MarkItDown PDF-to-Markdown conversion** - Preserves headings, tables, lists, and structure
- **Tesseract OCR fallback** - Automatically detects and processes scanned pages
- **Content-hash caching** - Fast repeated access to processed documents
- **Page-cited passages** - Bounded responses with clear citations
- **Multi-turn document analysis** - Register once, query many times

This server only reads local PDFs; it has no write component.

## Extraction Details

### Page segmentation and multi-column layouts
Page boundaries are determined authoritatively per page, and page number is
preserved so every returned passage carries an accurate citation. Text is
extracted with **pdfminer layout analysis** (`pdfminer.high_level.extract_pages`),
which performs layout segmentation and therefore keeps **two-column** and other
multi-column PDFs (typical of academic papers) correctly ordered — each column's
text stays intact with proper spacing, rather than being interleaved into one
mangled string. This is the same engine MarkItDown's PDF converter relies on for
text-based PDFs.

### OCR fallback for scanned pages
Pages whose extracted text is below a threshold (e.g. scanned pages with no text
layer) are rendered with `pdf2image` and passed to Tesseract OCR. The OCR text is
preserved in the page content so search/query work on scanned documents too. Each
OCRed page carries a confidence score and an extraction warning.

### Known limitation
Very uncommon glyphs (e.g. characters absent from the embedded font mapping) may
appear as `(cid:N)` sequences from pdfminer. This affects only a handful of
special characters in author names/titles, not the body text.

## Installation

```bash
# Install the package
pip install -e .

# Or install dependencies directly
pip install fastmcp markitdown[all] pytesseract pillow pydantic pdf2image pytest pytest-asyncio
```

## System Dependencies

### Tesseract OCR Setup

#### Linux (Ubuntu/Debian)
```bash
sudo apt-get update
sudo apt-get install tesseract-ocr tesseract-ocr-eng poppler-utils
```

#### Linux (Fedora/RHEL)
```bash
sudo dnf install tesseract tesseract-langpack-eng poppler-utils
```

#### macOS
```bash
brew install tesseract poppler
```

#### Windows
1. Download and install Tesseract from: https://github.com/UB-Mannheim/tesseract/wiki
   - Choose your language packs during installation
2. Download Poppler from: https://github.com/oschwartz10612/poppler-windows/releases
3. Add both to your PATH:
   - Tesseract: `C:\Program Files\Tesseract-OCR`
   - Poppler: `C:\path\to\poppler\bin`

### Verify Installation
```bash
tesseract --version
pdfinfo --version  # From poppler-utils
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PDF_MCP_CACHE_DIR` | Cache directory for processed documents | `~/.cache/pdf_mcp` |
| `TESSERACT_PATH` | Path to tesseract executable | Auto-detected |

### Example Configuration
```bash
export PDF_MCP_CACHE_DIR="$HOME/.cache/pdf_mcp"
export TESSERACT_PATH="/usr/bin/tesseract"
```

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

Endpoint: `http://<host>:<port>/mcp` at the given host and port.
`--host 0.0.0.0` makes the server reachable from other machines, e.g. the
Windows host when the server runs inside WSL.

### Running in WSL, using from Windows

Because this server needs Tesseract/poppler (available only inside WSL), run it
in WSL and let the Windows-host Jan client reach it over HTTP. To find the WSL
IP, run inside WSL:

```bash
hostname -I     # e.g. 172.21.101.128
```

**Important — default WSL2 (NAT mode):** that IP (e.g. `172.21.101.128`) is a
WSL2 NAT address and is **not directly reachable from Windows**. Two working
options:

**Option A — port proxy through Windows (recommended for NAT):**
Forward a Windows localhost port to the WSL server so Windows connects to its
own localhost. From an elevated Windows terminal:

```bat
netsh interface portproxy add v4tov4 listenport=8560 listenaddress=127.0.0.1 connectport=8560 connectaddress=172.21.101.128
```

Then in Windows Jan, connect to `http://127.0.0.1:8560/mcp`.
This proxy rule lasts only until Windows reboots (re-run after restart).

**Option B — mirrored networking (WSL ≥ 2.0):**
If `%USERPROFILE%\.wslconfig` contains `networkingMode=mirrored`, WSL shares the
Windows host's own IP, so Windows can reach the server directly at the host's LAN
IP via `http://<windows-lan-ip>:8560/mcp` with no proxy.

> The `ifconfig` value `172.21.101.128` with a `netmask 255.255.240.0` is a NAT
> address — use **Option A** unless you have switched to mirrored networking.

### MCP Tools

#### `pdf_register(file_path, description?)`
Register a PDF document for analysis.

```json
{
  "name": "pdf_register",
  "arguments": {
    "file_path": "file:///home/user/documents/report.pdf",
    "description": "Q4 financial report"
  }
}
```

Returns: `doc_id`, `total_pages`, `pages_with_ocr`, `warnings`

**Multi-column layout detection:**
If the PDF has a multi-column layout, the response includes:
- `layout`: Layout analysis (column count, layout type)
- `sample_text`: First 1000 chars from page 1
- `layout_guidance`: Suggests using `pdf_detect_layout` with sample text

#### `pdf_detect_layout(doc_id?, file_path?, verify_sentence?, use_alias_fast?)`
Detect the layout structure of a PDF document with **deterministic sentence verification** and optional LLM analysis.

**Deterministic Verification:**
Provide a `verify_sentence` (a sentence you expect to appear consecutively in the text). The server checks the RAW PDF text extraction to determine:
- `found`: Does the exact sentence appear?
- `fragmented`: Are sentence fragments found but out of order?
- `raw_text_sample`: First 500 chars of raw extraction for inspection

**LLM Analysis:**
When `use_alias_fast=true` and `BLABLADOR_TOKEN` is set, the server calls alias-fast with the FULL raw page 1 text (up to 8000 chars) to get LLM-based layout reasoning.

```json
{
  "name": "pdf_detect_layout",
  "arguments": {
    "doc_id": "pdf_a1b2c3d4e5f6g7h8",
    "verify_sentence": "Ideally, non-destructive observations of RSAs...",
    "use_alias_fast": true
  }
}
```

**Response includes:**
- `layout_type`: "single_column", "two_column", or "multi_column"
- `columns`: Number of detected columns (programmatic)
- `sentence_verification`: If verify_sentence provided:
  - `found`: boolean - exact sentence in text
  - `fragmented`: boolean - fragments found but broken
  - `raw_text_sample`: Raw extraction for inspection
- `llm_estimated_columns`: LLM column estimate
- `llm_layout_mode`: LLM layout mode ("two_column_flow", etc.)
- `llm_fragmentation_risk`: "low", "medium", or "high"
- `llm_reasoning`: Detailed LLM analysis of text patterns

**Example Workflow:**
1. Register PDF → get `sample_text` and layout warning
2. Call `pdf_detect_layout` with `verify_sentence` (a sentence spanning columns)
3. If `found=false` and `fragmented=true`, text extraction has column ordering issues
4. Review `llm_reasoning` for explanation and `recommended_extraction` mode

#### `pdf_metadata(doc_id)`
Get document metadata.

```json
{
  "name": "pdf_metadata",
  "arguments": {
    "doc_id": "pdf_a1b2c3d4e5f6g7h8"
  }
}
```

#### `pdf_get_page(doc_id, page_numbers, include_markdown?, max_length?)`
Get content from specific pages.

```json
{
  "name": "pdf_get_page",
  "arguments": {
    "doc_id": "pdf_a1b2c3d4e5f6g7h8",
    "page_numbers": "1-5"
  }
}
```

#### `pdf_search(doc_id, query, page_numbers?, max_results?, context_chars?)`
Search for text within a document.

```json
{
  "name": "pdf_search",
  "arguments": {
    "doc_id": "pdf_a1b2c3d4e5f6g7h8",
    "query": "machine learning"
  }
}
```

#### `pdf_query(doc_id, question, page_numbers?, max_pages?, max_chars?)`
Query a document for relevant passages.

```json
{
  "name": "pdf_query",
  "arguments": {
    "doc_id": "pdf_a1b2c3d4e5f6g7h8",
    "question": "What are the main findings?"
  }
}
```

## Multi-Column Layout Handling

Academic papers often use two-column layouts that can cause text extraction issues. The server provides tools to detect and handle these layouts.

### Detection Workflow

1. **Register the PDF** - `pdf_register` automatically detects layout and returns warnings:
   ```json
   {
     "layout": {
       "layout_type": "multi_column",
       "columns": 2,
       "warning": "Multi-column layout detected..."
     },
     "sample_text": "<first 1000 chars>",
     "layout_guidance": "Use pdf_detect_layout with sample_text..."
   }
   ```

2. **Get LLM Analysis** - Call `pdf_detect_layout` with sample text:
   ```json
   {
     "name": "pdf_detect_layout",
     "arguments": {
       "doc_id": "pdf_<hash>",
       "sample_text": "<from registration response>",
       "use_alias_fast": true
     }
   }
   ```

3. **Review LLM Reasoning** - The response includes:
   - `llm_reasoning`: Why the LLM chose this layout
   - `llm_layout_mode`: Recommended mode ("single_column", "two_column_flow", etc.)
   - `fragmentation_risk`: "low", "medium", or "high"
   - `recommended_extraction`: "position_order" or "logical_order"

### Layout Modes

- **`single_column`**: Standard left-to-right, top-to-bottom flow
- **`two_column_parallel`**: Two columns read top-to-bottom, left-to-right
- **`two_column_flow`**: Left column top-to-bottom, then right column top-to-bottom
- **`multi_column`**: Complex layouts (3+ columns)

### alias-fast Integration

When `BLABLADOR_TOKEN` is set, the server calls alias-fast to analyze sample text and provide LLM-based layout reasoning. This helps determine the correct extraction mode for complex PDFs.

## Jan MCP Configuration

Add this to your Jan configuration file (`~/.jan/config.json` or the Jan app settings):

```json
{
  "mcpServers": {
    "pdf-server": {
      "command": "/mnt/c/work/ConvenienceScripts/pdf_mcp/.venv/bin/python",
      "args": [
        "-m",
        "pdf_mcp.server"
      ],
      "env": {
        "PDF_MCP_CACHE_DIR": "/home/user/.cache/pdf_mcp"
      }
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
      "args": [
        "-m",
        "pdf_mcp.server"
      ],
      "cwd": "/mnt/c/work/ConvenienceScripts/pdf_mcp",
      "env": {
        "PDF_MCP_CACHE_DIR": "/home/user/.cache/pdf_mcp"
      }
    }
  }
}
```

> **Windows + WSL path handling**: Because this server needs Tesseract/poppler
> (available only inside WSL), it runs in WSL while the AI agent may run on the
> Windows host. The server automatically translates Windows paths to their WSL
> equivalents. `C:\Users\me\doc.pdf`, `C:/Users/me/doc.pdf`, and
> `file:///C:/Users/me/doc.pdf` all resolve to `/mnt/c/Users/me/doc.pdf` inside
> WSL. Point `pdf_register` at the Windows path and it will be found.

### Connecting Windows Jan to a WSL HTTP server

If Jan runs on Windows and the PDF server runs inside WSL over HTTP, configure
Jan with a `url` (Jan connects to the already-running server instead of spawning
a command):

```json
{
  "mcpServers": {
    "pdf-server": {
      "url": "http://127.0.0.1:8560/mcp"
    }
  }
}
```

- Use `http://127.0.0.1:8560/mcp` with the WSL NAT port proxy (Option A above).
- Use `http://<windows-lan-ip>:8560/mcp` with mirrored networking (Option B above).

## Testing with MCP Inspector

The MCP Inspector allows you to test your server interactively.

### Start the Inspector
```bash
npx @modelcontextprotocol/inspector
```

### Configure for PDF Server

In the Inspector UI:

1. **Transport Type**: Select `STDIO`
2. **Command**: `/mnt/c/work/ConvenienceScripts/pdf_mcp/.venv/bin/python`
3. **Arguments**: `-m pdf_mcp.server`
4. **Working Directory**: `/mnt/c/work/ConvenienceScripts/pdf_mcp`
5. Click **Connect**

### Test Workflow

1. **List Tools**: Click the "Tools" tab → "List Tools"
   - Verify all 5 tools are available: `pdf_register`, `pdf_metadata`, `pdf_get_page`, `pdf_search`, `pdf_query`

2. **Register a PDF**:
   ```json
   {
     "file_path": "file:///mnt/c/work/ConvenienceScripts/pdf_mcp/test_data/paper.pdf"
   }
   ```

3. **Get Metadata**: Use the returned `doc_id`
   ```json
   {
     "doc_id": "pdf_a1b2c3d4..."
   }
   ```

4. **Get Pages**:
   ```json
   {
     "doc_id": "pdf_a1b2c3d4...",
     "page_numbers": "1-3"
   }
   ```

5. **Search**:
   ```json
   {
     "doc_id": "pdf_a1b2c3d4...",
     "query": "machine learning"
   }
   ```

6. **Query**:
   ```json
   {
     "doc_id": "pdf_a1b2c3d4...",
     "question": "What is the main topic?"
   }
   ```

## Running Unit Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=pdf_mcp --cov-report=term-missing

# Run specific test
pytest tests/test_server.py::TestServerTools::test_pdf_register_text_pdf -v
```

## Security Considerations

1. **Read-Only**: The server only reads PDFs; it has no write component.
2. **Local Use Only**: This server is designed for local, trusted use only.
3. **No Authentication**: The server does not implement authentication.
4. **File Privileges**: The server runs with the privileges of the user executing it.

## Troubleshooting

### Tesseract Not Found
```
Error: Tesseract not available
```
- Verify installation: `tesseract --version`
- Set `TESSERACT_PATH` environment variable if not in PATH

### Poppler Not Found
```
Error: pdf2image requires poppler
```
- Install poppler-utils (Linux) or Poppler (Windows/macOS)

### Permission Denied / File Not Found
```
Error: File does not exist: /mnt/c/...
```
- Confirm the Windows path maps correctly to WSL. `C:\Users\me\doc.pdf` becomes
  `/mnt/c/Users/me/doc.pdf`. Verify the file exists inside WSL:
  `ls /mnt/c/Users/me/doc.pdf`
- Ensure the relevant drive is mounted in WSL (typically automatic for `/mnt/c`).

### Cache Errors
```
Error: Failed to load cache data
```
- Clear cache: `rm -rf ~/.cache/pdf_mcp/*`
- Or set a different `PDF_MCP_CACHE_DIR`

## Project Structure

```
pdf_mcp/
├── src/
│   └── pdf_mcp/
│       ├── __init__.py
│       ├── server.py       # FastMCP server and tools
│       ├── models.py       # Pydantic data models
│       ├── pdf_processor.py # MarkItDown + OCR processing
│       ├── cache.py        # Content-hash caching
│       └── utils.py        # Path validation utilities
├── tests/
│   └── test_server.py      # Unit tests
├── test_data/
│   ├── paper.pdf          # PDF with text layer
│   └── paper_image.pdf    # Scanned PDF (no text)
├── pyproject.toml
└── README.md
```

## License

MIT License
