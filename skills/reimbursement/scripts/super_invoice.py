import pdfplumber
import re
import os
import json
import shutil
import subprocess
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple, Set
from pypinyin import lazy_pinyin

from _pathutil import add_root_arg, resolve_path
from _invoice_filters import (
    HIGH_VALUE_THRESHOLD,
    is_chenjing,
    is_high_value,
    is_material_fee,
    is_transport_fee,
    is_unclassified,
    parse_amount,
)

r"""
super_invoice — invoice extraction, dedup, sorting, and PDF classification.

Two-run workflow
    First run: extract all PDFs in invoices/, produce invoice_results.json.
    Agent fixes ERROR / 需人工校验 fields, then re-runs WITHOUT cleanup.
    Second run (when invoice_results.json is clean): produces
    invoice_results_sorted.json, output/, and invoice_errors.json.

PDF text extraction
    Uses the system ``pdftotext -layout`` binary (poppler-utils) as the
    primary extraction method.  Its column-aware output avoids the
    quantity+unit-price merge bug that pdfplumber can produce (e.g.
    ``"1155.75"`` → ``"1 155.75"``).  Falls back to pdfplumber if
    pdftotext is not installed.

Intra-batch deduplication
    When two PDFs share the same invoice number, the duplicate is removed
    from invoice_results.json automatically.  The duplicate PDF file is
    renamed to ``.backup`` (e.g. ``foo.pdf`` → ``foo.pdf.backup``) so that
    subsequent runs do not re-extract the already-removed invoice.

Missing-file cleanup
    If a PDF listed in invoice_results.json no longer exists in invoices/,
    the entry is removed from invoice_results.json. This is how cross-batch
    dedup (which removes PDFs via .backup) triggers JSON cleanup on the next
    run.

output/ behaviour
    output/ is NOT cleaned between runs. Files are copied in with current
    sequence numbers. If the invoice count changes (dedup, new invoices),
    old sequence-numbered files remain. Delete output/ manually before
    a fresh workflow to avoid stale files.
"""

# -------------------------- 颜色配置：终端输出颜色 --------------------------
class Colors:
    """ANSI颜色代码（Windows/Linux终端通用）"""
    RESET = '\033[0m'
    RED = '\033[91m'      # 红色（ERROR）
    YELLOW = '\033[93m'   # 黄色（WARNING）
    GREEN = '\033[92m'    # 绿色（INFO）
    BLUE = '\033[94m'     # 蓝色
    CYAN = '\033[96m'     # 青色

# -------------------------- 辅助函数：分类判断逻辑 --------------------------
def is_valid_invoice_number(num: str) -> bool:
    """验证发票号码是否有效：全数字且位数为8位或20位"""
    if not num:
        return False
    return num.isdigit() and len(num) in (8, 20)


def compact_whitespace(value: Any) -> str:
    """Remove OCR-inserted whitespace before comparing identifiers."""
    return re.sub(r"\s+", "", str(value or ""))

# 发票分类判定（is_material_fee / is_transport_fee / is_high_value / is_chenjing /
# is_unclassified）统一定义在 _invoice_filters.py。分类决定发票进哪个 output/ 子目录、
# 拿到哪个全局序号，下游生成器用同一组判定决定它进不进报账单和合并 PDF；两边必须同源，
# 否则会出现「被打印却没有报账单行」的序号错位。

def extract_buyer_seller_names(text_lines_clean: List[str], full_text: str) -> Dict[str, str]:
    """提取购买方和销售方名称"""
    # 主逻辑：直接匹配购/销名称
    name_match = re.search(r"购\s*名称[:：]\s*([^\n]+?)\s+销\s*名称[:：]\s*([^\n]+)", full_text)
    if name_match:
        return {
            "购买方名称": name_match.group(1).strip(),
            "销售方名称": name_match.group(2).strip()
        }

    name_match2 = re.search(r"买\s*名\s*称\s*[:：]\s*([^\n]+?)\s+售\s*名\s*称\s*[:：]\s*([^\n]+)", full_text)
    if name_match2:
        return {
            "购买方名称": name_match2.group(1).strip(),
            "销售方名称": name_match2.group(2).strip()
        }

    # 备用逻辑：从*税务局下一行提取
    tax_bureau_idx = next(
        (i for i, line in enumerate(text_lines_clean) if re.search(r".*税务局", line)),
        None
    )
    
    if tax_bureau_idx is not None and (tax_bureau_idx + 1) < len(text_lines_clean):
        name_line = text_lines_clean[tax_bureau_idx + 1]
        name_blocks = re.split(r"\s+", name_line.strip())
        if len(name_blocks) >= 2:
            if "共" in name_blocks[0] and "页" in name_blocks[0]:
                name_line = text_lines_clean[tax_bureau_idx + 2]
                name_blocks = re.split(r"\s+", name_line.strip())
            if len(name_blocks) >= 2:
                return {
                    "购买方名称": name_blocks[0].strip(),
                    "销售方名称": name_blocks[1].strip()
                }
    
    name_match3 = re.search(r"名称[:：]\s*([^\n]+?)\s+名称[:：]\s*([^\n]+)", full_text)
    if name_match3:
        return {
            "购买方名称": name_match3.group(1).strip(),
            "销售方名称": name_match3.group(2).strip()
        }
    
    name_match4 = re.search(r"买\s*名\s*称\s*([^\n]+?)\s+售\s*名\s*称\s*([^\n]+)", full_text)
    if name_match4:
        return {
            "购买方名称": name_match4.group(1).strip(),
            "销售方名称": name_match4.group(2).strip()
        }
    
    return {"购买方名称": "ERROR", "销售方名称": "ERROR"}


