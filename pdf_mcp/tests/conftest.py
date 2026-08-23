"""Shared pytest fixtures for PDF MCP server tests."""

from pathlib import Path

import pytest

import pdf_mcp.server as server

TEST_DATA = Path(__file__).parent.parent / "test_data"


@pytest.fixture(autouse=True)
def isolate_server_state(monkeypatch, tmp_path):
    """
    Isolate the server's global state between tests and give each test its own
    isolated cache so tests never touch the real cache.
    """
    monkeypatch.setattr(server, "_registered_docs", {})
    monkeypatch.setattr(server, "_processor", None)

    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(server, "_cache", server.PDFCache(cache_dir=str(cache_dir)))

    yield

    monkeypatch.undo()
    server._registered_docs = {}
    server._processor = None
    server._cache = None
