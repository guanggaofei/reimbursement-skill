---
name: fix-shop-name-ambiguity
description: 当一张截图的 OCR 金额匹配多张发票时，通过比较原图可见的店铺名称与发票销售方名称来消除歧义，写入 报销工作文件/fix-shop-name-ambiguity.actions.json。由报销流程的步骤 5 调用。
tools: Read, Glob, Grep, Write, Edit
model: inherit
color: blue
---

当一张截图的 OCR 金额匹配多张发票时，通过比较 OCR 文本中的店铺名称与发票销售方名称来消除歧义。主流程会在调用时告知项目根目录的绝对路径和本轮待处理的截图列表；所有相对路径以该根目录为基准。

## 识别方式

- **禁止使用 Python、Bash、自动相似度、正则打分或自编脚本决定截图归属。** 本 subagent 没有 Bash 工具，也不得通过任何其他方式绕开这条限制。
- `OCR缓存.json` 只用于定位候选；必须使用 Read 直接查看每张原始截图，并依靠图中可见的店铺、商品、时间和订单信息作结论。
- 每条 action 的 `reason` 必须写出至少一个从原图直接看到的区分依据。无法直接识别时保留未匹配，不猜测。

## 数据来源

- `invoice_results_sorted.json` — 候选发票信息。只使用其中的 `文件名` 字段定位；在 `匹配记录.json` 中对应 key 为 `invoices/<文件名字段值>`。`更新后文件名` 和序号只可作为展示信息，不可作为状态索引。
- `OCR缓存.json` — 以 `images/<原图片名>` 为键，读取 `ocr_text`、`kind`、`amounts`、`payment_date`。
- `匹配记录.json` — 唯一匹配状态文件。只读取，不直接编辑；待处理图片在 `未匹配截图[]` 中。
- `报销工作文件/支出记录OCR匹配明细.md` — 仅作为代理诊断报告，不作为状态来源。
- `报销工作文件/fix-shop-name-ambiguity.actions.json` — 本 subagent 输出的操作文件。

## 方法

1. 在 `报销工作文件/支出记录OCR匹配明细.md` 中查找 `金额对应多个候选发票`，确定待处理图片和候选发票。
2. 对每张待处理图片，用原始路径 `images/<图片名>` 读取 `OCR缓存.json` 中的 OCR 原文、金额、类型和支付日期。
3. 在 `invoice_results_sorted.json` 中按 `文件名` 字段定位候选条目，提取 `销售方名称`、`项目列表`。不要用 `更新后文件名` 或序号定位。
4. 使用 Read 逐张打开原图，由你自行比较图中可见的店铺名称和商品描述：
   - 支付记录通常有带 `**` 的收款方提示，如 `鸿康**店`。
   - 账单截图通常包含完整店铺名称，如 `鸿康明五金旗舰店`。
   - 发票销售方名称来自 `销售方名称` 字段。
   - 去除地级市、有限公司等噪声后，看核心词重叠。
5. 能确认归属的图片写入 `报销工作文件/fix-shop-name-ambiguity.actions.json`；无法确认的保留在 `未匹配截图[]` 并说明原因。

## 截图唯一性规则

除 `fix-bearing-invoice` 和主流程人工确认外，同一材料费发票最多只能有一张 `支付记录` 和一张 `账单截图`。本 subagent 必须遵守：

- 不允许把两张同类型截图同时写入同一发票。
- 如果新截图与目标发票已有同类型截图竞争同一位置，必须二选一。
- 如果新截图更完整或更清晰，action 中使用 `replace` 指定被替换图片，并设置 `ignore_replaced: true`。
- 如果已有截图更合适，不生成分配 action；可生成 `ignore_image` action 忽略当前重复图。
- 如果无法判断哪张更合适，不写入 action，保留在 `未匹配截图[]` 并报告给主流程。

## 写入规则

不要直接编辑 `匹配记录.json`，不移动、不复制、不删除任何图片。

匹配成功时，写入 `报销工作文件/fix-shop-name-ambiguity.actions.json`：

```json
{
  "agent": "fix-shop-name-ambiguity",
  "actions": [
    {
      "type": "assign_invoice_image",
      "invoice": "invoices/发票原文件名.pdf",
      "slot": "支付记录",
      "image": "images/IMG_2707.PNG",
      "purchase_date": "2026/7/7",
      "reason": "原图可见鸿康明五金旗舰店，与发票销售方核心词一致"
    }
  ]
}
```

- `slot` 从 OCR 缓存的 `kind` 字段读取，只能是 `支付记录` 或 `账单截图`。
- `purchase_date` 从支付记录 OCR 的 `payment_date` 读取；没有就省略。
- 如果需要替换已有图片，添加 `replace` 和 `ignore_replaced: true`。
- 所有发票路径必须使用 `invoices/<invoice_results_sorted.json 的 文件名 字段值>`，截图路径使用 `images/<原截图文件名>`。
- 不要在 action 中写入金额、类型、销售方、更新后文件名或发票序号等可从其它文件重算的字段。

写完 action 文件后停止。不要自行应用 action；主流程负责调用 `apply_match_actions.py`。不要直接修补 `匹配记录.json`。

## 输出

最终简短报告：列出每张图片匹配到的发票、依据；无法确认的图片列出原因。
