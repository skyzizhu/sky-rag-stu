"""节点 1：文档解析（Parser）。

产品视角：这一步相当于「收发室」——
不管你递进来的是 Word、PDF、网页剪藏还是纯文本，
它都负责把内容完整读出来，并登记一张「档案卡」（元数据 Metadata），
后续所有节点都只认这种统一的格式。

统一输出结构：
    {
        "text": "文档正文",
        "metadata": {
            "source":      文件名（来源）
            "file_type":   文件类型
            "title":       标题
            "section":     章节（解析阶段为空，切片时才知道）
            "page":        页码（解析阶段为空，切片时才知道）
            "created_at":  文件创建日期
            "updated_at":  文件修改日期
            "version":     版本（V1 固定 1.0）
            "status":      状态（V1 固定 active）
        }
    }
"""

from __future__ import annotations

import contextlib
import csv
import io
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic

from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from docx.oxml.ns import qn
from pypdf import PdfReader
from striprtf.striprtf import rtf_to_text

from src.console import console_print
from src.config import get_config
from src.metadata import parse_front_matter

# 支持的文件格式（图片格式交给 OCR 识别；xlsx/pptx/csv/tsv 由表格解析器读取；
# doc/xls/ppt/wps/et/dps 由 LibreOffice 转换为 docx/xlsx/pptx 后走既有解析器，见 _convert_via_soffice；
# docm/pptm 与 docx/pptx 同为 OOXML 容器，直接复用既有解析器）
SUPPORTED_EXTENSIONS = {".txt", ".md", ".rtf", ".html", ".htm", ".docx", ".docm", ".pdf",
                        ".xlsx", ".pptx", ".pptm", ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".bmp",
                        ".tif", ".tiff", ".gif", ".webp",
                        ".doc", ".xls", ".ppt", ".wps", ".et", ".dps"}

# 文本类扩展名：这类文件按文本读，后缀与内容不符时（如 PDF 改名 .txt）要明确拒绝
_TEXT_EXTENSIONS = {".txt", ".md", ".rtf", ".html", ".htm"}

# 老格式 / WPS 变体 → (LibreOffice 转换目标格式, 该格式的文档大类)
_SOFFICE_CONVERT = {
    ".doc": ("docx", "writer"), ".wps": ("docx", "writer"),
    ".xls": ("xlsx", "calc"), ".et": ("xlsx", "calc"),
    ".ppt": ("pptx", "impress"), ".dps": ("pptx", "impress"),
}

# PDF 分页标记：解析 PDF 时插入正文，用来记住「这段话原来在第几页」。
# 切片节点会读取这个标记，把页码写进元数据。
PAGE_MARKER_TEMPLATE = "<<<__RAG_PAGE__={page}>>>"
PAGE_MARKER_RE = re.compile(r"^<<<__RAG_PAGE__=(\d+)>>>\s*$", re.MULTILINE)

# 图片 OCR 的尺寸门槛（宽或高小于该值的图片直接跳过）：logo / 装饰小图上没有
# 可读内容，OCR 既慢又只会产出噪声
OCR_MIN_SIDE = 200


class ParseError(Exception):
    """某个文件解析失败时抛出，信息里带文件名。"""


@dataclass
class ParsedDocument:
    """内部统一的文档结构：正文 + 元数据 + Front Matter。"""

    text: str
    metadata: dict = field(default_factory=dict)
    front_matter: dict = field(default_factory=dict)  # Markdown 头部声明（非 md 文件为空）
    version_regions: list[dict] = field(default_factory=list)


def _version_region(text: str, location: str, source: str = "content", *,
                    include_empty: bool = False) -> list[dict]:
    """登记版本声明的候选区域，不推断版本；OCR 不作为权威声明。"""
    text = text[:4000]
    if not include_empty and not text.strip():
        return []
    return [{"source": source, "location": location, "text": text,
             "authoritative": source == "content"}]


def _file_dates(path: Path) -> tuple[str, str]:
    """读取文件的创建日期和修改日期（只保留到「日」）。"""
    stat = path.stat()
    created = getattr(stat, "st_birthtime", None) or stat.st_ctime
    updated = stat.st_mtime
    created_at = datetime.fromtimestamp(created).strftime("%Y-%m-%d")
    updated_at = datetime.fromtimestamp(updated).strftime("%Y-%m-%d")
    return created_at, updated_at


def _base_metadata(path: Path, title: str, file_type: str) -> dict:
    created_at, updated_at = _file_dates(path)
    return {
        "source": path.name,
        "file_type": file_type,
        "title": title,
        "section": None,
        "page": None,
        "created_at": created_at,
        "updated_at": updated_at,
        "version": "1.0",
        "status": "active",
    }


