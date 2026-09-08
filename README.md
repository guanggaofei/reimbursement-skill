# 报销材料自动整理 Skill

本仓库提供 Linux/macOS 与 Windows 两套 opencode 入口，用于自动整理发票 PDF、行程单和费用截图，并生成报账单、支出记录与合并 PDF。支付材料保留原始占位名称，由用户填写姓名后自行压缩。

## 安装

### 运行环境

- Python 3.10 或更高版本，并确保该解释器可使用 `venv` 和 `pip`。
- Poppler 提供的 `pdftotext` 和 `pdftoppm` 必须都在 `PATH` 中。Linux 通常安装 `poppler-utils`，macOS 可安装 Homebrew `poppler`。
- Windows 不自带 Poppler，Poppler 官方也不提供 Windows 安装包。可从社区维护的 [poppler-windows releases](https://github.com/oschwartz10612/poppler-windows/releases/latest) 下载预编译包；其中包含 `Library\bin\pdftotext.exe` 和 `Library\bin\pdftoppm.exe`。解压后将 `Library\bin` 加入 `PATH`，再在 PowerShell 中运行 `Get-Command pdftotext,pdftoppm` 验证两个命令。
- 源 PDF 使用未嵌入字体时，对应字体必须已安装且对 Poppler 可读；合并脚本会在 Poppler 无法创建字体时停止，避免静默生成缺字 PDF。

### Agent 安装要求

- Agent 获取本仓库时，必须将 Git 仓库克隆到 `/tmp/reimbursement-opencode-skill`，不得克隆到用户的报销项目目录或其他长期保存目录：

  ```bash
  git clone https://github.com/guanggaofei/reimbursement-opencode-skill.git /tmp/reimbursement-opencode-skill
  ```

- `/tmp/reimbursement-opencode-skill` 仅作为安装源；skill 和 subagent 仍应复制到用户报销项目的 `.opencode/` 目录。
- 安装完成后，Agent 必须明确告知用户：**重启 opencode，并开启一个新会话，以加载新安装的 skill。** 仅在当前会话中继续操作不能保证新 skill 已被加载。

当前工作目录应为报销项目根目录。

### opencode 版本

Linux/macOS：

```bash
mkdir -p .opencode/skills/reimbursement .opencode/agents invoices images
cp -r skills/reimbursement/assets skills/reimbursement/scripts skills/reimbursement/agents .opencode/skills/reimbursement/
cp skills/reimbursement/SKILL.md .opencode/skills/reimbursement/SKILL.md
cp agents/fix-bearing-invoice.md agents/fix-duplicate-screenshots.md agents/fix-shop-name-ambiguity.md agents/fix-trip-ambiguity.md .opencode/agents/
cp agents/fix-invoice-errors.md .opencode/agents/fix-invoice-errors.md
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force .opencode\skills\reimbursement, .opencode\agents, invoices, images | Out-Null
Copy-Item -Recurse -Force skills\reimbursement\assets, skills\reimbursement\scripts, skills\reimbursement\agents .opencode\skills\reimbursement\
Copy-Item -Force skills\reimbursement\SKILL.windows.md .opencode\skills\reimbursement\SKILL.md
Copy-Item -Force agents\fix-bearing-invoice.md, agents\fix-duplicate-screenshots.md, agents\fix-shop-name-ambiguity.md, agents\fix-trip-ambiguity.md .opencode\agents\
Copy-Item -Force agents\fix-invoice-errors.windows.md .opencode\agents\fix-invoice-errors.md
```

`.windows.md` 文件是安装源文件，不应作为额外入口复制到目标项目。

### Claude Code 版本

Claude Code 版本与 opencode 版本共用同一套 `skills/reimbursement/scripts/` 和 `assets/templates/`，仅流程文档和子代理定义按 Claude Code 的约定另写一份，分别位于本仓库的 `.claude/skills/reimbursement/SKILL.md` 和 `.claude/agents/fix-*.md`。两者产出的文件完全一致，可以只装其一，也可以同时安装。

在报销项目根目录下执行，`SRC` 指向本仓库的克隆位置：

```bash
SRC=/tmp/reimbursement-opencode-skill
mkdir -p .claude/skills/reimbursement .claude/agents invoices images
cp -r "$SRC/skills/reimbursement/assets" "$SRC/skills/reimbursement/scripts" .claude/skills/reimbursement/
cp "$SRC/.claude/skills/reimbursement/SKILL.md" .claude/skills/reimbursement/SKILL.md
cp "$SRC"/.claude/agents/fix-*.md .claude/agents/
```

安装完成后需重启 Claude Code 或开启新会话，以加载新安装的 skill 和 subagent。

本仓库的 `.claude/skills/reimbursement/` 只存放 `SKILL.md`，不含 `scripts/` 与 `assets/`——脚本在仓库中只保留 `skills/reimbursement/` 下的唯一一份，由上面的安装命令复制到目标项目。因此 `SKILL.md` 中形如 `.claude/skills/reimbursement/scripts/xxx.py` 的路径只在安装后的用户项目中成立，在本仓库内不成立，这与 opencode 版 `SKILL.md` 引用 `.opencode/...` 的方式一致。

Claude Code 版本没有 Windows 专用入口；Windows 用户请使用 opencode 版本的 `SKILL.windows.md`，或自行把 `.claude/skills/reimbursement/SKILL.md` 中的路径与命令改为 PowerShell 形式。

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
