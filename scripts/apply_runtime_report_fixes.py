#!/usr/bin/env python3
"""Apply small runtime fixes before running the monitor in GitHub Actions."""

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


def main() -> int:
    text = SCRIPT_PATH.read_text(encoding="utf-8")

    text = text.replace(
        'def normalize_token(value: object) -> str:\n'
        '    return normalize_space(value).strip(" ,;|")\n',
        'def normalize_token(value: object) -> str:\n'
        '    return normalize_space(value).strip(" ,;|/")\n',
        1,
    )

    disease_block = '''def disease_keywords(value: str) -> list[str]:
    normalized = normalize_token(value)
    if not normalized:
        return []
    out: list[str] = []
    for part in re.split(r"[|/;+\\uff1b\\uff0c\\u3001]+", normalized):
        token = normalize_token(part)
        if not token:
            continue
        mapped = DISEASE_KEYWORD_MAP.get(token)
        candidates = mapped if mapped else (token,)
        for candidate in candidates:
            candidate_token = normalize_token(candidate)
            if candidate_token and candidate_token not in out:
                out.append(candidate_token)
    return out


'''
    text = replace_block(text, "def disease_keywords", "def product_keywords", disease_block)

    product_block = '''def product_keywords(value: str) -> list[str]:
    normalized = normalize_token(value)
    if not normalized:
        return []
    out: list[str] = []
    for part in re.split(r"[|/;+\\uff1b\\uff0c\\u3001\\s]+", normalized):
        token = normalize_token(part)
        if token and not re.fullmatch(r"[A-Za-z]{1,2}", token) and token not in out:
            out.append(token)
    for mapped in PRODUCT_KEYWORD_MAP.get(normalized, ()):
        token = normalize_token(mapped)
        if token and not re.fullmatch(r"[A-Za-z]{1,2}", token) and token not in out:
            out.append(token)
    return out


'''
    text = replace_block(text, "def product_keywords", "def keywords_from", product_block)

    text = text.replace(
        '        for part in re.split(r"[;|,\\uff0c\\u3001\\s]+", str(value)):\n',
        '        for part in re.split(r"[;|/,\\uff0c\\u3001\\s]+", str(value)):\n',
        1,
    )

    if "class ArticleCandidate" not in text:
        text = text.replace(
            'class CompanyScanResult:\n'
            '    company: str\n'
            '    official_urls: tuple[str, ...]\n'
            '    pages_checked: int = 0\n'
            '    findings: list[Finding] = field(default_factory=list)\n'
            '    unavailable_reason: str | None = None\n',
            'class CompanyScanResult:\n'
            '    company: str\n'
            '    official_urls: tuple[str, ...]\n'
            '    pages_checked: int = 0\n'
            '    findings: list[Finding] = field(default_factory=list)\n'
            '    unavailable_reason: str | None = None\n'
            '\n'
            '\n'
            '@dataclass(frozen=True)\n'
            'class ArticleCandidate:\n'
            '    url: str\n'
            '    title_hint: str = ""\n'
            '    date_hint: date | None = None\n',
            1,
        )

    text = text.replace(
        '        self.links: dict[str, int] = {}\n'
        '        self._href: str | None = None\n',
        '        self.links: dict[str, int] = {}\n'
        '        self.link_texts: dict[str, str] = {}\n'
        '        self._href: str | None = None\n',
        1,
    )
    text = text.replace(
        '            if score > self.links.get(url, -999):\n'
        '                self.links[url] = score\n'
        '        self._href = None\n',
        '            if score > self.links.get(url, -999):\n'
        '                self.links[url] = score\n'
        '                self.link_texts[url] = text\n'
        '        self._href = None\n',
        1,
    )

    if "def extract_article_candidates" not in text:
        candidate_func = '''def extract_article_candidates(page_url: str, page_text: str, roots: tuple[str, ...]) -> list[ArticleCandidate]:
    parser = LinkParser(page_url, roots)
    parser.feed(page_text)
    ranked = sorted(parser.links.items(), key=lambda item: (-item[1], item[0]))
    candidates: list[ArticleCandidate] = []
    seen: set[str] = set()
    for url, score in ranked:
        if score <= 0 or not looks_like_article_url(url):
            continue
        title_hint = parser.link_texts.get(url, "")
        date_hint = parse_date_text(title_hint)
        if date_hint is None:
            path_tail = re.escape(urlparse(url).path.rsplit("/", 1)[-1])
            match = re.search(path_tail, page_text, flags=re.IGNORECASE)
            if match:
                start = max(match.start() - 700, 0)
                end = min(match.end() + 700, len(page_text))
                context = strip_tags(page_text[start:end])
                date_hint = parse_date_text(context)
                if not title_hint:
                    title_hint = context[:180]
        candidates.append(ArticleCandidate(url=url, title_hint=title_hint, date_hint=date_hint))
        seen.add(url)
        if len(candidates) >= MAX_LINKS_FROM_PAGE:
            break
    for match in re.finditer(r"""(?P<href>(?:https?://[^"'<>\\s]+)?/?(?:en/)?NewsDetails-\\d+-\\d+\\.html)""", page_text, flags=re.IGNORECASE):
        url = normalize_url(page_url, match.group("href"))
        if not url or url in seen or not same_site_or_subsite(url, roots) or not looks_like_article_url(url):
            continue
        start = max(match.start() - 700, 0)
        end = min(match.end() + 700, len(page_text))
        context = strip_tags(page_text[start:end])
        candidates.append(ArticleCandidate(url=url, title_hint=context[:180], date_hint=parse_date_text(context)))
        seen.add(url)
        if len(candidates) >= MAX_LINKS_FROM_PAGE:
            break
    return candidates


'''
        text = text.replace("class TextExtractor(HTMLParser):\n", candidate_func + "class TextExtractor(HTMLParser):\n", 1)

    if "def looks_like_listing_url" not in text:
        article_block = '''def looks_like_listing_url(url: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    parts = [part for part in path.split("/") if part]
    basename = parts[-1] if parts else ""
    listing_names = {
        "news",
        "news.html",
        "media",
        "media.html",
        "press",
        "press.html",
        "press-release",
        "press-release.html",
        "press-releases",
        "press-releases.html",
        "releases",
        "releases.html",
        "newsroom",
        "newsroom.html",
    }
    if basename in listing_names:
        return True
    return any(path.endswith(suffix) for suffix in ("/media/releases", "/newsroom/releases"))


def looks_like_article_url(url: str) -> bool:
    if has_skippable_extension(url):
        return False
    path = urlparse(url).path.lower()
    if looks_like_listing_url(url):
        return False
    if any(hint in path for hint in LOW_VALUE_PATH_HINTS):
        return False
    if re.search(r"/20\\d{2}/\\d{1,2}/", path):
        return True
    if re.search(r"/20\\d{2}-\\d{2}-\\d{2}", path):
        return True
    if re.search(r"(newsdetails|news-detail|news_detail|detail|article|story)[-/]?\\d+", path):
        return True
    if any(hint in path for hint in LINK_HINTS) and path_depth(url) >= 2:
        return True
    return path_depth(url) >= 3 and any(ch.isdigit() for ch in path)


'''
        text = replace_block(text, "def looks_like_article_url", "def score_discovered_link", article_block)

    if "def clean_report_title" not in text:
        title_block = '''def extract_title_from_page(page_text: str, fallback_url: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\\']og:title["\\'][^>]+content=["\\']([^"\\']+)["\\']',
        r'<meta[^>]+name=["\\']title["\\'][^>]+content=["\\']([^"\\']+)["\\']',
        r"<title[^>]*>(.*?)</title>",
        r"<h1[^>]*>(.*?)</h1>",
    )
    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = strip_tags(match.group(1)).strip(" -|")
            if title:
                return clean_report_title(title)
    slug = urlparse(fallback_url).path.rstrip("/").rsplit("/", 1)[-1]
    return clean_report_title(normalize_space(slug.replace("-", " ").replace("_", " ")) or fallback_url)


def clean_report_title(title: str) -> str:
    title = normalize_space(title)
    title = re.sub(r"\\s*[-|]\\s*(Media|News|Press Release|Press Releases)\\s*$", "", title, flags=re.IGNORECASE)
    return title.strip(" -|") or title


def clean_candidate_title(title_hint: str, fallback_url: str) -> str:
    title = normalize_space(title_hint)
    title = re.sub(r"^\\d{4}[-/]\\d{2}[-/]\\d{2}\\s*", "", title)
    title = re.sub(r"^\\d{1,2}\\s+[A-Za-z]+\\s+\\d{4}\\s*", "", title)
    title = clean_report_title(title)
    if title:
        return title[:260]
    return extract_title_from_page("", fallback_url)


'''
        text = replace_block(text, "def extract_title_from_page", "def parse_date_text", title_block)

    if "def evidence_snippet" not in text:
        evidence_func = '''def evidence_snippet(text: str, keywords: list[str], fallback: str) -> str:
    normalized = normalize_space(text)
    lower = normalized.lower()
    positions = [lower.find(keyword.lower()) for keyword in keywords if keyword and keyword.lower() in lower]
    if not positions:
        return normalize_space(fallback)[:320]
    center = max(min(positions) - 120, 0)
    snippet = normalized[center : center + 360].strip()
    return snippet or normalize_space(fallback)[:320]


'''
        text = text.replace("def watch_item_label(item: WatchItem) -> str:\n", evidence_func + "def watch_item_label(item: WatchItem) -> str:\n", 1)

        text = text.replace(
            '    return Finding(\n'
            '        company=company.company,\n'
            '        title=title,\n'
            '        url=source_url,\n'
            '        published_on=published_on,\n',
            '    evidence_terms = (\n'
            '        match_info["products"]\n'
            '        + match_info["trials"]\n'
            '        + match_info["diseases"]\n'
            '        + match_info["context"]\n'
            '        + match_info["watch_items"]\n'
            '    )\n'
            '    return Finding(\n'
            '        company=company.company,\n'
            '        title=title,\n'
            '        url=source_url,\n'
            '        published_on=published_on,\n',
            1,
        )
        text = text.replace(
            '        evidence=evidence,\n',
            '        evidence=evidence_snippet(combined_text, evidence_terms, evidence or title),\n',
            1,
        )

    text = text.replace(
        '    end_date: date,\n'
        ') -> Finding | None:\n'
        '    published_on = extract_date_from_page(page_text)\n',
        '    end_date: date,\n'
        '    date_hint: date | None = None,\n'
        '    title_hint: str = "",\n'
        ') -> Finding | None:\n'
        '    published_on = extract_date_from_page(page_text) or date_hint\n',
        1,
    )
    text = text.replace(
        '    title = extract_title_from_page(page_text, page_url)\n'
        '    plain_text = strip_tags(page_text)\n',
        '    title = extract_title_from_page(page_text, page_url)\n'
        '    if title_hint and title.lower() in {"media-news", "news", "media", "press releases", "press release"}:\n'
        '        title = clean_report_title(title_hint)\n'
        '    plain_text = strip_tags(page_text)\n',
        1,
    )
    text = text.replace(
        '    article_queue: deque[str] = deque()\n',
        '    article_queue: deque[ArticleCandidate] = deque()\n',
        1,
    )
    text = text.replace(
        '    def enqueue_article(url: str) -> None:\n',
        '    def enqueue_article(url: str, title_hint: str = "", date_hint: date | None = None) -> None:\n',
        1,
    )
    text = text.replace(
        '            article_queue.append(url)\n'
        '            queued_articles.add(url)\n',
        '            article_queue.append(ArticleCandidate(url=url, title_hint=title_hint, date_hint=date_hint))\n'
        '            queued_articles.add(url)\n',
        1,
    )
    text = text.replace(
        '    def collect_finding(url: str, page_text: str) -> None:\n'
        '        finding = evaluate_page(company, url, page_text, config, start_date, end_date)\n',
        '    def collect_finding(\n'
        '        url: str,\n'
        '        page_text: str,\n'
        '        date_hint: date | None = None,\n'
        '        title_hint: str = "",\n'
        '    ) -> None:\n'
        '        finding = evaluate_page(company, url, page_text, config, start_date, end_date, date_hint, title_hint)\n',
        1,
    )
    if "def collect_candidate_hint(candidate: ArticleCandidate)" not in text:
        text = text.replace(
            '        if finding and finding.url not in finding_urls:\n'
            '            findings.append(finding)\n'
            '            finding_urls.add(finding.url)\n'
            '\n'
            '    while (\n',
            '        if finding and finding.url not in finding_urls:\n'
            '            findings.append(finding)\n'
            '            finding_urls.add(finding.url)\n'
            '\n'
            '    def collect_candidate_hint(candidate: ArticleCandidate) -> None:\n'
            '        if candidate.date_hint is None or not is_in_scan_window(candidate.date_hint, start_date, end_date):\n'
            '            return\n'
            '        title = clean_candidate_title(candidate.title_hint, candidate.url)\n'
            '        combined_text = f"{title}\\n{candidate.title_hint}"\n'
            '        finding = build_finding(company, title, candidate.url, candidate.date_hint, combined_text, config, candidate.title_hint)\n'
            '        if finding and finding.url not in finding_urls:\n'
            '            findings.append(finding)\n'
            '            finding_urls.add(finding.url)\n'
            '\n'
            '    while (\n',
            1,
        )
    text = text.replace(
        '        for link in extract_links(url, page_text, roots):\n'
        '            if looks_like_article_url(link):\n'
        '                enqueue_article(link)\n'
        '            elif not EXACT_URLS_ONLY:\n'
        '                enqueue_discovery(link)\n',
        '        for candidate in extract_article_candidates(url, page_text, roots):\n'
        '            enqueue_article(candidate.url, candidate.title_hint, candidate.date_hint)\n'
        '            collect_candidate_hint(candidate)\n'
        '        if not EXACT_URLS_ONLY:\n'
        '            for link in extract_links(url, page_text, roots):\n'
        '                if looks_like_article_url(link):\n'
        '                    continue\n'
        '                enqueue_discovery(link)\n',
        1,
    )
    text = text.replace(
        '        url = article_queue.popleft()\n'
        '        queued_articles.discard(url)\n',
        '        candidate = article_queue.popleft()\n'
        '        url = candidate.url\n'
        '        queued_articles.discard(url)\n',
        1,
    )
    text = text.replace(
        '        fetch_success = True\n'
        '        pages_checked += 1\n'
        '        article_pages_checked += 1\n'
        '        collect_finding(url, page_text)\n',
        '        fetch_success = True\n'
        '        pages_checked += 1\n'
        '        article_pages_checked += 1\n'
        '        collect_finding(url, page_text, candidate.date_hint, candidate.title_hint)\n',
        1,
    )

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
        for root in root_urls(company.official_urls):
            for path in config.common_paths:
                add_seed(urljoin(root, path))
    return seeds


'''
    text = replace_block(text, "def candidate_urls", "def scan_company", candidate_urls_block)

    SCRIPT_PATH.write_text(text, encoding="utf-8", newline="\n")
    print("Runtime report extraction fixes applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
