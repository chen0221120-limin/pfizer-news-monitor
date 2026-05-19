#!/usr/bin/env python3
"""Monitor configured GI oncology pages and generate a Word report."""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import http.client
import json
import os
import re
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo


CONFIG_PATH = Path("config/gi_monitor_config.json")
STATE_PATH = Path(".state/pfizer_news_seen.json")
REPORT_DIR = Path("reports")
REPORT_PREFIX = "gi-oncology-monitor"

DEFAULT_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "12"))
DEFAULT_RETRIES = int(os.getenv("REQUEST_RETRIES", "2"))
MAX_PAGES_PER_COMPANY = int(os.getenv("MAX_PAGES_PER_COMPANY", "42"))
MAX_ENTRY_PAGES_PER_COMPANY = int(os.getenv("MAX_ENTRY_PAGES_PER_COMPANY", "18"))
MAX_ARTICLE_PAGES_PER_COMPANY = int(os.getenv("MAX_ARTICLE_PAGES_PER_COMPANY", "24"))
MAX_LINKS_FROM_PAGE = int(os.getenv("MAX_LINKS_FROM_PAGE", "24"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))
DISCOVERY_QUEUE_LIMIT = int(os.getenv("DISCOVERY_QUEUE_LIMIT", "120"))
EXACT_URLS_ONLY = os.getenv("EXACT_URLS_ONLY", "false").lower() in {"1", "true", "yes"}

LINK_HINTS = (
    "news",
    "press",
    "media",
    "release",
    "pipeline",
    "clinical",
    "trial",
    "study",
    "oncology",
    "cancer",
    "tumor",
    "gastric",
    "colorectal",
    "pancreatic",
    "biliary",
    "esophageal",
    "hepatocellular",
    "liver",
    "research",
    "development",
)

HIGH_PRIORITY_PATH_HINTS = (
    "news",
    "newsroom",
    "press",
    "release",
    "media",
    "pipeline",
    "research",
    "development",
    "science",
    "clinical",
    "trial",
    "product",
    "oncology",
)

LOW_VALUE_PATH_HINTS = (
    "career",
    "careers",
    "job",
    "investor",
    "event",
    "events",
    "contact",
    "privacy",
    "cookie",
    "terms",
    "esg",
    "governance",
    "supplier",
)

SKIP_FILE_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".pdf",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
)

DISEASE_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    "结直肠癌": ("结直肠癌", "colorectal cancer", "colorectal", "crc", "colon cancer", "rectal cancer"),
    "胃癌": ("胃癌", "gastric cancer", "gastric", "stomach cancer", "gastroesophageal cancer"),
    "胰腺导管腺癌": ("胰腺导管腺癌", "pancreatic ductal adenocarcinoma", "pdac", "pancreatic cancer"),
    "胆道癌": ("胆道癌", "biliary tract cancer", "btc", "cholangiocarcinoma", "biliary cancer"),
    "食管癌": ("食管癌", "esophageal cancer", "oesophageal cancer", "esophageal"),
    "肝细胞癌": ("肝细胞癌", "hepatocellular carcinoma", "hcc", "liver cancer"),
    "肝癌": ("肝癌", "hepatocellular carcinoma", "hcc", "liver cancer"),
}

PRODUCT_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    "图卡替尼": ("图卡替尼", "tucatinib"),
    "赛沃替尼": ("赛沃替尼", "savolitinib"),
    "西奥罗尼": ("西奥罗尼", "chiauranib"),
}


@dataclass(frozen=True)
class WatchItem:
    disease: str = ""
    target: str = ""
    product: str = ""
    trial_id: str = ""


@dataclass(frozen=True)
class CompanyConfig:
    company: str
    official_urls: tuple[str, ...]
    diseases: tuple[str, ...]
    targets: tuple[str, ...]
    products: tuple[str, ...]
    trial_ids: tuple[str, ...]
    watch_items: tuple[WatchItem, ...] = ()


@dataclass(frozen=True)
class MonitorConfig:
    scan_days: int
    event_terms: tuple[str, ...]
    gi_context_terms: tuple[str, ...]
    common_paths: tuple[str, ...]
    companies: tuple[CompanyConfig, ...]


