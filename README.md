# GI Oncology Competitor Monitor

This project can run either on a local Windows computer with Python or through manual GitHub Actions workflows to scan official company websites for GI oncology R&D updates.

The monitor is driven by:

```text
config/gi_monitor_config.json
```

The configuration was generated from the curated Excel tracker and includes:

- company names
- official website URLs
- monitored diseases
- targets
- company products
- clinical trial identifiers

## Local Run

This repository is ready to run directly on a company Windows computer that already has Python installed.

Recommended local entry points:

- `run_group_a.bat`
- `run_group_b.bat`
- `run_all_groups.bat`

What each file does:

- `run_group_a.bat`: scans the first half of companies
- `run_group_b.bat`: scans the second half of companies
- `run_all_groups.bat`: runs group A first, then group B

You can also run the Python script directly:

```bash
python scripts/pfizer_news_monitor.py --company-group-count 2 --company-group-index 1 --state .state/pfizer_news_seen_group_a.json --report-prefix gi-oncology-monitor-group-a
python scripts/pfizer_news_monitor.py --company-group-count 2 --company-group-index 2 --state .state/pfizer_news_seen_group_b.json --report-prefix gi-oncology-monitor-group-b
```

Generated Word reports are written to:

```text
reports/
```

## GitHub Trigger

The workflows are manual-only.

Open GitHub Actions and run both workflows when you want a full scan:

- `GI Monitor Group A`
- `GI Monitor Group B`

The company list is split automatically into 2 stable groups, so the same master configuration can be scanned in two smaller runs instead of one large run.

## Scan Logic

Each run scans the last 3 calendar days, based on Beijing time.

For each configured company, the script checks the configured official websites plus common content areas such as:

- news and newsroom pages
- media and press release pages
- pipeline pages
- R&D pages
- clinical trial pages
- product pages

A page is included in the report when it has a recognized publication date within the last 3 days and matches either:

- an already monitored company product or clinical trial identifier
- an interested target plus GI oncology context plus a clinical/R&D event term

If a company website cannot be reached or no readable official content can be found, the Word report lists that company in a separate section.

## Browser Discovery Trial

A first trial browser-assisted mode is now available for a small set of harder websites.

When enabled, the script will:

- open selected listing pages in a real browser
- capture a screenshot of the rendered page
- read visible title/date/link candidates from the rendered page
- use those rendered items as discovery signals before the normal article crawl

This is meant to reduce false positives from:

- sitemap pages
- pipeline overview pages
- homepage marketing text
- JS-rendered listing pages that do not expose clean HTML to the current crawler

Browser discovery is optional and is disabled by default.

Example local run:

```bash
set BROWSER_DISCOVERY_ENABLED=true
set NODE_EXECUTABLE=C:\path\to\node.exe
set NODE_MODULES_DIR=C:\path\to\node_modules
python scripts/pfizer_news_monitor_browser_trial.py --company-group-count 2 --company-group-index 1 --state .state/pfizer_news_seen_group_a.json --report-prefix gi-oncology-monitor-group-a-browser-test
```

Screenshots from this trial mode are written to:

```text
reports/browser-previews/
```

## Output

Every successful run generates a Microsoft Word document in:

```text
reports/gi-oncology-monitor-YYYYMMDD-HHMMSS.docx
```

Each workflow uploads its file as a GitHub Actions artifact:

```text
news-monitor-report-group-a
news-monitor-report-group-b
```

The report includes:

- scan time
- 3-day scan window
- number of monitored companies
- number of matched findings
- matched company updates grouped by company
- a news title or matched title-like item extracted from the page
- the matched text snippet, so the result can be reviewed before opening the link
- matched products, targets, trial identifiers, and context terms
- companies whose official content could not be read

## State

The monitor stores the latest scan metadata in:

```text
.state/pfizer_news_seen_group_a.json
.state/pfizer_news_seen_group_b.json
```

## Local Test

To test locally without generating a Word document or changing state:

```bash
python scripts/pfizer_news_monitor.py --dry-run
```

To reduce or increase concurrency:

```bash
python scripts/pfizer_news_monitor.py --dry-run --max-workers 4
```

The workflow defaults are tuned for broader coverage while keeping each manual run smaller:

```text
MAX_WORKERS=10
MAX_PAGES_PER_COMPANY=42
MAX_ENTRY_PAGES_PER_COMPANY=18
MAX_ARTICLE_PAGES_PER_COMPANY=24
MAX_LINKS_FROM_PAGE=24
REQUEST_TIMEOUT_SECONDS=12
REQUEST_RETRIES=2
```

These same defaults are also used when you run the scripts locally unless you override them with environment variables.
