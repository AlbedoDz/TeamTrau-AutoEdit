"""Disk cache for expensive per-file analysis results (scores, crops, transcripts, tags, audio).

Entries are JSON files under ``config.cache_dir/analysis``. A key combines the file
identity (resolved path + size + ``st_mtime_ns``) with the parameters that influence the
result, so editing a clip or changing a relevant setting invalidates the entry by itself.
Writes are atomic (temp file + rename). Everything is a no-op when ``config.enable_cache``
is False, and a corrupt or unreadable entry is simply a miss.
"""

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from src.core.config import PipelineConfig

logger = logging.getLogger(__name__)

# Bump when the stored schema or the algorithm of any cached analysis changes.
CACHE_VERSION = 1

_write_lock = threading.Lock()


def file_fingerprint(path: Path) -> str:
    """Identity of a file: resolved path, size and mtime in nanoseconds."""
    p = Path(path)
    st = p.stat()
    return f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}"


def scene_key(start: float, end: float) -> str:
    """Stable per-scene key inside a per-clip entry."""
    return f"{start:.3f}-{end:.3f}"


class AnalysisCache:
    """JSON key/value store scoped by namespace (``visual``, ``reframe``, ``whisper`` ...)."""

    def __init__(self, config: PipelineConfig) -> None:
        self.enabled = bool(config.enable_cache)
        self.root = Path(config.cache_dir) / "analysis"

    def make_key(self, path: Path, **params: Any) -> str | None:
        """Hash of file identity + parameters; None when the file cannot be stat'ed."""
        try:
            fp = file_fingerprint(path)
        except OSError:
            return None
        payload = json.dumps(
            {"v": CACHE_VERSION, "fp": fp, "p": params}, sort_keys=True, default=str
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    def _file(self, namespace: str, path: Path, key: str) -> Path:
        safe_stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in Path(path).stem)[:40]
        return self.root / namespace / f"{safe_stem}_{key}.json"

    def load(self, namespace: str, path: Path, key: str | None) -> Any | None:
        """Return the stored JSON value or None on miss/disabled/corrupt entry."""
        if not self.enabled or key is None:
            return None
        f = self._file(namespace, path, key)
        try:
            with f.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.debug("Analysis cache read failed for %s: %s", f.name, exc)
            return None

    def save(self, namespace: str, path: Path, key: str | None, value: Any) -> None:
        """Atomically persist a JSON-serialisable value; failures are non-fatal."""
        if not self.enabled or key is None:
            return
        f = self._file(namespace, path, key)
        tmp = f.with_name(f"{f.name}.{threading.get_ident()}.part")
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(value, fh)
            with _write_lock:
                os.replace(tmp, f)
        except (OSError, TypeError, ValueError) as exc:
            logger.debug("Analysis cache write failed for %s: %s", f.name, exc)
        finally:
            tmp.unlink(missing_ok=True)
