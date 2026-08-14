"""Apply fixes from invoice_fixes.json to invoice_results.json.

Usage:
    python apply_invoice_fixes.py --root .

invoice_fixes.json format (keyed by filename):
{
    "发票文件名.pdf": {
        "购买方税号": "12100000470095016Q",
        "价税合计金额": 39.04,
        "开票时间": [{"年": 2026, "月": 7, "日": 1}],
        "项目列表": [{"项目名称": "...", "单价": 39.65}],
        "发票号码状态": "正常"
    }
}

Each key in the fix dict completely replaces the corresponding key in
the invoice entry.  To remove items from an array (e.g. delete a negative
adjustment row from 项目列表), include only the items to keep.

Nested fields use the standard dot-notation path format, for example
"项目列表.0.项目名称".  Top-level fields use their field name directly.

Sanity checks:
  - 价税合计金额 / 单价 must be non-negative numbers
  - 购买方税号 / 销售方税号 must be strings ≥ 15 chars
  - 发票号码状态 must be 正常 or 需人工校验
  - 开票时间 entries must contain 年/月/日 keys
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _pathutil import INTERNAL_DIR, add_root_arg, resolve_path


def _resolve(target: dict, path: str):
    parts = path.split(".")
    obj = target
    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            return obj, part
        if part.isdigit():
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj, parts[-1]


def _validate(fn: str, key: str, value) -> None:
    if key in ("价税合计金额", "单价") and isinstance(value, (int, float)):
        assert value >= 0, f"金额为负: {fn}.{key} = {value}"
    if key in ("购买方税号", "销售方税号"):
        assert isinstance(value, str) and len(value) >= 15, \
            f"税号过短: {fn}.{key} = {value!r}"
    if key == "发票号码状态":
        assert value in ("正常", "需人工校验"), \
            f"非法发票号码状态: {fn}.{key} = {value!r}"
    if key == "开票时间":
        assert isinstance(value, list), f"开票时间格式错误: {fn}"
        for d in value:
            assert all(k in d for k in ("年", "月", "日")), \
                f"开票时间缺少字段: {fn}"


def _check(result: dict, fixes: dict) -> int:
    fixed_count = 0
    for inv in result["发票信息"]:
        fn = inv["文件名"]
        if fn not in fixes:
            continue
        fix = fixes[fn]
        for key, value in fix.items():
            # Dot-notation is the standard nested-field format for invoice fixes.
            if "." in key:
                parent, leaf = _resolve(inv, key)
                _validate(fn, leaf, value)
                parent[leaf] = value
                continue
            _validate(fn, key, value)
            inv[key] = value
        fixed_count += 1
    return fixed_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_root_arg(parser)
    parser.add_argument("--results", type=Path, default=Path("invoice_results.json"))
    parser.add_argument("--fixes", type=Path, default=INTERNAL_DIR / "invoice_fixes.json")
    args = parser.parse_args()
    root = args.root.resolve()

    results_path = resolve_path(root, args.results)
    fixes_path = resolve_path(root, args.fixes)

    if not fixes_path.exists():
        print(f"{fixes_path} not found, nothing to apply")
        return

    with open(results_path) as f:
        results = json.load(f)
    with open(fixes_path) as f:
        fixes = json.load(f)

    fixed_count = _check(results, fixes)

    with open(results_path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"Applied fixes to {fixed_count} invoices")


if __name__ == "__main__":
    main()
