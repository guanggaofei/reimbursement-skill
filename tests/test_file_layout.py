from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "reimbursement" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _pathutil import INTERNAL_DIR, resolve_path  # noqa: E402
from apply_invoice_fixes import _check  # noqa: E402
from apply_match_actions import ActionError, slot_counts, validate_unique_slots  # noqa: E402
from generate_reimbursement_xlsx import build_rows, first_item_quantity  # noqa: E402
from merge_output_pdfs import (  # noqa: E402
    A4_HEIGHT,
    A4_WIDTH,
    DEFAULT_DPI,
    SIGNATURE_LINE_LENGTH,
    add_image_page,
    add_invoice_header,
    center_image_on_a4,
    collect_pdfs,
    invoice_sequence,
    render_pdf_pages,
)
from merge_output_pdfs import collect_pdfs, invoice_sequence  # noqa: E402
from verify_screenshot_coverage import build_issue_summary  # noqa: E402


class PathLayoutTests(unittest.TestCase):
    def test_relative_and_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(resolve_path(root, INTERNAL_DIR / "x.json"), (root / INTERNAL_DIR / "x.json").resolve())
            absolute = (root / "outside.json").resolve()
            self.assertEqual(resolve_path(root, absolute), absolute)

    def test_pdf_classification_only_includes_material_and_taxi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            expected = []
            for folder, filename in [
                ("1_材料费", "10.pdf"),
                ("2_打车费", "2.pdf"),
                ("3_高价发票", "3.pdf"),
                ("4_辰景发票", "4.pdf"),
            ]:
                path = output / folder / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"pdf")
                if folder in {"1_材料费", "2_打车费"}:
                    expected.append(path)
            self.assertEqual(set(collect_pdfs(output)), set(expected))

    def test_super_invoice_output_contract_is_unchanged(self) -> None:
        source = (SCRIPTS / "super_invoice.py").read_text(encoding="utf-8")
        for folder in ("1_材料费", "2_打车费", "3_高价发票", "4_辰景发票"):
            self.assertIn(f'"{folder}"', source)
        self.assertIn('r / "invoice_results.json"', source)
        self.assertIn('root / "invoice_results_sorted.json"', source)
        self.assertIn('root / "invoice_errors.json"', source)
        self.assertIn('root / "output"', source)

    def test_internal_defaults_are_under_work_directory(self) -> None:
        expected_sources = {
            "check_invoice_errors.py": 'INTERNAL_DIR / "invoice_errors_raw.json"',
            "apply_invoice_fixes.py": 'INTERNAL_DIR / "invoice_fixes.json"',
            "extract_trip_sheets.py": 'INTERNAL_DIR / "行程单数据.json"',
            "organize_expense_records.py": 'INTERNAL_DIR / "支出记录OCR匹配明细.md"',
            "generate_expense_record_docx.py": 'INTERNAL_DIR / "支出记录DOCX生成结果.md"',
            "dump_ocr_cache.py": 'INTERNAL_DIR / "OCR缓存原文.md"',
            "generate_payment_record_docx.py": 'INTERNAL_DIR / "支付记录/xxx_17-24_支付记录.docx"',
            "generate_payment_explanations.py": 'INTERNAL_DIR / "支付说明"',
        }
        for filename, declaration in expected_sources.items():
            with self.subTest(filename=filename):
                source = (SCRIPTS / filename).read_text(encoding="utf-8")
                self.assertIn(declaration, source)


