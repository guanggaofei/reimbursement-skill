# reimbursement-skill：opencode 安装

本页只用于给 opencode 安装报销材料整理技能。若安装目标是其他框架，请转到 [Codex 分支 README](https://github.com/guanggaofei/reimbursement-skill/blob/codex/README.md) 或 [Claude Code 分支 README](https://github.com/guanggaofei/reimbursement-skill/blob/claude-code/README.md)；不确定目标时返回 [主分支安装入口](https://github.com/guanggaofei/reimbursement-skill/blob/main/README.md)。

## 安装前

- 确认目标框架为 opencode，安装源分支为 `opencode`。
- 当前工作目录必须是用户保存本批报销材料的项目根目录。
- 需要 Git、Python 3.10+（包含 venv、pip）和 Poppler 的 `pdftotext`、`pdftoppm`。Windows 将 Poppler 的 `Library\bin` 加入 PATH，并确保源 PDF 所用中文字体可用。
- 仓库仅克隆到系统临时目录作为安装源。已有安装源或同名技能/子代理时先检查，再决定更新；以下首次安装命令会停止，避免直接覆盖。
- WSL 使用 Linux 命令及 Linux 虚拟环境；Windows 原生环境使用 PowerShell 命令。

## Windows PowerShell

先进入报销项目根目录，然后执行：

```powershell
$ErrorActionPreference = 'Stop'
$taskSource = Join-Path $env:TEMP 'reimbursement-skill-opencode'
if (Test-Path -LiteralPath $taskSource) { throw "安装源已存在，请先检查：$taskSource" }
if (Test-Path -LiteralPath '.opencode/skills/reimbursement') { throw '报销技能已存在，请先检查再更新' }
git clone --branch opencode --single-branch https://github.com/guanggaofei/reimbursement-skill.git $taskSource
if ($LASTEXITCODE -ne 0) { throw '克隆失败' }
$taskAgentNames = @('fix-bearing-invoice', 'fix-duplicate-screenshots', 'fix-shop-name-ambiguity', 'fix-trip-ambiguity', 'fix-invoice-errors')
foreach ($taskAgentName in $taskAgentNames) {
  if (Test-Path -LiteralPath ".opencode/agents/$taskAgentName.md") {
    throw "子代理已存在，请先检查：$taskAgentName"
  }
}
New-Item -ItemType Directory -Force '.opencode/skills/reimbursement', '.opencode/agents', invoices, images | Out-Null
Copy-Item -LiteralPath (Join-Path $taskSource 'skills/reimbursement/scripts'), (Join-Path $taskSource 'skills/reimbursement/assets') -Destination '.opencode/skills/reimbursement' -Recurse
Copy-Item -LiteralPath (Join-Path $taskSource 'skills/reimbursement/agents') -Destination '.opencode/skills/reimbursement' -Recurse
Copy-Item -LiteralPath (Join-Path $taskSource 'skills/reimbursement/SKILL.windows.md') -Destination '.opencode/skills/reimbursement/SKILL.md'
foreach ($taskAgentName in $taskAgentNames) {
  $taskAgentFile = "$taskAgentName.md"
  if ($taskAgentName -eq 'fix-invoice-errors') { $taskAgentFile = 'fix-invoice-errors.windows.md' }
  Copy-Item -LiteralPath (Join-Path $taskSource "agents/$taskAgentFile") -Destination ".opencode/agents/$taskAgentName.md"
}
```

## Linux / macOS

先进入报销项目根目录，然后执行：

```bash
(
  set -eu
  SRC=/tmp/reimbursement-skill-opencode
  if [ -e "$SRC" ] || [ -e .opencode/skills/reimbursement ]; then
    echo '安装源或报销技能已存在，请先检查再更新。'
    exit 1
  fi
  git clone --branch opencode --single-branch https://github.com/guanggaofei/reimbursement-skill.git "$SRC"
  for name in fix-bearing-invoice fix-duplicate-screenshots fix-shop-name-ambiguity fix-trip-ambiguity fix-invoice-errors; do
    if [ -e ".opencode/agents/$name.md" ]; then
      echo "子代理已存在，请先检查：$name"
      exit 1
    fi
  done
  mkdir -p .opencode/skills/reimbursement .opencode/agents invoices images
  cp -R "$SRC/skills/reimbursement/scripts" "$SRC/skills/reimbursement/assets" .opencode/skills/reimbursement/
  cp "$SRC/skills/reimbursement/SKILL.md" .opencode/skills/reimbursement/SKILL.md
  cp -R "$SRC/skills/reimbursement/agents" .opencode/skills/reimbursement/
  for name in fix-bearing-invoice fix-duplicate-screenshots fix-shop-name-ambiguity fix-trip-ambiguity fix-invoice-errors; do
    cp "$SRC/agents/$name.md" ".opencode/agents/$name.md"
  done
)
```

Windows 安装时，将 `SKILL.windows.md` 复制为目标 `SKILL.md`，并将 `fix-invoice-errors.windows.md` 复制为目标 `fix-invoice-errors.md`；Linux/macOS 使用各自不带 `.windows` 的入口。不要把 Windows 源文件作为额外技能或子代理重复安装。

已克隆对应分支时，可将上方安装源变量改为该克隆的绝对路径，跳过克隆步骤，从创建目标目录和复制文件的步骤继续；仍需检查目标文件，不能覆盖用户已有修改。

## 安装验证与交付

确认 `.opencode/skills/reimbursement/SKILL.md`、`scripts/`、`assets/templates/` 已存在。确认 `.opencode/agents/` 中五个 `fix-*.md` 子代理定义完整。

安装完成后，明确告知用户安装位置，并提醒：重启 opencode，开启一个新会话以加载技能和子代理。在报销项目中使用：

```text
请使用 reimbursement 技能整理当前项目的报销材料。
```

本次安装只复制技能和子代理文件，不运行发票提取、清理或 OCR。Python 包在用户启动报销流程后按技能要求检查，使用该报销项目的 `.venv` 安装；不安装到系统 Python。