@dataclass
class Finding:
    company: str
    title: str
    url: str
    published_on: date
    matched_products: list[str] = field(default_factory=list)
    matched_targets: list[str] = field(default_factory=list)
    matched_trials: list[str] = field(default_factory=list)
    matched_context: list[str] = field(default_factory=list)
    matched_watch_items: list[str] = field(default_factory=list)
    reason: str = ""
    evidence: str = ""


@dataclass
class CompanyScanResult:
    company: str
    official_urls: tuple[str, ...]
    pages_checked: int = 0
    findings: list[Finding] = field(default_factory=list)
    unavailable_reason: str | None = None


def company_has_hits(result: CompanyScanResult) -> bool:
    return bool(result.findings)


def company_unavailable(result: CompanyScanResult) -> bool:
    return result.unavailable_reason is not None


def company_scanned_without_hits(result: CompanyScanResult) -> bool:
    return result.pages_checked > 0 and not result.findings and result.unavailable_reason is None


def bjt_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def scan_time_label() -> str:
    return bjt_now().strftime("%Y-%m-%d %H:%M:%S CST")


def normalize_space(value: object) -> str:
    return " ".join(html.unescape(str(value or "")).replace("\xa0", " ").split())


def normalize_token(value: object) -> str:
    return normalize_space(value).strip(" ,;|")


def disease_keywords(value: str) -> list[str]:
    normalized = normalize_token(value)
    if not normalized:
        return []
    mapped = DISEASE_KEYWORD_MAP.get(normalized)
    if mapped:
        return [normalize_token(item) for item in mapped if normalize_token(item)]
    return [normalized]


def product_keywords(value: str) -> list[str]:
    normalized = normalize_token(value)
    if not normalized:
        return []
    out: list[str] = []
    for part in re.split(r"[|/;+；，、\s]+", normalized):
        token = normalize_token(part)
        if token and token not in out:
            out.append(token)
    for mapped in PRODUCT_KEYWORD_MAP.get(normalized, ()): 
        token = normalize_token(mapped)
        if token and token not in out:
            out.append(token)
    return out


