#!/usr/bin/env python3
"""Apply final runtime patches before running the monitor."""

from __future__ import annotations

import re
from pathlib import Path


SCRIPT_PATH = Path("scripts/pfizer_news_monitor.py")


def replace_block(text: str, start: str, end: str, replacement: str) -> str:
    pattern = re.compile(rf"{re.escape(start)}.*?(?={re.escape(end)})", re.DOTALL)
    new_text, count = pattern.subn(lambda _match: replacement, text, count=1)
    if count != 1:
        raise RuntimeError(f"Could not replace block starting with {start!r}")
    return new_text


DATE_AND_PUBLICATION_BLOCK = '''def parse_date_text(value: str | None) -> date | None:
    if not value:
        return None
    text = normalize_space(value)
    chinese_match = re.search(r"(?<!\\d)(\\d{4})\\s*\\u5e74\\s*(\\d{1,2})\\s*\\u6708\\s*(\\d{1,2})\\s*\\u65e5(?!\\d)", text)
    if chinese_match:
        try:
            return date(int(chinese_match.group(1)), int(chinese_match.group(2)), int(chinese_match.group(3)))
        except ValueError:
            pass
    patterns = (
        ("%Y-%m-%d", r"(?<!\\d)(\\d{4}-\\d{2}-\\d{2})(?!\\d)"),
        ("%Y/%m/%d", r"(?<!\\d)(\\d{4}/\\d{2}/\\d{2})(?!\\d)"),
        ("%m/%d/%Y", r"(?<!\\d)(\\d{1,2}/\\d{1,2}/\\d{4})(?!\\d)"),
        ("%m.%d.%Y", r"(?<!\\d)(\\d{2}\\.\\d{2}\\.\\d{4})(?!\\d)"),
        ("%d %B %Y", r"(?<!\\d)(\\d{1,2}\\s+[A-Za-z]+\\s+\\d{4})(?!\\d)"),
        ("%B %d, %Y", r"(?<!\\w)([A-Za-z]+\\s+\\d{1,2},\\s+\\d{4})(?!\\w)"),
        ("%Y%m%d", r"(?<!\\d)(\\d{8})(?!\\d)"),
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
        r"(\\d{4}\\s*\\u5e74\\s*\\d{1,2}\\s*\\u6708\\s*\\d{1,2}\\s*\\u65e5)",
        r'<meta[^>]+property=["\\']article:published_time["\\'][^>]+content=["\\']([^"\\']+)["\\']',
        r'<meta[^>]+name=["\\']publishdate["\\'][^>]+content=["\\']([^"\\']+)["\\']',
        r'<meta[^>]+name=["\\']date["\\'][^>]+content=["\\']([^"\\']+)["\\']',
        r'<time[^>]+datetime=["\\']([^"\\']+)["\\']',
        r"(\\d{4}-\\d{2}-\\d{2})",
        r"(\\d{4}/\\d{2}/\\d{2})",
        r"(\\d{1,2}/\\d{1,2}/\\d{4})",
        r"([A-Za-z]+\\s+\\d{1,2},\\s+\\d{4})",
        r"(\\d{1,2}\\s+[A-Za-z]+\\s+\\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            parsed = parse_date_text(match.group(1))
            if parsed is not None:
                return parsed
    return None


def is_publication_like_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(term in path for term in ("publication", "poster", "presentation", "abstract", "science"))


def wordpress_publications_api_urls(page_url: str) -> list[str]:
    parsed = urlparse(page_url)
    path = parsed.path.lower()
    if not is_publication_like_url(page_url):
        return []
    root = f"{parsed.scheme}://{parsed.netloc}"
    rest_bases: list[str] = []

    def add_base(value: str) -> None:
        if value not in rest_bases:
            rest_bases.append(value)

    if "publication" in path or "science" in path:
        add_base("publications")
        add_base("publication")
    if "presentation" in path:
        add_base("presentations")
        add_base("presentation")
        add_base("publications")
    if "poster" in path:
        add_base("posters")
        add_base("poster")
        add_base("publications")
    if "abstract" in path:
        add_base("abstracts")
        add_base("abstract")
        add_base("publications")
    return [f"{root}/wp-json/wp/v2/{base}?per_page=100" for base in rest_bases[:3]]


def list_text(value: object) -> list[str]:
    if isinstance(value, list):
        return [normalize_space(str(item)) for item in value if normalize_space(str(item))]
    if value:
        return [normalize_space(str(value))]
    return []


def publication_item_url(site_root: str, item: dict[str, object]) -> str:
    acf = item.get("acf") if isinstance(item.get("acf"), dict) else {}
    external_link = normalize_space(str(acf.get("external_link") or ""))
    attachment = acf.get("file_attachment")
    attachment_url = ""
    if isinstance(attachment, dict):
        attachment_url = normalize_space(str(attachment.get("url") or ""))
    post_name = normalize_space(str(item.get("post_name") or ""))
    if external_link:
        return external_link
    if attachment_url:
        return attachment_url
    link = item.get("link")
    if isinstance(link, str) and normalize_space(link):
        return normalize_space(link)
    if post_name:
        return urljoin(site_root, f"/publications/{post_name}/")
    slug = normalize_space(str(item.get("slug") or ""))
    if slug:
        post_type = normalize_space(str(item.get("type") or "publications"))
        return urljoin(site_root, f"/{post_type}/{slug}/")
    return urljoin(site_root, "/publications/")


def extract_publication_api_findings(
    company: CompanyConfig,
    listing_url: str,
    api_text: str,
    config: MonitorConfig,
    start_date: date,
    end_date: date,
) -> list[Finding]:
    try:
        data = json.loads(api_text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    parsed = urlparse(listing_url)
    site_root = f"{parsed.scheme}://{parsed.netloc}"
    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        acf = item.get("acf") if isinstance(item.get("acf"), dict) else {}
        date_text = normalize_space(str(acf.get("publication_date") or item.get("post_date") or item.get("post_date_gmt") or item.get("date") or item.get("date_gmt") or ""))
        published_on = parse_date_text(date_text)
        if published_on is None or not is_in_scan_window(published_on, start_date, end_date):
            continue
        raw_title = item.get("post_title") or item.get("title") or ""
        if isinstance(raw_title, dict):
            raw_title = raw_title.get("rendered") or ""
        title = normalize_space(html.unescape(strip_tags(str(raw_title))))
        if not title:
            continue
        publication_url = publication_item_url(site_root, item)
        conferences = list_text(acf.get("conference_publications"))
        programs = list_text(acf.get("program"))
        types = list_text(acf.get("type"))
        evidence_parts = [
            title,
            f"Publication date: {date_text}" if date_text else "",
            f"Conference/journal: {', '.join(conferences)}" if conferences else "",
            f"Program: {', '.join(programs)}" if programs else "",
            f"Type: {', '.join(types)}" if types else "",
            f"Listing page: {listing_url}",
            f"Link: {publication_url}",
        ]
        excerpt = item.get("excerpt")
        if isinstance(excerpt, dict):
            excerpt_text = normalize_space(strip_tags(str(excerpt.get("rendered") or "")))
            if excerpt_text:
                evidence_parts.append(excerpt_text)
        combined_text = "\\n".join(part for part in evidence_parts if part)
        finding = build_finding(company, title, publication_url, published_on, combined_text, config, combined_text)
        if finding:
            findings.append(finding)
    return findings


'''


