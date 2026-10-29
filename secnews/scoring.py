"""Severity / importance scoring for a threat-intelligence bulletin.

Every story (an article, or a group of outlets' articles on the same event; see stories.py) gets a score from
0 to 100, a level (Critical / High / Medium / Low), the reasons behind the score, and the MITRE ATT&CK techniques
its coverage mentions. Evidence is pooled across the story's articles: one outlet may give the CVSS score and
another report that it's being exploited. The criteria live in data/scoring.json.

The score deliberately ignores how recent a story is, so a severe story from two days ago still outranks
today's minor ones in the "Top threats" view.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .classify import _compile
from .models import Article
from .stories import outlets

DATA = Path(__file__).parent / "data"

CVSS_RE = re.compile(
    r"\bCVSS(?:\s*v?\d(?:\.\d)?)?(?:\s+base)?(?:\s+score)?(?:\s+(?:of|is|rated|at))?\s*[:=]?\s*\(?(\d{1,2}(?:\.\d)?)(?!\d)", re.I)
_MULT = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9}
_NOUNS = (r"people|persons|individuals|customers|users|records|accounts|patients|members|citizens|residents|employees|"
          r"devices|servers|routers|firewalls|websites|sites|organi[sz]ations|companies|businesses|victims|endpoints|"
          r"phones|cards|subscribers|students|voters|households|downloads|installs|machines|systems|instances|repos|"
          r"repositories|stores|hospitals|schools")
SCALE_RE = re.compile(
    rf"(?<![\w$€£.])(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|mn|bn|[kmb])?\+?\s+(?:[\w'’-]+\s+){{0,3}}?(?:{_NOUNS})\b",
    re.I)
MONEY_RE = re.compile(r"(?:\$|€|£|USD\s?|EUR\s?|GBP\s?)(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|mn|bn|[kmb])?\b", re.I)
LOSS_RE = re.compile(r"\b(stole|stolen|steal|theft|thefts|heist|loss|losses|lost|ransom|ransoms|drained|fraud|scam|scams|"
                     r"laundered|laundering|extort|extorted|extortion|siphoned|looted)\b", re.I)


def _number(num: str, mult: str | None) -> float:
    try:
        value = float(num.replace(",", ""))
    except ValueError:
        return 0.0
    return value * _MULT.get((mult or "").lower(), 1)


def _regex(keywords: list[str], patterns: list[str]) -> re.Pattern | None:
    """One regex for a signal: its keywords (matched like categories.json) plus any raw patterns."""
    parts = []
    for _, pat, _ in _compile(keywords):
        parts.append(f"(?:{pat.pattern})" if pat.flags & re.IGNORECASE else f"(?-i:{pat.pattern})")
    parts += [f"(?:{p})" for p in patterns]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


@dataclass
class Signal:
    id: str
    label: str
    points: int = 0
    regex: re.Pattern | None = None
    computed: str = ""
    params: dict = field(default_factory=dict)
    unless: str = ""


@dataclass
class Group:
    id: str
    name: str
    max: int
    signals: list[Signal]


@dataclass
class Technique:
    id: str
    name: str
    tactic: str
    regex: re.Pattern | None


class Scorer:
    def __init__(self, path: Path | str | None = None):
        data = json.loads(Path(path or DATA / "scoring.json").read_text(encoding="utf-8"))
        self.levels = sorted(data["levels"], key=lambda l: -l["min"])
        self.groups = []
        for g in data["groups"]:
            sigs = []
            for s in g["signals"]:
                params = {k: v for k, v in s.items() if k not in ("id", "label", "points", "keywords", "patterns", "computed",
                                                                  "unless") and not k.startswith("_")}
                sigs.append(Signal(s["id"], s["label"], s.get("points", 0), _regex(s.get("keywords", []), s.get("patterns", [])),
                                   s.get("computed", ""), params, s.get("unless", "")))
            self.groups.append(Group(g["id"], g["name"], g["max"], sigs))
        self.negation = re.compile("|".join(data.get("negations", [])), re.I) if data.get("negations") else None
        self.not_threat = re.compile("|".join(data.get("not_threats", [])), re.I) if data.get("not_threats") else None
        self.not_threat_cap = data.get("not_threat_cap", 20)
        self.techniques = [Technique(t["id"], t["name"], t["tactic"], _regex(t.get("keywords", []), t.get("patterns", [])))
                           for t in data.get("ttps", [])]
        self.technique_names = {t.id: t.name for t in self.techniques}
        self.technique_tactics = {t.id: t.tactic for t in self.techniques}

    def level_of(self, score: int) -> dict:
        return next(l for l in self.levels if score >= l["min"])

    # ------------------------------------------------------------------ one story

    def _computed(self, sig: Signal, story: list[Article], texts: list[str]) -> int:
        kind = sig.computed
        if kind == "cvss":
            best = max((float(m) for t in texts for m in CVSS_RE.findall(t) if 0 < float(m) <= 10), default=0.0)
            return sig.points if best >= sig.params.get("min_cvss", 11) else 0
        if kind in ("scale", "money"):
            found = 0.0
            for t in texts:
                if kind == "scale":
                    found = max([found] + [_number(n, m) for n, m in SCALE_RE.findall(t)])
                else:
                    for m in MONEY_RE.finditer(t):
                        near = t[max(0, m.start() - 80): m.end() + 80]
                        if LOSS_RE.search(near):
                            found = max(found, _number(m.group(1), m.group(2)))
            return next((pts for minimum, pts in sig.params.get("tiers", []) if found >= minimum), 0)
        if kind == "coverage":
            n = len(outlets(story[0], story[1:]))
            return next((pts for minimum, pts in sig.params.get("tiers", []) if n >= minimum), 0)
        if kind == "industries":
            wanted = set(sig.params.get("industries", []))
            return sig.points if any(wanted & set(a.industries) for a in story) else 0
        if kind == "topics":
            wanted = set(sig.params.get("topics", []))
            return sig.points if any(wanted & set(a.topics) for a in story) else 0
        if kind == "sources":
            wanted = set(sig.params.get("sources", []))
            return sig.points if any(a.source in wanted for a in story) else 0
        return 0

    def score_story(self, story: list[Article]) -> tuple[int, list[dict], list[str]]:
        """(score, reasons [{"label", "points"}], ATT&CK technique ids) for one story's articles."""
        texts = [f"{a.title}. {a.summary}" for a in story]
        # "No evidence of exploitation" mustn't count as exploitation.
        confirmed = [t for t in texts if not (self.negation and self.negation.search(t))]
        reasons, total = [], 0
        for g in self.groups:
            found: dict[str, int] = {}
            for sig in g.signals:
                if sig.unless and sig.unless in found:
                    continue
                pool = confirmed if g.id == "exploitation" else texts
                pts = self._computed(sig, story, texts) if sig.computed else 0
                if not pts and sig.regex and any(sig.regex.search(t) for t in pool):
                    pts = sig.points
                if pts:
                    found[sig.id] = pts
            room = g.max
            for sig in sorted(g.signals, key=lambda s: -found.get(s.id, 0)):
                pts = min(found.get(sig.id, 0), room)
                if pts > 0:
                    reasons.append({"group": g.name, "label": sig.label, "points": pts})
                    room -= pts
            total += g.max - room
        score = min(100, total)
        titles = [a.title for a in story]
        if self.not_threat and sum(bool(self.not_threat.search(t)) for t in titles) * 2 > len(titles):
            if score > self.not_threat_cap:
                reasons.append({"group": "", "label": "Not a threat report (business news, event or guide)",
                                "points": self.not_threat_cap - score})
                score = self.not_threat_cap
        ttps = [t.id for t in self.techniques if t.regex and any(t.regex.search(x) for x in texts)]
        reasons.sort(key=lambda r: -r["points"])
        return score, reasons, ttps

    # ------------------------------------------------------------------ everything

    def score_all(self, articles: list[Article]) -> dict[str, int]:
        """Score every story and store the result on each of its articles. Returns how many stories are at each level."""
        stories: dict[str, list[Article]] = defaultdict(list)
        for a in articles:
            stories[a.story or a.link].append(a)
        counts: dict[str, int] = defaultdict(int)
        for key, story in stories.items():
            story.sort(key=lambda a: a.link != key)  # the story's representative article first
            score, reasons, ttps = self.score_story(story)
            level = self.level_of(score)["id"]
            counts[level] += 1
            for a in story:
                a.score, a.severity, a.reasons, a.ttps = score, level, reasons, ttps
        return dict(counts)

    def legend(self) -> dict:
        """What the site and reports need to explain scores."""
        return {
            "levels": [{"id": l["id"], "name": l["name"], "min": l["min"], "color": l.get("color", "")} for l in self.levels],
            "groups": [{"name": g.name, "max": g.max, "signals": [s.label for s in g.signals]} for g in self.groups],
            "ttps": {t.id: {"name": t.name, "tactic": t.tactic} for t in self.techniques},
        }


def load_scorer(path: Path | str | None = None) -> Scorer:
    return Scorer(path)
