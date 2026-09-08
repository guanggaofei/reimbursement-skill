#!/usr/bin/env python3
"""Collect 大额发票 (单价>1000, 非辰景) material for the 单价大额发票汇总表.

Per 《报销指南》 section 2, invoices with a unit price above 1000 元 are handled
as 设备费 and leave the ordinary flow entirely — they appear in neither the
报账单, the 支出记录, nor the offline print PDF.  Instead the user fills a row
in the 飞书 spreadsheet 单价大额发票汇总表 and attaches three files per invoice.

Outputs (project root, only when at least one 大额发票 exists)
    大额发票/                     renamed PDF + 订单截图 + 支付记录
    单价大额发票汇总表.xlsx        one row per invoice, columns match the 飞书 table
    大额发票生成结果.md            per-invoice manifest and missing-screenshot report

Naming follows the guide's example ``奶龙_2222_大额发票.pdf``.  The 姓名 field
stays as the ``xxx`` placeholder used elsewhere in this skill (支付记录/支付说明
docx do the same); the user renames the files and fills the 姓名 column before
uploading.  When one invoice matches several screenshots of the same kind the
files get ``_1``/``_2`` suffixes and the spreadsheet cell lists all of them.

Missing screenshots are reported but never fatal — screenshot gaps are already
converged by the coverage check in step 5 and announced in step 6, so raising
here would only stall the pipeline a second time.
"""

from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from _pathutil import add_root_arg, resolve_path
from _invoice_filters import high_unit_price_count, high_value_invoices, max_high_unit_price
from _matching_records import (
    DEFAULT_MATCH_RECORD,
    image_paths,
    invoice_images,
    invoice_key,
    load_match_record,
)


DEFAULT_SORTED_JSON = Path("invoice_results_sorted.json")
DEFAULT_OUTPUT_DIR = Path("大额发票")
DEFAULT_SUMMARY_XLSX = Path("单价大额发票汇总表.xlsx")
DEFAULT_REPORT_MD = Path("大额发票生成结果.md")

NAME_PLACEHOLDER = "xxx"
MISSING_TEXT = "缺失"

COLUMNS = [
    "姓名",
    "报账单编号（若无）",
    "单价",
    "支付金额",
    "发票号码",
    "发票文件",
    "订单截图",
    "支付记录",
    "处理反馈",
]

# 订单截图 in the 飞书 table is what 匹配记录.json calls 账单截图.
KINDS = [("账单截图", "订单截图"), ("支付记录", "支付记录")]


def money_text(value: Any) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def base_name(amount_text: str) -> str:
    return f"{NAME_PLACEHOLDER}_{amount_text}_大额发票"


def copy_screenshots(
    root: Path,
    entry: dict[str, Any],
    record_kind: str,
    label: str,
    stem: str,
    output_dir: Path,
) -> tuple[list[str], list[str]]:
    """Copy every screenshot of one kind, returning (written names, warnings)."""
    relative = invoice_images(entry, record_kind)
    sources = image_paths(root, relative)
    warnings = [
        f"{item}: 文件不存在"
        for item in sorted(set(relative) - {path.relative_to(root).as_posix() for path in sources})
    ]

    written: list[str] = []
    for index, source in enumerate(sources, start=1):
        # 单张不加后缀（与指南示例一致），多张才编号。
        suffix = "" if len(sources) == 1 else f"_{index}"
        target_name = f"{stem}_{label}{suffix}{source.suffix.lower()}"
        shutil.copy2(source, output_dir / target_name)
        written.append(target_name)
    return written, warnings


def build_entries(root: Path, sorted_json: Path, match_record: Path, output_dir: Path) -> list[dict[str, Any]]:
    data = json.loads(sorted_json.read_text(encoding="utf-8"))
    invoices = high_value_invoices(data.get("发票信息", []) or [])
    if not invoices:
        return []

    record = load_match_record(match_record)
    mapping = record.get("发票映射", {})
    output_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    for inv in invoices:
        source_name = str(inv.get("文件名") or "")
        amount_text = money_text(inv.get("价税合计金额"))
        stem = base_name(amount_text)
        notes: list[str] = []

        source_pdf = root / "invoices" / source_name
        pdf_name = f"{stem}.pdf"
        if source_pdf.exists():
            shutil.copy2(source_pdf, output_dir / pdf_name)
        else:
            pdf_name = MISSING_TEXT
            notes.append(f"源发票 PDF 不存在：invoices/{source_name}")

        invoice_entry = mapping.get(invoice_key(source_name), {})
        files: dict[str, list[str]] = {}
        for record_kind, label in KINDS:
            written, warnings = copy_screenshots(
                root, invoice_entry, record_kind, label, stem, output_dir
            )
            files[label] = written
            notes.extend(warnings)
            if not written:
                notes.append(f"缺少{label}")

        unit_price = max_high_unit_price(inv)
        over_count = high_unit_price_count(inv)
        if over_count > 1:
            notes.append(f"有 {over_count} 个项目单价超过 1000 元，「单价」列取其中最大值")

        entries.append({
            "源文件": source_name,
            "发票文件": pdf_name,
            "单价": money_text(unit_price) if unit_price is not None else MISSING_TEXT,
            "支付金额": amount_text,
            "发票号码": str(inv.get("发票号码") or ""),
            "订单截图": files["订单截图"],
            "支付记录": files["支付记录"],
            "备注": notes,
        })
    return entries


