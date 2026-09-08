#!/usr/bin/env python3
"""Generate payment explanation DOCX files from invoice warning JSON.

The template is an existing .docx file. This script keeps the original DOCX
package and XML structure, and only edits the specific text nodes that need to
change. It intentionally avoids python-docx for writing because high-level DOCX
writers may merge or rebuild runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from xml.dom import minidom
from xml.sax.saxutils import escape

from _pathutil import INTERNAL_DIR, add_root_arg, resolve_path
from _invoice_filters import is_high_value
from _matching_records import DEFAULT_MATCH_RECORD, invoice_key, load_match_record


SCRIPT_ROOT = Path(__file__).resolve().parents[1]


def template_path(name: str) -> Path:
    candidates = [
        SCRIPT_ROOT / "assets/templates" / name,
        SCRIPT_ROOT / "reimbursement/assets/templates" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


DEFAULT_TEMPLATE = template_path("xxx_xxx_支付说明.docx")
DEFAULT_ERRORS = Path("invoice_errors.json")
DEFAULT_RESULTS = Path("invoice_results_sorted.json")
DEFAULT_OUTPUT_DIR = INTERNAL_DIR / "支付说明"
DEFAULT_UNPACK_DIR = INTERNAL_DIR / "支付说明/docx_unpacked"

DEFAULT_REASON = "淘宝购买，该公司收款方名为"
SELLER_PAYEE_MAP = {
    "安庆市固基五金有限公司": "固万**店",
    "深圳市硅智科技有限公司": "优信**店",
}

TEMPLATE_DATE_XML = (
    '<w:t>填制日期：</w:t></w:r><w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr>'
    '<w:t xml:space="preserve"> </w:t></w:r><w:r><w:t>202</w:t></w:r>'
    '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr><w:t>5</w:t></w:r>'
    '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr><w:t>年</w:t></w:r>'
    '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr><w:t>10</w:t></w:r>'
    '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr><w:t>月</w:t></w:r>'
    '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr><w:t>26</w:t></w:r>'
    '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr><w:t>日</w:t></w:r>'
)

TEMPLATE_CONTENT_EMPTY_P = (
    '<w:p w14:paraId="7A1302D8" w14:textId="226AD8EA" w:rsidR="00834383" '
    'w:rsidRDefault="00834383"><w:pPr><w:pStyle w:val="a9"/>'
    '<w:spacing w:before="0" w:beforeAutospacing="0" w:after="0" '
    'w:afterAutospacing="0" w:line="500" w:lineRule="exact"/>'
    '<w:jc w:val="center"/></w:pPr></w:p>'
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money_text(value: Decimal) -> str:
    return f"{value:.2f}"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 occurrence, found {count}")
    return text.replace(old, new, 1)


def replace_count(text: str, old: str, new: str, expected: int, label: str) -> str:
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{label}: expected {expected} occurrences, found {count}")
    return text.replace(old, new)


def amount_to_chinese(value: Decimal) -> str:
    digits = "零壹贰叁肆伍陆柒捌玖"
    units = ["", "拾", "佰", "仟"]
    groups = ["", "万", "亿"]

    integer = int(value)
    cents = int((value - Decimal(integer)) * 100)
    jiao, fen = divmod(cents, 10)

    def group_to_cn(num: int) -> str:
        if num == 0:
            return ""
        chars: list[str] = []
        zero = False
        for pos in range(3, -1, -1):
            factor = 10**pos
            digit = num // factor
            num %= factor
            if digit:
                if zero:
                    chars.append("零")
                    zero = False
                chars.append(digits[digit] + units[pos])
            elif chars:
                zero = True
        return "".join(chars)

    if integer == 0:
        result = "零元"
    else:
        group_values: list[int] = []
        n = integer
        while n:
            group_values.append(n % 10000)
            n //= 10000
        parts: list[str] = []
        pending_zero = False
        for idx in range(len(group_values) - 1, -1, -1):
            group = group_values[idx]
            if group == 0:
                pending_zero = bool(parts)
                continue
            if pending_zero or (parts and group < 1000):
                parts.append("零")
            parts.append(group_to_cn(group) + groups[idx])
            pending_zero = False
        result = "".join(parts) + "元"

    if jiao == 0 and fen == 0:
        return result + "整"
    if jiao:
        result += digits[jiao] + "角"
    elif integer:
        result += "零"
    if fen:
        result += digits[fen] + "分"
    return result


def invoice_display_index(inv: dict[str, Any]) -> int:
    name = str(inv.get("更新后文件名") or "")
    match = re.match(r"(\d+)_", name)
    if match:
        return int(match.group(1))
    return int(inv["发票序号"]) + 1


def simplify_invoice_content(invoices: list[dict[str, Any]], max_chars: int = 8) -> str:
    names = []
    for inv in invoices:
        for item in inv.get("项目列表", []) or []:
            name = str(item.get("项目名称") or "").strip()
            if name:
                names.append(name)
    if not names:
        return "材料费"[:max_chars]

    common_prefix = names[0]
    for name in names[1:]:
        while common_prefix and not name.startswith(common_prefix):
            common_prefix = common_prefix[:-1]
    common_prefix = re.sub(r"[【\\[（(].*$", "", common_prefix).strip()

    merged = "".join(names)
    if len(common_prefix) >= 4 and common_prefix != "金属制品":
        content = common_prefix
    else:
        if "螺丝" in merged or "螺栓" in merged or "螺母" in merged:
            content = "金属制品螺丝"
        elif "铝" in merged:
            content = "金属制品铝材"
        elif "金属" in merged:
            content = "金属制品"
        else:
            content = re.sub(r"[【\\[（(].*$", "", names[0]).strip() or "材料费"

    content = content.replace(" ", "")
    return content[:max_chars]


def build_date_xml(year: int, month: int, day: int) -> str:
    century = year // 10
    last = year % 10
    return (
        '<w:t>填制日期：</w:t></w:r><w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr>'
        '<w:t xml:space="preserve"> </w:t></w:r>'
        f"<w:r><w:t>{century}</w:t></w:r>"
        '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr>'
        f"<w:t>{last}</w:t></w:r>"
        '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr><w:t>年</w:t></w:r>'
        '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr>'
        f"<w:t>{month}</w:t></w:r>"
        '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr>'
        "<w:t>月</w:t></w:r>"
        '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr>'
        f"<w:t>{day}</w:t></w:r>"
        '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr><w:t>日</w:t></w:r>'
    )


def build_content_p(content: str) -> str:
    content = escape(content)
    return (
        '<w:p w14:paraId="7A1302D8" w14:textId="226AD8EA" w:rsidR="00834383" '
        'w:rsidRDefault="00834383"><w:pPr><w:pStyle w:val="a9"/>'
        '<w:spacing w:before="0" w:beforeAutospacing="0" w:after="0" '
        'w:afterAutospacing="0" w:line="500" w:lineRule="exact"/>'
        '<w:jc w:val="center"/></w:pPr>'
        '<w:r><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr>'
        f"<w:t>{content}</w:t></w:r></w:p>"
    )


def split_company_name(name: str) -> tuple[str, str, str]:
    if name.endswith("有限公司") and len(name) > 7:
        prefix = name[:3]
        suffix = "有限公司"
        middle = name[3 : -len(suffix)]
        return prefix, middle, suffix
    if len(name) <= 3:
        return name, "", ""
    return name[:3], name[3:], ""


def split_payee(payee: str) -> tuple[str, str, str]:
    if "**" in payee:
        left, right = payee.split("**", 1)
        return left, "**", right
    if len(payee) <= 2:
        return payee, "", ""
    return payee[:2], payee[2:-1], payee[-1]


def extract_payee_from_ocr(
    invoices: list[dict[str, Any]],
    match_record: Path,
    ocr_cache: dict[str, Any],
    root: Path,
) -> str | None:
    """Try to auto-detect the payee name from payment-record OCR text.

    For each invoice, finds its payment record screenshot in 匹配记录.json,
    reads the OCR text from OCR缓存.json by original image path (fallback: SHA256),
    then scans for the first masked payee line.  Returns the payee name if all
    invoices agree on the same value, otherwise ``None``.
    """
    record = load_match_record(match_record)
    mapping = record.get("发票映射", {})
    payees: list[str] = []
    for inv in invoices:
        rec_entry = mapping.get(invoice_key(str(inv.get("文件名") or "")), {})
        for image_rel in rec_entry.get("支付记录", []) or []:
            image_path = root / image_rel
            entry = ocr_cache.get(image_rel)
            if entry is None and image_path.exists():
                sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
                entry = ocr_cache.get(sha)
            if entry is None:
                continue
            text: str = entry.get("ocr_text", "")
            for line in text.splitlines():
                stripped = line.strip()
                if "**" in stripped:
                    payees.append(stripped)
                    break
                if "*" in stripped and len(stripped) <= 10:
                    payees.append(stripped)
                    break
            if payees:
                break

    if not payees:
        return None
    if len(set(payees)) == 1:
        return payees[0]
    return None


def make_docx(
    *,
    template: Path,
    output: Path,
    fill_date: tuple[int, int, int],
    content: str,
    amount: Decimal,
    seller: str,
    payee: str,
    reason_prefix: str,
) -> None:
    if len(content) > 8:
        raise ValueError(f"发票内容超过8个字：{content}")

    with zipfile.ZipFile(template) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")

    xml = replace_once(xml, TEMPLATE_DATE_XML, build_date_xml(*fill_date), "fill date")
    xml = replace_once(xml, TEMPLATE_CONTENT_EMPTY_P, build_content_p(content), "invoice content")
    xml = replace_once(
        xml,
        "<w:t>贰佰贰拾伍元叁角贰分</w:t>",
        f"<w:t>{escape(amount_to_chinese(amount))}</w:t>",
        "amount uppercase",
    )
    xml = replace_once(xml, "<w:t>225.32</w:t>", f"<w:t>{money_text(amount)}</w:t>", "amount numeric")

    seller_a, seller_b, seller_c = (escape(part) for part in split_company_name(seller))
    xml = replace_once(xml, "<w:t>深圳市</w:t>", f"<w:t>{seller_a}</w:t>", "seller prefix")
    xml = replace_once(xml, "<w:t>硅智科技</w:t>", f"<w:t>{seller_b}</w:t>", "seller middle")
    xml = replace_once(xml, "<w:t>有限公司</w:t>", f"<w:t>{seller_c}</w:t>", "seller suffix")

    payee_a, payee_b, payee_c = (escape(part) for part in split_payee(payee))
    xml = replace_count(xml, "<w:t>优信</w:t>", f"<w:t>{payee_a}</w:t>", 2, "payee prefix")
    xml = replace_count(xml, "<w:t>**</w:t>", f"<w:t>{payee_b}</w:t>", 2, "payee mask")
    xml = replace_count(xml, "<w:t>店</w:t>", f"<w:t>{payee_c}</w:t>", 2, "payee suffix")
    xml = replace_once(xml, "<w:t>淘宝购买</w:t>", f"<w:t>{escape(reason_prefix[:4])}</w:t>", "reason first")
    xml = replace_once(
        xml,
        "<w:t>，该公司收款方名</w:t>",
        f"<w:t>{escape(reason_prefix[4:12])}</w:t>",
        "reason second",
    )
    xml = replace_once(xml, "<w:t>为</w:t>", f"<w:t>{escape(reason_prefix[12:])}</w:t>", "reason third")

    with zipfile.ZipFile(template, "r") as zin, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "word/document.xml":
                data = xml.encode("utf-8")
            zout.writestr(info, data)


def warning_groups(errors: dict[str, Any], invoices_by_source: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []

    # 连号组必须整组提交，每一张都要有支付说明和支付记录，因此不在组内剔除任何发票。
    # 大额发票本来也进不了连号组：super_invoice 的连号检查会跳过它们。
    for group in errors.get("连号发票", []) or []:
        invoices = [invoices_by_source[item["文件名"]] for item in group.get("所有重复发票", [])]
        if not invoices:
            continue
        if any(inv.get("行程单文件名") != "无需" for inv in invoices):
            continue
        display_indexes = [invoice_display_index(inv) for inv in invoices]
        groups.append({"kind": "连号发票", "invoices": invoices, "indexes": display_indexes})

    grouped_files = {inv["文件名"] for group in groups for inv in group["invoices"]}
    for category, entries in errors.items():
        if category == "连号发票":
            continue
        for item in entries or []:
            reason = str(item.get("问题原因") or "")
            if "支付说明" not in reason or "支付记录" not in reason:
                continue
            filename = item.get("文件名")
            if filename not in invoices_by_source or filename in grouped_files:
                continue
            inv = invoices_by_source[filename]
            # 单张入口要剔除大额发票：检查5（价税合计超1000元）不跳过它们，
            # 但大额发票走单价大额发票汇总表，不需要支付说明与支付记录。
            if inv.get("行程单文件名") != "无需" or is_high_value(inv):
                continue
            groups.append({"kind": category, "invoices": [inv], "indexes": [invoice_display_index(inv)]})

    return groups


def output_name(group: dict[str, Any]) -> str:
    indexes = sorted(group["indexes"])
    if len(indexes) == 1:
        index_part = str(indexes[0])
    elif indexes == list(range(indexes[0], indexes[-1] + 1)):
        index_part = f"{indexes[0]}-{indexes[-1]}"
    else:
        parts: list[str] = []
        start = end = indexes[0]
        for value in indexes[1:]:
            if value == end + 1:
                end = value
            else:
                parts.append(str(start) if start == end else f"{start}-{end}")
                start = end = value
        parts.append(str(start) if start == end else f"{start}-{end}")
        index_part = "&".join(parts)
    return f"xxx_{index_part}_支付说明.docx"


def refresh_unpack(template: Path, generated: list[Path], unpack_dir: Path) -> None:
    if unpack_dir.exists():
        shutil.rmtree(unpack_dir)
    (unpack_dir / "template").mkdir(parents=True)
    with zipfile.ZipFile(template) as zf:
        zf.extractall(unpack_dir / "template")
    raw_template = zipfile.ZipFile(template).read("word/document.xml")
    (unpack_dir / "template_document_pretty.xml").write_text(
        minidom.parseString(raw_template).toprettyxml(indent="  "),
        encoding="utf-8",
    )

    for path in generated:
        target = unpack_dir / path.stem
        target.mkdir(parents=True)
        with zipfile.ZipFile(path) as zf:
            zf.extractall(target)
        raw = zipfile.ZipFile(path).read("word/document.xml")
        (unpack_dir / f"{path.stem}_document_pretty.xml").write_text(
            minidom.parseString(raw).toprettyxml(indent="  "),
            encoding="utf-8",
        )


def write_report(path: Path, generated: list[dict[str, Any]]) -> None:
    lines = ["# 支付说明生成结果", ""]
    if not generated:
        lines.append("未发现需要生成支付说明的发票。")
    for item in generated:
        lines.extend(
            [
                f"## {item['path'].name}",
                "",
                f"- 类型：{item['kind']}",
                f"- 发票序号：{item['index_text']}",
                f"- 销售方：{item['seller']}",
                f"- 收款方：{item['payee']}",
                f"- 发票内容：{item['content']}",
                f"- 金额：{money_text(item['amount'])}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate payment explanation DOCX files.")
    add_root_arg(parser)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--errors", type=Path, default=DEFAULT_ERRORS)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--unpack-dir", type=Path, default=DEFAULT_UNPACK_DIR)
    parser.add_argument("--date", default="2026-6-28", help="Fill date as YYYY-M-D.")
    parser.add_argument("--payee", action="append", default=[], help="Seller=Payee mapping; may be repeated.")
    parser.add_argument("--reason-prefix", default=DEFAULT_REASON)
    parser.add_argument("--report", type=Path, default=Path("支付说明生成结果.md"))
    parser.add_argument("--ocr-cache", type=Path, default=Path("OCR缓存.json"), help="OCR cache JSON (keyed by images/<原图片名>)")
    parser.add_argument("--match-record", type=Path, default=DEFAULT_MATCH_RECORD)
    parser.add_argument("--no-unpack", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    args.template = resolve_path(root, args.template)
    args.errors = resolve_path(root, args.errors)
    args.results = resolve_path(root, args.results)
    args.output_dir = resolve_path(root, args.output_dir)
    args.unpack_dir = resolve_path(root, args.unpack_dir)
    args.report = resolve_path(root, args.report)
    args.ocr_cache = resolve_path(root, args.ocr_cache)
    args.match_record = resolve_path(root, args.match_record)
    year, month, day = (int(part) for part in args.date.split("-"))
    payee_map = dict(SELLER_PAYEE_MAP)
    for entry in args.payee:
        if "=" not in entry:
            raise ValueError(f"--payee must be Seller=Payee: {entry}")
        seller, payee = entry.split("=", 1)
        payee_map[seller] = payee

    ocr_cache = read_json(args.ocr_cache) if args.ocr_cache.exists() else {}

    results = read_json(args.results)
    errors = read_json(args.errors)
    invoices = results.get("发票信息", [])
    invoices_by_source = {inv["文件名"]: inv for inv in invoices}
    groups = warning_groups(errors, invoices_by_source)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    generated_docs: list[Path] = []
    generated_report: list[dict[str, Any]] = []
    skipped_groups: list[dict[str, Any]] = []

    for group in groups:
        group_invoices = group["invoices"]
        sellers = Counter(str(inv.get("销售方名称") or "") for inv in group_invoices)
        seller = sellers.most_common(1)[0][0]
        payee = payee_map.get(seller, "")
        if not payee:
            payee = extract_payee_from_ocr(group_invoices, args.match_record, ocr_cache, root)
            if payee:
                payee_map[seller] = payee
                print(f"[auto-detect] {seller} → {payee}")
            else:
                indexes = sorted(group["indexes"])
                idx_str = f"{indexes[0]}-{indexes[-1]}" if len(indexes) > 1 else str(indexes[0])
                amount_text = money_text(sum((money(inv.get("价税合计金额", 0)) for inv in group_invoices), Decimal("0.00")))
                print(f"[跳过] 无法自动识别收款方：{seller}（发票序号 {idx_str}，合计 {amount_text} 元）")
                print(f"  如需生成支付说明，请使用 --payee '{seller}=收款方'")
                skipped_groups.append({
                    "seller": seller,
                    "indexes": indexes,
                    "amount": amount_text,
                    "reason": "OCR缓存中未找到收款方名称（无**掩码行），用户未指定 --payee",
                })
                continue
        amount = sum((money(inv.get("价税合计金额", 0)) for inv in group_invoices), Decimal("0.00"))
        content = simplify_invoice_content(group_invoices, max_chars=8)
        out_path = args.output_dir / output_name(group)
        make_docx(
            template=args.template,
            output=out_path,
            fill_date=(year, month, day),
            content=content,
            amount=amount,
            seller=seller,
            payee=payee,
            reason_prefix=args.reason_prefix,
        )
        generated_docs.append(out_path)
        indexes = sorted(group["indexes"])
        generated_report.append(
            {
                "path": out_path,
                "kind": group["kind"],
                "index_text": f"{indexes[0]}-{indexes[-1]}" if len(indexes) > 1 else str(indexes[0]),
                "seller": seller,
                "payee": payee,
                "content": content,
                "amount": amount,
            }
        )

    if generated_docs and not args.no_unpack:
        refresh_unpack(args.template, generated_docs, args.unpack_dir)
    if generated_report or skipped_groups:
        write_report(args.report, generated_report)
    elif args.report.exists():
        args.report.unlink()

    for item in generated_report:
        print(f"generated {item['path']} amount={money_text(item['amount'])} content={item['content']}")

    if skipped_groups:
        print("\n===== 跳过的组（未生成支付说明）=====")
        print(f"{'销售方':<30} {'发票序号':<12} {'金额':<10} {'原因'}")
        print("-" * 80)
        for sg in skipped_groups:
            idx_str = f"{sg['indexes'][0]}-{sg['indexes'][-1]}" if len(sg['indexes']) > 1 else str(sg['indexes'][0])
            print(f"{sg['seller']:<30} {idx_str:<12} {sg['amount']:<10} {sg['reason']}")

        report_path = args.report
        if report_path.exists():
            lines = report_path.read_text(encoding="utf-8").splitlines()
            lines.extend(["", "## 跳过的组", "", "| 销售方 | 发票序号 | 金额 | 原因 |", "|------|---------|-----|------|"])
            for sg in skipped_groups:
                idx_str = f"{sg['indexes'][0]}-{sg['indexes'][-1]}" if len(sg['indexes']) > 1 else str(sg['indexes'][0])
                lines.append(f"| {sg['seller']} | {idx_str} | {sg['amount']} | {sg['reason']} |")
            report_path.write_text("\n".join(lines), encoding="utf-8")

    if not generated_report and not skipped_groups:
        print("no payment explanations needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
