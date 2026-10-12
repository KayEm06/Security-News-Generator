import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from secnews.classify import Classifier, deduplicate, filter_articles, normalize_url, resolve_ids
from secnews.cli import main
from secnews.config import Source, load_sources, load_taxonomy
from secnews.feeds import fetch_source, needs_page_lookup, page_description, page_image, parse_date, parse_feed
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

    def test_images(self):
        arts = parse("charity.xml")
        self.assertEqual(arts[0].image, "https://img.example.com/charity.jpg?w=320&h=180")  # media:thumbnail
        self.assertEqual(arts[1].image, "https://img.example.com/factory.png")  # skips 1x1 tracking pixel
        self.assertEqual(parse("rdf.xml")[0].image, "")

    def test_images_in_harder_feed_markup(self):
        feed = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title>Lazy</title><link>https://n.example.com/2026/10/lazy</link>
  <description><![CDATA[<img src="data:image/gif;base64,R0lGOD" data-lazy-src="/wp-content/uploads/lazy.jpg">]]></description></item>
<item><title>Srcset</title><link>https://n.example.com/a</link>
  <content:encoded><![CDATA[<img srcset="https://n.example.com/s.jpg 300w, https://n.example.com/l.jpg 1024w">]]></content:encoded></item>
<item><title>Escaped</title><link>https://n.example.com/b</link>
  <description>&amp;lt;img src="https://n.example.com/esc.jpg"&amp;gt;</description></item>
<item><title>Enclosure</title><link>https://n.example.com/c</link><enclosure url="https://n.example.com/enc.webp" length="0"/></item>
<item><title>Chrome first</title><link>https://n.example.com/d</link>
  <description><![CDATA[<img src="https://n.example.com/site-logo.png"><img src="https://n.example.com/icons/x.png"><img src="https://n.example.com/silicon-chip.jpg">]]></description></item>