def _read_text_smart(path: Path) -> str:
    """读文本：编码嗅探（报告 M1/M2）。

    顺序：BOM（UTF-16 家族 / UTF-8）→ 二进制嗅探（后缀改名/损坏文件明确拒绝）
    → UTF-8 严格解码 → GB18030 严格解码 → UTF-8 宽松解码（残余乱码交给入库体检 G1 把关）。
    - BOM 必须剥掉：md 的 Front Matter 与首行标题都容不得开头挂一个 \\ufeff；
    - UTF-16 无 BOM 时按「偶/奇数位 NUL 字节」启发式识别（记事本"Unicode"、Excel
      "Unicode 文本"导出多为半 ASCII 内容，此法可覆盖；纯中文无 BOM UTF-16 识别不了，
      解出来是似是而非的汉字，由体检的乱码统计兜底）；
    - NUL 字节密集 = 二进制文件（如 PDF 改名 .txt），继续按文本读只会入库乱码。
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")  # 按 BOM 自动选端序，且不保留 BOM 字符
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")

    ext = path.suffix.lower()
    if ext in _TEXT_EXTENSIONS:
        for magic, label in ((b"%PDF-", "PDF"), (_OOXML_MAGIC, "Office Open XML（docx/xlsx/pptx）"),
                             (_OLE_MAGIC, "老版 Office 文档（doc/xls/ppt）")):
            if raw.startswith(magic):
                raise ParseError(
                    f"文件内容是{label}，与后缀 {ext} 不符：{path.name}。请把后缀改回真实格式后重新入库")

    # UTF-16 无 BOM 启发式：一半位置是 NUL 字节 → 多字节内容是 ASCII/半角，可按端序还原
    if len(raw) >= 8 and not raw.startswith((b"%PDF", b"PK\x03\x04", _OLE_MAGIC)):
        zeros = raw.count(b"\x00")
        if zeros and zeros >= len(raw) // 20:
            even = sum(1 for i in range(0, len(raw), 2) if raw[i] == 0)
            odd = zeros - even
            if max(even, odd) >= zeros * 0.8:
                return raw.decode("utf-16-le" if odd >= even else "utf-16-be")
    if zeros := raw.count(b"\x00"):
        if zeros > max(32, len(raw) // 50):
            raise ParseError(
                f"文件内容疑似二进制数据，无法按文本读取：{path.name}。请检查后缀与实际格式是否一致")

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _read_txt(path: Path) -> tuple[str, str, dict]:
    """纯文本：直接读，标题用文件名。"""
    text = _read_text_smart(path)
    return text, "", {"_version_regions": _version_region(text, "开头文本", include_empty=True)}


def markdown_lines(text: str):
    """Yield source lines and their headings, excluding fenced code from heading detection."""
    lines = text.split("\n")
    fence = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        marker = re.match(r"^ {0,3}(\x60{3,}|~{3,})(.*)$", line)
        if marker:
            token, suffix = marker.groups()
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip():
                fence = ""
            yield line, 0, ""
            continue
        if not fence:
            heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(\S.*?)\s*$", line)
            if heading:
                yield line, len(heading[1]), re.sub(r"\s+#+$", "", heading[2])
                continue
            underline = re.fullmatch(r" {0,3}(=+|-+)\s*", lines[index]) if index < len(lines) else None
            if (underline and line.strip() and not line.startswith(("    ", "\t"))
                    and not re.match(r"\s*(?:[-+*>]|\d+[.)])\s", line)):
                index += 1
                level = 1 if underline[1][0] == "=" else 2
                yield f"{'#' * level} {line.strip()}", level, line.strip()
                continue
        yield line, 0, ""


def _read_markdown(path: Path) -> tuple[str, str, dict]:
    """Markdown：先拆 Front Matter（人工声明的分类信息），标题取正文的第一个一级标题。"""
    raw = _read_text_smart(path)
    front_matter, text = parse_front_matter(raw)
    title = next((label for _, level, label in markdown_lines(text) if level == 1), "")
    front_matter["_version_regions"] = _version_region(text, "开头文本", include_empty=True)
    return text, title, front_matter


def _read_rtf(path: Path) -> tuple[str, str, dict]:
    """RTF：用专门的解析库读出纯文本。"""
    raw = _read_text_smart(path)
    text = rtf_to_text(raw)
    return text, "", {"_version_regions": _version_region(text, "开头文本", include_empty=True)}


def html_to_text(source: bytes | str) -> tuple[str, str]:
    """HTML → (正文 Markdown, 页面标题)。

    网页文件上传与 URL 抓取（web_fetcher）共用这一份解析，保证两种入口
    出来的内容结构一致（报告 U5）。提取正文区域，并尽量保留标题、列表、
    表格、引用与代码结构。
    """
    # 直接交给 BeautifulSoup 读取字节，可依据 BOM / meta charset 识别旧中文网页编码。
    soup = BeautifulSoup(source, "html.parser")
    for tag in soup([
        "script", "style", "noscript", "template", "svg", "canvas", "iframe",
        "button", "input", "select", "textarea",
    ]):
        tag.decompose()

    h1 = soup.find("h1")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = h1.get_text(" ", strip=True) if h1 else page_title

    body = soup.body or soup
    candidates = body.select("article, main, [role='main']")
    root = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=body)
    body_length = len(body.get_text(" ", strip=True))
    root_length = len(root.get_text(" ", strip=True))
    # 很短的 main/article 可能只是推荐卡片，不应覆盖真正的 body 正文。
    if root is not body and root_length < 120 and root_length < body_length * 0.35:
        root = body

    noise_tags = ["nav", "footer", "aside", "form", "dialog"]
    if root is body:
        noise_tags.append("header")
    for tag in root.find_all(noise_tags):
        tag.decompose()
    for tag in root.select("[hidden], [aria-hidden='true']"):
        tag.decompose()

    # 先转换会包含其他标签的结构，再处理普通块级元素。
    # 表格从最内层开始处理（reversed 文档序）：嵌套表先转成文本，外层再读行，
    # 否则外层 find_all("tr") 会把内层行也抓进来——单元格泄漏成列 + 内容重复
    for table in reversed(list(root.find_all("table"))):
        rows = []
        for row in table.find_all("tr"):
            cells = [
                _squeeze_cjk_spaces(
                    re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
                ).replace("|", "\\|")
                for cell in row.find_all(["th", "td"])
            ]
            if cells:
                rows.append(cells)
        if rows:
            width = max(len(row) for row in rows)
            rows = [row + [""] * (width - len(row)) for row in rows]
            table_lines = ["| " + " | ".join(row) + " |" for row in rows]
            table_lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
            table.replace_with("\n\n" + "\n".join(table_lines) + "\n\n")
        else:
            table.decompose()

    for pre in list(root.find_all("pre")):
        code = pre.get_text("\n", strip=False).strip("\n")
        pre.replace_with(f"\n\n```\n{code}\n```\n\n")
    for code in list(root.find_all("code")):
        code.replace_with(f"`{code.get_text('', strip=True)}`")

    for image in list(root.find_all("img")):
        alt = re.sub(r"\s+", " ", image.get("alt", "")).strip()
        image.replace_with(f"[图片：{alt}]" if alt else "")
    for anchor in list(root.find_all("a")):
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        href = (anchor.get("href") or "").strip()
        if label and href.startswith(("http://", "https://")):
            anchor.replace_with(f"[{label}]({href})")
        else:
            anchor.replace_with(label)

    for list_tag in reversed(root.find_all(["ol", "ul"])):
        lines = []
        for index, item in enumerate(list_tag.find_all("li", recursive=False), start=1):
            item_text = re.sub(r"\s+", " ", item.get_text(" ", strip=True))
            if item_text:
                marker = f"{index}." if list_tag.name == "ol" else "-"
                lines.append(f"{marker} {item_text}")
        list_tag.replace_with("\n\n" + "\n".join(lines) + "\n\n")

    for quote in list(root.find_all("blockquote")):
        lines = [line.strip() for line in quote.get_text("\n").splitlines() if line.strip()]
        quote.replace_with("\n\n" + "\n".join(f"> {line}" for line in lines) + "\n\n")
    for heading in list(root.find_all(re.compile(r"^h[1-6]$"))):
        level = int(heading.name[1])
        heading.replace_with(f"\n\n{'#' * level} {heading.get_text(' ', strip=True)}\n\n")
    for br in list(root.find_all("br")):
        br.replace_with("\n")
    for block in root.find_all(["p", "div", "section", "article", "main", "header", "address", "figure", "figcaption", "dl", "dt", "dd"]):
        block.insert_before("\n\n")
        block.insert_after("\n\n")

    text = root.get_text("", strip=False)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, title


def _read_html(path: Path) -> tuple[str, str, dict]:
    text, title = html_to_text(path.read_bytes())
    return text, title, {"_version_regions": _version_region(text, "开头文本", include_empty=True)}


def _squeeze_cjk_spaces(text: str) -> str:
    """压缩汉字之间的空白：Word/网页表格为排版常写「工 作 经 历」，
    不压缩的话 jieba 分词与向量匹配都会失效，检索永远命中不了。"""
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)


# ---------------------------------------------------------------- OCR（RapidOCR）
# 中英文识别走 PaddleOCR 的 ONNX 模型（rapidocr_onnxruntime），纯 CPU 离线运行，
# 模型随包内置；引擎进程内单例懒加载——不用 OCR 的入库完全不加载模型。

_OCR_ENGINE = None  # RapidOCR 实例；False = 初始化失败（不再重试）
_OCR_CACHE: dict[str, str] = {}  # 报告 G2：同一张图（按内容哈希）只识别一次，PPT 里重复的 logo/页眉图不再重复识别
# 报告 G2 / 决策3：单文件 OCR 时间预算。parse_file 开始时设定截止时间，_ocr_image
# 每次识别前检查——超时后跳过剩余图片（不识别），跳过数量由 parse_file 写进提示。
# 入库流水线全程持锁串行，模块级状态即每文件一份。
_OCR_STATE = {"deadline": None, "skipped": 0}


def _ocr_budget_begin() -> None:
    """开始一个文件的解析：按配置重置 OCR 时间预算。"""
    budget = get_config().ocr_time_budget
    _OCR_STATE["deadline"] = (monotonic() + budget) if budget > 0 else None
    _OCR_STATE["skipped"] = 0


def _get_ocr_engine():
    """懒加载 OCR 引擎单例。OCR_ENABLED=false 或库不可用时返回 None，调用方跳过 OCR。"""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR

            _OCR_ENGINE = RapidOCR()
        except Exception as exc:
            console_print(f"⚠️ OCR 引擎不可用，图片/扫描件内容将不被识别（{type(exc).__name__}: {exc}）")
            _OCR_ENGINE = False
    return _OCR_ENGINE or None


def _flatten_to_rgb(image):
    """铺白底转 RGB：透明底图直接 convert('RGB') 会让黑字落在黑底上，整图识别为空（报告 I1）。"""
    from PIL import Image

    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        flattened = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        flattened.paste(rgba, mask=rgba.split()[-1])
        return flattened
    return image.convert("RGB")


def _ocr_image(data: bytes, hint: str = "") -> str:
    """对一段图片二进制做 OCR，返回按行拼接的识别文本；无文字 / 尺寸过小 / 失败返回空串。

    调用方自行决定是否 OCR（受 OCR_ENABLED 控制），本函数只负责"安全地识别一次"。
    报告 G2：识别行按置信度过滤（OCR_MIN_CONFIDENCE，默认 0.6——实测一份真实 PPT
    25% 的行低于 0.8，其中低分行的内容基本是识别噪声）；相同内容只识别一次。
    """
    if not get_config().ocr_enabled:
        return ""
    import hashlib

    digest = hashlib.md5(data).hexdigest()
    if digest in _OCR_CACHE:
        return _OCR_CACHE[digest]
    deadline = _OCR_STATE["deadline"]
    if deadline is not None and monotonic() > deadline:
        _OCR_STATE["skipped"] += 1
        return ""  # 报告 G2/决策3：单文件 OCR 时间预算用尽，跳过剩余图片
    text = _ocr_image_uncached(data, hint)
    if len(_OCR_CACHE) > 512:
        _OCR_CACHE.clear()  # 防止整库批量入库时缓存无限增长
    _OCR_CACHE[digest] = text
    return text


def _ocr_image_uncached(data: bytes, hint: str = "") -> str:
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        if min(image.size) < OCR_MIN_SIDE:
            return ""
        frame_count = getattr(image, "n_frames", 1)  # I2：多页 TIFF / GIF / 动图逐帧识别
    except Exception:
        return ""  # 解不开的图（损坏/编码不支持）按"没有文字"处理
    engine = _get_ocr_engine()
    if engine is None:
        return ""
    min_confidence = max(0.0, min(get_config().ocr_min_confidence, 0.99))
    try:
        import numpy as np

        lines: list[str] = []
        for frame_index in range(frame_count):
            image.seek(frame_index)
            pixels = np.array(_flatten_to_rgb(image))[:, :, ::-1]  # RGB → BGR（RapidOCR 按 OpenCV 约定读数组）
            result, _ = engine(pixels)
            for item in result or ():
                text = str(item[1]).strip() if len(item) >= 2 else ""
                if not text:
                    continue
                score = item[2] if len(item) >= 3 else 1.0
                if float(score) < min_confidence:
                    continue
                lines.append(text)
        if frame_count > 1:
            return "\n".join(lines)  # 多帧之间不再按行合并，保留帧分隔
    except Exception as exc:
        console_print(f"⚠️ 图片 OCR 失败{('（' + hint + '）') if hint else ''}：{exc}")
        return ""
    return "\n".join(lines)


# ---------------------------------------------------------------- 格式嗅探
# 公司文档里「后缀被人为改错」的文件很常见（docx 改名成 doc、反之亦然）。
# 按扩展名路由会把真 docx 送去 LibreOffice 绕远路、把真 doc 交给 python-docx 直接报错。
# 文件头（magic bytes）不会说谎：PK 头 = OOXML 家族（docx/xlsx/pptx），
# OLE2 头 = 老家族（doc/xls/ppt）。

_OOXML_MAGIC = b"PK\x03\x04"                      # ZIP 头：docx/xlsx/pptx 家族
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE2 复合文档头：doc/xls/ppt 老家族


def _sniff_binary_kind(path: Path) -> str:
    """按文件头嗅探真实容器格式，不信扩展名。

    返回 'ooxml'（PK 头：docx/xlsx/pptx 家族）/ 'ole'（OLE2 复合文档：doc/xls/ppt
    老家族）/ ''（两者都不是：rtf、文本等——交给原扩展名路由处理）。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return ""
    if head.startswith(_OOXML_MAGIC):
        return "ooxml"
    if head.startswith(_OLE_MAGIC):
        return "ole"
    return ""


