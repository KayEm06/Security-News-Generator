import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from secnews.classify import Classifier, deduplicate, filter_articles, normalize_url, resolve_ids
from secnews.cli import main
from secnews.config import Source, load_sources, load_taxonomy
from secnews.feeds import fetch_source, parse_date, parse_feed
from secnews.models import Article

FIX = Path(__file__).parent / "fixtures"
TAX = load_taxonomy()


def parse(name, src="Test"):
    return parse_feed((FIX / name).read_bytes(), Source(src, f"file://{FIX / name}"))


def tagged(title, summary=""):
    return Classifier(TAX).classify(Article(title=title, link="https://x.test/a", source="T", summary=summary))


class ParsingTests(unittest.TestCase):
    def test_rss(self):
        arts = parse("rss.xml")
        self.assertEqual(len(arts), 3)
        a = arts[0]
        self.assertIn("Ivanti", a.title)
        self.assertEqual(a.published, datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc))
        self.assertNotIn("<b>", a.summary)
        self.assertNotIn("alert", a.summary)
        self.assertEqual(a.feed_tags, ["Vulnerability"])
        self.assertIn("Europol", arts[1].summary)  # content:encoded fallback
        self.assertEqual(arts[1].published.day, 3)  # dc:date

    def test_atom(self):
        arts = parse("atom.xml")
        self.assertEqual(arts[0].link, "https://research.example.org/ivanti")  # rel=alternate wins
        self.assertEqual(arts[1].published, datetime(2026, 10, 4, 6, 15, tzinfo=timezone.utc))
        self.assertIn("manufacturing", arts[1].summary)
        self.assertEqual(arts[1].feed_tags, ["Ransomware"])

    def test_rdf(self):
        arts = parse("rdf.xml")
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0].link, "https://isc.example.net/diary/1")

    def test_malformed_feed_is_repaired(self):
        arts = parse("broken.xml")
        self.assertEqual(arts[0].title, "AT&T and R&D data leaked")
        self.assertEqual(arts[0].link, "https://broken.example.com/a?x=1&y=2")

    def test_html_page_reports_error(self):
        r = fetch_source(Source("Blocked", (FIX / "blocked.html").as_uri()))
        self.assertEqual(r.articles, [])
        self.assertIn("HTML page", r.error)

    def test_dates(self):
        self.assertEqual(parse_date("2026-10-04").tzinfo, timezone.utc)
        self.assertIsNone(parse_date("not a date"))


class ClassifierTests(unittest.TestCase):
    def test_topics_and_industries(self):
        a = tagged("Hackers exploit critical zero-day to breach hospitals", "Attackers hit CVE-2026-1234 at a health system.")
        self.assertEqual(a.topics[0], "vulnerabilities")
        self.assertIn("data-breaches", a.topics)
        self.assertIn("healthcare", a.industries)
        self.assertEqual(a.cves, ["CVE-2026-1234"])

    def test_fraud(self):
        a = tagged("Police arrest members of pig butchering crypto scam ring")
        self.assertEqual(a.topics[0], "fraud-cybercrime")

    def test_hyphenated_compound_matches(self):
        self.assertIn("ransomware", tagged("New ransomware-as-a-service operation emerges").topics)

    def test_short_acronyms_are_case_sensitive(self):
        self.assertNotIn("malware", tagged("A rat in the server room").topics)
        self.assertNotIn("ai-security", tagged("Ai Weiwei exhibition opens").topics)

    def test_single_weak_industry_mention_is_ignored(self):
        self.assertEqual(tagged("Patch now", "A school reported it.").industries, [])

    def test_charity_industry(self):
        a = tagged("Fake donation sites target Red Cross supporters", "Scammers posed as a charity.")
        self.assertIn("charity", a.industries)
        self.assertIn("fraud-cybercrime", a.topics)

    def test_locations(self):
        a = tagged("UK charity hit by ransomware", "The British non-profit told the ICO.")
        self.assertEqual(a.locations, ["uk-ireland"])
        self.assertIn("europe", tagged("German manufacturer recovers from cyberattack").locations)
        self.assertIn("north-america", tagged("U.S. hospitals warned", "").locations)
        self.assertEqual(tagged("Patch now", "Mentions Europe once.").locations, [])  # one weak hit isn't enough
        self.assertEqual(tagged("North Korean hackers steal crypto").locations, ["asia-pacific"])
        self.assertNotIn("north-america", tagged("Hackers target Latin America banks").locations)

    def test_source_forced_location(self):
        c = Classifier(TAX, [Source("NCSC", "x", locations=["uk-ireland"])])
        self.assertEqual(c.classify(Article(title="New guidance", link="https://x", source="NCSC")).locations, ["uk-ireland"])

    def test_unmatched_is_general(self):
        self.assertEqual(tagged("Weekly podcast episode").topics, ["general"])

    def test_source_forced_category(self):
        c = Classifier(TAX, [Source("DB", "x", categories=["data-breaches"])])
        a = c.classify(Article(title="Weekly roundup", link="https://x", source="DB"))
        self.assertEqual(a.topics, ["data-breaches"])


