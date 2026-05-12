# GI Oncology Competitor Monitor

This project runs a manual GitHub Actions workflow that scans official company websites for GI oncology R&D updates.

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

## Trigger

The workflow is manual-only.

Open GitHub Actions, select `Pfizer News Monitor`, and click `Run workflow` when you want to generate a new report.

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

## Output

Every successful run generates a Microsoft Word document in:

```text
reports/gi-oncology-monitor-YYYYMMDD-HHMMSS.docx
```

The workflow uploads the file as a GitHub Actions artifact:

```text
news-monitor-report
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
.state/pfizer_news_seen.json
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

The workflow defaults are tuned for faster daily manual scans:

```text
MAX_WORKERS=24
MAX_PAGES_PER_COMPANY=10
MAX_LINKS_FROM_PAGE=5
REQUEST_TIMEOUT_SECONDS=5
REQUEST_RETRIES=1
```