@contextlib.contextmanager
def _with_fixed_ext(path: Path, target_ext: str):
    """内容与扩展名不符时的解析辅助：复制一份扩展名正确的临时副本再解析。

    只有 openpyxl 按扩展名校验格式（.xls 后缀装着 xlsx 内容会被直接拒绝），
    python-docx / python-pptx 都按文件内容打开、无此检查，无需此辅助。
    """
    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="rag_sniff_"))
    tmp = tmp_dir / f"fixed{target_ext}"
    try:
        shutil.copyfile(path, tmp)
        yield tmp
    finally:
        try:
            tmp.unlink()
            tmp_dir.rmdir()
        except OSError:
            pass


# 老格式转换的进程锁：soffice 并发实例之间会互相争抢用户配置目录，必须串行
_soffice_lock = __import__("threading").Lock()


def _convert_via_soffice(path: Path, target_ext: str, family: str) -> Path:
    """用 LibreOffice（soffice）把老格式 / WPS 变体转换为既有格式，返回转换产物路径。

    - 产物写到原目录下的 .rag_converted/ 缓存（内容指纹命名）：同一文件重复入库
      不会重复转换（增量判断命中时甚至不会走到这里）；
    - 超时 120 秒；soffice 不存在 / 转换失败 → ParseError 带安装提示。
    """
    import hashlib
    import shutil
    import subprocess

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise ParseError(
            f"解析 {path.suffix} 需要 LibreOffice（转换 {path.suffix} → {target_ext}），"
            "但系统里没有找到 soffice。请安装 LibreOffice 后重试"
            "（macOS: brew install --cask libreoffice；Linux: apt install libreoffice；"
            "Windows: 官网安装后把 soffice.exe 所在目录加入 PATH）。"
        )
    out_dir = path.parent / ".rag_converted"
    out_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha1(f"{path.name}|{path.stat().st_mtime_ns}".encode()).hexdigest()[:12]
    # 指纹作为文件名一部分（真实扩展名必须留在末尾——openpyxl/pptx 按扩展名校验格式）
    converted = out_dir / f"{path.stem}__{fingerprint}.{target_ext}"
    if converted.exists() and converted.stat().st_size > 0:
        return converted  # 缓存命中：内容没变不重转

    fmt_flag = {"docx": "docx", "xlsx": "xlsx", "pptx": "pptx"}[target_ext]
    with _soffice_lock:  # 转换与产物改名全程串行：并发 soffice 会争抢输出，rename 也会有竞态
        try:
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", fmt_flag,
                 "--outdir", str(out_dir), str(path)],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise ParseError(
                f"转换超时（120 秒）：{path.name} → {target_ext}。"
                "文件可能过大或已损坏；也可以先用 Office/WPS 打开另存为新格式后重新入库。"
            ) from exc
        produced = out_dir / f"{path.stem}.{target_ext}"
        if proc.returncode != 0 or not produced.exists() or produced.stat().st_size == 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            hint = detail[-1][:120] if detail else "未知原因"
            raise ParseError(
                f"转换失败：{path.name} → {target_ext}（{hint}）。"
                "可以先用 Office/WPS 打开该文件另存为 docx/xlsx/pptx 后重新入库。"
            )
        # 统一重命名为带指纹的缓存名，避免同目录不同文件互相覆盖
        final = converted
        if produced != final:
            # 先清掉同 stem 的全部旧缓存条目（含旧命名与旧指纹），再把新产物 rename 到位
            for stale in out_dir.glob(f"{path.stem}*"):
                if stale != produced:
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            produced.replace(final)
        return final



def _read_legacy_office(path: Path, target_ext: str, family: str):
    """老格式 / WPS 变体：soffice 转换为既有格式后，委托对应的既有 reader。"""
    converted = _convert_via_soffice(path, target_ext, family)
    reader = _READERS.get(f".{target_ext}")  # _READERS 的键带前导点
    if reader is None:  # 防御：转换目标必须是既有格式（docx/xlsx/pptx）
        raise ParseError(f"内部错误：转换目标 {target_ext!r} 没有对应解析器")
    text, title, fm = reader(converted)
    fm.setdefault("source_note", f"由 {path.suffix.upper()} 经 LibreOffice 转换解析")
    return text, title, fm


def _read_doc(path: Path) -> tuple[str, str, dict]:
    if _sniff_binary_kind(path) == "ooxml":
        # 假 .doc：文件头是 PK（实为 docx）——直接按 docx 解析，不走 LibreOffice
        return _read_docx(path)
    return _read_legacy_office(path, "docx", "writer")


def _read_wps(path: Path) -> tuple[str, str, dict]:
    if _sniff_binary_kind(path) == "ooxml":
        return _read_docx(path)
    return _read_legacy_office(path, "docx", "writer")


def _read_xls(path: Path) -> tuple[str, str, dict]:
    if _sniff_binary_kind(path) == "ooxml":
        # 假 .xls：实为 xlsx——复制成 .xlsx 临时副本再解析（openpyxl 按扩展名校验格式）
        with _with_fixed_ext(path, ".xlsx") as fixed:
            return _read_xlsx(fixed)
    return _read_legacy_office(path, "xlsx", "calc")


def _read_et(path: Path) -> tuple[str, str, dict]:
    if _sniff_binary_kind(path) == "ooxml":
        with _with_fixed_ext(path, ".xlsx") as fixed:
            return _read_xlsx(fixed)
    return _read_legacy_office(path, "xlsx", "calc")


def _read_ppt(path: Path) -> tuple[str, str, dict]:
    if _sniff_binary_kind(path) == "ooxml":
        # 假 .ppt：实为 pptx——直接按 pptx 解析
        return _read_pptx(path)
    return _read_legacy_office(path, "pptx", "impress")


