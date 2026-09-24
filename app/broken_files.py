"""乱码与损坏书籍的检测 / 修复辅助（供管理员「乱码与损坏文件」页使用）。

背景：部分书籍在入库前文件名就已损坏（原中文字节丢失，落盘即为 U+FFFD 替换符），
按字节还原不可能的。但**真实书名基本都写在文件内容里**，因此这里走「读内容反推书名」。

对外提供三件事：
  - extract_title(path, ext)  从文件内容反推书名 -> (建议书名, 来源说明)
  - inspect_file(path, ext)   判断文件是否可正常解析 -> (状态, 说明)
  - decode_best(raw)          多编码试解 + 中文打分（乱码文件里也能捞出可用片段）
"""
import os
import re
import zipfile

REPLACEMENT = '\ufffd'
TRY_ENCODINGS = ('utf-8', 'gb18030', 'big5', 'utf-16')
CJK = re.compile(r'[\u4e00-\u9fff]')
BAD_CTRL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

# 明显不是书名的行首
SKIP_LINE = re.compile(
    r'^(第\s*[0-9０-９一二三四五六七八九十百千零〇两]{1,12}\s*[章节回卷篇部集幕节]'
    r'|正文|简介|内容简介|内容|作者|分类|标签|来源|声明|版权|目录|序章|楔子|后记|尾声'
    r'|完本感言|上架感言|https?://|www\.|www$|转载|仅供)'
)
NAME_PAT = re.compile(r'(?:书名|标题|book\s*name|bookname|title)\s*[:：]\s*(.{2,60})', re.I)
BOOKMARK_PAT = re.compile(r'《([^》]{2,50})》')
TRAILING_NOISE = re.compile(
    r'(完结|全本|完本|校对版|精校版|未删减|无删减|无错版|TXT|txt|下载|免费阅读|全文字)[》\]）)】]*$'
)


def _clean_name(s):
    s = (s or '').strip()
    s = s.strip('「」『』“”"\'《》【】[]()（）<>·-—_ ')
    s = re.sub(r'\s+', ' ', s)
    s = TRAILING_NOISE.sub('', s).strip(' ·-—_')
    s = re.sub(r'[（(]\s*[)）]', '', s).strip()
    return s[:80]


def decode_best(raw):
    """多编码试解，用「中文字数 - 替换符/控制符惩罚」打分。

    返回 (text, encoding_name)。
    """
    best_text = None
    best_enc = ''
    best_score = None
    for enc in TRY_ENCODINGS:
        try:
            t = raw.decode(enc)
        except Exception:
            continue
        score = len(CJK.findall(t))
        score -= 40 * t.count(REPLACEMENT)
        score -= 20 * len(BAD_CTRL.findall(t[:4000]))
        score -= 200 * t.count('\x00')
        if best_score is None or score > best_score:
            best_text, best_enc, best_score = t, enc, score
    if best_text is None:
        return raw.decode('utf-8', 'replace'), 'utf-8(replace)'
    return best_text, best_enc


