#!/usr/bin/env python3
"""Apply focused date parsing fixes before running the monitor."""

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


DATE_BLOCK = '''def parse_date_text(value: str | None) -> date | None:
    if not value:
        return None
    text = normalize_space(value)
    chinese_match = re.search(r"(?<!\\d)(\\d{4})\\s*\\u5e74\\s*(\\d{1,2})\\s*\\u6708\\s*(\\d{1,2})\\s*\\u65e5(?!\\d)", text)
    if chinese_match:
        try:
            return date(
                int(chinese_match.group(1)),
                int(chinese_match.group(2)),
                int(chinese_match.group(3)),
            )
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
        date_text = normalize_space(
            str(
                acf.get("publication_date")
                or item.get("post_date")
                or item.get("post_date_gmt")
                or item.get("date")
                or item.get("date_gmt")
                or ""
            )
        )
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


def main() -> int:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    text = replace_block(text, "def parse_date_text", "def append_unique", DATE_BLOCK)
    candidate_urls_block = '''def candidate_urls(company: CompanyConfig, config: MonitorConfig) -> list[str]:
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
    text = replace_block(text, "def candidate_urls", "def scan_company", candidate_urls_block)
    if "publication_api_checked: set[str] = set()" not in text:
        text = text.replace(
            "    findings: list[Finding] = []\n"
            "    finding_urls: set[str] = set()\n",
            "    findings: list[Finding] = []\n"
            "    finding_urls: set[str] = set()\n"
            "    publication_api_checked: set[str] = set()\n",
            1,
        )
    if "def collect_publication_api(listing_url: str) -> None:" not in text:
        text = text.replace(
            "    def collect_candidate_hint(candidate: ArticleCandidate) -> None:\n"
            "        if candidate.date_hint is None or not is_in_scan_window(candidate.date_hint, start_date, end_date):\n"
            "            return\n"
            "        title = clean_candidate_title(candidate.title_hint, candidate.url)\n"
            "        combined_text = f\"{title}\\n{candidate.title_hint}\"\n"
            "        finding = build_finding(company, title, candidate.url, candidate.date_hint, combined_text, config, candidate.title_hint)\n"
            "        if finding and finding.url not in finding_urls:\n"
            "            findings.append(finding)\n"
            "            finding_urls.add(finding.url)\n\n",
            "    def collect_candidate_hint(candidate: ArticleCandidate) -> None:\n"
            "        if candidate.date_hint is None or not is_in_scan_window(candidate.date_hint, start_date, end_date):\n"
            "            return\n"
            "        title = clean_candidate_title(candidate.title_hint, candidate.url)\n"
            "        combined_text = f\"{title}\\n{candidate.title_hint}\"\n"
            "        finding = build_finding(company, title, candidate.url, candidate.date_hint, combined_text, config, candidate.title_hint)\n"
            "        if finding and finding.url not in finding_urls:\n"
            "            findings.append(finding)\n"
            "            finding_urls.add(finding.url)\n\n"
            "    def collect_publication_api(listing_url: str) -> bool:\n"
            "        api_success = False\n"
            "        for api_url in wordpress_publications_api_urls(listing_url):\n"
            "            if api_url in publication_api_checked:\n"
            "                continue\n"
            "            publication_api_checked.add(api_url)\n"
            "            try:\n"
            "                api_text = fetch_text(api_url)\n"
            "            except Exception:\n"
            "                continue\n"
            "            api_success = True\n"
            "            for finding in extract_publication_api_findings(company, listing_url, api_text, config, start_date, end_date):\n"
            "                if finding.url not in finding_urls:\n"
            "                    findings.append(finding)\n"
            "                    finding_urls.add(finding.url)\n"
            "        return api_success\n\n",
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
    SCRIPT_PATH.write_text(text, encoding="utf-8", newline="\n")
    print("Date parsing fix applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
