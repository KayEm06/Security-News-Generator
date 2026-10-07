"""Fetching and parsing RSS 2.0, RSS 1.0 (RDF) and Atom feeds using only the standard library."""

from __future__ import annotations

import gzip
import html
import html.entities
import re
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

try:  # Prefer hardened parser when available; stdlib expat already refuses external entities.
    from defusedxml import ElementTree as ET  # type: ignore
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET  # noqa: N817

from .config import Source
from .models import Article

USER_AGENT = "Mozilla/5.0 (compatible; secnews/0.1; +https://github.com/kayem06/security-news-generator)"
MAX_FEED_BYTES = 10 * 1024 * 1024
SUMMARY_CHARS = 500


@dataclass
class FetchResult:
    source: Source
    articles: list[Article]
    error: str | None = None


# --------------------------------------------------------------------------- text helpers


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, limit: int = SUMMARY_CHARS) -> str:
    # Drop common feed boilerplate like "The post X appeared first on Y."
    text = re.sub(r"\s*The post .{0,300}? appeared first on .{0,100}?\.?$", "", text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    dt = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        pass
    if dt is None:
        iso = value.replace("Z", "+00:00")
        iso = re.sub(r"\.\d+", "", iso)  # drop fractional seconds of any precision
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- parsing


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _children(el, *names):
    return [c for c in el if _local(c.tag) in names]


def _text(el, *names) -> str:
    c = _child(el, *names)
    if c is None:
        return ""
    # Atom 'xhtml' content embeds child elements; serialise all text inside.
    return "".join(c.itertext()).strip()


_BARE_AMP = re.compile(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#\d+|#x[0-9a-fA-F]+);)")
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NAMED_ENTITY = re.compile(r"&([a-zA-Z][a-zA-Z0-9]*);")
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


def _fix_entity(m: re.Match) -> str:
    name = m.group(1)
    if name in _XML_ENTITIES:
        return m.group(0)
    cp = html.entities.name2codepoint.get(name)
    return f"&#{cp};" if cp else f"&amp;{name};"


def _parse_xml(data: bytes):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        # Many real-world feeds have stray control characters or unescaped ampersands.
        text = data.decode("utf-8", errors="replace")
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
        text = _BAD_CHARS.sub("", _NAMED_ENTITY.sub(_fix_entity, _BARE_AMP.sub("&amp;", text)))
        return ET.fromstring(text.encode("utf-8"))


def parse_feed(data: bytes, source: Source) -> list[Article]:
    root = _parse_xml(data)
    root_name = _local(root.tag)
    if root_name == "feed":
        entries = _children(root, "entry")
        parse_entry = _parse_atom_entry
    else:
        channel = _child(root, "channel")
        entries = _children(root, "item")  # RSS 1.0 items are siblings of channel
        if channel is not None:
            entries += _children(channel, "item")
        parse_entry = _parse_rss_item

    articles = []
    for e in entries:
        art = parse_entry(e, source)
        if art and art.title and art.link:
            articles.append(art)
    return articles


def _parse_rss_item(item, source: Source) -> Article | None:
    title = html_to_text(_text(item, "title"))
    link = _text(item, "link")
    if not link:
        guid = _child(item, "guid")
        if guid is not None and (guid.text or "").startswith("http"):
            link = guid.text.strip()
    body = _text(item, "description") or _text(item, "encoded")
    published = parse_date(_text(item, "pubDate") or _text(item, "date") or _text(item, "published"))
    tags = [html_to_text(c.text or "") for c in _children(item, "category", "subject") if c.text]
    return Article(
        title=title,
        link=link.strip(),
        source=source.name,
        source_type=source.type,
        published=published,
        summary=truncate(html_to_text(body)),
        feed_tags=[t for t in tags if t],
    )


def _parse_atom_entry(entry, source: Source) -> Article | None:
    title = html_to_text(_text(entry, "title"))
    link = ""
    for l in _children(entry, "link"):
        rel = l.get("rel", "alternate")
        if rel == "alternate" and l.get("href"):
            link = l.get("href")
            break
    if not link:
        l = _child(entry, "link")
        link = l.get("href", "") if l is not None else ""
    body = _text(entry, "summary") or _text(entry, "content")
    published = parse_date(_text(entry, "published") or _text(entry, "updated"))
    tags = [c.get("term") or c.get("label") or "" for c in _children(entry, "category")]
    return Article(
        title=title,
        link=link.strip(),
        source=source.name,
        source_type=source.type,
        published=published,
        summary=truncate(html_to_text(body)),
        feed_tags=[t for t in tags if t],
    )


# --------------------------------------------------------------------------- fetching


def _download(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URLs come from local config
        data = resp.read(MAX_FEED_BYTES + 1)
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("feed larger than 10 MB")
    if encoding == "gzip":
        data = gzip.decompress(data)
    elif encoding == "deflate":
        data = zlib.decompress(data)
    return data


def fetch_source(source: Source, timeout: float = 20.0) -> FetchResult:
    try:
        data = _download(source.url, timeout)
        if data.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
            raise ValueError("received an HTML page instead of a feed (blocked or URL changed)")
        return FetchResult(source, parse_feed(data, source))
    except Exception as exc:  # report per-source failures without aborting the run
        msg = str(exc) or exc.__class__.__name__
        return FetchResult(source, [], error=f"{exc.__class__.__name__}: {msg}"[:300])


def fetch_all(sources: list[Source], workers: int = 10, timeout: float = 20.0) -> list[FetchResult]:
    sources = [s for s in sources if s.enabled]
    if not sources:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(sources)))) as pool:
        return list(pool.map(lambda s: fetch_source(s, timeout), sources))