CANDIDATE_URLS_BLOCK = '''def candidate_urls(company: CompanyConfig, config: MonitorConfig) -> list[str]:
    seeds: list[str] = []

    def add_seed(url: str) -> None:
        normalized = normalize_space(url)
        if normalized and normalized not in seeds:
            seeds.append(normalized)

    def add_site_variants(url: str) -> None:
        parsed = urlparse(url)
        if parsed.netloc.lower().endswith("henlius.com") and parsed.path == "/News.html":
            add_seed(f"{parsed.scheme}://{parsed.netloc}/en/News.html")

    for url in company.official_urls:
        add_seed(url)
        add_site_variants(url)
    if not EXACT_URLS_ONLY:
        publication_paths = (
            "/publications",
            "/publication",
            "/science/publications",
            "/research/publications",
            "/our-science/publications",
            "/publications-posters",
            "/science/publications-posters",
            "/our-science/publications-presentations",
            "/abstracts",
            "/posters",
            "/presentations",
        )
        publication_roots = root_urls(tuple(url for url in company.official_urls if is_publication_like_url(url)))
        for root in publication_roots:
            for path in publication_paths:
                add_seed(urljoin(root, path))
        for root in root_urls(company.official_urls):
            for path in config.common_paths:
                add_seed(urljoin(root, path))
    return seeds


'''


