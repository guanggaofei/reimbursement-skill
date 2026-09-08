#!/usr/bin/env python3
"""Generate the expense-record DOCX table from 匹配记录.json."""

from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree
from PIL import Image

from _pathutil import INTERNAL_DIR, add_root_arg, resolve_path
from _invoice_filters import ordinary_invoices as filter_ordinary_invoices
from _matching_records import DEFAULT_MATCH_RECORD, image_paths, invoice_images, invoice_key, load_match_record


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
}

REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
EMU_PER_CM = 360000
IMAGE_WIDTH_CM = 9

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


DEFAULT_TEMPLATE = template_path("Hello World 2026支出记录模板V1.0.docx")


@dataclass
class RowImages:
    bills: list[Path] = field(default_factory=list)
    payments: list[Path] = field(default_factory=list)


def read_zip(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path, "r") as zin:
        return {name: zin.read(name) for name in zin.namelist()}


def write_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, data in files.items():
            zout.writestr(name, data)


def ordinary_invoices(invoice_json: Path) -> list[dict]:
    """走普通线上流程的发票：排除大额发票，保留辰景发票。"""
    data = json.loads(invoice_json.read_text(encoding="utf-8"))
    invoices = data.get("发票信息", [])
    if not isinstance(invoices, list) or not invoices:
        raise RuntimeError(f"no invoice list found in {invoice_json}")
    return filter_ordinary_invoices(invoices)


def invoice_count(invoice_json: Path) -> int:
    return len(ordinary_invoices(invoice_json))


def find_table(root: etree._Element) -> etree._Element:
    tables = root.xpath(".//w:tbl", namespaces=NS)
    if not tables:
        raise RuntimeError("no table found in document.xml")
    return tables[0]


def table_rows(table: etree._Element) -> list[etree._Element]:
    rows = table.xpath("./w:tr", namespaces=NS)
    if len(rows) < 3:
        raise RuntimeError("expected at least one header row and two data rows")
    return rows


def has_images(row: etree._Element) -> bool:
    return bool(row.xpath(".//a:blip", namespaces=NS))


def next_relationship_id(rels_root: etree._Element) -> int:
    max_id = 0
    for rel in rels_root:
        rel_id = rel.get("Id", "")
        if rel_id.startswith("rId") and rel_id[3:].isdigit():
            max_id = max(max_id, int(rel_id[3:]))
    return max_id + 1


def image_aspect_from_paragraph(paragraph: etree._Element) -> float:
    extents = paragraph.xpath(".//wp:extent", namespaces=NS)
    if not extents:
        raise RuntimeError("template row does not contain image sizing")
    cx = int(extents[0].get("cx"))
    cy = int(extents[0].get("cy"))
    return cy / cx


def image_aspect_from_file(path: Path, fallback: float) -> float:
    try:
        with Image.open(path) as image:
            width, height = image.size
        if width > 0 and height > 0:
            return height / width
    except Exception:
        pass
    return fallback


def images_from_match_record(root_dir: Path, match_record: Path, invoice_json: Path) -> tuple[dict[int, RowImages], list[str]]:
    invoices = ordinary_invoices(invoice_json)
    rows: dict[int, RowImages] = {index: RowImages() for index in range(1, len(invoices) + 1)}
    skipped: list[str] = []
    record = load_match_record(match_record)
    mapping = record.get("发票映射", {})

    for order, inv in enumerate(invoices, start=1):
        entry = mapping.get(invoice_key(str(inv.get("文件名") or "")), {})
        bills_rel = invoice_images(entry, "账单截图")
        payments_rel = invoice_images(entry, "支付记录")
        bills = image_paths(root_dir, bills_rel)
        payments = image_paths(root_dir, payments_rel)
        missing = set(bills_rel + payments_rel) - {path.relative_to(root_dir).as_posix() for path in bills + payments}
        for item in sorted(missing):
            skipped.append(f"{item}: file not found")
        rows[order].bills.extend(bills)
        rows[order].payments.extend(payments)
    return rows, skipped


