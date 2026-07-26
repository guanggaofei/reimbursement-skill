---
name: reimbursement
description: "Trigger when the user indicates they are executing the reimbursement workflow."
---

# 报销流程（Linux/macOS）

## 核心规则

1. 默认从步骤 1 顺序执行；仅在用户明确指定起点时跳转。
2. bundled 脚本出现未说明的错误时停止并报告。
3. 所有写入值必须来自 PDF、截图 OCR、用户确认或脚本结果，不猜测。
4. `invoice_results.json` 仅允许通过修复脚本更新；`invoice_results_sorted.json` 和 `invoice_errors.json` 只读。
5. 不复制、重命名或迁移 `super_invoice.py` 生成的三个 JSON 与 `output/`。

## 环境

先运行 `python3 --version`，确认 Python 为 3.10 或更高版本且可使用 `venv` 和 `pip`。使用项目 `.venv`；不存在时运行 `python3 -m venv .venv`，创建失败时停止并报告。所需 Python 包包括 `pdfplumber`、`rapidocr-onnxruntime`、`onnxruntime`、`Pillow`、`pypinyin`、`pypdf`、`python-docx`、`lxml`。

开始流程前确认 `pdftotext` 和 `pdftoppm` 均可执行；任一缺失时停止并告知用户需要安装 Poppler，不自行安装系统软件。
源 PDF 使用未嵌入的中文字体时，Poppler 必须能访问对应字体；出现字体创建失败时停止并报告具体 PDF，不继续生成缺字文档。

缺少 Python 包时，先向用户列出缺少的包、用途和完整安装命令并等待批准。完整环境安装命令为：

```bash
.venv/bin/python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pdfplumber rapidocr-onnxruntime onnxruntime Pillow pypinyin pypdf python-docx lxml
```

只缺少部分包时仅安装缺少项，不重复安装全部依赖。所有 Python 脚本必须通过 `.venv/bin/python` 调用，禁止使用系统 `python` 或 `python3`。

## 路径约定

根目录保存输入、稳定状态、用户报告和最终产物：

- `invoices/`、`images/`、`output/`
- `invoice_results.json`、`invoice_results_sorted.json`、`invoice_errors.json`
- `OCR缓存.json`、`匹配记录.json`
- 历史 `第x批报账单.xlsx`
- `支出记录OCR整理结果.md`、`待审核截图/`
- `支付说明生成结果.md`（存在相应分组时）
- `Hello World 2026报账单填写结果.xlsx`
- `Hello World 2026支出记录填写结果.docx`
- `合并发票_纵向居中.pdf`

`报销工作文件/` 仅保存代理内部文件：

- `invoice_errors_raw.json`、`invoice_fixes.json`、`行程单数据.json`
- `截图问题统计.json`
- `支出记录OCR匹配明细.md`、`支出记录DOCX生成结果.md`、`OCR缓存原文.md`
- 所有 subagent action JSON
- `支付记录/`、`支付说明/` 及其中的 DOCX
- DOCX 解包、XML 调试文件和其他临时产物

## 自动化流程

### 1. 清理本轮派生产物

保留 `invoices/`、`images/`、`OCR缓存.json`、`匹配记录.json`、历史报账单和 skill 文件。清理其余本轮派生产物：

```bash
rm -rf output/ 报销工作文件/ 待审核截图/
rm -f invoice_results.json invoice_results_sorted.json invoice_errors.json
rm -f 支出记录OCR整理结果.md 支付说明生成结果.md
rm -f 'Hello World 2026报账单填写结果.xlsx' 'Hello World 2026支出记录填写结果.docx'
rm -f 支付说明与支付记录.zip 辰景发票.zip 合并发票_纵向居中.pdf
```

### 2. 验证输入与出租车配对

确认 `invoices/` 和 `images/` 存在，然后运行：

```bash
.venv/bin/python .opencode/skills/reimbursement/scripts/check_taxi_pairs.py --root .
```

