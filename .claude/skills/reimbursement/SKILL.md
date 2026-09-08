---
name: reimbursement
description: 整理报销发票和支出记录截图：运行 super_invoice 提取发票字段、处理 ERROR/需人工校验、用 OCR 匹配支付记录与账单截图，生成报账单 XLSX、支出记录 DOCX、支付说明与支付记录、单价大额发票汇总表和合并打印 PDF。当用户提到报销、发票整理、报账单、支出记录、贴发票时使用。
metadata:
  requires:
    bins: ["python3", "pdftotext", "pdftoppm"]
---

# 报销流程（Claude Code / Linux / macOS）

本 skill 与 opencode 版本共用同一套 Python 脚本和数据契约，产出完全一致的文件。差异仅在子代理的调用方式。

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
- `大额发票/`、`单价大额发票汇总表.xlsx`、`大额发票生成结果.md`（存在大额发票时）
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

## 调用子代理的通用要求

本 skill 的五个子代理定义在 `.claude/agents/` 下，用 Agent 工具调用，`subagent_type` 取对应文件的 `name`：`fix-invoice-errors`、`fix-shop-name-ambiguity`、`fix-trip-ambiguity`、`fix-duplicate-screenshots`、`fix-bearing-invoice`。

**这些子代理以零上下文启动**，看不到本次会话的任何内容。每次调用的 prompt 必须自带它工作所需的全部信息，至少包含：

1. **项目根目录的绝对路径**，例如 `/Users/xxx/报销2026`。子代理的所有相对路径以此为基准。
2. **本轮要处理的具体条目**，从对应的统计/错误文件里摘出来逐条列明，不要只说“处理所有问题”。已在前几轮解决的条目不要再传。
3. **它要写入的 action 文件路径**（见下表），并明确要求它只写 action 文件、不要自己应用、不要直接改 `匹配记录.json`。
4. **本轮是第几轮、上限 3 轮**，以及“无法可靠判断时保留未匹配并说明原因，不要猜测”。

子代理的详细方法、数据来源和 action 格式已写在它自己的定义文件里，prompt 中不需要重复，但上述四项每次都要给。

## 自动化流程

### 1. 清理本轮派生产物

保留 `invoices/`、`images/`、`OCR缓存.json`、`匹配记录.json`、历史报账单和 skill 文件。清理其余本轮派生产物：

```bash
rm -rf output/ 报销工作文件/ 待审核截图/ 大额发票/
rm -f invoice_results.json invoice_results_sorted.json invoice_errors.json
rm -f 支出记录OCR整理结果.md 支付说明生成结果.md 大额发票生成结果.md
rm -f 'Hello World 2026报账单填写结果.xlsx' 'Hello World 2026支出记录填写结果.docx'
rm -f 单价大额发票汇总表.xlsx 合并发票_纵向居中.pdf
```

### 2. 验证输入与出租车配对

确认 `invoices/` 和 `images/` 存在，然后运行：

```bash
.venv/bin/python .claude/skills/reimbursement/scripts/check_taxi_pairs.py --root .
```

### 3. 运行发票提取并修复字段

```bash
.venv/bin/python .claude/skills/reimbursement/scripts/super_invoice.py --root .
.venv/bin/python .claude/skills/reimbursement/scripts/check_invoice_errors.py --root .
```

`check_invoice_errors.py` 写入 `报销工作文件/invoice_errors_raw.json`。若其中 `error_count > 0`，用 Agent 工具调用 `fix-invoice-errors`。它只读取该错误列表并写入 `报销工作文件/invoice_fixes.json`。调用时 prompt 必须包含：

- 项目根目录绝对路径。
- 本轮要修复的错误条目（从 `invoice_errors_raw.json` 的 `errors[]` 逐条列出 `文件名`、`字段`、`当前值`），只列本轮仍未解决的。
- 输出文件路径 `报销工作文件/invoice_fixes.json`，并说明只写该文件、不要自行应用。
- 本轮轮次和 3 轮上限。

然后执行：

```bash
.venv/bin/python .claude/skills/reimbursement/scripts/apply_invoice_fixes.py --root .
.venv/bin/python .claude/skills/reimbursement/scripts/check_invoice_errors.py --root .
```

最多修复 3 轮；错误数不下降或字段无法可靠确定时停止。若根目录存在历史 `第x批报账单.xlsx`，运行 `cross_batch_dedup.py --root .`，然后重新执行本步骤。最终再次运行 `super_invoice.py --root .`，确认它仍只生成根目录 `invoice_results.json`、`invoice_results_sorted.json`、`invoice_errors.json` 和 `output/`。

`invoice_errors.json` 中的 `未匹配分类` 是业务判定结果，不属于字段级错误，`fix-invoice-errors` 修不了它，修复循环也不会因它继续。该类发票归入 `output/5_未匹配/`，常见成因是项目提取失败、非辰景的住宿发票，或所有单价都无法解析。这类发票仍会进入报账单和支出记录以免漏报，但需要向用户逐张列出，由用户确认其报销通道。

