# -*- coding: utf-8 -*-
"""书籍整理辅助（纯函数层）：文件名规范化 / 分册归组 / 拆分方案 / 番外识别。

刻意不引入 Flask 与数据库依赖：输入字符串，输出普通数据结构。
这样管理页可以拿真实书库数据做「只读预览」，也便于单元级验证，
不会在预览阶段误写任何数据或动到磁盘。
"""
import os
import re
from collections import defaultdict

CJK = re.compile(r'[\u4e00-\u9fff]')

# ── 文件名规范化 ────────────────────────────────────────────────────────────

# 成对括号里的「站点 / 来源 / 广告 / 状态」标记，
# 如 [小说下载网]、【www.abc.com】、（首发于某论坛）、（全本）
NOISE_BRACKET = re.compile(
    r'[\[【(（]\s*[^\[\]【】()（）]{0,40}?'
    r'(?:www\.|https?://|\.[a-z]{2,4}(?![\w])|网|站|论坛|书库|社区|书城|下载|首发|'
    r'转载|扫描|录入|校对|整理|电子书|免费阅读|txt|epub|'
    r'全本|完结|完本|已完结|校对版|精校版|未删减|无删减|无错版|典藏版|收藏版|精排)'
    r'[^\[\]【】()（）]{0,40}?\s*[\]】)）]', re.I)

# 裸网址
NOISE_URL = re.compile(r'(?:https?://|www\.)[^\s\]\)）】》」]{2,80}', re.I)

# 尾部冗余词（含被误当成书名一部分的扩展名）
NOISE_TAIL = re.compile(
    r'(?:[\s\-_—·．.]*'
    r'(?:txt|epub|pdf|mobi|azw3|docx?|rtf|全本|完结|完本|已完结|校对版|精校版|精排|'
    r'未删减|无删减|无错版|典藏版|收藏版|免费阅读|全文字|全本免费|下载|高清|排版)+)'
    r'\s*$', re.I)

# 编号前缀，如「001. 」「12、」「[3] 」
NUM_PREFIX = re.compile(r'^\s*(?:\[\s*\d{1,4}\s*\]|[(（]\s*\d{1,4}\s*[)）]|\d{1,4})\s*[.、_\-—·)\]]\s*')

LEAD_TRAIL = ' \t\u3000-_—·．.,、:：;；"\'“”'
# 左端孤立的右括号 / 右端孤立的左括号（成对出现的不动，避免把「书名（上）」的括号吃掉）
STRAY_HEAD = re.compile(r'^[\s\]）)】》」』>]+')
STRAY_TAIL = re.compile(r'[\s\[（(【《「『<]+$')
EXT_LIKE = re.compile(r'\.(?:txt|epub|pdf|mobi|azw3|md|html?|log|cn|docx?|rtf|djvu|fb2)$', re.I)

# 明显不可能是文件名的整串（保留字/系统保留名）
_RESERVED = {'', '.', '..', 'con', 'prn', 'aux', 'nul'}


def clean_stem(stem):
    """规范化书名主干（不含扩展名）。返回 (清理后的名字, 命中的整理原因列表)。"""
    s = (stem or '').strip()
    reasons = []
    if not s:
        return '', reasons

    n = NOISE_BRACKET.sub(' ', s)
    if n != s:
        s = n
        reasons.append('去掉括号噪声')

    n = NOISE_URL.sub(' ', s)
    if n != s:
        s = n
        reasons.append('去掉网址')

    n = NOISE_TAIL.sub('', s)
    if n != s:
        s = n
        reasons.append('去掉冗余尾缀')

    n = NUM_PREFIX.sub('', s)
    if n != s:
        s = n
        reasons.append('去掉编号前缀')

    if len(s) > 3 and s[0] in '《「『' and s[-1] in '》」』':
        s = s[1:-1]
        reasons.append('去掉书名号')

    # 分隔符与空白规范化：下划线/连续空格 → 单空格，去掉悬空的尾部连接符
    n = re.sub(r'[_\u3000\s]+', ' ', s)
    n = STRAY_HEAD.sub('', n)
    n = STRAY_TAIL.sub('', n)
    n = re.sub(r'\s*[-—–]{1,}\s*$', '', n)
    n = n.strip(LEAD_TRAIL)
    n = re.sub(r'\s{2,}', ' ', n).strip()
    # 噪声可能夹在书名号里（如《红楼梦》【全本】），清完再看一次是否整体被书名号包裹
    if len(n) > 3 and n[0] in '《「『' and n[-1] in '》」』':
        n = n[1:-1].strip(LEAD_TRAIL)
        reasons.append('去掉书名号')
    if n != s:
        s = n
        reasons.append('规范空白与分隔符')

    return s, reasons