def keywords_from(values: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for value in values:
        for part in re.split(r"[;|,，、\s]+", str(value)):
            token = normalize_token(part)
            if len(token) >= 2 and token not in out:
                out.append(token)
    return out


def load_config(path: Path) -> MonitorConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    companies: list[CompanyConfig] = []
    for item in raw.get("companies", []):
        companies.append(
            CompanyConfig(
                company=item["company"],
                official_urls=tuple(item.get("official_urls", [])),
                diseases=tuple(item.get("diseases", [])),
                targets=tuple(item.get("targets", [])),
                products=tuple(item.get("products", [])),
                trial_ids=tuple(item.get("trial_ids", [])),
                watch_items=tuple(
                    WatchItem(
                        disease=normalize_space(watch.get("disease", "")),
                        target=normalize_space(watch.get("target", "")),
                        product=normalize_space(watch.get("product", "")),
                        trial_id=normalize_space(watch.get("trial_id", "")),
                    )
                    for watch in item.get("watch_items", [])
                ),
            )
        )
    return MonitorConfig(
        scan_days=int(raw.get("scan_days", 3)),
        event_terms=tuple(raw.get("event_terms", [])),
        gi_context_terms=tuple(raw.get("gi_context_terms", [])),
        common_paths=tuple(raw.get("common_paths", [])),
        companies=tuple(companies),
    )


def slice_companies(config: MonitorConfig, group_count: int, group_index: int) -> MonitorConfig:
    if group_count <= 1:
        return config
    if group_index < 1 or group_index > group_count:
        raise ValueError(f"group_index must be between 1 and {group_count}")
    selected = tuple(
        company
        for offset, company in enumerate(config.companies)
        if offset % group_count == group_index - 1
    )
    return replace(config, companies=selected)


def fetch_text(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; GIOncologyMonitor/3.0; +https://github.com/actions)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except http.client.IncompleteRead as exc:
            if exc.partial:
                return exc.partial.decode("utf-8", errors="replace")
            last_error = exc
        except (HTTPError, URLError, OSError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(attempt)
    assert last_error is not None
    raise last_error


def normalize_url(base_url: str, href: str) -> str:
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return parsed._replace(query="", fragment="").geturl()


def root_urls(urls: tuple[str, ...]) -> tuple[str, ...]:
    roots: list[str] = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            root = f"{parsed.scheme}://{parsed.netloc}/"
            if root not in roots:
                roots.append(root)
    return tuple(roots)


def same_site_or_subsite(candidate: str, roots: tuple[str, ...]) -> bool:
    candidate_host = urlparse(candidate).netloc.lower()
    for root in roots:
        root_host = urlparse(root).netloc.lower()
        if candidate_host == root_host or candidate_host.endswith("." + root_host):
            return True
    return False


def has_skippable_extension(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in SKIP_FILE_EXTENSIONS)


def path_depth(url: str) -> int:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return len(parts)


def looks_like_article_url(url: str) -> bool:
    if has_skippable_extension(url):
        return False
    path = urlparse(url).path.lower()
    if any(hint in path for hint in LOW_VALUE_PATH_HINTS):
        return False
    if re.search(r"/20\d{2}/\d{1,2}/", path):
        return True
    if re.search(r"/20\d{2}-\d{2}-\d{2}", path):
        return True
    if any(hint in path for hint in LINK_HINTS) and path_depth(url) >= 2:
        return True
    return path_depth(url) >= 3 and any(ch.isdigit() for ch in path)


def score_discovered_link(url: str, anchor_text: str) -> int:
    if has_skippable_extension(url):
        return -100
    path = urlparse(url).path.lower()
    text = anchor_text.lower()
    score = 0
    if any(hint in path for hint in LOW_VALUE_PATH_HINTS):
        score -= 20
    if any(hint in path for hint in HIGH_PRIORITY_PATH_HINTS):
        score += 8
    if looks_like_article_url(url):
        score += 10
    if any(hint in text for hint in LINK_HINTS):
        score += 4
    if parse_date_text(anchor_text):
        score += 4
    score += min(path_depth(url), 5)
    return score


class LinkParser(HTMLParser):
    def __init__(self, base_url: str, roots: tuple[str, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.roots = roots
        self.links: dict[str, int] = {}
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        url = normalize_url(self.base_url, self._href)
        text = normalize_space(" ".join(self._text))
        if url and same_site_or_subsite(url, self.roots):
            score = score_discovered_link(url, text)
            if score > self.links.get(url, -999):
                self.links[url] = score
        self._href = None
        self._text = []


def extract_links(page_url: str, page_text: str, roots: tuple[str, ...]) -> list[str]:
    parser = LinkParser(page_url, roots)
    parser.feed(page_text)
    ranked = sorted(parser.links.items(), key=lambda item: (-item[1], item[0]))
    return [url for url, score in ranked if score > 0][:MAX_LINKS_FROM_PAGE]


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_tags(value: str) -> str:
    parser = TextExtractor()
    parser.feed(re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", value))
    return normalize_space(" ".join(parser.parts))


def extract_title_from_page(page_text: str, fallback_url: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']title["\'][^>]+content=["\']([^"\']+)["\']',
        r"<title[^>]*>(.*?)</title>",
        r"<h1[^>]*>(.*?)</h1>",
    )
    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = strip_tags(match.group(1)).strip(" -|")
            if title:
                return title
    slug = urlparse(fallback_url).path.rstrip("/").rsplit("/", 1)[-1]
    return normalize_space(slug.replace("-", " ").replace("_", " ")) or fallback_url


def parse_date_text(value: str | None) -> date | None:
    if not value:
        return None
    text = normalize_space(value)
    patterns = (
        ("%Y-%m-%d", r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)"),
        ("%Y/%m/%d", r"(?<!\d)(\d{4}/\d{2}/\d{2})(?!\d)"),
        ("%m.%d.%Y", r"(?<!\d)(\d{2}\.\d{2}\.\d{4})(?!\d)"),
        ("%d %B %Y", r"(?<!\d)(\d{1,2}\s+[A-Za-z]+\s+\d{4})(?!\d)"),
        ("%B %d, %Y", r"(?<!\w)([A-Za-z]+\s+\d{1,2},\s+\d{4})(?!\w)"),
        ("%Y%m%d", r"(?<!\d)(\d{8})(?!\d)"),
    )
    for fmt, pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            return datetime.strptime(match.group(1), fmt).date()
        except ValueError:
            continue
    return None


def extract_date_from_page(page_text: str) -> date | None:
    patterns = (
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']publishdate["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)["\']',
        r'<time[^>]+datetime=["\']([^"\']+)["\']',
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{4}/\d{2}/\d{2})",
        r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            parsed = parse_date_text(match.group(1))
            if parsed is not None:
                return parsed
    return None


def append_unique(items: list[str], values: list[str]) -> None:
    for value in values:
        if value and value not in items:
            items.append(value)


def find_keyword_hits(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    hits: list[str] = []
    for keyword in keywords:
        if keyword and keyword.lower() in lower and keyword not in hits:
            hits.append(keyword)
    return hits


def watch_item_label(item: WatchItem) -> str:
    parts = [part for part in (item.disease, item.product, item.trial_id) if part]
    return " | ".join(parts)


def match_company_watch_items(company: CompanyConfig, combined_text: str) -> dict[str, list[str]] | None:
    product_hits_all: list[str] = []
    trial_hits_all: list[str] = []
    disease_hits_all: list[str] = []
    watch_item_hits: list[str] = []
    reasons: list[str] = []

    watch_items = company.watch_items
    if not watch_items:
        watch_items = tuple(
            WatchItem(disease=d, product=p, trial_id=tr)
            for d in (company.diseases or ("",))
            for p in (company.products or ("",))
            for tr in (company.trial_ids or ("",))
        )

    for item in watch_items:
        trial_hits = find_keyword_hits(combined_text, keywords_from((item.trial_id,))) if item.trial_id else []
        product_hits = find_keyword_hits(combined_text, product_keywords(item.product)) if item.product else []
        disease_hits = find_keyword_hits(combined_text, disease_keywords(item.disease)) if item.disease else []
        target_hits = find_keyword_hits(combined_text, keywords_from((item.target,))) if item.target else []

        trial_match = bool(trial_hits)
        disease_product_match = bool(disease_hits and product_hits)
        if not trial_match and not disease_product_match:
            continue

        append_unique(product_hits_all, product_hits)
        append_unique(trial_hits_all, trial_hits)
        append_unique(disease_hits_all, disease_hits)
        append_unique(product_hits_all, target_hits)

        label = watch_item_label(item)
        if label and label not in watch_item_hits:
            watch_item_hits.append(label)
        if trial_match and "Matched trial ID" not in reasons:
            reasons.append("Matched trial ID")
        if disease_product_match and "Matched disease + drug from one watch row" not in reasons:
            reasons.append("Matched disease + drug from one watch row")

    if not reasons:
        return None

    return {
        "products": product_hits_all,
        "targets": [],
        "trials": trial_hits_all,
        "diseases": disease_hits_all,
        "context": disease_hits_all,
        "watch_items": watch_item_hits,
        "reasons": reasons,
    }


def build_finding(
    company: CompanyConfig,
    title: str,
    source_url: str,
    published_on: date,
    combined_text: str,
    config: MonitorConfig,
    evidence: str = "",
) -> Finding | None:
    match_info = match_company_watch_items(company, combined_text)
    if match_info is None:
        return None
    return Finding(
        company=company.company,
        title=title,
        url=source_url,
        published_on=published_on,
        matched_products=match_info["products"][:8],
        matched_targets=match_info["targets"][:8],
        matched_trials=match_info["trials"][:8],
        matched_context=match_info["context"][:8],
        matched_watch_items=match_info["watch_items"][:8],
        evidence=evidence,
        reason="; ".join(match_info["reasons"]),
    )


def is_in_scan_window(published_on: date, start_date: date, end_date: date) -> bool:
    return start_date <= published_on <= end_date


def evaluate_page(
    company: CompanyConfig,
    page_url: str,
    page_text: str,
    config: MonitorConfig,
    start_date: date,
    end_date: date,
) -> Finding | None:
    published_on = extract_date_from_page(page_text)
    if published_on is None or not is_in_scan_window(published_on, start_date, end_date):
        return None

    title = extract_title_from_page(page_text, page_url)
    plain_text = strip_tags(page_text)
    combined = f"{title}\n{plain_text}"
    evidence = plain_text[:320].strip() if plain_text else title
    return build_finding(company, title, page_url, published_on, combined, config, evidence)


def candidate_urls(company: CompanyConfig, config: MonitorConfig) -> list[str]:
    seeds: list[str] = []
    for url in company.official_urls:
        normalized = normalize_space(url)
        if normalized and normalized not in seeds:
            seeds.append(normalized)
    if not EXACT_URLS_ONLY:
        for root in root_urls(company.official_urls):
            for path in config.common_paths:
                candidate = normalize_space(urljoin(root, path))
                if candidate and candidate not in seeds:
                    seeds.append(candidate)
    return seeds


def scan_company(company: CompanyConfig, config: MonitorConfig, start_date: date, end_date: date) -> CompanyScanResult:
    if not company.official_urls:
        return CompanyScanResult(
            company=company.company,
            official_urls=company.official_urls,
            unavailable_reason="未配置官网地址",
        )

    roots = root_urls(company.official_urls)
    discovery_queue = deque(candidate_urls(company, config))
    article_queue: deque[str] = deque()
    queued_discovery = set(discovery_queue)
    queued_articles: set[str] = set()
    seen_urls: set[str] = set()
    pages_checked = 0
    discovery_pages_checked = 0
    article_pages_checked = 0
    fetch_success = False
    findings: list[Finding] = []
    finding_urls: set[str] = set()

    def enqueue_discovery(url: str) -> None:
        if (
            url
            and url not in seen_urls
            and url not in queued_discovery
            and len(queued_discovery) < DISCOVERY_QUEUE_LIMIT
            and same_site_or_subsite(url, roots)
            and not has_skippable_extension(url)
        ):
            discovery_queue.append(url)
            queued_discovery.add(url)

    def enqueue_article(url: str) -> None:
        if (
            url
            and url not in seen_urls
            and url not in queued_articles
            and same_site_or_subsite(url, roots)
            and not has_skippable_extension(url)
        ):
            article_queue.append(url)
            queued_articles.add(url)

    def collect_finding(url: str, page_text: str) -> None:
        finding = evaluate_page(company, url, page_text, config, start_date, end_date)
        if finding and finding.url not in finding_urls:
            findings.append(finding)
            finding_urls.add(finding.url)

    while (
        discovery_queue
        and pages_checked < MAX_PAGES_PER_COMPANY
        and discovery_pages_checked < MAX_ENTRY_PAGES_PER_COMPANY
    ):
        url = discovery_queue.popleft()
        queued_discovery.discard(url)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            page_text = fetch_text(url)
        except Exception:
            continue
        fetch_success = True
        pages_checked += 1
        discovery_pages_checked += 1

        for link in extract_links(url, page_text, roots):
            if looks_like_article_url(link):
                enqueue_article(link)
            elif not EXACT_URLS_ONLY:
                enqueue_discovery(link)

    while (
        article_queue
        and pages_checked < MAX_PAGES_PER_COMPANY
        and article_pages_checked < MAX_ARTICLE_PAGES_PER_COMPANY
    ):
        url = article_queue.popleft()
        queued_articles.discard(url)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            page_text = fetch_text(url)
        except Exception:
            continue
        fetch_success = True
        pages_checked += 1
        article_pages_checked += 1
        collect_finding(url, page_text)

        for link in extract_links(url, page_text, roots):
            if looks_like_article_url(link):
                enqueue_article(link)

    unavailable_reason = None
    if not fetch_success:
        unavailable_reason = "官网无法访问，或未找到可读取的官网内容"

    findings.sort(key=lambda item: (item.published_on, item.title.lower()), reverse=True)
    return CompanyScanResult(
        company=company.company,
        official_urls=company.official_urls,
        pages_checked=pages_checked,
        findings=findings,
        unavailable_reason=unavailable_reason,
    )


def scan_all(config: MonitorConfig, max_workers: int) -> tuple[list[Finding], list[CompanyScanResult]]:
    end_date = bjt_now().date()
    start_date = end_date - timedelta(days=max(config.scan_days - 1, 0))
    results: list[CompanyScanResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(scan_company, company, config, start_date, end_date)
            for company in config.companies
        ]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item.company.lower())
    findings = [finding for result in results for finding in result.findings]
    findings.sort(key=lambda item: (item.company.lower(), -item.published_on.toordinal(), item.title.lower()))
    return findings, results


def paragraph_xml(text: str, style: str | None = None) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{style_xml}<w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>"


def hyperlink_paragraph_xml(label: str, url: str, rel_id: str) -> str:
    return (
        "<w:p>"
        f"<w:r><w:t xml:space=\"preserve\">{escape(label)}：</w:t></w:r>"
        f"<w:hyperlink r:id=\"{rel_id}\" w:history=\"1\">"
        "<w:r><w:rPr><w:rStyle w:val=\"Hyperlink\"/></w:rPr>"
        f"<w:t xml:space=\"preserve\">{escape(url)}</w:t>"
        "</w:r></w:hyperlink></w:p>"
    )


def format_list(values: list[str]) -> str:
    return "；".join(values) if values else "未命中"


def build_document_xml(
    findings: list[Finding],
    results: list[CompanyScanResult],
    label: str,
    start_date: date,
    end_date: date,
) -> str:
    unavailable = [result for result in results if company_unavailable(result)]
    scanned_without_hits = [result for result in results if company_scanned_without_hits(result)]
    hit_companies = len([result for result in results if company_has_hits(result)])
    body = [
        paragraph_xml("GI肿瘤竞品研发动态监测报告", "Title"),
        paragraph_xml(f"扫描时间：{label}", "Subtitle"),
        paragraph_xml(f"扫描范围：{start_date.isoformat()} 至 {end_date.isoformat()}（近3天）", "Subtitle"),
        paragraph_xml(f"监测公司数：{len(results)}"),
        paragraph_xml(f"命中动态数：{len(findings)}"),
        paragraph_xml(f"命中公司数：{hit_companies}"),
        paragraph_xml(f"已扫描但近3天未命中公司数：{len(scanned_without_hits)}"),
        paragraph_xml(f"官网不可访问或未读到内容的公司数：{len(unavailable)}"),
    ]

    rel_index = 2
    if findings:
        current_company = None
        for finding in findings:
            if finding.company != current_company:
                current_company = finding.company
                body.append(paragraph_xml(current_company, "Heading1"))
            body.extend(
                [
                    paragraph_xml(f"发布日期：{finding.published_on.isoformat()}"),
                    paragraph_xml(f"新闻标题：{finding.title or '未提取到明确标题'}"),
                    paragraph_xml(f"命中原因：{finding.reason}"),
                    paragraph_xml(f"命中产品：{format_list(finding.matched_products)}"),
                    paragraph_xml(f"命中靶点：{format_list(finding.matched_targets)}"),
                    paragraph_xml(f"命中试验编号：{format_list(finding.matched_trials)}"),
                    paragraph_xml(f"相关上下文：{format_list(finding.matched_context)}"),
                    paragraph_xml(f"命中监测行：{format_list(finding.matched_watch_items)}"),
                    paragraph_xml(f"命中片段：{finding.evidence or finding.title}"),
                    hyperlink_paragraph_xml("网页地址", finding.url, f"rId{rel_index}"),
                ]
            )
            rel_index += 1
    else:
        body.append(paragraph_xml("近3天未发现命中已关注产品或目标疾病条件的官网动态。", "Heading1"))

    if scanned_without_hits:
        body.append(paragraph_xml("已扫描但近3天未命中", "Heading1"))
        for result in scanned_without_hits:
            urls = "；".join(result.official_urls) if result.official_urls else "未配置"
            body.append(paragraph_xml(f"{result.company}：已检查 {result.pages_checked} 个页面，近3天未命中符合条件的信息。监控页：{urls}"))

    if unavailable:
        body.append(paragraph_xml("官网不可访问或未读到内容", "Heading1"))
        for result in unavailable:
            urls = "；".join(result.official_urls) if result.official_urls else "未配置"
            body.append(paragraph_xml(f"{result.company}：{result.unavailable_reason}。监控页：{urls}"))

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    {''.join(body)}
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""


def build_relationships_xml(findings: list[Finding]) -> str:
    relationships = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    ]
    for index, finding in enumerate(findings, start=2):
        relationships.append(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{escape(finding.url)}" TargetMode="External"/>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  {''.join(relationships)}
</Relationships>
"""


def styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="120" w:line="259" w:lineRule="auto"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="240"/></w:pPr>
    <w:rPr><w:b/><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/><w:sz w:val="38"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="160"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/><w:color w:val="666666"/><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:outlineLvl w:val="0"/><w:spacing w:before="240" w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/><w:sz w:val="28"/></w:rPr>
  </w:style>
  <w:style w:type="character" w:styleId="Hyperlink">
    <w:name w:val="Hyperlink"/>
    <w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr>
  </w:style>
</w:styles>
"""


def create_word_document(
    output_path: Path,
    findings: list[Finding],
    results: list[CompanyScanResult],
    label: str,
    start_date: date,
    end_date: date,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""
    package_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types)
        docx.writestr("_rels/.rels", package_rels)
        docx.writestr("word/document.xml", build_document_xml(findings, results, label, start_date, end_date))
        docx.writestr("word/_rels/document.xml.rels", build_relationships_xml(findings))
        docx.writestr("word/styles.xml", styles_xml())


def save_state(path: Path, findings: list[Finding], results: list[CompanyScanResult], start_date: date, end_date: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "last_scan_bjt": scan_time_label(),
        "scan_start_date": start_date.isoformat(),
        "scan_end_date": end_date.isoformat(),
        "companies_scanned": len(results),
        "findings": len(findings),
        "unavailable_companies": len([result for result in results if result.unavailable_reason]),
    }
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_summary(findings: list[Finding], results: list[CompanyScanResult], start_date: date, end_date: date) -> None:
    unavailable = [result for result in results if company_unavailable(result)]
    scanned_without_hits = [result for result in results if company_scanned_without_hits(result)]
    hit_companies = len([result for result in results if company_has_hits(result)])
    checked_pages = sum(result.pages_checked for result in results)
    print(f"Scan window: {start_date.isoformat()} to {end_date.isoformat()}")
    print(f"Companies configured: {len(results)}")
    print(f"Pages checked: {checked_pages}")
    print(f"Matched findings: {len(findings)}")
    print(f"Companies with hits: {hit_companies}")
    print(f"Companies scanned with no hit: {len(scanned_without_hits)}")
    print(f"Companies without readable official content: {len(unavailable)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-workers", type=int, default=int(os.getenv("MAX_WORKERS", str(MAX_WORKERS))))
    parser.add_argument("--company-group-count", type=int, default=int(os.getenv("COMPANY_GROUP_COUNT", "1")))
    parser.add_argument("--company-group-index", type=int, default=int(os.getenv("COMPANY_GROUP_INDEX", "1")))
    parser.add_argument("--report-prefix", default=os.getenv("REPORT_PREFIX_OVERRIDE", REPORT_PREFIX))
    args = parser.parse_args()

    config = load_config(args.config)
    config = slice_companies(config, max(args.company_group_count, 1), args.company_group_index)
    end_date = bjt_now().date()
    start_date = end_date - timedelta(days=max(config.scan_days - 1, 0))
    findings, results = scan_all(config, max_workers=max(args.max_workers, 1))
    label = scan_time_label()
    print_summary(findings, results, start_date, end_date)

    filename_label = bjt_now().strftime("%Y%m%d-%H%M%S")
    output_path = args.output_dir / f"{args.report_prefix}-{filename_label}.docx"
    if not args.dry_run:
        create_word_document(output_path, findings, results, label, start_date, end_date)
        save_state(args.state, findings, results, start_date, end_date)
        print(f"Word document generated: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