def extract_tax_ids(text_lines_clean: List[str], full_text_flat: str, tax_bureau_idx: Optional[int]) -> Dict[str, str]:
    """提取购买方和销售方税号

    full_text_flat has all whitespace collapsed to single spaces, so
    literal tokens are made whitespace-tolerant with ``_flex()`` and
    numeric captures accept embedded spaces (stripped after matching).
    """
    def _flex(token: str) -> str:
        """Return a regex fragment matching `token` with optional whitespace between any characters."""
        return r'\s*'.join(re.escape(c) for c in token)

    tax_id_label = _flex("统一社会信用代码/纳税人识别号")
    tax_id_cap = r"([A-Z0-9\s]+)"
    # 主逻辑：直接匹配信用代码
    tax_match = re.search(
        rf"信\s*{tax_id_label}[:：]\s*{tax_id_cap}\s+信\s*{tax_id_label}[:：]\s*{tax_id_cap}",
        full_text_flat
    )
    if tax_match:
        return {
            "购买方税号": re.sub(r'\s+', '', tax_match.group(1)),
            "销售方税号": re.sub(r'\s+', '', tax_match.group(2))
        }
    
    tax_match2 = re.search(
        rf"{tax_id_label}[:：]\s*{tax_id_cap}\s+\s*{tax_id_label}[:：]\s*{tax_id_cap}",
        full_text_flat
    )
    if tax_match2:
        return {
            "购买方税号": re.sub(r'\s+', '', tax_match2.group(1)),
            "销售方税号": re.sub(r'\s+', '', tax_match2.group(2))
        }

    tax_id_label2 = _flex("统一社会信用代码纳税人识别号")
    tax_match3 = re.search(
        rf"{tax_id_label2}[:：]\s*{tax_id_cap}\s+\s*{tax_id_label2}[:：]\s*{tax_id_cap}",
        full_text_flat
    )
    if tax_match3:
        return {
            "购买方税号": re.sub(r'\s+', '', tax_match3.group(1)),
            "销售方税号": re.sub(r'\s+', '', tax_match3.group(2))
        }

    tax_id_label3 = _flex("统一社会信用代码")
    tax_id_label4 = _flex("纳税人识别号")
    tax_match4 = re.search(
        rf"{tax_id_label3}\s*{tax_id_label4}\s*{tax_id_cap}\s+\s*{tax_id_label3}\s*{tax_id_label4}\s*{tax_id_cap}",
        full_text_flat
    )
    if tax_match4:
        return {
            "购买方税号": re.sub(r'\s+', '', tax_match4.group(1)),
            "销售方税号": re.sub(r'\s+', '', tax_match4.group(2))
        }
    
    # 备用逻辑：从*税务局下第二行提取
    if tax_bureau_idx is not None and (tax_bureau_idx + 2) < len(text_lines_clean):
        tax_line = text_lines_clean[tax_bureau_idx + 2]
        tax_blocks = re.split(r"\s+", tax_line.strip())

        name_line = text_lines_clean[tax_bureau_idx + 1]
        name_blocks = re.split(r"\s+", name_line.strip())
        if len(name_blocks) >= 2:
            if "共" in name_blocks[0] and "页" in name_blocks[0]:
                tax_line = text_lines_clean[tax_bureau_idx + 3]
                tax_blocks = re.split(r"\s+", tax_line.strip())
            
            if len(tax_blocks) >= 2:
                if re.match(r"[A-Z0-9]+", tax_blocks[0]) and re.match(r"[A-Z0-9]+", tax_blocks[1]):
                    return {
                        "购买方税号": tax_blocks[0].strip(),
                        "销售方税号": tax_blocks[1].strip()
                    }

    return {"购买方税号": "ERROR", "销售方税号": "ERROR"}


def extract_invoice_number(full_text_flat: str, full_text_clean: str) -> Dict[str, str]:
    """提取发票号码及状态
    full_text_flat tolerates embedded whitespace in numeric fields (e.g. '2633200 0005317 959991').
    """
    # 主逻辑：直接匹配发票号码
    num_match = re.search(r"发票号码[:：]\s*([\d\s]+)", full_text_flat)
    if num_match:
        num = re.sub(r'\s+', '', num_match.group(1))
        if is_valid_invoice_number(num):
            return {
                "发票号码": num,
                "发票号码状态": "正常"
            }

    # 备用逻辑：多规则匹配
    candidate_patterns = [
        re.search(r"统一发票监.*?(\d{20})", full_text_clean),
        re.search(r"统一发票监.*?(\d{8})", full_text_clean),
        re.search(r"(\d{20})", full_text_clean),
        re.search(r"(\d{8})", full_text_clean)
    ]

    candidates = [match.group(1) for match in candidate_patterns if match]
    valid_candidates = [num for num in candidates if is_valid_invoice_number(num)]

    if valid_candidates:
        return {
            "发票号码": valid_candidates[0],
            "发票号码状态": "需人工校验"
        }

    return {"发票号码": "ERROR", "发票号码状态": "ERROR"}


def extract_invoice_date(full_text: str) -> List[Dict[str, str]]:
    """提取开票时间"""
    # 主规则匹配
    date_patterns = [
        re.compile(r"开票日期[:：]\s*(\d{4}\s*年\s*\d{2}\s*月\s*\d{2}\s*日)", re.IGNORECASE),
        re.compile(r"开票日期[:：]\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
    ]
    
    date_match = next((p.search(full_text) for p in date_patterns if p.search(full_text)), None)
    date_text = date_match.group(1) if date_match else ""

    # 备用规则
    if not date_text:
        special_match = re.search(r"国家税务总局\s*(\d{4}\s*年\s*\d{2}\s*月\s*\d{2}\s*日)", full_text)
        date_text = special_match.group(1) if special_match else "ERROR"

    # 格式化日期
    if date_text != "ERROR":
        cn_match = re.search(r"(\d{4})\s*年\s*(\d{2})\s*月\s*(\d{2})\s*日", date_text)
        dash_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", date_text)
        
        if cn_match:
            year, month, day = cn_match.groups()
            return [{"年": int(year), "月": int(month), "日": int(day)}]
        elif dash_match:
            year, month, day = dash_match.groups()
            return [{"年": int(year), "月": int(month), "日": int(day)}]

    return []


def extract_total_amount(full_text: str) -> str:
    """提取价税合计金额（返回浮点数格式，失败返回ERROR）"""
    total_match = re.search(r"价税合计.*?([\d,.]+)", full_text)
    if not total_match:
        return "ERROR"
    
    # 清理金额字符串（去除逗号分隔符）并转换为浮点数
    amount_str = total_match.group(1).replace(",", "").strip()
    try:
        return float(amount_str)
    except ValueError:
        # 若转换失败（如非数字格式），返回ERROR
        return "ERROR"


def extract_items(text_lines: List[str]) -> List[Dict[str, str]]:
    """提取项目列表及单价"""
    # 定位项目表格范围
    start_idx = next(
        (i + 1 for i, line in enumerate(text_lines) if re.search(r"项目名称", line, re.IGNORECASE)),
        None
    )
    
    if not start_idx:
        return [{"项目名称": "ERROR", "单价": "ERROR"}]

    end_idx = len(text_lines)
    for i, line in enumerate(text_lines[start_idx:]):
        if re.search(r"合计|价税合计|小计", line, re.IGNORECASE):
            end_idx = start_idx + i
            break

    # 提取项目内容
    items = []
    current_item = None
    if start_idx < end_idx:
        for line in text_lines[start_idx:end_idx]:
            line = line.strip()
            if not line or len(line) < 5 or re.search(r"合 计|价税合计|小计|出行人", line):
                continue

            fields = re.split(r'\s+', line)
            if len(fields) >= 6:
                if current_item:
                    items.append(current_item)

                if len(fields) >= 7:
                    price_str = fields[-4].strip().replace(",", "")
                else:
                    price_str = fields[1].strip().replace(",", "")
                
                try:
                    price = float(price_str)
                except ValueError:
                    price = "ERROR"

                current_item = {
                    "项目名称": fields[0].replace("*", ""),
                    "单价": price
                }
            elif current_item:
                current_item["项目名称"] += fields[0].replace("*", "")

        if current_item and "下载次数" not in current_item["项目名称"]:
            items.append(current_item)

    if not items:
        return [{"项目名称": "ERROR", "单价": "ERROR"}]

    return items

def get_matched_trip(pdf_path: str) -> str:
    """给打车费匹配行程单"""
    invoice_filename = os.path.basename(pdf_path)
    if "发票" not in invoice_filename:
        print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} 打车发票文件名格式异常，无法匹配行程单：{invoice_filename}")
        return "ERROR"
    else:
        trip_filename = invoice_filename.replace("发票", "行程单")
        trip_path = os.path.join(os.path.dirname(pdf_path), trip_filename)

        if os.path.exists(trip_path):
            return trip_filename
        else:
            print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} 找不到匹配的行程单：{trip_filename}")
            return "ERROR"