class ReimbursementXlsxTests(unittest.TestCase):
    @patch("generate_reimbursement_xlsx.subprocess.run")
    def test_first_item_quantity_truncates_decimal(self, run: unittest.mock.Mock) -> None:
        run.return_value = CompletedProcess(
            args=[],
            returncode=0,
            stdout="项目名称              数 量       单价\n螺丝                  2.9        10.00\n合计\n",
            stderr="",
        )

        self.assertEqual(first_item_quantity(Path("example.pdf")), Decimal("2"))

    @patch("generate_reimbursement_xlsx.first_item_quantity", return_value=Decimal("2"))
    def test_build_rows_sets_quantity_and_unit_price(self, _quantity: unittest.mock.Mock) -> None:
        invoices = [{
            "文件名": "example.pdf",
            "更新后文件名": "1_example.pdf",
            "价税合计金额": 39.04,
            "发票号码": "123",
            "行程单文件名": "无需",
            "项目列表": [{"项目名称": "螺丝"}],
        }]

        rows = build_rows(Path("."), invoices, {})

        self.assertEqual(rows[0]["quantity"], "2")
        self.assertEqual(rows[0]["unit_price"], "19.520000")

    @patch("generate_reimbursement_xlsx.first_item_quantity", return_value=Decimal("1"))
    def test_build_rows_rejects_unit_price_over_1000(self, _quantity: unittest.mock.Mock) -> None:
        invoices = [{
            "文件名": "example.pdf",
            "更新后文件名": "1_example.pdf",
            "价税合计金额": 1000.01,
            "发票号码": "123",
            "行程单文件名": "无需",
            "项目列表": [{"项目名称": "螺丝"}],
        }]

        with self.assertRaises(RuntimeError):
            build_rows(Path("."), invoices, {})


class InvoiceFixPathTests(unittest.TestCase):
    def test_dot_notation_is_the_standard_nested_field_format(self) -> None:
        result = {
            "发票信息": [{
                "文件名": "example.pdf",
                "项目列表": [
                    {"项目名称": "旧名称", "单价": 1.0},
                    *[{"项目名称": f"项目{i}", "单价": float(i)} for i in range(1, 13)],
                ],
            }]
        }
        fixes = {
            "example.pdf": {
                "项目列表.0.项目名称": "新名称",
                "项目列表.12.单价": 12.5,
            }
        }

        self.assertEqual(_check(result, fixes), 1)
        items = result["发票信息"][0]["项目列表"]
        self.assertEqual(items[0]["项目名称"], "新名称")
        self.assertEqual(items[12]["单价"], 12.5)

    def test_dot_notation_keeps_nested_price_validation(self) -> None:
        result = {
            "发票信息": [{
                "文件名": "example.pdf",
                "项目列表": [{"项目名称": "项目", "单价": 1.0}],
            }]
        }
        fixes = {"example.pdf": {"项目列表.0.单价": -1}}

        with self.assertRaisesRegex(AssertionError, "金额为负"):
            _check(result, fixes)


class MatchActionValidationTests(unittest.TestCase):
    def test_existing_duplicate_slot_does_not_block_unrelated_action(self) -> None:
        record = {
            "发票映射": {
                "invoices/example.pdf": {
                    "支付记录": ["images/a.png", "images/b.png"],
                    "账单截图": [],
                    "行程明细": [],
                }
            }
        }
        existing_counts = slot_counts(record)

        validate_unique_slots(record, {"agent": "fix-trip-ambiguity"}, existing_counts)

    def test_new_duplicate_slot_is_rejected(self) -> None:
        record = {
            "发票映射": {
                "invoices/example.pdf": {
                    "支付记录": ["images/a.png"],
                    "账单截图": [],
                    "行程明细": [],
                }
            }
        }
        existing_counts = slot_counts(record)
        record["发票映射"]["invoices/example.pdf"]["支付记录"].append("images/b.png")

        with self.assertRaises(ActionError):
            validate_unique_slots(record, {"agent": "fix-trip-ambiguity"}, existing_counts)


class ScreenshotIssueSummaryTests(unittest.TestCase):
    def test_issue_summary_counts_repeatable_categories(self) -> None:
        record = {
            "未匹配截图": [
                {"原因": "金额对应多个候选发票，交由后处理视觉识别"},
                {"原因": "金额对应多个候选行程，交由后处理视觉识别"},
                {"原因": "与截图 x.png 同时匹配同一发票，需人工识别"},
                {"原因": "金额不匹配任何发票或打车行程"},
            ]
        }

        summary = build_issue_summary(record, [object()], [(object(), "支付记录")], [object()])  # type: ignore[list-item]

        self.assertEqual(summary["店铺名称歧义"], 1)
        self.assertEqual(summary["行程歧义"], 1)
        self.assertEqual(summary["重复截图"], 1)
        self.assertEqual(summary["完全无截图发票"], 1)
        self.assertEqual(summary["截图不完整发票"], 1)
        self.assertEqual(summary["缺失行程截图"], 1)
        self.assertEqual(summary["未匹配截图总数"], 4)


