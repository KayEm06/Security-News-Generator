"""Command-line interface: ``secnews fetch | briefing | subscribers | sources | categories``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .archive import DEFAULT_KEEP_DAYS, default_path, load_archive, save_archive
from .classify import Classifier, deduplicate, filter_articles, resolve_ids
from .config import load_site, load_sources, load_taxonomy
from .feeds import enrich_from_pages, fetch_all, needs_page_lookup
from .models import Article
from .render import FORMATS, render


def _date_arg(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got '{value}'")


def _gather_options() -> argparse.ArgumentParser:
    """Options shared by every command that collects articles (fetch, briefing)."""
    g = argparse.ArgumentParser(add_help=False)
    c = g.add_argument_group("collecting")
    c.add_argument("--type", action="append", metavar="TYPE",
                   help="source type: news, specialist, vendor-research, government, blog")
    c.add_argument("--input", metavar="JSON", help="re-use articles from a previous --format json run instead of fetching")
    c.add_argument("--no-merge", action="store_true", help="don't merge the same story reported by multiple outlets")
    c.add_argument("--no-page-lookup", "--no-images", dest="no_page_lookup", action="store_true",
                   help="don't open article pages to find missing pictures or expand one-line summaries")
    c.add_argument("--archive", metavar="JSON",
                   help="where fetched articles are kept between runs (default: ~/.secnews/archive.json)")
    c.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS,
                   help=f"how long the archive keeps articles (default: {DEFAULT_KEEP_DAYS})")
    c.add_argument("--no-archive", action="store_true", help="don't read or update the archive")
    c.add_argument("--backfill", type=int, default=0, metavar="PAGES",
                   help="also fetch up to PAGES older pages of each feed that supports it, to fill in history "
                        "(e.g. 5 on a first run; default: 0)")
    c.add_argument("--workers", type=int, default=10, help="parallel downloads (default: 10)")
    c.add_argument("--timeout", type=float, default=20, help="per-feed timeout in seconds (default: 20)")
    c.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    return g


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="secnews",
        description="Pull cybersecurity news from many sources, tag it by category/industry and build a digest.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--sources-file", help="custom sources JSON (default: bundled list)")
    p.add_argument("--categories-file", help="custom categories JSON (default: bundled rules)")
    p.add_argument("--site-file", help="custom page/briefing settings JSON (default: bundled site.json)")
    sub = p.add_subparsers(dest="command", required=True)
    gather = _gather_options()

    f = sub.add_parser("fetch", parents=[gather], help="fetch, tag and filter articles, then write a report",
                       description="Examples:\n"
                                   "  secnews fetch -o digest.html\n"
                                   "  secnews fetch --days 7 -c vulnerabilities -c ransomware -o vulns.md\n"
                                   "  secnews fetch -i healthcare,finance --format csv -o sectors.csv\n"
                                   "  secnews fetch -l uk -i charity -o uk-charities.md\n"
                                   "  secnews fetch --input last-week.json -c fraud -o fraud.md",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    when = f.add_argument_group("time window")
    when.add_argument("--days", type=int, default=30, help="look back this many days (default: 30)")
    when.add_argument("--since", type=_date_arg, help="start date YYYY-MM-DD (overrides --days)")
    when.add_argument("--until", type=_date_arg, help="end date YYYY-MM-DD, inclusive")
    when.add_argument("--include-undated", action="store_true", help="keep articles without a publish date")

    flt = f.add_argument_group("filters (repeat or comma-separate; ids, names or prefixes accepted)")
    flt.add_argument("-c", "--category", action="append", metavar="TOPIC", help="topic, e.g. vulnerabilities, fraud, ransomware")
    flt.add_argument("-i", "--industry", action="append", metavar="SECTOR", help="industry, e.g. healthcare, finance, charity")
    flt.add_argument("-l", "--location", action="append", metavar="REGION", help="location, e.g. uk, europe, north-america; country names also work (germany -> europe)")
    flt.add_argument("-s", "--source", action="append", metavar="NAME", help="only sources whose name contains NAME")
    flt.add_argument("--search", metavar="TEXT", help="free-text match on title/summary/CVE")
    flt.add_argument("--cve-only", action="store_true", help="only articles that mention a CVE id")

    out = f.add_argument_group("output")
    out.add_argument("-o", "--output", help="output file; format inferred from extension (.md .html .json .csv). Default: stdout")
    out.add_argument("-f", "--format", choices=FORMATS, help="output format (default: from -o extension, else md)")

    b = sub.add_parser("briefing", parents=[gather], help="build the daily briefing email and optionally send it",
                       description="Builds an email of the top stories from the last 24 hours.\n\n"
                                   "Examples:\n"
                                   "  secnews briefing -o briefing.html          # preview it in a browser\n"
                                   "  secnews briefing --send                    # email everyone in the subscriber list\n"
                                   "  secnews briefing --send --to me@example.com\n\n"
                                   "Set up sending once with 'secnews email setup'. Environment variables override it\n"
                                   "(useful for scheduled jobs): SECNEWS_SMTP_HOST, SECNEWS_SMTP_PORT (587),\n"
                                   "SECNEWS_SMTP_USER, SECNEWS_SMTP_PASSWORD, SECNEWS_SMTP_FROM,\n"
                                   "SECNEWS_SMTP_SECURITY (starttls | ssl | none)",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    b.add_argument("--hours", type=int, default=24, help="stories from the last N hours (default: 24)")
    b.add_argument("--max", type=int, help="maximum stories (default: briefing_max_stories in site.json)")
    b.add_argument("-o", "--output", help="also save the email as an HTML file")
    b.add_argument("--send", action="store_true", help="email it to the subscriber list (or --to)")
    b.add_argument("--to", action="append", metavar="EMAIL", help="send to these addresses instead of the subscriber list")
    b.add_argument("--subscribers", metavar="FILE", help="subscriber list (default: ~/.secnews/subscribers.txt)")

    e = sub.add_parser("email", help="set up and test sending email (for the daily briefing)",
                       description="  secnews email setup                 # guided set-up, then sends a test email\n"
                                   "  secnews email test --to me@example.com\n"
                                   "  secnews email show                  # current settings (password hidden)",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    e.add_argument("action", choices=["setup", "test", "show"])
    e.add_argument("--to", metavar="EMAIL", help="where to send the test email (default: the sending address)")

    m = sub.add_parser("subscribers", help="manage the daily briefing subscriber list")
    m.add_argument("action", choices=["list", "add", "remove"])
    m.add_argument("emails", nargs="*", metavar="EMAIL")
    m.add_argument("--subscribers", metavar="FILE", help="subscriber list (default: ~/.secnews/subscribers.txt)")

    s = sub.add_parser("sources", help="list configured sources")
    s.add_argument("--check", action="store_true", help="fetch every feed and report status / article counts")

    sub.add_parser("categories", help="list topic, industry and location categories")
    return p


def _log(args, msg: str) -> None:
    if not getattr(args, "quiet", False):
        print(msg, file=sys.stderr)


def _gather(args, taxonomy) -> tuple[list[Article], list[Article], dict[str, str], Path | None]:
    """Fetch (or load), merge the archive, tag and de-duplicate.

    Returns (articles for this run, every article incl. other source types, fetch errors, archive path).
    """
    all_sources = sources = load_sources(args.sources_file)
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
        extra = f" (plus up to {args.backfill} older pages each)" if args.backfill else ""
        _log(args, f"Fetching {len(active)} feeds{extra}…")
        results = fetch_all(active, workers=args.workers, timeout=args.timeout, backfill=max(0, args.backfill))
        articles = []
        for r in results:
            if r.error:
                errors[r.source.name] = r.error
                _log(args, f"  ✗ {r.source.name}: {r.error}")
            else:
                _log(args, f"  ✓ {r.source.name}: {len(r.articles)} articles")
            articles.extend(r.articles)
        _log(args, f"Fetched {len(articles)} articles ({len(results) - len(errors)}/{len(results)} feeds OK)")

    # Feeds only carry recent items, so earlier runs' articles are kept in an archive and merged back in.
    archive_path = None if (args.input or args.no_archive) else Path(args.archive) if args.archive else default_path()
    if archive_path:
        archived = load_archive(archive_path)
        if archived:
            _log(args, f"Merged {len(archived)} articles from earlier runs ({archive_path})")
        articles = archived + articles  # archived first, so their saved pictures/summaries win de-duplication

    # Re-tag on every run so edits to categories.json apply to saved JSON too.
    classifier = Classifier(taxonomy, all_sources)
    for a in articles:
        classifier.classify(a)
    if not args.input:
        # With --no-merge, still drop exact repeats of the same link (e.g. archived + re-fetched).
        articles = deduplicate(articles, similarity=2.0 if args.no_merge else 0.7)
    everything = articles
    if archive_path and args.type:
        allowed = {s.name for s in sources}
        articles = [a for a in articles if a.source in allowed]
    return articles, everything, errors, archive_path


def _enrich_and_archive(args, selected: list[Article], everything: list[Article], archive_path: Path | None) -> None:
    pending = sum(1 for a in selected if needs_page_lookup(a))
    if pending and not args.no_page_lookup:
        _log(args, f"Reading {pending} article pages for missing pictures and short summaries…")
        images, summaries = enrich_from_pages(selected, workers=args.workers, timeout=min(args.timeout, 10))
        _log(args, f"  found {images} pictures, expanded {summaries} summaries")
    if archive_path:
        kept = save_archive(archive_path, everything, args.keep_days)
        _log(args, f"Archive now holds {kept} articles from the last {args.keep_days} days")


def cmd_fetch(args) -> int:
    taxonomy = load_taxonomy(args.categories_file)
    try:
        topics = resolve_ids(args.category, taxonomy.topics, "topic")
        industries = resolve_ids(args.industry, taxonomy.industries, "industry")
        locations = resolve_ids(args.location, taxonomy.locations, "location")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    articles, everything, errors, archive_path = _gather(args, taxonomy)

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
    _enrich_and_archive(args, filtered, everything, archive_path)

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
    meta = {"since": since, "until": until or now, "errors": errors, "filters": "; ".join(filters),
            "site": load_site(args.site_file)}

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


def cmd_briefing(args) -> int:
    from . import briefing as bf

    taxonomy = load_taxonomy(args.categories_file)
    site = load_site(args.site_file)
    title = site.get("briefing_title") or "Daily Briefing"
    limit = args.max or int(site.get("briefing_max_stories") or 15)

    articles, everything, _, archive_path = _gather(args, taxonomy)
    now = datetime.now(timezone.utc)
    recent = filter_articles(articles, since=now - timedelta(hours=args.hours))
    stories = bf.pick_stories(recent, limit)
    _enrich_and_archive(args, stories, everything, archive_path)

    html_body = bf.build_html(stories, taxonomy, title, now)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(html_body, encoding="utf-8")
        _log(args, f"Wrote briefing with {len(stories)} stories to {args.output}")
    if not args.send:
        if not args.output:
            sys.stdout.write(bf.build_text(stories, title, now) + "\n")
        return 0

    recipients = [e.strip().lower() for e in args.to] if args.to else bf.load_subscribers(
        Path(args.subscribers) if args.subscribers else bf.subscribers_path())
    if not recipients:
        print("error: nobody to send to. Add people with 'secnews subscribers add EMAIL' or use --to.", file=sys.stderr)
        return 2
    try:
        settings = bf.smtp_settings()
    except bf.SmtpConfigError as e:
        print(f"error: email isn't set up: {e}.", file=sys.stderr)
        return 2
    subject = f"{title}: {now.strftime('%d %b %Y')}"
    failed = bf.send(recipients, subject, html_body, bf.build_text(stories, title, now), settings)
    _log(args, f"Sent briefing to {len(recipients) - len(failed)} of {len(recipients)} subscribers")
    for addr in failed:
        print(f"  ✗ could not send to {addr}", file=sys.stderr)
    return 1 if failed and len(failed) == len(recipients) else 0


def _ask(prompt: str, default: str = "") -> str:
    answer = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return answer or default


def _send_test(bf, to: str) -> int:
    try:
        settings = bf.smtp_settings()
    except bf.SmtpConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    html_body = ("<p>This is a test from <b>secnews</b>. If you can read this, the daily briefing can be sent "
                 "from this account.</p>")
    try:
        failed = bf.send([to], "secnews test email", html_body, "This is a test from secnews. Email is working.", settings)
    except Exception as exc:  # show the server's reason (wrong password, SMTP disabled, blocked port...)
        print(f"✗ Couldn't send: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        tip = next((p["tip"] for p in bf.PROVIDERS.values() if p["host"] == settings["host"] and p["tip"]), "")
        if tip:
            print(f"  Tip: {tip}", file=sys.stderr)
        return 1
    if failed:
        print(f"✗ The server refused the address {to}", file=sys.stderr)
        return 1
    print(f"✓ Test email sent to {to} at {now.strftime('%H:%M UTC')}. Check the inbox (and spam folder).")
    return 0


def cmd_email(args) -> int:
    from . import briefing as bf
    import getpass

    path = bf.email_config_path()
    if args.action == "show":
        cfg = bf.load_email_config()
        if not cfg:
            print(f"Not set up yet ({path} doesn't exist). Run: secnews email setup")
            return 0
        for k in ("host", "port", "security", "user", "sender"):
            print(f"{k:<9} {cfg.get(k, '')}")
        print(f"password  {'(saved)' if cfg.get('password') else '(not saved: uses SECNEWS_SMTP_PASSWORD)'}")
        print(f"file      {path}")
        return 0
    if args.action == "test":
        try:
            sender = bf.smtp_settings()["sender"]
        except bf.SmtpConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return _send_test(bf, args.to or sender)

    print("Set up email for the daily briefing. Press Enter to accept the [default].\n")
    keys = list(bf.PROVIDERS)
    for n, k in enumerate(keys, 1):
        print(f"  {n}. {bf.PROVIDERS[k]['label']}")
    choice = _ask("\nWhich email provider do you use? (number)", "1")
    try:
        provider = bf.PROVIDERS[keys[int(choice) - 1]]
    except (ValueError, IndexError):
        print("error: pick one of the numbers above", file=sys.stderr)
        return 2
    if provider["tip"]:
        print(f"\nNote: {provider['tip']}")
    old = bf.load_email_config()
    cfg = {"host": provider["host"] or _ask("SMTP server (e.g. mail.example.com)", old.get("host", ""))}
    if provider["host"]:
        cfg["port"], cfg["security"] = provider["port"], provider["security"]
    else:
        cfg["port"] = int(_ask("Port", str(old.get("port", 587))))
        cfg["security"] = _ask("Security: starttls, ssl or none", old.get("security", "starttls"))
    cfg["user"] = _ask("Email address you sign in with", old.get("user", ""))
    cfg["sender"] = _ask("Send the briefing from (address people will see)", old.get("sender") or cfg["user"])
    password = getpass.getpass("Password or app password (typing is hidden): ")
    if not password and old.get("password"):
        password = old["password"]
    store = _ask(f"Save the password in {path}? It's readable only by your user account (y/n)", "y").lower()
    if store.startswith("y"):
        cfg["password"] = password
    else:
        print("Not saved. Set SECNEWS_SMTP_PASSWORD in your environment before sending.")
        import os
        os.environ["SECNEWS_SMTP_PASSWORD"] = password  # still usable for the test below
    saved_to = bf.save_email_config(cfg)
    print(f"\nSaved settings to {saved_to}")
    to = _ask("Send a test email to", cfg["sender"])
    return _send_test(bf, to)


def cmd_subscribers(args) -> int:
    from . import briefing as bf

    path = Path(args.subscribers) if args.subscribers else bf.subscribers_path()
    current = bf.load_subscribers(path)
    if args.action == "list":
        for e in current:
            print(e)
        print(f"{len(current)} subscriber(s) in {path}", file=sys.stderr)
        return 0
    if not args.emails:
        print(f"error: give at least one email address to {args.action}", file=sys.stderr)
        return 2
    emails = [e.strip().lower() for e in args.emails]
    bad = [e for e in emails if not bf.EMAIL_RE.match(e)]
    if bad:
        print(f"error: not an email address: {', '.join(bad)}", file=sys.stderr)
        return 2
    if args.action == "add":
        updated = current + [e for e in emails if e not in current]
    else:
        updated = [e for e in current if e not in emails]
    bf.save_subscribers(path, updated)
    print(f"{len(updated)} subscriber(s) in {path}")
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
    commands = {"fetch": cmd_fetch, "briefing": cmd_briefing, "email": cmd_email, "subscribers": cmd_subscribers,
                "sources": cmd_sources, "categories": cmd_categories}
    return commands[args.command](args)
