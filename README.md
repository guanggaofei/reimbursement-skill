# reimbursement-skill 安装入口

本页用于引导 Agent 为当前报销项目选择并安装正确的版本。仓库地址：[guanggaofei/reimbursement-skill](https://github.com/guanggaofei/reimbursement-skill)。

## 1. 确定使用的 Agent 框架

优先遵循用户明确指定的安装目标；未指定时，使用当前实际运行的框架。不能根据模型名称、操作系统或目录中残留的配置文件猜测框架。仍无法确定时，只询问用户要安装到 Codex、Claude Code 还是 opencode。

| 安装目标 | 必须读取的安装说明 |
| --- | --- |
| Codex | [codex 分支 README](https://github.com/guanggaofei/reimbursement-skill/blob/codex/README.md) |
| Claude Code | [claude-code 分支 README](https://github.com/guanggaofei/reimbursement-skill/blob/claude-code/README.md) |
| opencode | [opencode 分支 README](https://github.com/guanggaofei/reimbursement-skill/blob/opencode/README.md) |

三个版本均提供 Windows 原生环境与 Linux/macOS 的安装方法；WSL 使用 Linux 方法。

## 2. 进入对应分支完成安装

1. 读取目标分支 README 的完整内容，按照其中的步骤安装。不要仅凭本页链接或其他框架的命令安装。
2. 确定用户保存本批报销材料的项目根目录。将仓库克隆到系统临时目录作为安装源，仅把目标框架所需的 skill、脚本、模板和子代理定义复制到报销项目。
3. 明确选择目标分支：Codex 对应 `codex`，Claude Code 对应 `claude-code`，opencode 对应 `opencode`。不要使用默认 `main` 分支作为安装源。
4. 按目标操作系统执行该分支的安装命令；检查目标文件已到位，原有同名技能或子代理存在时先核对再更新。
5. 告知用户安装位置、使用入口，以及该框架 README 要求的重启或新会话操作。

`main` 是开发主分支。安装时不复制主分支的测试文件和 `AGENTS.md`，也不开始提取发票、清理报销材料或运行 OCR；用户另行要求整理报销材料后，再按已安装技能执行。