### 4. 提取行程数据

```bash
.venv/bin/python .claude/skills/reimbursement/scripts/extract_trip_sheets.py --root .
```

输出 `报销工作文件/行程单数据.json`。

### 5. OCR 与截图匹配

OCR 可能耗时很长，**禁止由代理直接运行 `organize_expense_records.py`**，以免工具调用超时被中断。代理必须暂停流程，请用户在自己的终端中运行，并等待用户确认完成后再继续。不要试图用后台运行绕开这条限制。

面向不熟悉终端的用户时，按以下方式说明：

1. 告诉用户按 `Ctrl+Alt+T` 打开终端；macOS 用户按 `Command+空格`，输入“终端”并打开。
2. 根据当前项目根目录生成一条可直接复制的完整命令，路径必须替换为实际绝对路径，不得保留占位符：

```bash
cd "/实际的项目根目录" && .venv/bin/python .claude/skills/reimbursement/scripts/organize_expense_records.py --root .
```

3. 告诉用户把整行命令复制到终端，按回车后不要关闭终端，等待看到“OCR 处理完成”。
4. 告诉用户完成后回到会话回复“运行完成”。用户确认前不得继续后续步骤。
5. 告诉用户如果运行意外中断，重新执行同一行命令即可；脚本会读取 `OCR缓存.json`，已识别的图片不需要重做。

该脚本读取根目录输入、三个 JSON、`output/` 与稳定缓存，写入：

- 根目录 `OCR缓存.json`、`匹配记录.json`、`支出记录OCR整理结果.md`
- `报销工作文件/支出记录OCR匹配明细.md`

新增少量截图时也由用户按上述方式运行单行命令，并在末尾添加 `--scan-only`。

先运行覆盖率检查，刷新用户报告并生成机器可读的分类计数：

```bash
.venv/bin/python .claude/skills/reimbursement/scripts/verify_screenshot_coverage.py --root . --update-report --issue-summary-json 报销工作文件/截图问题统计.json
```

按“店铺名称歧义 → 行程歧义 → 重复截图”的顺序处理，**禁止并行调用**这三个子代理，否则会互相覆盖 action 文件。三类问题分别执行独立的收敛循环：

| 计数键 | `subagent_type` | action 文件 |
| --- | --- | --- |
| `店铺名称歧义` | `fix-shop-name-ambiguity` | `报销工作文件/fix-shop-name-ambiguity.actions.json` |
| `行程歧义` | `fix-trip-ambiguity` | `报销工作文件/fix-trip-ambiguity.actions.json` |
| `重复截图` | `fix-duplicate-screenshots` | `报销工作文件/fix-duplicate-screenshots.actions.json` |

子代理只写 action JSON，不自行应用；每轮由主流程统一执行：

```bash
.venv/bin/python .claude/skills/reimbursement/scripts/apply_match_actions.py --root . --actions 报销工作文件/<agent-name>.actions.json
```

每一类型最多处理 3 轮。每轮读取 `截图问题统计.json` 中该类型的轮前数量，只把当前仍未解决的条目交给一个新的同类型 subagent（每轮都新起一个，不要复用上一轮的）；应用其 action 后重新运行覆盖率检查并读取轮后数量。轮后数量下降且仍大于 0 时继续下一轮；降为 0 时完成；数量未下降、subagent 无可靠 action 或达到 3 轮时立即停止该类型并报告残留，不得反复空跑。

调用这三个子代理时，prompt 除通用四项外还要带上本轮待处理条目的具体清单——从 `报销工作文件/支出记录OCR匹配明细.md` 里摘出对应标记（`金额对应多个候选发票` / `金额对应多个候选行程` / `同时匹配同一发票，需人工识别`）的图片路径和候选发票，逐条列明。

`fix-bearing-invoice` 只处理“完全无截图发票”，在上述三类循环之后最多调用一次，不执行收敛重试。调用时 prompt 中要给出待处理的稳定发票路径列表，形如 `invoices/example.pdf,invoices/example2.pdf`，其中文件名取自 `invoice_results_sorted.json` 的 `文件名` 字段。应用其 action 后最后再运行一次覆盖率检查并刷新报告与分类计数。

将仍在 `匹配记录.json` 的 `未匹配截图[]` 中的原图复制到根目录 `待审核截图/`，不移动或改名原图。

### 6. 生成 DOCX 与 XLSX

```bash
.venv/bin/python .claude/skills/reimbursement/scripts/generate_expense_record_docx.py --root .
.venv/bin/python .claude/skills/reimbursement/scripts/generate_payment_record_docx.py --root .
.venv/bin/python .claude/skills/reimbursement/scripts/generate_payment_explanations.py --root . --date YYYY-M-D
.venv/bin/python .claude/skills/reimbursement/scripts/generate_reimbursement_xlsx.py --root .
.venv/bin/python .claude/skills/reimbursement/scripts/generate_high_value_invoices.py --root .
```