def add_content_type(files: dict[str, bytes], extension: str) -> None:
    ext = extension.lower()
    content_type = mimetypes.types_map.get(f".{ext}")
    if ext in ("jpg", "jpeg"):
        content_type = "image/jpeg"
    elif ext == "png":
        content_type = "image/png"
    if not content_type:
        raise RuntimeError(f"unsupported image extension: {extension}")

    root = etree.fromstring(files["[Content_Types].xml"])
    default_tag = f"{{{CT_NS}}}Default"
    for default in root.findall(default_tag):
        if default.get("Extension") == ext:
            return
    default = etree.SubElement(root, default_tag)
    default.set("Extension", ext)
    default.set("ContentType", content_type)
    files["[Content_Types].xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def add_image_relationship(
    files: dict[str, bytes],
    rels_root: etree._Element,
    image_path: Path,
    rel_id_number: int,
    media_number: int,
) -> tuple[str, int, int]:
    ext = image_path.suffix.lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"
    add_content_type(files, ext)

    rel_id = f"rId{rel_id_number}"
    target = f"media/expense_record_{media_number}.{ext}"
    rel = etree.SubElement(rels_root, f"{{{REL_NS}}}Relationship")
    rel.set("Id", rel_id)
    rel.set("Type", IMAGE_REL_TYPE)
    rel.set("Target", target)
    files[f"word/{target}"] = image_path.read_bytes()
    return rel_id, rel_id_number + 1, media_number + 1


def blank_paragraph() -> etree._Element:
    paragraph = etree.Element(f"{{{NS['w']}}}p")
    ppr = etree.SubElement(paragraph, f"{{{NS['w']}}}pPr")
    jc = etree.SubElement(ppr, f"{{{NS['w']}}}jc")
    jc.set(f"{{{NS['w']}}}val", "center")
    return paragraph


def clear_cell(cell: etree._Element) -> None:
    tc_pr = cell.find(f"{{{NS['w']}}}tcPr")
    for child in list(cell):
        if child is not tc_pr:
            cell.remove(child)
    cell.append(blank_paragraph())


def source_picture_paragraph(row_template: etree._Element, cell_index: int) -> etree._Element:
    cells = row_template.xpath("./w:tc", namespaces=NS)
    if len(cells) <= cell_index:
        raise RuntimeError("template row has fewer than three cells")
    paragraphs = cells[cell_index].xpath("./w:p[.//a:blip]", namespaces=NS)
    if not paragraphs:
        raise RuntimeError("template image cell does not contain a picture paragraph")
    return paragraphs[0]


def load_picture_sources(
    template_rows: list[etree._Element], picture_template: Path
) -> tuple[etree._Element, etree._Element]:
    filled_rows = [row for row in template_rows[1:] if has_images(row)]
    if filled_rows:
        source_row = filled_rows[1] if len(filled_rows) > 1 else filled_rows[0]
        return source_picture_paragraph(source_row, 1), source_picture_paragraph(source_row, 2)

    if not picture_template.exists():
        raise RuntimeError(
            "current template contains no embedded picture XML and picture template is missing"
        )
    picture_files = read_zip(picture_template)
    picture_root = etree.fromstring(picture_files["word/document.xml"])
    picture_table = find_table(picture_root)
    picture_rows = table_rows(picture_table)
    picture_filled_rows = [row for row in picture_rows[1:] if has_images(row)]
    if not picture_filled_rows:
        raise RuntimeError("picture template does not contain embedded picture XML")
    source_row = picture_filled_rows[1] if len(picture_filled_rows) > 1 else picture_filled_rows[0]
    return source_picture_paragraph(source_row, 1), source_picture_paragraph(source_row, 2)


def picture_paragraph(
    source: etree._Element,
    rel_id: str,
    width_emu: int,
    height_emu: int,
    picture_id: int,
) -> etree._Element:
    paragraph = copy.deepcopy(source)
    for blip in paragraph.xpath(".//a:blip", namespaces=NS):
        blip.set(f"{{{NS['r']}}}embed", rel_id)
    for extent in paragraph.xpath(".//wp:extent", namespaces=NS):
        extent.set("cx", str(width_emu))
        extent.set("cy", str(height_emu))
    for extent in paragraph.xpath(".//pic:spPr/a:xfrm/a:ext", namespaces=NS):
        extent.set("cx", str(width_emu))
        extent.set("cy", str(height_emu))
    for doc_pr in paragraph.xpath(".//wp:docPr", namespaces=NS):
        doc_pr.set("id", str(picture_id))
        doc_pr.set("name", f"Picture {picture_id}")
    return paragraph


def set_cell_pictures(
    cell: etree._Element,
    source_paragraph: etree._Element,
    image_paths: list[Path],
    files: dict[str, bytes],
    rels_root: etree._Element,
    rel_id_number: int,
    media_number: int,
    picture_id: int,
    width_emu: int,
    height_emu: int,
) -> tuple[int, int, int, list[str]]:
    tc_pr = cell.find(f"{{{NS['w']}}}tcPr")
    for child in list(cell):
        if child is not tc_pr:
            cell.remove(child)

    inserted: list[str] = []
    if not image_paths:
        cell.append(blank_paragraph())
        return rel_id_number, media_number, picture_id, inserted

    for image_path in image_paths:
        image_height_emu = round(width_emu * image_aspect_from_file(image_path, height_emu / width_emu))
        rel_id, rel_id_number, media_number = add_image_relationship(
            files, rels_root, image_path, rel_id_number, media_number
        )
        cell.append(
            picture_paragraph(
                source_paragraph, rel_id, width_emu, image_height_emu, picture_id
            )
        )
        picture_id += 1
        inserted.append(image_path.name)
    return rel_id_number, media_number, picture_id, inserted


