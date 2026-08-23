"""Content-hash based caching system for PDF processing."""

import json
import logging
import os
import pickle
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from .utils import compute_file_hash

logger = logging.getLogger(__name__)


class CacheEntry(BaseModel):
    """A single cache entry."""

    file_hash: str
    file_path: str
    file_size: int
    total_pages: int
    processed_at: float
    page_contents: List[Dict[str, Any]]
    metadata: Dict[str, Any]


class PDFCache:
    """
    Cache for processed PDF documents using content hashing.

    The cache stores processed results keyed by content hash of the PDF file.
    If the file content hasn't changed (same hash), cached results are returned.
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        max_size_mb: int = 500,
        max_age_days: int = 30,
    ):
        """
        Initialize the cache.

        Args:
            cache_dir: Directory to store cache files. Defaults to ~/.cache/pdf_mcp
            max_size_mb: Maximum cache size in megabytes
            max_age_days: Maximum age of cache entries in days
        """
        if cache_dir is None:
            cache_dir = os.path.expanduser("~/.cache/pdf_mcp")

        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.max_age_seconds = max_age_days * 24 * 60 * 60

        self._index_path = self.cache_dir / "index.json"
        self._index: Dict[str, CacheEntry] = {}

        # Load existing index
        self._load_index()

        # Cleanup old entries
        self._cleanup()

    def _load_index(self) -> None:
        """Load the cache index from disk."""
        if self._index_path.exists():
            try:
                with open(self._index_path, "r") as f:
                    data = json.load(f)
                    self._index = {
                        k: CacheEntry(**v) for k, v in data.get("entries", {}).items()
                    }
                logger.info(f"Loaded {len(self._index)} cache entries")
            except Exception as e:
                logger.warning(f"Failed to load cache index: {e}")
                self._index = {}

    def _save_index(self) -> None:
        """Save the cache index to disk."""
        try:
            data = {
                "entries": {k: v.model_dump() for k, v in self._index.items()}
            }
            with open(self._index_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save cache index: {e}")

    def _cleanup(self) -> None:
        """Remove expired and oversized cache entries."""
        current_time = time.time()
        removed = 0

        # Remove expired entries
        expired = [
            k
            for k, v in self._index.items()
            if current_time - v.processed_at > self.max_age_seconds
        ]
        for key in expired:
            self._remove_entry(key)
            removed += len(expired)

        # Remove oldest entries if cache is too large
        cache_size = self._get_cache_size()
        while cache_size > self.max_size_bytes and self._index:
            # Find oldest entry
            oldest_key = min(self._index.keys(), key=lambda k: self._index[k].processed_at)
            self._remove_entry(oldest_key)
            removed += 1
            cache_size = self._get_cache_size()

        if removed > 0:
            logger.info(f"Cleaned up {removed} cache entries")

    def _remove_entry(self, key: str) -> None:
        """Remove a cache entry."""
        if key in self._index:
            entry = self._index[key]
            cache_file = self.cache_dir / f"{key}.pkl"
            if cache_file.exists():
                cache_file.unlink()
            del self._index[key]
            self._save_index()

    def _get_cache_size(self) -> int:
        """Get total size of cache files."""
        total = 0
        for f in self.cache_dir.glob("*.pkl"):
            total += f.stat().st_size
        return total

    def get(
        self, file_hash: str, file_path: str, file_size: int
    ) -> Optional[Dict[str, Any]]:
        """
        Get cached results for a file.

        Args:
            file_hash: Content hash of the file
            file_path: Path to the file
            file_size: Size of the file in bytes

        Returns:
            Cached results dict or None if not found/stale
        """
        entry = self._index.get(file_hash)

        if entry is None:
            return None

        # Verify file hasn't changed
        if entry.file_path != file_path or entry.file_size != file_size:
            logger.debug(f"Cache miss: file metadata changed for {file_hash}")
            return None

        # Load cached data
        cache_file = self.cache_dir / f"{file_hash}.pkl"
        if not cache_file.exists():
            # Index exists but data file is missing - corrupt entry
            self._remove_entry(file_hash)
            return None

        try:
            with open(cache_file, "rb") as f:
                data = pickle.load(f)
            logger.debug(f"Cache hit for {file_hash}")
            return data
        except Exception as e:
            logger.error(f"Failed to load cache data: {e}")
            self._remove_entry(file_hash)
            return None

    def put(
        self,
        file_hash: str,
        file_path: str,
        file_size: int,
        total_pages: int,
        page_contents: List[Dict[str, Any]],
        metadata: Dict[str, Any],
    ) -> None:
        """
        Store processed results in cache.

        Args:
            file_hash: Content hash of the file
            file_path: Path to the file
            file_size: Size of the file in bytes
            total_pages: Total number of pages
            page_contents: List of page content dicts
            metadata: Document metadata
        """
        entry = CacheEntry(
            file_hash=file_hash,
            file_path=file_path,
            file_size=file_size,
            total_pages=total_pages,
            processed_at=time.time(),
            page_contents=page_contents,
            metadata=metadata,
        )

        # Save data file
        cache_file = self.cache_dir / f"{file_hash}.pkl"
        try:
            with open(cache_file, "wb") as f:
                pickle.dump(
                    {
                        "page_contents": page_contents,
                        "metadata": metadata,
                        "total_pages": total_pages,
                    },
                    f,
                )
        except Exception as e:
            logger.error(f"Failed to save cache data: {e}")
            return

        # Update index
        self._index[file_hash] = entry
        self._save_index()

        logger.debug(f"Cached results for {file_hash} ({total_pages} pages)")

    def clear(self) -> None:
        """Clear all cache entries."""
        for key in list(self._index.keys()):
            self._remove_entry(key)
        logger.info("Cache cleared")

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            "entries": len(self._index),
            "size_mb": self._get_cache_size() / (1024 * 1024),
            "cache_dir": str(self.cache_dir),
        }