def _read_dps(path: Path) -> tuple[str, str, dict]:
    if _sniff_binary_kind(path) == "ooxml":
        return _read_pptx(path)
    return _read_legacy_office(path, "pptx", "impress")


def _docx_opening_text(para, text: str) -> tuple[str, bool]:
    """仅识别显式分页边界；文档开头不等同于 Word 渲染后的第一页。"""
    from docx.oxml.ns import qn

    if para.paragraph_format.page_break_before:
        return "", True
    if not para._p.xpath(".//w:br[@w:type='page']"):
        return text, False
    prefix: list[str] = []
    for node in para._p.xpath("./w:r/* | ./w:hyperlink/w:r/*"):
        if node.tag == qn("w:br") and node.get(qn("w:type")) == "page":
            break
        if node.tag == qn("w:t"):
            prefix.append(node.text or "")
        elif node.tag == qn("w:tab"):
            prefix.append("\t")
        elif node.tag in (qn("w:br"), qn("w:cr")):
            prefix.append("\n")
    return "".join(prefix).strip(), True


def _append_docx_opening(text: str, opening: dict, separator: str = "\n") -> bool:
    if opening["stopped"] or not text.strip():
        return False
    separator = separator if opening["parts"] else ""
    text = (separator + text)[:opening["remaining"]]
    opening["parts"].append(text)
    opening["remaining"] -= len(text)
    if len(opening["parts"]) >= 20 or opening["remaining"] <= 0:
        opening["stopped"] = True
    return True


def _unwrap_content_controls(doc) -> None:
    """把内容控件（w:sdt）的子元素提升到父级后删除外壳（报告 W2）。

    python-docx 的 iter_inner_content / paragraphs 都会跳过 sdt 包装——Word 内置
    封面、目录域、表单控件都用它，控件里的文字此前整体丢失。原地展开后按正常
    文档流读取；嵌套控件由 while 多轮展开。
    """
    body = doc.element.body
    while True:
        sdts = body.findall(".//" + qn("w:sdt"))
        if not sdts:
            return
        for sdt in sdts:
            parent = sdt.getparent()
            content = sdt.find(qn("w:sdtContent"))
            if parent is None:
                continue
            if content is None:
                parent.remove(sdt)
                continue
            index = parent.index(sdt)
            for child in reversed(list(content)):
                parent.insert(index, child)
            parent.remove(sdt)


def _docx_numbering_maps(doc) -> tuple[dict, dict]:
    """读取编号定义：numId→abstractNumId 与 (abstractId, 级别)→(编号格式, 文本模板)。

    文档没有编号部件（numbering.xml）时返回空表，调用方跳过编号还原。
    """
    try:
        element = doc.part.numbering_part.element
    except Exception:
        return {}, {}
    num_map: dict[int, str] = {}
    fmt_map: dict[tuple[str, int], tuple[str, str]] = {}
    for num in element.findall(qn("w:num")):
        abstract = num.find(qn("w:abstractNumId"))
        if abstract is not None:
            num_map[int(num.get(qn("w:numId")))] = abstract.get(qn("w:val"))
    for abstract in element.findall(qn("w:abstractNum")):
        abstract_id = abstract.get(qn("w:abstractNumId"))
        for level in abstract.findall(qn("w:lvl")):
            try:
                ilvl = int(level.get(qn("w:ilvl"), "0"))
            except ValueError:
                continue
            fmt = level.find(qn("w:numFmt"))
            text = level.find(qn("w:lvlText"))
            fmt_map[(abstract_id, ilvl)] = (
                fmt.get(qn("w:val")) if fmt is not None else "decimal",
                text.get(qn("w:val")) if text is not None else f"%{ilvl + 1}.",
            )
    return num_map, fmt_map


_NUMBERING_PREFIX_RE = re.compile(
    r"^\s*(?:\d{1,2}[.、)）]|[一二三四五六七八九十百]+[、.．]|[（(]\d{1,2}[)）]|[a-zA-Z][.、)]|[-•·–—][ \t])")


def _docx_list_marker(para, numbering: dict | None) -> str:
    """还原自动编号 / 项目符号为文本前缀（报告 W3）。近似规则：

    - 项目符号（bullet）统一还原为 "- "；
    - 十进制编号按 numId+级别计数，更深层级随层级回退重置（Word 行为）；
    - 字母 / 罗马 / 中文数字等格式用同一计数器近似为阿拉伯数字——编号数值
      远比格式重要，检索命中主要靠编号本身；
    - 段落文字已自带编号样式（如手打的 "1. "）时不重复加前缀。
    """
    if numbering is None:
        return ""
    numpr = None
    ppr = para._p.find(qn("w:pPr"))
    if ppr is not None:
        numpr = ppr.find(qn("w:numPr"))
    if numpr is None:  # 编号挂在样式上（"List Number" 这类）时从样式链取
        style = getattr(para, "style", None)
        style_element = getattr(style, "element", None)
        if style_element is not None:
            style_ppr = style_element.find(qn("w:pPr"))
            if style_ppr is not None:
                numpr = style_ppr.find(qn("w:numPr"))
    if numpr is None:
        return ""
    num_id_el = numpr.find(qn("w:numId"))
    if num_id_el is None:
        return ""
    try:
        num_id = int(num_id_el.get(qn("w:val")))
    except (TypeError, ValueError):
        return ""
    ilvl = 0
    ilvl_el = numpr.find(qn("w:ilvl"))
    if ilvl_el is not None:
        try:
            ilvl = int(ilvl_el.get(qn("w:val")))
        except (TypeError, ValueError):
            ilvl = 0
    abstract = numbering["num_map"].get(num_id)
    fmt, template = numbering["fmt_map"].get((abstract, ilvl), ("decimal", f"%{ilvl + 1}."))
    if fmt == "none":
        return ""
    if fmt == "bullet":
        return "- "
    counters = numbering["counters"]
    counters[(num_id, ilvl)] = counters.get((num_id, ilvl), 0) + 1
    for key in [key for key in counters if key[0] == num_id and key[1] > ilvl]:
        counters.pop(key)
    marker = template or f"%{ilvl + 1}."
    for level in range(ilvl + 1):
        marker = marker.replace(f"%{level + 1}", str(counters.get((num_id, level), 1)))
    marker = re.sub(r"%\d", "1", marker)  # 未定义的层级占位符按 1 兜底
    return marker + " "


def _docx_paragraph_images(para) -> list[tuple[str, str]]:
    """段落里的内嵌图 → [(关系ID, 替代文字)]，按文档流顺序（报告 W4）。"""
    images: list[tuple[str, str]] = []
    for drawing in para._p.findall(".//" + qn("w:drawing")):
        blip = drawing.find(".//" + qn("a:blip"))
        r_id = blip.get(qn("r:embed")) if blip is not None else None
        if not r_id:
            continue
        doc_pr = drawing.find(".//" + qn("wp:docPr"))
        alt = (doc_pr.get("descr") or doc_pr.get("name") or "").strip() if doc_pr is not None else ""
        images.append((r_id, alt))
    return images


def _docx_paragraph_line(para, title_state: dict, opening: dict | None = None,
                         numbering: dict | None = None) -> str:
    """段落 → 输出行：标题样式转 Markdown # 标记（V3.9 从 docx reader 拆出供复用）。"""
    line = para.text.strip()
    if para._p.xpath(".//w:ins//w:t"):
        # 报告 W1：开着修订的文档，新增文字包在 w:ins 里，para.text 读不到。
        # 按文档流取全部 w:t 等价于"接受全部修订"；被删除的文字是 w:delText，天然排除。
        line = "".join(node.text or "" for node in para._p.xpath(".//w:t")).strip()
    if opening is not None and not opening["stopped"]:
        native, stopped = _docx_opening_text(para, line)
        _append_docx_opening(native, opening)
        opening["stopped"] |= stopped
    if not line:
        return ""
    style = para.style.name if para.style is not None else ""
    if style == "Title" and not title_state.get("title"):
        title_state["title"] = line
        return line
    if style.startswith("Heading"):
        try:
            level = min(int(style.split()[-1]), 6)
        except ValueError:
            level = 2
        line = "#" * level + " " + line
        if not title_state.get("title"):
            title_state["title"] = line
        return line
    marker = _docx_list_marker(para, numbering)
    if marker and not _NUMBERING_PREFIX_RE.match(line):
        line = marker + line
    if not line.startswith("#"):  # 报告 W5：没套标题样式、只设了大纲级别的段落同样当标题
        ppr = para._p.find(qn("w:pPr"))
        outline = ppr.find(qn("w:outlineLvl")) if ppr is not None else None
        if outline is not None:
            try:
                level = min(int(outline.get(qn("w:val"))) + 1, 6)
                line = "#" * level + " " + line
                if not title_state.get("title"):
                    title_state["title"] = line
            except (TypeError, ValueError):
                pass
    return line


