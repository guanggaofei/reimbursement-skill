# AGENTS.md

这是 reimbursement-skill 的 Codex 安装分支（`codex`）。
安装任务先读取本分支 README.md，只安装其中列出的文件。
开发、业务规则修改和测试在 [main 分支](https://github.com/guanggaofei/reimbursement-skill/blob/main/AGENTS.md) 完成；本安装分支不保存测试文件。

运行报销流程前读取对应平台的技能入口。保留原始输入和稳定匹配状态；
不要绕过用户手动运行长时间 OCR 的步骤。分类规则统一使用
`skills/reimbursement/scripts/_invoice_filters.py`；修复任务只写修复/action JSON，
由主流程串行应用。分支同步时保留本框架的入口、子代理调用方式和安装 README。