两份最终 Office 文件位于根目录；支付记录、支付说明及技术/调试文件位于 `报销工作文件/`；仅在存在需要确认或查看的分组时，在根目录保留 `支付说明生成结果.md`。

发票分类决定去向，五类互不重叠且覆盖全部发票：

- **材料费**（所有单价≤1000 元）走完整的线上线下流程。
- **打车费**照常，发票与行程单一并提交。
- **大额发票**（存在单价>1000 元、购买方不含辰景）按设备费走单价大额发票汇总表，不进报账单、支出记录、合并 PDF、支付说明和支付记录。`generate_high_value_invoices.py` 在存在此类发票时生成 `大额发票/`、`单价大额发票汇总表.xlsx` 和 `大额发票生成结果.md`；不存在时不产生任何文件。混价发票（部分项目超 1000）整张按大额处理。
- **辰景发票**（购买方含辰景）照常进报账单和支出记录，但不线下打印，需另行提交电子发票。
- **未匹配发票**归入 `output/5_未匹配/`，仍进报账单和支出记录，但需向用户逐张列出待确认。

报账单的“数量”和“单价”按以下规则填写：从原发票 PDF 读取第一条项目的数量；数量为非整数时取 `int`，数量栏为空时填 `1`；单价填写“价税合计金额 ÷ 处理后的数量”。处理后的数量必须大于 0。大额发票已由分类过滤排除在报账单与支出记录之外，`build_rows` 中的 1000 元上限仅作兜底断言；一旦触发说明分类判定与此处推算的单价不一致，停止并报告。

支付说明仅以 `invoice_errors.json` 中明确要求同时添加支付说明与支付记录的分组为入口。无法可靠确定收款方时停止该组，不猜测。

上述 DOCX 与 XLSX 命令完成后、进入步骤 7 前，必须立即读取最新的 `支出记录OCR整理结果.md` 和 `匹配记录.json`，向用户告知截图匹配缺口：

- 逐张列出完全未匹配或截图不完整的发票，包括 `invoices/<原发票文件名>`、输出中的发票文件名、金额和缺失位置（`支付记录` 或 `账单截图`）。
- 打车发票必须精确到行程序号，并列出该行程缺少的截图位置。
- 对每个缺失位置，列出 `匹配记录.json` 中原因明确指向该发票或行程的候选截图原路径，如 `images/IMG_1234.png`；没有可靠候选时明确写“未找到候选截图”，不得仅凭相同金额猜测。
- 另列出仍在 `未匹配截图[]` 中的每张截图原路径和原因，确保用户能精确定位需要核对的图片。
- 存在大额发票时，读取 `大额发票生成结果.md` 的“待处理问题”表，逐张列出缺少订单截图或支付记录的大额发票；这类发票的截图缺口不会阻断流程，但会导致汇总表无法完整提交。
- 存在 `invoice_errors.json` 的 `未匹配分类` 条目时，逐张列出发票文件名和问题原因，说明它们已归入 `output/5_未匹配/` 且仍在报账单中，请用户确认报销通道。
- 即使没有缺口，也要明确告知“所有发票截图已完整匹配”。该告知是进度通知，不中断后续打包流程，除非用户要求暂停。

### 7. 合并 PDF

```bash
.venv/bin/python .claude/skills/reimbursement/scripts/merge_output_pdfs.py --root .
```

命令完成后，提示用户完成以下收尾操作：

- 检查 `报销工作文件/支付记录/` 和 `报销工作文件/支付说明/` 中的 DOCX，将文件名及文档内容里的 `xxx` 改为自己的姓名。
- 存在大额发票时：将 `大额发票/` 中文件名里的 `xxx` 改为自己的姓名，按 `单价大额发票汇总表.xlsx` 的内容填写飞书上的「单价大额发票汇总表」并上传对应文件。这类发票不参与线上线下普通流程，也不需要线下打印。
- 存在辰景发票时：`output/4_辰景发票/` 中的发票需自行改名压缩后作为电子发票单独提交，不线下打印；这类发票本身已包含在报账单和支出记录中。

### 8. 验证

确认最终 DOCX/XLSX 可作为 ZIP 打开。确认报账单与支出记录的发票行数一致且均不含大额发票。确认报账单中每行数量和单价已填写、数量为正数、单价不超过 1000 元，且数量乘单价与发票金额在允许精度内一致。存在大额发票时，确认 `大额发票/` 中每张大额发票至少有对应的 PDF，且 `单价大额发票汇总表.xlsx` 可作为 ZIP 打开、行数与大额发票张数一致。确认合并 PDF 可正常打开且页面内容完整。确认 `super_invoice.py` 的五类输出目录（`1_材料费`、`2_打车费`、`3_高价发票`、`4_辰景发票`、`5_未匹配`）和三个 JSON 文件名未改变，内部文件均位于 `报销工作文件/`。不自动创建任何 ZIP。