def safe_stem(stem, limit=150):
    """把任意字符串收敛成可安全落盘的 Windows/Linux 通用文件名主干。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', str(stem or ''))
    s = re.sub(r'\s{2,}', ' ', s).strip().strip('. ')
    if s.lower() in _RESERVED:
        s = ''
    return s[:limit]


def suggest_filename(title, filename, file_type=''):
    """给一本书推荐规范化的磁盘文件名；无需变更时返回 None。

    目标名优先取用户维护过的 title（通常更干净），退回文件名主干。
    返回 {'old_name', 'new_name', 'reasons'}。
    """
    fn = os.path.basename((filename or '').strip())
    if not fn:
        return None
    stem, ext = os.path.splitext(fn)
    if not ext:
        ext = '.' + str(file_type or '').lstrip('.').lower() if file_type else ''
    if ext.lower() in ('.', ''):
        ext = ''

    src = EXT_LIKE.sub('', (title or '').strip())
    if not src:
        src = stem
    cleaned, reasons = clean_stem(src)
    cleaned = safe_stem(cleaned)
    if not cleaned:
        return None
    new_name = cleaned + ext
    if new_name == fn:
        return None
    # 只差大小写的重命名在大小写敏感的文件系统上有碰撞风险，收益却近乎为零 → 不提议
    if new_name.lower() == fn.lower():
        return None
    if not reasons:
        reasons = ['文件名与书名不一致']
    return {'old_name': fn, 'new_name': new_name, 'reasons': reasons}


# ── 分册 / 分章归组 ─────────────────────────────────────────────────────────

_CN_DIGITS = {'零': 0, '〇': 0, '一': 1, '壹': 1, '二': 2, '两': 2, '贰': 2, '三': 3,
              '叁': 3, '四': 4, '肆': 4, '五': 5, '伍': 5, '六': 6, '陆': 6, '七': 7,
              '柒': 7, '八': 8, '捌': 8, '九': 9, '玖': 9}
_CN_NUM_CHARS = ''.join(_CN_DIGITS) + '十拾'


def cn_to_int(text):
    """中文数字 → 整数（支持 1~99：一 / 十 / 十一 / 二十 / 二十三）。无法识别返回 None。"""
    t = (text or '').strip()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if not re.fullmatch('[' + re.escape(_CN_NUM_CHARS) + ']+', t):
        return None
    if re.search(r'[百佰千千万]', t):
        return None
    if '十' in t or '拾' in t:
        sep = '十' if '十' in t else '拾'
        left, _, right = t.partition(sep)
        tens = _CN_DIGITS.get(left, 1) if left else 1
        if left and left not in _CN_DIGITS:
            return None
        if right and right not in _CN_DIGITS:
            return None
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(t) == 1:
        return _CN_DIGITS.get(t)
    return None


_NUM = r'(?:[0-9０-９]{1,3}|[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾]{1,3})'

# A) 「（上）/（中）/（下）/（上册）/（下部）」这类方位分册
PAT_DIR = re.compile(
    r'^(?P<base>.+?)\s*'
    r'(?:[（(\[【]\s*(?P<d1>上上?|中|下下?|上[卷部篇册集季]|中[卷部篇册集季]|下[卷部篇册集季])\s*[）)\]】]'
    r'|[\s\-_—·]+(?P<d2>上[卷部篇册集季]?|中[卷部篇册集季]?|下[卷部篇册集季]?))$')

# B) 「第N册/部/卷/集/篇/章/回」，或「N册」带分隔/括号
PAT_NUM_UNIT = re.compile(
    r'^(?P<base>.+?)\s*'
    r'[（(\[【]?\s*(?:第\s*)?(?P<num>' + _NUM + r')\s*'
    r'(?P<unit>册|部|卷|集|篇|季|章|回|节)\s*[）)\]】]?$')

# C) 纯编号结尾（分章文件常见），如「书名-1」「书名(2)」「书名 003」
PAT_PLAIN_NUM = re.compile(
    r'^(?P<base>.+?)\s*'
    r'(?:[（(\[【]\s*(?P<n1>\d{1,3})\s*[）)\]】]'
    r'|[\s\-_—·]+(?P<n2>\d{1,3}))$')

_DIR_ORD = {'上': 1, '上上': 1, '中': 2, '下': 3, '下下': 3,
            '上卷': 1, '中卷': 2, '下卷': 3,
            '上部': 1, '中部': 2, '下部': 3,
            '上篇': 1, '中篇': 2, '下篇': 3,
            '上册': 1, '中册': 2, '下册': 3,
            '上集': 1, '中集': 2, '下集': 3,
            '上季': 1, '中季': 2, '下季': 3}


def series_of(title):
    """判断书名是否属于「分册/分章」系列。

    返回 {'base','order','kind','label'} 或 None。kind: volume（分册）/ chapter（分章）。
    """
    t = EXT_LIKE.sub('', (title or '').strip())
    if len(t) < 3:
        return None
    # 书名里带《》时先剥掉，否则尾部的 》会挡住匹配
    t = t.strip('《》「」')
    if len(t) < 3:
        return None

    m = PAT_DIR.match(t)
    if m:
        base = m.group('base').strip(LEAD_TRAIL)
        d = m.group('d1') or m.group('d2') or ''
        if len(base) >= 2 and d in _DIR_ORD:
            return {'base': base, 'order': _DIR_ORD[d], 'kind': 'volume', 'label': d}

    m = PAT_NUM_UNIT.match(t)
    if m:
        base = m.group('base').strip(LEAD_TRAIL)
        n = cn_to_int(m.group('num'))
        unit = m.group('unit')
        if len(base) >= 2 and n:
            return {'base': base, 'order': n,
                    'kind': 'chapter' if unit in ('章', '回', '节') else 'volume',
                    'label': unit + m.group('num')}

    m = PAT_PLAIN_NUM.match(t)
    if m:
        base = m.group('base').strip(LEAD_TRAIL)
        raw = m.group('n1') or m.group('n2') or ''
        if len(base) >= 2 and raw:
            # 纯数字结尾误判率相对高（「人类简史 2」也可能真是续作），
            # 因此只归到 chapter 类且必须成组（>=2 本）才会出现在预览里，交由人工确认
            return {'base': base, 'order': int(raw), 'kind': 'chapter', 'label': raw}

    return None


def group_series(rows, min_size=2, max_groups=80, max_books_per_group=40):
    """把书籍列表聚成「同系列」分组。

    rows: 可迭代的 {'id','title','filename','file_type'} 字典。
    同系列 = base 归一化（去空白 + 大小写折叠）后相同；每组按 order 升序。
    """
    buckets = defaultdict(list)
    for r in rows:
        name = (r.get('title') or '').strip()
        if not name:
            name = os.path.splitext(r.get('filename') or '')[0]
        info = series_of(name)
        if not info:
            continue
        key = (info['kind'], re.sub(r'\s+', '', info['base']).casefold())
        buckets[key].append({'id': r.get('id'), 'title': name,
                             'filename': r.get('filename') or '',
                             'file_type': r.get('file_type') or '',
                             'order': info['order'], 'label': info['label'],
                             'base': info['base']})

    groups = []
    for (kind, _key), members in buckets.items():
        if len(members) < min_size:
            continue
        members.sort(key=lambda m: (m['order'], m['id'] or 0))
        groups.append({
            'base': members[0]['base'],
            'kind': kind,
            'count': len(members),
            'orders': [m['order'] for m in members],
            'labels': [m['label'] for m in members],
            'books': members[:max_books_per_group],
        })
    # 顺序完全一致（同一编号出现多次）说明更可能是真重复书而非分册，排在后面
    groups.sort(key=lambda g: (-g['count'], len(set(g['orders'])) != g['count'], g['base']))
    return groups[:max_groups]


# ── 拆分方案 ────────────────────────────────────────────────────────────────

CHAP_RE = re.compile(
    r'^[ \t\u3000]{0,6}(?:'
    r'第\s*[0-9０-９零〇一二三四五六七八九十百千两]{1,12}\s*[章回节卷篇部集]'
    r'|Chapter\s*\d{1,4}|CHAPTER\s*\d{1,4}'
    r'|序章|楔子|引子|尾声|终章|后记|番外'
    r')[^\n]{0,60}$', re.M)


def plan_split(text, mode='chapter', parts=2):
    """给出切分方案（不写盘）：list of {'index','start','end','chars','hint'}。

    mode='chapter' 按章节标题切（章节不足时自动退回等长切）；
    mode='size'    按大致等长切，且只在换行处切，保证不切断行。
    """
    text = text or ''
    n = len(text)
    if n < 2000:
        return []
    try:
        parts = int(parts)
    except (TypeError, ValueError):
        parts = 2
    parts = max(2, min(parts, 50))

    cuts = [0]
    if mode == 'chapter':
        marks = [m.start() for m in CHAP_RE.finditer(text)]
        marks = [p for p in marks if p > 0]
        marks = sorted(set(marks))
        if len(marks) < parts:
            return plan_split(text, 'size', parts)
        step = max(1, len(marks) // parts)
        for i in range(1, parts):
            p = marks[min(i * step, len(marks) - 1)]
            if p > cuts[-1]:
                cuts.append(p)
    else:
        target = max(1, n // parts)
        for i in range(1, parts):
            want = target * i
            if want >= n:
                break
            nl = text.find('\n', want)
            cut = n if nl < 0 else nl + 1
            if cut > cuts[-1]:
                cuts.append(cut)
    cuts.append(n)

    out = []
    for i in range(len(cuts) - 1):
        a, b = cuts[i], cuts[i + 1]
        if b <= a:
            continue
        head = ''
        for line in text[a:b].split('\n', 8)[:8]:
            line = line.strip()
            if line:
                head = line[:60]
                break
        out.append({'index': len(out) + 1, 'start': a, 'end': b,
                    'chars': b - a, 'hint': head})
    return out if len(out) >= 2 else []


# ── 番外 / 衍生内容识别 ─────────────────────────────────────────────────────

EXTRA_RULES = (
    ('番外', re.compile(r'番外|外传|外篇|别传|续篇|外章')),
    ('前传', re.compile(r'前传|前篇|前作')),
    ('后传', re.compile(r'后传|后篇|终章|完结篇|大结局')),
    ('短篇', re.compile(r'短篇|短篇集|小短篇')),
    ('合集', re.compile(r'合集|文集|全集|套装|合辑|作品集')),
    ('设定集', re.compile(r'设定集|资料集|人物志|图鉴|百科|附录|大辞典')),
    ('同人', re.compile(r'同人|番外同人')),
)


def extra_tags(title, filename=''):
    """按书名/文件名给出建议标签（只读，不写库）。"""
    text = '%s %s' % (title or '', os.path.splitext(filename or '')[0])
    if not text.strip():
        return []
    tags = []
    for tag, pat in EXTRA_RULES:
        if pat.search(text):
            tags.append(tag)
    return tags