def _docx_page_segments(para, line: str) -> list[str]:
    """Split explicit Word page breaks without inferring pagination from layout."""
    from docx.oxml.ns import qn

    if not para._p.xpath(".//w:br[@w:type='page']"):
        return [line]
    parts = [""]
    for node in para._p.xpath(".//w:r/*"):
        if node.tag == qn("w:br") and node.get(qn("w:type")) == "page":
            parts.append("")
        elif node.tag == qn("w:t"):
            parts[-1] += node.text or ""
        elif node.tag == qn("w:tab"):
            parts[-1] += "\t"
        elif node.tag in (qn("w:br"), qn("w:cr")):
            parts[-1] += "\n"
    if (heading := re.match(r"^#{1,6} ", line)) and parts[0].strip():
        parts[0] = heading[0] + parts[0]
    return parts


def _docx_table_text(table, title_state: dict, opening: dict | None = None) -> str:
    """表格 → 管道分隔文本；嵌套表递归读出（V3.9：此前嵌套表内容静默丢失）。

    合并单元格在 row.cells 里会重复出现（横向合并同行重复、纵向合并跨行共享
    同一 tc），只输出第一次，其余留空（报告 W6），避免同一内容在一行里重复。
    """
    rows: list[str] = []
    # 合并单元格去重：lxml 元素代理对象会被回收、id() 可能被复用，必须同时保存
    # 强引用（见 value→tc），否则嵌套表等场景会误判"重复列"导致内容丢失（W6）。
    seen_tcs: dict[int, object] = {}
    for row in table.rows:
        cells: list[str] = []
        opening_row_started = False
        for cell in row.cells:
            tc = cell._tc
            if id(tc) in seen_tcs:
                cells.append("")
                continue
            seen_tcs[id(tc)] = tc
            cell_parts: list[str] = []
            native_parts: list[str] = []
            stopped = False
            for para in cell.paragraphs:
                text = para.text.strip()
                if text:
                    cell_parts.append(text)
                if opening is not None and not opening["stopped"] and not stopped:
                    native, stopped = _docx_opening_text(para, text)
                    if native:
                        native_parts.append(native)
            if opening is not None and not opening["stopped"]:
                # 单元格仍各计一个非空块，但同一行用管道保留「标签 | 值」关系。
                appended = _append_docx_opening(
                    " / ".join(native_parts), opening,
                    " | " if opening_row_started else "\n",
                )
                opening_row_started |= appended
                opening["stopped"] |= stopped
            for nested in getattr(cell, "tables", []):
                nested_text = _docx_table_text(nested, title_state, opening)
                if nested_text:
                    cell_parts.append(nested_text.replace("\n", " / "))
            cells.append(_squeeze_cjk_spaces(" / ".join(p for p in cell_parts if p)))
        rows.append(_squeeze_cjk_spaces(" | ".join(cells)))
    return "\n".join(rows)


def _read_docx(path: Path) -> tuple[str, str, dict]:
    """Word：按文档流顺序读（段落与表格穿插，V3.9 修复表格被挪到文档尾部）；
    标题样式转 Markdown # 标记；嵌套表递归；读取各节页眉/页脚文字。
    修订文档按"接受全部修订"读取（W1）；内容控件展开后按正常文档流读取（W2）；
    自动编号/项目符号还原为文本前缀（W3）；内嵌图片的 OCR 文字落在图片所在位置（W4）。
    （文本框 / SmartArt 文字暂不支持，读取范围记录在 realize.md A1。）
    后缀改错的文件（.docx 实为 doc 老格式）按文件头嗅探自动走转换链路。"""
    if _sniff_binary_kind(path) == "ole":
        # 文件头是 OLE2 复合文档 → 实为老格式，交给 LibreOffice 转换
        return _read_legacy_office(path, "docx", "writer")
    doc = DocxDocument(str(path))
    _unwrap_content_controls(doc)  # 报告 W2：封面/目录域/表单控件的文字不再丢失
    has_revisions = bool(doc.element.body.xpath(".//w:ins") or doc.element.body.xpath(".//w:del"))
    num_map, fmt_map = _docx_numbering_maps(doc)
    numbering_state = {"num_map": num_map, "fmt_map": fmt_map, "counters": {}} if num_map else None
    parts: list[str] = []
    title_state: dict = {"title": ""}
    opening: dict = {"parts": [], "remaining": 4000, "stopped": False}
    page_segment = 1
    image_counter = 0
    placed_partnames: set[str] = set()  # 报告 W4：正文里的图按文档流位置就地识别，避免重复
    # 阅读顺序：iter_inner_content 按文档流产出段落与表格（python-docx ≥1.1）
    for item in doc.iter_inner_content():
        if hasattr(item, "rows"):  # Table
            table_text = _docx_table_text(item, title_state, opening)
            if table_text.strip():
                parts.append(table_text)
            continue
        line = _docx_paragraph_line(item, title_state, opening, numbering_state)
        for index, text in enumerate(_docx_page_segments(item, line)):
            if index or item.paragraph_format.page_break_before:
                page_segment += 1
                parts.append(PAGE_MARKER_TEMPLATE.format(page=page_segment))
            parts.append(text)
        for r_id, alt in _docx_paragraph_images(item):
            try:
                part = doc.part.related_parts[r_id]
            except KeyError:
                continue
            partname = str(part.partname)
            if partname in placed_partnames:
                continue
            placed_partnames.add(partname)
            image_counter += 1
            ocr_text = _ocr_image(part.blob, hint=f"{path.name} 内嵌图{image_counter}")
            if ocr_text:
                parts.append(f"[文档内图片 {image_counter} · OCR 识别]\n{ocr_text}")
            elif alt:
                parts.append(f"[图片：{alt}]")  # OCR 无文字/未开启时，至少保留替代文字线索
    title = title_state.get("title", "")
    if not title:  # 报告 G3：正文没有标题样式时回落到文档属性标题
        core_title = (doc.core_properties.title or "").strip()
        if core_title and core_title != path.stem:
            title = core_title

    # 页眉 / 页脚：每节的页眉页脚文字（通常为文档名、页码说明等辅助信息）。
    # "链接到前一节"的页眉会在每个 section 重复，去重后再输出（报告 W7）。
    seen_header_lines: set[str] = set()
    for section in doc.sections:
        for container, label in ((section.header, "页眉"), (section.footer, "页脚")):
            if container is None:
                continue
            for para in container.paragraphs:
                line = para.text.strip()
                if line and line not in seen_header_lines:
                    seen_header_lines.add(line)
                    parts.append(f"[{label}] {line}")

    # 不在正文文档流里的图（页眉/页脚图、表格单元格里的图）：统一追加在文末识别。
    # docProps/thumbnail.jpeg 是文件属性缩略图，不是文档内容，排除。
    leftovers: list[bytes] = []
    for part in doc.part.package.iter_parts():
        if (part.content_type.startswith("image/") and not str(part.partname).startswith("/docProps/")
                and str(part.partname) not in placed_partnames):
            placed_partnames.add(str(part.partname))
            leftovers.append(part.blob)
    for index, blob in enumerate(leftovers, start=image_counter + 1):
        ocr_text = _ocr_image(blob, hint=f"{path.name} 内嵌图{index}")
        if ocr_text:
            parts.append(f"[文档内图片 {index} · OCR 识别]\n{ocr_text}")

    regions = _version_region("".join(opening["parts"]), "文档开头")
    if page_segment > 1:
        parts.insert(0, PAGE_MARKER_TEMPLATE.format(page=1))
    front_matter = {"_version_regions": regions}
    if has_revisions:
        front_matter["source_note"] = "文档含修订痕迹，已按「接受全部修订」读取"
    return "\n\n".join(parts), title, front_matter


