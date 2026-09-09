# reimbursement-skill：Claude Code 安装

本页只用于给 Claude Code 安装报销材料整理技能。若安装目标是其他框架，请转到 [Codex 分支 README](https://github.com/guanggaofei/reimbursement-skill/blob/codex/README.md) 或 [opencode 分支 README](https://github.com/guanggaofei/reimbursement-skill/blob/opencode/README.md)；不确定目标时返回 [主分支安装入口](https://github.com/guanggaofei/reimbursement-skill/blob/main/README.md)。

## 安装前

- 确认目标框架为 Claude Code，安装源分支为 `claude-code`。
- 当前工作目录必须是用户保存本批报销材料的项目根目录。
- 需要 Git、Python 3.10+（包含 venv、pip）和 Poppler 的 `pdftotext`、`pdftoppm`。Windows 将 Poppler 的 `Library\bin` 加入 PATH，并确保源 PDF 所用中文字体可用。
- 仓库仅克隆到系统临时目录作为安装源。已有安装源或同名技能/子代理时先检查，再决定更新；以下首次安装命令会停止，避免直接覆盖。
- WSL 使用 Linux 命令及 Linux 虚拟环境；Windows 原生环境使用 PowerShell 命令。

## Windows PowerShell

先进入报销项目根目录，然后执行：

```powershell
$ErrorActionPreference = 'Stop'
$taskSource = Join-Path $env:TEMP 'reimbursement-skill-claude-code'
if (Test-Path -LiteralPath $taskSource) { throw "安装源已存在，请先检查：$taskSource" }
if (Test-Path -LiteralPath '.claude/skills/reimbursement') { throw '报销技能已存在，请先检查再更新' }
git clone --branch claude-code --single-branch https://github.com/guanggaofei/reimbursement-skill.git $taskSource
if ($LASTEXITCODE -ne 0) { throw '克隆失败' }
$taskAgentNames = @('fix-bearing-invoice', 'fix-duplicate-screenshots', 'fix-shop-name-ambiguity', 'fix-trip-ambiguity', 'fix-invoice-errors')
foreach ($taskAgentName in $taskAgentNames) {
  if (Test-Path -LiteralPath ".claude/agents/$taskAgentName.md") {
    throw "子代理已存在，请先检查：$taskAgentName"
  }
}
New-Item -ItemType Directory -Force '.claude/skills/reimbursement', '.claude/agents', invoices, images | Out-Null
Copy-Item -LiteralPath (Join-Path $taskSource 'skills/reimbursement/scripts'), (Join-Path $taskSource 'skills/reimbursement/assets') -Destination '.claude/skills/reimbursement' -Recurse
Copy-Item -LiteralPath (Join-Path $taskSource '.claude/skills/reimbursement/SKILL.md'), (Join-Path $taskSource '.claude/skills/reimbursement/SKILL.windows.md') -Destination '.claude/skills/reimbursement'
foreach ($taskAgentName in $taskAgentNames) {
  Copy-Item -LiteralPath (Join-Path $taskSource ".claude/agents/$taskAgentName.md") -Destination ".claude/agents/$taskAgentName.md"
}
```

## Linux / macOS

先进入报销项目根目录，然后执行：

```bash
(
  set -eu
  SRC=/tmp/reimbursement-skill-claude-code
  if [ -e "$SRC" ] || [ -e .claude/skills/reimbursement ]; then
    echo '安装源或报销技能已存在，请先检查再更新。'
    exit 1
  fi
  git clone --branch claude-code --single-branch https://github.com/guanggaofei/reimbursement-skill.git "$SRC"
  for name in fix-bearing-invoice fix-duplicate-screenshots fix-shop-name-ambiguity fix-trip-ambiguity fix-invoice-errors; do
    if [ -e ".claude/agents/$name.md" ]; then
      echo "子代理已存在，请先检查：$name"
      exit 1
    fi
  done
  mkdir -p .claude/skills/reimbursement .claude/agents invoices images
  cp -R "$SRC/skills/reimbursement/scripts" "$SRC/skills/reimbursement/assets" .claude/skills/reimbursement/
  cp "$SRC/.claude/skills/reimbursement/SKILL.md" .claude/skills/reimbursement/SKILL.md
  cp "$SRC/.claude/skills/reimbursement/SKILL.windows.md" .claude/skills/reimbursement/SKILL.windows.md
  for name in fix-bearing-invoice fix-duplicate-screenshots fix-shop-name-ambiguity fix-trip-ambiguity fix-invoice-errors; do
    cp "$SRC/.claude/agents/$name.md" ".claude/agents/$name.md"
  done
)
```

两个平台都安装 `SKILL.md` 与 `SKILL.windows.md`；主入口在 Windows 上引导读取 PowerShell 流程。五个子代理共用跨平台定义，字段读取和金额计算子代理支持 Bash / PowerShell。脚本和模板仍从仓库的 `skills/reimbursement/` 复制，安装后放在 `.claude/skills/reimbursement/`。

已克隆对应分支时，可将上方安装源变量改为该克隆的绝对路径，跳过克隆步骤，从创建目标目录和复制文件的步骤继续；仍需检查目标文件，不能覆盖用户已有修改。

## 安装验证与交付

确认 `.claude/skills/reimbursement/SKILL.md`、`scripts/`、`assets/templates/` 已存在。确认 `.claude/agents/` 中五个 `fix-*.md` 子代理定义完整。确认 `SKILL.windows.md` 也已安装。

安装完成后，明确告知用户安装位置，并提醒：重启 Claude Code，开启一个新会话以加载技能和子代理。在报销项目中使用：

```text
请使用 reimbursement 技能整理当前项目的报销材料。
```

本次安装只复制技能和子代理文件，不运行发票提取、清理或 OCR。Python 包在用户启动报销流程后按技能要求检查，使用该报销项目的 `.venv` 安装；不安装到系统 Python。
