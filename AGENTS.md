# AGENTS.md

Repository: https://github.com/guanggaofei/reimbursement-skill

`main` is the development and default branch. It retains the skill source, subagent
definitions, templates, tests, and this development guide. Its README serves only
as an installation router; keep architecture and contributor instructions here.

The three installation branches are `codex`, `opencode`, and `claude-code`.
Each branch README documents only its own installation, with links redirecting
agents targeting another framework. All three support Windows and Linux/macOS.
Keep tests only on `main`; do not ship `tests/`, `test/`, or test files on installation
branches. Removing tests there does not remove them from main or Git history.

Preserve the existing framework directories. On main, `skills/reimbursement/`
contains Codex entrypoints and the shared scripts/templates, `agents/` contains
opencode subagent definitions, and `.claude/` contains Claude Code entrypoints and
subagents. opencode entrypoints live on its installation branch under
`skills/reimbursement/`. Never overwrite a release branch's framework entrypoints
with the Codex ones when synchronizing common scripts.

Codex installs into `.agents/skills/reimbursement/`; its `references/fix-*.md` are
task instructions, not registered agent types. opencode uses `.opencode/skills/`
and `.opencode/agents/`. Claude Code uses `.claude/skills/` and `.claude/agents/`.
Only the main workflow applies repair/action JSON in all editions.

## What this is for

Read `skills/reimbursement/SKILL.md` (Linux/macOS) or `SKILL.windows.md` first — it is the authoritative
8-step process description. This file only covers things that aren't obvious from reading that file: the
invariants the code depends on, and gotchas that have already caused bugs once.

## Architecture

```
skills/reimbursement/
  SKILL.md / SKILL.windows.md   the process, step by step (this is what the agent follows)
  agents/openai.yaml            Codex UI metadata / default prompt
  references/fix-*.md         Codex repair tasks and action contracts
  assets/templates/             Hello World 报账单.xlsx, 支出记录.docx, 支付说明.docx templates
  scripts/                      all the Python — see below
agents/*.md                    opencode subagent definitions
.claude/skills/reimbursement/   Claude Code Unix and Windows entrypoints
.claude/agents/*.md             Claude Code cross-platform subagent definitions
tests/test_file_layout.py       the only test file; run with pytest
```

### The invoice pipeline, in one paragraph

`super_invoice.py` OCRs `invoices/*.pdf`, extracts fields, and calls `sort_invoices()` to classify every
invoice into exactly one of five categories and copy it into `output/<category>/`, renaming it
`{global_seq}_价税合计_xxx_发票.pdf`. That `global_seq` is later parsed back out by
`_matching_records.display_index()` and is what payment-material filenames (`xxx_{idx}_支付记录.docx`) and
the merged print PDF's stamped invoice numbers are keyed on. Everything downstream — `generate_reimbursement_xlsx.py`,
`generate_expense_record_docx.py`, `generate_payment_record_docx.py`, `generate_payment_explanations.py`,
`generate_high_value_invoices.py`, `merge_output_pdfs.py` — reads `invoice_results_sorted.json` /
`invoice_errors.json` / `匹配记录.json` and decides per-invoice whether to include it.

### The five categories are one shared judgment, not two

`skills/reimbursement/scripts/_invoice_filters.py` is the **single source of truth** for "what category is
this invoice." `super_invoice.sort_invoices()` imports it to decide `output/` placement and `global_seq`;
every generator imports it to decide whether an invoice reaches 报账单/支出记录/支付说明/支付记录/合并PDF.

**Never reimplement one of these predicates locally in a generator script.** If classification and
downstream filtering disagree even once, an invoice can be printed with a sequence number stamped on it
while having no 报账单 row (or vice versa) — and every invoice after it in that category is then off by
one on the printed page. This has happened twice already (see git history): once because a generator's
local copy of "is this 辰景/大额" drifted from `super_invoice.py`'s, and once because a mixed-price
invoice (e.g. items priced `[800, 1500]`) was classified as 材料费 (needs only *one* item ≤1000) while the
filter used elsewhere required *all* items ≤1000.

The five categories, in `output/` folder order (also the `global_seq` order):

| Folder | Predicate | Downstream treatment |
| --- | --- | --- |
| `1_材料费` | `is_material_fee` — every item price ≤1000, no 住宿/运输 items | full online + offline flow |
| `2_打车费` | `is_transport_fee` — filename or every item name signals 打车/运输 | full flow, trip sheet (行程单) attached |
| `3_高价发票` | `is_high_value` — any item price >1000, buyer not 辰景 | **excluded** from 报账单/支出记录/合并PDF/支付说明/支付记录; routed to `generate_high_value_invoices.py` (单价大额发票汇总表) instead |
| `4_辰景发票` | `is_chenjing` — buyer name contains 辰景 | stays in 报账单/支出记录; excluded from the printed PDF (submitted as electronic invoice separately) |
| `5_未匹配` | `is_unclassified` — none of the above matched (usually: item extraction failed, or a non-辰景 lodging invoice) | stays in the ordinary flow (not silently dropped) but flagged in `invoice_errors.json` for manual review |

These five predicates are mutually exclusive and exhaustive — every invoice matches exactly one. If you
add a sixth category, keep that property (there's a test for it:
`InvoiceCategoryTests.test_every_invoice_lands_in_exactly_one_category`).

