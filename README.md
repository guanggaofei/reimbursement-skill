# 报销材料自动整理 Skill（Codex）

为浙江大学 Hello World 机器人队整理发票 PDF、行程单和费用截图，生成报账单、支出记录、支付材料、大额发票汇总表与打印 PDF。

## 版本分支

| 分支 | 版本 | 安装位置 |
| --- | --- | --- |
| [codex](https://github.com/guanggaofei/reimbursement-opencode-skill/tree/codex)（主分支） | Codex，Windows / Linux / macOS | `.agents/skills/reimbursement/` |
| [opencode](https://github.com/guanggaofei/reimbursement-opencode-skill/tree/opencode) | opencode，Windows / Linux / macOS | `.opencode/skills/` 与 `.opencode/agents/` |
| [claude-code](https://github.com/guanggaofei/reimbursement-opencode-skill/tree/claude-code) | Claude Code，Linux / macOS | `.claude/skills/` 与 `.claude/agents/` |

选择对应分支，按照该分支的 README 安装。Git 分支名称不支持空格，所以 Claude Code 分支命名为 `claude-code`。三版沿用同一套业务脚本和输出契约；修改业务规则时应同步到另两个分支。

## Codex 安装

需要 Python 3.10+（包含 venv、pip）和 Poppler 的 `pdftotext`、`pdftoppm`。Windows 可从 [poppler-windows](https://github.com/oschwartz10612/poppler-windows/releases/latest) 获取社区构建，将 `Library\bin` 加入 PATH；Linux/macOS 安装相应 Poppler 包。PDF 未嵌入的中文字体也须可被 Poppler 读取。

先进入你保存本批报销材料的目录。仓库克隆到系统临时目录作为安装源，报销项目只接收技能文件；以下命令遇到源目录已存在或技能已安装会停止，避免覆盖。更新时先检查已有内容，再明确选择更新的文件。

### Windows PowerShell

```powershell
$taskSource = Join-Path $env:TEMP 'reimbursement-codex-skill'
if (Test-Path -LiteralPath $taskSource) { throw "安装源已存在，请先检查：$taskSource" }
if (Test-Path -LiteralPath '.agents/skills/reimbursement') { throw '报销技能已存在，请先检查再更新' }
git clone --branch codex --single-branch https://github.com/guanggaofei/reimbursement-opencode-skill.git $taskSource
if ($LASTEXITCODE -ne 0) { throw '克隆失败' }
New-Item -ItemType Directory -Force '.agents/skills', invoices, images | Out-Null
Copy-Item -LiteralPath (Join-Path $taskSource 'skills/reimbursement') -Destination '.agents/skills/reimbursement' -Recurse
```

### Linux / macOS

```bash
(
  set -eu
  SRC=/tmp/reimbursement-codex-skill
  if [ -e "$SRC" ] || [ -e .agents/skills/reimbursement ]; then
    echo '安装源或报销技能已存在，请先检查再更新。'
    exit 1
  fi
  git clone --branch codex --single-branch https://github.com/guanggaofei/reimbursement-opencode-skill.git "$SRC"
  mkdir -p .agents/skills invoices images
  cp -R "$SRC/skills/reimbursement" .agents/skills/reimbursement
)
```

已下载本仓库时，可以跳过克隆，确认当前分支为 `codex`，直接把 `skills/reimbursement/` 完整复制到报销项目的 `.agents/skills/reimbursement/`。请同时保留 `SKILL.md`、`SKILL.windows.md`、`references/`、`agents/openai.yaml`、`scripts/` 和 `assets/`，不要用 Windows 文件覆盖主入口；主入口会根据操作系统引导读取对应流程。

安装后开启 Codex 新会话，在报销项目目录中使用：

```text
请使用 $reimbursement 整理当前文件夹中的报销材料。
```

将发票和行程单放入 `invoices/`，原始支付记录与账单截图放入 `images/`。技能检查环境并使用本项目 `.venv`；缺少 Python 包时先列出缺项和安装命令。长时间 OCR 匹配仍由用户在自己的终端运行，Codex 会给出含实际绝对路径的完整命令，待用户回复“运行完成”后继续。

## Codex 适配说明

- `skills/reimbursement/SKILL.md` 为主入口和 Unix 八步流程，`SKILL.windows.md` 为对应 PowerShell 八步流程。
- 五类修复方法放在 `references/fix-*.md`，按需读取。存在子代理工具时可委派通用子代理，否则主代理按同一规则执行；不依赖自定义代理注册。
- 修复任务只写指定 JSON，主流程串行应用并验证；三类截图歧义各最多三轮，完全无截图发票最多处理一次。
- 所有截图结论须查看原图；保留既有分类、稳定文件名、原图保护和大额发票规则。
- `agents/openai.yaml` 提供技能名称和默认提示词。旧 `agents/`、`.claude/` 内容保留供参考，Codex 安装不复制它们。

安装目录、技能发现与元数据依据 [OpenAI 官方技能文档](https://learn.chatgpt.com/docs/build-skills)。

## 开发验证

脚本唯一源码位于 `skills/reimbursement/scripts/`。使用项目虚拟环境运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_file_layout.py -v
```

Linux/macOS 将解释器改为 `.venv/bin/python`。未安装 pytest 时，同一套 unittest 测试也可用 `-m unittest discover -s tests -p test_file_layout.py -v` 运行。修改任一平台流程时同步检查另一个入口与修复参考说明。

## 文件布局

根目录保留原始输入、可复用状态、用户报告和最终产物：

- `invoices/`、`images/`
- `output/`
- `invoice_results.json`、`invoice_results_sorted.json`、`invoice_errors.json`
- `OCR缓存.json`、`匹配记录.json`
- 历史 `第x批报账单.xlsx`
- `支出记录OCR整理结果.md`、`待审核截图/`
- `支付说明生成结果.md`（存在相应分组时）
- `大额发票/`、`单价大额发票汇总表.xlsx`、`大额发票生成结果.md`（存在大额发票时）
- `Hello World 2026报账单填写结果.xlsx`
- `Hello World 2026支出记录填写结果.docx`
- `合并发票_纵向居中.pdf`

代理内部技术文件统一位于 `报销工作文件/`：

- `invoice_errors_raw.json`、`invoice_fixes.json`、`行程单数据.json`、`截图问题统计.json`
- `支出记录OCR匹配明细.md`、`支出记录DOCX生成结果.md`、`OCR缓存原文.md`
- subagent action JSON
- 未打包的 `支付记录/`、`支付说明/`
- DOCX 解包、XML 调试文件和其他临时产物

`super_invoice.py` 的输出契约保持不变：它仍在根目录写入三个 JSON，并写入根目录 `output/`（`1_材料费`、`2_打车费`、`3_高价发票`、`4_辰景发票`、`5_未匹配`）；不复制、改名或迁移这些文件。

发票分类判定集中在 `skills/reimbursement/scripts/_invoice_filters.py`，`super_invoice.py` 与各生成器共用同一组判定。这一点是硬性要求：分类决定发票进哪个 `output/` 子目录、拿到哪个全局序号，生成器用同样的判定决定它进不进报账单；两边一旦分歧，就会出现「被打印并盖了序号却没有报账单行」的情况，其后所有发票的纸质序号全部错位。

发票分类决定下游去向。**大额发票**（存在单价>1000 元、购买方不含辰景）按《报销指南》第 2 节作设备费处理，不进报账单、支出记录、合并 PDF、支付说明和支付记录，改由 `generate_high_value_invoices.py` 产出单价大额发票汇总表所需的材料（重命名后的发票 PDF、订单截图、支付记录，以及一份列头与飞书表一致的 xlsx）；混价发票整张按大额处理。**辰景发票**照常进报账单和支出记录，但不参与线下打印，需另行提交电子发票。两类发票的姓名均保留 `xxx` 占位符，由用户改名后自行压缩上传。**未匹配发票**（项目提取失败、非辰景住宿、单价均无法解析）归入 `5_未匹配`，仍进报账单以免漏报，同时在 `invoice_errors.json` 中以 `未匹配分类` 列出待人工确认。

合并 PDF 默认以 400 DPI 通过 `pdftoppm` 按每页 CropBox 将源 PDF 先渲染为图片，再将图片居中放入全新的 A4 页面，不直接合并或嵌入源 PDF 页面对象。每个发票页在标题上方标记发票序号并预留两条各 3 cm 的签名线；行程单页不添加标记。
