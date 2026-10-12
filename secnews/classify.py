"""Keyword tagging, CVE extraction, de-duplication and filtering."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Category, Source, Taxonomy
from .models import Article

CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)
_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|source$|rss$)", re.IGNORECASE)
_STOPWORDS = set(
    "a an the and or of in on to for with by from at as is are was were be has have had its it this that "
    "after over into new says say report reports via".split()
)


def _compile(keywords: list[str]) -> list[tuple[str, re.Pattern, bool]]:
    """(keyword, pattern, is_phrase) per keyword.

    Whole word/phrase, tolerant of hyphen vs. space and of a plural ending ("power plant" also matches
    "power plants"). Short acronyms (all caps, <=4 chars) are case-sensitive and matched exactly.
    """
    pats = []
    for kw in keywords:
        words = re.split(r"[\s\-]+", kw.strip())
        acronym = kw.isupper() and len(kw) <= 4
        last = words[-1]
        if acronym:
            tail = re.escape(last) + "s?"  # PLC -> PLCs, HMI -> HMIs (still case-sensitive)
        elif not last[-1:].isalpha() or last.endswith("s"):
            tail = re.escape(last)
        elif re.search(r"[^aeiou]y$", last, re.IGNORECASE):  # facility -> facilities
            tail = re.escape(last[:-1]) + "(?:y|ies)"
        else:
            tail = re.escape(last) + "(?:s|es)?"
        body = r"[\s\-]+".join([re.escape(p) for p in words[:-1]] + [tail])
        flags = 0 if acronym else re.IGNORECASE
        pats.append((kw, re.compile(rf"(?<!\w){body}(?!\w)", flags), len(words) > 1))
    return pats


class Classifier:
    def __init__(self, taxonomy: Taxonomy, sources: list[Source] | None = None):
        self.taxonomy = taxonomy
        self._topics = [(c, _compile(c.keywords)) for c in taxonomy.topics]
        self._industries = [(c, _compile(c.keywords)) for c in taxonomy.industries]
        self._locations = [(c, _compile(c.keywords)) for c in taxonomy.locations]
        self._source_topics = {s.name: s.categories for s in (sources or [])}
        self._source_locations = {s.name: s.locations for s in (sources or [])}
        self._source_industries = {s.name: s.industries for s in (sources or [])}

    @staticmethod
    def _score(rules, title: str, body: str, tags: str, min_score: int) -> list[str]:
        """Title hits, the publisher's own tags and multi-word phrases are strong (2); a lone word in the body is 1."""
        scored = []
        for cat, pats in rules:
            score = 0
            for _, p, phrase in pats:
                if p.search(title) or p.search(tags):
                    score += 2
                elif p.search(body):
                    score += 2 if phrase else 1
            if score >= min_score:
                scored.append((score, cat.id))
        scored.sort(key=lambda x: -x[0])  # stable: ties keep config order
        return [cid for _, cid in scored]

    def classify(self, art: Article) -> Article:
        body, tags = art.summary, " | ".join(art.feed_tags)
        t = self.taxonomy
        topics = self._score(self._topics, art.title, body, tags, t.topic_min_score)
        for forced in self._source_topics.get(art.source, []):
            if forced not in topics:
                topics.append(forced)
        art.topics = topics or ["general"]
        industries = self._score(self._industries, art.title, body, tags, t.industry_min_score)
        for forced in self._source_industries.get(art.source, []):
            if forced not in industries:
                industries.append(forced)
        art.industries = industries
        locations = self._score(self._locations, art.title, body, tags, t.location_min_score)
        for forced in self._source_locations.get(art.source, []):
            if forced not in locations:
                locations.append(forced)
        art.locations = locations
        art.cves = sorted({m.upper() for m in CVE_RE.findall(f"{art.title} {body} {tags}")})
        return art


# --------------------------------------------------------------------------- de-duplication


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)])
    host = parts.netloc.lower().removeprefix("www.")
    return urlunsplit(("https", host, parts.path.rstrip("/"), query, ""))


def _title_tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in _STOPWORDS and len(w) > 1}


def deduplicate(articles: list[Article], similarity: float = 0.7) -> list[Article]:
    """Drop exact duplicates and merge near-identical headlines from different outlets.

    The earliest article is kept; the others' outlets go into ``also_reported_by`` so widely
    covered stories are easy to spot.
    """
    articles = sorted(articles, key=lambda a: (a.published is None, a.published or datetime.min))
    kept: list[Article] = []
    seen_urls: dict[str, Article] = {}
    token_index: list[tuple[set[str], Article]] = []
    for art in articles:
        url = normalize_url(art.link)
        if url in seen_urls:
            continue
        toks = _title_tokens(art.title)
        match = None
        if len(toks) >= 4:
            for other_toks, other in token_index:
                if art.cves and other.cves and not set(art.cves) & set(other.cves):
                    continue  # similar wording but different vulnerabilities: separate stories
                inter = len(toks & other_toks)
                if inter and inter / len(toks | other_toks) >= similarity:
                    match = other
                    break
        seen_urls[url] = match or art
        if match:
            if art.source != match.source and art.source not in match.also_reported_by:
                match.also_reported_by.append(art.source)
            match.cves = sorted(set(match.cves) | set(art.cves))
            continue
        kept.append(art)
        token_index.append((toks, art))
    return kept


# --------------------------------------------------------------------------- filtering


def resolve_ids(values: list[str] | None, cats: list[Category], kind: str) -> set[str]:
    """Accept category ids, display names or unambiguous prefixes, case-insensitively."""
    if not values:
        return set()
    out = set()
    lookup = {c.id.lower(): c.id for c in cats} | {c.name.lower(): c.id for c in cats}
    if kind == "topic":
        lookup["general"] = "general"
    for raw in values:
        for v in (x.strip().lower() for x in raw.split(",")):
            if not v:
                continue
            if v in lookup:
                out.add(lookup[v])
                continue
            hits = {cid for key, cid in lookup.items() if key.startswith(v) or v in key.split(" ")}
            if not hits:  # fall back to keywords, e.g. "germany" -> europe
                hits = {c.id for c in cats if v in (k.lower() for k in c.keywords)}
            if len(hits) == 1:
                out.add(hits.pop())
            else:
                choices = ", ".join(sorted({c.id for c in cats}))
                why = "ambiguous" if hits else "unknown"
                raise ValueError(f"{why} {kind} '{v}'. Choose from: {choices}")
    return out


def filter_articles(
    articles: list[Article],
    since: datetime | None = None,
    until: datetime | None = None,
    topics: set[str] | None = None,
    industries: set[str] | None = None,
    locations: set[str] | None = None,
    sources: list[str] | None = None,
    search: str | None = None,
    cve_only: bool = False,
    include_undated: bool = False,
) -> list[Article]:
    src_terms = [s.lower() for s in sources or []]
    needle = search.lower() if search else None
    out = []
    for a in articles:
        if a.published is None:
            if not include_undated and (since or until):
                continue
        else:
            if since and a.published < since:
                continue
            if until and a.published > until:
                continue
        if topics and not topics.intersection(a.topics):
            continue
        if industries and not industries.intersection(a.industries):
            continue
        if locations and not locations.intersection(a.locations):
            continue
        if src_terms and not any(t in a.source.lower() for t in src_terms):
            continue
        if needle and needle not in f"{a.title} {a.summary} {' '.join(a.cves)}".lower():
            continue
        if cve_only and not a.cves:
            continue
        out.append(a)
    out.sort(key=lambda a: (a.published is not None, a.published or datetime.min), reverse=True)
    return out
