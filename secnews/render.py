"""Output formats: Markdown digest, interactive HTML, JSON and CSV."""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .config import Taxonomy
from .models import Article
from .scoring import load_scorer
from .stories import leads, outlets

FORMATS = ("md", "html", "json", "csv")


def _date(a: Article) -> str:
    return a.published.strftime("%Y-%m-%d") if a.published else "undated"


def _period(meta: dict) -> str:
    since, until = meta.get("since"), meta.get("until")
    fmt = lambda d: d.strftime("%d %b %Y")  # noqa: E731
    if since and until:
        return f"{fmt(since)} – {fmt(until)}"
    if since:
        return f"since {fmt(since)}"
    return "all available"


def _md_escape(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def to_markdown(articles: list[Article], taxonomy: Taxonomy, meta: dict) -> str:
    name = taxonomy.name_of
    sources = {a.source for a in articles}
    stories = leads(articles)  # one entry per story; other outlets' articles listed under it
    legend = (meta.get("scorer") or load_scorer()).legend()
    level_name = {l["id"]: l["name"] for l in legend["levels"]}
    out = [
        "# Cybersecurity News Digest",
        "",
        f"**Period:** {_period(meta)}  ",
        f"**Articles:** {len(articles)} from {len(sources)} sources, in {len(stories)} stories  ",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    if meta.get("filters"):
        out.append(f"  \n**Filters:** {meta['filters']}")
    out.append("")

    by_topic: dict[str, list[Article]] = {}
    for a in stories:
        by_topic.setdefault(a.primary_topic, []).append(a)
    order = [t.id for t in taxonomy.topics if t.id in by_topic] + (["general"] if "general" in by_topic else [])

    if articles:
        out += ["## At a glance", "", "| Category | Stories |", "|---|---:|"]
        out += [f"| [{name(t)}](#{_anchor(name(t))}) | {len(by_topic[t])} |" for t in order]
        out.append("")
        out += _top_threats(stories, legend)
        widely = sorted((a for a in stories if len(outlets(a)) > 1), key=lambda a: -len(outlets(a)))[:10]
        if widely:
            out += ["### Most widely reported", ""]
            out += [f"- [{_md_escape(a.title)}]({a.link}) — {len(outlets(a))} outlets" for a in widely]
            out.append("")
        cve_counts = Counter(c for a in articles for c in a.cves)
        if cve_counts:
            out += ["### CVEs mentioned", ""]
            out.append(", ".join(f"{c} ({n})" if n > 1 else c for c, n in sorted(cve_counts.items(), key=lambda x: (-x[1], x[0]))))
            out.append("")

    for t in order:
        items = sorted(by_topic[t], key=lambda a: (-a.score, -(a.published.timestamp() if a.published else 0)))
        out += [f"## {name(t)}", ""]
        for a in items:
            out.append(f"### [{_md_escape(a.title)}]({a.link})")
            bits = ([f"**{level_name.get(a.severity, a.severity)} ({a.score})**"] if a.severity else []) + [f"*{a.source}*", _date(a)]
            others = [name(x) for x in a.topics if x != t and x != "general"]
            if others:
                bits.append("Also: " + ", ".join(others))
            if a.industries:
                bits.append("Sectors: " + ", ".join(name(i) for i in a.industries))
            if a.locations:
                bits.append("Locations: " + ", ".join(name(l) for l in a.locations))
            out.append(" · ".join(bits) + "  ")
            if a.related:
                covered = ", ".join(f"[{_md_escape(r.source)}]({r.link})" for r in a.related)
                out.append(f"Also covered by: {covered}  ")
            elif a.also_reported_by:
                out.append(f"Also reported by: {', '.join(a.also_reported_by)}  ")
            if a.reasons:
                out.append("Why: " + ", ".join(f"{r['label']} ({r['points']:+d})" for r in a.reasons) + "  ")
            if a.ttps:
                out.append("ATT&CK: " + ", ".join(f"{t} {legend['ttps'].get(t, {}).get('name', '')}".strip() for t in a.ttps) + "  ")
            cves = sorted({c for x in [a, *a.related] for c in x.cves})
            if cves:
                out.append(f"CVEs: {', '.join(cves)}  ")
            if a.summary:
                out += ["", a.summary]
            out.append("")
    if not articles:
        out.append("_No articles matched the selected filters._\n")
    return "\n".join(out)


def _top_threats(stories: list[Article], legend: dict) -> list[str]:
    """The bulletin's lead: the most severe stories of the period, then the ATT&CK techniques seen most."""
    level_name = {l["id"]: l["name"] for l in legend["levels"]}
    ranked = sorted((a for a in stories if a.score), key=lambda a: (-a.score, -(a.published.timestamp() if a.published else 0)))
    top = [a for a in ranked if a.severity in ("critical", "high")][:15] or ranked[:5]
    out = []
    if top:
        out += ["### Top threats", "", "Ranked by severity and importance, not date (see *How stories are scored* in the README).", "",
                "| # | Severity | Story | Why |", "|---:|---|---|---|"]
        for i, a in enumerate(top, 1):
            why = ", ".join(r["label"] for r in a.reasons if r["points"] > 0)[:160]
            title = _md_escape(a.title).replace("|", "\\|")
            out.append(f"| {i} | {level_name.get(a.severity, a.severity)} ({a.score}) | [{title}]({a.link}) | {why} |")
        out.append("")
    ttps = Counter(t for a in stories for t in a.ttps)
    if ttps:
        out += ["### ATT&CK techniques seen", "", "| Technique | Tactic | Stories |", "|---|---|---:|"]
        for t, n in ttps.most_common(12):
            info = legend["ttps"].get(t, {})
            out.append(f"| {t} {info.get('name', '')} | {info.get('tactic', '')} | {n} |")
        out.append("")
    return out


def _anchor(text: str) -> str:
    return re.sub(r"[^a-z0-9 -]", "", text.lower()).replace(" ", "-")


def to_json(articles: list[Article], taxonomy: Taxonomy, meta: dict) -> str:
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "since": meta["since"].isoformat() if meta.get("since") else None,
        "until": meta["until"].isoformat() if meta.get("until") else None,
        "errors": meta.get("errors", {}),
        "articles": [a.to_dict() for a in articles],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def to_csv(articles: list[Article], taxonomy: Taxonomy, meta: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["published", "source", "title", "link", "primary_category", "categories", "industries", "locations", "cves", "also_reported_by", "story", "severity", "score", "why", "attack_techniques", "summary"])
    for a in articles:
        w.writerow([
            a.published.isoformat() if a.published else "",
            a.source,
            a.title,
            a.link,
            taxonomy.name_of(a.primary_topic),
            "; ".join(taxonomy.name_of(t) for t in a.topics),
            "; ".join(taxonomy.name_of(i) for i in a.industries),
            "; ".join(taxonomy.name_of(l) for l in a.locations),
            "; ".join(a.cves),
            "; ".join(a.also_reported_by),
            a.story or a.link,
            a.severity,
            a.score,
            "; ".join(f"{r['label']} ({r['points']:+d})" for r in a.reasons),
            "; ".join(a.ttps),
            a.summary,
        ])
    return buf.getvalue()


def to_html(articles: list[Article], taxonomy: Taxonomy, meta: dict) -> str:
    data = {
        "period": _period(meta),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "topics": [{"id": t.id, "name": t.name, "color": t.color} for t in taxonomy.topics]
        + [{"id": "general", "name": "General", "color": "#94a3b8"}],
        "industries": [{"id": i.id, "name": i.name} for i in taxonomy.industries],
        "locations": [{"id": l.id, "name": l.name} for l in taxonomy.locations],
        "site": {k: meta.get("site", {}).get(k, "") for k in ("subscribe_url", "subscribe_email", "briefing_title")},
        "scoring": (meta.get("scorer") or load_scorer()).legend(),
        "articles": [a.to_dict() for a in articles],
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    data_dir = Path(__file__).parent / "data"
    template = (data_dir / "report.html").read_text(encoding="utf-8")
    logo = (data_dir / "logo.svg").read_text(encoding="utf-8").strip()
    return template.replace("<!--__LOGO__-->", logo).replace("/*__DATA__*/null", blob)


RENDERERS = {"md": to_markdown, "html": to_html, "json": to_json, "csv": to_csv}


def render(fmt: str, articles: list[Article], taxonomy: Taxonomy, meta: dict) -> str:
    return RENDERERS[fmt](articles, taxonomy, meta)
