# Security-News-Generator (`secnews`)

Pulls the latest cybersecurity news from about 70 sources, tags each article by **topic** (vulnerabilities, ransomware, fraud & cybercrime, …), **industry** (healthcare, finance, charities, …) and **location** (UK & Ireland, Europe, North America, …), and builds a security news site you can browse, search and filter, plus a daily briefing email.

- **Pure Python standard library**: Python 3.9+, nothing to install.
- **Sources:**
  - news outlets (BleepingComputer, The Hacker News, The Record, Krebs, SecurityWeek, Dark Reading, CSO Online, TechCrunch…)
  - vendor research (Talos, Unit 42, Microsoft, Google TI, Check Point, Sophos, ESET…)
  - government (CISA advisories, CISA ICS advisories, CISA's Known Exploited Vulnerabilities catalogue, NCSC UK)
  - specialist outlets for exploits (Exploit-DB, Full Disclosure, Project Zero, watchTowr), threat research (SentinelOne Labs, Huntress, Red Canary, The DFIR Report, Citizen Lab), ICS/OT (Dragos), cloud (Wiz, AWS, Datadog), supply chain (Socket), AI security, breaches (Troy Hunt, Have I Been Pwned), healthcare (HIPAA Journal) and crypto fraud (Chainalysis)
- **The site:**
  - "Search articles" box
  - a section for Latest and each category
  - a LIVE strip with the newest stories
  - a front page with the lead story, a Latest column, category blocks, More stories and "Load more stories"
  - saved stories
  - CVE Watch
  - a daily briefing sign-up
  - filters for industry, location, source and time
- **Filters:** category, industry, location, source, source type, date range, free text, CVE-only.
- **Extras:** shows each article's picture and a proper summary (taken from the feed, or from the article page when the feed has no picture or only a one-liner), extracts CVE IDs, merges the same story when several outlets cover it ("3 outlets"), and reports feeds that fail without stopping the run.
- **Outputs:** interactive HTML, Markdown, JSON, CSV.

## Quick start

```bash
git clone https://github.com/kayem06/security-news-generator && cd security-news-generator

# Build the news site (last 30 days) and open reports/digest.html in your browser
python3 -m secnews fetch -o reports/digest.html
```

Optionally install it as a `secnews` command: `pip install .` (or `pipx install .`).

## Using the site

- **Sections:** the navy bar has Latest plus one section per category. **Saved** and **CVE Watch** sit on the right.
- **Search articles** (top right) searches titles, summaries, sources and CVE IDs. Press Esc to clear it.
- **LIVE** cycles through the five newest stories. Hover to pause it.
- **Save** on any story keeps it in your browser's Saved section (see *How saved stories are kept* below).
- **CVE Watch:** add a CVE ID (`CVE-2026-1281`) or a product or vendor name (`Fortinet`). Matching stories are flagged "Watching", the CVE Watch section lists them, and the nav shows how many are new since you last looked. **Trending CVEs** lists the most-reported CVEs, each with a one-click Watch button.
- **Refine stories** (right-hand column) filters every section by industry, location, source, time period, or CVE mentions only.

### How saved stories are kept

Saved stories and your CVE watchlist are stored in the browser, not in the report file. Each saved story keeps its own copy of the title, link, picture and summary, so it stays in Saved even after it has dropped out of newer reports. They carry over to a new digest as long as you:

- **open it in the same browser on the same computer**, and
- **write the new digest to the same file** (for example always `reports/digest.html`). Chrome and Edge share saved stories between all report files, but Firefox and Safari keep them separate per file.

Clearing your browser data, or using a private window, removes them. If you use the hosted version (see *Automatic updates*), the site always lives at the same web address, so saved stories persist naturally. They're still per browser, though, so each colleague has their own.

## Daily briefing email

`secnews briefing` builds an email of the day's top stories (most widely reported first) from the last 24 hours.

```bash
python3 -m secnews briefing -o briefing.html      # preview in a browser
python3 -m secnews subscribers add jo@example.com  # manage the list (~/.secnews/subscribers.txt)
python3 -m secnews briefing --send                 # email everyone on the list
```

### Setting up email (once)

```bash
python3 -m secnews email setup
```

This asks which provider you use (Microsoft 365, Gmail, Outlook.com or other), your email address and password, then sends you a test email. The settings are saved in `~/.secnews/email.json`, which only your user account can read. You can choose not to save the password and set `SECNEWS_SMTP_PASSWORD` instead. `secnews email test` re-sends the test, and `secnews email show` shows the settings with the password hidden.

Provider notes:
- **Gmail / Google Workspace:** you need an *App Password* (Google Account › Security › 2-Step Verification › App passwords). Your normal password won't work.
- **Microsoft 365 (work account):** your IT team may need to enable *Authenticated SMTP* for the mailbox. If the test says `SmtpClientAuthentication is disabled`, that's the fix.
- **Outlook.com / Hotmail:** Microsoft is phasing out password sign-in for personal accounts, so this may be refused. Use another account if so.
- **Proton Mail:** free Proton accounts can't send from other apps. There are two ways on paid plans:
  - **Proton Mail Bridge** (Mail Plus, Unlimited and other paid plans). Install Bridge, sign in, then choose *Proton Mail via Proton Mail Bridge* in `secnews email setup`. Use the SMTP username and *Bridge password* shown in Bridge, not your Proton login password. Bridge uses its own certificate: either export it (*Bridge › Settings › Advanced settings › Export TLS certificates*) and give the `cert.pem` path, or let setup skip the check. Skipping is only allowed for a mail app on your own computer. Your computer must be on with Bridge running when the briefing sends, so use Option B in *Automatic updates*. GitHub Actions can't reach Bridge.
  - **SMTP token** (Proton for Business with a custom-domain address). Create a token in *Proton Mail › Settings › IMAP/SMTP › SMTP tokens* and choose *Proton Mail SMTP token* in setup. This connects straight to `smtp.protonmail.ch`, so it also works from GitHub Actions: set the secret `SECNEWS_SMTP_HOST` to `smtp.protonmail.ch`, `SECNEWS_SMTP_USER` to your custom-domain address, and `SECNEWS_SMTP_PASSWORD` to the token.
- For a team, a dedicated mailbox such as `security-news@yourorg.com` is best.

Environment variables override the saved settings, which is how scheduled jobs and GitHub Actions use it:

| Variable | Example |
|---|---|
| `SECNEWS_SMTP_HOST` | `smtp.office365.com` |
| `SECNEWS_SMTP_PORT` | `587` (default) |
| `SECNEWS_SMTP_USER` / `SECNEWS_SMTP_PASSWORD` | the sending mailbox and its password or app password |
| `SECNEWS_SMTP_FROM` | `security-news@yourorg.com` (defaults to the user) |
| `SECNEWS_SMTP_SECURITY` | `starttls` (default), `ssl` or `none` |

Each subscriber gets their own copy, so nobody sees the list.

**The sign-up box on the site.** A page opened from your computer can't store a mailing list itself, so set one of these in `secnews/data/site.json`:
- `subscribe_email`: your own address. The box opens a pre-filled email to you, and you add the person with `secnews subscribers add`.
- `subscribe_url`: a newsletter service's sign-up endpoint that accepts a posted `email` field, such as Buttondown, Mailchimp, Formspree or a Microsoft/Google Form. Use this if you'd rather a service manage the list and send the emails.

## Automatic updates

There are two ways to keep the site fresh and send the briefing every morning without running anything by hand.

### Option A: GitHub Actions (runs in the cloud, even when your computer is off)

The repository includes two workflows. Scheduled runs only start once they're on the `main` branch, so merge the pull request first.

| Workflow | When | What it does |
|---|---|---|
| **Update news site** (`update-site.yml`) | every 4 hours | fetches all sources, keeps the archive between runs, builds the site, and keeps a downloadable copy (*Actions › run › Artifacts › news-site*) |
| **Daily briefing email** (`daily-briefing.yml`) | 06:47 UTC every day | sends the briefing to everyone on the subscriber list |

Set-up, in the repository on GitHub:

1. **Email:** *Settings › Secrets and variables › Actions › New repository secret*. Add `SECNEWS_SMTP_HOST`, `SECNEWS_SMTP_USER` and `SECNEWS_SMTP_PASSWORD`, plus `SECNEWS_SMTP_FROM` if different. Add `SECNEWS_SUBSCRIBERS` with one email address per line. It's a secret, so the list isn't visible in the repository.
2. **Publish the site as a web page (optional):** GitHub Pages can publish the site at an address like `https://<you>.github.io/Security-News-Generator/`. It needs either a **public** repository or a paid plan (GitHub Pro) for a private one. The published page can be opened by anyone with the link. To turn it on:
   - *Settings › Pages › Source: GitHub Actions*
   - *Settings › Secrets and variables › Actions › Variables › New variable*: `PUBLISH_SITE` = `true`
3. Run each workflow once by hand (*Actions › workflow › Run workflow*) to check it works.

Notes:
- **Free minutes:** a private repository gets 2,000 free Actions minutes a month, and these workflows use roughly 800.
- **Open pages update themselves:** a published site that someone has open shows a *New stories available* button when a newer version is published.
- **Inactive public repos:** GitHub pauses scheduled workflows in public repositories after 60 days without any commits. Re-enable them from the Actions tab.

### Option B: on your own computer

Schedule the commands with your computer's scheduler. Use the full path to the folder you cloned.

**macOS / Linux** (`crontab -e`):
```cron
# rebuild the site every 4 hours, send the briefing at 07:45 on weekdays
0 */4 * * *  cd ~/Security-News-Generator && python3 -m secnews fetch -q -o reports/digest.html
45 7 * * 1-5 cd ~/Security-News-Generator && python3 -m secnews briefing -q --send
```

**Windows** (Command Prompt):
```bat
schtasks /create /tn "secnews site" /sc hourly /mo 4 /tr "cmd /c cd /d %USERPROFILE%\Security-News-Generator && python -m secnews fetch -q -o reports\digest.html"
schtasks /create /tn "secnews briefing" /sc weekly /d MON,TUE,WED,THU,FRI /st 07:45 /tr "cmd /c cd /d %USERPROFILE%\Security-News-Generator && python -m secnews briefing -q --send"
```

Your computer has to be on at those times. Keep the digest path the same so saved stories carry over.

## Weekly bulletin workflow

1. **Pull the news once and keep a snapshot** (the last 30 days by default)
   ```bash
   python3 -m secnews fetch -o reports/2026-W41.json
   ```
2. **Review interactively:** build the HTML from the snapshot (no re-download) and open it in a browser:
   ```bash
   python3 -m secnews fetch --input reports/2026-W41.json -o reports/2026-W41.html
   ```
   Browse the sections, search for actors or products, or narrow things down with **Refine stories**. Stories are shown newest first, with **Load more stories** at the bottom. If a time period filter finds nothing, the page tells you when the most recent matching story was and offers a button to widen the period.

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
| `secnews briefing` | Build the daily briefing email; `--send` emails it. See `secnews briefing -h`. |
| `secnews email setup\|test\|show` | Set up sending email, send a test, or show the settings. |
| `secnews subscribers list\|add\|remove` | Manage the briefing's subscriber list. |
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

- **`sources.json`**: add a feed with `{"name": "...", "url": "https://.../feed", "type": "news"}`. Add `"categories": ["vulnerabilities"]`, `"industries": ["healthcare"]` or `"locations": ["uk-ireland"]` to tag every article from that source, or `"enabled": false` to pause it. RSS 2.0, RSS 1.0/RDF and Atom are all supported, plus CISA's KEV JSON (`"format": "cisa-kev"`). Source types are `news`, `specialist`, `vendor-research`, `government` and `blog`. Ransomware.live, which lists every leak-site victim, is included but switched off because it's very high volume.
- **`site.json`**: daily briefing sign-up settings, the email's title, and how many stories it includes.
- **`categories.json`**: add keywords, new topics, industries or locations. For example, to filter by a single country, add a location entry with just that country's keywords. Each topic's `"color"` (a hex code such as `"#166534"`) sets its colour in the HTML report. Very dark colours are shown as solid tags with white text. Changes also apply to saved JSON snapshots, because `--input` re-tags articles on every run.

Run `secnews sources --check` after changing feeds; some publishers change feed URLs or block unknown clients from time to time.

## Development

```bash
python3 -m unittest discover -s tests -t .
```

Tests use local fixture feeds (RSS, Atom, RDF, malformed XML, a blocked HTML page) and need no network access.