def title_from_text(text):
    """从正文里挑出最像书名的一行。返回 (书名, 来源说明) 或 (None, '')。"""
    lines = [l.strip() for l in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    lines = [l for l in lines if l]

    # 1) 「书名：XXX」这类显式标注最可靠
    for l in lines[:30]:
        m = NAME_PAT.search(l)
        if m:
            t = _clean_name(m.group(1))
            if t and CJK.search(t):
                return t, '正文「书名：」标注'

    # 2) 正文里的《XXX》
    for l in lines[:40]:
        for m in BOOKMARK_PAT.finditer(l):
            t = _clean_name(m.group(1))
            if t and 2 <= len(t) <= 50:
                return t, '正文书名号《》'

    # 3) 前若干行里第一个「像书名」的短行
    for l in lines[:12]:
        t = _clean_name(l)
        if not t or CJK.search(t) is None:
            continue
        if SKIP_LINE.match(t):
            continue
        if len(t) < 2 or len(t) > 40:
            continue
        if len(re.findall(r'[，。！？；：,.;:!?、]', t)) > 2:
            continue
        return t, '正文首行'
    return None, ''


def _from_txt(path):
    with open(path, 'rb') as f:
        raw = f.read(65536)
    text, enc = decode_best(raw)
    t, src = title_from_text(text)
    if not t:
        return None, ''
    return t, '%s · %s' % (src, enc)


def _from_epub(path):
    with zipfile.ZipFile(path) as z:
        opf = None
        for n in z.namelist():
            if n.lower().endswith('.opf'):
                opf = n
                break
        if not opf:
            return None, ''
        data = z.read(opf).decode('utf-8', 'replace')
    m = re.search(r'<dc:title[^>]*>(.*?)</dc:title>', data, re.S | re.I)
    if not m:
        return None, ''
    t = _clean_name(re.sub(r'<[^>]+>', '', m.group(1)))
    return (t, 'EPUB 元数据 dc:title') if t else (None, '')


def _from_pdf(path):
    try:
        from PyPDF2 import PdfReader
        with open(path, 'rb') as f:
            meta = PdfReader(f).metadata
        t = _clean_name(getattr(meta, 'title', '') or '')
        return (t, 'PDF 元数据 Title') if t else (None, '')
    except Exception:
        pass
    # 退化为在文件头里找 /Title
    try:
        with open(path, 'rb') as f:
            raw = f.read(262144)
        m = re.search(rb'/Title\s*\((.{2,120}?)\)', raw, re.S)
        if m:
            t, _enc = decode_best(m.group(1))
            t = _clean_name(t)
            if t:
                return t, 'PDF /Title'
    except Exception:
        pass
    return None, ''


def extract_title(path, ext):
    """从文件内容反推真实书名。返回 (建议书名, 来源说明)；失败返回 (None, '')。"""
    ext = (ext or '').lower().lstrip('.')
    try:
        if ext == 'epub':
            return _from_epub(path)
        if ext == 'pdf':
            return _from_pdf(path)
        if ext in ('txt', 'md', 'log', 'cn', 'html', 'htm'):
            return _from_txt(path)
    except Exception:
        return None, ''
    return None, ''


MOJI_RANGES = (
    ('\u0080', '\u00ff'),    # Latin-1 补充区：GBK 首字节被当 Unicode 码位
    ('\u0100', '\u024f'),    # Latin 扩展区
    ('\u0370', '\u04ff'),    # 希腊/西里尔：GBK 双字节被当 UTF-8 解出的典型形态
    ('\u02b0', '\u036f'),    #  Modifier 区
)


def _in_moji_ranges(ch):
    for lo, hi in MOJI_RANGES:
        if lo <= ch <= hi:
            return True
    return False


def looks_mojibake(s):
    """名字是否像「编码被误读」的乱码（含替换符或大量异体字符）。"""
    if not s:
        return False
    if REPLACEMENT in s:
        return True
    hit = sum(1 for ch in s if _in_moji_ranges(ch))
    return hit >= max(2, len(s) // 4)


def recover_mojibake(s):
    """把「UTF-8 化的 GBK/Big5」这类乱码还原成中文。

    典型形态：目录名原本是 GBK 字节，落盘时被按 UTF-8 解码，于是每个中文变成
    2~3 个西里尔/拉丁扩展字符（如 '小说系' -> 'С˵ϵ'）。此时
    ``name.encode('utf-8').decode('gbk')`` 可以精确还原。

    还原结果必须通过校验才返回：不含替换符、含至少一个汉字、长度不长于原文。
    无法还原返回 None。
    """
    if not s or not looks_mojibake(s):
        return None
    for enc in ('gb18030', 'gbk', 'big5'):
        for raw in (s.encode('utf-8', 'ignore'), s.encode('latin-1', 'ignore')):
            if not raw:
                continue
            try:
                cand = raw.decode(enc)
            except Exception:
                continue
            if REPLACEMENT in cand:
                continue
            if not CJK.search(cand):
                continue
            # 还原后不应引入不可见控制符，也不该比原文更长
            if BAD_CTRL.search(cand):
                continue
            if len(cand) > len(s):
                continue
            # 必须能原样逆变换回去：否则说明只是「碰巧解出了汉字」，名字未必正确。
            # 改名涉及磁盘目录，宁可少改也不要改错。
            try:
                if cand.encode(enc) != raw:
                    continue
            except Exception:
                continue
            return cand
    return None


def inspect_file(path, ext):
    """判断文件是否可正常解析。返回 (status, detail)。

    status: ok / missing（磁盘上没有）/ empty（空文件）/ unreadable（无法解析）
    """
    if not path or not os.path.exists(path):
        return 'missing', '磁盘上找不到该文件'
    try:
        size = os.path.getsize(path)
    except Exception as e:
        return 'unreadable', '无法读取文件属性：%s' % e
    if size == 0:
        return 'empty', '文件大小为 0 字节'

    ext = (ext or '').lower().lstrip('.')
    try:
        if ext == 'epub':
            with zipfile.ZipFile(path) as z:
                if z.testzip() is not None:
                    return 'unreadable', '压缩包已损坏'
                if not any(n.lower().endswith('.opf') for n in z.namelist()):
                    return 'unreadable', '结构不完整（找不到 OPF）'
            return 'ok', ''
        if ext == 'pdf':
            with open(path, 'rb') as f:
                if not f.read(5).startswith(b'%PDF'):
                    return 'unreadable', '不是有效的 PDF（缺少文件头）'
            return 'ok', ''
        if ext in ('txt', 'md', 'log', 'cn', 'html', 'htm'):
            with open(path, 'rb') as f:
                raw = f.read(8192)
            if not raw.strip(b'\x00 \t\r\n'):
                return 'empty', '文件开头为空'
            if raw.strip(b'\x00') == b'':
                return 'unreadable', '内容全为零字节'
            text, _enc = decode_best(raw)
            if text and text.count(REPLACEMENT) > max(20, len(text) // 5):
                return 'unreadable', '编码无法识别（乱码比例过高）'
            return 'ok', ''
    except Exception as e:
        return 'unreadable', '%s: %s' % (type(e).__name__, e)
    return 'ok', ''
