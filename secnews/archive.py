"""Local article archive, so reports can reach further back than feeds keep items."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Article

DEFAULT_KEEP_DAYS = 90


def default_path() -> Path:
    return Path(os.environ.get("SECNEWS_ARCHIVE") or Path.home() / ".secnews" / "archive.json")


def load_archive(path: Path) -> list[Article]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    return [Article.from_dict(a) for a in payload.get("articles", [])]


def save_archive(path: Path, articles: list[Article], keep_days: int = DEFAULT_KEEP_DAYS) -> int:
    """Write dated articles from the last ``keep_days`` days; returns how many were kept."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    keep = [a for a in articles if a.published and a.published >= cutoff]
    keep.sort(key=lambda a: a.published, reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"articles": [a.to_dict() for a in keep]}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # atomic, so an interrupted run can't corrupt the archive
    return len(keep)
