#!/usr/bin/env python3
"""Monitor selected pharma newsroom pages and generate a Word report for each scan."""

from __future__ import annotations

import argparse
import html
import http.client
import json
import os
import re
import time
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo


PFIZER_URL = "https://www.pfizer.com/newsroom"
ASTRAZENECA_URL = "https://www.astrazeneca.com/media-centre.html"
ROCHE_SITEMAP_URL = "https://www.roche.com/sitemap-0.xml"
INNOVENT_API_URL = "https://www.innoventbio.com/api/news"
INNOVENT_NEWS_URL_TEMPLATE = "https://www.innoventbio.com/#/news/{id}"

STATE_PATH = Path(".state/pfizer_news_seen.json")
REPORT_DIR = Path("reports")
REPORT_PREFIX = "news-monitor"
REPORT_CUTOFF_DATE = date(2026, 4, 1)

PFIZER_PRESS_RELEASE_MARKER = "/news/press-release/"
PFIZER_MAX_PAGES = 12
ASTRAZENECA_MARKER = "/media-centre/press-releases/"
ROCHE_RELEASE_PREFIX = "https://www.roche.com/media/releases/"
ROCHE_MAX_ITEMS = 200
SLOT_START_HOURS_BJT = (9, 12, 17)
SLOT_WINDOW_HOURS = 2
EXIT_OUTSIDE_WINDOW = 2
EXIT_ALREADY_SCANNED = 3


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    url: str
    published_on: date


def bjt_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def scan_time_label() -> str:
    return bjt_now().strftime("%Y-%m-%d %H:%M:%S CST")