REPORT_BLOCK = '''def finding_source_type(finding: Finding) -> str:
    text = f"{finding.url}\\n{finding.evidence}".lower()
    if any(term in text for term in ("publication", "poster", "abstract", "presentation", ".pdf", "journal", "conference")):
        return "出版物/会议摘要"
    if any(term in text for term in ("press", "release", "news")):
        return "公司新闻/新闻稿"
    return "网页动态"


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
        paragraph_xml("GI肿瘤竞品公司动态监测报告", "Title"),
        paragraph_xml(f"扫描时间：{label}", "Subtitle"),
        paragraph_xml(f"扫描范围：{start_date.isoformat()} 至 {end_date.isoformat()}（近3天）", "Subtitle"),
        paragraph_xml(f"监测公司数：{len(results)}"),
        paragraph_xml(f"命中动态数：{len(findings)}"),
        paragraph_xml(f"命中公司数：{hit_companies}"),
    ]

    rel_index = 2
    if findings:
        current_company = None
        item_number = 0
        for finding in findings:
            if finding.company != current_company:
                current_company = finding.company
                item_number = 0
                body.append(paragraph_xml(current_company, "Heading1"))
            item_number += 1
            title = finding.title or "未提取到明确标题"
            body.extend(
                [
                    paragraph_xml(f"{item_number}. {title}", "Heading2"),
                    paragraph_xml(f"条目类型：{finding_source_type(finding)}"),
                    paragraph_xml(f"发布日期：{finding.published_on.isoformat()}"),
                    paragraph_xml(f"命中原因：{finding.reason or '命中监测关键词'}"),
                    paragraph_xml(f"命中产品：{format_list(finding.matched_products)}"),
                    paragraph_xml(f"命中靶点：{format_list(finding.matched_targets)}"),
                    paragraph_xml(f"命中试验编号：{format_list(finding.matched_trials)}"),
                    paragraph_xml(f"关联疾病/上下文：{format_list(finding.matched_context)}"),
                    paragraph_xml(f"对应Excel监测行：{format_list(finding.matched_watch_items)}"),
                    paragraph_xml(f"证据摘要：{finding.evidence or title}"),
                    hyperlink_paragraph_xml("点击打开原文/出版物", finding.url, f"rId{rel_index}"),
                ]
            )
            rel_index += 1
    else:
        body.append(paragraph_xml("本次扫描近3天内未发现命中动态。", "Heading1"))
        body.append(paragraph_xml("如后续出现命中，正文将展示条目标题、类型、发布日期、命中原因、证据摘要和原文链接。"))

    if scanned_without_hits:
        body.append(page_break_xml())
        body.append(paragraph_xml("附录A：已扫描但近3天未命中", "Subtitle"))
        for result in scanned_without_hits:
            urls = "；".join(result.official_urls) if result.official_urls else "未配置"
            body.append(paragraph_xml(f"{result.company}：已检查 {result.pages_checked} 个页面，近3天未命中符合条件的信息。监控页：{urls}"))

    if unavailable:
        body.append(page_break_xml())
        body.append(paragraph_xml("附录B：官网不可访问或未读到内容", "Subtitle"))
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


'''


COLLECT_BLOCK = '''    def collect_candidate_hint(candidate: ArticleCandidate) -> None:
        if candidate.date_hint is None or not is_in_scan_window(candidate.date_hint, start_date, end_date):
            return
        title = clean_candidate_title(candidate.title_hint, candidate.url)
        combined_text = f"{title}\n{candidate.title_hint}"
        finding = build_finding(company, title, candidate.url, candidate.date_hint, combined_text, config, candidate.title_hint)
        if finding and finding.url not in finding_urls:
            findings.append(finding)
            finding_urls.add(finding.url)

    def collect_publication_api(listing_url: str) -> bool:
        api_success = False
        for api_url in wordpress_publications_api_urls(listing_url):
            if api_url in publication_api_checked:
                continue
            publication_api_checked.add(api_url)
            try:
                api_text = fetch_text(api_url)
            except Exception:
                continue
            api_success = True
            for finding in extract_publication_api_findings(company, listing_url, api_text, config, start_date, end_date):
                if finding.url not in finding_urls:
                    findings.append(finding)
                    finding_urls.add(finding.url)
        return api_success

'''


def main() -> int:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    text = replace_block(text, "def parse_date_text", "def append_unique", DATE_AND_PUBLICATION_BLOCK)
    text = replace_block(text, "def candidate_urls", "def scan_company", CANDIDATE_URLS_BLOCK)
    report_start = "def finding_source_type" if "def finding_source_type" in text else "def build_document_xml"
    text = replace_block(text, report_start, "def build_relationships_xml", REPORT_BLOCK)
    text = replace_block(text, "    def collect_candidate_hint", "    while (", COLLECT_BLOCK)

    if "publication_api_checked: set[str] = set()" not in text:
        text = text.replace(
            "    findings: list[Finding] = []\n"
            "    finding_urls: set[str] = set()\n",
            "    findings: list[Finding] = []\n"
            "    finding_urls: set[str] = set()\n"
            "    publication_api_checked: set[str] = set()\n",
            1,
        )
    if "        api_success = collect_publication_api(url)\n" not in text:
        text = text.replace(
            "        seen_urls.add(url)\n"
            "        try:\n",
            "        seen_urls.add(url)\n"
            "        api_success = collect_publication_api(url)\n"
            "        if api_success:\n"
            "            fetch_success = True\n"
            "        try:\n",
            1,
        )
    if '<w:style w:type="paragraph" w:styleId="Heading2">' not in text:
        text = text.replace(
            '  <w:style w:type="character" w:styleId="Hyperlink">\n',
            '  <w:style w:type="paragraph" w:styleId="Heading2">\n'
            '    <w:name w:val="heading 2"/>\n'
            '    <w:basedOn w:val="Normal"/>\n'
            '    <w:next w:val="Normal"/>\n'
            '    <w:qFormat/>\n'
            '    <w:pPr><w:outlineLvl w:val="1"/><w:spacing w:before="180" w:after="80"/></w:pPr>\n'
            '    <w:rPr><w:b/><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/><w:sz w:val="24"/></w:rPr>\n'
            '  </w:style>\n'
            '  <w:style w:type="character" w:styleId="Hyperlink">\n',
            1,
        )

    SCRIPT_PATH.write_text(text, encoding="utf-8", newline="\n")
    print("Runtime patches applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
