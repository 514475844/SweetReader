# -*- coding: utf-8 -*-
"""文件类型识别 —— 按**内容**判断真实格式，而不只信扩展名。

真实书库里按扩展名判断经常出错：`.txt` 其实是 EPUB、`.epub` 其实是个普通 ZIP、
`.pdf` 其实是 HTML……这些书在读者端表现为「打不开」，但扫描阶段看不出来。
本模块只干一件事：读文件头（必要时读 ZIP 目录）给出真实格式，并判断
「扩展名 vs 内容」是否一致，不一致时给出可读告警。

对外接口：
    sniff_stream(stream, name)   → 真实类型（不可判定时返回 None）
    resolve_format(ext, sniffed) → (入库类型, 告警文本或 '')
    importable_formats()         → 支持格式清单（供前端展示，后端单一来源）
"""

import os
import zipfile

# ---- 扩展名 → 内部 file_type（与阅读器路由的判断口径保持一致）----
EXT_TYPE_MAP = {
    '.txt': 'txt', '.text': 'txt', '.log': 'txt', '.cn': 'txt',
    '.md': 'md', '.markdown': 'md',
    '.epub': 'epub',
    '.pdf': 'pdf',
    '.mobi': 'mobi', '.azw': 'azw', '.azw3': 'azw3',
    '.fb2': 'fb2',
    '.html': 'html', '.htm': 'html', '.xhtml': 'html', '.mhtml': 'html',
    '.rtf': 'rtf',
    '.zip': 'zip', '.rar': 'rar',
}

# 展示名（前端格式清单用）
TYPE_LABELS = {
    'txt': '纯文本', 'md': 'Markdown', 'epub': 'EPUB', 'pdf': 'PDF',
    'mobi': 'MOBI', 'azw': 'AZW', 'azw3': 'AZW3', 'fb2': 'FictionBook',
    'html': '网页', 'rtf': 'RTF', 'zip': 'ZIP 压缩包', 'rar': 'RAR 压缩包',
}

# 支持格式清单：kind=book 为书籍、kind=archive 为批量压缩包
SUPPORTED_FORMATS = [
    ('.txt', 'book', '纯文本，最常见'),
    ('.epub', 'book', '标准电子书'),
    ('.pdf', 'book', 'PDF 文档'),
    ('.md', 'book', 'Markdown'),
    ('.html', 'book', '网页 / 单页'),
    ('.mobi', 'book', 'Kindle 旧格式'),
    ('.azw3', 'book', 'Kindle 新格式'),
    ('.fb2', 'book', 'FictionBook'),
    ('.log', 'book', '与 txt 同处理'),
    ('.cn', 'book', '与 txt 同处理'),
    ('.zip', 'archive', '批量导入，自动解压'),
    ('.rar', 'archive', '批量导入，需服务端支持'),
]

# 只按扩展名还不够的结构化格式：出现在弱扩展名（txt/md）下要改判
STRONG_TYPES = {'epub', 'pdf', 'mobi', 'azw3', 'zip', 'rar', 'fb2'}
# 弱扩展名：换行可见即可，内容与扩展名不符不构成问题
WEAK_TYPES = {'txt', 'md', 'html'}

# 允许的「扩展名 vs 内容」组合（同族，不算冲突、不告警）
COMPATIBLE = {
    'epub': {'epub'},
    'pdf': {'pdf'},
    'zip': {'zip', 'epub'},
    'rar': {'rar'},
    'mobi': {'mobi', 'azw3', 'azw'},
    'azw': {'mobi', 'azw3', 'azw'},
    'azw3': {'mobi', 'azw3', 'azw'},
    'fb2': {'fb2'},
    # 纯文本家族互相兼容
    'txt': {'txt', 'md', 'html', 'fb2'},
    'md': {'txt', 'md', 'html'},
    'html': {'html', 'txt', 'md'},
    'rtf': {'rtf', 'txt'},
}

SNIFF_BYTES = 8192       # 嗅探读取的字节数（含正文判断所需的最少量）
_HTML_TOKENS = (b'<html', b'<!doctype html', b'<head', b'<body')
_TEXT_TOKENS_ENC = ('utf-8', 'gb18030', 'utf-16')


def type_label(file_type):
    """给内部类型一个中文展示名。"""
    return TYPE_LABELS.get((file_type or '').lower(), (file_type or '').upper() or '未知')


def _is_texty(head):
    """判断字节串是否像纯文本（能用常见中文编码解出来且无 NUL）。"""
    if not head:
        return False
    if b'\x00' in head:
        # UTF-16 会有 NUL，但它本来就是文本，单独放行
        try:
            head.decode('utf-16')
            return True
        except Exception:
            return False
    for enc in _TEXT_TOKENS_ENC:
        try:
            head.decode(enc)
            return True
        except Exception:
            continue
    return False


