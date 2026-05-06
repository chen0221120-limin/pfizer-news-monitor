#!/usr/bin/env python3
"""Monitor Pfizer newsroom and generate a Word document for new items."""

from __future__ import annotations

import argparse
import html
import http.client
import json
import os
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


NEWSROOM_URL = "https://www.pfizer.com/newsroom"
STATE_PATH = Path(".state/pfizer_news_seen.json")
REPORT_DIR = Path("reports")
ARTICLE_PATH_MARKERS = (
    "/news/press-release/",
    "/news/articles/",
    "/news/announcements/",
    "/news/updates-and-statements/",
    "/news/partnering-news/",
)


@dataclass(frozen=True)
class NewsItem:
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
                "Mozilla/5.0 (compatible; PfizerNewsMonitor/1.0; "
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


def normalize_url(href: str) -> str:
    absolute = urljoin(NEWSROOM_URL, href)
    parsed = urlparse(absolute)
    return parsed._replace(query="", fragment="").geturl()


def is_news_article(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != "www.pfizer.com":
        return False
    return any(marker in parsed.path for marker in ARTICLE_PATH_MARKERS)


def fetch_news_items() -> list[NewsItem]:
    parser = LinkCollector()
    parser.feed(fetch_text(NEWSROOM_URL))

    seen_urls: set[str] = set()
    items: list[NewsItem] = []
    for href, title in parser.links:
        url = normalize_url(href)
        if not is_news_article(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(NewsItem(title=title, url=url))
    if not items:
        raise RuntimeError("No Pfizer newsroom article links were found.")
    return items


def load_state(path: Path) -> tuple[bool, dict]:
    if not path.exists():
        return False, {"seen_urls": []}
    return True, json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, urls: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_scan_bjt": scan_time_label(),
        "seen_urls": sorted(set(urls)),
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
        request = Request(endpoint, headers={"User-Agent": "PfizerNewsMonitor/1.0"})
        with urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        translated = data.get("responseData", {}).get("translatedText", "").strip()
        return html.unescape(translated) if translated else title
    except (OSError, URLError, json.JSONDecodeError):
        return title


def build_report_text(items: list[NewsItem], translated_titles: dict[str, str], label: str) -> str:
    lines = [
        f"扫描时间：{label}",
        "",
        "发现 Pfizer Newsroom 新动态：",
        "",
    ]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {translated_titles[item.url]}")
        lines.append(f"   原标题：{item.title}")
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
        paragraph_xml("Pfizer Newsroom 新动态", "Title"),
        paragraph_xml(f"扫描时间：{label}", "Subtitle"),
        paragraph_xml(f"本次扫描发现 {len(items)} 条新动态。"),
    ]
    for index, item in enumerate(items, start=1):
        body_parts.extend(
            [
                paragraph_xml(f"{index}. {translated_titles[item.url]}", "Heading1"),
                paragraph_xml(f"原标题：{item.title}"),
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

    state_exists, state = load_state(args.state)
    seen_urls = set(state.get("seen_urls", []))
    current_items = fetch_news_items()
    current_urls = {item.url for item in current_items}
    new_items = [item for item in current_items if item.url not in seen_urls]

    first_run_should_report = (
        os.getenv("REPORT_ON_FIRST_RUN", "").lower() in {"1", "true", "yes"}
        or os.getenv("SEND_ON_FIRST_RUN", "").lower() in {"1", "true", "yes"}
    )
    if not state_exists and not first_run_should_report:
        print("State file does not exist. Initializing baseline without generating a Word document.")
        save_state(args.state, current_urls)
        return 0

    if not new_items:
        print("No new Pfizer newsroom items found.")
        return 0

    label = scan_time_label()
    translated_titles = {item.url: translate_title(item.title) for item in new_items}
    report_text = build_report_text(new_items, translated_titles, label)
    filename_label = bjt_now().strftime("%Y%m%d-%H%M%S")
    output_path = args.output_dir / f"pfizer-news-{filename_label}.docx"

    print(f"Found {len(new_items)} new item(s).")
    print(report_text)

    if not args.dry_run:
        create_word_document(output_path, new_items, translated_titles, label)
        print(f"Word document generated: {output_path}")

    save_state(args.state, seen_urls | current_urls)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