class MergedPdfLayoutTests(unittest.TestCase):
    def test_default_rasterization_is_400_dpi(self) -> None:
        self.assertEqual(DEFAULT_DPI, 400)

    def test_invoice_sequence_excludes_trip_sheet(self) -> None:
        self.assertEqual(invoice_sequence(Path("47_价税合计_25_00_发票.pdf")), 47)
        self.assertIsNone(invoice_sequence(Path("47_价税合计_25_00_行程单.pdf")))

    def test_header_has_sequence_and_two_three_centimeter_lines(self) -> None:
        from PIL import Image

        image = Image.new("RGB", (600, 300), "white")
        add_invoice_header(image, 47, content_top=200, dpi=72)
        line_y = 176
        black_pixels = [x for x in range(image.width) if image.getpixel((x, line_y)) == (0, 0, 0)]
        runs = []
        for x in black_pixels:
            if not runs or x > runs[-1][1] + 1:
                runs.append([x, x])
            else:
                runs[-1][1] = x
        signature_lines = [run for run in runs if run[1] - run[0] >= 80]

        self.assertEqual(len(signature_lines), 2)
        for start, end in signature_lines:
            self.assertEqual(end - start, round(SIGNATURE_LINE_LENGTH))

    def test_pdftoppm_renders_the_cropbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "page-1.png").write_bytes(b"rendered")
            with (
                patch("merge_output_pdfs.shutil.which", return_value="/usr/bin/pdftoppm"),
                patch("merge_output_pdfs.subprocess.run") as run,
            ):
                run.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
                pages = render_pdf_pages(Path("invoice.pdf"), output_dir, dpi=200)

            command = run.call_args.args[0]
            self.assertEqual(pages, [output_dir / "page-1.png"])
            self.assertIn("-cropbox", command)
            self.assertEqual(command[command.index("-r") + 1], "200")

    def test_pdftoppm_font_failure_stops_the_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("merge_output_pdfs.shutil.which", return_value="pdftoppm"),
                patch("merge_output_pdfs.subprocess.run") as run,
            ):
                run.return_value = CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="",
                    stderr="Syntax Error: Couldn't create a font for 'SimSun'",
                )
                with self.assertRaisesRegex(SystemExit, "could not render fonts"):
                    render_pdf_pages(Path("invoice.pdf"), Path(tmp), dpi=400)

    def test_output_page_contains_one_raster_image_on_exact_a4(self) -> None:
        from PIL import Image
        from pypdf import PdfReader, PdfWriter

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "source.png"
            Image.new("RGB", (600, 400), "red").save(source_path)
            image = center_image_on_a4(source_path, margin_x=0, margin_y=72, dpi=72, sequence=None)
            writer = PdfWriter()
            add_image_page(writer, image, jpeg_quality=92)
            image.close()
            output_path = root / "output.pdf"
            with output_path.open("wb") as output:
                writer.write(output)

            page = PdfReader(output_path).pages[0]
            xobjects = page["/Resources"]["/XObject"]
            images = [ref.get_object() for ref in xobjects.values() if ref.get_object()["/Subtype"] == "/Image"]
            operators = [operator for _, operator in page.get_contents().operations]

            self.assertAlmostEqual(float(page.mediabox.width), A4_WIDTH, places=5)
            self.assertAlmostEqual(float(page.mediabox.height), A4_HEIGHT, places=5)
            self.assertEqual(len(images), 1)
            self.assertEqual(operators, [b"q", b"cm", b"Do", b"Q"])


if __name__ == "__main__":
    unittest.main()
