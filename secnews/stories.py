"""Group articles about the same event (e.g. 15 outlets covering one Ivanti zero-day) into stories.

Articles are compared on the words in their headline and summary, weighted by how rare each word is across
everything collected, so shared names like "Ivanti" or "Salt Typhoon" count for much more than "hackers" or
"vulnerability". A shared CVE ID links two articles outright, and articles naming different CVEs are never
grouped. Groups only grow within a few days of their first article.

Nothing is merged or deleted: every article gets a ``story`` id (the link of the article chosen to represent
its story) and the views decide how to show the group.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import timedelta

from .models import Article

WINDOW = timedelta(days=3)
THRESHOLD = 0.3  # similarity (0-1) needed to join a story
MERGE_THRESHOLD = 0.3  # similarity needed to join two groups (see _merge_similar)
NAME_SHARE = 0.015  # a headline word in at most this share of all articles (or 6) counts as a distinctive name
HOUSE_SHARE = 0.15  # words in at least this share of one outlet's articles are its boilerplate (see _house_words)
MAX_CVES = 5  # round-ups (Patch Tuesday, weekly recaps) list many CVEs; don't let them link stories

_STOP = set("""
a an the and or of in on to for with by from at as is are was were be been being has have had its it this that these
those after before over under into out up down off about against new says said say report reports reported via than
then also more most other some such no not only own same so too very can will just should now how what when where which
who whom why all any both each few amid while during per across among within without week weeks day days today
yesterday year years month months latest update updates updated first last next one two three four five six
""".split())
# Everyday security vocabulary: shared by unrelated stories, so it says little about whether two are the same one.
_STOP |= set("""
security cyber cybersecurity attack attacker hack hacker hacking threat actor exploit vulnerability vuln flaw bug
issue patch fix critical severe severity high warn warning alert malware malicious deploy target campaign group
gang researcher research company firm user customer data system network organization organisation incident
breach compromise access code remote execution zero day zero-day exploitation active actively wild release released
""".split())
# Round-ups mention many stories at once, so they're never grouped with any one of them.
ROUNDUP = re.compile(r"\b(recap|round-?up|week in review|weekly update|this week in|in other news|newsletter|digest|"
                     r"news brief|stormcast|podcast)\b", re.I)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9.]*[a-z0-9]|[a-z0-9]")


def _stem(w: str) -> str:
    """Crude English stemming so exploited/exploiting/exploits all count as one word."""
    if len(w) > 5 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 5 and w.endswith(("ches", "shes", "sses", "xes")):
        return w[:-2]  # patches -> patch
    if len(w) > 4 and w.endswith("ed") and not w.endswith("eed"):
        return w[:-2]
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


_STOP_STEMS = {_stem(w) for w in _STOP}


def tokens(text: str) -> set[str]:
    out = set()
    for w in _TOKEN.findall(text.lower()):
        w = w.strip(".")
        if len(w) < 2 or w in _STOP or w.isdigit() and len(w) < 4:
            continue
        w = _stem(w)
        if w not in _STOP and w not in _STOP_STEMS:
            out.add(w)
    return out


def _words(a: Article, house: dict[str, set[str]]) -> tuple[set[str], set[str]]:
    """(headline words, summary words), without the outlet's own boilerplate."""
    skip = house.get(a.source, set())
    return tokens(a.title) - skip, tokens(a.summary[:400]) - skip


def _house_words(articles: list[Article]) -> dict[str, set[str]]:
    """Words an outlet repeats across many of its own articles (its name, sign-offs, recurring phrases such as
    "Related:" lines). They make that outlet's unrelated articles look alike, so they're ignored for it."""
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[str, int] = defaultdict(int)
    for a in articles:
        totals[a.source] += 1
        for w in tokens(a.title) | tokens(a.summary[:400]):
            counts[a.source][w] += 1
    return {src: {w for w, c in words.items() if c >= 4 and c >= HOUSE_SHARE * totals[src]}
            for src, words in counts.items() if totals[src] >= 8}


def _vector(a: Article, idf: dict[str, float], house: dict[str, set[str]]) -> tuple[dict[str, float], float]:
    """Headline words count double; the summary's opening adds context."""
    title, summary = _words(a, house)
    vec: dict[str, float] = {}
    for w in summary:
        vec[w] = idf.get(w, 0.0)
    for w in title:
        vec[w] = 2 * idf.get(w, 0.0)
    return vec, math.sqrt(sum(v * v for v in vec.values())) or 1.0


def _cosine(x, y) -> float:
    (vx, nx), (vy, ny) = x, y
    if len(vx) > len(vy):
        vx, vy = vy, vx
    return sum(v * vy[w] for w, v in vx.items() if w in vy) / (nx * ny)


def _cves(a: Article) -> set[str]:
    return set(a.cves) if 0 < len(a.cves) <= MAX_CVES else set()