def fetch_text(url: str, timeout: int = 30, retries: int = 3) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; PharmaNewsMonitor/1.0; "
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
                return response.read().decode(charset, errors="replace")
        except http.client.IncompleteRead as exc:
            if exc.partial:
                return exc.partial.decode("utf-8", errors="replace")
            last_error = exc
        except (OSError, URLError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(attempt * 2)
    assert last_error is not None
    raise last_error


def fetch_json(url: str, timeout: int = 30) -> dict:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; PharmaNewsMonitor/1.0; +https://github.com/actions)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_url(base_url: str, href: str) -> str:
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    return parsed._replace(query="", fragment="").geturl()


def clean_title(title: str) -> str:
    cleaned = " ".join(html.unescape(title).split())
    return cleaned.strip(" -|")


def is_pfizer_press_release(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != "www.pfizer.com":
        return False
    return PFIZER_PRESS_RELEASE_MARKER in parsed.path


def parse_date_text(value: str | None) -> date | None:
    if not value:
        return None

    text = " ".join(str(value).replace("\xa0", " ").split()).strip(" ,.-")
    if not text:
        return None

    text = re.sub(r"(?i)^published\s+", "", text)

    iso_match = re.search(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", text)
    if iso_match:
        try:
            return datetime.strptime(iso_match.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass

    dotted_match = re.search(r"(?<!\d)(\d{2}\.\d{2}\.\d{4})(?!\d)", text)
    if dotted_match:
        try:
            return datetime.strptime(dotted_match.group(1), "%m.%d.%Y").date()
        except ValueError:
            pass

    month_match = re.search(
        r"(?<!\d)(\d{1,2}\s+[A-Za-z]+\s+\d{4})(?!\d)",
        text,
    )
    if month_match:
        try:
            return datetime.strptime(month_match.group(1), "%d %B %Y").date()
        except ValueError:
            pass

    slash_match = re.search(r"(?<!\d)(\d{4}/\d{2}/\d{2})(?!\d)", text)
    if slash_match:
        try:
            return datetime.strptime(slash_match.group(1), "%Y/%m/%d").date()
        except ValueError:
            pass

    compact_match = re.search(r"(?<!\d)(\d{8})(?!\d)", text)
    if compact_match:
        try:
            return datetime.strptime(compact_match.group(1), "%Y%m%d").date()
        except ValueError:
            pass

    return None


def extract_title_from_page(html_text: str, fallback_url: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']title["\'][^>]+content=["\']([^"\']+)["\']',
        r"<title>(.*?)</title>",
        r"<h1[^>]*>(.*?)</h1>",
    )
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        title = clean_title(re.sub(r"<[^>]+>", " ", match.group(1)))
        if title:
            parts = [part.strip() for part in title.split("|")]
            if len(parts) > 1 and parts[0].lower() in {"roche", "astrazeneca", "pfizer"}:
                return clean_title(parts[-1])
            return title

    slug = fallback_url.rstrip("/").rsplit("/", 1)[-1]
    return clean_title(slug.replace("-", " "))


def extract_date_from_page(html_text: str) -> date | None:
    patterns = (
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']publishdate["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)["\']',
        r'(?i)published[^<]{0,40}(\d{1,2}\s+[A-Za-z]+\s+\d{4})',
        r'(\d{2}\.\d{2}\.\d{4})',
        r'(\d{4}-\d{2}-\d{2})',
    )
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        parsed = parse_date_text(match.group(1))
        if parsed is not None:
            return parsed
    return None


class PfizerPressReleaseParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self._current_date: date | None = None
        self._last_seen_date: date | None = None
        self._seen_urls: set[str] = set()
        self.items: list[NewsItem] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._current_href = href
            self._current_text = []
            self._current_date = self._last_seen_date

    def handle_data(self, data: str) -> None:
        parsed = parse_date_text(data)
        if parsed is not None:
            self._last_seen_date = parsed
            if self._current_href is None:
                self._current_date = parsed
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current_href is None:
            return
        title = clean_title("".join(self._current_text))
        url = normalize_url(PFIZER_URL, self._current_href)
        published_on = self._current_date
        if title and published_on and is_pfizer_press_release(url) and url not in self._seen_urls:
            self._seen_urls.add(url)
            self.items.append(
                NewsItem(
                    source="Pfizer",
                    title=title,
                    url=url,
                    published_on=published_on,
                )
            )
        self._current_href = None
        self._current_text = []
        self._current_date = None


def fetch_pfizer_items() -> list[NewsItem]:
    items: list[NewsItem] = []
    seen_urls: set[str] = set()
    reached_cutoff = False

    for page_number in range(PFIZER_MAX_PAGES):
        page_url = PFIZER_URL if page_number == 0 else f"{PFIZER_URL}?page={page_number}"
        parser = PfizerPressReleaseParser()
        parser.feed(fetch_text(page_url))
        page_items = [item for item in parser.items if item.url not in seen_urls]
        if not page_items:
            break

        for item in page_items:
            seen_urls.add(item.url)
            if item.published_on >= REPORT_CUTOFF_DATE:
                items.append(item)
            else:
                reached_cutoff = True

        if reached_cutoff:
            break

    return items


class AstraZenecaLatestPressReleaseParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self._seen_urls: set[str] = set()
        self.items: list[NewsItem] = []

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

        url = normalize_url(ASTRAZENECA_URL, self._current_href)
        parsed = urlparse(url)
        text = clean_title("".join(self._current_text))
        date_match = re.search(r"(.+?)\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})$", text)
        if (
            parsed.netloc == "www.astrazeneca.com"
            and ASTRAZENECA_MARKER in parsed.path
            and url not in self._seen_urls
            and date_match
        ):
            published_on = parse_date_text(date_match.group(2))
            title = clean_title(date_match.group(1))
            if published_on is not None and title:
                self._seen_urls.add(url)
                self.items.append(
                    NewsItem(
                        source="AstraZeneca",
                        title=title,
                        url=url,
                        published_on=published_on,
                    )
                )

        self._current_href = None
        self._current_text = []


def fetch_astrazeneca_items() -> list[NewsItem]:
    parser = AstraZenecaLatestPressReleaseParser()
    parser.feed(fetch_text(ASTRAZENECA_URL))
    items = [item for item in parser.items if item.published_on >= REPORT_CUTOFF_DATE]
    return items


def roche_sort_key(url: str) -> tuple[str, str]:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", url)
    return (match.group(1) if match else "", url)


def fetch_roche_urls() -> list[str]:
    xml_text = fetch_text(ROCHE_SITEMAP_URL)
    url_matches = re.findall(r"https://www\.roche\.com/media/releases/[^<]+", xml_text)

    urls = []
    seen_urls: set[str] = set()
    for url in sorted(url_matches, key=roche_sort_key, reverse=True):
        normalized = normalize_url(ROCHE_RELEASE_PREFIX, url)
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        urls.append(normalized)
        if len(urls) >= ROCHE_MAX_ITEMS:
            break

    if not urls:
        raise RuntimeError("No Roche media release URLs were found in the sitemap.")
    return urls


def fetch_roche_items() -> list[NewsItem]:
    items: list[NewsItem] = []
    for url in fetch_roche_urls():
        published_on = parse_date_text(url)
        if published_on is None or published_on < REPORT_CUTOFF_DATE:
            continue
        page_html = fetch_text(url)
        title = extract_title_from_page(page_html, url)
        items.append(
            NewsItem(
                source="Roche",
                title=title,
                url=url,
                published_on=published_on,
            )
        )
    return items


def candidate_date_values(entry: object) -> list[str]:
    candidates: list[str] = []
    if isinstance(entry, dict):
        preferred_keys = (
            "publishTime",
            "publishDate",
            "publishedAt",
            "published_at",
            "releaseDate",
            "release_date",
            "createTime",
            "createdAt",
            "created_at",
            "date",
            "newsDate",
            "showTime",
            "time",
        )
        for key in preferred_keys:
            value = entry.get(key)
            if value not in (None, ""):
                candidates.append(str(value))
        for value in entry.values():
            candidates.extend(candidate_date_values(value))
    elif isinstance(entry, list):
        for value in entry:
            candidates.extend(candidate_date_values(value))
    elif isinstance(entry, (str, int, float)):
        candidates.append(str(entry))
    return candidates


def extract_innovent_date(entry: dict, detail_url: str) -> date | None:
    for candidate in candidate_date_values(entry):
        parsed = parse_date_text(candidate)
        if parsed is not None:
            return parsed

    try:
        page_html = fetch_text(detail_url)
    except (OSError, URLError):
        return None
    return extract_date_from_page(page_html)


def fetch_innovent_items() -> list[NewsItem]:
    payload = fetch_json(INNOVENT_API_URL)
    raw_items = payload.get("data", [])
    items: list[NewsItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        news_id = entry.get("id")
        title = clean_title(str(entry.get("title", "")).strip())
        if not news_id or not title:
            continue

        url = INNOVENT_NEWS_URL_TEMPLATE.format(id=news_id)
        published_on = extract_innovent_date(entry, url)
        if published_on is None or published_on < REPORT_CUTOFF_DATE:
            continue

        items.append(
            NewsItem(
                source="Innovent",
                title=title,
                url=url,
                published_on=published_on,
            )
        )

    return items


def load_state(path: Path) -> tuple[bool, dict]:
    if not path.exists():
        return False, {"last_slot_key": None}

    raw_state = json.loads(path.read_text(encoding="utf-8"))
    return True, {
        "last_scan_bjt": raw_state.get("last_scan_bjt"),
        "last_slot_key": raw_state.get("last_slot_key"),
    }


def save_state(path: Path, last_slot_key: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_scan_bjt": scan_time_label(),
        "last_slot_key": last_slot_key,
        "report_cutoff_date": REPORT_CUTOFF_DATE.isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def translate_title(title: str) -> str:
    if os.getenv("DISABLE_TRANSLATION", "").lower() in {"1", "true", "yes"}:
        return title

    endpoint = (
        "https://api.mymemory.translated.net/get"
        f"?q={quote(title)}&langpair=en%7Czh-CN"
    )
    try:
        request = Request(endpoint, headers={"User-Agent": "PharmaNewsMonitor/1.0"})
        with urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        translated = data.get("responseData", {}).get("translatedText", "").strip()
        return html.unescape(translated) if translated else title
    except (OSError, URLError, json.JSONDecodeError):
        return title


def build_translated_titles(items: list[NewsItem]) -> dict[str, str]:
    title_cache: dict[str, str] = {}
    translated: dict[str, str] = {}
    for item in items:
        if item.title not in title_cache:
            title_cache[item.title] = translate_title(item.title)
        translated[item.url] = title_cache[item.title]
    return translated


def current_slot_key(now: datetime) -> str | None:
    if now.weekday() > 4:
        return None
    for start_hour in SLOT_START_HOURS_BJT:
        if start_hour <= now.hour < start_hour + SLOT_WINDOW_HOURS:
            return now.strftime("%Y-%m-%d") + f"-{start_hour:02d}"
    return None


def should_force_scan() -> bool:
    return os.getenv("FORCE_SCAN", "").lower() in {"1", "true", "yes"}


def format_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def group_items_by_source(items: list[NewsItem]) -> OrderedDict[str, list[NewsItem]]:
    grouped: OrderedDict[str, list[NewsItem]] = OrderedDict()
    source_order = ("Pfizer", "AstraZeneca", "Roche", "Innovent")
    for source in source_order:
        source_items = [
            item
            for item in items
            if item.source == source
        ]
        if source_items:
            grouped[source] = sorted(
                source_items,
                key=lambda item: (item.published_on, item.title.lower()),
                reverse=True,
            )
    return grouped


def build_report_text(
    items: list[NewsItem],
    translated_titles: dict[str, str],
    label: str,
    summary_text: str,
) -> str:
    lines = [
        f"扫描时间：{label}",
        f"统计起点：{REPORT_CUTOFF_DATE.isoformat()}",
        "",
        f"扫描结果：{summary_text}",
    ]
    if not items:
        return "\n".join(lines).strip() + "\n"

    grouped = group_items_by_source(items)
    overall_index = 1
    for source, source_items in grouped.items():
        lines.append("")
        lines.append(f"{source}")
        lines.append("-" * len(source))
        for item in source_items:
            lines.append(f"{overall_index}. 发布日期：{format_date(item.published_on)}")
            lines.append(f"   中文标题：{translated_titles[item.url]}")
            lines.append(f"   英文标题：{item.title}")
            lines.append(f"   网页地址：{item.url}")
            lines.append("")
            overall_index += 1
    return "\n".join(lines).strip() + "\n"


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


def hyperlink_paragraph_xml(text: str, rel_id: str) -> str:
    return (
        "<w:p>"
        '<w:r><w:t xml:space="preserve">网页地址：</w:t></w:r>'
        f'<w:hyperlink r:id="{rel_id}" w:history="1">'
        '<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
        f'<w:t xml:space="preserve">{escape(text)}</w:t>'
        "</w:r></w:hyperlink>"
        "</w:p>"
    )


def build_document_xml(
    items: list[NewsItem],
    translated_titles: dict[str, str],
    label: str,
    summary_text: str,
) -> str:
    body_parts = [
        paragraph_xml("企业新闻监测报告", "Title"),
        paragraph_xml(f"扫描时间：{label}", "Subtitle"),
        paragraph_xml(f"统计起点：{REPORT_CUTOFF_DATE.isoformat()}", "Subtitle"),
        paragraph_xml(f"扫描结果：{summary_text}"),
    ]
    grouped = group_items_by_source(items)
    rel_index = 2
    for source, source_items in grouped.items():
        body_parts.append(paragraph_xml(source, "Heading1"))
        for item in source_items:
            body_parts.extend(
                [
                    paragraph_xml(f"发布日期：{format_date(item.published_on)}"),
                    paragraph_xml(f"中文标题：{translated_titles[item.url]}"),
                    paragraph_xml(f"英文标题：{item.title}"),
                    hyperlink_paragraph_xml(item.url, f"rId{rel_index}"),
                ]
            )
            rel_index += 1

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    {''.join(body_parts)}
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""


def build_relationships_xml(items: list[NewsItem]) -> str:
    relationships = [
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    ]
    for index, item in enumerate(items, start=2):
        relationships.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            f'Target="{escape(item.url)}" TargetMode="External"/>'
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
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="240"/></w:pPr>
    <w:rPr><w:b/><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/><w:sz w:val="44"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="240"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/><w:color w:val="666666"/><w:sz w:val="24"/></w:rPr>
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
    items: list[NewsItem],
    translated_titles: dict[str, str],
    label: str,
    summary_text: str,
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
        docx.writestr(
            "word/document.xml",
            build_document_xml(items, translated_titles, label, summary_text),
        )
        docx.writestr("word/_rels/document.xml.rels", build_relationships_xml(items))
        docx.writestr("word/styles.xml", styles_xml())


def create_report(
    output_dir: Path,
    items: list[NewsItem],
    translated_titles: dict[str, str],
    label: str,
    summary_text: str,
    dry_run: bool,
) -> None:
    report_text = build_report_text(items, translated_titles, label, summary_text)
    filename_label = bjt_now().strftime("%Y%m%d-%H%M%S")
    output_path = output_dir / f"{REPORT_PREFIX}-{filename_label}.docx"
    print(report_text)
    if not dry_run:
        create_word_document(output_path, items, translated_titles, label, summary_text)
        print(f"Word document generated: {output_path}")


def fetch_items_for_report() -> list[NewsItem]:
    items = (
        fetch_pfizer_items()
        + fetch_astrazeneca_items()
        + fetch_roche_items()
        + fetch_innovent_items()
    )
    return sorted(
        items,
        key=lambda item: (item.published_on, item.source, item.title.lower()),
        reverse=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = bjt_now()
    forced = should_force_scan()

    _, state = load_state(args.state)
    slot_key = current_slot_key(now)
    if not forced:
        if slot_key is None:
            print("Current Beijing time is outside the scheduled scan windows.")
            return EXIT_OUTSIDE_WINDOW
        if state.get("last_slot_key") == slot_key:
            print(f"Scan already completed for Beijing slot {slot_key}.")
            return EXIT_ALREADY_SCANNED

    items = fetch_items_for_report()
    translated_titles = build_translated_titles(items)

    label = scan_time_label()
    if items:
        summary_text = (
            f"自 {REPORT_CUTOFF_DATE.isoformat()} 起共发现 {len(items)} 条符合条件的 press release。"
        )
        print(
            f"Collected {len(items)} press release item(s) on or after {REPORT_CUTOFF_DATE.isoformat()}."
        )
    else:
        summary_text = f"自 {REPORT_CUTOFF_DATE.isoformat()} 起未发现符合条件的 press release。"
        print(
            f"No press release items were found on or after {REPORT_CUTOFF_DATE.isoformat()}."
        )

    create_report(
        args.output_dir,
        items,
        translated_titles,
        label,
        summary_text,
        args.dry_run,
    )

    if not args.dry_run:
        save_state(args.state, slot_key or state.get("last_slot_key"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
