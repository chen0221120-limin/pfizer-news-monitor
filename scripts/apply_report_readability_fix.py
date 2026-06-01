#!/usr/bin/env python3
"""Make the generated Word report easier to read."""

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


REPORT_BLOCK = '''def finding_source_type(finding: Finding) -> str:
    text = f"{finding.url}\\n{finding.evidence}".lower()
    if any(term in text for term in ("publication", "poster", "abstract", "presentation", ".pdf", "journal", "conference")):
        return "出版物 / 会议摘要"
    if any(term in text for term in ("press", "release", "news")):
        return "新闻稿 / 公司新闻"
    return "网页更新"


def report_value(values: list[str]) -> str:
    return "；".join(values) if values else ""


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
            source_type = finding_source_type(finding)
            title = finding.title or "未提取到明确标题"
            body.append(paragraph_xml(f"{item_number}. [{source_type}] {title}", "Heading2"))
            body.append(paragraph_xml(f"发布日期：{finding.published_on.isoformat()}"))
            product_text = report_value(finding.matched_products)
            context_text = report_value(finding.matched_context)
            target_text = report_value(finding.matched_targets)
            trial_text = report_value(finding.matched_trials)
            if product_text:
                body.append(paragraph_xml(f"相关产品：{product_text}"))
            if context_text:
                body.append(paragraph_xml(f"相关疾病/领域：{context_text}"))
            if target_text:
                body.append(paragraph_xml(f"相关靶点：{target_text}"))
            if trial_text:
                body.append(paragraph_xml(f"临床试验编号：{trial_text}"))
            body.append(hyperlink_paragraph_xml("打开原文", finding.url, f"rId{rel_index}"))
            rel_index += 1
    else:
        body.append(paragraph_xml("本次扫描近3天内未发现命中动态。", "Heading1"))
        body.append(paragraph_xml("如后续出现命中，正文将按公司展示标题、类型、发布日期、相关产品/疾病和原文链接。"))

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


def main() -> int:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    start = "def finding_source_type" if "def finding_source_type" in text else "def build_document_xml"
    text = replace_block(text, start, "def build_relationships_xml", REPORT_BLOCK)
    SCRIPT_PATH.write_text(text, encoding="utf-8", newline="\n")
    print("Report readability fix applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