### 3. 运行发票提取并修复字段

```bash
.venv/bin/python .opencode/skills/reimbursement/scripts/super_invoice.py --root .
.venv/bin/python .opencode/skills/reimbursement/scripts/check_invoice_errors.py --root .
```

`check_invoice_errors.py` 写入 `报销工作文件/invoice_errors_raw.json`。若其中 `error_count > 0`，调用 `@fix-invoice-errors`；subagent 只读取该错误列表，并写入 `报销工作文件/invoice_fixes.json`。然后执行：

```bash
.venv/bin/python .opencode/skills/reimbursement/scripts/apply_invoice_fixes.py --root .
.venv/bin/python .opencode/skills/reimbursement/scripts/check_invoice_errors.py --root .
```

最多修复 3 轮；错误数不下降或字段无法可靠确定时停止。若根目录存在历史 `第x批报账单.xlsx`，运行 `cross_batch_dedup.py --root .`，然后重新执行本步骤。最终再次运行 `super_invoice.py --root .`，确认它仍只生成根目录 `invoice_results.json`、`invoice_results_sorted.json`、`invoice_errors.json` 和 `output/`。

### 4. 提取行程数据

```bash
.venv/bin/python .opencode/skills/reimbursement/scripts/extract_trip_sheets.py --root .
```

输出 `报销工作文件/行程单数据.json`。

### 5. OCR 与截图匹配

OCR 可能耗时很长，禁止由代理直接运行 `organize_expense_records.py`，以免 opencode 超时终止进程。代理必须暂停流程，请用户在自己的终端中运行，并等待用户确认完成后再继续。

面向不熟悉终端的用户时，按以下方式说明：

1. 告诉用户按 `Ctrl+Alt+T` 打开终端；macOS 用户按 `Command+空格`，输入“终端”并打开。
2. 根据当前项目根目录生成一条可直接复制的完整命令，路径必须替换为实际绝对路径，不得保留占位符：

```bash
cd "/实际的项目根目录" && .venv/bin/python .opencode/skills/reimbursement/scripts/organize_expense_records.py --root .
```

3. 告诉用户把整行命令复制到终端，按回车后不要关闭终端，等待看到“OCR 处理完成”。
4. 告诉用户完成后回到 opencode 回复“运行完成”。用户确认前不得继续后续步骤。
5. 告诉用户如果运行意外中断，重新执行同一行命令即可；脚本会读取 `OCR缓存.json`，已识别的图片不需要重做。

读取根目录输入、三个 JSON、`output/` 与稳定缓存，写入：

- 根目录 `OCR缓存.json`、`匹配记录.json`、`支出记录OCR整理结果.md`
- `报销工作文件/支出记录OCR匹配明细.md`

新增少量截图时也由用户按上述方式运行单行命令，并在末尾添加 `--scan-only`。

subagent 只写 action JSON，不自行应用；由主流程统一执行：

```bash
.venv/bin/python .opencode/skills/reimbursement/scripts/apply_match_actions.py --root . --actions 报销工作文件/<agent-name>.actions.json
```

先运行覆盖率检查，刷新用户报告并生成机器可读的分类计数：

```bash
.venv/bin/python .opencode/skills/reimbursement/scripts/verify_screenshot_coverage.py --root . --update-report --issue-summary-json 报销工作文件/截图问题统计.json
```

按“店铺名称歧义 → 行程歧义 → 重复截图”的顺序处理，禁止并行写 action。三类问题分别执行独立的收敛循环：

| 计数键 | subagent | action 文件 |
| --- | --- | --- |
| `店铺名称歧义` | `@fix-shop-name-ambiguity` | `报销工作文件/fix-shop-name-ambiguity.actions.json` |
| `行程歧义` | `@fix-trip-ambiguity` | `报销工作文件/fix-trip-ambiguity.actions.json` |
| `重复截图` | `@fix-duplicate-screenshots` | `报销工作文件/fix-duplicate-screenshots.actions.json` |