def _sniff_zip(stream, head):
    """ZIP 家族细分：EPUB / 普通 ZIP。"""
    try:
        stream.seek(0)
        with zipfile.ZipFile(stream) as zf:
            names = zf.namelist()
            # 标准 EPUB：第一个条目必须是 mimetype 且内容为 application/epub+zip
            if names and names[0] == 'mimetype':
                try:
                    if zf.read('mimetype')[:20] == b'application/epub+zip':
                        return 'epub'
                except Exception:
                    pass
            if 'META-INF/container.xml' in names:
                return 'epub'          # 少了 mimetype，但结构仍是 EPUB
        return 'zip'
    except Exception:
        return 'zip'                   # 头是 PK 但目录读不出来，仍按压缩包处理
    finally:
        try:
            stream.seek(0)
        except Exception:
            pass


def sniff_stream(stream, name=''):
    """嗅探真实格式。stream 需可 seek（Werkzeug 的 FileStorage 满足）。

    返回内部类型字符串；完全无法判定时返回 None（调用方保留扩展名判定）。
    """
    try:
        stream.seek(0)
        head = stream.read(SNIFF_BYTES)
    except Exception:
        return None
    finally:
        try:
            stream.seek(0)
        except Exception:
            pass

    if not head:
        return 'empty'

    # ---- 结构最明确的一批：先认它们 ----
    if head[:4] == b'%PDF':
        return 'pdf'
    if head[:4] in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08'):
        return _sniff_zip(stream, head)
    if head[:7] == b'Rar!\x1a\x07\x00' or head[:7] == b'Rar!\x1a\x07\x01':
        return 'rar'
    if head[:5] == b'{\\rtf':
        return 'rtf'

    # ---- PalmDB 头（MOBI / AZW / AZW3）----
    if len(head) >= 68:
        ptype = head[60:68]
        if ptype.startswith(b'BOOKMOBI'):
            # AZW3 被称作 KF8，靠版本号区分：这里不做细粒度区分，统一按 azw3 处理
            if len(head) >= 80 and head[76:80] == b'BOUND':
                return 'azw3'
            return 'mobi'
        if ptype.startswith(b'TPZ'):
            return 'azw'

    low = head[:4096].lower()
    if b'<fictionbook' in low:
        return 'fb2'
    if any(tok in low for tok in _HTML_TOKENS):
        return 'html'
    if _is_texty(head):
        return 'txt'
    return None


def resolve_format(ext, sniffed):
    """把「扩展名判定」与「内容嗅探」合成最终入库类型。

    返回 (file_type, warning)：
      - warning 为空串表示二者一致或内容不可判定（保持原判定）；
      - 弱扩展名（txt/md）下探到结构化格式时**改判**，否则保留扩展名判定并给告警。
    """
    ext = (ext or '').lower()
    declared = EXT_TYPE_MAP.get(ext) or (ext.lstrip('.') or 'txt').lower()

    if not sniffed or sniffed == 'empty':
        return declared, ''
    # 扩展名不在支持清单内：扩展名本身给不出可用的阅读器类型，
    # 内容认得出就按内容入库，否则只能维持原名（多半打不开）。
    if ext not in EXT_TYPE_MAP:
        return sniffed, ('扩展名 %s 不在支持格式清单内，检测到实际内容为 %s，已按实际格式入库'
                         % (ext or '（无）', type_label(sniffed)))
    if sniffed == declared:
        return declared, ''
    if sniffed in COMPATIBLE.get(declared, ()):
        return declared, ''

    # 内容比扩展名更可信：txt/md 里藏着的 EPUB/PDF/ZIP 直接改判
    if declared in WEAK_TYPES and sniffed in STRONG_TYPES:
        return sniffed, ('扩展名为 %s，实际内容是 %s，已按实际格式入库'
                         % (ext or '（无）', type_label(sniffed)))

    # 其余情况保留扩展名判定，只提示内容不符，避免误伤能正常打开的文件
    return declared, ('扩展名 %s（%s）与文件内容（%s）不一致，请确认文件是否正常'
                      % (ext or '（无）', type_label(declared), type_label(sniffed)))


def importable_formats():
    """支持格式清单（后端单一来源，前端展示与校验都用它）。"""
    items = []
    for ext, kind, note in SUPPORTED_FORMATS:
        items.append({
            'ext': ext,
            'type': EXT_TYPE_MAP.get(ext, ext.lstrip('.')),
            'label': type_label(EXT_TYPE_MAP.get(ext, ext.lstrip('.'))),
            'kind': kind,
            'note': note,
        })
    return items


def accept_attribute():
    """拼出 <input accept="..."> 的值。"""
    return ','.join(ext for ext, kind, _ in SUPPORTED_FORMATS if kind == 'book') + ',' + \
           ','.join(ext for ext, kind, _ in SUPPORTED_FORMATS if kind == 'archive')
