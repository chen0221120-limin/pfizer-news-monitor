# Pharma News Monitor

This project scans the following news pages on GitHub Actions:

- [Pfizer Newsroom](https://www.pfizer.com/newsroom)
- [AstraZeneca Press Releases](https://www.astrazeneca.com/media-centre/press-releases.html)
- [Roche Media Releases](https://www.roche.com/media/releases)
- [Innovent News](https://www.innoventbio.com/#/news)

## Schedule

GitHub Actions runs every 5 minutes on weekdays.

The script only performs a real scan for these Beijing slots:

- Beijing `09:00`
- Beijing `12:00`
- Beijing `17:00`

Each slot has a catch-up window of up to 2 hours:

- `09:00` slot can still be completed between `09:00` and `10:59`
- `12:00` slot can still be completed between `12:00` and `13:59`
- `17:00` slot can still be completed between `17:00` and `18:59`

The Python script still allows only one real scan per slot. This makes the schedule more reliable than relying on one exact minute trigger.

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
- scan result summary
- source site, translated Chinese title, original title, and article URL when new items are found

If no new items are detected, the document is still generated and explicitly states that no new updates were found.

## State

The monitor stores scan state in:

```text
.state/pfizer_news_seen.json
```

This file now keeps per-site seen URLs and the last completed Beijing time slot.

## Files

- `.github/workflows/pfizer-news-monitor.yml`: scheduled GitHub Actions workflow
- `scripts/pfizer_news_monitor.py`: fetches monitored pages, detects new items, translates titles, and generates the Word document
- `.state/pfizer_news_seen.json`: stores seen URLs and slot state

## Manual Test

You can trigger the workflow manually from the GitHub Actions page. Manual runs bypass the schedule window.

To test locally without generating a Word document or changing the saved state:

```bash
python scripts/pfizer_news_monitor.py --dry-run
```

To allow the first run to generate a Word document for the current set of items:

```bash
REPORT_ON_FIRST_RUN=true python scripts/pfizer_news_monitor.py
```