</channel></rss>"""
        imgs = [a.image for a in parse_feed(feed, Source("T", "x"))]
        self.assertEqual(imgs, [
            "https://n.example.com/wp-content/uploads/lazy.jpg",  # lazy-load attribute, relative path resolved
            "https://n.example.com/l.jpg",  # largest srcset entry
            "https://n.example.com/esc.jpg",  # double-escaped HTML
            "https://n.example.com/enc.webp",  # untyped enclosure with an image extension
            "https://n.example.com/silicon-chip.jpg",  # logo/icon skipped, "silicon" isn't an icon
        ])

    def test_atom_xhtml_image(self):
        feed = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>X</title>
<link href="https://a.example.org/post"/><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
<p>Text</p><img src="/media/pic.png" width="640"/></div></content></entry></feed>"""
        self.assertEqual(parse_feed(feed, Source("T", "x"))[0].image, "https://a.example.org/media/pic.png")

    def test_page_image(self):
        base = "https://site.example/news/story"
        og = '<head><meta content="https://cdn.example/og.jpg" property="og:image"><meta name="twitter:image" content="https://cdn.example/tw.jpg"></head>'
        self.assertEqual(page_image(og, base), "https://cdn.example/og.jpg")
        self.assertEqual(page_image('<head><meta name="twitter:image" content="/tw.jpg"></head>', base), "https://site.example/tw.jpg")
        self.assertEqual(page_image('<head><link rel="image_src" href="https://cdn.example/s.jpg"></head>', base), "https://cdn.example/s.jpg")
        jsonld = '<head><script type="application/ld+json">{"@type":"NewsArticle","image":{"@type":"ImageObject","url":"https:\\/\\/cdn.example\\/ld.jpg"}}</script></head>'
        self.assertEqual(page_image(jsonld, base), "https://cdn.example/ld.jpg")
        body = '<head></head><body><img src="/logo.png"><article><img src="/a/1x1.gif" width="1"><img src="/a/main.jpg"></article></body>'
        self.assertEqual(page_image(body, base), "https://site.example/a/main.jpg")
        self.assertEqual(page_image("<head><title>none</title></head><body><p>x</p></body>", base), "")

    def test_page_description(self):
        long_meta = "x " * 120
        self.assertEqual(page_description(f'<head><meta property="og:description" content="{long_meta}"></head>'),
                         long_meta.strip())
        page = """<head><meta name="description" content="Short teaser."></head><body>
            <nav><p>Home | News | Subscribe to our newsletter for daily updates on security</p></nav>
            <article><p>By Staff</p>
            <p>Threat actors are exploiting a critical flaw in Atlassian Data Center products to read sensitive files.</p>
            <p>Sign up for our newsletter to get stories like this one in your inbox every morning, free.</p>
            <p>The vendor has released fixes and urges customers to upgrade immediately, according to the advisory.</p>
            </article></body>"""
        self.assertEqual(page_description(page),
                         "Threat actors are exploiting a critical flaw in Atlassian Data Center products to read sensitive "
                         "files. The vendor has released fixes and urges customers to upgrade immediately, according to the advisory.")
        self.assertEqual(page_description('<head><meta name="description" content="Only this."></head><body></body>'), "Only this.")

    def test_backfill_older_pages(self):
        from unittest import mock

        def feed(*ids):
            items = "".join(f"<item><title>Story {i}</title><link>https://w.example/{i}</link></item>" for i in ids)
            return f"<rss><channel>{items}</channel></rss>".encode()

        pages = {"https://w.example/feed/": feed(1, 2), "https://w.example/feed/?paged=2": feed(3, 4),
                 "https://w.example/feed/?paged=3": feed(5)}

        def fake_download(url, timeout, **kw):
            if url not in pages:
                raise OSError("404")
            return pages[url]

        with mock.patch("secnews.feeds._download", fake_download):
            r = fetch_source(Source("WP", "https://w.example/feed/"), backfill=5)  # stops at the 404 on page 4
            self.assertEqual([a.title for a in r.articles], [f"Story {i}" for i in range(1, 6)])
            self.assertEqual(len(fetch_source(Source("WP", "https://w.example/feed/")).articles), 2)  # default: no paging
        # A feed that ignores ?paged= returns page 1 again; we stop instead of looping.
        with mock.patch("secnews.feeds._download", lambda url, timeout, **kw: feed(1, 2)):
            self.assertEqual(len(fetch_source(Source("X", "https://x.example/rss"), backfill=5).articles), 2)

    def test_cisa_kev(self):
        from secnews.feeds import parse_kev
        recent = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
        data = json.dumps({"vulnerabilities": [
            {"cveID": "CVE-2026-1281", "vendorProject": "Ivanti", "product": "Connect Secure", "dateAdded": recent,
             "vulnerabilityName": "Ivanti Connect Secure Authentication Bypass", "shortDescription": "An auth bypass.",
             "requiredAction": "Apply mitigations.", "dueDate": "2026-10-29", "knownRansomwareCampaignUse": "Known"},
            {"cveID": "CVE-2021-44228", "vendorProject": "Apache", "product": "Log4j2", "dateAdded": "2021-12-10",
             "vulnerabilityName": "Log4Shell", "shortDescription": "Old entry."},
        ]}).encode()
        src = Source("CISA KEV", "x", format="cisa-kev", categories=["vulnerabilities"])
        arts = parse_kev(data, src)
        self.assertEqual(len(arts), 1)  # the 2021 entry is too old to be news
        a = Classifier(TAX, [src]).classify(arts[0])
        self.assertIn("CVE-2026-1281", a.title)
        self.assertEqual(a.link, "https://nvd.nist.gov/vuln/detail/CVE-2026-1281")
        self.assertIn("Known to be used in ransomware", a.summary)
        self.assertEqual(a.cves, ["CVE-2026-1281"])
        self.assertIn("vulnerabilities", a.topics)
        self.assertIn("ransomware", a.topics)
        self.assertFalse(needs_page_lookup(a))

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

    def test_plurals_and_phrases(self):
        self.assertIn("energy", tagged("Cyberattack on Polish power plants").industries)  # plural
        self.assertIn("energy", tagged("Hackers breach Israeli water facilities").industries)  # y -> ies
        self.assertIn("government", tagged("Spies target ministries across Asia").industries)
        # a specific phrase in the summary is enough on its own; a lone generic word isn't
        self.assertIn("energy", tagged("Ransomware hits Halliburton", "Operations at the energy company were disrupted.").industries)
        self.assertEqual(tagged("Patch now", "It affects energy use of laptops.").industries, [])
        # noisy words that used to tag Energy
        self.assertNotIn("energy", tagged("Malicious package found in CI pipeline").industries)
        self.assertNotIn("energy", tagged("Crypto drainer abuses fake gas fee prompts").industries)

    def test_source_forced_industry(self):
        c = Classifier(TAX, [Source("HIPAA Journal", "x", industries=["healthcare"])])
        self.assertEqual(c.classify(Article(title="Weekly roundup", link="x", source="HIPAA Journal")).industries, ["healthcare"])

    def test_publisher_tags_count_strongly(self):
        a = Classifier(TAX).classify(Article(title="Weekly roundup", link="x", source="T", feed_tags=["Healthcare"]))
        self.assertIn("healthcare", a.industries)

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

    def test_similar_titles_with_different_cves_stay_separate(self):
        now = datetime.now(timezone.utc)
        a = Article(title="CISA adds CVE-2026-1111 to Known Exploited Vulnerabilities: Ivanti Connect Secure flaw",
                    link="https://n/1", source="CISA", published=now, cves=["CVE-2026-1111"])
        b = Article(title="CISA adds CVE-2026-2222 to Known Exploited Vulnerabilities: Ivanti Connect Secure flaw",
                    link="https://n/2", source="CISA", published=now, cves=["CVE-2026-2222"])
        self.assertEqual(len(deduplicate([a, b])), 2)

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
        self.assertGreaterEqual(len(sources), 60)
        self.assertEqual(len({s.name for s in sources}), len(sources))
        self.assertEqual(len({s.url for s in sources}), len(sources))
        topic_ids = {t.id for t in TAX.topics}
        industry_ids = {i.id for i in TAX.industries}
        location_ids = {l.id for l in TAX.locations}
        for s in sources:
            self.assertTrue(s.url.startswith("https://"), s.name)
            self.assertIn(s.format, ("feed", "cisa-kev"), s.name)
            self.assertIn(s.type, ("news", "specialist", "vendor-research", "government", "blog"), s.name)
            self.assertTrue(set(s.categories) <= topic_ids, s.name)
            self.assertTrue(set(s.industries) <= industry_ids, s.name)
            self.assertTrue(set(s.locations) <= location_ids, s.name)
        all_ids = [c.id for c in TAX.topics + TAX.industries + TAX.locations]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_site_config(self):
        from secnews.config import load_site
        site = load_site()
        self.assertIn("subscribe_url", site)
        self.assertFalse(any(k.startswith("_") for k in site))


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

    def run_cli(self, *args, pages=False, archive=None):
        out, err = io.StringIO(), io.StringIO()
        if args and args[0] in ("fetch", "briefing"):
            if not pages:
                args = (*args, "--no-page-lookup")  # fixture links point at example.com; keep tests offline
            args = (*args, "--archive", str(archive)) if archive else (*args, "--no-archive")
        if args and args[0] == "briefing" and "--sent-log" not in args:
            args = (*args, "--sent-log", str(self.tmp / "sent.json"))  # never touch ~/.secnews in tests
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sources-file", str(self.sources), *args])
        return code, out.getvalue(), err.getvalue()

    def test_archive_keeps_articles_between_runs(self):
        archive = self.tmp / "archive.json"
        self.sources.write_text(json.dumps({"sources": [{"name": "Outlet A", "url": (FIX / "rss.xml").as_uri()}]}))
        self.run_cli("fetch", "--since", "2026-09-30", "--keep-days", "36500", "-q", archive=archive)
        saved = json.loads(archive.read_text())["articles"]
        self.assertIn("Police arrest members of pig butchering crypto scam ring", [a["title"] for a in saved])

        # Next week the article has dropped out of Outlet A's feed, but the archive still has it.
        self.sources.write_text(json.dumps({"sources": [{"name": "Lab B", "url": (FIX / "atom.xml").as_uri()}]}))
        out = self.tmp / "r.json"
        _, _, err = self.run_cli("fetch", "--since", "2026-09-30", "--keep-days", "36500", "-o", str(out), archive=archive)
        self.assertIn("Merged", err)
        titles = [a["title"] for a in json.loads(out.read_text())["articles"]]
        self.assertIn("Police arrest members of pig butchering crypto scam ring", titles)
        self.assertIn("LockBit affiliate deploys new ransomware-as-a-service encryptor against manufacturers", titles)
        self.assertEqual(len(titles), len(set(titles)))  # the re-fetched Ivanti story isn't duplicated
        ivanti = next(a for a in json.loads(out.read_text())["articles"] if "Ivanti" in a["title"])
        self.assertEqual(ivanti["also_reported_by"], ["Lab B"])

    def test_archive_prunes_old_articles(self):
        from secnews.archive import load_archive, save_archive
        now = datetime.now(timezone.utc)
        arts = [Article(title=t, link=f"https://x/{t}", source="S", published=now - timedelta(days=d))
                for t, d in (("new", 1), ("month", 40), ("old", 120))] + [Article(title="undated", link="https://x/u", source="S")]
        path = self.tmp / "a.json"
        self.assertEqual(save_archive(path, arts, keep_days=90), 2)
        self.assertEqual([a.title for a in load_archive(path)], ["new", "month"])
        self.assertEqual(load_archive(self.tmp / "missing.json"), [])

    def test_page_lookup(self):
        desc = ("Attackers breached the council's network and encrypted file servers, forcing staff back to pen "
                "and paper while services for residents were disrupted across the borough for most of the week.")
        page = self.tmp / "article.html"
        page.write_text(f'<html><head><meta property="og:image" content="https://cdn.example/hero.jpg">'
                        f'<meta name="description" content="{desc}"></head><body></body></html>')
        feed = self.tmp / "feed.xml"
        feed.write_text(f"""<rss version="2.0"><channel><item><title>Ransomware hits council</title>
            <link>{page.as_uri()}</link><pubDate>Mon, 05 Oct 2026 09:00:00 +0000</pubDate>
            <description>No picture in the feed.</description></item></channel></rss>""")
        self.sources.write_text(json.dumps({"sources": [{"name": "Local", "url": feed.as_uri()}]}))
        out = self.tmp / "r.json"
        code, _, err = self.run_cli("fetch", "--since", "2026-09-30", "-o", str(out), pages=True)
        self.assertEqual(code, 0, err)
        self.assertIn("found 1 pictures, expanded 1 summaries", err)
        art = json.loads(out.read_text())["articles"][0]
        self.assertEqual(art["image"], "https://cdn.example/hero.jpg")
        self.assertEqual(art["summary"], desc)

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

    def _recent_feed(self):
        now = datetime.now(timezone.utc)
        items = "".join(
            f"<item><title>{t}</title><link>https://n.example/{i}</link><pubDate>{(now - timedelta(hours=h)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"<description>{t} — details of the incident and what defenders should do next.</description></item>"
            for i, (t, h) in enumerate([("Ransomware hits hospital chain", 2), ("Critical Fortinet flaw CVE-2026-24851 patched", 5),
                                        ("Old story from last week", 24 * 8)]))
        feed = self.tmp / "recent.xml"
        feed.write_text(f"<rss><channel>{items}</channel></rss>")
        self.sources.write_text(json.dumps({"sources": [{"name": "Wire", "url": feed.as_uri()}]}))

    def test_briefing_preview(self):
        self._recent_feed()
        out = self.tmp / "briefing.html"
        code, _, err = self.run_cli("briefing", "--focus", "all", "-o", str(out))
        self.assertEqual(code, 0, err)
        html = out.read_text()
        self.assertIn("Ransomware hits hospital chain", html)
        self.assertIn("CVE-2026-24851", html)
        self.assertNotIn("Old story from last week", html)  # outside the last 24 hours
        self.assertIn("top security stories", html)

    def test_briefing_send(self):
        from unittest import mock
        self._recent_feed()
        subs = self.tmp / "subs.txt"
        self.run_cli("subscribers", "add", "A@Example.com", "b@example.com", "--subscribers", str(subs))
        env = {"SECNEWS_SMTP_HOST": "smtp.example.com", "SECNEWS_SMTP_FROM": "news@example.com",
               "SECNEWS_SMTP_USER": "news@example.com", "SECNEWS_SMTP_PASSWORD": "pw"}
        with mock.patch.dict("os.environ", env), mock.patch("smtplib.SMTP") as smtp:
            server = smtp.return_value.__enter__.return_value
            code, _, err = self.run_cli("briefing", "--send", "--subscribers", str(subs))
        self.assertEqual(code, 0, err)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("news@example.com", "pw")
        sent = [c.args[0] for c in server.send_message.call_args_list]
        self.assertEqual([m["To"] for m in sent], ["a@example.com", "b@example.com"])  # one email each, nobody sees the list
        self.assertIn("Ransomware hits hospital chain", sent[0].get_body(("html",)).get_content())
        self.assertIn("Sent briefing to 2 of 2", err)

    def test_briefing_send_needs_config(self):
        from unittest import mock
        self._recent_feed()
        with mock.patch.dict("os.environ", {"SECNEWS_EMAIL_CONFIG": str(self.tmp / "none.json")}, clear=True):
            code, _, err = self.run_cli("briefing", "--send", "--to", "me@example.com")
        self.assertEqual(code, 2)
        self.assertIn("secnews email setup", err)

    def test_email_setup_wizard(self):
        import os
        import stat
        from unittest import mock
        cfg_path = self.tmp / "email.json"
        # email account, Gmail, user, sender=default, save pw, test to
        answers = iter(["2", "2", "me@gmail.com", "", "y", "boss@example.com"])
        with mock.patch.dict("os.environ", {"SECNEWS_EMAIL_CONFIG": str(cfg_path)}, clear=True), \
             mock.patch("builtins.input", lambda _: next(answers)), \
             mock.patch("getpass.getpass", lambda _: "app-pass-123"), \
             mock.patch("smtplib.SMTP") as smtp:
            server = smtp.return_value.__enter__.return_value
            code, out, err = self.run_cli("email", "setup")
            self.assertEqual(code, 0, err)
            smtp.assert_called_with("smtp.gmail.com", 587, timeout=30)
            server.login.assert_called_once_with("me@gmail.com", "app-pass-123")
            self.assertEqual(server.send_message.call_args.args[0]["To"], "boss@example.com")
            self.assertIn("Test email sent", out)
            cfg = json.loads(cfg_path.read_text())
            self.assertEqual((cfg["host"], cfg["sender"], cfg["password"]), ("smtp.gmail.com", "me@gmail.com", "app-pass-123"))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o600)
            # The saved settings are what briefing --send uses.
            self._recent_feed()
            code, _, err = self.run_cli("briefing", "--send", "--to", "team@example.com")
            self.assertEqual(code, 0, err)
            code, out, _ = self.run_cli("email", "show")
            self.assertIn("(saved)", out)
            self.assertNotIn("app-pass-123", out)

    def _fake_buttondown(self, calls, fail_status=None):
        """Stand-in for urllib.request.urlopen that records Buttondown API calls."""
        import io as _io
        import urllib.error

        class Resp(_io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        import urllib.request
        real_urlopen = urllib.request.urlopen

        def urlopen(req, timeout=None):
            if not getattr(req, "full_url", str(req)).startswith("https://api.buttondown.com/"):
                return real_urlopen(req, timeout=timeout)  # e.g. the local fixture feed
            calls.append(req)
            if fail_status:
                raise urllib.error.HTTPError(req.full_url, fail_status, "err", {}, _io.BytesIO(b'{"detail":"API access requires a paid plan"}'))
            if req.full_url.endswith("/subscribers?page_size=1"):
                return Resp(b'{"count": 3, "results": []}')
            return Resp(b'{"id": "em_1", "absolute_url": "https://buttondown.com/acme/archive/briefing"}')
        return urlopen

    def test_email_setup_buttondown(self):
        from unittest import mock
        cfg_path, site_path = self.tmp / "email.json", self.tmp / "site.json"
        site_path.write_text(json.dumps({"_comment": "keep me", "subscribe_url": "", "subscribe_email": "old@x.com",
                                         "briefing_title": "Daily"}))
        answers = iter(["1", "acme", "y"])  # Buttondown, username, connect sign-up box
        calls = []
        with mock.patch.dict("os.environ", {"SECNEWS_EMAIL_CONFIG": str(cfg_path)}, clear=True), \
             mock.patch("builtins.input", lambda _: next(answers)), \
             mock.patch("getpass.getpass", lambda _: "bd-key-123"), \
             mock.patch("urllib.request.urlopen", self._fake_buttondown(calls)):
            code, out, err = self.run_cli("--site-file", str(site_path), "email", "setup")
        self.assertEqual(code, 0, err)
        self.assertIn("You have 3 subscribers", out)
        self.assertEqual(calls[0].get_header("Authorization"), "Token bd-key-123")
        cfg = json.loads(cfg_path.read_text())
        self.assertEqual((cfg["method"], cfg["buttondown_username"]), ("buttondown", "acme"))
        site = json.loads(site_path.read_text())
        self.assertEqual(site["subscribe_url"], "https://buttondown.com/api/emails/embed-subscribe/acme")
        self.assertEqual((site["subscribe_email"], site["_comment"]), ("", "keep me"))

    def test_briefing_send_via_buttondown(self):
        from unittest import mock
        self._recent_feed()
        calls = []
        with mock.patch.dict("os.environ", {"SECNEWS_BUTTONDOWN_API_KEY": "k", "SECNEWS_EMAIL_CONFIG": str(self.tmp / "none.json")}, clear=True), \
             mock.patch("urllib.request.urlopen", self._fake_buttondown(calls)), mock.patch("smtplib.SMTP") as smtp:
            code, _, err = self.run_cli("briefing", "--send")
            self.assertEqual(code, 0, err)
            smtp.assert_not_called()  # no mailbox involved
            sent = json.loads(calls[-1].data)
            self.assertEqual(calls[-1].full_url, "https://api.buttondown.com/v1/emails")
            self.assertEqual(sent["status"], "about_to_send")
            self.assertEqual(calls[-1].get_header("X-buttondown-live-dangerously"), "true")
            self.assertIn("Ransomware hits hospital chain", sent["body"])
            self.assertNotIn("<html", sent["body"])  # Buttondown wraps it with its own header/footer/unsubscribe
            self.assertIn("2 stories sent to your Buttondown subscribers", err)

            code, _, err = self.run_cli("briefing", "--draft", "--sent-log", str(self.tmp / "sent2.json"))
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(calls[-1].data)["status"], "draft")
            self.assertIsNone(calls[-1].get_header("X-buttondown-live-dangerously"))

    def test_focus_briefing_plan(self):
        from secnews.briefing import plan_briefing
        now = datetime.now(timezone.utc)
        c = Classifier(TAX)

        def art(title, hours, also=()):
            return c.classify(Article(title=title, link=f"https://n/{title}", source="S",
                                      published=now - timedelta(hours=hours), also_reported_by=list(also)))
        arts = [
            art("Unitronics PLCs targeted by hacktivists", 3),
            art("Mirai variant exploits DrayTek routers", 5),
            art("Huge ransomware attack on retailer", 2, also=["A", "B"]),
            art("Phishing kit targets Microsoft 365", 4),
            art("Siemens SIMATIC flaw patched", 30),              # 1 day ago: catch-up material
            art("Hikvision cameras exposed online", 24 * 6),     # 6 days ago: catch-up material
            art("Modbus attack disrupts heating", 24 * 20),      # too old for catch-up (14 days)
            art("Moxa industrial router takeover", 50),          # already sent before
        ]
        sent = {"https://n/Moxa industrial router takeover"}
        secs = plan_briefing(arts, now, focus=["ics-ot", "iot-hardware"], focus_names="IoT & ICS/OT",
                             min_focus=5, catchup_days=14, general=1, already_sent=sent)
        titles = {s.heading: [a.title for a in s.stories] for s in secs}
        self.assertEqual(set(titles["Today in IoT & ICS/OT"]),
                         {"Unitronics PLCs targeted by hacktivists", "Mirai variant exploits DrayTek routers"})
        self.assertEqual(titles["Catch up"], ["Siemens SIMATIC flaw patched", "Hikvision cameras exposed online"])
        self.assertEqual(titles["Also in security today"], ["Huge ransomware attack on retailer"])  # most widely reported
        self.assertTrue(next(s for s in secs if s.heading == "Catch up").catch_up)
        # Enough fresh focus stories: no catch-up section.
        secs = plan_briefing(arts, now, focus=["ics-ot", "iot-hardware"], focus_names="X", min_focus=2, general=0)
        self.assertEqual([s.heading for s in secs], ["Today in X"])

    def test_sent_log_prunes_old_entries(self):
        from secnews.briefing import load_sent, record_sent
        path = self.tmp / "sent.json"
        record_sent(path, ["https://a"], "2026-08-01")
        record_sent(path, ["https://b"], "2026-10-12", keep_days=60)
        log = load_sent(path)
        self.assertEqual((log["last_sent_day"], list(log["links"])), ("2026-10-12", ["https://b"]))

    def test_forget_sent_day(self):
        from secnews.briefing import load_sent, record_sent
        path = self.tmp / "sent.json"
        record_sent(path, ["https://a"], "2026-10-11")
        record_sent(path, ["https://b", "https://c"], "2026-10-12")
        code, _, err = self.run_cli("briefing", "--sent-log", str(path), "--forget-sent", "2026-10-12")
        self.assertEqual(code, 0, err)
        self.assertIn("Forgot 2 stories", err)
        log = load_sent(path)
        self.assertEqual((log["last_sent_day"], list(log["links"])), ("2026-10-11", ["https://a"]))

    def test_briefing_scheduled_for_9am_and_once_per_day(self):
        from unittest import mock
        from zoneinfo import ZoneInfo
        self._recent_feed()
        london = ZoneInfo("Europe/London")
        local_now = datetime.now(london)
        calls = []
        env = {"SECNEWS_BUTTONDOWN_API_KEY": "k", "SECNEWS_EMAIL_CONFIG": str(self.tmp / "none.json")}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch("urllib.request.urlopen", self._fake_buttondown(calls)):
            if local_now.hour < 22:  # schedule for an hour from now (same day)
                at = (local_now + timedelta(hours=1)).strftime("%H:%M")
                code, _, err = self.run_cli("briefing", "--focus", "all", "--send", "--at", at, "--timezone", "Europe/London",
                                            "--once-per-day")
                self.assertEqual(code, 0, err)
                payload = json.loads(calls[-1].data)
                self.assertEqual(payload["status"], "scheduled")
                when = datetime.fromisoformat(payload["publish_date"]).astimezone(london)
                self.assertEqual(when.strftime("%H:%M"), at)
                self.assertIn("scheduled for", err)
                # A second run the same day (e.g. the backup schedule) does nothing.
                n = len(calls)
                code, _, err = self.run_cli("briefing", "--focus", "all", "--send", "--at", at, "--timezone", "Europe/London",
                                            "--once-per-day")
                self.assertEqual((code, len(calls)), (0, n))
                self.assertIn("already gone out", err)
            if local_now.hour >= 1:  # the send time has already passed: deliver immediately
                past = (local_now - timedelta(hours=1)).strftime("%H:%M")
                code, _, err = self.run_cli("briefing", "--focus", "all", "--send", "--at", past, "--timezone", "Europe/London",
                                            "--sent-log", str(self.tmp / "late.json"))
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(calls[-1].data)["status"], "about_to_send")

    def test_buttondown_without_api_access_suggests_paste(self):
        from unittest import mock
        self._recent_feed()
        with mock.patch.dict("os.environ", {"SECNEWS_BUTTONDOWN_API_KEY": "k"}, clear=True), \
             mock.patch("urllib.request.urlopen", self._fake_buttondown([], fail_status=403)):
            code, _, err = self.run_cli("briefing", "--send")
        self.assertEqual(code, 1)
        self.assertIn("--paste", err)
        out = self.tmp / "paste.html"
        code, stdout, _ = self.run_cli("briefing", "--paste", "-o", str(out))
        self.assertEqual(code, 0)
        self.assertIn("Ransomware hits hospital chain", out.read_text())
        self.assertIn("paste the contents", stdout)

    def test_email_test_reports_server_errors(self):
        import smtplib
        from unittest import mock
        cfg_path = self.tmp / "email.json"
        cfg_path.write_text(json.dumps({"host": "smtp.office365.com", "port": 587, "security": "starttls",
                                        "user": "me@org.com", "sender": "me@org.com", "password": "x"}))
        with mock.patch.dict("os.environ", {"SECNEWS_EMAIL_CONFIG": str(cfg_path)}, clear=True), \
             mock.patch("smtplib.SMTP") as smtp:
            smtp.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"SmtpClientAuthentication is disabled")
            code, _, err = self.run_cli("email", "test")
        self.assertEqual(code, 1)
        self.assertIn("SmtpClientAuthentication is disabled", err)
        self.assertIn("Authenticated SMTP", err)  # provider tip

    def test_subscribers(self):
        subs = self.tmp / "s.txt"
        self.run_cli("subscribers", "add", "x@example.com", "y@example.com", "--subscribers", str(subs))
        self.run_cli("subscribers", "add", "x@example.com", "--subscribers", str(subs))  # no duplicates
        self.run_cli("subscribers", "remove", "y@example.com", "--subscribers", str(subs))
        self.assertEqual(subs.read_text().split(), ["x@example.com"])
        code, _, err = self.run_cli("subscribers", "add", "not-an-email", "--subscribers", str(subs))
        self.assertEqual(code, 2)
        self.assertIn("not an email", err)

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
