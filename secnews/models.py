from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass
class Article:
    title: str
    link: str
    source: str
    published: datetime | None = None
    summary: str = ""
    image: str = ""
    source_type: str = ""
    feed_tags: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    cves: list[str] = field(default_factory=list)
    also_reported_by: list[str] = field(default_factory=list)

    @property
    def primary_topic(self) -> str:
        return self.topics[0] if self.topics else "general"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.isoformat() if self.published else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        d = dict(d)
        if d.get("published"):
            d["published"] = datetime.fromisoformat(d["published"])
        return cls(**d)