Unit prices are parsed with `Decimal(str(value))`, never `isinstance(..., (int, float))` comparisons —
《报销材料整理使用说明》 warns that users hand-fixing `ERROR` fields often leave the value quoted
(`"1.00"` instead of `1.00`), and a quoted number must classify identically to a bare one.

### Two independent error pipelines — don't conflate them

- `check_invoice_errors.py` → `报销工作文件/invoice_errors_raw.json`: scans for literal `"ERROR"` /
  `"需人工校验"` field values. Drives the `fix-invoice-errors` fix loop (max 3 rounds). This is about
  **extraction quality**, not business rules.
- `super_invoice.check_invoice_errors()` (confusingly similar name, different function) → root
  `invoice_errors.json`: business-rule findings (家具/日用杂品/单价超1000/连号发票/打车缺行程单/未匹配分类).
  This is the file that drives `generate_payment_record_docx.py` and `generate_payment_explanations.py`
  (they scan for `问题原因` containing both "支付说明" and "支付记录"), and is also what gets uploaded to
  飞书 as-is.

Adding a new business-rule error category is safe — the fix-loop won't try to "fix" it. Just make sure its
`问题原因` string doesn't accidentally contain both trigger substrings unless it should actually generate
payment materials.

### 连号发票 groups are all-or-nothing

If any invoice in a 连号 group needs 支付说明/支付记录, *every* invoice in that group does — don't filter
individual invoices out of a group. 大额发票 can't appear in a 连号 group in the first place (the check
that builds 连号 groups skips 高价 invoices), so no filtering is needed there; a single-invoice entry
(not part of a group) *does* still need the 大额 filter, since 检查5 (价税合计超1000元) doesn't skip 高价
invoices when flagging them.

### Root vs `报销工作文件/`

Root holds inputs, stable state (`OCR缓存.json`, `匹配记录.json`), and anything the user needs to read,
edit, or upload. `报销工作文件/` (see `_pathutil.INTERNAL_DIR`) holds everything else: raw error/action
JSON, unpacked DOCX/XML debug output, and payment-material DOCX before the user renames the `xxx`
placeholder in them. When adding a new script, decide which bucket its output belongs in before writing
the `--output` default.

### The OCR step can't be run by the agent directly

`organize_expense_records.py` (OCR matching) can run long enough to hit the agent runtime's timeout. Per
`SKILL.md` step 5, the agent must pause and have the *user* run it in their own terminal, then wait for
confirmation before continuing. Don't try to "fix" this by running it in the background or backgrounding
the process — it's a documented constraint, not an oversight.

## Environment

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    pdfplumber rapidocr-onnxruntime onnxruntime Pillow pypinyin pypdf python-docx lxml
```

On Windows use `.venv\Scripts\python.exe`. All Unix scripts are invoked as `.venv/bin/python .agents/skills/reimbursement/scripts/<script>.py --root .`
— never the system `python`/`python3`. Poppler (`pdftotext`, `pdftoppm`) must be installed separately and
on `PATH`.

On this development machine, a plain `pip install cryptography` can pull a wheel whose OpenSSL symbols
(`_DTLS_get_data_mtu`) don't exist in the system's libssl, which makes `import pdfplumber` fail with a
`dlopen` `ImportError` (pdfplumber pulls in `cryptography` transitively). Pin `cryptography<42` (tested:
`41.0.7`) in that case. This is a local environment quirk, not a project dependency constraint — don't
add a version pin to any install command in `SKILL.md` because of it.

## Testing

Run tests on `main` using the project virtual environment. Windows equivalent:
`.\.venv\Scripts\python.exe -m pytest tests/test_file_layout.py -v`.

```bash
.venv/bin/python -m pytest tests/test_file_layout.py -v
```

`tests/test_file_layout.py` imports directly from `skills/reimbursement/scripts/` (it inserts that
directory onto `sys.path`) and is the only place classification, filename, and path-layout invariants are
checked. `super_invoice.py` itself imports `pdfplumber` and `pypinyin` at module scope, so tests that need
its classification logic import it from `_invoice_filters.py` instead (which has no heavy dependencies) —
keep it that way rather than mocking `pdfplumber` out.

When you change a predicate in `_invoice_filters.py`, run the full suite — `generate_reimbursement_xlsx.py`,
`generate_expense_record_docx.py`, `generate_payment_record_docx.py`, and `generate_payment_explanations.py`
all depend on it agreeing with `super_invoice.py`'s classification, and a silent disagreement only shows up
as an off-by-one in the printed PDF, not as an exception.

## Windows parity

Each framework's `SKILL.windows.md` mirrors its `SKILL.md`. Any process change
(new script, new step, new cleanup target) must update both operating-system entries.
Claude Code ships both files under `.claude/skills/reimbursement/`; its Windows
shell-enabled subagents allow PowerShell as well as Bash, with the same narrow
read/compute restrictions. Git Bash uses `./.venv/Scripts/python.exe`, PowerShell
uses `.\.venv\Scripts\python.exe`; never send PowerShell syntax directly to Bash.

Before publishing, verify the four branch names, tests only on main, the same
shared scripts/templates on all branches, valid installation paths and links,
and complete Windows and Unix packages. Synchronize selected common changes;
do not merge all of main into release branches and reintroduce tests or other
framework installation instructions. Push ordinary fast-forward updates to
`https://github.com/guanggaofei/reimbursement-skill.git` without rewriting history.