每一类型最多处理 3 轮。每轮读取 `截图问题统计.json` 中该类型的轮前数量，只把当前仍未解决的条目交给一个新的同类型 subagent；应用其 action 后重新运行覆盖率检查并读取轮后数量。轮后数量下降且仍大于 0 时继续下一轮；降为 0 时完成；数量未下降、subagent 无可靠 action 或达到 3 轮时立即停止该类型并报告残留，不得反复空跑。

`@fix-bearing-invoice` 只处理“完全无截图发票”，在上述三类循环之后最多调用一次，不执行收敛重试。应用其 action 后最后再运行一次覆盖率检查并刷新报告与分类计数。

将仍在 `匹配记录.json` 的 `未匹配截图[]` 中的原图复制到根目录 `待审核截图/`，不移动或改名原图。

### 6. 生成 DOCX 与 XLSX

```bash
.venv/bin/python .opencode/skills/reimbursement/scripts/generate_expense_record_docx.py --root .
.venv/bin/python .opencode/skills/reimbursement/scripts/generate_payment_record_docx.py --root .
.venv/bin/python .opencode/skills/reimbursement/scripts/generate_payment_explanations.py --root . --date YYYY-M-D
.venv/bin/python .opencode/skills/reimbursement/scripts/generate_reimbursement_xlsx.py --root .
```

两份最终 Office 文件位于根目录；支付记录、支付说明及技术/调试文件位于 `报销工作文件/`；仅在存在需要确认或查看的分组时，在根目录保留 `支付说明生成结果.md`。

报账单的“数量”和“单价”按以下规则填写：从原发票 PDF 读取第一条项目的数量；数量为非整数时取 `int`，数量栏为空时填 `1`；单价填写“价税合计金额 ÷ 处理后的数量”。处理后的数量必须大于 0，单价不得超过 1000 元，否则停止并报告。

支付说明仅以 `invoice_errors.json` 中明确要求同时添加支付说明与支付记录的分组为入口。无法可靠确定收款方时停止该组，不猜测。

上述 DOCX 与 XLSX 命令完成后、进入步骤 7 前，必须立即读取最新的 `支出记录OCR整理结果.md` 和 `匹配记录.json`，向用户告知截图匹配缺口：

- 逐张列出完全未匹配或截图不完整的发票，包括 `invoices/<原发票文件名>`、输出中的发票文件名、金额和缺失位置（`支付记录` 或 `账单截图`）。
- 打车发票必须精确到行程序号，并列出该行程缺少的截图位置。
- 对每个缺失位置，列出 `匹配记录.json` 中原因明确指向该发票或行程的候选截图原路径，如 `images/IMG_1234.png`；没有可靠候选时明确写“未找到候选截图”，不得仅凭相同金额猜测。
- 另列出仍在 `未匹配截图[]` 中的每张截图原路径和原因，确保用户能精确定位需要核对的图片。
- 即使没有缺口，也要明确告知“所有发票截图已完整匹配”。该告知是进度通知，不中断后续打包流程，除非用户要求暂停。

### 7. 合并 PDF

```bash
.venv/bin/python .opencode/skills/reimbursement/scripts/merge_output_pdfs.py --root .
```

命令完成后，提示用户检查 `报销工作文件/支付记录/` 和 `报销工作文件/支付说明/` 中的 DOCX，将文件名及文档内容里的 `xxx` 改为自己的姓名。

### 8. 验证

确认最终 DOCX/XLSX 可作为 ZIP 打开。确认报账单中每行数量和单价已填写、数量为正数、单价不超过 1000 元，且数量乘单价与发票金额在允许精度内一致。确认合并 PDF 可正常打开且页面内容完整。确认 `super_invoice.py` 的四类输出目录和三个 JSON 文件名未改变，内部文件均位于 `报销工作文件/`。不自动创建任何 ZIP。
