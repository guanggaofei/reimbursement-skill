from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "reimbursement" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _pathutil import INTERNAL_DIR, resolve_path  # noqa: E402
from _invoice_filters import (  # noqa: E402
    high_value_invoices,
    is_chenjing,
    is_high_value,
    is_material_fee,
    is_transport_fee,
    is_unclassified,
    ordinary_invoices,
)
from apply_invoice_fixes import _check  # noqa: E402
from apply_match_actions import ActionError, slot_counts, validate_unique_slots  # noqa: E402
from generate_payment_explanations import warning_groups  # noqa: E402
from generate_payment_record_docx import auto_collect_groups_from_record  # noqa: E402
from generate_reimbursement_xlsx import build_rows, first_item_quantity  # noqa: E402
from generate_high_value_invoices import (  # noqa: E402
    COLUMNS,
    build_entries,
    summary_rows,
    write_summary_xlsx,
)
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
                ("5_未匹配", "5.pdf"),
            ]:
                path = output / folder / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"pdf")
                if folder in {"1_材料费", "2_打车费"}:
                    expected.append(path)
            self.assertEqual(set(collect_pdfs(output)), set(expected))

    def test_super_invoice_output_contract_is_unchanged(self) -> None:
        source = (SCRIPTS / "super_invoice.py").read_text(encoding="utf-8")
        for folder in ("1_材料费", "2_打车费", "3_高价发票", "4_辰景发票", "5_未匹配"):
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

    @patch("generate_reimbursement_xlsx.first_item_quantity", return_value=Decimal("1"))
    def test_filtering_high_value_keeps_build_rows_from_raising(self, _quantity: unittest.mock.Mock) -> None:
        """报账单先过滤大额发票，1000 元断言因此只是兜底而不是常规路径。"""
        ordinary = {
            "文件名": "material.pdf",
            "更新后文件名": "1_material.pdf",
            "价税合计金额": 86.7,
            "发票号码": "111",
            "行程单文件名": "无需",
            "购买方名称": "浙江大学",
            "项目列表": [{"项目名称": "螺丝", "单价": 85.84}],
        }
        big = {
            "文件名": "big.pdf",
            "更新后文件名": "2_big.pdf",
            "价税合计金额": 5999.0,
            "发票号码": "222",
            "行程单文件名": "无需",
            "购买方名称": "浙江大学",
            "项目列表": [{"项目名称": "计算机", "单价": 5999.0}],
        }

        with self.assertRaises(RuntimeError):
            build_rows(Path("."), [ordinary, big], {})

        rows = build_rows(Path("."), ordinary_invoices([ordinary, big]), {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["invoice_no"], "111")


class InvoiceCategoryTests(unittest.TestCase):
    """五类分类判定互斥且完备 —— super_invoice 与下游生成器共用同一组判定。"""

    CATEGORIES = [
        ("1_材料费", is_material_fee),
        ("2_打车费", is_transport_fee),
        ("3_高价发票", is_high_value),
        ("4_辰景发票", is_chenjing),
        ("5_未匹配", is_unclassified),
    ]

    @staticmethod
    def _invoice(name: str, buyer: str, items: list[tuple[str, object]]) -> dict:
        return {
            "文件名": name,
            "购买方名称": buyer,
            "项目列表": [{"项目名称": item, "单价": price} for item, price in items],
        }

    def _category(self, inv: dict) -> str:
        hits = [name for name, predicate in self.CATEGORIES if predicate(inv)]
        self.assertEqual(len(hits), 1, f"expected exactly one category, got {hits}")
        return hits[0]

    def test_every_invoice_lands_in_exactly_one_category(self) -> None:
        cases = [
            ("普通材料费", self._invoice("a.pdf", "浙江大学", [("航模配件", 85.84)]), "1_材料费"),
            # 混价发票必须走大额通道：归成材料费会被打印却没有报账单行，
            # 导致其后所有材料费发票的纸质序号错位。
            ("混价发票", self._invoice("b.pdf", "浙江大学", [("螺丝", 800), ("电机", 1500)]), "3_高价发票"),
            # 阈值严格大于 1000，正好 1000 仍是材料费。
            ("单价正好1000", self._invoice("c.pdf", "浙江大学", [("螺丝", 800), ("件", 1000)]), "1_材料费"),
            # 人工修正 ERROR 字段时常留下引号，带引号的数字必须与裸数字同样分类。
            ("字符串单价超额", self._invoice("d.pdf", "浙江大学", [("电机", "1500")]), "3_高价发票"),
            ("字符串单价正常", self._invoice("e.pdf", "浙江大学", [("配件", "85.84")]), "1_材料费"),
            ("打车费", self._invoice("打车f.pdf", "浙江大学", [("运输服务", 10.1)]), "2_打车费"),
            ("辰景发票即使高价", self._invoice("g.pdf", "杭州辰景信息咨询有限公司", [("货运", 5999)]), "4_辰景发票"),
            ("非辰景住宿", self._invoice("h.pdf", "浙江大学", [("住宿服务", 300)]), "5_未匹配"),
            ("项目提取失败", self._invoice("i.pdf", "浙江大学", [("ERROR", "ERROR")]), "5_未匹配"),
            ("单价无法解析", self._invoice("j.pdf", "浙江大学", [("配件", "ERROR")]), "5_未匹配"),
            ("辰景提取失败", self._invoice("k.pdf", "杭州辰景信息咨询有限公司", [("ERROR", "ERROR")]), "4_辰景发票"),
        ]
        for label, invoice, expected in cases:
            with self.subTest(label):
                self.assertEqual(self._category(invoice), expected)

    def test_mixed_price_invoice_leaves_the_ordinary_flow(self) -> None:
        mixed = self._invoice("b.pdf", "浙江大学", [("螺丝", 800), ("电机", 1500)])
        material = self._invoice("a.pdf", "浙江大学", [("航模配件", 85.84)])

        self.assertEqual(ordinary_invoices([material, mixed]), [material])
        self.assertEqual(high_value_invoices([material, mixed]), [mixed])

    def test_super_invoice_uses_the_shared_predicates(self) -> None:
        """分类必须 import 共用判定，不得重新实现，否则两套判据会再次漂移。"""
        source = (SCRIPTS / "super_invoice.py").read_text(encoding="utf-8")
        self.assertIn("from _invoice_filters import", source)
        for name in ("is_material_fee", "is_transport_fee", "is_high_value", "is_unclassified"):
            self.assertNotIn(f"def {name}(", source)


class InvoiceFilterTests(unittest.TestCase):
    """大额发票离开普通流程，辰景发票留在普通流程。"""

    @staticmethod
    def _invoice(name: str, buyer: str, prices: list) -> dict:
        return {
            "文件名": name,
            "购买方名称": buyer,
            "项目列表": [{"项目名称": "件", "单价": price} for price in prices],
        }

    def test_high_value_excludes_chenjing_and_keeps_ordinary(self) -> None:
        material = self._invoice("material.pdf", "浙江大学", [85.84])
        big = self._invoice("big.pdf", "浙江大学", [5999.0])
        chenjing = self._invoice("chenjing.pdf", "杭州辰景信息咨询有限公司", [5999.0])

        self.assertFalse(is_high_value(material))
        self.assertTrue(is_high_value(big))
        # 辰景发票即使单价超过 1000 也不算大额发票，它走自己的电子发票通道。
        self.assertFalse(is_high_value(chenjing))

        invoices = [material, big, chenjing]
        self.assertEqual(
            [inv["文件名"] for inv in ordinary_invoices(invoices)],
            ["material.pdf", "chenjing.pdf"],
        )
        self.assertEqual([inv["文件名"] for inv in high_value_invoices(invoices)], ["big.pdf"])

    def test_unparseable_unit_price_is_not_high_value(self) -> None:
        for price in ("ERROR", None, ""):
            with self.subTest(price=price):
                self.assertFalse(is_high_value(self._invoice("x.pdf", "浙江大学", [price])))

    def test_threshold_is_strictly_greater_than_1000(self) -> None:
        self.assertFalse(is_high_value(self._invoice("x.pdf", "浙江大学", [1000.0])))
        self.assertTrue(is_high_value(self._invoice("x.pdf", "浙江大学", [1000.01])))


class HighValueInvoiceTests(unittest.TestCase):
    SORTED_JSON = {
        "发票信息": [
            {
                "文件名": "material.pdf",
                "更新后文件名": "1_价税合计_86_70_发票.pdf",
                "购买方名称": "浙江大学",
                "发票号码": "111",
                "价税合计金额": 86.7,
                "行程单文件名": "无需",
                "项目列表": [{"项目名称": "航模配件", "单价": 85.84}],
            },
            {
                "文件名": "big.pdf",
                "更新后文件名": "2_价税合计_5999_00_发票.pdf",
                "购买方名称": "浙江大学",
                "发票号码": "222",
                "价税合计金额": 5999.0,
                "行程单文件名": "无需",
                "项目列表": [{"项目名称": "计算机", "单价": 5999.0}],
            },
        ]
    }

    def _project(self, root: Path, bills: list[str], payments: list[str]) -> tuple[Path, Path]:
        (root / "invoices").mkdir()
        (root / "images").mkdir()
        for name in ("material.pdf", "big.pdf"):
            (root / "invoices" / name).write_bytes(b"%PDF-1.4\n")
        for name in bills + payments:
            (root / "images" / name).write_bytes(b"\x89PNG\r\n")

        sorted_json = root / "invoice_results_sorted.json"
        sorted_json.write_text(json.dumps(self.SORTED_JSON, ensure_ascii=False), encoding="utf-8")

        match_record = root / "匹配记录.json"
        match_record.write_text(
            json.dumps(
                {
                    "版本": 2,
                    "发票映射": {
                        "invoices/big.pdf": {
                            "发票文件": "invoices/big.pdf",
                            "支付记录": [f"images/{name}" for name in payments],
                            "账单截图": [f"images/{name}" for name in bills],
                            "行程明细": [],
                            "购买日期": "2026-04-01",
                        }
                    },
                    "未匹配截图": [],
                    "忽略截图": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return sorted_json, match_record

    def test_single_screenshot_gets_no_index_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sorted_json, match_record = self._project(root, ["b.png"], ["p.png"])
            output_dir = root / "大额发票"

            entries = build_entries(root, sorted_json, match_record, output_dir)

            self.assertEqual([entry["源文件"] for entry in entries], ["big.pdf"])
            self.assertEqual(entries[0]["订单截图"], ["xxx_5999.00_大额发票_订单截图.png"])
            self.assertEqual(entries[0]["支付记录"], ["xxx_5999.00_大额发票_支付记录.png"])
            self.assertEqual(entries[0]["单价"], "5999.00")
            self.assertEqual(entries[0]["备注"], [])
            self.assertTrue((output_dir / "xxx_5999.00_大额发票.pdf").exists())

    def test_multiple_screenshots_get_index_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sorted_json, match_record = self._project(root, ["b1.png", "b2.png"], ["p.png"])

            entries = build_entries(root, sorted_json, match_record, root / "大额发票")

            self.assertEqual(
                entries[0]["订单截图"],
                ["xxx_5999.00_大额发票_订单截图_1.png", "xxx_5999.00_大额发票_订单截图_2.png"],
            )
            cell = summary_rows(entries)[1][COLUMNS.index("订单截图")]
            self.assertEqual(
                cell,
                "xxx_5999.00_大额发票_订单截图_1.png、xxx_5999.00_大额发票_订单截图_2.png",
            )

    def test_missing_screenshots_are_reported_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sorted_json, match_record = self._project(root, ["b.png"], [])

            entries = build_entries(root, sorted_json, match_record, root / "大额发票")

            self.assertEqual(entries[0]["支付记录"], [])
            self.assertIn("缺少支付记录", entries[0]["备注"])
            self.assertEqual(summary_rows(entries)[1][COLUMNS.index("支付记录")], "缺失")

    def test_summary_row_matches_feishu_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sorted_json, match_record = self._project(root, ["b.png"], ["p.png"])
            entries = build_entries(root, sorted_json, match_record, root / "大额发票")

            rows = summary_rows(entries)

            self.assertEqual(rows[0], COLUMNS)
            row = dict(zip(COLUMNS, rows[1]))
            self.assertEqual(row["姓名"], "xxx")
            # 大额发票不进报账单，编号恒为「无」。
            self.assertEqual(row["报账单编号（若无）"], "无")
            self.assertEqual(row["支付金额"], "5999.00")
            self.assertEqual(row["发票号码"], "222")
            self.assertEqual(row["处理反馈"], "")

    def test_summary_xlsx_is_a_readable_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "单价大额发票汇总表.xlsx"
            write_summary_xlsx(output, [COLUMNS, ["xxx"] * len(COLUMNS)])

            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

            self.assertIn("[Content_Types].xml", names)
            self.assertIn("xl/workbook.xml", names)
            self.assertIn("姓名", sheet)

    def test_nothing_is_written_without_high_value_invoices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sorted_json, match_record = self._project(root, ["b.png"], ["p.png"])
            only_material = {"发票信息": [self.SORTED_JSON["发票信息"][0]]}
            sorted_json.write_text(json.dumps(only_material, ensure_ascii=False), encoding="utf-8")
            output_dir = root / "大额发票"

            self.assertEqual(build_entries(root, sorted_json, match_record, output_dir), [])
            self.assertFalse(output_dir.exists())


class PaymentMaterialGroupingTests(unittest.TestCase):
    """连号组整组提交；单张入口剔除大额发票。两个生成器必须给出一致的分组。"""

    MATERIAL = {
        "文件名": "a.pdf",
        "更新后文件名": "1_a.pdf",
        "购买方名称": "浙江大学",
        "行程单文件名": "无需",
        "价税合计金额": 1200.0,
        "项目列表": [{"项目名称": "螺丝", "单价": 600.0}],
    }
    BIG = {
        "文件名": "b.pdf",
        "更新后文件名": "3_b.pdf",
        "购买方名称": "浙江大学",
        "行程单文件名": "无需",
        "价税合计金额": 5999.0,
        "项目列表": [{"项目名称": "电机", "单价": 5999.0}],
    }

    def _errors(self, category: str) -> dict:
        if category == "连号发票":
            return {
                "连号发票": [{
                    "重复组信息": "2026-03-20 | 某商家",
                    "重复发票总数": 2,
                    "所有重复发票": [
                        {"发票序号": 0, "文件名": "a.pdf"},
                        {"发票序号": 1, "文件名": "b.pdf"},
                    ],
                    "问题原因": "共 2 张发票为同一时间且同一销售方, 需要额外添加支付说明与支付记录",
                }]
            }
        return {
            "价税合计超1000元": [{
                "发票序号": 1,
                "文件名": "b.pdf",
                "问题原因": "价税合计 5999.0 元超过 1000.0 元，需要提交支付说明与支付记录",
            }]
        }

    def test_serial_group_keeps_every_invoice(self) -> None:
        """连号组里的每一张都要有支付说明和支付记录，不得在组内剔除。"""
        errors = self._errors("连号发票")
        by_source = {"a.pdf": self.MATERIAL, "b.pdf": self.BIG}

        groups = warning_groups(errors, by_source)

        self.assertEqual(len(groups), 1)
        self.assertEqual([inv["文件名"] for inv in groups[0]["invoices"]], ["a.pdf", "b.pdf"])

    def test_single_entry_high_value_is_excluded(self) -> None:
        """大额发票走单价大额发票汇总表，单张入口不生成支付说明。"""
        groups = warning_groups(self._errors("价税合计超1000元"), {"b.pdf": self.BIG})

        self.assertEqual(groups, [])

    def test_both_generators_agree_on_the_same_group(self) -> None:
        """支付说明与支付记录必须同进同出，否则会出现有记录没说明的组。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "images").mkdir()
            for name in ("pa.png", "pb.png"):
                (root / "images" / name).write_bytes(b"\x89PNG\r\n")

            errors_path = root / "invoice_errors.json"
            errors_path.write_text(json.dumps(self._errors("连号发票"), ensure_ascii=False), encoding="utf-8")
            results_path = root / "invoice_results_sorted.json"
            results_path.write_text(
                json.dumps({"发票信息": [self.MATERIAL, self.BIG]}, ensure_ascii=False), encoding="utf-8"
            )
            match_path = root / "匹配记录.json"
            match_path.write_text(
                json.dumps({
                    "版本": 2,
                    "发票映射": {
                        "invoices/a.pdf": {"发票文件": "invoices/a.pdf", "支付记录": ["images/pa.png"],
                                           "账单截图": [], "行程明细": [], "购买日期": ""},
                        "invoices/b.pdf": {"发票文件": "invoices/b.pdf", "支付记录": ["images/pb.png"],
                                           "账单截图": [], "行程明细": [], "购买日期": ""},
                    },
                    "未匹配截图": [],
                    "忽略截图": [],
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            record_groups = auto_collect_groups_from_record(errors_path, results_path, match_path, root)
            explanation_groups = warning_groups(
                json.loads(errors_path.read_text(encoding="utf-8")),
                {"a.pdf": self.MATERIAL, "b.pdf": self.BIG},
            )

            self.assertEqual(len(record_groups), len(explanation_groups))
            # 支付记录组收齐了两张发票的截图，说明组内没有发票被丢掉。
            self.assertEqual(len(record_groups[0]["images"]), 2)


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