def _read_pdf(path: Path) -> tuple[str, str, dict]:
    """PDF：逐页读文字层；扫描页（没有文字层）整页交给 OCR；普通页的内嵌大图
    也抽出识别（图表截图上的文字同样可检索）。页与页之间插入分页标记。"""
    reader = PdfReader(str(path))
    if reader.is_encrypted and not reader.decrypt(""):
        # 空密码解不开（真加密）才拒绝；仅权限加密（很多企业 PDF）任何阅读器都能直接读
        raise ParseError("PDF 已加密且需要密码，无法读取")
    pages: list[str] = []
    regions: list[dict] = []
    for index, page in enumerate(reader.pages, start=1):
        page_text = (page.extract_text() or "").strip()
        if index == 1:
            regions.extend(_version_region(page_text, "第1页"))
        page_parts: list[str] = []
        if page_text:
            page_parts.append(page_text)

        ocr_blocks: list[str] = []
        try:
            embedded = list(page.images)
        except Exception:
            embedded = []  # 个别 PDF 的图片流解不出来，跳过不影响文字层
        for image in embedded:
            try:
                data = image.data
            except Exception:
                continue
            ocr_text = _ocr_image(data, hint=f"{path.name} 第{index}页")
            if ocr_text:
                ocr_blocks.append(ocr_text)

        if index == 1:
            regions.extend(_version_region("\n".join(ocr_blocks), "第1页", "ocr"))
        if ocr_blocks:
            if not page_text:
                # 整页没有文字层 → 扫描件，OCR 结果就是这一页的全部内容
                page_parts.append("[扫描页 · OCR 识别]\n" + "\n".join(ocr_blocks))
            else:
                page_parts.append("[页面内图片 · OCR 识别]\n" + "\n".join(ocr_blocks))

        pages.append(f"{PAGE_MARKER_TEMPLATE.format(page=index)}\n" + "\n".join(page_parts))
    title = ""  # 报告 F4/G3：文档属性里的 /Title 比文件名更有信息量时作为标题
    try:
        pdf_title = str((reader.metadata or {}).get("/Title") or "").strip()
    except Exception:
        pdf_title = ""
    if pdf_title and pdf_title != path.stem:
        title = pdf_title
    return "\n".join(pages), title, {"_version_regions": regions}


XLSX_ROW_GROUP = 4  # 行组切分的数据行数（R6-M07/C03；调大=更大卡，调小=更碎）
XLSX_MAX_ROWS = 5000  # 单个工作表收录的数据行上限（R6 起；超出部分截断并在入库日志提示）
XLSX_HEADER_SCAN_ROWS = 10  # 表头/标题只在前 N 个可见行里识别（报告 E2）


def _format_decimals(number_format: str) -> int:
    """从数字格式串里取小数位数（如 "0.0%" → 1、"¥#,##0.00" → 2）。"""
    match = re.search(r"\.([0#]+)", number_format)
    return len(match.group(1)) if match else 0


def _xlsx_formulas(path: Path) -> dict[int, set[tuple[int, int]]]:
    """每个工作表里公式单元格的 (行, 列) 集合（报告 E5）。

    data_only 模式拿不到公式本身，要区分「空单元格」和「没有缓存值的公式」
    必须用 data_only=False 再扫一遍；read_only 流式读取，代价与表格大小线性。
    """
    result: dict[int, set[tuple[int, int]]] = {}
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(str(path), data_only=False, read_only=True)
        try:
            for index, sheet in enumerate(workbook.worksheets):
                coords = set()
                for row in sheet.iter_rows():
                    for cell in row:
                        if isinstance(cell.value, str) and cell.value.startswith("="):
                            coords.add((cell.row, cell.column))
                result[index] = coords
        finally:
            workbook.close()
    except Exception:
        pass
    return result


def _xlsx_cell_text(cell, formula_coords: set[tuple[int, int]]) -> str:
    """单元格 → 显示文本（报告 E1/E5/E6）。

    按「Excel 里显示的样子」输出而不是存储值：0.15 配 0% 格式输出 15%、
    日期不再带 00:00:00 噪声、超长整数编号不再变科学计数法丢尾数、
    布尔值输出是/否；无缓存值的公式显式标注；单元格内换行统一成 " / "。
    """
    value = cell.value
    if value is None:
        return "[公式]" if (cell.row, cell.column) in formula_coords else ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, datetime):
        if value.year <= 1900 and not re.search(r"[yd]", (cell.number_format or "").lower()):
            return value.strftime("%H:%M:%S")  # Excel 日期纪元里的纯时间值
        if value.hour or value.minute or value.second:
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, timedelta):
        total_minutes = int(value.total_seconds()) // 60
        return f"{total_minutes // 60}:{total_minutes % 60:02d}"
    if isinstance(value, (int, float)):
        number_format = cell.number_format or "General"
        if "%" in number_format:
            return f"{value * 100:.{_format_decimals(number_format)}f}%"
        if isinstance(value, float) and value.is_integer() and abs(value) >= 1e15:
            return str(int(value))  # 超长编号：浮点 str 是科学计数法且尾数已失真
        if "#,##" in number_format:
            text = f"{value:,.{_format_decimals(number_format)}f}"
            currency = number_format.split("#", 1)[0].strip().strip('"')
            return f"{currency}{text}" if currency else text
        if isinstance(value, float) and value.is_integer() and "e" not in number_format.lower():
            return str(int(value))
        return str(value)
    return (str(value).replace("\r\n", " / ").replace("\n", " / ").replace("|", "\\|")).strip()


def _xlsx_sheet_text(sheet, formula_coords: set[tuple[int, int]]) -> tuple[list[str], str | None, list[str], bool]:
    """一个工作表 → (标题行文本列表, 表头行文本, 数据行文本列表, 是否截断)。

    报告 E2：跨列合并的大标题（如「某市××统计表」）识别为标题行，不再冒充表头；
    表头行带横向合并（多级表头）时与下一行拼接成单行表头；纵向合并的类别列
    向下补值，让每一行数据都自带类别。
    报告 E3：隐藏的工作表 / 行 / 列不入库。
    """
    hidden_columns: set[int] = set()
    for key, dimension in sheet.column_dimensions.items():
        if dimension.hidden:
            for column_index in range(dimension.min or 1, (dimension.max or dimension.min or 1) + 1):
                hidden_columns.add(column_index)
    merged_ranges = list(sheet.merged_cells.ranges)
    max_col = sheet.max_column or 1

    def row_hidden(row_number: int) -> bool:
        dimension = sheet.row_dimensions.get(row_number)
        return bool(dimension and dimension.hidden)

    def cell_text(row_number: int, col: int) -> str:
        if col in hidden_columns:
            return ""
        try:
            cell = sheet.cell(row=row_number, column=col)
        except Exception:
            return ""
        return _xlsx_cell_text(cell, formula_coords)

    def anchor_of(row_number: int, col: int) -> tuple[int, int]:
        for rng in merged_ranges:
            if rng.min_row <= row_number <= rng.max_row and rng.min_col <= col <= rng.max_col:
                return rng.min_row, rng.min_col
        return row_number, col

    row_texts: list[tuple[int, list[str]]] = []
    truncated = False
    for row_number in range(1, (sheet.max_row or 0) + 1):
        if row_hidden(row_number):
            continue
        texts = [cell_text(row_number, col) for col in range(1, max_col + 1)]
        if not any(text.strip() for text in texts):
            continue
        if len(row_texts) >= XLSX_MAX_ROWS + XLSX_HEADER_SCAN_ROWS:
            truncated = True  # 上限按数据行计，这里多留表头扫描余量
            break
        row_texts.append((row_number, texts))

    values = dict(row_texts)

    def value_at(row_number: int, col: int) -> str:
        anchor_row, anchor_col = anchor_of(row_number, col)
        row = values.get(anchor_row)  # values 直接映射 行号→文本列表
        return row[anchor_col - 1] if row and anchor_col - 1 < len(row) else ""

    def nonempty_count(index: int) -> int:
        """该行有效非空列数：合并区域的值跟随锚点，同一合并区只计一次。"""
        row_number = row_texts[index][0]
        counted: set[tuple[int, int]] = set()
        count = 0
        for col in range(1, max_col + 1):
            if col in hidden_columns:
                continue
            anchor = anchor_of(row_number, col)
            if anchor in counted:
                continue
            counted.add(anchor)
            if value_at(*anchor).strip():
                count += 1
        return count

    # 找标题行与表头行：非空列数 ≤1 的行是跨列大标题；首个 ≥2 列的行是表头
    title_lines: list[str] = []
    header_index: int | None = None
    for index in range(min(len(row_texts), XLSX_HEADER_SCAN_ROWS)):
        count = nonempty_count(index)
        if count >= 2:
            header_index = index
            break
        title_lines.append(" ".join(text for text in row_texts[index][1] if text.strip()))
    if header_index is None:
        if not row_texts:
            return [], None, [], False
        header_index, title_lines = 0, []  # 单列清单没有独立表头，沿用旧行为：首行即表头

    header_row_number, header_cells = row_texts[header_index]
    data_start = header_index + 1
    # 报告 E2：表头行带横向合并（多级表头）且下一行非空 → 上下拼接成单行表头
    has_horizontal_merge = any(rng.min_row <= header_row_number <= rng.max_row and rng.max_col > rng.min_col
                               for rng in merged_ranges)
    if has_horizontal_merge and data_start < len(row_texts) and nonempty_count(data_start) >= 2:
        next_row_number = row_texts[data_start][0]
        header_cells = [value_at(next_row_number, col) if value_at(next_row_number, col).strip()
                        else value_at(header_row_number, col) for col in range(1, max_col + 1)]
        data_start += 1
    header_line = _squeeze_cjk_spaces(" | ".join(header_cells)).rstrip(" |")

    # 报告 E2：纵向合并的列（类别列）向下补值
    ffill_columns = {rng.min_col for rng in merged_ranges
                     if rng.min_row != rng.max_row and rng.min_col == rng.max_col}
    data_lines: list[str] = []
    last_values: dict[int, str] = {}
    for _row_number, texts in row_texts[data_start:]:
        cells = list(texts)
        for col in ffill_columns:
            index = col - 1
            if index >= len(cells):
                continue
            if cells[index].strip():
                last_values[col] = cells[index]
            elif col in last_values:
                cells[index] = last_values[col]
        line = _squeeze_cjk_spaces(" | ".join(cells)).rstrip(" |")
        if not line:
            continue
        data_lines.append(line)
        if len(data_lines) >= XLSX_MAX_ROWS:
            truncated = True
            break
    return title_lines, header_line, data_lines, truncated