def _normalize_pdftotext(text: str) -> str:
    """Collapse runs of horizontal whitespace to single space, preserving line breaks.

    pdftotext -layout produces column-aligned output with repeated spaces.
    Normalizing reduces it to a pdfplumber-compatible format while keeping
    the improved column separation (e.g. ``"1 155.75"`` instead of merged
    ``"1155.75"``).
    """
    return '\n'.join(re.sub(r'[ \t]+', ' ', line) for line in text.split('\n'))


def extract_invoice_info(invoice_id: int, pdf_path: str) -> Dict:
    """提取单张PDF发票的完整信息"""
    invoice_info = {
        "发票序号": invoice_id,
        "购买方名称": "",
        "购买方税号": "",
        "销售方名称": "",
        "销售方税号": "",
        "发票号码": "",
        "发票号码状态": "正常",
        "开票时间": [],
        "价税合计金额": "",
        "项目列表": [],
        "行程单文件名": "",
        "更新后行程单文件名":"",
        "文件名": os.path.basename(pdf_path),
        "更新后文件名": "",
    }

    # Try system pdftotext first (better column separation), fall back to pdfplumber
    extraction_method = 'pdfplumber'
    try:
        result = subprocess.run(
            ['pdftotext', '-layout', pdf_path, '-'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            full_text = _normalize_pdftotext(result.stdout)
            extraction_method = 'pdftotext'
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        pass

    if extraction_method == 'pdfplumber':
        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join([page.extract_text() or "" for page in pdf.pages])

    # 预处理文本
    text_lines = full_text.split("\n")
    text_lines_clean = [line.strip() for line in text_lines if line.strip()]
    full_text_clean = " ".join(text_lines_clean)
    full_text_flat = re.sub(r'\s+', ' ', full_text)  # collapse all whitespace for field extraction

    # 提取*税务局行索引（供多个提取函数使用）
    tax_bureau_idx = next(
        (i for i, line in enumerate(text_lines_clean) if re.search(r".*税务局", line)),
        None
    )

    # 逐个提取信息
    name_info = extract_buyer_seller_names(text_lines_clean, full_text)
    invoice_info.update(name_info)

    tax_info = extract_tax_ids(text_lines_clean, full_text_flat, tax_bureau_idx)
    invoice_info.update(tax_info)

    number_info = extract_invoice_number(full_text_flat, full_text_clean)
    invoice_info.update(number_info)

    invoice_info["开票时间"] = extract_invoice_date(full_text)
    invoice_info["价税合计金额"] = extract_total_amount(full_text)
    invoice_info["项目列表"] = extract_items(text_lines)

    item_sum = invoice_info["价税合计金额"]
    if isinstance(item_sum, (int, float)):
        for item in invoice_info["项目列表"]:
            if item["单价"] != "ERROR":
                if item["单价"] > item_sum * 1.8:
                    item["单价"] = "ERROR"
        
    if is_transport_fee(invoice_info):
        invoice_info["行程单文件名"] = get_matched_trip(pdf_path)
    else:
        invoice_info["行程单文件名"] = "无需"

        
    return invoice_info

def batch_process_invoices(folder_path: str, existing_filenames: set) -> List[Dict]:
    """
    【按文件名过滤】批量处理文件夹中的PDF发票：
    仅处理不在existing_filenames中的新文件（通过原始文件名判断）
    :param folder_path: 发票文件夹路径
    :param existing_filenames: JSON中已存在的发票原始文件名集合
    :return: 新提取的发票信息列表
    """
    new_invoices = []
    # 新发票序号 = 已存在发票数（确保序号连续）
    invoice_id = len(existing_filenames)  
    
    for filename in os.listdir(folder_path):
        # 只处理PDF文件
        if "行程单" in filename:
            # print(f"{Colors.GREEN}[INFO]{Colors.RESET} 忽略行程单文件：{filename}")
            continue

        if not filename.lower().endswith(".pdf"):
            continue
        
        # 核心判断：文件名不在已存在集合中 → 新文件，需处理
        if filename in existing_filenames:
            # print(f"{Colors.GREEN}[INFO]{Colors.RESET} 发票已存在（文件名：{filename}），跳过处理")
            continue
        
        # 处理新发票
        pdf_path = os.path.join(folder_path, filename)
        try:
            info = extract_invoice_info(invoice_id, pdf_path)
            new_invoices.append(info)
            invoice_id += 1
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} 已处理新发票：{filename}（序号：{info['发票序号']}）")
        
        except Exception as e:
            print(f"{Colors.RED}[ERROR]{Colors.RESET} 处理 {filename} 出错: {str(e)}")
    
    return new_invoices

def save_to_json(invoices: List[Dict], output_file: str):
    """保存发票信息到JSON，自动计算并更新“发票总数”"""
    json_data = {
        "发票总数": len(invoices),  # 总数 = 已存在数 + 新文件数
        "发票信息": invoices
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    print(f"\n{Colors.GREEN}[INFO]{Colors.RESET} 结果已保存至: {os.path.abspath(output_file)}")

def load_json_data(json_path: str) -> Optional[Dict[str, Any]]:
    """
    读取JSON文件并返回数据，只读取一次供多个函数使用
    :param json_path: JSON文件路径
    :return: JSON数据字典，失败则返回None
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"{Colors.RED}[ERROR]{Colors.RESET} {json_path} 不是有效的JSON文件")
        return None
    except FileNotFoundError:
        print(f"{Colors.RED}[ERROR]{Colors.RESET} 未找到文件 {json_path}")
        return None
    except Exception as e:
        print(f"{Colors.RED}[ERROR]{Colors.RESET} 读取 {json_path} 时出错: {str(e)}")
        return None

def get_existing_file_info(json_data: Optional[Dict[str, Any]]) -> Tuple[Set[str], List[Dict]]:
    """
    【核心修改】从JSON中提取已存在的发票文件名集合和发票列表：
    以“文件名”作为唯一标识（而非发票号码）
    :param json_data: 已加载的JSON数据
    :return: (已存在文件名集合, 已存在发票列表)
    """
    existing_filenames: Set[str] = set()
    existing_invoices = []
    
    if not json_data or not isinstance(json_data, dict) or "发票信息" not in json_data:
        print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} JSON格式无效或无发票数据，视为无已存在文件")
        return existing_filenames, existing_invoices
    
    # 遍历JSON中的所有发票，提取“文件名”字段（原始文件名）
    for inv in json_data["发票信息"]:
        inv_filename = inv.get("文件名", "")  # “文件名”字段记录原始PDF名称
        if inv_filename and inv_filename.lower().endswith(".pdf"):
            existing_filenames.add(inv_filename)
        existing_invoices.append(inv)
    
    print(f"{Colors.GREEN}[INFO]{Colors.RESET} 从JSON中读取到 {len(existing_filenames)} 个已存在的发票文件名")
    return existing_filenames, existing_invoices

def check_json_need_regenerate(folder_path: str, json_data: Optional[Dict[str, Any]]) -> Tuple[bool, Set[str], List[Dict]]:
    """
    【核心逻辑】判断是否需要处理新发票：
    1. 提取JSON中已记录的所有发票文件名
    2. 对比文件夹中实际PDF文件名，检查是否有新文件
    :param folder_path: 发票文件夹路径
    :param json_data: 已加载的JSON数据
    :return: (是否有新文件需处理, 已存在文件名集合, 已存在发票列表)
    """
    if not json_data:
        print(f"{Colors.GREEN}[INFO]{Colors.RESET} JSON数据无效，视为无已存在文件，需处理所有发票")
        return True, set(), []
    
    # 步骤1：获取JSON中已有的文件名和发票列表
    existing_filenames, existing_invoices = get_existing_file_info(json_data)
    
    # 步骤2：统计文件夹中所有PDF文件数量
    folder_pdf_files = [f for f in os.listdir(folder_path) if f.lower().endswith(".pdf")]
    pdf_count = len(folder_pdf_files)
    
    if pdf_count == 0:
        print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} 发票文件夹中无PDF文件，无需处理")
        return False, existing_filenames, existing_invoices
    
    # 步骤3：检查是否有新文件（文件名不在已存在集合中）
    has_new_file = False
    for filename in folder_pdf_files:

        if "行程单" in filename:
            # print(f"{Colors.GREEN}[INFO]{Colors.RESET} 忽略行程单文件：{filename}")
            continue

        if filename not in existing_filenames:
            has_new_file = True
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} 检测到新发票文件：{filename}")
            # 无需遍历所有，找到一个新文件即可判断需要处理
            break
    
    # 步骤4：输出判断结果
    if has_new_file:
        print(f"{Colors.GREEN}[INFO]{Colors.RESET} 共检测到 {pdf_count} 个PDF文件，其中包含新文件，需要处理")
        return True, existing_filenames, existing_invoices
    else:
        print(f"{Colors.GREEN}[INFO]{Colors.RESET} 文件夹中 {pdf_count} 个PDF文件均已存在于JSON中（按文件名判断），无需处理新文件")
        return False, existing_filenames, existing_invoices

def sort_invoices(invoice_list: List[Dict], root: Optional[Path] = None, source_folder: str = "invoices") -> List[Dict]:
    """
    按需求排序发票并分类处理：
    1. 分类顺序：
       - 类别1：所有单价≤1000元的材料费（项目名不含住宿/运输）
       - 类别2：打车费发票（项目名含运输）
       - 类别3：存在单价>1000元且购买方不含辰景的发票
       - 类别4：购买方为辰景的发票
       - 类别5：以上都不匹配，需人工确认报销通道
    2. 分类内排序：开票时间升序 → 销售方名称拼音升序
    3. 分类处理：创建对应文件夹，复制发票并更新文件名，同步到JSON
    """
    # Resolve root path
    if root is None:
        root = Path.cwd()
    source_folder_str = str(resolve_path(root, Path(source_folder)))
    output_root = root / "output"
    # -------------------------- 1. 定义分类规则与文件夹配置 --------------------------
    # 分类配置：(类别名称, 类别判断函数, 文件夹名)
    categories = [
        (
            "材料费(所有单价≤1000元)",
            is_material_fee,
            "1_材料费"
        ),
        (
            "打车费(含运输)",
            is_transport_fee,
            "2_打车费"
        ),
        (
            "高价发票(单价>1000元_非辰景)",
            is_high_value,
            "3_高价发票"
        ),
        (
            "辰景发票",
            is_chenjing,
            "4_辰景发票"
        ),
        (
            # is_unclassified 是前四类的补集，因此每张发票恰好命中一类。
            # 未匹配发票单独成类而不再并入辰景：它们的购买方并不含「辰景」，
            # 混进辰景块会让后续检查跳过条件失效，也会把辰景的全局序号推后。
            "未匹配分类",
            is_unclassified,
            "5_未匹配"
        )
    ]

    # 创建所有分类文件夹（避免重复创建报错）
    for _, _, folder_name in categories:
        folder_path = output_root / folder_name
        os.makedirs(folder_path, exist_ok=True)
        print(f"{Colors.GREEN}[INFO]{Colors.RESET} 已确保分类文件夹存在: {folder_path}")

    # -------------------------- 2. 发票分类 --------------------------
    # 初始化分类容器
    categorized_invoices = {folder_name: [] for _, _, folder_name in categories}
    
    for invoice in invoice_list:
        # 遍历分类规则，匹配首个符合条件的类别
        for _, judge_func, folder_name in categories:
            if judge_func(invoice):
                categorized_invoices[folder_name].append(invoice)
                break
        else:
            # 防御性断言：is_unclassified 是前四类的补集，正常情况下走不到这里。
            categorized_invoices["5_未匹配"].append(invoice)
            print(f"{Colors.RED}[ERROR]{Colors.RESET} 发票{invoice['发票序号']}_{invoice['文件名']}未命中任何分类判定，分类判定集不完备，已放入5_未匹配")

    # -------------------------- 3. 分类内排序与重命名 --------------------------
    sorted_all = []  # 最终合并的排序结果
    global_seq = 0   # 全局排序序号（用于文件名）

    for folder_name in [c[2] for c in categories]:  # 按分类顺序处理
        invoices_in_cat = categorized_invoices[folder_name]
        if not invoices_in_cat:
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} 分类文件夹{folder_name}无发票，跳过排序")
            continue

        # 分类内排序：按时间→销售方拼音排序
        sorted_cat = sorted(invoices_in_cat, key=_get_sort_key)
        
        # 处理当前分类的发票：重命名→复制→更新JSON字段
        for seq_in_cat, invoice in enumerate(sorted_cat, 1):
            global_seq += 1  # 全局序号自增（确保唯一）
            
            # 生成新文件名：完成排序后的序号_价税总金额.pdf（保留2位小数避免科学计数）
            total_amount = invoice["价税合计金额"]
            if total_amount == "ERROR":
                amount_str = "ERROR"
            else:
                amount_str = f"{float(total_amount):.2f}".replace(".", "_")  # 替换小数点避免文件名问题
            new_filename = f"{global_seq}_价税合计_{amount_str}_发票.pdf"
            
            # 更新发票字典的"更新后文件名"字段（同步到JSON）
            invoice["更新后文件名"] = new_filename
            
            # 复制发票到对应分类文件夹
            source_path = os.path.join(source_folder_str, invoice["文件名"])
            target_path = os.path.join(str(output_root), folder_name, new_filename)
            try:
                shutil.copy2(source_path, target_path)  # copy2保留文件元数据
                # print(f"{Colors.GREEN}[INFO]{Colors.RESET} 已复制发票: {source_path} → {target_path}")

                # 若为打车费，处理行程单复制
                if folder_name == "2_打车费":
                    trip_filename = invoice.get("行程单文件名", "")
                    if trip_filename and  trip_filename != "ERROR":
                        source_trip_path = os.path.join(source_folder_str, trip_filename)

                        if not os.path.exists(source_trip_path):
                            print(f"{Colors.RED}[ERROR]{Colors.RESET} 找不到行程单文件：{source_trip_path}")
                            continue

                        new_trip_filename = new_filename.replace("发票", "行程单")
                        target_trip_path = os.path.join(str(output_root), folder_name, new_trip_filename)
                        try:
                            shutil.copy2(source_trip_path, target_trip_path)
                            # print(f"{Colors.GREEN}[INFO]{Colors.RESET} 已复制行程单: {source_trip_path} → {target_trip_path}")
                            invoice["更新后行程单文件名"] = new_trip_filename
                        except Exception as e:
                            print(f"{Colors.RED}[ERROR]{Colors.RESET} 复制行程单{trip_filename}出错: {str(e)}")
                    else:
                        print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} 发票{invoice['文件名']}无有效行程单文件名，跳过行程单复制")


            except Exception as e:
                print(f"{Colors.RED}[ERROR]{Colors.RESET} 复制发票{invoice['文件名']}出错: {str(e)}")
                invoice["更新后文件名"] = f"复制失败_{invoice['文件名']}"  # 标记失败状态

        # 将当前分类排序后的发票加入总结果
        sorted_all.extend(sorted_cat)

    # -------------------------- 4. 重新分配全局序号 --------------------------
    for idx, invoice in enumerate(sorted_all):
        invoice["发票序号"] = idx  # 覆盖原有序号，按最终排序重新编号

    print(f"{Colors.GREEN}[INFO]{Colors.RESET} 发票分类排序完成，共处理 {len(sorted_all)} 张发票")
    print(f"{Colors.GREEN}[INFO]{Colors.RESET} 分类统计:")
    for folder_name, invoices in categorized_invoices.items():
        print(f"  - {folder_name}: {len(invoices)} 张")
    return sorted_all

def _get_sort_key(invoice: Dict) -> tuple:
    """获取分类内排序键：(年, 月, 日, 销售方拼音)"""
    # 1. 处理开票时间（异常情况用极大值排在最后）
    try:
        date_info = invoice["开票时间"][0]
        year = int(date_info["年"])
        month = int(date_info["月"])
        day = int(date_info["日"])
    except (IndexError, KeyError, ValueError, TypeError):
        year = 9999
        month = 12
        day = 31

    # 2. 处理销售方拼音（异常情况用"zzzzz"排在最后）
    seller_name = invoice.get("销售方名称", "ERROR").strip()
    if seller_name == "ERROR" or not seller_name:
        seller_pinyin = "zzzzz"
    else:
        # 转换为无音调小写拼音（确保排序一致性）
        seller_pinyin = "".join(lazy_pinyin(seller_name, style=0)).lower()

    return (year, month, day, seller_pinyin)



def check_invoice_errors(sorted_invoices: List[Dict], allowed_buyers: List[Dict], root: Path) -> Dict[str, List[Dict]]:
    """
    执行所有问题检查逻辑，返回问题汇总
    :param sorted_invoices: 已排序的发票列表
    :param allowed_buyers: 允许的购买方列表（格式：[{"名称": "xxx", "税号": "xxx"}, ...]）
    :return: 问题汇总字典
    """
    errors = {
        "抬头税号错误": [],
        "家具": [],
        "日用杂品":[],
        "项目单价超1000元": [],
        "价税合计超1000元": [],
        "连号发票": [],
        "打车发票缺少行程单": [],
        "未匹配分类": []
    }

    # -------------------------- 检查1：抬头和税号是否匹配允许列表 --------------------------
    allowed_buyer_map = {
        (compact_whitespace(b["名称"]), compact_whitespace(b["税号"])): b
        for b in allowed_buyers
    }
    for invoice in sorted_invoices:
        buyer_name = invoice["购买方名称"].strip()
        buyer_tax = invoice["购买方税号"].strip()
        # 跳过提取问题的情况
        if buyer_name == "ERROR" or buyer_tax == "ERROR":
            continue
        # 检查是否在允许列表中
        buyer_key = (compact_whitespace(buyer_name), compact_whitespace(buyer_tax))
        if buyer_key not in allowed_buyer_map:
            errors["抬头税号错误"].append({
                "发票序号": invoice["发票序号"],
                "文件名": invoice["文件名"],
                "购买方名称": buyer_name,
                "购买方税号": buyer_tax,
                "问题原因": "不在允许的抬头税号列表中"
            })

    # -------------------------- 检查2：项目名称包含"家具"等敏感词 --------------------------
    sensitive_words = ["家具"]  # 可扩展敏感词列表
    for invoice in sorted_invoices:
        items = invoice["项目列表"]
        # 跳过提取问题的情况
        if items[0]["项目名称"] == "ERROR":
            continue
        # 检查每个项目名称
        for item in items:
            item_name = item["项目名称"]
            for word in sensitive_words:
                if word in item_name:
                    errors["家具"].append({
                        "发票序号": invoice["发票序号"],
                        "文件名": invoice["文件名"],
                        "项目名称": item_name,
                        "敏感词": word,
                        "问题原因": f"该类型发票无法报销'{word}'"
                    })
                    break  # 同一项目包含多个敏感词时，只记录一次

    # -------------------------- 检查3：项目名称包含"日用杂品"等敏感词 --------------------------
    sensitive_words2 = ["日用杂品"]  # 可扩展敏感词列表
    for invoice in sorted_invoices:
        items = invoice["项目列表"]
        # 跳过提取问题的情况
        if items[0]["项目名称"] == "ERROR":
            continue
        # 检查每个项目名称
        for item in items:
            item_name = item["项目名称"]
            for word in sensitive_words2:
                if word in item_name:
                    errors["日用杂品"].append({
                        "发票序号": invoice["发票序号"],
                        "文件名": invoice["文件名"],
                        "项目名称": item_name,
                        "敏感词": word,
                        "问题原因": f"该类型发票需要额外添加支付说明与支付记录'{word}'"
                    })
                    break  # 同一项目包含多个敏感词时，只记录一次

    # -------------------------- 检查4：项目单价超1000元 --------------------------
    max_price = float(HIGH_VALUE_THRESHOLD)
    for invoice in sorted_invoices:

        # 辰景发票不检查单价
        if is_chenjing(invoice):
            continue

        items = invoice["项目列表"]
        # 跳过提取问题的情况
        if items[0]["单价"] == "ERROR":
            continue
        # 检查每个项目单价。用 Decimal 解析而非直接比较：人工修正 ERROR 字段时
        # 常把数字写成带引号的字符串，字符串与 float 直接比较会抛 TypeError 导致闪退。
        for item in items:
            price = item["单价"]
            parsed = parse_amount(price)
            if parsed is not None and parsed > HIGH_VALUE_THRESHOLD:
                errors["项目单价超1000元"].append({
                    "发票序号": invoice["发票序号"],
                    "文件名": invoice["文件名"],
                    "项目名称": item["项目名称"],
                    "单价": price,
                    "问题原因": f"单价超 {max_price} 元，需走特殊通道报销"
                })

    # -------------------------- 检查5：价税合计超1000元 --------------------------
    max_total = float(HIGH_VALUE_THRESHOLD)
    for invoice in sorted_invoices:

        # 辰景发票不检查价税合计
        if is_chenjing(invoice):
            continue

        total = invoice["价税合计金额"]
        # 跳过提取问题的情况
        if total == "ERROR":
            continue
        parsed_total = parse_amount(total)
        if parsed_total is not None and parsed_total > HIGH_VALUE_THRESHOLD:
            errors["价税合计超1000元"].append({
                "发票序号": invoice["发票序号"],
                "文件名": invoice["文件名"],
                "价税合计金额": total,
                "问题原因": f"价税合计 {total} 元超过 {max_total} 元，需要提交支付说明与支付记录"
            })

    # -------------------------- 检查6：连号发票 --------------------------
    # 用字典记录已出现的 (开票时间, 销售方名称) 组合
    date_seller_groups = {}
    
    for invoice in sorted_invoices:
        # 提取开票时间（年、月、日）

        # 打车发票不检查连号，统一提交行程单
        if is_transport_fee(invoice):
            continue
        
        # 辰景发票不检查连号
        if is_chenjing(invoice):
            continue

        # 单价超1000元非辰景发票不检查连号
        if is_high_value(invoice):
            continue
        
        try:
            date_info = invoice["开票时间"][0]
            year = int(date_info["年"])
            month = int(date_info["月"])
            day = int(date_info["日"])
            invoice_date_str = f"{year:04d}-{month:02d}-{day:02d}"  # 格式化展示用
        except (IndexError, KeyError, ValueError, TypeError):
            continue  # 时间提取失败，跳过
          
        # 提取销售方名称
        seller_name = invoice.get("销售方名称", "ERROR").strip()
        if seller_name == "ERROR" or not seller_name:
            continue  # 销售方无效，跳过
          
        # 定义组键（唯一标识同一时间+同一销售方）
        group_key = (year, month, day, seller_name)
    
        # 将发票加入对应组（首次出现则创建组）
        if group_key not in date_seller_groups:
            date_seller_groups[group_key] = []
        date_seller_groups[group_key].append({
            "发票序号": invoice["发票序号"],
            "文件名": invoice["文件名"],
            "开票时间": invoice_date_str
        })
    
    # 遍历所有组，只处理包含2张及以上发票的重复组
    for group_key, invoices_in_group in date_seller_groups.items():
        if len(invoices_in_group) >= 2:  # 重复组（2张及以上）
            # 解析组键信息（用于问题描述）
            year, month, day, seller_name = group_key
            invoice_date_str = invoices_in_group[0]["开票时间"]  # 组内时间一致，取第一个即可
    
            # 生成错误记录：包含该组所有发票信息
            errors["连号发票"].append({
                "重复组信息": f"{invoice_date_str} | {seller_name}",
                "重复发票总数": len(invoices_in_group),
                "所有重复发票": [
                    {
                        "发票序号": inv["发票序号"],
                        "文件名": inv["文件名"]
                    } for inv in invoices_in_group
                ],
                "问题原因": f"共 {len(invoices_in_group)} 张发票为同一时间（{invoice_date_str}）且同一销售方（{seller_name}）, 需要额外添加支付说明与支付记录"
            })


    # -------------------------- 检查7：打车发票缺少行程单 --------------------------
    for invoice in sorted_invoices:
        if is_transport_fee(invoice):
            trip_filename = invoice.get("行程单文件名", "")
            if trip_filename == "ERROR" or not trip_filename or trip_filename == "无需" or not os.path.exists(os.path.join(str(root / "invoices"), trip_filename)):
                errors["打车发票缺少行程单"].append({
                    "发票序号": invoice["发票序号"],
                    "文件名": invoice["文件名"],
                    "问题原因": "打车发票缺少匹配的行程单文件"
                })

    # -------------------------- 检查8：未匹配任何分类 --------------------------
    # 这类发票归入 output/5_未匹配/，通常源于项目提取失败、非辰景的住宿发票，
    # 或所有单价都无法解析。问题原因不含「支付说明」+「支付记录」组合，
    # 以免被支付材料生成脚本误认为需要生成支付说明与支付记录的条目。
    for invoice in sorted_invoices:
        if is_unclassified(invoice):
            errors["未匹配分类"].append({
                "发票序号": invoice["发票序号"],
                "文件名": invoice["文件名"],
                "问题原因": "无法匹配任何发票类别，需人工确认该发票的报销通道"
            })

    # -------------------------- 输出错误汇总 --------------------------
    print("\n" + "="*80)
    print("                      发票错误检查结果汇总")
    print("="*80)
    total_errors = 0
    for error_type, error_list in errors.items():
        count = len(error_list)
        total_errors += count
        print(f"\n【{error_type}】（共 {count} 条）:")
        if count == 0:
            print("  - 无错误")
            continue
        # 打印每条错误详情
        for idx, err in enumerate(error_list, 1):
            if error_type == "连号发票":
                # 重复组的打印格式（适配新结构）
                print(f"  {idx}. {err['问题原因']}")
                # 拆分嵌套 f-string：先构造列表字符串，再在外层 f-string 中使用
                _inv_list_str = ", ".join([f"序号{inv['发票序号']}（{inv['文件名']}）" for inv in err['所有重复发票']])
                print(f"     → 涉及发票：{_inv_list_str}")
            else:
                # 其他错误的打印格式（保持原有逻辑）
                print(f"  {idx}. 发票序号{err['发票序号']}（文件：{err['文件名']}）：{err['问题原因']}")
                if "重复发票序号" in err:
                    print(f"     → 重复发票：序号{err['重复发票序号']}（文件：{err['重复发票文件名']}）")
                if "项目名称" in err:
                    print(f"     → 涉及项目：{err['项目名称']}（单价：{err.get('单价', '无')}）")
    
    print(f"\n" + "="*80)
    print(f"总错误数：{total_errors} 条")
    print("="*80)

    return errors

def analyze_invoice_json(invoices: List[Dict], root: Path) -> None:
    """核心分析函数：执行排序 + 错误检查"""
    # 1. 提取原始发票列表
    original_invoices = invoices
    if not original_invoices:
        print(f"{Colors.RED}[ERROR]{Colors.RESET} JSON中无发票数据")
        return

    # 2. 按需求排序发票
    sorted_invoices = sort_invoices(original_invoices, root=root)

    # 3. 定义允许的购买方列表（需根据实际需求修改！）
    allowed_buyers = [
        {"名称": "浙江大学", "税号": "12100000470095016Q"},
        {"名称": "杭州辰景信息咨询有限公司", "税号": "91330108MAC1F5Q350"},
        # 此处添加更多允许的购买方...
    ]
    print(f"{Colors.GREEN}[INFO]{Colors.RESET} 允许的购买方列表已加载（共 {len(allowed_buyers)} 个）")

    # 4. 执行所有错误检查
    errors = check_invoice_errors(sorted_invoices, allowed_buyers, root)

    # 5. 保存排序后的结果（更新JSON）
    save_to_json(sorted_invoices, str(root / "invoice_results_sorted.json"))

    # 6. （可选）保存错误汇总到文件
    with open(root / "invoice_errors.json", "w", encoding="utf-8") as f:
        json.dump(errors, f, ensure_ascii=False, indent=2)
    print(f"\n{Colors.GREEN}[INFO]{Colors.RESET} 错误汇总已保存至：{(root / 'invoice_errors.json').resolve()}")

def check_json_for_errors(json_data: Optional[Dict[str, Any]]) -> bool:
    """
    检查JSON数据中是否存在任何值为"ERROR"的字段（使用已加载的JSON数据）
    :param json_data: 已加载的JSON数据
    :return: 是否存在错误（True表示有错误，False表示无错误）
    """
    if json_data is None:
        print(f"{Colors.RED}[ERROR]{Colors.RESET} 无法检查错误：JSON数据无效")
        return True

    has_error = False

    # 递归检查函数
    def recursive_check(element: Any, parent_path: str = ""):
        nonlocal has_error
        # 处理字典类型
        if isinstance(element, dict):
            for key, value in element.items():
                current_path = f"{parent_path}.{key}" if parent_path else key
                if key == "开票时间" and value in ([], "", None):
                    has_error = True
                # 检查当前值是否为ERROR
                elif value == "ERROR":
                    has_error = True
                # 递归检查子元素
                elif value == "需人工校验":
                    has_error = True
                else:
                    recursive_check(value, current_path)
        # 处理列表类型
        elif isinstance(element, list):
            for index, item in enumerate(element):
                current_path = f"{parent_path}[{index}]" if parent_path else f"[{index}]"
                recursive_check(item, current_path)

    # 开始检查整个JSON
    recursive_check(json_data)

    # 输出总结信息
    if not has_error:
        print(f"{Colors.GREEN}[INFO]{Colors.RESET} 未发现任何异常")
    else:
        print(f"{Colors.RED}[ERROR]{Colors.RESET} JSON文件中存在识别失败项目(用ERROR标注)、缺失开票时间，或存在仍需人工校验发票号码，请先自行审查修正")

    return has_error

def check_missing_files(folder_path: str, json_invoices: List[Dict]) -> List[Dict]:
    """
    检查JSON中存在但发票文件夹中没有的发票（无效发票）
    :param folder_path: 发票文件夹路径
    :param json_invoices: JSON中的发票列表
    :return: 无效发票列表（JSON中有但文件夹中没有的发票）
    """
    if not os.path.exists(folder_path):
        print(f"{Colors.RED}[ERROR]{Colors.RESET} 发票文件夹 {folder_path} 不存在，无法检查文件存在性")
        return []
    
    # 获取文件夹中所有PDF文件名（小写统一匹配）
    folder_pdfs = {f.lower() for f in os.listdir(folder_path) if f.lower().endswith(".pdf")}
    missing_invoices = []
    
    for invoice in json_invoices:
        inv_filename = invoice.get("文件名", "").lower()
        # 跳过无文件名或非PDF的记录
        if not inv_filename or not inv_filename.endswith(".pdf"):
            continue
        # 检查文件是否存在于文件夹中
        if inv_filename not in folder_pdfs:
            missing_invoices.append(invoice)
            print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} 无效发票：JSON中存在（序号{invoice['发票序号']}，文件{invoice['文件名']}），但文件夹中无此文件")
    
    return missing_invoices

def clean_invalid_invoices(json_data: Dict[str, Any], missing_invoices: List[Dict]) -> Dict[str, Any]:
    """
    从JSON数据中删除无效发票（文件夹中不存在的发票），并重新编号
    :param json_data: 原始JSON数据
    :param missing_invoices: 无效发票列表
    :return: 清理后的JSON数据
    """
    if not missing_invoices:
        return json_data  # 无无效发票，直接返回原数据
    
    # 提取无效发票的文件名（用于过滤）
    missing_filenames = {inv.get("文件名", "").lower() for inv in missing_invoices}
    # 过滤有效发票（排除无效发票）
    valid_invoices = [
        inv for inv in json_data.get("发票信息", [])
        if inv.get("文件名", "").lower() not in missing_filenames
    ]
    
    # 重新编号，确保发票序号连续（从0开始）
    for new_idx, invoice in enumerate(valid_invoices):
        invoice["发票序号"] = new_idx
    
    # 更新JSON数据（总数和发票列表）
    cleaned_json = {
        "发票总数": len(valid_invoices),
        "发票信息": valid_invoices
    }

    print(f"{Colors.GREEN}[INFO]{Colors.RESET} 已从JSON中删除 {len(missing_invoices)} 张无效发票，剩余 {len(valid_invoices)} 张有效发票")
    return cleaned_json

def clean_duplicate_invoices(invoices: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    清理重复发票（按发票号码），返回清理后的列表和被删除的重复项
    :param invoices: 待清理的发票列表
    :return: (清理后无重复的发票列表, 被删除的重复发票列表)
    """
    # 1. 分离有效发票（发票号码≠ERROR）和无效发票
    valid_invs = [inv for inv in invoices if inv.get("发票号码") != "ERROR"]
    invalid_invs = [inv for inv in invoices if inv.get("发票号码") == "ERROR"]
    
    # 2. 记录首次出现的发票，过滤重复
    unique_num_map = {}  # key: 发票号码, value: 首次出现的发票
    deleted_dups = []    # 被删除的重复项
    
    for inv in valid_invs:
        inv_num = inv["发票号码"]
        if inv_num not in unique_num_map:
            unique_num_map[inv_num] = inv  # 首次出现，保留
        else:
            deleted_dups.append(inv)       # 重复，加入删除列表
            print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} 重复发票：号码{inv_num}（序号{inv['发票序号']}，文件{inv['文件名']}）将删除")
    
    # 3. 合并无重复有效发票和无效发票，按原序号排序
    clean_invs = list(unique_num_map.values()) + invalid_invs
    clean_invs.sort(key=lambda x: x["发票序号"])  # 保持原顺序
    
    # 4. 重新编号（确保序号连续）
    for new_idx, inv in enumerate(clean_invs):
        inv["发票序号"] = new_idx
    
    # 5. 输出清理结果
    if deleted_dups:
        print(f"被删除重复发票详情：")
        for idx, inv in enumerate(deleted_dups, 1):
            print(f"  {idx}. 序号{inv['发票序号']} | 号码{inv['发票号码']} | 文件{inv['文件名']}")
    print("="*80 + "\n")
    
    return clean_invs, deleted_dups

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Super Invoice: extract, sort, and classify invoices.")
    add_root_arg(parser)
    args = parser.parse_args()
    r = args.root.resolve()

    INVOICE_FOLDER = str(r / "invoices")
    JSON_FILE_PATH = str(r / "invoice_results.json")

    print(f"{Colors.BLUE}={Colors.BLUE}"*80 + "\n")
    print(f"{Colors.BLUE}Super Invoice{Colors.BLUE}\n")
    print(f"{Colors.BLUE}你的 Hello World 超级发票管家启动{Colors.BLUE}\n")
    print(f"{Colors.BLUE}版本：v1.3.0  日期：2025-10-21 作者：C88{Colors.BLUE}\n")
    print(f"{Colors.BLUE}={Colors.BLUE}"*80 + "\n")

    if not os.path.exists(INVOICE_FOLDER):
        os.makedirs(INVOICE_FOLDER)
        print(f"{Colors.YELLOW}[WARNING]{Colors.RESET}  已创建发票文件夹: {INVOICE_FOLDER}，请将PDF发票放入该文件夹后重新运行")
    else:
        # 步骤1：加载JSON数据（仅读取一次）
        json_data = load_json_data(JSON_FILE_PATH) if os.path.exists(JSON_FILE_PATH) else None

        # 步骤2：新增 - 检查并清理JSON中的无效发票（文件夹中不存在的发票）
        if json_data and "发票信息" in json_data and json_data["发票信息"]:
            # 检查无效发票
            missing_invoices = check_missing_files(INVOICE_FOLDER, json_data["发票信息"])
            if missing_invoices:
                # 清理无效发票并更新JSON文件
                cleaned_json = clean_invalid_invoices(json_data, missing_invoices)
                save_to_json(cleaned_json["发票信息"], JSON_FILE_PATH)
                # 重新加载清理后的JSON数据
                json_data = load_json_data(JSON_FILE_PATH)
            else:
                print(f"{Colors.GREEN}[INFO]{Colors.RESET} 所有JSON中的发票在文件夹中均存在，无需清理")
        elif json_data:
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} JSON中无发票数据，无需清理")

        # 步骤2：【核心修改】按文件名判断是否有新文件需处理
        need_process_new, existing_filenames, existing_invoices = check_json_need_regenerate(INVOICE_FOLDER, json_data)

        # 步骤3：处理新文件（如有）并更新JSON
        if need_process_new:
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} 开始处理新发票文件...")
            # 批量处理新文件（仅处理不在existing_filenames中的文件）
            new_invoices = batch_process_invoices(INVOICE_FOLDER, existing_filenames)
            
            if new_invoices:
                # 合并已有发票和新发票
                all_invoices = existing_invoices + new_invoices
                # 保存更新后的所有发票（自动更新总数）
                save_to_json(all_invoices, JSON_FILE_PATH)
                print(f"{Colors.GREEN}[INFO]{Colors.RESET} 新发票处理完成，共新增 {len(new_invoices)} 张发票，当前总发票数：{len(all_invoices)}")
                
                # 重新加载更新后的JSON数据用于后续检查
                json_data = load_json_data(JSON_FILE_PATH)
            else:
                print(f"{Colors.GREEN}[INFO]{Colors.RESET} 未检测到可正常处理的新发票文件（可能均为处理错误）")
                all_invoices = existing_invoices
        else:
            # 无新文件，直接使用已有数据
            all_invoices = existing_invoices
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} 无新发票文件需处理，使用已有 {len(all_invoices)} 张发票数据进行分析")

        # 清理重复发票并更新JSON
        if all_invoices:
            all_invoices, deleted_dups = clean_duplicate_invoices(all_invoices)
            # 立即保存清理重复后的结果到JSON，防止重复项残留
            save_to_json(all_invoices, JSON_FILE_PATH)

            # 将已移除的重复PDF重命名为.backup，避免后续运行再次提取
            for dup in deleted_dups:
                pdf_path = os.path.join(INVOICE_FOLDER, dup["文件名"])
                if os.path.exists(pdf_path):
                    backup_path = pdf_path + ".backup"
                    os.rename(pdf_path, backup_path)
                    print(f"{Colors.YELLOW}[INFO]{Colors.RESET} 重复发票PDF已备份: {dup['文件名']} → {os.path.basename(backup_path)}")
        else:
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} 无发票数据，无需清理重复")
            exit()

        # 步骤4：预览发票信息（如有）
        if all_invoices:
            first_invoice = all_invoices[0]
            print(f"\n{Colors.GREEN}[INFO]{Colors.RESET} 发票信息预览:")
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} 文件名: {first_invoice['文件名']}")
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} 发票号码: {first_invoice['发票号码']}")
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} 价税合计: {first_invoice['价税合计金额']}")
            print(f"{Colors.GREEN}[INFO]{Colors.RESET} 项目数量: {len(first_invoice['项目列表'])}\n")
        else:
            print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} 无可用发票数据，无法预览")

        # 步骤5：检查JSON中的错误（如ERROR字段）
        has_errors = check_json_for_errors(json_data)

        # 步骤6：无错误则执行分析（排序+错误检查）
        if not has_errors and all_invoices:
            analyze_invoice_json(all_invoices, r)
        elif has_errors:
            print(f"{Colors.RED}[ERROR]{Colors.RESET} 因JSON中存在错误，暂不执行后续分析，请先修正错误")
        else:
            print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} 无发票数据，无法执行后续分析")

        print("程序已运行完成")