class DedupFilterTests(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(
            normalize_url("http://www.Example.com/a/?utm_source=rss&id=3"),
            normalize_url("https://example.com/a?id=3"),
        )

    def test_merge_same_story(self):
        arts = parse("rss.xml", "Outlet A") + parse("atom.xml", "Outlet B")
        for a in arts:
            Classifier(TAX).classify(a)
        merged = deduplicate(arts)
        self.assertEqual(len(merged), 4)
        ivanti = next(a for a in merged if "Ivanti" in a.title)
        self.assertEqual(ivanti.source, "Outlet A")  # earliest kept
        self.assertEqual(ivanti.also_reported_by, ["Outlet B"])
        self.assertEqual(ivanti.cves, ["CVE-2026-1234", "CVE-2026-56789"])

    def test_filters(self):
        arts = [Classifier(TAX).classify(a) for a in parse("rss.xml") + parse("atom.xml", "Lab")]
        since = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(len(filter_articles(arts, since=since)), 4)
        fraud = filter_articles(arts, since=since, topics={"fraud-cybercrime"})
        self.assertEqual([a.title[:6] for a in fraud], ["Police"])
        self.assertEqual(len(filter_articles(arts, industries={"manufacturing"})), 1)
        self.assertEqual(len(filter_articles(arts, sources=["lab"])), 2)
        self.assertEqual(len(filter_articles(arts, cve_only=True)), 2)
        self.assertEqual(len(filter_articles(arts, search="lockbit")), 1)

    def test_resolve_ids(self):
        self.assertEqual(resolve_ids(["fraud", "Ransomware & Extortion"], TAX.topics, "topic"), {"fraud-cybercrime", "ransomware"})
        self.assertEqual(resolve_ids(["vuln,phish"], TAX.topics, "topic"), {"vulnerabilities", "phishing"})
        self.assertEqual(resolve_ids(["health"], TAX.industries, "industry"), {"healthcare"})
        self.assertEqual(resolve_ids(["uk", "Germany"], TAX.locations, "location"), {"uk-ireland", "europe"})
        self.assertEqual(resolve_ids(["us"], TAX.locations, "location"), {"north-america"})
        with self.assertRaises(ValueError):
            resolve_ids(["nonsense"], TAX.topics, "topic")


class ConfigTests(unittest.TestCase):
    def test_bundled_config_is_consistent(self):
        sources = load_sources()
        self.assertGreaterEqual(len(sources), 25)
        topic_ids = {t.id for t in TAX.topics}
        location_ids = {l.id for l in TAX.locations}
        for s in sources:
            self.assertTrue(s.url.startswith("https://"), s.name)
            self.assertTrue(set(s.categories) <= topic_ids, s.name)
            self.assertTrue(set(s.locations) <= location_ids, s.name)
        all_ids = [c.id for c in TAX.topics + TAX.industries + TAX.locations]
        self.assertEqual(len(all_ids), len(set(all_ids)))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        feeds = [
            {"name": "Outlet A", "url": (FIX / "rss.xml").as_uri()},
            {"name": "Lab B", "url": (FIX / "atom.xml").as_uri(), "type": "vendor-research"},
            {"name": "ISC", "url": (FIX / "rdf.xml").as_uri(), "type": "blog"},
            {"name": "Blocked", "url": (FIX / "blocked.html").as_uri()},
        ]
        self.sources = self.tmp / "sources.json"
        self.sources.write_text(json.dumps({"sources": feeds}))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sources-file", str(self.sources), *args])
        return code, out.getvalue(), err.getvalue()

    def test_all_formats(self):
        common = ["fetch", "--since", "2026-09-30", "--until", "2026-10-07"]
        for ext in ("md", "html", "json", "csv"):
            out = self.tmp / f"r.{ext}"
            code, _, err = self.run_cli(*common, "-o", str(out))
            self.assertEqual(code, 0, err)
            self.assertIn("✗ Blocked", err)
            text = out.read_text()
            self.assertIn("Ivanti", text)
            self.assertNotIn("podcast", text)
        html = (self.tmp / "r.html").read_text()
        self.assertNotIn("/*__DATA__*/null", html)
        self.assertNotIn("<script>alert", html)
        md = (self.tmp / "r.md").read_text()
        self.assertIn("## Vulnerabilities & Exploits", md)
        self.assertIn("Also reported by: Lab B", md)
        data = json.loads((self.tmp / "r.json").read_text())
        self.assertEqual(len(data["articles"]), 4)
        self.assertIn("Blocked", data["errors"])

    def test_category_filter_and_reuse_json(self):
        saved = self.tmp / "all.json"
        self.run_cli("fetch", "--since", "2026-09-30", "-o", str(saved))
        code, out, _ = self.run_cli("fetch", "--input", str(saved), "--since", "2026-09-30", "-c", "phishing", "-i", "education", "-q")
        self.assertEqual(code, 0)
        self.assertIn("QR codes", out)
        self.assertNotIn("Ivanti", out)
        self.assertIn("**Filters:** categories: Phishing & Social Engineering; industries: Education", out)

    def test_source_type_filter(self):
        _, out, _ = self.run_cli("fetch", "--since", "2026-09-30", "--type", "vendor-research", "-q")
        self.assertIn("LockBit", out)
        self.assertNotIn("Police arrest", out)

    def test_location_and_charity_filters(self):
        self.sources.write_text(json.dumps({"sources": [{"name": "UK News", "url": (FIX / "charity.xml").as_uri()}]}))
        common = ["fetch", "--since", "2026-09-30", "-q"]
        _, out, _ = self.run_cli(*common, "-l", "uk", "-i", "charity")
        self.assertIn("UK charity hit by ransomware", out)
        self.assertNotIn("German", out)
        self.assertIn("Locations: UK & Ireland", out)
        _, out, _ = self.run_cli(*common, "-l", "europe")
        self.assertIn("German manufacturer", out)
        self.assertNotIn("UK charity", out)
        csv_path = self.tmp / "loc.csv"
        self.run_cli(*common, "-o", str(csv_path))
        self.assertIn("locations", csv_path.read_text().splitlines()[0])

    def test_bad_category(self):
        code, _, err = self.run_cli("fetch", "-c", "bogus")
        self.assertEqual(code, 2)
        self.assertIn("unknown topic", err)

    def test_list_commands(self):
        self.assertEqual(self.run_cli("categories")[0], 0)
        code, out, _ = self.run_cli("sources")
        self.assertIn("Lab B", out)


if __name__ == "__main__":
    unittest.main()