def _sheet_table_parts(title: str, title_lines: list[str], header_line: str | None,
                       data_lines: list[str], truncated: bool) -> tuple[list[str], str | None]:
    """把一个工作表/CSV 的解析结果组装成「Sheet 标题 + 行组分块」文本（Excel 与 CSV 共用）。

    行组切分（2026-11-21，R6-M07/C03）：每 XLSX_ROW_GROUP 数据行一组，组标题用 H1
    （chunker 唯一不合并的章边界），每组重复表头使列名自足；小表（≤组行数）不切。
    截断时同时返回提示文本，供调用方写进入库日志（报告 E4）。
    """
    parts: list[str] = [f"## Sheet：{title}", *title_lines]
    note: str | None = None
    if truncated:
        note = f"Sheet「{title}」超过 {XLSX_MAX_ROWS} 行，后续行未收录"
        parts.append(f"（{note}）")
    if header_line is None:
        return parts, note
    if len(data_lines) <= XLSX_ROW_GROUP:
        # 小表不切：沿用「Sheet 标题 + 表头 + 全部行」的单块结构
        parts.append(header_line)
        parts.extend(data_lines)
    else:
        for start in range(0, len(data_lines), XLSX_ROW_GROUP):
            group = data_lines[start:start + XLSX_ROW_GROUP]
            parts.append(f"# {title}（第 {start + 1}~{start + len(group)} 行）")
            parts.append(header_line)
            parts.extend(group)
            parts.append("")
    return parts, note


def _read_xlsx(path: Path) -> tuple[str, str, dict]:
    """Excel：每个工作表转成「Sheet 标题 + 行组分块的表格文本」。

    - data_only=True：公式单元格取最近一次计算值（拿不到计算值的公式显式标注 [公式]）
    - 隐藏的工作表 / 行 / 列不入库（报告 E3）
    - 单表超过 5000 行截断，截断在入库日志里提示（报告 E4）
    - 行组切分（2026-11-21，R6-M07/C03）：每 XLSX_ROW_GROUP 数据行一组，组标题用
      H1（chunker 唯一不合并的章边界），每组重复表头使列名自足；小表（≤组行数）
      不切仍为单卡。
    """
    if _sniff_binary_kind(path) == "ole":
        # 假 .xlsx：文件头是 OLE2 → 实为 xls 老格式，交给 LibreOffice 转换
        return _read_legacy_office(path, "xlsx", "calc")
    from openpyxl import load_workbook

    formulas = _xlsx_formulas(path)
    # 图片提取用普通模式（read_only 模式不加载内嵌图片）；表格读完后对每张图 OCR
    workbook = load_workbook(str(path), data_only=True)
    parts: list[str] = []
    notes: list[str] = []
    try:
        sheet_images: list[tuple[str, bytes]] = []
        for sheet_index, sheet in enumerate(workbook.worksheets):
            if sheet.sheet_state != "visible":
                continue  # 报告 E3：隐藏工作表
            title_lines, header_line, data_lines, truncated = _xlsx_sheet_text(sheet, formulas.get(sheet_index, set()))
            if header_line is None:
                continue  # 空表
            sheet_parts, note = _sheet_table_parts(sheet.title, title_lines, header_line, data_lines, truncated)
            parts.extend(sheet_parts)
            if note:
                notes.append(note)
            for image in getattr(sheet, "_images", []):
                try:
                    blob = image._data()
                except Exception:
                    blob = None
                if blob:
                    sheet_images.append((sheet.title, blob))
            parts.append("")
    finally:
        workbook.close()
    if sheet_images:
        ocr_blocks: list[str] = []
        for index, (sheet_title, blob) in enumerate(sheet_images, start=1):
            ocr_text = _ocr_image(blob, hint=f"{path.name} {sheet_title} 内嵌图{index}")
            if ocr_text:
                ocr_blocks.append(f"[{sheet_title} 内嵌图片 {index} · OCR 识别]\n{ocr_text}")
        if ocr_blocks:
            parts.append("\n\n".join(ocr_blocks))
    return "\n".join(parts).strip(), "", ({"source_note": "；".join(notes)} if notes else {})


def _iter_shapes_flat(shapes):
    """深度优先展平组合形状（GroupShape.shapes）：组内文字/表格不再被静默跳过。"""
    for shape in shapes:
        if shape.shape_type == 6 and hasattr(shape, "shapes"):  # MSO_SHAPE_TYPE.GROUP
            yield from _iter_shapes_flat(shape.shapes)
        else:
            yield shape


