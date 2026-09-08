"""Invoice category predicates — the single source of truth.

``super_invoice.sort_invoices`` uses these to decide which ``output/`` folder an
invoice lands in and therefore which ``global_seq`` it receives; the document
generators use them to decide which invoices reach 报账单, 支出记录, 合并 PDF,
支付说明 and 支付记录.  Both sides must agree: if a folder says "material fee"
while a generator says "high value", the invoice is printed with a sequence
number stamped on it but has no 报账单 row, and every printed invoice after it
is off by one.  Keeping one implementation here is what prevents that.

The five categories are mutually exclusive and exhaustive, in this order:

1. ``1_材料费``     — 材料费, every unit price ≤1000
2. ``2_打车费``     — 打车费
3. ``3_高价发票``   — 单价>1000 且购买方不含辰景
4. ``4_辰景发票``   — 购买方含辰景
5. ``5_未匹配``     — 以上都不匹配，需人工确认报销通道

Downstream routing differs per category:

- 高价发票 leave the ordinary flow entirely (《报销指南》 section 2): excluded
  from 报账单, 支出记录, 合并打印 PDF, 支付说明 and 支付记录, and reported by
  ``generate_high_value_invoices.py`` for the 单价大额发票汇总表.
- 辰景发票 stay in 报账单 and 支出记录 but are not printed offline; the user
  submits them separately as electronic invoices.
- 未匹配发票 stay in the ordinary flow so no expense is silently dropped, but
  are flagged in ``invoice_errors.json`` for manual review.

Unit prices are parsed with ``Decimal(str(...))`` rather than checked with
``isinstance``: 《报销材料整理使用说明》 warns that users fixing ERROR fields by
hand often leave the value quoted (``单价: "1.00"``), and a quoted number must
classify the same as a bare one.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


HIGH_VALUE_THRESHOLD = Decimal("1000")


def parse_amount(value: Any) -> Decimal | None:
    """Parse a money field, returning None for ERROR and unparseable values.

    Values reach us as floats from extraction but as strings when a user fixed
    an ERROR field by hand and left the quotes on — both must compare the same.
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def unit_prices(inv: dict[str, Any]) -> list[Decimal]:
    """Parse every item unit price, skipping ERROR and unparseable values."""
    prices: list[Decimal] = []
    for item in inv.get("项目列表") or []:
        if not isinstance(item, dict):
            continue
        price = parse_amount(item.get("单价"))
        if price is not None:
            prices.append(price)
    return prices


def _item_names(inv: dict[str, Any]) -> list[str]:
    return [
        str(item.get("项目名称") or "").lower()
        for item in inv.get("项目列表") or []
        if isinstance(item, dict)
    ]


def _extraction_failed(inv: dict[str, Any]) -> bool:
    """First item name is ERROR — item extraction produced nothing usable."""
    names = _item_names(inv)
    return not names or names[0] == "error"


def is_chenjing(inv: dict[str, Any]) -> bool:
    """辰景发票：购买方名称含「辰景」。"""
    return "辰景" in str(inv.get("购买方名称") or "")


def is_transport_fee(inv: dict[str, Any]) -> bool:
    """打车费：文件名含打车/出租，或每个项目名都含运输/订车。"""
    file_name = str(inv.get("文件名") or "").lower()
    if "打车" in file_name or "出租" in file_name:
        return True
    if _extraction_failed(inv):
        return False
    return all("运输" in name or "订车" in name for name in _item_names(inv))


def is_high_value(inv: dict[str, Any]) -> bool:
    """大额发票：存在单价>1000 的项目，且不是辰景发票。

    走《报销指南》第 2 节的单价大额发票汇总表通道，不参与普通线上线下流程。
    """
    if is_chenjing(inv):
        return False
    return any(price > HIGH_VALUE_THRESHOLD for price in unit_prices(inv))


def is_material_fee(inv: dict[str, Any]) -> bool:
    """材料费：非打车费、项目名不含住宿、且所有单价均 ≤1000。

    任一项目单价超过 1000 元，整张发票就走大额通道 —— 混价发票（例如
    ``[800, 1500]``）必须归入大额发票，否则它会被打印并盖上序号却没有报账单行。
    """
    if is_transport_fee(inv) or is_chenjing(inv):
        return False
    if _extraction_failed(inv):
        return False
    if any("住宿" in name for name in _item_names(inv)):
        return False
    prices = unit_prices(inv)
    if not prices:
        return False
    return all(price <= HIGH_VALUE_THRESHOLD for price in prices)


def is_unclassified(inv: dict[str, Any]) -> bool:
    """未匹配：前四类都不命中，需人工确认报销通道。"""
    return not (
        is_material_fee(inv)
        or is_transport_fee(inv)
        or is_high_value(inv)
        or is_chenjing(inv)
    )


def max_high_unit_price(inv: dict[str, Any]) -> Decimal | None:
    """汇总表「单价」列取值：超过 1000 的项目单价中的最大值。"""
    over = [price for price in unit_prices(inv) if price > HIGH_VALUE_THRESHOLD]
    return max(over) if over else None


def high_unit_price_count(inv: dict[str, Any]) -> int:
    """超过 1000 元的项目条数，用于在生成报告中提示取值来源。"""
    return sum(1 for price in unit_prices(inv) if price > HIGH_VALUE_THRESHOLD)


def ordinary_invoices(invoices: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """走普通线上流程的发票：排除大额发票，保留辰景发票和未匹配发票。"""
    return [inv for inv in invoices if not is_high_value(inv)]


def high_value_invoices(invoices: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """走单价大额发票汇总表的发票。"""
    return [inv for inv in invoices if is_high_value(inv)]
