# Pfizer News Monitor

This project scans the Pfizer Newsroom on GitHub Actions at Beijing time on weekdays at 09:07, 12:07, and 17:07:

https://www.pfizer.com/newsroom

When new items appear since the previous scan, it generates a local Microsoft Word document containing:

- scan time
- translated Chinese title
- original English title
- article URL

The first run creates a baseline state file and does not generate a historical report. Later runs create a Word document only when new items are detected.

## GitHub Actions Output

When new items are found, the workflow generates:

```text
reports/pfizer-news-YYYYMMDD-HHMMSS.docx
```

The workflow also uploads the Word document as a GitHub Actions artifact:

```text
pfizer-news-report
```

You can download it from the `Artifacts` section of the related workflow run.

## Files

- `.github/workflows/pfizer-news-monitor.yml`: scheduled GitHub Actions workflow
- `scripts/pfizer_news_monitor.py`: fetches Pfizer Newsroom, detects new items, translates titles, and generates the Word document
- `.state/pfizer_news_seen.json`: stores the URLs that have already been seen

## Manual Test

You can trigger the workflow manually from the GitHub Actions page.

To test the scan locally without generating a Word document:

```bash
python scripts/pfizer_news_monitor.py --dry-run
```

To allow the first run to generate a Word document for the current set of items:

```bash
REPORT_ON_FIRST_RUN=true python scripts/pfizer_news_monitor.py
```