def cell_text(entry: dict[str, Any], label: str) -> str:
    names = entry[label]
    return "、".join(names) if names else MISSING_TEXT


def summary_rows(entries: list[dict[str, Any]]) -> list[list[str]]:
    rows = [COLUMNS]
    for entry in entries:
        rows.append([
            NAME_PLACEHOLDER,
            # 大额发票不进报账单，因此该列恒为「无」。
            "无",
            entry["单价"],
            entry["支付金额"],
            entry["发票号码"],
            entry["发票文件"],
            cell_text(entry, "订单截图"),
            cell_text(entry, "支付记录"),
            # 处理反馈由用户在飞书上填写（已提交/已开款/留空）。
            "",
        ])
    return rows


def column_ref(index: int) -> str:
    """1-based column index to spreadsheet letters (1 -> A)."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def sheet_xml(rows: list[list[str]]) -> bytes:
    """Build sheet1.xml with every cell as an inline string.

    Inline strings avoid a sharedStrings part entirely, and keeping the invoice
    number as text stops Excel from rewriting long digit runs in scientific
    notation — the same reason generate_reimbursement_xlsx.py writes it as text.
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        f'<dimension ref="A1:{column_ref(len(COLUMNS))}{len(rows)}"/>',
        "<sheetData>",
    ]
    for row_index, row in enumerate(rows, start=1):
        lines.append(f'<row r="{row_index}">')
        for column_index, value in enumerate(row, start=1):
            ref = f"{column_ref(column_index)}{row_index}"
            if value == "":
                lines.append(f'<c r="{ref}" t="inlineStr"/>')
            else:
                lines.append(
                    f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                    f"{escape(value)}</t></is></c>"
                )
        lines.append("</row>")
    lines.extend(["</sheetData>", "</worksheet>"])
    return "".join(lines).encode("utf-8")


def write_summary_xlsx(path: Path, rows: list[list[str]]) -> None:
    """Write a minimal single-sheet xlsx without adding an openpyxl dependency."""
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ).encode("utf-8"),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ).encode("utf-8"),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ).encode("utf-8"),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ).encode("utf-8"),
        "xl/worksheets/sheet1.xml": sheet_xml(rows),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, data in parts.items():
            zout.writestr(name, data)


def build_report_md(entries: list[dict[str, Any]], output_dir: Path, summary: Path) -> str:
    lines = [
        "# 大额发票生成结果",
        "",
        "单价超过 1000 元的发票按设备费处理，**不参与报账单、支出记录和线下打印**。",
        f"请把 `{output_dir.name}/` 中文件名里的 `{NAME_PLACEHOLDER}` 改成自己的姓名，",
        f"再按 `{summary.name}` 的内容填写飞书上的「单价大额发票汇总表」并上传对应文件。",
        "",
        "## 产物清单",
        "",
        "| 源发票 | 发票文件 | 单价 | 支付金额 | 订单截图 | 支付记录 |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for entry in entries:
        lines.append(
            f"| `{entry['源文件']}` | `{entry['发票文件']}` | {entry['单价']} | "
            f"{entry['支付金额']} | {cell_text(entry, '订单截图')} | {cell_text(entry, '支付记录')} |"
        )

    problems = [(entry, note) for entry in entries for note in entry["备注"]]
    lines.extend(["", "## 待处理问题", ""])
    if problems:
        lines.extend(["| 源发票 | 问题 |", "| --- | --- |"])
        for entry, note in problems:
            lines.append(f"| `{entry['源文件']}` | {note} |")
    else:
        lines.append("无。所有大额发票的 PDF、订单截图和支付记录均已齐备。")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_root_arg(parser)
    parser.add_argument("--sorted-json", type=Path, default=DEFAULT_SORTED_JSON)
    parser.add_argument("--match-record", type=Path, default=DEFAULT_MATCH_RECORD)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY_XLSX)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_MD)
    args = parser.parse_args()

    root = args.root.resolve()
    sorted_json = resolve_path(root, args.sorted_json)
    match_record = resolve_path(root, args.match_record)
    output_dir = resolve_path(root, args.output_dir)
    summary = resolve_path(root, args.summary)
    report = resolve_path(root, args.report)

    if not sorted_json.exists():
        raise SystemExit(f"sorted JSON not found: {sorted_json}")
    if not match_record.exists():
        raise SystemExit(f"match record not found: {match_record}")

    entries = build_entries(root, sorted_json, match_record, output_dir)
    if not entries:
        print("no 大额发票 found; nothing written")
        return 0

    write_summary_xlsx(summary, summary_rows(entries))
    report.write_text(build_report_md(entries, output_dir, summary), encoding="utf-8")

    missing = sum(1 for entry in entries if entry["备注"])
    print(f"wrote={output_dir} invoices={len(entries)} summary={summary} report={report}")
    if missing:
        print(f"[WARNING] {missing} 张大额发票存在缺失项，详见 {report.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
