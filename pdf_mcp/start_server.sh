#!/bin/bash
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

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
CACHE_DIR="${HOME}/.cache/pdf_mcp"

# Default server settings
HOST="127.0.0.1"
PORT="8000"

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
        --help)
            echo "Usage: $0 [--host HOST] [--port PORT]"
            echo ""
            echo "Options:"
            echo "  --host HOST    HTTP bind host (default: 127.0.0.1)"
            echo "  --port PORT    HTTP bind port (default: 8000)"
            echo "  --help         Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                           # Start on http://127.0.0.1:8000"
            echo "  $0 --port 9000               # Start on http://127.0.0.1:9000"
            echo "  $0 --host 0.0.0.0 --port 80  # Start on http://0.0.0.0:80"
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

# Step 1: Clear the PDF cache
echo "[1/3] Clearing PDF cache..."
if [ -d "${CACHE_DIR}" ]; then
    rm -rf "${CACHE_DIR}"/*
    echo "      Cache cleared: ${CACHE_DIR}"
else
    echo "      Cache directory does not exist (will be created on first use)"
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

# Start server with HTTP transport and cache clearing
python -m pdf_mcp.server --transport http --host "${HOST}" --port "${PORT}" --clear-cache
