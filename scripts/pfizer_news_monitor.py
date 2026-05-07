#!/usr/bin/env python3
"""Monitor selected pharma newsroom pages and generate a Word report for new items."""

from __future__ import annotations

import argparse
import html
import http.client
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
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

PFIZER_MARKERS = (
    "/news/press-release/",
    "/news/articles/",
    "/news/announcements/",
    "/news/updates-and-statements/",
    "/news/partnering-news/",
)
ASTRAZENECA_MARKER = "/media-centre/press-releases/"
ROCHE_RELEASE_PREFIX = "https://www.roche.com/media/releases/"
ROCHE_MAX_ITEMS = 200
SLOT_HOURS_BJT = {9, 12, 17}


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    url: str


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._active_href: str | None = None
        self._active_text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._active_href = href
            self._active_text = []

    def handle_data(self, data: str) -> None:
        if self._active_href is not None:
            self._active_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._active_href is None:
            return
        text = " ".join("".join(self._active_text).split())
        if text:
            self.links.append((self._active_href, html.unescape(text)))
        self._active_href = None
        self._active_text = []


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


def is_pfizer_article(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != "www.pfizer.com":
        return False
    return any(marker in parsed.path for marker in PFIZER_MARKERS)


def fetch_pfizer_items() -> list[NewsItem]:
    parser = LinkCollector()
    parser.feed(fetch_text(PFIZER_URL))

    seen_urls: set[str] = set()
    items: list[NewsItem] = []
    for href, title in parser.links:
        url = normalize_url(PFIZER_URL, href)
        if not is_pfizer_article(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(NewsItem(source="Pfizer", title=clean_title(title), url=url))
    if not items:
        raise RuntimeError("No Pfizer newsroom article links were found.")
    return items


def fetch_astrazeneca_items() -> list[NewsItem]:
    parser = LinkCollector()
    parser.feed(fetch_text(ASTRAZENECA_URL))

    seen_urls: set[str] = set()
    items: list[NewsItem] = []
    for href, title in parser.links:
        url = normalize_url(ASTRAZENECA_URL, href)
        parsed = urlparse(url)
        if parsed.netloc != "www.astrazeneca.com":
            continue
        if ASTRAZENECA_MARKER not in parsed.path or url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(NewsItem(source="AstraZeneca", title=clean_title(title), url=url))
    if not items:
        raise RuntimeError("No AstraZeneca press release links were found.")
    return items


def roche_sort_key(url: str) -> tuple[str, str]:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", url)
    return (match.group(1) if match else "", url)


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


def fetch_roche_item(url: str) -> NewsItem:
    page_html = fetch_text(url)
    title = extract_title_from_page(page_html, url)
    return NewsItem(source="Roche", title=title, url=url)


def fetch_innovent_items() -> list[NewsItem]:
    payload = fetch_json(INNOVENT_API_URL)
    raw_items = payload.get("data", [])
    items: list[NewsItem] = []
    for entry in raw_items:
        news_id = entry.get("id")
        title = clean_title(str(entry.get("title", "")).strip())
        if not news_id or not title:
            continue
        url = INNOVENT_NEWS_URL_TEMPLATE.format(id=news_id)
        items.append(NewsItem(source="Innovent", title=title, url=url))
    if not items:
        raise RuntimeError("No Innovent news items were returned by the API.")
    return items


def load_state(path: Path) -> tuple[bool, dict]:
    if not path.exists():
        return False, {"seen_by_source": {}, "last_slot_key": None}

    raw_state = json.loads(path.read_text(encoding="utf-8"))
    seen_by_source = raw_state.get("seen_by_source")
    if not isinstance(seen_by_source, dict):
        seen_by_source = {}

    # Backward compatibility with the older Pfizer-only state structure.
    legacy_pfizer_urls = raw_state.get("seen_urls", [])
    if legacy_pfizer_urls and "Pfizer" not in seen_by_source:
        seen_by_source["Pfizer"] = legacy_pfizer_urls

    normalized = {
        "last_scan_bjt": raw_state.get("last_scan_bjt"),
        "last_slot_key": raw_state.get("last_slot_key"),
        "seen_by_source": {
            str(source): sorted({str(url) for url in urls})
            for source, urls in seen_by_source.items()
            if isinstance(urls, list)
        },
    }
    return True, normalized


def save_state(path: Path, seen_by_source: dict[str, set[str]], last_slot_key: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pfizer_urls = sorted(seen_by_source.get("Pfizer", set()))
    payload = {
        "last_scan_bjt": scan_time_label(),
        "last_slot_key": last_slot_key,
        "seen_urls": pfizer_urls,
        "seen_by_source": {
            source: sorted(urls)
            for source, urls in sorted(seen_by_source.items())
        },
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


def current_slot_key(now: datetime) -> str | None:
    if now.weekday() > 4:
        return None
    if now.hour not in SLOT_HOURS_BJT:
        return None
    return now.strftime("%Y-%m-%d") + f"-{now.hour:02d}"


def should_force_scan() -> bool:
    return os.getenv("FORCE_SCAN", "").lower() in {"1", "true", "yes"}


def build_report_text(items: list[NewsItem], translated_titles: dict[str, str], label: str) -> str:
    lines = [
        f"扫描时间：{label}",
        "",
        f"本次扫描发现 {len(items)} 条新动态：",
        "",
    ]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. 来源：{item.source}")
        lines.append(f"   中文标题：{translated_titles[item.url]}")
        lines.append(f"   原始标题：{item.title}")
        lines.append(f"   网页地址：{item.url}")
        lines.append("")
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


def build_document_xml(items: list[NewsItem], translated_titles: dict[str, str], label: str) -> str:
    body_parts = [
        paragraph_xml("企业新闻监测更新", "Title"),
        paragraph_xml(f"扫描时间：{label}", "Subtitle"),
        paragraph_xml(f"本次扫描发现 {len(items)} 条新动态。"),
    ]
    for index, item in enumerate(items, start=1):
        body_parts.extend(
            [
                paragraph_xml(f"{index}. {item.source}", "Heading1"),
                paragraph_xml(f"中文标题：{translated_titles[item.url]}"),
                paragraph_xml(f"原始标题：{item.title}"),
                hyperlink_paragraph_xml(item.url, f"rId{index + 1}"),
            ]
        )

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
    for index, item in enumerate(items, start=1):
        relationships.append(
            f'<Relationship Id="rId{index + 1}" '
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
        docx.writestr("word/document.xml", build_document_xml(items, translated_titles, label))
        docx.writestr("word/_rels/document.xml.rels", build_relationships_xml(items))
        docx.writestr("word/styles.xml", styles_xml())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = bjt_now()
    forced = should_force_scan()

    state_exists, state = load_state(args.state)
    slot_key = current_slot_key(now)
    if not forced:
        if slot_key is None:
            print("Current Beijing time is outside the scheduled scan windows.")
            return 0
        if state.get("last_slot_key") == slot_key:
            print(f"Scan already completed for Beijing slot {slot_key}.")
            return 0

    seen_by_source = {
        source: set(urls)
        for source, urls in state.get("seen_by_source", {}).items()
    }

    first_run_should_report = (
        os.getenv("REPORT_ON_FIRST_RUN", "").lower() in {"1", "true", "yes"}
        or os.getenv("SEND_ON_FIRST_RUN", "").lower() in {"1", "true", "yes"}
    )

    current_items = fetch_pfizer_items() + fetch_astrazeneca_items() + fetch_innovent_items()
    current_by_source: dict[str, set[str]] = {}
    for item in current_items:
        current_by_source.setdefault(item.source, set()).add(item.url)

    roche_urls = fetch_roche_urls()
    current_by_source["Roche"] = set(roche_urls)

    uninitialized_sources = {
        source for source in current_by_source if source not in seen_by_source
    }
    baseline_new_sources_only = state_exists and not first_run_should_report

    new_items = [
        item
        for item in current_items
        if item.url not in seen_by_source.get(item.source, set())
        and not (baseline_new_sources_only and item.source in uninitialized_sources)
    ]
    new_roche_urls = [
        url
        for url in roche_urls
        if url not in seen_by_source.get("Roche", set())
        and not (baseline_new_sources_only and "Roche" in uninitialized_sources)
    ]
    new_items.extend(fetch_roche_item(url) for url in new_roche_urls)

    merged_seen_by_source = {
        source: set(seen_by_source.get(source, set())) | urls
        for source, urls in current_by_source.items()
    }
    for source, urls in seen_by_source.items():
        merged_seen_by_source.setdefault(source, set()).update(urls)

    effective_slot_key = slot_key or state.get("last_slot_key")

    if not state_exists and not first_run_should_report:
        print("State file does not exist. Initializing baseline without generating a Word document.")
        if not args.dry_run:
            save_state(args.state, merged_seen_by_source, effective_slot_key)
        return 0

    if baseline_new_sources_only and uninitialized_sources:
        print(
            "Initialized baseline for newly added sources: "
            + ", ".join(sorted(uninitialized_sources))
        )

    if not new_items:
        print("No new items found across the monitored sites.")
        if not args.dry_run:
            save_state(args.state, merged_seen_by_source, effective_slot_key)
        return 0

    label = scan_time_label()
    translated_titles = {item.url: translate_title(item.title) for item in new_items}
    report_text = build_report_text(new_items, translated_titles, label)
    filename_label = bjt_now().strftime("%Y%m%d-%H%M%S")
    output_path = args.output_dir / f"{REPORT_PREFIX}-{filename_label}.docx"

    print(f"Found {len(new_items)} new item(s).")
    print(report_text)

    if not args.dry_run:
        create_word_document(output_path, new_items, translated_titles, label)
        print(f"Word document generated: {output_path}")
        save_state(args.state, merged_seen_by_source, effective_slot_key)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