def _read_pptx(path: Path) -> tuple[str, str, dict]:
    """PPT：逐页读——标题占位符转 # 标题，文本框/表格按序读取，页面内图片交给
    OCR（组合形状递归展开），演讲者备注跟在页尾。
    页与页之间插分页标记（与 PDF 同机制，切片时页码进元数据）。"""
    if _sniff_binary_kind(path) == "ole":
        # 假 .pptx：文件头是 OLE2 → 实为 ppt 老格式，交给 LibreOffice 转换
        return _read_legacy_office(path, "pptx", "impress")
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    def _collect_slide_images(shapes, out: list[bytes]) -> None:
        """收集一页里的所有图片字节；组合形状（Group）递归展开。
        图片占位符/普通图片都有 .image 属性，其他形状没有该属性。"""
        for shape in shapes:
            shape_type = getattr(shape, "shape_type", None)
            if shape_type == MSO_SHAPE_TYPE.GROUP:
                _collect_slide_images(getattr(shape, "shapes", []), out)
                continue
            image = getattr(shape, "image", None)
            if image is None:
                continue
            try:
                out.append(image.blob)
            except Exception:
                pass  # 链接型图片等取不到 blob 的，按无图处理

    prs = Presentation(str(path))
    parts: list[str] = []
    regions: list[dict] = []
    title = ""
    for index, slide in enumerate(prs.slides, start=1):
        if slide.element.get("show") == "0":
            continue  # 报告 T2：隐藏页（放映时跳过的草稿页）不入库
        parts.append(PAGE_MARKER_TEMPLATE.format(page=index))
        page_parts: list[str] = []

        title_shape = slide.shapes.title
        title_id = title_shape.shape_id if title_shape is not None else None
        if title_shape is not None and title_shape.has_text_frame:
            shape_title = title_shape.text_frame.text.strip()
            if shape_title:
                page_parts.append(f"# {shape_title}")
                if not title:
                    title = shape_title

        for shape in _iter_shapes_flat(slide.shapes):
            if shape.shape_id == title_id:
                continue
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    page_parts.extend(line.strip() for line in text.splitlines() if line.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [_squeeze_cjk_spaces(" | ".join(cell.text.strip() for cell in row.cells))]
                    if any(cells):
                        page_parts.append(cells[0])

        if index == 1:
            regions.extend(_version_region("\n".join(page_parts), "第1张幻灯片"))
        slide_images: list[bytes] = []
        _collect_slide_images(slide.shapes, slide_images)
        ocr_blocks: list[str] = []
        opening_ocr: list[str] = []
        for img_index, blob in enumerate(slide_images, start=1):
            ocr_text = _ocr_image(blob, hint=f"{path.name} 第{index}页图片{img_index}")
            if ocr_text:
                ocr_blocks.append(f"[页面图片 {img_index} · OCR 识别]\n{ocr_text}")
                if index == 1:
                    opening_ocr.append(ocr_text)
        if index == 1:
            regions.extend(_version_region("\n".join(opening_ocr), "第1张幻灯片", "ocr"))
        if ocr_blocks:
            page_parts.append("\n".join(ocr_blocks))

        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notes:
                page_parts.append(f"> 演讲者备注：{notes}")

        parts.append("\n".join(page_parts))
    return "\n".join(parts), title, {"_version_regions": regions}


def _sniff_delimiter(text: str) -> str:
    """CSV 分隔符嗅探：TSV 固定制表符；CSV 用 Sniffer（失败回落逗号）。"""
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _plain_sheet_text(rows: list[list[str]]) -> tuple[list[str], str | None, list[str], bool]:
    """无合并单元格的纯行表（CSV/TSV）→ (标题行文本, 表头行文本, 数据行文本, 是否截断)。

    表头/标题识别与 Excel 同规则：首个非空列数 ≥2 的行是表头，之前的单列行是
    大标题；没有合并单元格，因此不需要拼接与补值（报告 E7）。
    """
    non_empty = [(row, [str(cell).replace("\r\n", " / ").replace("\n", " / ").replace("|", "\\|").strip()
                        for cell in row])
                 for row in rows if any(str(cell).strip() for cell in row)]
    title_lines: list[str] = []
    header_index: int | None = None
    for index, (_, cells) in enumerate(non_empty[:XLSX_HEADER_SCAN_ROWS]):
        if sum(1 for cell in cells if cell.strip()) >= 2:
            header_index = index
            break
        title_lines.append(" ".join(cell for cell in cells if cell.strip()))
    if header_index is None:
        if not non_empty:
            return [], None, [], False
        header_index, title_lines = 0, []  # 单列清单沿用旧行为：首行即表头
    header_line = _squeeze_cjk_spaces(" | ".join(non_empty[header_index][1])).rstrip(" |")
    data_lines = []
    truncated = False
    for _row_number, cells in non_empty[header_index + 1:]:
        line = _squeeze_cjk_spaces(" | ".join(cells)).rstrip(" |")
        if line:
            data_lines.append(line)
        if len(data_lines) >= XLSX_MAX_ROWS:
            truncated = True
            break
    return title_lines, header_line, data_lines, truncated


def _read_csv(path: Path) -> tuple[str, str, dict]:
    """CSV/TSV（报告 E7）：复用 Excel 的行组表格结构与表头/标题识别。

    编码走 _read_text_smart（UTF-8/GBK/UTF-16 嗅探）；分隔符 TSV 固定制表符、
    CSV 嗅探（失败回落逗号）；引号内的换行与分隔符由 csv 模块处理。
    """
    text = _read_text_smart(path)
    delimiter = "\t" if path.suffix.lower() == ".tsv" else _sniff_delimiter(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    title_lines, header_line, data_lines, truncated = _plain_sheet_text(rows)
    parts, note = _sheet_table_parts(path.stem, title_lines, header_line, data_lines, truncated)
    return "\n".join(parts).strip(), "", ({"source_note": note} if note else {})


def _read_image(path: Path) -> tuple[str, str, dict]:
    """图片文件：整张图交给 OCR 识别（中英文），识别文本即正文；无文字则得到空文档。
    标题取识别结果的第一行（报告 G3），识别不出时由 parse_file 回落到文件名。"""
    text = _ocr_image(path.read_bytes(), hint=path.name)
    title = next((line.strip() for line in text.splitlines() if line.strip()), "")[:60]
    return text, title, {}


_READERS = {
    ".txt": _read_txt,
    ".md": _read_markdown,
    ".rtf": _read_rtf,
    ".html": _read_html,
    ".htm": _read_html,
    ".docx": _read_docx,
    ".docm": _read_docx,  # 启用宏的 Word，容器与 docx 相同
    ".doc": _read_doc,
    ".wps": _read_wps,
    ".pdf": _read_pdf,
    ".xlsx": _read_xlsx,
    ".xls": _read_xls,
    ".et": _read_et,
    ".csv": _read_csv,  # 报告 E7：CSV/TSV 复用表格行组结构
    ".tsv": _read_csv,
    ".pptx": _read_pptx,
    ".pptm": _read_pptx,  # 启用宏的 PPT，容器与 pptx 相同
    ".ppt": _read_ppt,
    ".dps": _read_dps,
    ".png": _read_image,
    ".jpg": _read_image,
    ".jpeg": _read_image,
    ".bmp": _read_image,
    ".tif": _read_image,   # 报告 I2：多页 TIFF/GIF 逐帧识别
    ".tiff": _read_image,
    ".gif": _read_image,
    ".webp": _read_image,
}


def parse_file(path: Path | str) -> ParsedDocument:
    """把单个文件解析成统一的 ParsedDocument。失败会抛出带文件名的错误。"""
    path = Path(path)
    if not path.exists():
        raise ParseError(f"文件不存在：{path}")
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        supported = "、".join(sorted(SUPPORTED_EXTENSIONS))
        raise ParseError(f"暂不支持 {ext} 格式（目前支持：{supported}）：{path.name}")

    _ocr_budget_begin()  # 报告 G2/决策3：单文件 OCR 时间预算（默认 120 秒）
    try:
        text, title, front_matter = _READERS[ext](path)
    except ParseError:
        raise
    except Exception as exc:  # 解析库的各类异常统一包装，避免用户看到天书
        raise ParseError(f"解析失败：{path.name}（{type(exc).__name__}: {exc}）") from exc

    skipped = _OCR_STATE["skipped"]
    if skipped:
        note = f"OCR 时间预算用尽，{skipped} 张图片未识别"
        existing = front_matter.get("source_note")
        front_matter["source_note"] = f"{existing}；{note}" if existing else note

    version_regions = front_matter.pop("_version_regions", [])
    metadata = _base_metadata(path, title or path.stem, ext.lstrip("."))
    return ParsedDocument(text=text, metadata=metadata, front_matter=front_matter,
                          version_regions=version_regions)


def parse_directory(dir_path: Path | str) -> tuple[list[ParsedDocument], list[str]]:
    """解析整个目录（含子目录），返回（成功列表, 失败原因列表）。"""
    from src.project_directories import is_generated_info

    dir_path = Path(dir_path)
    if not dir_path.exists():
        raise ParseError(f"知识目录不存在：{dir_path}")

    docs: list[ParsedDocument] = []
    failures: list[str] = []
    for path in sorted(dir_path.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(dir_path).parts):
            continue  # 隐藏目录（.rag_converted 转换缓存等）不入解析清单
        if not path.is_file() or path.name.startswith(".") or is_generated_info(path):
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            docs.append(parse_file(path))
        except ParseError as exc:
            failures.append(str(exc))
    return docs, failures


# ---------------- 直接运行本文件：打印解析结果，人工检查 ----------------
if __name__ == "__main__":
    cfg = get_config()
    docs, failures = parse_directory(cfg.knowledge_dir)
    print(f"知识目录：{cfg.knowledge_dir}")
    print(f"解析成功 {len(docs)} 个文件，失败 {len(failures)} 个\n")

    for failure in failures:
        print(f"❌ {failure}")

    for doc in docs:
        meta = doc.metadata
        preview = re.sub(r"\s+", " ", doc.text[:120])
        print("=" * 60)
        print(f"文件：{meta['source']}（{meta['file_type']}）")
        print(f"标题：{meta['title']}  字数：{len(doc.text)}")
        print(f"日期：创建 {meta['created_at']} / 修改 {meta['updated_at']}")
        print(f"正文开头：{preview}……")
    sys.exit(0)
