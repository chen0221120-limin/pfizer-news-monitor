#!/usr/bin/env python3
"""Monitor GI oncology competitor websites and generate a Word report."""

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
from dataclasses import dataclass, field
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

DEFAULT_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "5"))
DEFAULT_RETRIES = int(os.getenv("REQUEST_RETRIES", "1"))
MAX_PAGES_PER_COMPANY = int(os.getenv("MAX_PAGES_PER_COMPANY", "10"))
MAX_LINKS_FROM_PAGE = int(os.getenv("MAX_LINKS_FROM_PAGE", "5"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "24"))

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
    "hcc",
    "research",
    "development",
)


@dataclass(frozen=True)
class CompanyConfig:
    company: str
    official_urls: tuple[str, ...]
    diseases: tuple[str, ...]
    targets: tuple[str, ...]
    products: tuple[str, ...]
    trial_ids: tuple[str, ...]


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
    reason: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class TextBlock:
    text: str
    tag: str
    url: str | None = None


@dataclass
class CompanyScanResult:
    company: str
    official_urls: tuple[str, ...]
    pages_checked: int = 0
    findings: list[Finding] = field(default_factory=list)
    unavailable_reason: str | None = None


def bjt_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def scan_time_label() -> str:
    return bjt_now().strftime("%Y-%m-%d %H:%M:%S CST")


def normalize_space(value: str) -> str:
    return " ".join(html.unescape(str(value or "")).replace("\xa0", " ").split())


def normalize_token(value: str) -> str:
    value = normalize_space(value)
    value = value.replace("＋", "+").replace("／", "/").replace("（", "(").replace("）", ")")
    return value.strip(" ,;|")


def load_config(path: Path) -> MonitorConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    companies = []
    for item in raw.get("companies", []):
        companies.append(
            CompanyConfig(
                company=item["company"],
                official_urls=tuple(item.get("official_urls", [])),
                diseases=tuple(item.get("diseases", [])),
                targets=tuple(item.get("targets", [])),
                products=tuple(item.get("products", [])),
                trial_ids=tuple(item.get("trial_ids", [])),
            )
        )
    return MonitorConfig(
        scan_days=int(raw.get("scan_days", 3)),
        event_terms=tuple(raw.get("event_terms", [])),
        gi_context_terms=tuple(raw.get("gi_context_terms", [])),
        common_paths=tuple(raw.get("common_paths", [])),
        companies=tuple(companies),
    )