def generate(args: argparse.Namespace) -> None:
    root_dir = args.root.resolve()
    template = resolve_path(root_dir, Path(args.template))
    match_record = resolve_path(root_dir, Path(args.match_record))
    invoice_json = resolve_path(root_dir, Path(args.invoice_json))
    picture_template = resolve_path(root_dir, Path(args.picture_template))
    output = resolve_path(root_dir, Path(args.output))
    report = resolve_path(root_dir, Path(args.report))

    count = invoice_count(invoice_json)
    files = read_zip(template)
    doc_root = etree.fromstring(files["word/document.xml"])
    rels_root = etree.fromstring(files["word/_rels/document.xml.rels"])
    table = find_table(doc_root)
    rows = table_rows(table)

    filled_rows = [row for row in rows[1:] if has_images(row)]
    row_template = (
        filled_rows[1]
        if len(filled_rows) > 1
        else filled_rows[0]
        if filled_rows
        else rows[1]
    )
    bill_source, payment_source = load_picture_sources(rows, picture_template)

    for row in rows[1:]:
        table.remove(row)

    if not match_record.exists():
        raise RuntimeError(f"match record not found: {match_record}")
    images_by_order, skipped = images_from_match_record(root_dir, match_record, invoice_json)
    width_emu = IMAGE_WIDTH_CM * EMU_PER_CM
    height_emu = round(width_emu * image_aspect_from_paragraph(bill_source))
    rel_id_number = next_relationship_id(rels_root)
    media_number = 1
    picture_id = 100
    inserted: list[str] = []
    empty_rows: list[int] = []

    for order in range(1, count + 1):
        row = copy.deepcopy(row_template)
        cells = row.xpath("./w:tc", namespaces=NS)
        clear_cell(cells[1])
        clear_cell(cells[2])

        row_images = images_by_order[order]
        rel_id_number, media_number, picture_id, bill_names = set_cell_pictures(
            cells[1],
            bill_source,
            row_images.bills,
            files,
            rels_root,
            rel_id_number,
            media_number,
            picture_id,
            width_emu,
            height_emu,
        )
        rel_id_number, media_number, picture_id, payment_names = set_cell_pictures(
            cells[2],
            payment_source,
            row_images.payments,
            files,
            rels_root,
            rel_id_number,
            media_number,
            picture_id,
            width_emu,
            height_emu,
        )
        table.append(row)

        names = bill_names + payment_names
        if names:
            inserted.append(f"{order}: {', '.join(names)}")
        else:
            empty_rows.append(order)

    files["word/document.xml"] = etree.tostring(
        doc_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    files["word/_rels/document.xml.rels"] = etree.tostring(
        rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    write_zip(output, files)

    image_count = sum(
        len(row_images.bills) + len(row_images.payments)
        for row_images in images_by_order.values()
    )
    lines = [
        "# 支出记录 DOCX 生成结果",
        "",
        f"- 模板: `{template}`",
        f"- 图片XML模板: `{picture_template}`",
        f"- 输出: `{output}`",
        f"- 发票条数: {count}",
        f"- 表格数据行: {count}",
        f"- 写入图片: {image_count}",
        f"- 图片宽度: {IMAGE_WIDTH_CM}cm",
        "",
        "## 写入记录",
    ]
    lines.extend(f"- {item}" for item in inserted)
    lines.extend(["", "## 无图片行"])
    lines.extend(f"- {order}" for order in empty_rows)
    lines.extend(["", "## 跳过文件"])
    lines.extend(f"- {item}" for item in skipped)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"output={output}")
    print(f"invoice_rows={count} images={image_count} empty_rows={len(empty_rows)} skipped={len(skipped)}")
    print(f"report={report}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_root_arg(parser)
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--match-record", default=DEFAULT_MATCH_RECORD)
    parser.add_argument("--invoice-json", default="invoice_results_sorted.json")
    parser.add_argument("--picture-template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", default="Hello World 2026支出记录填写结果.docx")
    parser.add_argument("--report", default=INTERNAL_DIR / "支出记录DOCX生成结果.md")
    return parser.parse_args()


if __name__ == "__main__":
    generate(parse_args())
