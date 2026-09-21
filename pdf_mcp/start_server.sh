# PDF MCP Server Startup Script
# 
# This script:
# 1. Handles the PDF cache (retained by default, cleared with --clear-cache)
# 2. Optionally auto-registers PDFs from CSV list files
# 3. Starts the MCP server with HTTP transport for Jan.AI access
#
# Usage:
#   ./start_server.sh                                # Default: http://0.0.0.0:8000, keeps cache
#   ./start_server.sh --port 9000                    # Custom port
#   ./start_server.sh --host 0.0.0.0                 # Bind to all interfaces
#   ./start_server.sh --clear-cache                  # Wipe existing cache before start
#   ./start_server.sh --pdf-list my_pdfs.csv         # Auto-register PDFs listed in CSV

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
CACHE_DIR="${HOME}/.cache/pdf_mcp"

# Default server settings
HOST="0.0.0.0"
PORT="8000"
CLEAR_CACHE="false"
PDF_LISTS=()

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --clear-cache)
            CLEAR_CACHE="true"
            shift
            ;;
        --keep-cache)
            # Retaining the cache is now the default; accepted for compatibility
            shift
            ;;
        --pdf-list)
            PDF_LISTS+=("$2")
            shift 2
            ;;
        --help)
            echo "Usage: $0 [--host HOST] [--port PORT] [--clear-cache] [--pdf-list CSV]..."
            echo ""
            echo "Options:"
            echo "  --host HOST       HTTP bind host (default: *)"
            echo "  --port PORT       HTTP bind port (default: 8000)"
            echo "  --clear-cache     Wipe the existing PDF cache before starting (default: retain)"
            echo "  --pdf-list CSV    CSV file of PDFs to auto-register at startup (repeatable)."
            echo "                    Rows: 'path' or 'path,description'; '#' comments."
            echo "                    A directory row registers all *.pdf inside it recursively."
            echo "  --keep-cache      Deprecated no-op (cache retention is now the default)"
            echo "  --help            Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                              # Start on http://*:8000 (cache retained)"
            echo "  $0 --clear-cache                # Start with a fresh cache"
            echo "  $0 --host 127.0.0.1 --port 80   # Start on http://127.0.0.1:80"
            echo "  $0 --pdf-list reviews.csv       # Also register PDFs listed in reviews.csv"
            echo ""
            echo "PDF list example (reviews.csv):"
            echo "  # one entry per line; directories expand to all PDFs inside"
            echo "  /home/baker/vmware/shared/office/reviews/ieeevr2026"
            echo "  /some/other/paper.pdf,my review copy"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

echo "========================================"
echo "PDF MCP Server Startup"
echo "========================================"
echo ""

# Step 1: Handle the PDF cache (retain by default, clear with --clear-cache)
if [ "${CLEAR_CACHE}" = "true" ]; then
    echo "[1/3] Clearing PDF cache (--clear-cache)..."
    if [ -d "${CACHE_DIR}" ]; then
        rm -rf "${CACHE_DIR}"/*
        echo "      Cache cleared: ${CACHE_DIR}"
    else
        echo "      Cache directory does not exist (will be created on first use)"
    fi
else
    echo "[1/3] Retaining existing PDF cache..."
    if [ -d "${CACHE_DIR}" ]; then
        echo "      Cache kept: ${CACHE_DIR}"
    else
        echo "      Cache directory does not exist (will be created on first use)"
    fi
fi
echo ""

# Step 2: Activate virtual environment
echo "[2/3] Activating virtual environment..."
if [ -d "${VENV_DIR}" ]; then
    source "${VENV_DIR}/bin/activate"
    echo "      Virtualenv: ${VENV_DIR}"
else
    echo "ERROR: Virtual environment not found at ${VENV_DIR}"
    echo "Please run: python -m venv .venv && source .venv/bin/activate && pip install -e ."
    exit 1
fi
echo ""

# Step 3: Start the server
echo "[3/3] Starting PDF MCP Server..."
echo ""
echo "========================================"
echo "Server Information"
echo "========================================"
echo "  Transport:   HTTP"
echo "  Host:        ${HOST}"
echo "  Port:        ${PORT}"
echo "  Endpoint:    http://${HOST}:${PORT}/mcp"
echo "  Cache Dir:   ${CACHE_DIR}"
if [ ${#PDF_LISTS[@]} -gt 0 ]; then
    for LIST in "${PDF_LISTS[@]}"; do
        if [ ! -f "${LIST}" ]; then
            echo "ERROR: PDF list file not found: ${LIST}"
            exit 1
        fi
    done
    echo "  PDF Lists:   ${PDF_LISTS[*]}"
fi
echo "========================================"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

# Build server arguments
SERVER_ARGS=(--transport http --host "${HOST}" --port "${PORT}")
if [ "${CLEAR_CACHE}" = "true" ]; then
    SERVER_ARGS+=(--clear-cache)
fi
for LIST in "${PDF_LISTS[@]}"; do
    SERVER_ARGS+=(--pdf-list "${LIST}")
done

# Start server with HTTP transport
exec python -m pdf_mcp.server "${SERVER_ARGS[@]}"