def fetch_text(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; GIOncologyMonitor/2.0; "
                "+https://github.com/actions)"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                raw = response.read()
                return raw.decode(charset, errors="replace")
        except http.client.IncompleteRead as exc:
            if exc.partial:
                return exc.partial.decode("utf-8", errors="replace")
            last_error = exc
        except (HTTPError, OSError, URLError) as exc:
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


def same_site_or_subsite(candidate: str, roots: tuple[str, ...]) -> bool:
    candidate_host = urlparse(candidate).netloc.lower()
    for root in roots:
        root_host = urlparse(root).netloc.lower()
        if candidate_host == root_host or candidate_host.endswith("." + root_host):
            return True
    return False


def likely_useful_link(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(hint in path for hint in LINK_HINTS)


class LinkParser(HTMLParser):
    def __init__(self, base_url: str, roots: tuple[str, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.roots = roots
        self.links: list[str] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._current_href = href
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current_href is None:
            return
        url = normalize_url(self.base_url, self._current_href)
        text = normalize_space(" ".join(self._current_text)).lower()
        if (
            url
            and same_site_or_subsite(url, self.roots)
            and (likely_useful_link(url) or any(hint in text for hint in LINK_HINTS))
            and url not in self.links
        ):
            self.links.append(url)
        self._current_href = None


class TextBlockParser(HTMLParser):
    BLOCK_TAGS = {"a", "h1", "h2", "h3", "h4", "p", "li", "time"}

    def __init__(self, base_url: str, roots: tuple[str, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.roots = roots
        self.blocks: list[TextBlock] = []
        self._stack: list[tuple[str, str | None, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in self.BLOCK_TAGS:
            return
        href = dict(attrs).get("href") if tag == "a" else None
        url = normalize_url(self.base_url, href) if href else None
        self._stack.append((tag, url, []))

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1][2].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._stack:
            return
        current_tag, url, pieces = self._stack[-1]
        if tag != current_tag:
            return
        self._stack.pop()
        text = normalize_space(" ".join(pieces))
        if len(text) < 8:
            return
        if url and not same_site_or_subsite(url, self.roots):
            url = None
        self.blocks.append(TextBlock(text=text, tag=current_tag, url=url))


def strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return normalize_space(value)


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
    return normalize_space(slug.replace("-", " ").replace("_", " "))


def parse_date_text(value: str | None) -> date | None:
    if not value:
        return None
    text = normalize_space(value).strip(" ,.-")
    date_patterns = (
        ("%Y-%m-%d", r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)"),
        ("%Y/%m/%d", r"(?<!\d)(\d{4}/\d{2}/\d{2})(?!\d)"),
        ("%m.%d.%Y", r"(?<!\d)(\d{2}\.\d{2}\.\d{4})(?!\d)"),
        ("%d %B %Y", r"(?<!\d)(\d{1,2}\s+[A-Za-z]+\s+\d{4})(?!\d)"),
        ("%B %d, %Y", r"(?<!\w)([A-Za-z]+\s+\d{1,2},\s+\d{4})(?!\w)"),
        ("%Y%m%d", r"(?<!\d)(\d{8})(?!\d)"),
    )
    for fmt, pattern in date_patterns:
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
        r"(?i)published[^<]{0,80}(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        r"(?i)date[^<]{0,80}(\d{4}-\d{2}-\d{2})",
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{4}/\d{2}/\d{2})",
        r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            parsed = parse_date_text(match.group(1))
            if parsed is not None:
                return parsed
    return None


def keywords_from(values: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for value in values:
        for part in re.split(r"[;|,，、]\s*", str(value)):
            part = normalize_token(part)
            if len(part) >= 2 and part not in out:
                out.append(part)
    return out


def find_keyword_hits(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    hits = []
    for keyword in keywords:
        if keyword and keyword.lower() in lower and keyword not in hits:
            hits.append(keyword)
    return hits


def extract_text_blocks(page_text: str, page_url: str, roots: tuple[str, ...]) -> list[TextBlock]:
    parser = TextBlockParser(page_url, roots)
    parser.feed(page_text)
    return parser.blocks


def score_title_block(
    block: TextBlock,
    product_keywords: list[str],
    trial_keywords: list[str],
    target_keywords: list[str],
    disease_keywords: list[str],
    event_terms: tuple[str, ...],
) -> int:
    text = block.text
    product_hits = find_keyword_hits(text, product_keywords)
    trial_hits = find_keyword_hits(text, trial_keywords)
    target_hits = find_keyword_hits(text, target_keywords)
    disease_hits = find_keyword_hits(text, disease_keywords)
    event_hits = find_keyword_hits(text, list(event_terms))
    score = 0
    score += 40 if product_hits else 0
    score += 40 if trial_hits else 0
    score += 18 if target_hits else 0
    score += 12 if disease_hits else 0
    score += 10 if event_hits else 0
    score += 5 if parse_date_text(text) else 0
    score += 8 if block.tag in {"a", "h1", "h2", "h3", "h4"} else 0
    score += 8 if block.url else 0
    if len(text) > 260:
        score -= 12
    if len(text) > 500:
        score -= 25
    return score


def best_report_title(
    company: CompanyConfig,
    page_text: str,
    page_url: str,
    config: MonitorConfig,
    fallback_title: str,
) -> tuple[str, str, str]:
    product_keywords = keywords_from(company.products)
    trial_keywords = keywords_from(company.trial_ids)
    target_keywords = keywords_from(company.targets)
    disease_keywords = keywords_from(company.diseases) + list(config.gi_context_terms)
    roots = root_urls(company.official_urls)
    candidates = extract_text_blocks(page_text, page_url, roots)
    if not candidates:
        return fallback_title, page_url, ""

    best_block: TextBlock | None = None
    best_score = 0
    for block in candidates:
        score = score_title_block(
            block,
            product_keywords,
            trial_keywords,
            target_keywords,
            disease_keywords,
            config.event_terms,
        )
        if score > best_score:
            best_score = score
            best_block = block

    if best_block is None or best_score < 18:
        return fallback_title, page_url, ""

    title = best_block.text
    if len(title) > 220:
        title = title[:217].rstrip() + "..."
    report_url = best_block.url or page_url
    evidence = best_block.text if len(best_block.text) <= 320 else best_block.text[:317].rstrip() + "..."
    return title, report_url, evidence


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

    product_hits = find_keyword_hits(combined, keywords_from(company.products))
    trial_hits = find_keyword_hits(combined, keywords_from(company.trial_ids))
    target_hits = find_keyword_hits(combined, keywords_from(company.targets))
    disease_hits = find_keyword_hits(combined, keywords_from(company.diseases) + list(config.gi_context_terms))
    event_hits = find_keyword_hits(combined, list(config.event_terms))

    product_or_trial_hit = bool(product_hits or trial_hits)
    new_target_gi_clinical_hit = bool(target_hits and disease_hits and event_hits)
    if not product_or_trial_hit and not new_target_gi_clinical_hit:
        return None

    report_title, report_url, evidence = best_report_title(company, page_text, page_url, config, title)

    reason_parts = []
    if product_or_trial_hit:
        reason_parts.append("命中已关注产品/临床试验")
    if new_target_gi_clinical_hit:
        reason_parts.append("命中GI肿瘤相关靶点和临床/R&D事件")

    return Finding(
        company=company.company,
        title=report_title,
        url=report_url,
        published_on=published_on,
        matched_products=product_hits[:8],
        matched_targets=target_hits[:8],
        matched_trials=trial_hits[:6],
        matched_context=(disease_hits + event_hits)[:10],
        evidence=evidence,
        reason="；".join(reason_parts),
    )


def root_urls(urls: tuple[str, ...]) -> tuple[str, ...]:
    roots = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            root = f"{parsed.scheme}://{parsed.netloc}/"
            if root not in roots:
                roots.append(root)
    return tuple(roots)


def candidate_urls(company: CompanyConfig, config: MonitorConfig) -> list[str]:
    urls: list[str] = []
    roots = root_urls(company.official_urls)
    for url in company.official_urls:
        if url and url not in urls:
            urls.append(url)
    for root in roots:
        for path in config.common_paths:
            candidate = urljoin(root, path.lstrip("/"))
            if candidate not in urls:
                urls.append(candidate)
        sitemap = urljoin(root, "sitemap.xml")
        if sitemap not in urls:
            urls.append(sitemap)
    return urls[:MAX_PAGES_PER_COMPANY]


def extract_links(page_url: str, page_text: str, roots: tuple[str, ...]) -> list[str]:
    parser = LinkParser(page_url, roots)
    parser.feed(page_text)
    return parser.links[:MAX_LINKS_FROM_PAGE]


def scan_company(company: CompanyConfig, config: MonitorConfig, start_date: date, end_date: date) -> CompanyScanResult:
    if not company.official_urls:
        return CompanyScanResult(
            company=company.company,
            official_urls=company.official_urls,
            unavailable_reason="未配置官网地址",
        )

    roots = root_urls(company.official_urls)
    queue = candidate_urls(company, config)
    seen_urls: set[str] = set()
    pages_checked = 0
    fetch_success = False
    findings: list[Finding] = []

    while queue and pages_checked < MAX_PAGES_PER_COMPANY:
        url = queue.pop(0)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            page_text = fetch_text(url)
        except Exception:
            continue
        fetch_success = True
        pages_checked += 1

        if likely_useful_link(url) or url in company.official_urls:
            finding = evaluate_page(company, url, page_text, config, start_date, end_date)
            if finding and finding.url not in {item.url for item in findings}:
                findings.append(finding)

        for link in extract_links(url, page_text, roots):
            if link not in seen_urls and link not in queue and len(queue) < MAX_PAGES_PER_COMPANY * 2:
                queue.append(link)

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
    return (
        "<w:p>"
        f"{style_xml}"
        '<w:r><w:t xml:space="preserve">'
        f"{escape(text)}"
        "</w:t></w:r>"
        "</w:p>"
    )


def hyperlink_paragraph_xml(label: str, url: str, rel_id: str) -> str:
    return (
        "<w:p>"
        f'<w:r><w:t xml:space="preserve">{escape(label)}：</w:t></w:r>'
        f'<w:hyperlink r:id="{rel_id}" w:history="1">'
        '<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
        f'<w:t xml:space="preserve">{escape(url)}</w:t>'
        "</w:r></w:hyperlink>"
        "</w:p>"
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
    unavailable = [result for result in results if result.unavailable_reason]
    body = [
        paragraph_xml("GI肿瘤竞品研发动态监测报告", "Title"),
        paragraph_xml(f"扫描时间：{label}", "Subtitle"),
        paragraph_xml(f"扫描范围：{start_date.isoformat()} 至 {end_date.isoformat()}（近3天）", "Subtitle"),
        paragraph_xml(f"监测公司数：{len(results)}"),
        paragraph_xml(f"命中动态数：{len(findings)}"),
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
                    paragraph_xml(f"新闻标题/命中条目：{finding.title}"),
                    paragraph_xml(f"命中原因：{finding.reason}"),
                    paragraph_xml(f"命中产品：{format_list(finding.matched_products)}"),
                    paragraph_xml(f"命中靶点：{format_list(finding.matched_targets)}"),
                    paragraph_xml(f"命中试验编号：{format_list(finding.matched_trials)}"),
                    paragraph_xml(f"相关上下文：{format_list(finding.matched_context)}"),
                    paragraph_xml(f"命中片段：{finding.evidence or finding.title}"),
                    hyperlink_paragraph_xml("网页地址", finding.url, f"rId{rel_index}"),
                ]
            )
            rel_index += 1
    else:
        body.append(paragraph_xml("近3天未发现命中已关注产品或GI肿瘤相关靶点的新官网动态。", "Heading1"))

    if unavailable:
        body.append(paragraph_xml("未找到可读取官网内容的公司", "Heading1"))
        for result in unavailable:
            urls = "；".join(result.official_urls) if result.official_urls else "未配置"
            body.append(paragraph_xml(f"{result.company}：{result.unavailable_reason}。官网：{urls}"))

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
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    ]
    for index, finding in enumerate(findings, start=2):
        relationships.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            f'Target="{escape(finding.url)}" TargetMode="External"/>'
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
    unavailable = [result for result in results if result.unavailable_reason]
    checked_pages = sum(result.pages_checked for result in results)
    print(f"Scan window: {start_date.isoformat()} to {end_date.isoformat()}")
    print(f"Companies configured: {len(results)}")
    print(f"Pages checked: {checked_pages}")
    print(f"Matched findings: {len(findings)}")
    print(f"Companies without readable official content: {len(unavailable)}")
    for finding in findings[:50]:
        print(f"- {finding.company} | {finding.published_on.isoformat()} | {finding.title} | {finding.url}")
    if len(findings) > 50:
        print(f"... {len(findings) - 50} more finding(s)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-workers", type=int, default=int(os.getenv("MAX_WORKERS", MAX_WORKERS)))
    args = parser.parse_args()

    config = load_config(args.config)
    end_date = bjt_now().date()
    start_date = end_date - timedelta(days=max(config.scan_days - 1, 0))

    findings, results = scan_all(config, max_workers=max(args.max_workers, 1))
    label = scan_time_label()
    print_summary(findings, results, start_date, end_date)

    filename_label = bjt_now().strftime("%Y%m%d-%H%M%S")
    output_path = args.output_dir / f"{REPORT_PREFIX}-{filename_label}.docx"
    if not args.dry_run:
        create_word_document(output_path, findings, results, label, start_date, end_date)
        save_state(args.state, findings, results, start_date, end_date)
        print(f"Word document generated: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
