"""Command-line interface: ``secnews fetch | sources | categories``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .classify import Classifier, deduplicate, filter_articles, resolve_ids
from .config import load_sources, load_taxonomy
from .feeds import enrich_from_pages, fetch_all, needs_page_lookup
from .models import Article
from .render import FORMATS, render


def _date_arg(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got '{value}'")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="secnews",
        description="Pull cybersecurity news from many sources, tag it by category/industry and build a digest.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--sources-file", help="custom sources JSON (default: bundled list)")
    p.add_argument("--categories-file", help="custom categories JSON (default: bundled rules)")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="fetch, tag and filter articles, then write a report",
                       description="Examples:\n"
                                   "  secnews fetch -o digest.html\n"
                                   "  secnews fetch --days 7 -c vulnerabilities -c ransomware -o vulns.md\n"
                                   "  secnews fetch -i healthcare,finance --format csv -o sectors.csv\n"
                                   "  secnews fetch -l uk -i charity -o uk-charities.md\n"
                                   "  secnews fetch --input last-week.json -c fraud -o fraud.md",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    when = f.add_argument_group("time window")
    when.add_argument("--days", type=int, default=7, help="look back this many days (default: 7)")
    when.add_argument("--since", type=_date_arg, help="start date YYYY-MM-DD (overrides --days)")
    when.add_argument("--until", type=_date_arg, help="end date YYYY-MM-DD, inclusive")
    when.add_argument("--include-undated", action="store_true", help="keep articles without a publish date")

    flt = f.add_argument_group("filters (repeat or comma-separate; ids, names or prefixes accepted)")
    flt.add_argument("-c", "--category", action="append", metavar="TOPIC", help="topic, e.g. vulnerabilities, fraud, ransomware")
    flt.add_argument("-i", "--industry", action="append", metavar="SECTOR", help="industry, e.g. healthcare, finance, charity")
    flt.add_argument("-l", "--location", action="append", metavar="REGION", help="location, e.g. uk, europe, north-america; country names also work (germany -> europe)")
    flt.add_argument("-s", "--source", action="append", metavar="NAME", help="only sources whose name contains NAME")
    flt.add_argument("--type", action="append", metavar="TYPE", help="source type: news, vendor-research, government, blog")
    flt.add_argument("--search", metavar="TEXT", help="free-text match on title/summary/CVE")
    flt.add_argument("--cve-only", action="store_true", help="only articles that mention a CVE id")

    out = f.add_argument_group("output")
    out.add_argument("-o", "--output", help="output file; format inferred from extension (.md .html .json .csv). Default: stdout")
    out.add_argument("-f", "--format", choices=FORMATS, help="output format (default: from -o extension, else md)")
    out.add_argument("--input", metavar="JSON", help="re-use articles from a previous --format json run instead of fetching")
    out.add_argument("--no-merge", action="store_true", help="don't merge the same story reported by multiple outlets")
    out.add_argument("--no-page-lookup", "--no-images", dest="no_page_lookup", action="store_true",
                     help="don't open article pages to find missing pictures or expand one-line summaries")
    out.add_argument("--workers", type=int, default=10, help="parallel downloads (default: 10)")
    out.add_argument("--timeout", type=float, default=20, help="per-feed timeout in seconds (default: 20)")
    out.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")

    s = sub.add_parser("sources", help="list configured sources")
    s.add_argument("--check", action="store_true", help="fetch every feed and report status / article counts")

    sub.add_parser("categories", help="list topic, industry and location categories")
    return p


def _log(args, msg: str) -> None:
    if not getattr(args, "quiet", False):
        print(msg, file=sys.stderr)


def cmd_fetch(args) -> int:
    sources = load_sources(args.sources_file)
    taxonomy = load_taxonomy(args.categories_file)
    try:
        topics = resolve_ids(args.category, taxonomy.topics, "topic")
        industries = resolve_ids(args.industry, taxonomy.industries, "industry")
        locations = resolve_ids(args.location, taxonomy.locations, "location")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.type:
        wanted = {t.strip().lower() for raw in args.type for t in raw.split(",")}
        sources = [s for s in sources if s.type.lower() in wanted]

    errors: dict[str, str] = {}
    if args.input:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        articles = [Article.from_dict(a) for a in payload["articles"]]
        allowed = {s.name for s in sources}
        if args.type:
            articles = [a for a in articles if a.source in allowed]
        errors = payload.get("errors", {})
        _log(args, f"Loaded {len(articles)} articles from {args.input}")
    else:
        active = [s for s in sources if s.enabled]
        _log(args, f"Fetching {len(active)} feeds…")
        results = fetch_all(active, workers=args.workers, timeout=args.timeout)
        articles = []
        for r in results:
            if r.error:
                errors[r.source.name] = r.error
                _log(args, f"  ✗ {r.source.name}: {r.error}")
            else:
                _log(args, f"  ✓ {r.source.name}: {len(r.articles)} articles")
            articles.extend(r.articles)
        _log(args, f"Fetched {len(articles)} articles ({len(results) - len(errors)}/{len(results)} feeds OK)")

    # Re-tag on every run so edits to categories.json apply to saved JSON too.
    classifier = Classifier(taxonomy, sources)
    for a in articles:
        classifier.classify(a)
    if not args.no_merge and not args.input:
        articles = deduplicate(articles)

    now = datetime.now(timezone.utc)
    since = args.since or (now - timedelta(days=args.days))
    until = args.until + timedelta(days=1) - timedelta(microseconds=1) if args.until else None
    filtered = filter_articles(
        articles,
        since=since,
        until=until,
        topics=topics or None,
        industries=industries or None,
        locations=locations or None,
        sources=args.source,
        search=args.search,
        cve_only=args.cve_only,
        include_undated=args.include_undated,
    )

    pending = sum(1 for a in filtered if needs_page_lookup(a))
    if pending and not args.no_page_lookup:
        _log(args, f"Reading {pending} article pages for missing pictures and short summaries…")
        images, summaries = enrich_from_pages(filtered, workers=args.workers, timeout=min(args.timeout, 10))
        _log(args, f"  found {images} pictures, expanded {summaries} summaries")

    filters = []
    if topics:
        filters.append("categories: " + ", ".join(taxonomy.name_of(t) for t in sorted(topics)))
    if industries:
        filters.append("industries: " + ", ".join(taxonomy.name_of(i) for i in sorted(industries)))
    if locations:
        filters.append("locations: " + ", ".join(taxonomy.name_of(l) for l in sorted(locations)))
    if args.source:
        filters.append("sources: " + ", ".join(args.source))
    if args.type:
        filters.append("source types: " + ", ".join(args.type))
    if args.search:
        filters.append(f'search: "{args.search}"')
    if args.cve_only:
        filters.append("CVE mentions only")
    meta = {"since": since, "until": until or now, "errors": errors, "filters": "; ".join(filters)}

    fmt = args.format
    if not fmt and args.output:
        ext = Path(args.output).suffix.lower().lstrip(".")
        fmt = {"markdown": "md", "htm": "html"}.get(ext, ext)
    fmt = fmt if fmt in FORMATS else "md"

    text = render(fmt, filtered, taxonomy, meta)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
        _log(args, f"Wrote {len(filtered)} articles to {args.output} ({fmt})")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_sources(args) -> int:
    sources = load_sources(args.sources_file)
    if not args.check:
        width = max(len(s.name) for s in sources)
        for s in sources:
            flag = "" if s.enabled else "  (disabled)"
            print(f"{s.name:<{width}}  {s.type:<16} {s.url}{flag}")
        return 0
    results = fetch_all(sources)
    bad = 0
    for r in results:
        if r.error:
            bad += 1
            print(f"✗ {r.source.name}: {r.error}")
        else:
            dated = [a.published for a in r.articles if a.published]
            newest = max(dated).strftime("%Y-%m-%d") if dated else "n/a"
            print(f"✓ {r.source.name}: {len(r.articles)} items, newest {newest}")
    print(f"\n{len(results) - bad}/{len(results)} feeds OK")
    return 1 if bad == len(results) else 0


def cmd_categories(args) -> int:
    t = load_taxonomy(args.categories_file)
    groups = (
        ("Topics (-c/--category)", t.topics),
        ("Industries (-i/--industry)", t.industries),
        ("Locations (-l/--location)", t.locations),
    )
    for title, cats in groups:
        print(title)
        for c in cats:
            print(f"  {c.id:<20} {c.name}")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {"fetch": cmd_fetch, "sources": cmd_sources, "categories": cmd_categories}[args.command](args)
