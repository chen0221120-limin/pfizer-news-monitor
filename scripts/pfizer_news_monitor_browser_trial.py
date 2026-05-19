#!/usr/bin/env python3
"""Browser-assisted runner for the GI oncology monitor."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import pfizer_news_monitor as base


BROWSER_DISCOVERY_ENABLED = os.getenv("BROWSER_DISCOVERY_ENABLED", "false").lower() in {"1", "true", "yes"}
BROWSER_DISCOVERY_LIMIT = int(os.getenv("BROWSER_DISCOVERY_LIMIT", "2"))
BROWSER_RENDER_TIMEOUT_MS = int(os.getenv("BROWSER_RENDER_TIMEOUT_MS", "25000"))
BROWSER_OUTPUT_DIR = Path(os.getenv("BROWSER_OUTPUT_DIR", "reports/browser-previews"))
BROWSER_NODE_EXECUTABLE = os.getenv("NODE_EXECUTABLE", "node")
BROWSER_NODE_MODULES = os.getenv("NODE_MODULES_DIR", "")
BROWSER_DISCOVERY_DOMAINS = tuple(
    domain.strip().lower()
    for domain in os.getenv(
        "BROWSER_DISCOVERY_DOMAINS",
        "agenusbio.com,msd.com,jazzpharma.com,carsgen.com,merus.nl",
    ).split(",")
    if domain.strip()
)


@dataclass(frozen=True)
class RenderedNewsItem:
    title: str
    url: str
    date_text: str
    snippet: str = ""


def is_probable_listing_url(url: str) -> bool:
    path = base.urlparse(url).path.lower()
    if base.has_skippable_extension(url):
        return False
    if base.looks_like_article_url(url):
        return False
    if path in {"", "/"}:
        return True
    return any(hint in path for hint in base.HIGH_PRIORITY_PATH_HINTS)


def supports_browser_discovery(url: str) -> bool:
    if not BROWSER_DISCOVERY_ENABLED:
        return False
    host = base.urlparse(url).netloc.lower()
    return any(host == domain or host.endswith("." + domain) for domain in BROWSER_DISCOVERY_DOMAINS)


def run_browser_discovery(url: str, company_name: str) -> list[RenderedNewsItem]:
    if not supports_browser_discovery(url):
        return []

    script_path = CURRENT_DIR / "render_visible_news.js"
    if not script_path.exists():
        return []

    output_dir = Path("tmp/browser-discovery")
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", company_name).strip("-").lower() or "company"
    stamp = str(int(time.time() * 1000))
    json_path = output_dir / f"{slug}-{stamp}.json"
    screenshot_path = BROWSER_OUTPUT_DIR / f"{slug}-{stamp}.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if BROWSER_NODE_MODULES:
        env["NODE_PATH"] = BROWSER_NODE_MODULES

    try:
        subprocess.run(
            [
                BROWSER_NODE_EXECUTABLE,
                str(script_path),
                url,
                str(json_path),
                str(screenshot_path),
                str(BROWSER_RENDER_TIMEOUT_MS),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    try:
        raw_items = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    items: list[RenderedNewsItem] = []
    for item in raw_items:
        title = base.normalize_space(item.get("title", ""))
        item_url = base.normalize_space(item.get("url", ""))
        date_text = base.normalize_space(item.get("date", ""))
        snippet = base.normalize_space(item.get("snippet", ""))
        if title and item_url and date_text:
            items.append(RenderedNewsItem(title=title, url=item_url, date_text=date_text, snippet=snippet))
    return items


def scan_company(company: base.CompanyConfig, config: base.MonitorConfig, start_date: date, end_date: date) -> base.CompanyScanResult:
    if not company.official_urls:
        return base.CompanyScanResult(
            company=company.company,
            official_urls=company.official_urls,
            unavailable_reason="未配置官网地址",
        )

    roots = base.root_urls(company.official_urls)
    discovery_queue = deque(base.candidate_urls(company, config))
    article_queue: deque[str] = deque()
    queued_discovery = set(discovery_queue)
    queued_articles: set[str] = set()
    seen_urls: set[str] = set()
    pages_checked = 0
    discovery_pages_checked = 0
    article_pages_checked = 0
    fetch_success = False
    findings: list[base.Finding] = []
    finding_urls: set[str] = set()
    browser_discovery_runs = 0

    def enqueue_discovery(url: str) -> None:
        if (
            url
            and url not in seen_urls
            and url not in queued_discovery
            and len(queued_discovery) < base.DISCOVERY_QUEUE_LIMIT
            and base.same_site_or_subsite(url, roots)
            and not base.has_skippable_extension(url)
        ):
            discovery_queue.append(url)
            queued_discovery.add(url)

    def enqueue_article(url: str) -> None:
        if (
            url
            and url not in seen_urls
            and url not in queued_articles
            and base.same_site_or_subsite(url, roots)
            and not base.has_skippable_extension(url)
        ):
            article_queue.append(url)
            queued_articles.add(url)

    def collect_finding(url: str, page_text: str) -> None:
        finding = base.evaluate_page(company, url, page_text, config, start_date, end_date)
        if finding and finding.url not in finding_urls:
            findings.append(finding)
            finding_urls.add(finding.url)

    while (
        discovery_queue
        and pages_checked < base.MAX_PAGES_PER_COMPANY
        and discovery_pages_checked < base.MAX_ENTRY_PAGES_PER_COMPANY
    ):
        url = discovery_queue.popleft()
        queued_discovery.discard(url)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            page_text = base.fetch_text(url)
        except Exception:
            continue
        fetch_success = True
        pages_checked += 1
        discovery_pages_checked += 1

        if (
            browser_discovery_runs < BROWSER_DISCOVERY_LIMIT
            and supports_browser_discovery(url)
            and is_probable_listing_url(url)
        ):
            browser_discovery_runs += 1
            for item in run_browser_discovery(url, company.company):
                if item.url:
                    enqueue_article(item.url)

        for link in base.extract_links(url, page_text, roots):
            if base.looks_like_article_url(link):
                enqueue_article(link)
            elif not base.EXACT_URLS_ONLY:
                enqueue_discovery(link)

    while (
        article_queue
        and pages_checked < base.MAX_PAGES_PER_COMPANY
        and article_pages_checked < base.MAX_ARTICLE_PAGES_PER_COMPANY
    ):
        url = article_queue.popleft()
        queued_articles.discard(url)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            page_text = base.fetch_text(url)
        except Exception:
            continue
        fetch_success = True
        pages_checked += 1
        article_pages_checked += 1
        collect_finding(url, page_text)

        for link in base.extract_links(url, page_text, roots):
            if base.looks_like_article_url(link):
                enqueue_article(link)

    unavailable_reason = None
    if not fetch_success:
        unavailable_reason = "官网无法访问，或未找到可读取的官网内容"

    findings.sort(key=lambda item: (item.published_on, item.title.lower()), reverse=True)
    return base.CompanyScanResult(
        company=company.company,
        official_urls=company.official_urls,
        pages_checked=pages_checked,
        findings=findings,
        unavailable_reason=unavailable_reason,
    )


def scan_all(config: base.MonitorConfig, max_workers: int) -> tuple[list[base.Finding], list[base.CompanyScanResult]]:
    end_date = base.bjt_now().date()
    start_date = end_date - timedelta(days=max(config.scan_days - 1, 0))
    results: list[base.CompanyScanResult] = []

    with base.concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(scan_company, company, config, start_date, end_date)
            for company in config.companies
        ]
        for future in base.concurrent.futures.as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda item: item.company.lower())
    findings = [finding for result in results for finding in result.findings]
    findings.sort(key=lambda item: (item.company.lower(), -item.published_on.toordinal(), item.title.lower()))
    return findings, results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=base.CONFIG_PATH)
    parser.add_argument("--state", type=Path, default=base.STATE_PATH)
    parser.add_argument("--output-dir", type=Path, default=base.REPORT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-workers", type=int, default=int(os.getenv("MAX_WORKERS", str(base.MAX_WORKERS))))
    parser.add_argument("--company-group-count", type=int, default=int(os.getenv("COMPANY_GROUP_COUNT", "1")))
    parser.add_argument("--company-group-index", type=int, default=int(os.getenv("COMPANY_GROUP_INDEX", "1")))
    parser.add_argument("--report-prefix", default=os.getenv("REPORT_PREFIX_OVERRIDE", base.REPORT_PREFIX))
    args = parser.parse_args()

    config = base.load_config(args.config)
    config = base.slice_companies(config, max(args.company_group_count, 1), args.company_group_index)
    end_date = base.bjt_now().date()
    start_date = end_date - timedelta(days=max(config.scan_days - 1, 0))

    findings, results = scan_all(config, max_workers=max(args.max_workers, 1))
    label = base.scan_time_label()
    base.print_summary(findings, results, start_date, end_date)

    filename_label = base.bjt_now().strftime("%Y%m%d-%H%M%S")
    output_path = args.output_dir / f"{args.report_prefix}-{filename_label}.docx"
    if not args.dry_run:
        base.create_word_document(output_path, findings, results, label, start_date, end_date)
        base.save_state(args.state, findings, results, start_date, end_date)
        print(f"Word document generated: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