def _quality(a: Article) -> tuple:
    """Which article represents a story: one with a picture and a real summary, from a news outlet, earliest."""
    return (bool(a.image), min(len(a.summary), 300) // 100, a.source_type == "news", -(a.published.timestamp()))


def group_stories(articles: list[Article], threshold: float = THRESHOLD) -> int:
    """Set ``story`` on every article; returns how many stories have more than one article."""
    dated = sorted((a for a in articles if a.published), key=lambda a: a.published)
    for a in articles:
        a.story = a.link
    if not dated:
        return 0
    house = _house_words(dated)
    df: dict[str, int] = defaultdict(int)
    for a in dated:
        title, summary = _words(a, house)
        for w in title | summary:
            df[w] += 1
    n = len(dated)
    # Words in almost every article ("security", "attack") are worth ~nothing; a name seen once or twice a lot.
    idf = {w: max(0.1, math.log((n + 1) / (c + 0.5)) - 1.0) for w, c in df.items()}

    # Names like "Typhoon" or "Jaguar" are rare overall; "Microsoft" or "Windows" are in too many stories to count.
    name_df = max(6, int(n * NAME_SHARE))

    groups: list[dict] = []  # {"start", "members": [(article, vector, names)], "cves", "centroid", "norm", "words"}
    by_word: dict[str, list[int]] = defaultdict(list)  # word -> groups whose headlines use it, oldest first
    by_cve: dict[str, list[int]] = defaultdict(list)
    for a in dated:
        if ROUNDUP.search(a.title):
            continue  # stays a story of its own
        vec = _vector(a, idf, house)
        cves = _cves(a)
        title_words = _words(a, house)[0]
        title_names = {w for w in title_words if df.get(w, 0) <= name_df}
        candidates = set()
        for w in title_words:
            for gi in reversed(by_word[w]):  # most recently extended first
                age = a.published - groups[gi]["start"]
                if age > 2 * WINDOW:  # a group only grows for WINDOW, so everything before this is older still
                    break
                if age <= WINDOW:
                    candidates.add(gi)
        candidates |= {g for c in cves for g in by_cve[c] if a.published - groups[g]["start"] <= WINDOW}
        best, best_score = None, threshold
        for gi in candidates:
            g = groups[gi]
            if cves and g["cves"] and not cves & g["cves"]:
                continue  # similar wording, different vulnerabilities: separate stories
            if cves & g["cves"]:
                score = 1.0
            else:  # like one of its recent articles, or like the story as a whole
                recent = g["members"][-8:]
                score = max([_cosine(vec, m[1]) for m in recent] + [_cosine(vec, (g["centroid"], g["norm"]))])
                # Headlines sharing two distinctive names ("Salt Typhoon", "Jaguar Land Rover") need less else in common.
                if score >= threshold / 2 and any(len(title_names & m[2]) >= 2 for m in recent):
                    score = max(score, threshold)
            if score >= best_score:
                best, best_score = gi, score
        if best is None:
            best = len(groups)
            groups.append({"start": a.published, "members": [], "cves": set(), "centroid": {}, "norm": 1.0,
                           "names": set()})
        g = groups[best]
        g["members"].append((a, vec, title_names))
        for w, v in vec[0].items():
            g["centroid"][w] = g["centroid"].get(w, 0.0) + v
        g["norm"] = math.sqrt(sum(v * v for v in g["centroid"].values())) or 1.0
        g["cves"] |= cves
        g["names"] |= title_names
        for w in title_words:
            if best not in by_word[w][-20:]:
                by_word[w].append(best)
        for c in cves:
            if best not in by_cve[c]:
                by_cve[c].append(best)

    multi = 0
    for members in _merge_similar(groups):
        if len({m.source for m in members}) < 2:
            continue  # only different outlets covering the same event make a story; one outlet's posts stay apart
        lead = max(members, key=_quality)
        for m in members:
            m.story = lead.link
        multi += 1
    return multi


def _merge_similar(groups: list[dict]) -> list[list[Article]]:
    """Coverage often starts as two threads (the advisory, then 'hackers exploit X') before articles that bridge
    them arrive. Join groups that share a distinctive headline name, read alike as a whole, started within the
    window and don't name different CVEs. Returns each final story's articles."""
    parent = list(range(len(groups)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_name: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(groups):  # groups are in order of their first article
        seen = set()
        for w in g["names"]:
            for j in reversed(by_name[w]):
                if g["start"] - groups[j]["start"] > WINDOW:
                    break
                if j in seen:
                    continue
                seen.add(j)
                h = groups[j]
                if g["cves"] and h["cves"] and not g["cves"] & h["cves"]:
                    continue
                if _cosine((g["centroid"], g["norm"]), (h["centroid"], h["norm"])) >= MERGE_THRESHOLD:
                    parent[find(i)] = find(j)
            by_name[w].append(i)
    out: dict[int, list[Article]] = defaultdict(list)
    for i, g in enumerate(groups):
        out[find(i)] += [m[0] for m in g["members"]]
    return list(out.values())


def collapse(articles: list[Article]) -> list[tuple[Article, list[Article]]]:
    """One entry per story, in the order of each story's first appearance in ``articles``:
    (the article to show, the others on the same story, newest first)."""
    order: list[str] = []
    members: dict[str, list[Article]] = defaultdict(list)
    for a in articles:
        key = a.story or a.link
        if key not in members:
            order.append(key)
        members[key].append(a)
    out = []
    for key in order:
        group = members[key]
        lead = next((a for a in group if a.link == key), group[0])
        rest = sorted((a for a in group if a is not lead), key=lambda a: a.published.timestamp() if a.published else 0,
                      reverse=True)
        out.append((lead, rest))
    return out


def outlets(lead: Article, related: list[Article] | None = None) -> list[str]:
    """Every outlet that covered the story, lead's first."""
    related = getattr(lead, "related", []) if related is None else related
    seen = [lead.source]
    for name in lead.also_reported_by + [s for a in related for s in [a.source, *a.also_reported_by]]:
        if name not in seen:
            seen.append(name)
    return seen


def leads(articles: list[Article]) -> list[Article]:
    """One article per story (newest stories first), each with ``.related``: the story's other articles."""
    ordered = sorted(articles, key=lambda a: a.published.timestamp() if a.published else float("-inf"), reverse=True)
    out = []
    for lead, rest in collapse(ordered):
        lead.related = rest
        out.append(lead)
    return out


def latest(a: Article):
    """When the story last had new coverage."""
    dates = [x.published for x in [a, *getattr(a, "related", [])] if x.published]
    return max(dates) if dates else None
