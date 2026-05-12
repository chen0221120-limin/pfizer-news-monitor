# Pharma News Monitor

This project scans the following news pages on GitHub Actions:

- [Pfizer Newsroom](https://www.pfizer.com/newsroom)
- [AstraZeneca Press Releases](https://www.astrazeneca.com/media-centre/press-releases.html)
- [Roche Media Releases](https://www.roche.com/media/releases)
- [Innovent News](https://www.innoventbio.com/#/news)

## Trigger

This workflow is now manual-only.

Use GitHub Actions and click `Run workflow` whenever you want to run a scan and generate a Word report.

## Output

For every successful scan, the workflow generates a local Microsoft Word document in:

```text
reports/news-monitor-YYYYMMDD-HHMMSS.docx
```

The workflow also uploads the file as a GitHub Actions artifact:

```text
news-monitor-report
```

The report includes:

- scan time
- cutoff date (`2026-04-01`)
- scan result summary
- every monitored item published on or after `2026-04-01`
- grouped by company
- newest-to-oldest ordering within each company
- source site, publication date, Chinese title, English title, and article URL

The report is no longer limited to "new since last scan". Each scan rebuilds the report from the live page data and includes all qualifying items published on or after `2026-04-01`.

## State

The monitor stores scan state in:

```text
.state/pfizer_news_seen.json
```

This file now keeps the latest scan time and the active report cutoff date.

## Files

- `.github/workflows/pfizer-news-monitor.yml`: manually triggered GitHub Actions workflow
- `scripts/pfizer_news_monitor.py`: fetches monitored pages, filters items published on or after `2026-04-01`, translates titles when practical, and generates the Word document
- `.state/pfizer_news_seen.json`: stores the latest scan metadata

## Manual Test

You can trigger the workflow manually from the GitHub Actions page.

To test locally without generating a Word document or changing the saved state:

```bash
python scripts/pfizer_news_monitor.py --dry-run
```

To skip online translation during a local test:

```bash
DISABLE_TRANSLATION=true python scripts/pfizer_news_monitor.py --dry-run
```
