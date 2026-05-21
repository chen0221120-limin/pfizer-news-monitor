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


'''


def main() -> int:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    text = replace_block(text, "def parse_date_text", "def append_unique", DATE_BLOCK)
    SCRIPT_PATH.write_text(text, encoding="utf-8", newline="\n")
    print("Date parsing fix applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
