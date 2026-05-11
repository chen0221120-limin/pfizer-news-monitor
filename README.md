# Pharma News Monitor

This project scans the following news pages on GitHub Actions:

- [Pfizer Newsroom](https://www.pfizer.com/newsroom)
- [AstraZeneca Press Releases](https://www.astrazeneca.com/media-centre/press-releases.html)
- [Roche Media Releases](https://www.roche.com/media/releases)
- [Innovent News](https://www.innoventbio.com/#/news)

## Schedule

GitHub Actions runs every 5 minutes on weekdays, but only inside the UTC hours that map to the Beijing scan windows and their catch-up windows.

The script only performs a real scan for these Beijing slots:

- Beijing `09:00`
- Beijing `12:00`
- Beijing `17:00`

Each slot has a catch-up window of up to 2 hours:

- `09:00` slot can still be completed between `09:00` and `10:59`
- `12:00` slot can still be completed between `12:00` and `13:59`
- `17:00` slot can still be completed between `17:00` and `18:59`

The workflow no longer runs all day. It only starts during the hours that can produce a real scan, and the Python script still allows only one real scan per slot.

If a run still starts outside a valid slot, or if that Beijing slot has already been completed, the job exits with a non-zero code. In other words: a green `Success` run should now mean a real scan happened and a Word report should exist.

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

This file now keeps the last completed Beijing time slot, the latest scan time, and the active report cutoff date.

## Files

- `.github/workflows/pfizer-news-monitor.yml`: scheduled GitHub Actions workflow
- `scripts/pfizer_news_monitor.py`: fetches monitored pages, filters items published on or after `2026-04-01`, translates titles when practical, and generates the Word document
- `.state/pfizer_news_seen.json`: stores slot state for schedule deduplication

## Manual Test

You can trigger the workflow manually from the GitHub Actions page. Manual runs bypass the schedule window.

To test locally without generating a Word document or changing the saved state:

```bash
python scripts/pfizer_news_monitor.py --dry-run
```

To skip online translation during a local test:

```bash
DISABLE_TRANSLATION=true python scripts/pfizer_news_monitor.py --dry-run
```
