# PDF MCP Server Startup Script
# 
# This script:
# 1. Clears the PDF cache
# 2. Starts the MCP server with HTTP transport for Jan.AI access
#
# Usage:
#   ./start_server.sh                    # Default: http://127.0.0.1:8000
#   ./start_server.sh --port 9000        # Custom port
#   ./start_server.sh --host 0.0.0.0     # Bind to all interfaces
#   ./start_server.sh --keep-cache       # Retain existing cache (don't clear it)

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
CACHE_DIR="${HOME}/.cache/pdf_mcp"

# Default server settings
HOST="0.0.0.0"
PORT="8000"
KEEP_CACHE="false"

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
        --keep-cache)
            KEEP_CACHE="true"
            shift
            ;;
        --help)
            echo "Usage: $0 [--host HOST] [--port PORT] [--keep-cache]"
            echo ""
            echo "Options:"
            echo "  --host HOST    HTTP bind host (default: *)"
            echo "  --port PORT    HTTP bind port (default: 8000)"
            echo "  --keep-cache   Retain the existing PDF cache (default: clear it)"
            echo "  --help         Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                           # Start on http://*:8000 (clears cache)"
            echo "  $0 --port 9000               # Start on http://*:9000"
            echo "  $0 --host 127.0.0.1 --port 80  # Start on http://127.0.0.1:80"
            echo "  $0 --keep-cache              # Start keeping the existing cache"
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

# Step 1: Clear the PDF cache (unless --keep-cache)
if [ "${KEEP_CACHE}" = "true" ]; then
    echo "[1/3] Retaining existing PDF cache (--keep-cache)..."
    if [ -d "${CACHE_DIR}" ]; then
        echo "      Cache kept: ${CACHE_DIR}"
    else
        echo "      Cache directory does not exist (will be created on first use)"
    fi
else
    echo "[1/3] Clearing PDF cache..."
    if [ -d "${CACHE_DIR}" ]; then
        rm -rf "${CACHE_DIR}"/*
        echo "      Cache cleared: ${CACHE_DIR}"
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
echo "========================================"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

# Start server with HTTP transport; clear cache unless --keep-cache
if [ "${KEEP_CACHE}" = "true" ]; then
    python -m pdf_mcp.server --transport http --host "${HOST}" --port "${PORT}"
else
    python -m pdf_mcp.server --transport http --host "${HOST}" --port "${PORT}" --clear-cache
fi
