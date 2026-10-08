from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_SOURCES = DATA_DIR / "sources.json"
DEFAULT_CATEGORIES = DATA_DIR / "categories.json"
DEFAULT_SITE = DATA_DIR / "site.json"


def load_site(path: Path | str | None = None) -> dict:
    """Page/briefing settings; keys starting with '_' are comments."""
    data = json.loads(Path(path or DEFAULT_SITE).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


@dataclass
class Source:
    name: str
    url: str
    type: str = "news"
    categories: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    format: str = "feed"  # "feed" (RSS/Atom) or "cisa-kev" (CISA Known Exploited Vulnerabilities JSON)
    enabled: bool = True
    note: str = ""


@dataclass
class Category:
    id: str
    name: str
    keywords: list[str]
    color: str = ""


@dataclass
class Taxonomy:
    topics: list[Category]
    industries: list[Category]
    locations: list[Category] = field(default_factory=list)
    topic_min_score: int = 1
    industry_min_score: int = 2
    location_min_score: int = 2

    def name_of(self, cat_id: str) -> str:
        for c in self.topics + self.industries + self.locations:
            if c.id == cat_id:
                return c.name
        return "General" if cat_id == "general" else cat_id


def load_sources(path: Path | str | None = None) -> list[Source]:
    data = json.loads(Path(path or DEFAULT_SOURCES).read_text(encoding="utf-8"))
    return [Source(**s) for s in data["sources"]]


def load_taxonomy(path: Path | str | None = None) -> Taxonomy:
    data = json.loads(Path(path or DEFAULT_CATEGORIES).read_text(encoding="utf-8"))
    return Taxonomy(
        topics=[Category(**c) for c in data["topics"]],
        industries=[Category(**c) for c in data["industries"]],
        locations=[Category(**c) for c in data.get("locations", [])],
        topic_min_score=data.get("topic_min_score", 1),
        industry_min_score=data.get("industry_min_score", 2),
        location_min_score=data.get("location_min_score", 2),
    )
