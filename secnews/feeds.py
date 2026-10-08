"""Fetching and parsing RSS 2.0, RSS 1.0 (RDF) and Atom feeds using only the standard library."""

from __future__ import annotations

import gzip
import html
import html.entities
import re
import urllib.request
import zlib
from urllib.parse import urljoin
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


# --------------------------------------------------------------------------- images

_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_TAG_ATTR = re.compile(r"""([a-zA-Z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_IMAGE_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|avif)(?:[?#]|$)", re.IGNORECASE)
# Never the article picture: tracking pixels, author photos, emoji, share buttons, ads.
_JUNK_IMAGE = re.compile(
    r"pixel|spacer|blank\.gif|gravatar|avatar|/emoji|/smilies/|feedburner\.com/~|sprite|"
    r"share[-_]?button|/ads?/|doubleclick",
    re.IGNORECASE,
)
# Site chrome that shows up as <img> tags in page/feed bodies (a share image may legitimately be a logo).
_CHROME_IMAGE = re.compile(r"(?<![a-z])(?:logos?|icons?|favicon|badges?)(?![a-z])|\.svg(?:[?#]|$)", re.IGNORECASE)
_LAZY_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-orig-file", "data-large-file", "src")


def _attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): html.unescape(m.group(2) or m.group(3) or m.group(4) or "") for m in _TAG_ATTR.finditer(tag)}


def _http(url: str | None, base: str = "") -> str:
    """Absolute http(s) URL, resolving relative paths against the article link; '' if unusable."""
    url = html.unescape((url or "").strip())
    if not url:
        return ""
    if base and not url.lower().startswith(("http://", "https://")):
        url = urljoin(base, url)
    return url if url.lower().startswith(("https://", "http://")) else ""


def _too_small(attrs: dict[str, str]) -> bool:
    for key in ("width", "height"):
        m = re.match(r"\s*(\d+)", attrs.get(key, ""))
        if m and int(m.group(1)) < 80:
            return True
    return False


def _img_url(a: dict[str, str], base: str) -> str:
    """Best source URL for one <img>, honouring lazy-load attributes and srcset; '' if it looks like chrome."""
    if _too_small(a):
        return ""
    candidates = [a.get(k, "") for k in _LAZY_SRC_ATTRS]
    srcset = a.get("srcset") or a.get("data-srcset") or ""
    if srcset:  # take the last (usually largest) entry
        candidates.append(srcset.split(",")[-1].strip().split(" ")[0])
    for c in candidates:
        url = _http(c, base)
        if url and not _JUNK_IMAGE.search(url) and not _CHROME_IMAGE.search(url):
            return url
    return ""


def _img_from_html(body: str, base: str = "") -> str:
    """First plausible article picture in an HTML fragment."""
    if not body:
        return ""
    if "<img" not in body.lower() and "&lt;img" in body.lower():
        body = html.unescape(body)  # double-escaped HTML in some feeds
    for tag in _IMG_TAG.findall(body):
        url = _img_url(_attrs(tag), base)
        if url:
            return url
    return ""


def _find_image(el, *bodies: str, base: str = "") -> str:
    """Article image from media:thumbnail/content, image enclosures, inline XHTML or the first real <img>."""
    for c in el.iter():
        name, url = _local(c.tag), c.get("url") or c.get("href")
        typ, medium = c.get("type", ""), c.get("medium", "")
        is_image_type = typ.startswith("image/") or (not typ and url and _IMAGE_EXT.search(url))
        if name == "thumbnail" and url:
            found = _http(url, base)
        elif name == "content" and url and (medium == "image" or is_image_type):
            found = _http(url, base)
        elif name == "enclosure" and url and is_image_type:
            found = _http(url, base)
        elif name == "link" and c.get("rel") == "enclosure" and url and is_image_type:
            found = _http(url, base)
        elif name == "img":  # Atom type="xhtml" content keeps <img> as real elements
            found = _img_url({k.lower(): v for k, v in c.attrib.items()}, base)
        else:
            continue
        if found and not _JUNK_IMAGE.search(found):
            return found
    for body in bodies:
        found = _img_from_html(body, base)
        if found:
            return found
    return ""


_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_LINK_TAG = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_JSONLD_IMAGE = re.compile(r'"image"\s*:\s*(?:\[\s*)?(?:\{[^{}]*?"url"\s*:\s*)?"([^"]+)"')
_PAGE_IMAGE_KEYS = ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src")
MAX_PAGE_BYTES = 1024 * 1024


def page_image(page: str, base: str) -> str:
    """Main picture declared by an article web page: Open Graph / Twitter card, image_src, JSON-LD, then <img>."""
    end = page.lower().find("</head>")
    head = page[: end if end > 0 else 300_000]
    found: dict[str, str] = {}
    for tag in _META_TAG.findall(head):
        a = _attrs(tag)
        key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
        if key in _PAGE_IMAGE_KEYS + ("image", "thumbnailurl") and a.get("content"):
            found.setdefault(key, a["content"])
    for key in _PAGE_IMAGE_KEYS + ("image", "thumbnailurl"):
        url = _http(found.get(key), base)
        if url and not _JUNK_IMAGE.search(url):
            return url
    for tag in _LINK_TAG.findall(head):
        a = _attrs(tag)
        if a.get("rel", "").lower() == "image_src":
            url = _http(a.get("href"), base)
            if url:
                return url
    for m in _JSONLD_IMAGE.finditer(page):
        url = _http(m.group(1).replace("\\/", "/"), base)
        if url and not _JUNK_IMAGE.search(url):
            return url
    body = page[end:] if end > 0 else page
    art = re.search(r"<article\b.*?</article>", body, re.IGNORECASE | re.DOTALL)
    return _img_from_html(art.group(0) if art else body, base)


SHORT_SUMMARY = 200  # summaries shorter than this get a fuller description from the article page
_PAGE_DESC_KEYS = ("og:description", "description", "twitter:description")
_PARA = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_STRIP_BLOCKS = re.compile(r"<(script|style|noscript|nav|header|footer|aside|form|figure)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BOILERPLATE = re.compile(
    r"cookie|subscribe|sign up|newsletter|advertis|all rights reserved|©|copyright|follow us|"
    r"share this|related articles?|read more|click here|javascript|log ?in to|you may also like",
    re.IGNORECASE,
)


def page_description(page: str) -> str:
    """A fuller article summary: the page's meta description if substantial, else its opening paragraphs."""
    end = page.lower().find("</head>")
    head = page[: end if end > 0 else 300_000]
    meta = ""
    for tag in _META_TAG.findall(head):
        a = _attrs(tag)
        key = (a.get("property") or a.get("name") or "").lower()
        if key in _PAGE_DESC_KEYS:
            text = html_to_text(a.get("content", ""))
            if len(text) > len(meta):
                meta = text
    if len(meta) >= SHORT_SUMMARY:
        return truncate(meta)
    body = page[end:] if end > 0 else page
    art = re.search(r"<article\b.*?</article>", body, re.IGNORECASE | re.DOTALL)
    body = _STRIP_BLOCKS.sub(" ", art.group(0) if art else body)
    paras: list[str] = []
    for m in _PARA.finditer(body):
        text = html_to_text(m.group(1))
        if len(text) < 60 or _BOILERPLATE.search(text):
            continue
        paras.append(text)
        if sum(len(p) for p in paras) >= SUMMARY_CHARS:
            break
    from_paras = " ".join(paras)
    return truncate(from_paras if len(from_paras) > len(meta) else meta)


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
    description, encoded = _text(item, "description"), _text(item, "encoded")
    body = description or encoded
    published = parse_date(_text(item, "pubDate") or _text(item, "date") or _text(item, "published"))
    tags = [html_to_text(c.text or "") for c in _children(item, "category", "subject") if c.text]
    return Article(
        title=title,
        link=link.strip(),
        source=source.name,
        source_type=source.type,
        published=published,
        summary=truncate(html_to_text(body)),
        image=_find_image(item, description, encoded, base=link.strip()),
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
    summary, content = _text(entry, "summary"), _text(entry, "content")
    body = summary or content
    published = parse_date(_text(entry, "published") or _text(entry, "updated"))
    tags = [c.get("term") or c.get("label") or "" for c in _children(entry, "category")]
    return Article(
        title=title,
        link=link.strip(),
        source=source.name,
        source_type=source.type,
        published=published,
        summary=truncate(html_to_text(body)),
        image=_find_image(entry, summary, content, base=link.strip()),
        feed_tags=[t for t in tags if t],
    )


# --------------------------------------------------------------------------- fetching


FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
PAGE_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"


def _download(url: str, timeout: float, accept: str = FEED_ACCEPT, max_bytes: int = MAX_FEED_BYTES,
              truncate_ok: bool = False) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip, deflate"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - feed URLs come from local config
        data = resp.read(max_bytes + 1)
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if len(data) > max_bytes:
        if not truncate_ok:
            raise ValueError(f"response larger than {max_bytes // (1024 * 1024)} MB")
        data = data[:max_bytes]
    if encoding == "gzip":
        data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data) if truncate_ok else gzip.decompress(data)
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


def needs_page_lookup(article: Article) -> bool:
    return not article.image or len(article.summary) < SHORT_SUMMARY


def _read_page(article: Article, timeout: float) -> tuple[str, str]:
    """(image, description) from the article's web page; empty strings if it can't be read."""
    try:
        data = _download(article.link, timeout, accept=PAGE_ACCEPT, max_bytes=MAX_PAGE_BYTES, truncate_ok=True)
        page = data.decode("utf-8", errors="replace")
    except Exception:  # unreadable pages keep the feed's summary and the placeholder image
        return "", ""
    image = "" if article.image else page_image(page, article.link)
    desc = page_description(page) if len(article.summary) < SHORT_SUMMARY else ""
    return image, desc


def enrich_from_pages(articles: list[Article], workers: int = 10, timeout: float = 10.0,
                      limit: int = 500) -> tuple[int, int]:
    """Read article pages to fill in missing pictures and expand one-line summaries.

    Returns (pictures found, summaries expanded).
    """
    todo = [a for a in articles if needs_page_lookup(a)
            and a.link.lower().startswith(("https://", "http://", "file://"))][:limit]
    if not todo:
        return 0, 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as pool:
        results = list(pool.map(lambda a: _read_page(a, timeout), todo))
    images = summaries = 0
    for art, (image, desc) in zip(todo, results):
        if image:
            art.image, images = image, images + 1
        # Only replace when the page genuinely says more than the feed did.
        if desc and len(desc) > len(art.summary) + 40:
            art.summary, summaries = desc, summaries + 1
    return images, summaries
