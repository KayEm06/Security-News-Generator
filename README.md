# Security-News-Generator (`secnews`)

Pulls the latest cybersecurity news from ~30 sources, tags each article by **topic** (vulnerabilities, ransomware, fraud & cybercrime, …), **industry** (healthcare, finance, charities, …) and **location** (UK & Ireland, Europe, North America, …), and produces a digest you can filter and turn into a weekly threat intelligence bulletin.

- **Pure Python standard library**: Python 3.9+, nothing to install.
- **Sources:** news outlets (BleepingComputer, The Hacker News, The Record, Krebs, SecurityWeek, Dark Reading…), vendor research (Talos, Unit 42, Microsoft, Google TI, Check Point, Sophos, ESET…), government (CISA, NCSC UK) and blogs (SANS ISC, Schneier).
- **Filters:** category, industry, location, source, source type, date range, free text, CVE-only.
- **Extras:** shows each article's picture and a proper summary (taken from the feed, or from the article page when the feed has no picture or only a one-liner), extracts CVE IDs, merges the same story when several outlets cover it ("3 outlets"), and reports feeds that fail without stopping the run.
- **Outputs:** interactive HTML, Markdown, JSON, CSV.

## Quick start

```bash
git clone https://github.com/kayem06/security-news-generator && cd security-news-generator

# Last 7 days, everything, as an interactive page
python3 -m secnews fetch -o reports/digest.html
```

Optionally install it as a `secnews` command: `pip install .` (or `pipx install .`).

## Weekly bulletin workflow

1. **Pull the news once and keep a snapshot** (the last 30 days by default)
   ```bash
   python3 -m secnews fetch -o reports/2026-W41.json
   ```
2. **Review interactively:** build the HTML from the snapshot (no re-download) and open it in a browser:
   ```bash
   python3 -m secnews fetch --input reports/2026-W41.json -o reports/2026-W41.html
   ```
   Click category, industry, location, source and time period filters, or search for actors or products. The page opens on the last 7 days. If a filter finds nothing in that period, the page tells you when the most recent matching article was and offers a button to widen the period.

### Going back further: the archive

News feeds only carry each site's latest items, often just the last few days for busy sites. So every `fetch` also saves what it found to an archive (`~/.secnews/archive.json`) and merges earlier runs back in. Run it at least weekly and history builds up, so you can look back weeks even for quiet industries. The archive keeps 90 days by default (`--keep-days`). Use `--archive PATH` to store it elsewhere, for example a shared drive, or `--no-archive` to skip it. Pictures and summaries looked up from article pages are stored too, so they aren't fetched again.

To fill in history straight away rather than waiting for it to build up, add `--backfill 5` to a run. The tool then also fetches up to 5 older pages from each feed that supports paging; most of the WordPress-based sources do, and the others are skipped automatically. Once the archive has some history, normal runs are enough.

```bash
python3 -m secnews fetch --backfill 5 -o reports/digest.html
```
3. **Or slice it from the command line:**
   ```bash
   # Vulnerabilities + ransomware only, as Markdown
   python3 -m secnews fetch --input reports/2026-W41.json -c vulnerabilities -c ransomware -o vulns.md

   # Fraud & cybercrime affecting financial services
   python3 -m secnews fetch --input reports/2026-W41.json -c fraud -i finance

   # Anything involving UK charities
   python3 -m secnews fetch --input reports/2026-W41.json -l uk -i charity

   # Everything mentioning a CVE, as a spreadsheet
   python3 -m secnews fetch --input reports/2026-W41.json --cve-only -o cves.csv

   # Government advisories only
   python3 -m secnews fetch --type government --days 14
   ```

## Commands

| Command | What it does |
|---|---|
| `secnews fetch` | Fetch (or `--input` a saved JSON), tag, filter, write a report. See `secnews fetch -h`. |
| `secnews categories` | List topic and industry ids you can filter on. |
| `secnews sources` | List configured feeds. Add `--check` to test every feed and show item counts. |

Key `fetch` options:

| Option | |
|---|---|
| `--days N` / `--since YYYY-MM-DD` / `--until YYYY-MM-DD` | Time window (default: last 30 days) |
| `--archive PATH` / `--keep-days N` / `--no-archive` | Where earlier runs are kept (default `~/.secnews/archive.json`), for how long (default 90 days), or skip the archive |
| `-c, --category` | Topic filter. Repeat it or comma-separate values. Accepts ids, names or prefixes (`vuln`, `fraud`, `phish`) |
| `-i, --industry` | Industry filter (`healthcare`, `finance`, `charity`, `gov`, …) |
| `-l, --location` | Location filter (`uk`, `europe`, `north-america`, `asia-pacific`, …). Country names also work and map to their region (`germany` → Europe, `us` → North America) |
| `-s, --source` | Sources whose name contains the text |
| `--type` | `news`, `vendor-research`, `government`, `blog` |
| `--search TEXT`, `--cve-only` | Free-text match on title, summary and CVEs; keep only articles that mention a CVE |
| `-o FILE` / `-f FORMAT` | Output file; the format comes from the extension (`.html .md .json .csv`). Without `-o`, Markdown goes to stdout |
| `--input FILE.json` | Re-use a previous JSON snapshot instead of fetching |
| `--no-merge` | Keep duplicate coverage of the same story as separate entries |
| `--no-page-lookup` | Skip opening article pages. By default the tool reads the page of any article whose feed gave no picture or only a one-line summary, and fills them in. This option is faster, but those articles keep the feed's short text and show a source-name block instead of a picture |

Filters are combined with AND across types (category **and** industry **and** location) and with OR within one type (`-c fraud -c phishing` means either).

## Categories

**Topics:** Vulnerabilities & Exploits, Ransomware & Extortion, Malware & Tooling, Fraud & Cybercrime, Phishing & Social Engineering, Data Breaches & Leaks, Nation-State & APT, Supply Chain & Third Party, Cloud & Identity, AI & Emerging Tech, Policy, Regulation & Legal, ICS / OT & Critical Infrastructure. Articles that match none of these are tagged General.

**Industries:** Healthcare, Financial Services, Government & Public Sector, Defense & Aerospace, Education, Energy & Utilities, Manufacturing, Retail & Hospitality, Telecommunications, Technology & SaaS, Transportation & Logistics, Legal & Professional Services, Media & Entertainment, Charities & Non-profits.

**Locations:** UK & Ireland, Europe, North America, Latin America & Caribbean, Asia-Pacific, Middle East & North Africa, Sub-Saharan Africa, Russia & Central Asia. Each region is matched on country names, nationalities, major cities and national agencies (e.g. NCSC, ICO and Action Fraud count as UK). Articles from CISA are always tagged North America and articles from NCSC UK are always tagged UK & Ireland. Location tags record the places an article *mentions*. That usually means where the victims are, but a story about Russian or Chinese hackers will also be tagged with that region.

Tagging is keyword-based. Keywords are matched as whole words or phrases, including plurals ("power plant" also matches "power plants", "facility" matches "facilities"). A match scores 2 if it's in the title, in the publisher's own tags, or is a multi-word phrase in the summary; a single word in the summary scores 1. Topics need a score of 1, while industries and locations need 2. So an industry is tagged when the title mentions it, the summary contains a specific phrase like "energy company", or the summary mentions two of its keywords. An article can carry several tags. Its *primary* category, which decides where it appears in the Markdown digest, is the highest-scoring one.

## Customising

Both config files are plain JSON. Edit them in place (`secnews/data/`) or pass your own copies with `--sources-file` / `--categories-file`.

- **`sources.json`**: add a feed with `{"name": "...", "url": "https://.../feed", "type": "news"}`. Add `"categories": ["vulnerabilities"]` or `"locations": ["uk-ireland"]` to tag every article from that source, or `"enabled": false` to pause it. RSS 2.0, RSS 1.0/RDF and Atom are all supported.
- **`categories.json`**: add keywords, new topics, industries or locations. For example, to filter by a single country, add a location entry with just that country's keywords. Each topic's `"color"` (a hex code such as `"#166534"`) sets its colour in the HTML report. Very dark colours are shown as solid tags with white text. Changes also apply to saved JSON snapshots, because `--input` re-tags articles on every run.

Run `secnews sources --check` after changing feeds; some publishers change feed URLs or block unknown clients from time to time.

## Development

```bash
python3 -m unittest discover -s tests -t .
```

Tests use local fixture feeds (RSS, Atom, RDF, malformed XML, a blocked HTML page) and need no network access.
