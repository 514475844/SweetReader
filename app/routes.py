from flask import Blueprint, render_template, request, jsonify, send_file, redirect, url_for, flash, current_app, make_response, session
from flask_login import login_user, logout_user, login_required, current_user
from pathlib import Path
from datetime import datetime, timedelta, date
import ast
import io
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
import hashlib
import mimetypes
import zipfile

from app.models import (db, User, InviteCode, Book, Category, ReadingProgress,
                        UserTheme, Bookmark, log_action, LoginEvent)
from app.utils import BookUtils
from app.category_scanner import CategoryScanner
from app.plugins import registry
from app import broken_files
from app import book_tools
from app import file_types

bp = Blueprint('main', __name__)


ONLINE_WINDOW_MINUTES = 10   # 10 分钟内有活动即视为在线


@bp.before_app_request
def _touch_user_activity():
    """记录在线状态：每 60 秒最多更新一次，避免每个请求都写库。"""
    # 静态资源请求不参与在线统计，避免每个 css/js/封面 都触发一次用户查询
    if request.endpoint and request.endpoint.startswith('static'):
        return
    if not current_user.is_authenticated:
        return
    now = datetime.utcnow()
    last = getattr(current_user, 'last_active', None)
    if last and (now - last).total_seconds() < 60:
        return
    try:
        current_user.last_active = now
        db.session.commit()
    except Exception:
        db.session.rollback()


# ============ 内置编码检测 ============
# 编码探测只看文件开头的一段：判断编码用不了整本书，
# 而「整文件 × 8 种编码各解一遍」是大 txt 打开巨慢的根因（实测 20MB 文件要十几秒）。
DETECT_SAMPLE_BYTES = 262144        # 256KB 取样
DECODE_CACHE_MAX_ITEMS = 12         # 缓存条目上限
DECODE_CACHE_MAX_CHARS = 6_000_000  # 单条缓存最大字符数（约 12MB 内存）
_decode_cache = {}                  # key -> (text)
_decode_cache_order = []


def _decode_strict_or_none(raw, enc):
    """严格解码；取样截断可能切断多字节序列，允许裁掉末尾 1~3 字节后重试。"""
    for trim in range(0, 4):
        chunk = raw[:len(raw) - trim] if trim else raw
        try:
            return chunk.decode(enc)
        except UnicodeDecodeError:
            continue
        except Exception:
            return None
    return None


def detect_encoding(raw_bytes):
    # BOM 优先判定
    if raw_bytes.startswith(b'\xef\xbb\xbf'):
        return 'utf-8-sig'
    if raw_bytes.startswith(b'\xff\xfe'):
        return 'utf-16-le'
    if raw_bytes.startswith(b'\xfe\xff'):
        return 'utf-16-be'
    # 对所有候选编码各解一次再打分：
    #   1) 替换符(U+FFFD)越少越好；
    #   2) 在都「无替换符」时，按中文(CJK)字符数量判定——
    #      把 GBK 误当 UTF-8 解出的文本几乎不含真中文，GBK 正确解码的中文很多，
    #      故优先选 CJK 最多的解码，避免中文书变乱码。
    candidates = ['utf-8', 'gb18030', 'gbk', 'big5', 'big5hkscs',
                   'gb2312', 'shift-jis', 'euc-kr']
    sample = (raw_bytes[:DETECT_SAMPLE_BYTES]
              if len(raw_bytes) > DETECT_SAMPLE_BYTES else raw_bytes)
    decoded = {}
    for enc in candidates:
        t = _decode_strict_or_none(sample, enc)
        if t is not None:
            decoded[enc] = t
    if decoded:
        def _score(s):
            # CJK 计数只取前 5k 字采样，避免逐字符遍历大文件拖慢请求
            return (-s.count('\ufffd'),
                    sum(1 for ch in s[:5000] if '\u4e00' <= ch <= '\u9fff'))
        return max(decoded, key=lambda e: _score(decoded[e]))
    # 没有任何编码能「零错误」解开时，绝不能回退 utf-8 + errors=replace
    # （会吐出海量替换符→满屏乱码）。改为 best-effort：对每个候选硬解，
    # 选替换符最少的解码（中文书几乎总能由此正确还原）。
    best_enc, best_fffd, best_cjk = 'utf-8', None, -1
    for enc in candidates:
        try:
            t = sample.decode(enc, errors='replace')
        except Exception:
            continue
        fffd = t.count('\ufffd')
        cjk = sum(1 for ch in t[:5000] if '\u4e00' <= ch <= '\u9fff')
        if best_fffd is None or fffd < best_fffd or (fffd == best_fffd and cjk > best_cjk):
            best_fffd, best_cjk, best_enc = fffd, cjk, enc
    return best_enc


def decode_book_text(file_path):
    """读文件并按探测到的编码解码，只解一次；结果进一个小缓存，重复打开秒开。"""
    st = os.stat(file_path)
    key = (str(file_path), st.st_mtime, st.st_size)
    hit = _decode_cache.get(key)
    if hit is not None:
        return hit

    with open(file_path, 'rb') as f:
        raw = f.read()
    raw = fix_bom(raw)
    encoding = detect_encoding(raw)
    try:
        text = raw.decode(encoding)
    except Exception:
        text = raw.decode(encoding, errors='replace')

    if len(text) <= DECODE_CACHE_MAX_CHARS:
        _decode_cache[key] = text
        _decode_cache_order.append(key)
        while len(_decode_cache_order) > DECODE_CACHE_MAX_ITEMS:
            _decode_cache.pop(_decode_cache_order.pop(0), None)
    return text

def fix_bom(raw_bytes):
    if raw_bytes.startswith(b'\xef\xbb\xbf'):
        return raw_bytes[3:]
    return raw_bytes


# 系统保留用户名：注册/建号/改名时禁用（含子串匹配，覆盖 admin/root/管理员 等）
_FORBIDDEN_USERNAME_TOKENS = ['admin', 'root', '管理员', '超级管理员', '系统管理员',
                              'superuser', 'moderator', 'support', 'official', 'bot',
                              'guest', 'test', 'system', 'sudo', 'owner', '官方']
def is_forbidden_username(name):
    n = (name or '').strip().lower()
    if not n:
        return False
    return any(tok in n for tok in _FORBIDDEN_USERNAME_TOKENS)

def password_error(password):
    """密码强度校验：至少7位，含大小写字母、数字与特殊符号。合法返回 None，否则返回错误文案。"""
    if not password or len(password) < 7:
        return '密码至少需要7位'
    if not re.search(r'[A-Z]', password):
        return '密码需包含大写字母'
    if not re.search(r'[a-z]', password):
        return '密码需包含小写字母'
    if not re.search(r'[0-9]', password):
        return '密码需包含数字'
    if not re.search(r'[^A-Za-z0-9]', password):
        return '密码需包含特殊符号'
    return None

# ============ 子管理员功能白名单校验 ============
def _require_cap(cap):
    """管理员恒通过；子管理员需对应能力位开启；其余拒绝。"""
    if current_user.is_admin:
        return True
    if not current_user.is_sub_admin:
        return False
    if cap == 'import' and current_user.can_import:
        return True
    if cap == 'export' and current_user.can_export:
        return True
    if cap == 'manage_books' and current_user.can_manage_books:
        return True
    return False


def _hidden_plugins_of(user):
    """取该用户在首页隐藏的扩展卡片插件 id 集合。

    存储格式为 ',a,b,'（逗号包裹），避免 'stats' 命中 'stats_x' 这类前缀误判。
    """
    try:
        theme = UserTheme.query.filter_by(user_id=user.id).first()
    except Exception:
        return set()
    if not theme or not theme.hidden_plugins:
        return set()
    return {p for p in str(theme.hidden_plugins).split(',') if p}


def _set_hidden_plugins(user, ids):
    """写回用户隐藏的首页插件 id 集合（去重、限长、排序后逗号包裹存储）。"""
    clean = sorted({str(i).strip() for i in (ids or []) if str(i).strip()})
    theme = UserTheme.query.filter_by(user_id=user.id).first()
    if not theme:
        theme = UserTheme(user_id=user.id)
        db.session.add(theme)
    theme.hidden_plugins = (',' + ','.join(clean) + ',') if clean else ''
    db.session.commit()
    return clean


# ============ 首页 ============
@bp.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('main.library'))
    return redirect(url_for('main.login'))

@bp.route('/library')
@login_required
def library():
    books = Book.query.order_by(Book.upload_date.desc()).limit(9).all()
    # 只取一级分类做扁平列表（约定：不做树形分类）。
    # 原先模板用 {% for ... recursive %} 把全部分类整棵渲染进页面，
    # 首屏因此要 7~11 秒。改成一级列表后由 /category/<path> 逐级下钻。
    root = Category.query.filter_by(parent_id=None).first()
    if root:
        top_cats = Category.query.filter_by(parent_id=root.id) \
                             .order_by(Category.book_count.desc()).all()
    else:
        top_cats = Category.query.filter_by(level=1) \
                             .order_by(Category.book_count.desc()).all()
    # 只取一级分类做扁平列表（约定：不做树形分类）。
    # 原先模板用 {% for ... recursive %} 把全部分类整棵渲染进页面，
    # 首屏因此要 7~11 秒。改成一级列表后由 /category/<path> 逐级下钻。
    # 过滤噪声：磁盘目录名已损坏（含 U+FFFD 替换符，原中文不可恢复）的分类，
    # 以及误把正文章节名当成分类、书籍数极少（≤19 本）的垃圾项，
    # 否则侧栏 / 常用分类会被上百个乱码分类刷屏（实测 174 个一级分类里只有 4 个有效）。
    _MIN_CAT_BOOKS = 20

    def _cat_valid(c, level):
        nm = (c.name or '').strip()
        if not nm or '\ufffd' in nm:
            return False
        # 顶层额外隐藏书籍数过少的章节名误建类；深层子目录按文件夹名如实展示
        if level <= 1 and (c.book_count or 0) < _MIN_CAT_BOOKS:
            return False
        return True

    categories = [c for c in top_cats if _cat_valid(c, 1)]
    # 目录树：顶层节点一次性给出（带 has_children / valid 标记），
    # 子节点由 /api/categories/<id>/children 懒加载，避免整棵树渲染拖垮首屏。
    child_counts = dict(db.session.query(Category.parent_id, db.func.count(Category.id))
                        .group_by(Category.parent_id).all())
    cat_top = [{'id': c.id, 'name': c.name, 'path': c.path,
                'book_count': c.book_count or 0, 'level': 1,
                'has_children': child_counts.get(c.id, 0) > 0,
                'valid': _cat_valid(c, 1)} for c in top_cats]
    total_books = Book.query.count()
    total_categories = Category.query.count()
    # 首页扩展卡片：插件侧的总开关（管理员在插件管理页控制）之外，
    # 再叠加「用户级隐藏」（设置页可配）——用户只能让卡片更少，不能越过管理员启用的插件。
    hidden_plugins = _hidden_plugins_of(current_user)
    home_entries = [e for e in registry.homepage_entries()
                    if e['plugin_id'] not in hidden_plugins]
    return render_template('index.html',
                         books=books, categories=categories, cat_top=cat_top,
                         total_books=total_books, total_categories=total_categories,
                         plugins=home_entries,
                         is_admin=current_user.is_admin,
                         can_manage_books=_require_cap('manage_books'))

# ============ 登录/注册 ============
@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.library'))
    if request.method == 'POST':
        login_input = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter(db.or_(User.username == login_input, User.email == login_input)).first()
        # 已停用账号（含被弱口令清理停用的）必须挡在门外，
        # 否则 is_active 只是个摆设，停用形同虚设
        if user and user.is_active is False:
            flash('该账号已被停用', 'error')
            return render_template('login.html')
        if user and user.check_password(password):
            remember = bool(request.form.get('remember'))
            login_user(user, remember=remember)
            session.permanent = True
            try:
                user.last_login = datetime.utcnow()
                db.session.commit()
            except Exception:
                db.session.rollback()
            try:
                db.session.add(LoginEvent(
                    user_id=user.id, ts=datetime.utcnow(),
                    ip=request.remote_addr or '',
                    ua=(request.user_agent.string if request.user_agent else '')[:500],
                    success=True))
                db.session.commit()
            except Exception:
                db.session.rollback()
            return redirect(url_for('main.library'))
        if user and user.is_active is not False:
            try:
                db.session.add(LoginEvent(
                    user_id=user.id, ts=datetime.utcnow(),
                    ip=request.remote_addr or '',
                    ua=(request.user_agent.string if request.user_agent else '')[:500],
                    success=False))
                db.session.commit()
            except Exception:
                db.session.rollback()
        flash('用户名/邮箱或密码错误', 'error')
    return render_template('login.html')

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.library'))
    if request.method == 'POST':
        invite_code = request.form.get('invite_code', '').strip().upper()
        email = request.form.get('email', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        invite = InviteCode.query.filter_by(code=invite_code, is_used=False).first()
        if not invite:
            flash('无效的邀请码', 'error')
            return render_template('register.html')
        if invite.target_email != email:
            flash('邀请码绑定的邮箱不匹配', 'error')
            return render_template('register.html')
        if is_forbidden_username(username):
            flash('该用户名不可用，请换一个', 'error')
            return render_template('register.html')
        if User.query.filter_by(username=username).first():
            flash('用户名已被使用', 'error')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('该邮箱已注册', 'error')
            return render_template('register.html')
        if password != confirm_password:
            flash('两次输入的密码不一致', 'error')
            return render_template('register.html')
        pw_error = password_error(password)
        if pw_error:
            flash(pw_error, 'error')
            return render_template('register.html')
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        invite.is_used = True
        invite.used_by = user.id
        db.session.commit()
        flash('注册成功！请登录', 'success')
        return redirect(url_for('main.login'))
    return render_template('register.html')

@bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.login'))

# ============ API ============
def _file_type_variants(value):
    """格式筛选的大小写变体。

    库里主流存大写（TXT / EPUB），但存在个别小写残留行（file_type='txt'），
    直接等值比较会让这些书在任何格式筛选下都消失。
    """
    v = (value or '').strip()
    if not v:
        return []
    return list(dict.fromkeys([v, v.upper(), v.lower(), v.capitalize()]))


def _descendant_category_ids(root):
    """一次查询取回全部分类关系，在内存里展开 root 的整棵子树（含自己）。

    原实现只遍历 `root.children`（直接子分类，且每层各发一次查询），
    实测最深有 11 层分类、29 个分类带孙辈，挂在它们下面的 2349 本书会被漏掉。
    """
    if root is None:
        return []
    rows = db.session.query(Category.id, Category.parent_id).all()
    kids = {}
    for cid, pid in rows:
        kids.setdefault(pid, []).append(cid)
    out, stack, seen = [], [root.id], set()
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
        stack.extend(kids.get(cid, []))
    return out


@bp.route('/api/books')
@login_required
def api_books():
    """书籍列表（分页）。

    原实现 `Book.query.all()` 一次吐出全库（数据量大时响应极慢、JSON 体积与客户端解析开销都很高，
    客户端解析同样吃内存）。改为分页：默认 50 条、上限 200 条；
    返回结构仍是数组（旧调用方不受影响），总数与分页信息放在响应头。
    """
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get('per_page', 50))
    except (TypeError, ValueError):
        per_page = 50
    per_page = max(1, min(200, per_page))

    fmt = (request.args.get('format') or '').strip()
    keyword = (request.args.get('q') or request.args.get('search') or '').strip()

    query = Book.query
    if fmt:
        query = query.filter(Book.file_type.in_(_file_type_variants(fmt)))
    if keyword:
        like = '%%%s%%' % keyword
        query = query.filter(db.or_(Book.title.ilike(like), Book.author.ilike(like)))

    total = query.count()
    books = (query.order_by(Book.title.asc(), Book.id.asc())
             .offset((page - 1) * per_page).limit(per_page).all())
    resp = jsonify([{
        'id': b.id,
        'title': b.title or b.filename,
        'author': b.author or '',
        'file_type': b.file_type or '未知',
        'file_size_str': BookUtils.format_file_size(b.file_size) if b.file_size else '0 B'
    } for b in books])
    resp.headers['X-Total-Count'] = str(total)
    resp.headers['X-Page'] = str(page)
    resp.headers['X-Per-Page'] = str(per_page)
    return resp


@bp.route('/api/all-books')
@login_required
def api_all_books():
    """按标题首字符聚合计数（首页字母筛选按钮用），SQL 层一次算完，避免全量导出 13MB JSON。"""
    # 归类键在入库时已算好（中文按拼音首字母），直接分组，书籍再多也不卡
    rows = db.session.execute(db.text(
        "SELECT COALESCE(initial, 'Other') AS k, COUNT(*) AS n "
        "FROM book GROUP BY k"
    )).fetchall()
    return jsonify({k: n for k, n in rows})

@bp.route('/api/categories')
@login_required
def api_categories():
    mode = request.args.get('mode', 'tree')
    # 一次性取回全部分类，在内存里建树 / 拼全路径。
    # 原实现 flat 模式逐个调 get_full_path()（沿 parent 链一路查库）、tree 模式逐个取
    # cat.children，2028 个分类要发上千次查询 —— 实测 tree 模式 7.2s。
    rows = db.session.query(
        Category.id, Category.name, Category.path, Category.parent_id,
        Category.level, Category.book_count, Category.sort_order
    ).all()
    by_id = {r.id: r for r in rows}
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r.parent_id, []).append(r)

    def _order(rs):
        return sorted(rs, key=lambda r: (r.sort_order if r.sort_order is not None else 0, r.id))

    def _full_path(cid):
        parts, cur, guard = [], cid, 0
        while cur is not None and guard < 128:
            row = by_id.get(cur)
            if row is None:
                break
            parts.append(row.name)
            cur = row.parent_id
            guard += 1
        return '/'.join(reversed(parts))

    if mode == 'flat':
        return jsonify([{
            'id': r.id,
            'name': r.name,
            'full_path': _full_path(r.id),
            'level': r.level,
            'book_count': r.book_count,
            'path': r.path
        } for r in sorted(rows, key=lambda r: (r.path or ''))])

    def _node(r):
        return {
            'id': r.id,
            'name': r.name,
            'path': r.path,
            'level': r.level,
            'book_count': r.book_count,
            'children': [_node(c) for c in _order(by_parent.get(r.id, []))]
        }

    return jsonify([_node(r) for r in _order(by_parent.get(None, []))])


@bp.route('/api/categories/<int:parent_id>/children')
@login_required
def api_category_children(parent_id):
    """逐级懒加载子分类，供首页左侧可展开目录树使用（避免一次性渲染全部分类）。"""
    parent = Category.query.get_or_404(parent_id)
    kids = Category.query.filter_by(parent_id=parent.id) \
                         .order_by(Category.book_count.desc()).all()
    return jsonify([{
        'id': c.id,
        'name': c.name,
        'path': c.path,
        'book_count': c.book_count,
        'has_children': bool(c.children),
    } for c in kids])

# ============ 阅读 ============
@bp.route('/read/<int:book_id>')
@login_required
def read_book(book_id):
    book = Book.query.get_or_404(book_id)
    book.last_read = datetime.utcnow()
    book.read_count = (book.read_count or 0) + 1
    db.session.commit()
    return render_template('reader.html', book=book,
                           plugin_reader_tools=registry.reader_tools_entries())

@bp.route('/epub-reader')
@login_required
def epub_reader():
    """EPUB 在线阅读器页面（epub.js 前端渲染）。"""
    book_id = request.args.get('id', type=int)
    if not book_id:
        return redirect(url_for('main.library'))
    book = Book.query.get_or_404(book_id)
    if not (book.filename or '').lower().endswith('.epub'):
        flash('该文件不是 EPUB 格式', 'error')
        return redirect(url_for('main.library'))
    resp = make_response(render_template('epub_reader.html', book=book))
    # 禁止缓存阅读器页面，避免浏览器复用修复前缓存的旧页面/旧库
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@bp.route('/pdf-reader')
@login_required
def pdf_reader():
    """PDF 深度阅读器页面（pdf.js 前端渲染：目录大纲 / 页码进度 / 缩放 / 双页）。"""
    book_id = request.args.get('id', type=int)
    if not book_id:
        return redirect(url_for('main.library'))
    book = Book.query.get_or_404(book_id)
    ft = (book.file_type or '').lower()
    if ft != 'pdf' and not (book.filename or '').lower().endswith('.pdf'):
        flash('该文件不是 PDF 格式', 'error')
        return redirect(url_for('main.library'))
    resp = make_response(render_template('pdf_reader.html', book=book))
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Pragma'] = 'no-cache'
    return resp


def _doc_esc(t):
    if not t:
        return ''
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _docx_to_html(path):
    """用已安装的 python-docx 把 .docx 转成带标题/加粗/列表/表格的 HTML。
    不引入新依赖；格式还原够阅读用（mammoth 可后续升级保真度）。"""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d = Document(path)

    def heading_level(style_name):
        if not style_name:
            return 0
        sn = style_name.lower()
        if sn.startswith('title'):
            return 1
        if sn.startswith('heading'):
            digits = ''.join(ch for ch in sn if ch.isdigit())
            try:
                return int(digits) if digits else 1
            except Exception:
                return 1
        return 0

    def run_span(run):
        t = _doc_esc(run.text or '')
        if not t:
            return ''
        if run.bold:
            t = '<strong>' + t + '</strong>'
        if run.italic:
            t = '<em>' + t + '</em>'
        if run.underline:
            t = '<u>' + t + '</u>'
        return t

    def cell_text(cell):
        return _doc_esc('\n'.join(p.text or '' for p in cell.paragraphs))

    body = d.element.body
    parts = []
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            p = Paragraph(child, d)
            txt = ''.join(run_span(r) for r in p.runs)
            style = (p.style.name if p.style else '') or ''
            lvl = heading_level(style)
            sn = style.lower()
            if lvl:
                parts.append('<h{0}>{1}</h{0}>'.format(min(lvl, 3), txt or '&nbsp;'))
            elif sn.startswith('list bullet') or sn.startswith('list number'):
                parts.append('<ul><li>' + (txt or '&nbsp;') + '</li></ul>')
            elif txt.strip() == '':
                parts.append('<p>&nbsp;</p>')
            else:
                parts.append('<p>' + txt + '</p>')
        elif isinstance(child, CT_Tbl):
            tbl = Table(child, d)
            rows = []
            for row in tbl.rows:
                cells = ''.join('<td>' + cell_text(c) + '</td>' for c in row.cells)
                rows.append('<tr>' + cells + '</tr>')
            parts.append('<table>' + ''.join(rows) + '</table>')
    return ''.join(parts)


def _doc_reader_html(inner_html, book_id):
    """把转换出的正文包成带阅读器主题/设置穿透/进度回写的独立 HTML 页，
    供 reader.html 的 iframe（data-embed=doc）加载。"""
    page = (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>'
        ':root{--bg:#f5f0eb;--fg:#2a2a2a;--link:#8a6db8;--fs:17px;--ls:2.0;--bright:1}'
        'html,body{margin:0;background:var(--bg);color:var(--fg)}'
        '.sr-doc{max-width:820px;margin:0 auto;padding:22px 26px 140px;font-size:var(--fs);'
        'line-height:var(--ls);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",'
        '"Microsoft YaHei",serif;filter:brightness(var(--bright))}'
        '.sr-doc h1,.sr-doc h2,.sr-doc h3{line-height:1.35;margin:1.1em 0 .5em;font-weight:600}'
        '.sr-doc p{margin:0 0 .9em;text-align:justify}'
        '.sr-doc img{max-width:100%;height:auto}'
        '.sr-doc table{border-collapse:collapse;width:100%;margin:1em 0}'
        '.sr-doc td,.sr-doc th{border:1px solid #cbb;padding:6px 9px;font-size:.92em}'
        '.sr-doc a{color:var(--link)}'
        '.sr-doc ul,.sr-doc ol{padding-left:1.7em;margin:.4em 0 1em}'
        '.sr-doc li{margin:.2em 0}'
        '</style></head><body><div class="sr-doc" id="srDoc">__INNER__</div>'
        '<script>'
        '(function(){var root=document.documentElement;'
        'function applyTheme(t){var bg="#f5f0eb",fg="#2a2a2a",lk="#8a6db8";'
        'if(t==="dark"){bg="#1e1e1e";fg="#d8d8d8";lk="#b89ad8"}'
        'else if(t==="sepia"||t==="eye"){bg="#f5ecd9";fg="#5b4a32";lk="#9c6b3f"}'
        'root.style.setProperty("--bg",bg);root.style.setProperty("--fg",fg);'
        'root.style.setProperty("--link",lk);}'
        'function apply(s){if(!s)return;'
        'if(s.font_size)root.style.setProperty("--fs",s.font_size+"px");'
        'if(s.line_spacing)root.style.setProperty("--ls",s.line_spacing);'
        'if(s.brightness!=null)root.style.setProperty("--bright",s.brightness);'
        'if(s.theme)applyTheme(s.theme);}'
        'window.addEventListener("message",function(e){'
        'if(e.data&&e.data.type==="sr-embed-settings")apply(e.data);});'
        'try{parent.postMessage({type:"sr-embed-ready"},"*");}catch(_){}'
        'var bid=__BID__,last=0,tm=null;'
        'window.addEventListener("scroll",function(){clearTimeout(tm);tm=setTimeout(function(){'
        'var h=document.documentElement.scrollHeight-window.innerHeight;'
        'var p=h>0?Math.min(100,Math.round(window.scrollY/h*100)):0;'
        'if(p-last>=2){last=p;try{fetch("/api/progress/"+bid,{method:"POST",credentials:"same-origin",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify({progress:p/100,location:p})})'
        '.catch(function(){});}catch(_){}}};400);});'
        '})();'
        '</script></body></html>'
    )
    return page.replace('__INNER__', inner_html).replace('__BID__', str(book_id))


def _doc_fallback_html(book_id):
    return _doc_reader_html(
        '<p style="color:#a33">暂不支持旧版 .doc 格式。请用 Word / WPS 将文件另存为 '
        '.docx 后重新导入，即可正常阅读。</p>', book_id)

@bp.route('/api/read/<int:book_id>')
@login_required
def get_book_content(book_id):
    book = Book.query.get_or_404(book_id)
    file_path = Path(current_app.config['BOOKS_DIR']) / book.relative_path
    if not file_path.exists():
        file_path = Path(current_app.config['BOOKS_DIR']) / book.filename
    if not file_path.exists():
        return jsonify({'error': '文件不存在'}), 404
    ext = file_path.suffix.lower()
    # 二进制格式：直接返回原始文件，交给专用阅读器 / 下载
    BINARY_EXTS = {'.epub', '.pdf', '.mobi', '.azw', '.azw3', '.doc', '.docx',
                   '.ppt', '.pptx', '.xls', '.xlsx', '.zip', '.rar', '.7z',
                   '.cbz', '.cbr', '.djvu', '.mp3', '.jpg', '.jpeg', '.png',
                   '.gif', '.webp', '.bmp', '.tiff'}
    if ext == '.docx':
        try:
            return Response(_doc_reader_html(_docx_to_html(file_path), book_id),
                            mimetype='text/html; charset=utf-8')
        except Exception:
            pass
    if ext == '.doc':
        return Response(_doc_fallback_html(book_id), mimetype='text/html; charset=utf-8')
    if ext in BINARY_EXTS:
        return send_file(file_path)
    # 其余（含 .txt 及其它纯文本扩展名，如 .text/.cn/.log/.md 等）
    # 一律按文本解码后返回 UTF-8，避免 GBK 等中文编码被前端当 UTF-8 硬解成乱码
    try:
        text = decode_book_text(str(file_path))
        # 解码出大量替换符 => 实为二进制文件，退回原始文件
        if text.count('\ufffd') > max(5, len(text) // 50):
            return send_file(file_path)
        from flask import Response
        resp = Response(text, mimetype='text/plain; charset=utf-8')
        resp.headers['Cache-Control'] = 'private, max-age=300'
        return resp
    except Exception as e:
        return jsonify({'error': f'读取失败: {str(e)}'}), 500

# ============ 分类 ============
@bp.route('/categories')
@login_required
def categories_page():
    mode = request.args.get('mode', 'tree')
    return render_template('categories.html', mode=mode)

@bp.route('/category/<path:category_path>')
@login_required
def category_view(category_path):
    category = Category.query.filter_by(path=category_path).first_or_404()

    #  分类内搜索 + 分页。
    # 原来这里直接 .all() 把整个分类的书一次性捞出来渲染，大分类（几千本）会拖垮页面；
    # 改成服务端分页，每页 60 本，并支持按书名/作者筛选。
    q = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    if page < 1:
        page = 1
    per_page = 60
    query = Book.query.filter_by(category_id=category.id)
    if q:
        query = query.filter(db.or_(Book.title.ilike('%' + q + '%'),
                                    Book.author.ilike('%' + q + '%')))
    total = query.count()
    books = (query.order_by(Book.title.asc())
             .offset((page - 1) * per_page).limit(per_page).all())

    breadcrumb = []
    current = category
    while current:
        breadcrumb.insert(0, {'name': current.name, 'path': current.path})
        current = current.parent
    return render_template('category.html', category=category, books=books,
                           breadcrumb=breadcrumb, q=q, page=page,
                           per_page=per_page, total=total)

# ============ 管理员 ============

# [B3] 扫描任务状态：全量扫描可能耗时很久，绝不能放在同步请求里。
# 改为后台线程跑，前端轮询 /api/scan-progress 看进度。
scan_state = {
    'running': False,
    'phase': 'idle',          # idle / running / done / error
    'started_at': None,
    'finished_at': None,
    'error': None,
    'result': {},             # 与扫描器共享同一 dict，实时更新
}
_scan_lock = threading.Lock()


def _run_scan(app, force, incremental):
    with app.app_context():
        try:
            scan_state['phase'] = 'running'
            CategoryScanner.scan_and_sync(force=force, incremental=incremental,
                                          result_out=scan_state['result'])
            scan_state['phase'] = 'done'
        except Exception as e:
            scan_state['error'] = f'{type(e).__name__}: {e}'
            scan_state['phase'] = 'error'
            try:
                db.session.rollback()
            except Exception:
                pass
        finally:
            scan_state['running'] = False
            scan_state['finished_at'] = datetime.now().isoformat(timespec='seconds')


@bp.route('/api/scan-progress')
@login_required
def scan_progress():
    """[B3] 扫描进度轮询接口。"""
    r = scan_state['result'] or {}
    scanned = r.get('categories_scanned', 0)
    estimate = r.get('categories_estimate', 0) or 0
    pct = 0
    if scan_state['phase'] == 'done':
        pct = 100
    elif estimate and scanned:
        pct = min(99, int(scanned * 100 / estimate))
    started = scan_state['started_at']
    elapsed = None
    if started:
        try:
            elapsed = round((datetime.now() - datetime.fromisoformat(started)).total_seconds())
        except Exception:
            elapsed = None
    return jsonify({
        'running': scan_state['running'],
        'phase': scan_state['phase'],
        'percent': pct,
        'elapsed': elapsed,
        'started_at': started,
        'finished_at': scan_state['finished_at'],
        'error': scan_state['error'],
        'categories_scanned': scanned,
        'categories_estimate': estimate,
        'books_updated': r.get('books_updated', 0),
        'books_skipped': r.get('books_skipped', 0),
        'categories_added': r.get('categories_added', 0),
        'categories_removed': r.get('categories_removed', 0),
    })


@bp.route('/admin/sync', methods=['GET', 'POST'])
@login_required
def sync_categories():
    """[B3] 启动后台扫描，立即返回（不再阻塞 20 分钟）。

    [B8] 恢复 GET 支持：浏览器直接敲 URL 也能触发，
    前端两处调用都是 POST，两者行为一致。
    """
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403

    with _scan_lock:
        if scan_state['running']:
            return jsonify({
                'success': False,
                'running': True,
                'message': '扫描已在运行中，请等待完成',
                'percent': 0,
            }), 409

        force = request.args.get('force', '0') in ('1', 'true', 'True')
        incremental = request.args.get('incremental', '1') in ('1', 'true', 'True')

        scan_state.update({
            'running': True, 'phase': 'running', 'error': None,
            'started_at': datetime.now().isoformat(timespec='seconds'),
            'finished_at': None, 'result': {},
        })
        t = threading.Thread(
            target=_run_scan,
            args=(current_app._get_current_object(), force, incremental),
            daemon=True,
        )
        t.start()

    log_action(current_user, '扫描同步', '已在后台启动')
    return jsonify({
        'success': True,
        'async': True,
        'message': '扫描已在后台启动，请轮询 /api/scan-progress 查看进度'
    })

@bp.route('/admin/invite', methods=['GET', 'POST'])
@login_required
def admin_invite():
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        invite = InviteCode.create_for_email(email, current_user.id)
        log_action(current_user, '生成邀请码', f'目标邮箱 {email}，码 {invite.code}')
        flash(f'✅ 邀请码已生成: {invite.code}', 'success')
    invites = InviteCode.query.order_by(InviteCode.created_at.desc()).all()
    return render_template('admin_invite.html', invites=invites, now=datetime.utcnow())

@bp.route('/admin/users')
@login_required
def admin_users():
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))
    # 隐藏账号不暴露在用户列表里
    import datetime as _dt
    from sqlalchemy import func
    q = request.args.get('q', '').strip()
    role = request.args.get('role', 'all')
    status = request.args.get('status', 'all')
    sort = request.args.get('sort', 'created')
    online_since = datetime.utcnow() - _dt.timedelta(minutes=ONLINE_WINDOW_MINUTES)

    query = User.query
    if q:
        like = '%' + q + '%'
        query = query.filter(db.or_(User.username.like(like), User.email.like(like)))
    if role == 'admin':
        query = query.filter_by(is_admin=True)
    elif role == 'sub':
        query = query.filter_by(is_sub_admin=True)
    elif role == 'user':
        query = query.filter(db.and_(User.is_admin.is_(False), User.is_sub_admin.is_(False)))
    if status == 'active':
        query = query.filter_by(is_active=True)
    elif status == 'inactive':
        query = query.filter_by(is_active=False)
    elif status == 'online':
        query = query.filter(User.last_active >= online_since)

    if sort == 'active':
        # 最近活跃：NULL（从未登录）排到最后
        query = query.order_by(User.last_active.isnot(None), User.last_active.desc())
    else:
        query = query.order_by(User.created_at.desc())
    users = query.all()
    online_ids = {u.id for u in users
                  if u.last_active and u.last_active >= online_since}
    # 阅读状态计数（在读/已读/未读）单次聚合，避免 N+1
    rp_rows = db.session.query(ReadingProgress.user_id, ReadingProgress.status,
                               func.count()).group_by(ReadingProgress.user_id,
                                                      ReadingProgress.status).all()
    reading_counts = {}
    for _uid, _st, _cnt in rp_rows:
        reading_counts.setdefault(_uid, {'unread': 0, 'reading': 0, 'finished': 0})
        reading_counts[_uid][_st] = _cnt
    return render_template('admin_users.html', users=users, q=q, role=role,
                           status=status, sort=sort, online_ids=online_ids,
                           online_minutes=ONLINE_WINDOW_MINUTES,
                           reading_counts=reading_counts)

@bp.route('/admin/user/<int:user_id>/toggle', methods=['POST'])
@login_required
def toggle_user(user_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        return jsonify({'success': False, 'message': '不能禁用当前登录的账号'}), 400
    if user.is_admin and user.is_active:
        active_admin_count = User.query.filter_by(is_admin=True, is_active=True).count()
        if active_admin_count <= 1:
            return jsonify({'success': False, 'message': '不能禁用最后一位管理员'}), 400
    user.is_active = not user.is_active
    db.session.commit()
    state = '禁用' if not user.is_active else '启用'
    log_action(current_user, '用户管理', f'{state}账号 {user.username}')
    return jsonify({'success': True})

@bp.route('/admin/user/<int:user_id>/delete', methods=['DELETE'])
@login_required
def delete_user(user_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        return jsonify({'success': False, 'message': '不能删除当前登录的账号'}), 400
    if user.is_admin:
        admin_count = User.query.filter_by(is_admin=True).count()
        if admin_count <= 1:
            return jsonify({'success': False, 'message': '不能删除最后一位管理员'}), 400
    # 清理该用户的从属数据，避免留下悬空记录（阅读进度 / 书签 / 登录历史 / 主题设置）
    ReadingProgress.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    Bookmark.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    LoginEvent.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    UserTheme.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.delete(user)
    db.session.commit()
    log_action(current_user, '用户管理', f'删除账号 {user.username}')
    return jsonify({'success': True})

@bp.route('/admin/user/<int:user_id>/reset-password', methods=['POST'])
@login_required
def reset_user_password(user_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    user = User.query.get_or_404(user_id)
    data = request.json or {}
    new_password = data.get('password', '')
    err = password_error(new_password)
    if err:
        return jsonify({'success': False, 'message': err}), 400
    user.set_password(new_password)
    db.session.commit()
    log_action(current_user, '用户管理', f'重置账号 {user.username} 的密码')
    return jsonify({'success': True})

@bp.route('/admin/user/create', methods=['POST'])
@login_required
def create_user():
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    email = (data.get('email') or '').strip()
    password = data.get('password') or ''
    if not username:
        return jsonify({'success': False, 'message': '用户名不能为空'}), 400
    if is_forbidden_username(username):
        return jsonify({'success': False, 'message': '该用户名不可用（系统保留名）'}), 400
    if not email:
        return jsonify({'success': False, 'message': '邮箱不能为空'}), 400
    err = password_error(password)
    if err:
        return jsonify({'success': False, 'message': err}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'success': False, 'message': '用户名已被使用'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'success': False, 'message': '邮箱已被注册'}), 400
    user = User(username=username, email=email, is_admin=data.get('is_admin', False))
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    log_action(current_user, '创建用户', f'{username}（{"管理员" if user.is_admin else "普通用户"}）')
    return jsonify({'success': True, 'message': '用户创建成功'})

@bp.route('/admin/users/export')
@login_required
def export_users():
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    import datetime as _dt
    import csv, io as _io
    from sqlalchemy import func
    online_since = datetime.utcnow() - _dt.timedelta(minutes=ONLINE_WINDOW_MINUTES)
    users = User.query.order_by(User.created_at.desc()).all()
    rp_rows = db.session.query(ReadingProgress.user_id, ReadingProgress.status,
                               func.count()).group_by(ReadingProgress.user_id,
                                                      ReadingProgress.status).all()
    rc = {}
    for _uid, _st, _cnt in rp_rows:
        rc.setdefault(_uid, {'unread': 0, 'reading': 0, 'finished': 0})
        rc[_uid][_st] = _cnt
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(['用户名', '邮箱', '角色', '状态', '在线', '最近活跃', '注册时间',
                '在读', '已读', '未读', '备注'])
    for u in users:
        online = '是' if (u.last_active and u.last_active >= online_since) else '否'
        role = '管理员' if u.is_admin else ('子管理员' if u.is_sub_admin else '用户')
        c = rc.get(u.id, {'unread': 0, 'reading': 0, 'finished': 0})
        w.writerow([u.username, u.email, role,
                    '正常' if u.is_active else '禁用', online,
                    u.last_active.strftime('%Y-%m-%d %H:%M') if u.last_active else '从未登录',
                    u.created_at.strftime('%Y-%m-%d %H:%M'),
                    c['reading'], c['finished'], c['unread'], u.admin_note or ''])
    body = '\ufeff' + buf.getvalue()
    resp = current_app.response_class(body, mimetype='text/csv; charset=utf-8')
    resp.headers['Content-Disposition'] = 'attachment; filename="users.csv"'
    log_action(current_user, '用户管理', '导出用户列表 CSV（%d 人）' % len(users))
    return resp

@bp.route('/admin/user/<int:user_id>/note', methods=['POST'])
@login_required
def set_user_note(user_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    user = User.query.get_or_404(user_id)
    data = request.json or {}
    user.admin_note = (data.get('note') or '')[:500]
    db.session.commit()
    log_action(current_user, '用户管理', f'更新账号 {user.username} 的备注')
    return jsonify({'success': True})

@bp.route('/admin/logs')
@login_required
def admin_logs():
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))
    logs = []
    try:
        log_path = Path(current_app.config.get('LOGS_DIR', '/app/logs')) / 'actions.log'
        with open(log_path, 'r', encoding='utf-8') as f:
            logs = f.readlines()[-100:]
    except FileNotFoundError:
        pass
    logs = [line.strip() for line in logs if line.strip()]
    logs.reverse()   # 倒序：最新的一条显示在最上面
    return render_template('admin_logs.html', logs=logs)

# ============ 设置 ============
@bp.route('/settings')
@login_required
def settings_page():
    # 下发「全部」首页扩展入口（不过滤隐藏项），否则用户一旦隐藏就再也看不到开关，
    # 只能改数据库才能恢复。
    entries = [{'plugin_id': e['plugin_id'], 'label': e.get('label') or e['plugin_id']}
               for e in registry.homepage_entries()
               if e.get('widget') != 'random']
    return render_template('settings.html',
                           plugins_json=json.dumps(entries, ensure_ascii=False))

@bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'change_password':
            old_password = request.form.get('old_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')
            if not current_user.check_password(old_password):
                flash('当前密码错误', 'error')
                return render_template('profile.html')
            if new_password != confirm_password:
                flash('两次输入的密码不一致', 'error')
                return render_template('profile.html')
            pw_error = password_error(new_password)
            if pw_error:
                flash(pw_error, 'error')
                return render_template('profile.html')
            current_user.set_password(new_password)
            db.session.commit()
            log_action(current_user, '修改密码', '')
            flash('✅ 密码修改成功！请重新登录', 'success')
            return redirect(url_for('main.logout'))
        elif action == 'update_profile':
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip()
            if not username or not email:
                flash('用户名和邮箱不能为空', 'error')
                return render_template('profile.html')
            if User.query.filter(User.username == username, User.id != current_user.id).first():
                flash('用户名已被使用', 'error')
                return render_template('profile.html')
            if User.query.filter(User.email == email, User.id != current_user.id).first():
                flash('该邮箱已被注册', 'error')
                return render_template('profile.html')
            if username != current_user.username and is_forbidden_username(username):
                flash('该用户名不可用（系统保留名）', 'error')
                return render_template('profile.html')
            current_user.username = username
            current_user.email = email
            current_user.nickname = request.form.get('nickname', '').strip()
            # 头像仅通过 /api/user/avatar 上传修改，这里不再覆盖
            current_user.signature = request.form.get('signature', '').strip()[:300]
            db.session.commit()
            log_action(current_user, '更新个人资料', f'{username} / {email}')
            flash('✅ 个人信息已更新', 'success')
        elif action == 'checkin':
            flash('签到功能暂未开放', 'error')
        elif action == 'update_privacy':
            public = request.form.get('bookshelf_public') == 'on'
            theme = UserTheme.query.filter_by(user_id=current_user.id).first()
            if not theme:
                theme = UserTheme(user_id=current_user.id)
                db.session.add(theme)
            theme.bookshelf_public = public
            db.session.commit()
            flash('✅ 隐私设置已保存', 'success')
    login_events = LoginEvent.query.filter_by(user_id=current_user.id).order_by(LoginEvent.ts.desc()).limit(15).all()
    return render_template('profile.html', login_events=login_events)

# ============ 头像：上传 + Gravatar 兜底 ============
@bp.route('/api/user/avatar', methods=['POST'])
@login_required
def upload_avatar():
    """上传用户头像（PNG/JPG/GIF/WEBP，≤2MB），存于 instance/avatars，仅本人可改。"""
    f = request.files.get('avatar')
    if not f or not f.filename:
        return jsonify(success=False, message='未选择文件'), 400
    data = f.read()
    if len(data) > 2 * 1024 * 1024:
        return jsonify(success=False, message='图片不能超过 2MB'), 400
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        ext, mt = 'png', 'image/png'
    elif data[:3] == b'\xff\xd8\xff':
        ext, mt = 'jpg', 'image/jpeg'
    elif data[:6] in (b'GIF87a', b'GIF89a'):
        ext, mt = 'gif', 'image/gif'
    elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        ext, mt = 'webp', 'image/webp'
    else:
        return jsonify(success=False, message='仅支持 PNG / JPG / GIF / WEBP'), 400
    d = os.path.join(current_app.instance_path, 'avatars')
    os.makedirs(d, exist_ok=True)
    fn = 'u%d.%s' % (current_user.id, ext)
    with open(os.path.join(d, fn), 'wb') as fh:
        fh.write(data)
    # 清理同用户的旧头像文件
    for old in os.listdir(d):
        if old.startswith('u%d.' % current_user.id) and old != fn:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass
    current_user.avatar = 'av:' + fn
    db.session.commit()
    return jsonify(success=True, url='/avatar/%d' % current_user.id)


@bp.route('/avatar/<int:uid>')
def serve_avatar(uid):
    """返回用户头像：已上传则发文件；否则按邮箱 md5 跳转 Gravatar（identicon 兜底）。"""
    user = User.query.get_or_404(uid)
    av = user.avatar or ''
    if av.startswith('av:'):
        fn = av[3:]
        d = os.path.join(current_app.instance_path, 'avatars')
        p = os.path.join(d, fn)
        if os.path.exists(p):
            return send_file(p, max_age=86400, conditional=True)
    em = (user.email or '').strip().lower()
    h = hashlib.md5(em.encode('utf-8')).hexdigest() if em else '0' * 32
    return redirect('https://www.gravatar.com/avatar/%s?d=identicon&s=200' % h)




def _sanitize_user_css(raw):
    """阅读页自定义 CSS 收口：限长 + 去掉能跳出 <style> 或执行脚本的片段。

    自定义样式是纯前端能力，用户只可能对自己生效；这里主要防「写坏整页」
    与「把自己的 CSS 变成一个能加载外部资源的入口」两类问题。
    """
    if not isinstance(raw, str):
        return ''
    css = raw[:4000]
    low = css.lower()
    for bad in ('</style', '<style', '<script', '</script', '<iframe', '<link',
                '<object', '<embed', 'javascript:', 'expression(', '@import'):
        while bad in low:
            i = low.find(bad)
            css = css[:i] + css[i + len(bad):]
            low = css.lower()
    return css


@bp.route('/api/user/theme', methods=['GET', 'POST'])
@login_required
def user_theme():
    theme = UserTheme.query.filter_by(user_id=current_user.id).first()
    if not theme:
        theme = UserTheme(user_id=current_user.id)
        db.session.add(theme)
        db.session.commit()
    if request.method == 'POST':
        data = request.json or {}
        for key in ['theme', 'font_size', 'line_spacing', 'page_margin', 'language', 'detail_mode', 'recommend', 'immersive', 'font_family', 'text_align', 'page_color', 'image_hide', 'brightness', 'bg_texture', 'text_color', 'accent_color']:
            if key in data:
                setattr(theme, key, data[key])
        if 'custom_css' in data:
            theme.custom_css = _sanitize_user_css(data.get('custom_css'))
        # 统一主题命名（旧数据里的 light 与默认主题视为 default）
        if theme.theme == 'light':
            theme.theme = 'default'
        db.session.commit()
        return jsonify({'success': True})
    current_theme = theme.theme or 'default'
    if current_theme == 'light':
        current_theme = 'default'
    return jsonify({
        'theme': current_theme,
        'font_size': theme.font_size,
        'line_spacing': theme.line_spacing,
        'page_margin': theme.page_margin,
        'language': theme.language or 'zh',
        'detail_mode': bool(theme.detail_mode),
        'recommend': bool(theme.recommend),
        'immersive': bool(theme.immersive),
        'font_family': theme.font_family or '',
        'text_align': theme.text_align or 'left',
        'page_color': theme.page_color or '',
        'image_hide': bool(theme.image_hide),
        'brightness': theme.brightness if theme.brightness is not None else 100,
        'bg_texture': theme.bg_texture or '',
        'text_color': theme.text_color or '',
        'custom_css': theme.custom_css or '',
        'accent_color': theme.accent_color or ''
    })


# ============ 用户设置 ============
@bp.route('/api/user/settings', methods=['GET', 'POST'])
@login_required
def user_settings():
    theme = UserTheme.query.filter_by(user_id=current_user.id).first()
    if not theme:
        theme = UserTheme(user_id=current_user.id)
        db.session.add(theme)
        db.session.commit()
    if request.method == 'POST':
        data = request.json or {}
        key = data.get('key')
        value = data.get('value')
        if key == 'show_recommend':
            theme.recommend = bool(value)
        elif key == 'language':
            theme.language = str(value)[:10]
        elif key == 'detail_mode':
            theme.detail_mode = bool(value)
        elif key == 'immersive':
            theme.immersive = bool(value)
        elif key == 'hidden_plugins':
            # 首页扩展卡片：用户级隐藏列表（收成逗号包裹字符串存储）
            if isinstance(value, str):
                value = [x for x in value.split(',') if x]
            _set_hidden_plugins(current_user, value)
            return jsonify({'success': True,
                            'hidden_plugins': sorted({str(x) for x in (value or [])})})
        else:
            return jsonify({'success': False, 'error': f'未知设置项: {key}'}), 400
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({
        'success': True,
        'show_recommend': bool(theme.recommend),
        'language': theme.language or 'zh',
        'detail_mode': bool(theme.detail_mode),
        'immersive': bool(theme.immersive),
        'hidden_plugins': sorted(_hidden_plugins_of(current_user))
    })

# ============ 首页书库总览 ============
@bp.route('/api/library-overview')
@login_required
def api_library_overview():
    """首页「书库总览」卡片数据。

    全部走 SQL 聚合（count / group by），**不把书行拉进 Python** —— 大库上
    7 万行级别一旦 query.all() 就是秒级，这里三条聚合查询在 10ms 量级。
    """
    now = time.time()
    glob = _overview_cache['data']
    if glob is None or now - _overview_cache['ts'] >= _OVERVIEW_CACHE_TTL:
        total = db.session.query(db.func.count(Book.id)).scalar() or 0
        cat_total = db.session.query(db.func.count(Category.id)).scalar() or 0

        fmt_rows = (db.session.query(Book.file_type, db.func.count(Book.id))
                    .group_by(Book.file_type)
                    .order_by(db.func.count(Book.id).desc()).all())
        formats = [{'type': (r[0] or '未知').upper(), 'count': int(r[1])}
                   for r in fmt_rows if r[1]]
        other_formats = sum(f['count'] for f in formats[5:])
        formats = formats[:5]
        if other_formats:
            formats.append({'type': '其它', 'count': other_formats})
        glob = {'total': int(total), 'categories': int(cat_total), 'formats': formats}
        _overview_cache['ts'] = now
        _overview_cache['data'] = glob
    total = glob['total']
    cat_total = glob['categories']
    formats = glob['formats']

    # 阅读状态：只查当前用户的进度记录（数量级远小于书库），未记录的归为「未读」
    st_rows = (db.session.query(ReadingProgress.status,
                                db.func.count(ReadingProgress.id))
               .filter(ReadingProgress.user_id == current_user.id)
               .group_by(ReadingProgress.status).all())
    reading = finished = 0
    for st, n in st_rows:
        if st == 'reading':
            reading += int(n)
        elif st == 'finished':
            finished += int(n)
    unread = max(0, total - reading - finished)

    return jsonify({
        'success': True,
        'total': int(total),
        'categories': int(cat_total),
        'formats': formats,
        'status': {'unread': unread, 'reading': reading, 'finished': finished},
    })


# ============ 阅读进度 API ============
@bp.route('/api/progress/<int:book_id>', methods=['GET'])
@login_required
def get_progress(book_id):
    progress = ReadingProgress.query.filter_by(
        user_id=current_user.id, 
        book_id=book_id
    ).first()
    if progress:
        return jsonify({
            'success': True,
            'progress': progress.progress,
            'location': progress.last_location,
            'updated_at': progress.updated_at.isoformat() if progress.updated_at else None
        })
    return jsonify({'success': True, 'progress': 0, 'location': None})

@bp.route('/api/progress/<int:book_id>', methods=['POST'])
@login_required
def save_progress(book_id):
    data = request.get_json(silent=True) or {}
    try:
        progress_value = float(data.get('progress') or 0)
    except (TypeError, ValueError):
        progress_value = 0.0
    progress_value = max(0.0, min(1.0, progress_value))
    location = data.get('location', '')
    if location is not None and not isinstance(location, str):
        location = str(location)

    progress = ReadingProgress.query.filter_by(
        user_id=current_user.id,
        book_id=book_id
    ).first()

    #  阅读状态自动更新：读到 98% 以上自动标记「已读」
    auto_status = 'finished' if progress_value >= 0.98 else 'reading'

    if not progress:
        progress = ReadingProgress(
            user_id=current_user.id,
            book_id=book_id,
            progress=progress_value,
            status=auto_status,
            last_location=location,
            updated_at=datetime.utcnow()
        )
        db.session.add(progress)
    else:
        progress.progress = progress_value
        progress.last_location = location
        progress.updated_at = datetime.utcnow()
        # 已手动标「已读」的不被自动回退，其余随进度更新
        if progress.status != 'finished':
            progress.status = auto_status

    db.session.commit()
    return jsonify({'success': True, 'message': '进度已保存', 'status': progress.status})

@bp.route('/api/progress/<int:book_id>/status', methods=['POST'])
@login_required
def set_book_status(book_id):
    """ 阅读状态手动更新：未读 / 在读 / 已读"""
    data = request.get_json(silent=True) or {}
    status = (data.get('status') or '').strip()
    if status not in ('unread', 'reading', 'finished'):
        return jsonify({'success': False, 'message': '状态不合法'}), 400
    book = Book.query.get_or_404(book_id)
    rec = ReadingProgress.query.filter_by(user_id=current_user.id, book_id=book.id).first()
    if not rec:
        rec = ReadingProgress(user_id=current_user.id, book_id=book.id, progress=0)
        db.session.add(rec)
    if status == 'unread':
        rec.progress = 0
    elif status == 'finished' and (rec.progress or 0) < 0.98:
        rec.progress = 1.0
    rec.status = status
    rec.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'status': status})


@bp.route('/api/progress/all', methods=['GET'])
@login_required
def get_all_progress():
    progress_list = ReadingProgress.query.filter_by(
        user_id=current_user.id
    ).all()
    return jsonify([{
        'book_id': p.book_id,
        'progress': p.progress,
        'location': p.last_location,
        'updated_at': p.updated_at.isoformat() if p.updated_at else None
    } for p in progress_list])

# ============ 继续阅读 ============
@bp.route('/api/continue-reading')
@login_required
def get_continue_reading():
    progress = ReadingProgress.query.filter_by(
        user_id=current_user.id
    ).order_by(ReadingProgress.updated_at.desc()).first()
    if progress:
        book = Book.query.get(progress.book_id)
        if book:
            return jsonify({
                'success': True,
                'book_id': book.id,
                'book_title': book.title or book.filename,
                'progress': progress.progress
            })
    return jsonify({'success': False})

# ============ 书架与收藏（B15） ============
@bp.route('/bookshelf')
@login_required
def bookshelf():
    """我的书架（私有）。"""
    return render_template('bookshelf.html', public=False)

@bp.route('/bookshelf/<username>')
def bookshelf_public(username):
    """公开书架（仅当该用户开启 bookshelf_public）。"""
    return render_template('bookshelf.html', public=True, owner=username)

@bp.route('/api/bookshelf')
@login_required
def api_bookshelf():
    """当前用户书架：按阅读状态分组 + 收藏标记，支持排序。"""
    scope = (request.args.get('scope') or 'all').strip()
    sort = (request.args.get('sort') or 'updated').strip()
    q = ReadingProgress.query.filter_by(user_id=current_user.id)
    if scope in ('unread', 'reading', 'finished'):
        q = q.filter_by(status=scope)
    elif scope == 'favorite':
        q = q.filter_by(favorite=True)
    recs = q.all()
    book_ids = [r.book_id for r in recs]
    books = Book.query.filter(Book.id.in_(book_ids)).all() if book_ids else []
    bmap = {b.id: b for b in books}
    items = []
    for r in recs:
        b = bmap.get(r.book_id)
        if not b:
            continue
        items.append({
            'book_id': b.id,
            'title': b.title or b.filename,
            'author': b.author or '未知作者',
            'file_type': b.file_type or '未知',
            'progress': round(r.progress or 0, 4),
            'status': r.status or 'unread',
            'favorite': bool(r.favorite),
            'updated_at': r.updated_at.strftime('%Y-%m-%d %H:%M') if r.updated_at else '',
        })
    if sort == 'title':
        items.sort(key=lambda x: (x['title'] or '').lower())
    elif sort == 'author':
        items.sort(key=lambda x: (x['author'] or '').lower())
    elif sort == 'progress':
        items.sort(key=lambda x: x['progress'])
    else:
        items.sort(key=lambda x: x['updated_at'], reverse=True)
    all_recs = ReadingProgress.query.filter_by(user_id=current_user.id).all()
    counts = {
        'unread': sum(1 for r in all_recs if (r.status or 'unread') == 'unread'),
        'reading': sum(1 for r in all_recs if r.status == 'reading'),
        'finished': sum(1 for r in all_recs if r.status == 'finished'),
        'favorite': sum(1 for r in all_recs if r.favorite),
        'total': len(all_recs),
    }
    return jsonify({'success': True, 'books': items, 'counts': counts})

@bp.route('/api/bookshelf/public/<username>')
def api_bookshelf_public(username):
    """公开书架（仅当该用户开启 bookshelf_public，只读）。"""
    u = User.query.filter_by(username=username).first()
    if not u:
        return jsonify({'success': False, 'private': False, 'message': '用户不存在'}), 404
    theme = UserTheme.query.filter_by(user_id=u.id).first()
    if not (theme and theme.bookshelf_public):
        return jsonify({'success': False, 'private': True})
    recs = ReadingProgress.query.filter_by(user_id=u.id).all()
    book_ids = [r.book_id for r in recs]
    books = Book.query.filter(Book.id.in_(book_ids)).all() if book_ids else []
    bmap = {b.id: b for b in books}
    items = []
    for r in recs:
        b = bmap.get(r.book_id)
        if not b:
            continue
        items.append({
            'book_id': b.id,
            'title': b.title or b.filename,
            'author': b.author or '未知作者',
            'file_type': b.file_type or '未知',
            'progress': round(r.progress or 0, 4),
            'status': r.status or 'unread',
            'updated_at': r.updated_at.strftime('%Y-%m-%d %H:%M') if r.updated_at else '',
        })
    return jsonify({'success': True, 'owner': u.nickname or u.username, 'books': items})

@bp.route('/api/bookshelf/favorite', methods=['POST'])
@login_required
def toggle_favorite():
    """切换某本书的收藏状态（无记录则先建 ReadingProgress）。"""
    data = request.get_json(silent=True) or {}
    book_id = data.get('book_id')
    if not book_id:
        return jsonify({'success': False, 'message': '缺少 book_id'}), 400
    if not Book.query.get(book_id):
        return jsonify({'success': False, 'message': '书籍不存在'}), 404
    rec = ReadingProgress.query.filter_by(user_id=current_user.id, book_id=book_id).first()
    if not rec:
        rec = ReadingProgress(user_id=current_user.id, book_id=book_id, progress=0, status='unread')
        db.session.add(rec)
    rec.favorite = not rec.favorite
    rec.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'favorite': rec.favorite})

# ============ 书签  ============
@bp.route('/api/bookmarks/<int:book_id>', methods=['GET'])
@login_required
def api_list_bookmarks(book_id):
    marks = Bookmark.query.filter_by(
        user_id=current_user.id, book_id=book_id
    ).order_by(Bookmark.progress.asc()).all()
    return jsonify({
        'success': True,
        'bookmarks': [{
            'id': m.id,
            'progress': m.progress,
            'excerpt': m.excerpt or '',
            'note': m.note or '',
            'created_at': m.created_at.strftime('%m-%d %H:%M') if m.created_at else '',
        } for m in marks]
    })


@bp.route('/api/bookmarks', methods=['POST'])
@login_required
def api_add_bookmark():
    data = request.get_json(silent=True) or {}
    book_id = data.get('book_id')
    if not book_id:
        return jsonify({'success': False, 'message': '缺少 book_id'}), 400
    if not Book.query.get(book_id):
        return jsonify({'success': False, 'message': '书籍不存在'}), 404

    m = Bookmark(
        user_id=current_user.id,
        book_id=book_id,
        progress=float(data.get('progress') or 0),
        excerpt=(data.get('excerpt') or '')[:200],
        note=(data.get('note') or '')[:200],
    )
    db.session.add(m)
    db.session.commit()
    return jsonify({'success': True, 'id': m.id})


@bp.route('/api/bookmark/<int:mark_id>', methods=['DELETE'])
@login_required
def api_delete_bookmark(mark_id):
    m = Bookmark.query.get_or_404(mark_id)
    if m.user_id != current_user.id:
        return jsonify({'success': False, 'message': '无权操作'}), 403
    db.session.delete(m)
    db.session.commit()
    return jsonify({'success': True})


# ============ 书籍详情页  ============
@bp.route('/book/<int:book_id>')
@login_required
def book_detail(book_id):
    book = Book.query.get_or_404(book_id)

    progress = ReadingProgress.query.filter_by(
        user_id=current_user.id, book_id=book_id).first()

    # 阅读状态：无记录为未读，否则用记录状态或按进度推导
    if not progress:
        cur_status = 'unread'
    elif progress.status:
        cur_status = progress.status
    else:
        cur_status = 'finished' if (progress.progress or 0) >= 0.98 else 'reading'
    cur_status_text = {'unread': '未读', 'reading': '在读', 'finished': '已读'}.get(cur_status, '未读')

    # 面包屑：逐级父分类
    crumbs = []
    c = book.category
    while c:
        crumbs.insert(0, c)
        c = c.parent
    # 根分类 path 为空（P0 修复后），拼接时跳过
    crumb_path = '/'.join([x.path for x in crumbs if x.path])

    return render_template(
        'book_detail.html',
        book=book,
        progress=progress,
        cur_status=cur_status,
        cur_status_text=cur_status_text,
        crumbs=crumbs,
        crumb_path=crumb_path,
        is_fav=bool(progress.favorite) if progress else False,
        size_str=BookUtils.format_file_size(book.file_size) if book.file_size else '未知',
    )


@bp.route('/api/book/<int:book_id>')
@login_required
def api_book(book_id):
    book = Book.query.get_or_404(book_id)
    return jsonify({
        'success': True,
        'id': book.id,
        'title': book.title or book.filename,
        'author': book.author or '',
        'file_type': book.file_type,
        'file_size_str': BookUtils.format_file_size(book.file_size) if book.file_size else '0 B',
        'path': book.relative_path or book.filename,
        'category': book.category.name if book.category else '',
        'modified_time': book.modified_time.strftime('%Y-%m-%d %H:%M') if book.modified_time else '',
        'read_count': book.read_count or 0,
    })


# ============ 乱码与损坏文件 ============
def _bad_text_clause():
    """书名或文件名含替换符（U+FFFD，原中文已丢失）的书籍。"""
    like = '%' + '\ufffd' + '%'
    return db.or_(Book.title.like(like), Book.filename.like(like))


def _book_abs_path(book):
    base = str(current_app.config['BOOKS_DIR'])
    rel = (book.relative_path or book.filename or '').strip()
    return os.path.join(base, rel) if rel else ''


def _rename_book_file(book, new_title):
    """按新书名重命名磁盘文件（保留扩展名与原目录）。

    返回 (ok, 消息, 新的相对路径)；相对路径为空表示无需变更。
    """
    books_dir = str(current_app.config['BOOKS_DIR'])
    old_rel = (book.relative_path or book.filename or '').strip()
    if not old_rel:
        return False, '没有记录文件路径', ''
    src = os.path.join(books_dir, old_rel)
    if not os.path.exists(src):
        return False, '磁盘上找不到原文件', ''
    ext = os.path.splitext(old_rel)[1] or ('.' + (book.file_type or 'txt'))
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', new_title).strip().strip('.')
    if not safe:
        return False, '文件名不合法', ''
    d = os.path.dirname(old_rel)
    cand = os.path.join(d, safe + ext) if d else (safe + ext)
    dst = os.path.join(books_dir, cand)
    n = 2
    while os.path.exists(dst) and os.path.normcase(dst) != os.path.normcase(src):
        cand = (os.path.join(d, '%s (%d)%s' % (safe, n, ext)) if d
                else ('%s (%d)%s' % (safe, n, ext)))
        dst = os.path.join(books_dir, cand)
        n += 1
    if os.path.normcase(dst) == os.path.normcase(src):
        return True, '文件名无需变更', ''
    try:
        os.rename(src, dst)
    except Exception as e:
        return False, str(e), ''
    return True, '文件已重命名为「%s」' % os.path.basename(cand), cand


def _move_book_file_to_category(book, tgt):
    """把书的磁盘文件真实搬到目标分类所在目录（需求 #33）。

    tgt 为 None 表示搬到书库根目录（未分类）。目标目录不存在则自动创建；
    同名冲突自动加 " (n)" 后缀，绝不覆盖已有文件。
    返回 (ok, 消息, skipped)；skipped=True 表示无需/未动磁盘。
    """
    books_dir = str(current_app.config['BOOKS_DIR'])
    old_rel = (book.relative_path or book.filename or '').strip()
    if not old_rel:
        return False, '没有记录文件路径', True
    src = os.path.join(books_dir, old_rel)
    if not os.path.exists(src):
        return False, '磁盘上找不到原文件', True
    fname = os.path.basename(old_rel)
    tgt_sub = (tgt.path if tgt else '') or ''
    tgt_dir = os.path.join(books_dir, tgt_sub) if tgt_sub else books_dir
    if os.path.normcase(os.path.dirname(os.path.abspath(src))) == \
            os.path.normcase(os.path.abspath(tgt_dir)):
        return True, '文件已在目标目录', True
    try:
        os.makedirs(tgt_dir, exist_ok=True)
    except Exception as e:
        return False, '创建目标目录失败：' + str(e), False
    dst = os.path.join(tgt_dir, fname)
    stem, ext = os.path.splitext(fname)
    n = 2
    while os.path.exists(dst):
        dst = os.path.join(tgt_dir, '%s (%d)%s' % (stem, n, ext))
        n += 1
    try:
        os.rename(src, dst)
    except Exception as e:
        return False, '移动文件失败：' + str(e), False
    new_rel = os.path.relpath(dst, books_dir).replace(os.sep, '/')
    book.relative_path = new_rel
    book.filename = os.path.basename(dst)
    return True, '文件已移动到「%s」' % (tgt.name if tgt else '未分类'), False


@bp.route('/admin')
@login_required
def admin_hub():
    if not (current_user.is_admin or current_user.is_sub_admin):
        return redirect(url_for('main.library'))
    return render_template('admin_hub.html',
                           is_admin=current_user.is_admin,
                           is_sub_admin=current_user.is_sub_admin)


@bp.route('/admin/broken')
@login_required
def admin_broken():
    """乱码与损坏文件管理页（管理员或已授权的子管理员）。"""
    if not _require_cap('manage_books'):
        flash('无权限访问该页面', 'error')
        return redirect(url_for('main.library'))
    # 只按「含替换符」筛会漏掉纯形态乱码（GBK 字节被当 UTF-8 解出的那类），
    # 这里统一用 looks_mojibake 判定，并顺带算出可还原的名字。
    cats = Category.query.all()
    child_of = {}
    for c in cats:
        child_of[c.parent_id] = child_of.get(c.parent_id, 0) + 1
    garbled_cats = []
    for c in cats:
        if not broken_files.looks_mojibake(c.name or ''):
            continue
        rec = broken_files.recover_mojibake(c.name or '')
        garbled_cats.append({
            'id': c.id,
            'name': c.name or '',
            'path': c.path or c.name or '',
            'count': c.book_count or 0,
            'children': child_of.get(c.id, 0),
            'recovered': rec or '',
        })
    garbled_cats.sort(key=lambda g: (-(g['count'] or 0), -len(g['path']), g['name']))
    recoverable = sum(1 for g in garbled_cats if g['recovered'])
    empty_junk = sum(1 for g in garbled_cats
                     if not g['recovered'] and not g['count'] and not g['children'])
    total_bad = Book.query.filter(_bad_text_clause()).count()
    return render_template('admin_broken.html',
                           garbled_cats=garbled_cats, total_bad=total_bad,
                           garbled_total=len(garbled_cats),
                           garbled_recoverable=recoverable,
                           garbled_empty=empty_junk)


@bp.route('/api/admin/broken/scan', methods=['GET'])
@login_required
def api_admin_broken_scan():
    """分段扫描：书名 / 文件名乱码与损坏文件。offset + limit 分页，避免长请求。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    scope = request.args.get('scope', 'title')
    try:
        offset = max(0, int(request.args.get('offset', 0)))
        limit = min(30, max(1, int(request.args.get('limit', 15))))
    except ValueError:
        offset, limit = 0, 15

    q = Book.query.filter(_bad_text_clause()) if scope != 'all' else Book.query
    total = q.count()
    rows = q.order_by(Book.id).offset(offset).limit(limit).all()

    items = []
    for b in rows:
        path = _book_abs_path(b)
        status, detail = broken_files.inspect_file(path, b.file_type)
        suggested, source = '', ''
        ftype = (b.file_type or '').lower()
        if ftype in ('txt', 'md', 'log', 'cn', 'epub', 'pdf') and os.path.exists(path):
            suggested, source = broken_files.extract_title(path, ftype)
        items.append({
            'id': b.id,
            'title': b.title or '',
            'filename': b.filename or '',
            'file_type': b.file_type or '',
            'file_size': b.file_size or 0,
            'status': status,
            'status_text': detail or '',
            'mojibake': ('\ufffd' in (b.title or '')) or ('\ufffd' in (b.filename or '')),
            'suggested_title': suggested or '',
            'suggest_source': source or '',
        })
    return jsonify({'success': True, 'total': total, 'offset': offset,
                    'items': items, 'has_more': offset + limit < total})


@bp.route('/api/admin/broken/fix', methods=['POST'])
@login_required
def api_admin_broken_fix():
    """应用修复：写入新书名，可选同时把磁盘文件也改名（避免下次扫描又读回乱码名）。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    if not items:
        return jsonify({'success': False, 'message': '没有要修复的条目'}), 400
    fixed, failed = [], []
    for it in items[:200]:
        try:
            bid = int(it.get('id'))
        except (TypeError, ValueError):
            continue
        book = Book.query.get(bid)
        if book is None:
            failed.append({'id': bid, 'message': '书籍不存在'})
            continue
        new_title = (it.get('title') or '').strip()
        if not new_title:
            failed.append({'id': bid, 'message': '书名为空'})
            continue
        old_title = book.title or ''
        book.title = new_title[:512]
        report = ['书名：%s → %s' % (old_title, book.title)]
        want_rename = it.get('rename_file', data.get('rename_file', True))
        if want_rename and book.file_type:
            okr, msg, newrel = _rename_book_file(book, new_title[:200])
            report.append(msg)
            if okr and newrel:
                book.filename = os.path.basename(newrel)
                book.relative_path = newrel
        fixed.append({'id': bid, 'title': book.title, 'report': '；'.join(report)})
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '保存失败：' + str(e)}), 500
    if fixed:
        log_action(current_user, '修复乱码书籍', '%d 本' % len(fixed))
    msg = '已修复 %d 本' % len(fixed)
    if failed:
        msg += '，失败 %d 本' % len(failed)
    return jsonify({'success': True, 'fixed': fixed, 'failed': failed, 'message': msg})


@bp.route('/api/admin/broken/delete', methods=['POST'])
@login_required
def api_admin_broken_delete():
    """删除乱码 / 损坏书籍：磁盘正文 + 封面 + 阅读进度 + 书签 + 记录一并清理。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    data = request.get_json(silent=True) or {}
    ids = data.get('ids') or []
    if not ids:
        return jsonify({'success': False, 'message': '没有选中条目'}), 400
    deleted, files_deleted, files_failed, results = 0, 0, 0, []
    for bid in ids[:300]:
        try:
            bid = int(bid)
        except (TypeError, ValueError):
            continue
        book = Book.query.get(bid)
        if book is None:
            continue
        title = book.title or ''
        rep = _purge_book(book)
        db.session.delete(book)
        deleted += 1
        if rep.get('file_deleted'):
            files_deleted += 1
        if rep.get('errors'):
            files_failed += 1
        results.append({'id': bid, 'title': title, 'file_deleted': bool(rep.get('file_deleted'))})
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '删除失败：' + str(e)}), 500
    if deleted:
        log_action(current_user, '删除乱码/损坏书籍', '%d 本' % deleted)
    return jsonify({'success': True, 'deleted': deleted,
                    'files_deleted': files_deleted, 'files_failed': files_failed,
                    'results': results,
                    'message': '已删除 %d 本（磁盘文件 %d 个）' % (deleted, files_deleted)})


@bp.route('/api/admin/broken/fix-cats', methods=['POST'])
@login_required
def api_admin_broken_fix_cats():
    """修复乱码分类目录。

    - 能还原出中文名的：改名（分类名 + path），可选把磁盘目录一起改名，
      并同步修正其下所有书籍的 relative_path，避免改名后全书变「文件丢失」。
    - 还原不出来的空目录（无书、无子分类）：连磁盘空目录一起删掉，避免下次扫描又灌回来。
    - 还原不出来但有内容的：跳过，留给人工处理，绝不误删。
    """
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    data = request.get_json(silent=True) or {}
    ids = data.get('ids') or []
    rename_dir = bool(data.get('rename_dir', True))
    if not ids:
        return jsonify({'success': False, 'message': '没有选中分类'}), 400

    books_dir = str(current_app.config['BOOKS_DIR'])
    # 深的先处理：父目录改名会让子目录的 path 失效
    cats = [c for c in (Category.query.get(i) for i in ids[:300] if isinstance(i, int)) if c]
    cats.sort(key=lambda c: -len(c.path or ''))

    renamed, removed, skipped = [], [], []
    for cat in cats:
        old_path = cat.path or ''
        rec = broken_files.recover_mojibake(cat.name or '')
        kids = Category.query.filter_by(parent_id=cat.id).count()
        books_here = (cat.book_count or 0)

        if not rec:
            if kids or books_here:
                skipped.append({'id': cat.id, 'name': cat.name, 'msg': '无法还原且仍有内容，已跳过'})
                continue
            # 无书无子分类：删掉磁盘上的空目录（仅当确实为空），再删分类
            abs_dir = os.path.join(books_dir, old_path) if old_path else ''
            try:
                if abs_dir and os.path.isdir(abs_dir) and not os.listdir(abs_dir):
                    os.rmdir(abs_dir)
                elif abs_dir and os.path.isdir(abs_dir):
                    skipped.append({'id': cat.id, 'name': cat.name, 'msg': '目录非空，已跳过'})
                    continue
            except Exception as e:
                skipped.append({'id': cat.id, 'name': cat.name, 'msg': '删除目录失败：%s' % e})
                continue
            db.session.delete(cat)
            removed.append({'id': cat.id, 'name': cat.name, 'msg': '已删除空乱码目录'})
            continue

        parent_prefix = os.path.dirname(old_path)
        new_path = (parent_prefix + '/' + rec) if parent_prefix else rec
        if Category.query.filter(Category.path == new_path, Category.id != cat.id).first():
            skipped.append({'id': cat.id, 'name': cat.name, 'msg': '目标分类已存在：%s' % rec})
            continue

        msg_bits = []
        abs_old = os.path.join(books_dir, old_path) if old_path else ''
        abs_new = os.path.join(books_dir, new_path) if new_path else ''
        dir_moved = False
        if rename_dir and abs_old and abs_new and os.path.isdir(abs_old):
            if os.path.exists(abs_new):
                msg_bits.append('磁盘目录已存在同名目录，仅改了分类名')
            else:
                try:
                    os.rename(abs_old, abs_new)
                    dir_moved = True
                    msg_bits.append('磁盘目录已改名')
                except Exception as e:
                    msg_bits.append('磁盘目录改名失败：%s' % e)

        # 分类：name / path 自身
        cat.name = rec
        cat.path = new_path
        # 子孙分类的 path 前缀同步
        prefix_old = old_path + '/'
        for child in Category.query.filter(Category.path.like(prefix_old + '%')).all():
            child.path = new_path + child.path[len(old_path):]
        # 书籍 relative_path 同步（否则改名后整目录的书都变「文件丢失」）
        moved_books = 0
        for b in Book.query.filter(Book.relative_path.like(prefix_old + '%')).all():
            b.relative_path = new_path + b.relative_path[len(old_path):]
            moved_books += 1
        if moved_books:
            msg_bits.append('同步 %d 本书的路径' % moved_books)

        renamed.append({'id': cat.id, 'name': old_path, 'new_name': rec,
                        'msg': '；'.join(msg_bits) or '已改名', 'dir_moved': dir_moved})

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '保存失败：' + str(e)}), 500
    if renamed or removed:
        log_action(current_user, '修复乱码分类',
                   '改名 %d 个，删除 %d 个' % (len(renamed), len(removed)))
    return jsonify({
        'success': True,
        'renamed': renamed, 'removed': removed, 'skipped': skipped,
        'message': '改名 %d 个，删除 %d 个，跳过 %d 个'
                   % (len(renamed), len(removed), len(skipped)),
    })


# ============ 插件框架 ============
PLUGIN_ID_RE = re.compile(r'^[a-z0-9_]{1,40}$')
PLUGIN_MAX_PY = 512 * 1024           # 单文件插件上限 512KB
PLUGIN_MAX_ZIP = 5 * 1024 * 1024     # 插件包上限 5MB
PLUGIN_MAX_FILES = 300               # 插件包内文件数上限
PLUGIN_MAX_UNZIP = 20 * 1024 * 1024  # 解压后总体积上限


def plugin_dir():
    """用户自放插件的持久目录。"""
    return os.path.join(current_app.instance_path, 'plugins')


def slugify_plugin_name(name):
    """把任意文件名收敛成合法的插件 id（小写字母/数字/下划线，长度 <= 40）。"""
    return re.sub(r'[^a-z0-9_]', '', str(name or '').lower())[:40]


def reregister_plugins():
    """重新扫描插件目录，并把新出现的蓝图注册进当前应用。"""
    registry.reload()
    registered = set(current_app.blueprints.keys())
    for _bp in registry.blueprints() + registry.settings_blueprints():
        if _bp.name not in registered:
            try:
                current_app.register_blueprint(_bp)
                registered.add(_bp.name)
            except Exception as e:
                print('[plugins] 运行时注册蓝图失败 %s: %s' % (_bp.name, e), flush=True)


def validate_plugin_source(src):
    """校验插件源码：可解析且定义了顶层 PLUGIN。返回 (ok, 错误信息)。"""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, '语法错误：第 %s 行 %s' % (e.lineno, e.msg)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if getattr(t, 'id', None) == 'PLUGIN':
                return True, ''
    return False, '未找到顶层 PLUGIN 定义（插件必须写 PLUGIN = Plugin(...)）'


@bp.route('/api/plugins', methods=['GET'])
@login_required
def api_plugins():
    """列出插件（普通用户只看启用态；管理员看全部含启用开关）。"""
    if current_user.is_admin:
        return jsonify({'success': True, 'plugins': registry.list_for_admin()})
    return jsonify({'success': True, 'plugins': [
        {'id': p.id, 'name': p.name, 'description': p.description,
         'homepage': [{'label': e.label, 'widget': e.widget, 'url': e.url}
                      for e in p.homepage]}
        for p in registry.active()
    ]})


@bp.route('/api/plugins/<plugin_id>/toggle', methods=['POST'])
@login_required
def api_plugin_toggle(plugin_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': '仅管理员可操作'}), 403
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled', True)
    if not registry.set_enabled(plugin_id, enabled):
        return jsonify({'success': False, 'message': '插件不存在'}), 404
    return jsonify({'success': True, 'enabled': registry.is_enabled(plugin_id)})


@bp.route('/api/plugins/nav', methods=['GET'])
@login_required
def api_plugins_nav():
    """返回插件注入前端的入口：阅读页工具栏按钮 + 管理员侧栏链接。"""
    out = {'reader_tools': registry.reader_tools_entries()}
    out['admin_nav'] = registry.admin_nav_entries() if current_user.is_admin else []
    return jsonify({'success': True, **out})


@bp.route('/api/plugins/scan', methods=['POST'])
@login_required
def api_plugins_scan():
    """重新扫描插件目录（用户丢入新插件后无需重启即可发现并注册蓝图）。"""
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': '仅管理员可操作'}), 403
    try:
        reregister_plugins()
        return jsonify({'success': True, 'plugins': registry.list_for_admin()})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/admin/plugins')
@login_required
def admin_plugins():
    if not current_user.is_admin:
        flash('仅管理员可访问', 'error')
        return redirect(url_for('main.library'))
    plugins = registry.list_for_admin()
    return render_template('admin_plugins.html', plugins=plugins)

@bp.route('/api/plugins/<plugin_id>/settings', methods=['GET', 'POST'])
@login_required
def api_plugin_settings(plugin_id):
    """读取/保存某插件的图形化设置（仅管理员）。GET 返回当前设置与 schema；POST 保存。"""
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': '仅管理员可操作'}), 403
    p = registry._plugins.get(plugin_id)
    if p is None:
        return jsonify({'success': False, 'message': '插件不存在'}), 404
    if request.method == 'GET':
        return jsonify({'success': True,
                        'settings': registry.get_settings(plugin_id),
                        'schema': p.settings_schema})
    data = request.get_json(force=True, silent=True) or {}
    payload = data.get('settings', data)
    if not registry.set_settings(plugin_id, payload):
        return jsonify({'success': False, 'message': '保存失败'}), 500
    return jsonify({'success': True, 'settings': registry.get_settings(plugin_id)})


@bp.route('/api/plugins/upload', methods=['POST'])
@login_required
def api_plugins_upload():
    """上传插件：单个 .py 文件，或 .zip 插件包（目录形式）。仅最高管理员。

    落盘前统一校验：文件名合法 / UTF-8 / 语法可解析 / 定义顶层 PLUGIN /
    zip 内路径安全；通过后写入用户插件目录并即时重新注册。
    """
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': '仅管理员可操作'}), 403
    f = request.files.get('file')
    if f is None or not f.filename:
        return jsonify({'success': False, 'message': '请选择要上传的文件'}), 400

    raw_name = os.path.basename(f.filename)
    ext = os.path.splitext(raw_name)[1].lower()
    target = plugin_dir()
    os.makedirs(target, exist_ok=True)

    # ---------- 单个 .py ----------
    if ext == '.py':
        data = f.read()
        if len(data) > PLUGIN_MAX_PY:
            return jsonify({'success': False, 'message': '单个插件文件不能超过 512KB'}), 400
        try:
            src = data.decode('utf-8')
        except UnicodeDecodeError:
            return jsonify({'success': False, 'message': '插件文件必须是 UTF-8 编码'}), 400
        pid = slugify_plugin_name(os.path.splitext(raw_name)[0])
        if not PLUGIN_ID_RE.match(pid):
            return jsonify({'success': False,
                            'message': '文件名只能用小写字母、数字、下划线（例如 my_plugin.py）'}), 400
        ok, err = validate_plugin_source(src)
        if not ok:
            return jsonify({'success': False, 'message': err}), 400
        # 同名插件包（目录）会让单文件版本失效，先清掉避免歧义
        pkg = os.path.join(target, pid)
        if os.path.isdir(pkg):
            shutil.rmtree(pkg, ignore_errors=True)
        with open(os.path.join(target, pid + '.py'), 'wb') as fh:
            fh.write(data)
        reregister_plugins()
        return jsonify({'success': True, 'plugin_id': pid,
                        'message': '插件已安装：%s' % pid,
                        'plugins': registry.list_for_admin()})

    # ---------- .zip 插件包 ----------
    if ext == '.zip':
        blob = f.read()
        if len(blob) > PLUGIN_MAX_ZIP:
            return jsonify({'success': False, 'message': '插件包不能超过 5MB'}), 400
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except Exception:
            return jsonify({'success': False, 'message': '不是有效的 zip 文件'}), 400

        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            return jsonify({'success': False, 'message': 'zip 包里没有文件'}), 400
        if len(infos) > PLUGIN_MAX_FILES:
            return jsonify({'success': False,
                            'message': 'zip 包内文件过多（最多 %d 个）' % PLUGIN_MAX_FILES}), 400
        if sum(i.file_size for i in infos) > PLUGIN_MAX_UNZIP:
            return jsonify({'success': False, 'message': 'zip 包解压后体积过大'}), 400

        tops = set()
        for i in infos:
            n = i.filename.replace('\\', '/')
            if n.startswith('/') or '..' in n.split('/') or re.match(r'^[a-zA-Z]:', n):
                return jsonify({'success': False,
                                'message': 'zip 包内含非法路径：%s' % i.filename}), 400
            tops.add(n.split('/')[0])

        single_py = (len(tops) == 1 and list(tops)[0].lower().endswith('.py'))
        pkg = slugify_plugin_name(os.path.splitext(raw_name)[0])
        if len(tops) == 1 and not single_py:
            pkg = slugify_plugin_name(list(tops)[0])
        if not PLUGIN_ID_RE.match(pkg):
            return jsonify({'success': False,
                            'message': '无法推断插件 ID：请把包名或 zip 内层目录命名为小写字母/数字/下划线'}), 400

        tmp = tempfile.mkdtemp(prefix='sr_plugin_in_')
        try:
            zf.extractall(tmp)

            if single_py:
                only = list(tops)[0]
                entry = os.path.join(tmp, only.replace('/', os.sep))
                src = io.open(entry, encoding='utf-8').read()
                ok, err = validate_plugin_source(src)
                if not ok:
                    return jsonify({'success': False, 'message': err}), 400
                pkgd = os.path.join(target, pkg)
                if os.path.isdir(pkgd):
                    shutil.rmtree(pkgd, ignore_errors=True)
                shutil.copyfile(entry, os.path.join(target, pkg + '.py'))
            else:
                root = tmp
                if len(tops) == 1:
                    root = os.path.join(tmp, list(tops)[0])
                initp = os.path.join(root, '__init__.py')
                modp = os.path.join(root, pkg + '.py')
                entry = initp if os.path.isfile(initp) else (modp if os.path.isfile(modp) else None)
                if entry is None:
                    return jsonify({'success': False,
                                    'message': '插件包内需要 __init__.py，或以插件 ID 命名的 %s.py' % pkg}), 400
                src = io.open(entry, encoding='utf-8').read()
                ok, err = validate_plugin_source(src)
                if not ok:
                    return jsonify({'success': False, 'message': err}), 400
                pyf = os.path.join(target, pkg + '.py')
                if os.path.isfile(pyf):
                    os.remove(pyf)
                dst = os.path.join(target, pkg)
                if os.path.isdir(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(root, dst)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        reregister_plugins()
        return jsonify({'success': True, 'plugin_id': pkg,
                        'message': '插件已安装：%s' % pkg,
                        'plugins': registry.list_for_admin()})

    return jsonify({'success': False, 'message': '仅支持 .py 或 .zip'}), 400


@bp.route('/api/plugins/<plugin_id>/delete', methods=['POST'])
@login_required
def api_plugin_delete(plugin_id):
    """删除用户自己放进去的插件（内置插件与随包插件不可删）。仅最高管理员。"""
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': '仅管理员可操作'}), 403
    target = plugin_dir()
    removed = []

    # 导入失败的插件以 broken_<文件名> 出现在列表里，这里还原真实文件名
    stem = plugin_id
    if stem.startswith('broken_'):
        stem = stem[len('broken_'):]
    cleaned = slugify_plugin_name(stem)
    if cleaned:
        stem = cleaned

    pyf = os.path.join(target, stem + '.py')
    pkgd = os.path.join(target, stem)
    if os.path.isfile(pyf):
        os.remove(pyf)
        removed.append(os.path.basename(pyf))
    if os.path.isdir(pkgd):
        shutil.rmtree(pkgd, ignore_errors=True)
        removed.append(stem + '/')
    if not removed:
        return jsonify({'success': False,
                        'message': '该插件不在用户插件目录中（内置或随包插件无法在此删除）'}), 400

    reregister_plugins()
    return jsonify({'success': True, 'message': '已删除：' + '、'.join(removed),
                    'plugins': registry.list_for_admin()})




@bp.route('/api/recent-books', methods=['GET'])
@login_required
def api_recent_books():
    """首页「最近阅读」小组件数据：最近读过的 N 本书及进度。"""
    rows = (ReadingProgress.query
            .filter_by(user_id=current_user.id)
            .order_by(ReadingProgress.updated_at.desc())
            .limit(8).all())
    out = []
    for rp in rows:
        b = Book.query.get(rp.book_id)
        if not b:
            continue
        out.append({
            'book_id': b.id,
            'title': b.title or b.filename,
            'progress': rp.progress or 0,
        })
    return jsonify({'success': True, 'books': out})


@bp.route('/api/random-book', methods=['GET'])
@login_required
def api_random_book():
    """首页「随便看看」小组件：随机一本书。"""
    b = Book.query.order_by(db.func.random()).first()
    if not b:
        return jsonify({'success': False})
    return jsonify({'success': True, 'book_id': b.id})


@bp.route('/api/recent-books/clear', methods=['POST'])
@login_required
def api_recent_books_clear():
    """清除当前用户的「最近阅读」记录。"""
    ReadingProgress.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({'success': True})


# ============ 管理员：书籍导入/导出/编码转换（仅 is_admin） ============
def _ensure_import_category():
    """确保存在一个顶层「导入」分类，返回其 id。"""
    root = Category.query.filter_by(parent_id=None).first()
    root_id = root.id if root else None
    cat = Category.query.filter_by(name='导入').first()
    if cat:
        return cat.id
    path = (root.get_full_path() + '/导入') if root else '导入'
    cat = Category(parent_id=root_id,
                  level=(root.level + 1) if root else 0,
                  name='导入', path=path, book_count=0)
    db.session.add(cat)
    db.session.commit()
    return cat.id


# ---- 导入增强（ /  批量压缩包、 自动分类） ----
def _expand_archive(f, tmp_root, kind='zip'):
    """展开压缩包，返回 [(成员名, 临时文件路径或 None, 分类提示)]。None 表示压缩包损坏。"""
    import io as _io, os, uuid, zipfile
    if kind == 'zip':
        try:
            opener = zipfile.ZipFile(_io.BytesIO(f.read()))
        except Exception:
            return [(f.filename, None, None)]
        names = opener.namelist()
    else:
        try:
            import rarfile
        except ImportError:
            return None
        try:
            opener = rarfile.RarFile(_io.BytesIO(f.read()))
        except Exception:
            return [(f.filename, None, None)]
        names = opener.namelist()
    items = []
    for name in names:
        if name.endswith('/') or name.endswith('\\'):
            continue
        parts = name.replace('\\', '/').split('/')
        cat_hint = parts[-2] if len(parts) >= 2 else None
        try:
            data = opener.read(name)
        except Exception:
            continue
        tmp = os.path.join(tmp_root, '%s_%s' % (kind, uuid.uuid4().hex))
        with open(tmp, 'wb') as fh2:
            fh2.write(data)
        items.append((parts[-1], tmp, cat_hint))
    opener.close()
    return items


def _match_category(raw_name, cat_hint):
    """ 根据文件名或压缩包子目录匹配已有分类（子串，取最长命中）。"""
    blob = ' '.join([s for s in (cat_hint, raw_name) if s]).lower()
    if not blob:
        return None
    best = None
    for c in Category.query.all():
        name = (c.name or '').strip().lower()
        if len(name) >= 2 and name in blob:
            if best is None or len(name) > len(best[0]):
                best = (name, c.id)
    return best[1] if best else None


# ---- 导入辅助：文件名解析作者与格式识别 ----
IMPORT_BLOCK_EXTS = {'.exe', '.sh', '.bat', '.cmd', '.com', '.dll', '.so',
                     '.msi', '.scr', '.app', '.deb', '.rpm'}
EXT_TYPE_MAP = {
    '.htm': 'html', '.html': 'html', '.mhtml': 'html', '.xhtml': 'html',
    '.azw': 'azw', '.azw3': 'azw3', '.md': 'md', '.markdown': 'md',
}


def parse_title_author(stem):
    """从文件名尽力解析书名与作者，解析不出作者时返回空串。"""
    s = (stem or '').strip()
    # 《书名》作者
    m = re.match(r'^\s*\u300a(.+?)\u300b\s*(.*)$', s)
    if m:
        return m.group(1).strip(), m.group(2).strip(' -_()[]\uff08\uff09')
    # 书名（作者） / 书名[作者]
    m = re.match(r'^(.+?)[\(\uff08\[\u3010]([^\)\uff09\]\u3011]{1,24})[\)\uff09\]\u3011]\s*$', s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # 书名 - 作者（后半段很短才当作作者，避免把副标题误判成人名）
    m = re.match(r'^(.{2,})[-\u2013\u2014_]\s*([^-\u2013\u2014_]{1,16})$', s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return s, ''


# ---- 导入跳过原因（失败可读原因）与上限 ----
SKIP_REASONS = {
    'blocked_type': '可执行文件，已拒绝导入',
    'archive_broken': '压缩包损坏或无法读取',
    'archive_unsupported': '服务端缺少该压缩格式的解压支持',
    'save_failed': '写入书库目录失败',
    'empty_file': '空文件（0 字节）',
    'too_large': '文件过大，超出单文件上限',
    'no_filename': '缺少文件名',
}
MAX_IMPORT_FILE_BYTES = 512 * 1024 * 1024      # 单文件 512MB
MAX_IMPORT_AUTO_CATEGORIES = 20                # 单次自动创建分类上限
MAX_IMPORT_LIST = 50                           # 结果里每条清单最多返回多少项


def _src_size(src):
    """取待导入文件的字节数（FileStorage 或临时文件路径）。"""
    try:
        if hasattr(src, 'stream'):
            pos = src.stream.tell()
            src.stream.seek(0, 2)
            size = src.stream.tell()
            src.stream.seek(pos)
            return size
        return os.path.getsize(src)
    except Exception:
        return -1


def _src_sniff(src, raw_name):
    """嗅探真实格式（FileStorage 或临时文件路径）。"""
    try:
        if hasattr(src, 'stream'):
            return file_types.sniff_stream(src.stream, raw_name)
        with open(src, 'rb') as fh:
            return file_types.sniff_stream(fh, raw_name)
    except Exception:
        return None


def _autocreate_category(hint, parent_id, created):
    """按压缩包内目录名自动创建分类（仅在开关打开时调用）。

    只取形态正常的目录名，长度 2~30，跳过通用目录名；单次上限 20 个，
    避免一个杂乱的压缩包把分类树灌满。
    """
    name = re.sub(r'[\r\n\t]+', ' ', (hint or '')).strip(' .-_')
    name = name.replace('/', '').replace('\\', '')
    if not (2 <= len(name) <= 30):
        return None
    if len(created) >= MAX_IMPORT_AUTO_CATEGORIES:
        return None
    if name.lower() in ('books', 'book', 'txt', 'text', 'epub', 'pdf', '小说',
                        '电子书', '新建文件夹', '下载', 'download', 'downloads'):
        return None
    if Category.query.filter_by(name=name).first():
        return None
    parent = Category.query.get(parent_id)
    if parent is None:
        return None
    try:
        cat = Category(parent_id=parent.id, level=(parent.level or 0) + 1, name=name,
                       path=((parent.path or parent.name) + '/' + name), book_count=0)
        db.session.add(cat)
        db.session.flush()
        created.append({'id': cat.id, 'name': name})
        return cat.id
    except Exception:
        db.session.rollback()
        return None


@bp.route('/api/admin/import', methods=['POST'])
@login_required
def admin_import_books():
    if not _require_cap('import'):
        return jsonify({'success': False, 'message': '无导入权限'}), 403
    files = request.files.getlist('files')
    if not files:
        return jsonify({'success': False, 'message': '未收到文件'}), 400
    auto_create = str(request.form.get('auto_create_category', '')).lower() in ('1', 'true', 'on', 'yes')
    import_cat_id = _ensure_import_category()
    books_dir = Path(current_app.config['BOOKS_DIR'])
    books_dir.mkdir(parents=True, exist_ok=True)
    imported = 0
    details = []
    skipped = []
    warnings = []
    created_categories = []
    affected_cats = {import_cat_id}

    def _skip(raw_name, code):
        skipped.append({'name': raw_name, 'code': code,
                        'reason': SKIP_REASONS.get(code, code)})

    import tempfile
    tmp_root = tempfile.mkdtemp(prefix='sr_imp_')
    expanded = []  # (raw_name, src, cat_hint)
    try:
        for f in files:
            if not f or not f.filename:
                continue
            ext = Path(f.filename).suffix.lower()
            if ext == '.zip':
                expanded.extend(_expand_archive(f, tmp_root, kind='zip'))
            elif ext == '.rar':
                r = _expand_archive(f, tmp_root, kind='rar')
                if r is None:
                    _skip(f.filename, 'archive_unsupported')
                else:
                    expanded.extend(r)
            else:
                expanded.append((f.filename, f, None))

        for raw_name, src, cat_hint in expanded:
            if src is None:
                _skip(raw_name, 'archive_broken')
                continue
            if not raw_name:
                _skip(raw_name, 'no_filename')
                continue
            ext = Path(raw_name).suffix.lower()
            #  自动识别格式：危险/可执行类型一律拒绝
            if ext in IMPORT_BLOCK_EXTS:
                _skip(raw_name, 'blocked_type')
                continue
            #  失败可读原因：空文件与超大文件在入库前就拦下，不再产生 0 字节书
            size = _src_size(src)
            if size == 0:
                _skip(raw_name, 'empty_file')
                continue
            if size > MAX_IMPORT_FILE_BYTES:
                _skip(raw_name, 'too_large')
                continue
            #  格式识别：按文件内容（而非扩展名）确认真实格式
            sniffed = _src_sniff(src, raw_name)
            file_type, fmt_warn = file_types.resolve_format(ext, sniffed)
            if ext not in file_types.EXT_TYPE_MAP and not sniffed:
                warnings.append({'name': raw_name, 'code': 'unknown_type',
                                 'detail': '扩展名 %s 不在支持格式清单内，且无法从内容判定真实格式，'
                                           '将按其名称作为格式入库，可能无法在阅读器中打开'
                                           % (ext or '（无）')})
            if fmt_warn:
                warnings.append({'name': raw_name, 'code': 'format_mismatch',
                                 'detail': fmt_warn})

            #  导入自动分类：文件名 / 压缩包子目录命中已有分类则归入
            cat_id = _match_category(raw_name, cat_hint)
            if cat_id is None and auto_create and cat_hint:
                cat_id = _autocreate_category(cat_hint, import_cat_id, created_categories)
            if cat_id is None:
                cat_id = import_cat_id
            affected_cats.add(cat_id)
            safe = re.sub(r'[^\w\-.一-鿿]+', '_', raw_name)
            if not safe:
                safe = 'book_%d' % (imported + 1)
            target = books_dir / safe
            if target.exists():
                stem, ext2 = target.stem, target.suffix
                i = 1
                while (books_dir / (stem + '_' + str(i) + ext2)).exists():
                    i += 1
                target = books_dir / (stem + '_' + str(i) + ext2)
            try:
                if hasattr(src, 'save'):
                    src.save(str(target))
                else:
                    import shutil
                    shutil.copyfile(src, str(target))
            except Exception:
                _skip(raw_name, 'save_failed')
                continue
            #  自动识别作者：必须用「原始文件名」解析
            title, author = parse_title_author(Path(raw_name).stem)
            b = Book(filename=target.name, relative_path=target.name, title=title,
                     author=author or None, file_type=file_type,
                     category_id=cat_id, upload_date=datetime.utcnow())
            # 补全入库字段：首字母归类键决定首页字母筛选（Book.initial），
            # 文件大小决定首页排序与详情页显示，导入路径此前两者都没写。
            try:
                b.file_size = target.stat().st_size
            except Exception:
                b.file_size = None
            b.initial = BookUtils.get_title_initial(title)
            b.modified_time = datetime.utcnow()
            db.session.add(b)
            imported += 1
            details.append({'name': raw_name, 'title': title,
                            'author': author, 'type': file_type,
                            'detected': sniffed or ext.lstrip('.')})
        db.session.commit()
    finally:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)

    #  导入后更新相关分类计数（含自动分类命中的分类）
    try:
        for cid in affected_cats:
            cat = Category.query.get(cid)
            if cat:
                cat.book_count = Book.query.filter_by(category_id=cat.id).count()
        db.session.commit()
    except Exception:
        db.session.rollback()

    #  失败可读原因：按原因归并计数，前端可直接分组展示
    summary = {}
    for item in skipped:
        slot = summary.setdefault(item['code'], {'code': item['code'],
                                                 'reason': item['reason'], 'count': 0})
        slot['count'] += 1
    skipped_summary = sorted(summary.values(), key=lambda x: -x['count'])

    if imported or created_categories:
        log_action(current_user, '导入书籍',
                   '%d 本，跳过 %d 个，新增分类 %d 个'
                   % (imported, len(skipped), len(created_categories)))

    return jsonify({'success': True, 'imported': imported,
                    'skipped': skipped[:MAX_IMPORT_LIST],
                    'skipped_total': len(skipped),
                    'skipped_summary': skipped_summary,
                    'warnings': warnings[:MAX_IMPORT_LIST],
                    'warning_total': len(warnings),
                    'created_categories': created_categories,
                    'details': details[:MAX_IMPORT_LIST],
                    'details_total': len(details)})


@bp.route('/api/admin/import/formats', methods=['GET'])
@login_required
def admin_import_formats():
    """支持格式清单（后端单一来源，避免前后端两套硬编码走偏）。"""
    if not _require_cap('import'):
        return jsonify({'success': False, 'message': '无导入权限'}), 403
    return jsonify({'success': True,
                    'formats': file_types.importable_formats(),
                    'accept': file_types.accept_attribute(),
                    'max_file_bytes': MAX_IMPORT_FILE_BYTES,
                    'max_auto_categories': MAX_IMPORT_AUTO_CATEGORIES})


@bp.route('/api/admin/export/<int:book_id>')
@login_required
def admin_export_book(book_id):
    if not _require_cap('export'):
        return jsonify({'success': False, 'message': '无导出权限'}), 403
    book = Book.query.get_or_404(book_id)
    file_path = Path(current_app.config['BOOKS_DIR']) / book.relative_path
    if not file_path.exists():
        file_path = Path(current_app.config['BOOKS_DIR']) / book.filename
    if not file_path.exists():
        return jsonify({'success': False, 'message': '文件不存在'}), 404
    log_action(current_user, '导出书籍', '《%s》(%s)' % (book.title or book.filename,
                                                     book.file_type or ''))
    return send_file(file_path, as_attachment=True)


MAX_EXPORT_BOOKS = 100                              # 单次打包导出的书籍数上限
MAX_EXPORT_BYTES = 500 * 1024 * 1024                 # 单次打包导出的原始大小上限


@bp.route('/api/admin/export-batch', methods=['POST'])
@login_required
def admin_export_batch():
    """批量导出：把选中的书打包成 ZIP（可选带元数据 CSV 与封面）。

    先落临时文件再发送，因此 Content-Length 已知，浏览器可显示真实下载进度；
    响应结束后立即删除临时文件，不在书库目录留任何产物。
    """
    if not _require_cap('export'):
        return jsonify({'success': False, 'message': '无导出权限'}), 403
    data = request.get_json(silent=True) or {}
    raw_ids = data.get('book_ids') or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({'success': False, 'message': '未选择要导出的书籍'}), 400
    book_ids = []
    for x in raw_ids:
        try:
            bid = int(x)
        except (TypeError, ValueError):
            continue
        if bid not in book_ids:
            book_ids.append(bid)
    if len(book_ids) > MAX_EXPORT_BOOKS:
        return jsonify({'success': False,
                        'message': '一次最多导出 %d 本，当前选了 %d 本'
                                   % (MAX_EXPORT_BOOKS, len(book_ids))}), 400
    with_meta = bool(data.get('with_meta', True))
    with_cover = bool(data.get('with_cover', False))

    books = Book.query.filter(Book.id.in_(book_ids)).all()
    if not books:
        return jsonify({'success': False, 'message': '选中的书籍不存在'}), 404
    books.sort(key=lambda b: book_ids.index(b.id))

    books_dir = Path(current_app.config['BOOKS_DIR'])
    static_dir = Path(current_app.static_folder or 'static')
    missing = []
    planned = []          # (book, 磁盘绝对路径)
    total_bytes = 0
    for b in books:
        fp = books_dir / (b.relative_path or b.filename)
        if not fp.exists():
            fp = books_dir / b.filename
        if not fp.exists():
            missing.append({'id': b.id, 'title': b.title or b.filename})
            continue
        planned.append((b, fp))
        try:
            total_bytes += fp.stat().st_size
        except Exception:
            pass
    if not planned:
        return jsonify({'success': False,
                        'message': '选中的书籍在磁盘上都找不到文件'}), 404
    if total_bytes > MAX_EXPORT_BYTES:
        return jsonify({'success': False,
                        'message': '选中书籍合计 %s，超出单次导出上限 %s'
                                   % (BookUtils.format_file_size(total_bytes),
                                      BookUtils.format_file_size(MAX_EXPORT_BYTES))}), 400

    import csv
    import uuid
    tmp_path = os.path.join(tempfile.gettempdir(), 'sr_exp_%s.zip' % uuid.uuid4().hex)
    used = set()
    exported = 0
    with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:

        def _arcname(name):
            # 保留原文件名（用户导出后拿回的就是自己的书），只去掉路径与控制字符
            clean = (name or '').replace('\\', '/').split('/')[-1]
            clean = re.sub(r'[\x00-\x1f]+', '', clean).strip()
            if clean in ('', '.', '..'):
                clean = 'book'
            if clean not in used:
                used.add(clean)
                return clean
            stem, ext2 = os.path.splitext(clean)
            i = 2
            while ('%s_%d%s' % (stem, i, ext2)) in used:
                i += 1
            clean = '%s_%d%s' % (stem, i, ext2)
            used.add(clean)
            return clean

        #  元数据（导出包含元数据）
        if with_meta:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(['id', 'title', 'author', 'filename', 'format', 'size_bytes',
                        'category_id', 'tags', 'upload_date'])
            for b, _fp in planned:
                w.writerow([b.id, b.title or '', b.author or '', b.filename,
                            b.file_type or '', b.file_size or 0, b.category_id or '',
                            b.tags or '',
                            b.upload_date.strftime('%Y-%m-%d %H:%M:%S') if b.upload_date else ''])
            zf.writestr('books_metadata.csv', '\ufeff' + buf.getvalue())

        for b, fp in planned:
            arc = _arcname(b.filename or fp.name)
            try:
                zf.write(str(fp), arcname=arc)
            except Exception as e:
                missing.append({'id': b.id, 'title': b.title or b.filename,
                                'reason': '打包失败: %s' % e})
                continue
            exported += 1
            #  导出包含封面
            if with_cover:
                cp = (b.cover_path or '').strip()
                if cp:
                    cover_src = Path(cp) if os.path.isabs(cp) else (static_dir / cp)
                    try:
                        if cover_src.exists():
                            zf.write(str(cover_src),
                                     arcname='covers/' + _arcname(cover_src.name))
                    except Exception:
                        pass

        zf.writestr('README.txt',
                    '书籍批量导出\n'
                    '书籍 %d 个%s\n'
                    'books_metadata.csv 为随包元数据（若已勾选）。\n'
                    % (exported, '，封面在 covers/ 目录' if with_cover else ''))

    try:
        size = os.path.getsize(tmp_path)
    except Exception:
        size = 0
    if size <= 0:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return jsonify({'success': False, 'message': '打包失败，未生成任何内容'}), 500

    from flask import after_this_request

    @after_this_request
    def _cleanup(resp):
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return resp

    log_action(current_user, '批量导出书籍',
               '%d 本打包为 ZIP（%s）' % (exported, BookUtils.format_file_size(size)))
    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    resp = send_file(tmp_path, as_attachment=True, mimetype='application/zip',
                     download_name='books_export_%s.zip' % stamp)
    return resp


@bp.route('/api/admin/convert-encoding', methods=['POST'])
@login_required
def admin_convert_encoding():
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    data = request.get_json(silent=True) or {}
    book_id = data.get('book_id')
    if not book_id:
        return jsonify({'success': False, 'message': '缺少 book_id'}), 400
    book = Book.query.get_or_404(book_id)
    file_path = Path(current_app.config['BOOKS_DIR']) / book.relative_path
    if not file_path.exists():
        file_path = Path(current_app.config['BOOKS_DIR']) / book.filename
    if not file_path.exists():
        return jsonify({'success': False, 'message': '文件不存在'}), 404
    try:
        with open(file_path, 'rb') as fh:
            raw = fh.read()
    except Exception as e:
        return jsonify({'success': False, 'message': '读取失败: %s' % e}), 500
    raw = fix_bom(raw)
    encoding = detect_encoding(raw)
    try:
        text = raw.decode(encoding)
    except Exception:
        text = raw.decode(encoding, errors='replace')
    before = text[:200]
    try:
        with open(file_path, 'wb') as fh:
            fh.write(text.encode('utf-8'))
        changed = encoding.lower() not in ('utf-8', 'utf8', 'ascii')
        return jsonify({'success': True, 'encoding': encoding, 'changed': changed,
                        'before': before, 'after': text[:200]})
    except Exception as e:
        return jsonify({'success': False, 'message': '写入失败: %s' % e}), 500


@bp.route('/admin/books')
@login_required
def admin_books():
    if not (current_user.is_admin or current_user.is_sub_admin):
        flash('仅管理员或被授权的子管理员可访问', 'error')
        return redirect(url_for('main.library'))
    page = request.args.get('page', 1, type=int)
    q = request.args.get('q', '').strip()
    query = Book.query
    if q:
        query = query.filter(Book.title.ilike('%' + q + '%') | Book.filename.ilike('%' + q + '%'))
    pagination = query.order_by(Book.id.desc()).paginate(page=page, per_page=30, error_out=False)
    return render_template('admin_books.html', books=pagination.items, page=page,
                           total=pagination.total, q=q,
                           import_formats=file_types.importable_formats(),
                           import_accept=file_types.accept_attribute(),
                           categories=Category.query.order_by(Category.path).all())

# ============ 书籍管理增强（ 信息编辑 /   查重去重） ============
@bp.route('/api/admin/book/<int:book_id>', methods=['GET', 'POST'])
@login_required
def admin_book_detail(book_id):
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    book = Book.query.get_or_404(book_id)
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        author = (data.get('author') or '').strip()
        desc = (data.get('description') or '').strip()
        file_type = (data.get('file_type') or '').strip().lower()
        cat_id = data.get('category_id')
        move_msg = ''
        if title:
            book.title = title
        book.author = author or None
        book.description = desc or None
        if file_type:
            book.file_type = file_type
        if cat_id is not None:
            try:
                cid = int(cat_id)
            except Exception:
                cid = None
            new_cid = cid if cid else None
            if data.get('move_files') and new_cid != book.category_id:
                tgt = Category.query.get(new_cid) if new_cid else None
                if new_cid and not tgt:
                    return jsonify({'success': False, 'message': '目标分类不存在'}), 404
                mok, move_msg, _mskip = _move_book_file_to_category(book, tgt)
                if not mok:
                    return jsonify({'success': False, 'message': move_msg}), 500
            book.category_id = new_cid
        db.session.commit()
        try:
            if book.category_id:
                cat = Category.query.get(book.category_id)
                if cat:
                    cat.book_count = Book.query.filter_by(category_id=cat.id).count()
                    db.session.commit()
        except Exception:
            db.session.rollback()
        log_action(current_user, '编辑书籍信息', '《%s》' % (book.title or book.filename))
        return jsonify({'success': True, 'message': move_msg})
    cats = Category.query.order_by(Category.name).all()
    return jsonify({'success': True, 'book': {
        'id': book.id, 'title': book.title, 'author': book.author,
        'description': book.description, 'file_type': book.file_type,
        'category_id': book.category_id, 'filename': book.filename,
        'cover_path': book.cover_path,
    }, 'categories': [{'id': c.id, 'name': c.name} for c in cats]})


@bp.route('/api/admin/books/duplicates')
@login_required
def admin_book_duplicates():
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    # 查重去重（内容相似度版）：
    #   1) 先按元数据(书名+作者 / 文件名)做精确分组（相似度 1.0，免读文件）；
    #   2) 再按 (文件类型, 体积分桶) 做候选聚类，对候选对提取正文计算
    #      Jaccard 相似度，>= threshold 即判定为重复。相同内容体积一致 -> 相似度 1.0。
    #   threshold 默认 0.9（前端可选 0.95）。
    try:
        threshold = float(request.args.get('threshold', 0.9))
    except Exception:
        threshold = 0.9
    threshold = max(0.5, min(1.0, threshold))

    import zipfile
    import html as _html_mod
    from collections import defaultdict

    _WORD = re.compile(r'\w+', re.UNICODE)

    def _norm(t):
        return re.sub(r'\s+', ' ', (t or '').lower()).strip()

    _BK_BASE = current_app.config['BOOKS_DIR']

    def _book_text(book):
        try:
            base = _BK_BASE
            p = os.path.join(base, book.relative_path or book.filename)
            if not os.path.exists(p):
                p = os.path.join(base, book.filename)
            if not os.path.exists(p):
                return ''
            ext = (book.file_type or '').lower() or os.path.splitext(p)[1].lower().lstrip('.')
            # 相似度判定取正文前 50KB 已足够（重复书的正文开头必然高度一致）；
            # 原 200KB 让每对候选的文件读取量与 4-gram 构造成本都放大 4 倍，
            # 在上限 3000 对的规模下，这是查重耗时的主要来源。
            MAX = 50000
            if ext in ('txt', 'md', 'text'):
                with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                    return f.read(MAX)
            if ext == 'epub':
                try:
                    z = zipfile.ZipFile(p)
                    parts = []
                    for n in z.namelist():
                        if n.lower().endswith(('.xhtml', '.html', '.htm', '.xml')):
                            try:
                                parts.append(_html_mod.unescape(z.read(n).decode('utf-8', 'ignore')))
                            except Exception:
                                continue
                    return re.sub(r'<[^>]+>', ' ', ' '.join(parts))[:MAX]
                except Exception:
                    return ''
            if ext == 'pdf':
                try:
                    from pypdf import PdfReader
                except Exception:
                    try:
                        from PyPDF2 import PdfReader
                    except Exception:
                        return ''
                try:
                    r = PdfReader(p)
                    chunks = []
                    for pg in r.pages[:60]:
                        try:
                            chunks.append(pg.extract_text() or '')
                        except Exception:
                            pass
                    return ' '.join(chunks)[:MAX]
                except Exception:
                    return ''
            if ext == 'docx':
                try:
                    from docx import Document
                    d = Document(p)
                    return ' '.join(par.text for par in d.paragraphs)[:MAX]
                except Exception:
                    return ''
        except Exception:
            return ''
        return ''

    def _sim(a, b):
        if not a or not b:
            return 0.0
        def grams(t, k=4):
            w = _WORD.findall(t)
            if len(w) < k:
                return frozenset((' '.join(w),))
            return frozenset(' '.join(w[i:i + k]) for i in range(len(w) - k + 1))
        ga, gb = grams(a), grams(b)
        if not ga or not gb:
            return 0.0
        inter = len(ga & gb)
        union = len(ga | gb)
        return inter / union if union else 0.0

    # 性能优化：原生 SQL 直接取，避免 ORM 行包装 + 全量字典复制（原 ~5.6s）
    from sqlalchemy import text as _sa_text
    _raw = db.session.execute(_sa_text(
        "SELECT id, title, author, filename, relative_path, file_type, "
        "COALESCE(file_size,0), category_id FROM book"
    )).fetchall()
    book_by_id = {}
    for _r in _raw:
        _rec = {'id': _r[0], 'title': _r[1], 'author': _r[2], 'filename': _r[3],
                'relative_path': _r[4], 'file_type': _r[5], 'file_size': _r[6],
                'category_id': _r[7]}
        book_by_id[_rec['id']] = _rec
    books = list(book_by_id.values())

    groups = []
    exact_ids = set()
    ta_map = defaultdict(list)
    fn_map = defaultdict(list)
    for b in books:
        ta_map[(b['title'] or '', b['author'] or '')].append(b)
        fn_map[b['filename']].append(b)
    for (title, author), members in ta_map.items():
        if title and len(members) > 1:
            groups.append({'title': title, 'author': author or '', 'count': len(members),
                           'similarity': 1.0, 'books': members})
            exact_ids.update(m['id'] for m in members)
    for fn, members in fn_map.items():
        if len(members) > 1:
            groups.append({'title': fn, 'author': '', 'count': len(members),
                           'similarity': 1.0, 'books': members})
            exact_ids.update(m['id'] for m in members)

    truncated = False
    if threshold < 1.0:
        buckets = defaultdict(list)
        for b in books:
            if b['id'] in exact_ids or not b['file_type']:
                continue
            buckets[(b['file_type'], b['file_size'] // 20000)].append(b)
        parent = {}
        def find(x):
            while parent.get(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        # 并行预取候选文件文本（文件 IO 密集，线程池显著提速；子线程无 app context，用闭包 _BK_BASE）
        from concurrent.futures import ThreadPoolExecutor as _TPE
        def _read_one(bid):
            bk = book_by_id.get(bid)
            if not bk:
                return bid, ''
            return bid, _norm(_book_text(bk))
        # 仅收集前 MAX_PAIRS 对涉及的候选书（与原截断语义一致，避免全量读 5 万本）
        _MAX_PAIRS = 3000
        _need = set()
        _pairs = 0
        for _key, _mem in buckets.items():
            if len(_mem) < 2:
                continue
            for _i in range(len(_mem)):
                for _j in range(_i + 1, len(_mem)):
                    _pairs += 1
                    if _pairs > _MAX_PAIRS:
                        break
                    _need.add(_mem[_i]['id'])
                    _need.add(_mem[_j]['id'])
                if _pairs > _MAX_PAIRS:
                    break
            if _pairs > _MAX_PAIRS:
                break
        _text_cache = {}
        if _need:
            with _TPE(max_workers=4) as _ex:
                for _bid, _t in _ex.map(_read_one, _need):
                    _text_cache[_bid] = _t
        def get_text(bid):
            return _text_cache.get(bid, '')
        MAX_PAIRS = 3000
        pairs = 0
        for key, members in buckets.items():
            if len(members) < 2:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    pairs += 1
                    if pairs > MAX_PAIRS:
                        truncated = True
                        break
                    a, b = members[i], members[j]
                    ta, tb = get_text(a['id']), get_text(b['id'])
                    if not ta or not tb:
                        continue
                    if abs(len(ta) - len(tb)) > max(len(ta), len(tb)) * 0.5:
                        continue
                    if _sim(ta, tb) >= threshold:
                        union(a['id'], b['id'])
                if truncated:
                    break
            if truncated:
                break
        comp = defaultdict(list)
        for b in books:
            if b['id'] in exact_ids:
                continue
            comp[find(b['id'])].append(b)
        for root, members in comp.items():
            if len(members) > 1:
                groups.append({'title': members[0]['title'] or members[0]['filename'],
                               'author': members[0]['author'] or '', 'count': len(members),
                               'similarity': round(threshold, 2), 'books': members})
                exact_ids.update(m['id'] for m in members)

    auto_select = []
    for g in groups:
        if g['count'] < 2:
            continue
        best = max(g['books'], key=lambda m: (m['file_size'], -m['id']))
        for m in g['books']:
            if m['id'] != best['id']:
                auto_select.append(m['id'])

    return jsonify({
        'success': True,
        'groups': [{
            'title': g['title'], 'author': g['author'], 'count': g['count'],
            'similarity': g['similarity'],
            'books': [{'id': m['id'], 'title': m['title'], 'author': m['author'],
                       'filename': m['filename'], 'category_id': m['category_id'],
                       'file_size': m['file_size']} for m in g['books']],
        } for g in groups],
        'total_groups': len(groups),
        'auto_select_ids': auto_select,
        'threshold': threshold,
        'truncated': truncated,
    })



def _purge_book(book):
    """彻底删除一本书：磁盘正文文件 + 封面 + 阅读器数据（阅读进度 / 书签）+ 数据库记录。

    返回执行报告，前端据此提示用户「文件到底删掉了没有」，避免静默留下孤儿文件。
    """
    report = {'file_path': '', 'file_deleted': False, 'file_missing': False,
              'cover_deleted': False, 'progress_removed': 0, 'bookmarks_removed': 0,
              'errors': []}
    books_dir = str(current_app.config['BOOKS_DIR'])

    # 1) 正文文件
    try:
        rel = (book.relative_path or book.filename or '').strip()
        path = rel if os.path.isabs(rel) else os.path.join(books_dir, rel)
        report['file_path'] = path
        if os.path.exists(path):
            os.remove(path)
            report['file_deleted'] = not os.path.exists(path)
            if not report['file_deleted']:
                report['errors'].append('磁盘文件删除失败：%s' % path)
        else:
            report['file_missing'] = True
    except Exception as e:
        report['errors'].append('磁盘文件删除异常：%s' % e)

    # 2) 封面文件（cover_path 相对 static 存放，也兼容绝对路径）
    try:
        cp = (book.cover_path or '').strip()
        if cp:
            base = str(current_app.static_folder or 'static')
            cand = cp if os.path.isabs(cp) else os.path.join(base, cp)
            if os.path.exists(cand):
                os.remove(cand)
                report['cover_deleted'] = True
    except Exception as e:
        report['errors'].append('封面删除异常：%s' % e)

    # 3) 阅读器数据：阅读进度 + 书签
    try:
        report['progress_removed'] = ReadingProgress.query.filter_by(
            book_id=book.id).delete(synchronize_session=False)
        report['bookmarks_removed'] = Bookmark.query.filter_by(
            book_id=book.id).delete(synchronize_session=False)
    except Exception as e:
        report['errors'].append('阅读数据清理异常：%s' % e)

    # 4) 数据库记录
    db.session.delete(book)
    _invalidate_tags_cache()  # 删书会改变标签计数
    return report


def _refresh_category_count(cat_id):
    if not cat_id:
        return
    try:
        cat = Category.query.get(cat_id)
        if cat:
            cat.book_count = Book.query.filter_by(category_id=cat.id).count()
    except Exception:
        db.session.rollback()


@bp.route('/api/admin/book/<int:book_id>/delete', methods=['POST'])
@login_required
def admin_delete_book(book_id):
    """删除单本：磁盘文件、封面、阅读进度、书签一并清理（不再静默吞异常）。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    book = Book.query.get_or_404(book_id)
    cat_id = book.category_id
    title = book.title or book.filename
    report = _purge_book(book)
    db.session.commit()
    _refresh_category_count(cat_id)
    db.session.commit()
    state = '已删' if report['file_deleted'] else ('本就不存在' if report['file_missing'] else '删除失败')
    log_action(current_user, '删除书籍', '《%s》（磁盘文件%s）' % (title, state))
    return jsonify({'success': True, 'report': report,
                    'message': ('已彻底删除《%s》' % title) if not report['errors']
                               else '记录已删除，但存在异常：' + '；'.join(report['errors'])})


# ============ 书籍管理深度增强（ 封面 /  合并 /  自定义分类 /  失效清理） ============
@bp.route('/api/admin/book/<int:book_id>/cover', methods=['POST', 'DELETE'])
@login_required
def admin_book_cover(book_id):
    """ 自定义封面：上传图片覆盖自动抽取的封面；DELETE 清除封面记录。
    存的是相对 static 的路径（如 covers/custom_12.jpg），前端直接用 /covers/xxx 访问。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    book = Book.query.get_or_404(book_id)
    base = Path(current_app.static_folder or 'static')
    cover_dir = base / 'covers'
    cover_dir.mkdir(parents=True, exist_ok=True)
    if request.method == 'DELETE':
        old = book.cover_path
        book.cover_path = None
        db.session.commit()
        if old:
            try:
                p = old if old.startswith('/') else str(base / old)
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        log_action(current_user, '清除书籍封面', '《%s》' % (book.title or book.filename))
        return jsonify({'success': True})
    f = request.files.get('cover')
    if not f or not f.filename:
        return jsonify({'success': False, 'message': '请选择图片文件'}), 400
    ext = (f.filename.rsplit('.', 1)[-1] or '').lower()
    if ext not in ('jpg', 'jpeg', 'png', 'webp', 'gif'):
        return jsonify({'success': False, 'message': '仅支持 jpg / png / webp / gif'}), 400
    data = f.read()
    if len(data) > 5 * 1024 * 1024:
        return jsonify({'success': False, 'message': '图片需小于 5MB'}), 400
    rel = 'covers/custom_%d.%s' % (book.id, 'jpg' if ext == 'jpeg' else ext)
    with open(base / rel, 'wb') as fp:
        fp.write(data)
    book.cover_path = rel
    db.session.commit()
    log_action(current_user, '上传书籍封面', '《%s》' % (book.title or book.filename))
    return jsonify({'success': True, 'cover_path': rel})


@bp.route('/api/admin/books/merge', methods=['POST'])
@login_required
def admin_books_merge():
    """ 书籍合并：把多本纯文本（txt/md）顺序拼成一本新书并入库。
    安全策略：① 源文件默认保留，只有显式 delete_sources=true 才删除；
    ② 流式分块读写，避免大文件一次性进内存把容器撑爆；③ 限制数量与类型。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in (data.get('ids') or []) if str(i).isdigit()]
    ids = list(dict.fromkeys(ids))
    delete_sources = bool(data.get('delete_sources'))
    if len(ids) < 2:
        return jsonify({'success': False, 'message': '请至少选择 2 本书'}), 400
    if len(ids) > 20:
        return jsonify({'success': False, 'message': '一次最多合并 20 本'}), 400
    books = Book.query.filter(Book.id.in_(ids)).order_by(Book.id.asc()).all()
    if len(books) != len(ids):
        return jsonify({'success': False, 'message': '存在无效书籍 ID'}), 400
    bad = [b.filename for b in books if (b.file_type or '').lower() not in ('txt', 'md')]
    if bad:
        return jsonify({'success': False,
                        'message': '仅支持 txt / md，不支持的书：' + '、'.join(bad[:3])}), 400
    books_dir = current_app.config['BOOKS_DIR']
    title = (data.get('title') or '').strip() or ('%s（合并）' % (books[0].title or books[0].filename))
    first_abs = os.path.join(str(books_dir), books[0].relative_path or books[0].filename)
    out_dir = os.path.dirname(first_abs)
    if not os.path.isdir(out_dir):
        return jsonify({'success': False, 'message': '目标目录不存在'}), 500
    safe = re.sub(r'[\\/:*?"<>|]', '_', title)[:80]
    out_path = None
    for i in range(1, 100):
        cand = os.path.join(out_dir, '%s%s.txt' % (safe, '' if i == 1 else '_%d' % i))
        if not os.path.exists(cand):
            out_path = cand
            break
    if not out_path:
        return jsonify({'success': False, 'message': '无法生成合并文件路径'}), 500
    total = 0
    written = 0
    try:
        with open(out_path, 'wb') as out:
            for b in books:
                src = os.path.join(str(books_dir), b.relative_path or b.filename)
                if not os.path.exists(src):
                    continue
                written += 1
                out.write(('\n\n===== %s =====\n\n' % (b.title or b.filename)).encode('utf-8'))
                with open(src, 'rb') as fin:
                    while True:
                        chunk = fin.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        total += len(chunk)
                out.write(b'\n')
    except Exception as e:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return jsonify({'success': False, 'message': '合并失败：%s' % e}), 500
    if written < 2:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return jsonify({'success': False, 'message': '可读源文件不足 2 个，未生成合并书'}), 400
    try:
        rel = os.path.relpath(out_path, str(books_dir))
    except Exception:
        rel = os.path.basename(out_path)
    nb = Book(filename=os.path.basename(out_path), title=title,
              author=books[0].author,
              description='由 %d 本书合并生成' % written,
              file_type='txt', file_size=total, relative_path=rel,
              initial=books[0].initial, category_id=books[0].category_id)
    db.session.add(nb)
    db.session.commit()
    removed = 0
    if delete_sources:
        for b in books:
            src = os.path.join(str(books_dir), b.relative_path or b.filename)
            try:
                if os.path.exists(src):
                    os.remove(src)
            except Exception:
                pass
            db.session.delete(b)
            removed += 1
        db.session.commit()
        try:
            if nb.category_id:
                cat = Category.query.get(nb.category_id)
                if cat:
                    cat.book_count = Book.query.filter_by(category_id=cat.id).count()
                    db.session.commit()
        except Exception:
            db.session.rollback()
    log_action(current_user, '合并书籍', '%d 本 → 《%s》' % (written, title))
    return jsonify({'success': True, 'book_id': nb.id, 'title': title,
                    'size': total, 'merged': written, 'removed_sources': removed})


@bp.route('/api/admin/category/create', methods=['POST'])
@login_required
def admin_category_create():
    """ 用户自定义分类：创建纯逻辑归类容器（不动磁盘）。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无权限'}), 403
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': '分类名不能为空'}), 400
    if '/' in name or '\\' in name:
        return jsonify({'success': False, 'message': '分类名不能包含斜杠'}), 400
    parent_id = data.get('parent_id')
    parent = Category.query.get(parent_id) if parent_id else None
    if parent_id and not parent:
        return jsonify({'success': False, 'message': '父分类不存在'}), 400
    path = (parent.path + '/' + name) if parent else name
    if Category.query.filter_by(path=path).first():
        return jsonify({'success': False, 'message': '同名分类已存在'}), 400
    cat = Category(name=name, path=path, parent_id=parent.id if parent else None,
                   level=(parent.level + 1) if parent else 1,
                   book_count=0, sort_order=0)
    db.session.add(cat)
    db.session.commit()
    log_action(current_user, '新建分类', name)
    return jsonify({'success': True, 'category': {'id': cat.id, 'name': cat.name,
                                                  'path': cat.path, 'level': cat.level}})


@bp.route('/api/admin/category/<int:cat_id>/delete', methods=['POST'])
@login_required
def admin_category_delete(cat_id):
    """ 配套：只允许删除空分类（无书、无子分类），避免误删真实目录数据。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无权限'}), 403
    cat = Category.query.get_or_404(cat_id)
    if (cat.book_count or 0) > 0 or Category.query.filter_by(parent_id=cat.id).count() > 0:
        return jsonify({'success': False, 'message': '该分类下仍有书籍或子分类，不能删除'}), 400
    db.session.delete(cat)
    db.session.commit()
    log_action(current_user, '删除分类', cat.name)
    return jsonify({'success': True})


@bp.route('/api/admin/books/invalid')
@login_required
def admin_books_invalid():
    """ 失效书籍扫描：磁盘文件已丢失但数据库仍存在的记录（手动挪走文件后的残留）。
    全量 stat 很慢，这里按 offset/limit 分段扫描，返回 has_more 供前端继续。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    offset = request.args.get('offset', 0, type=int)
    limit = min(request.args.get('limit', 300, type=int), 1000)
    rows = (db.session.query(Book.id, Book.title, Book.filename, Book.relative_path, Book.file_type)
            .order_by(Book.id.asc()).offset(offset).limit(limit).all())
    books_dir = str(current_app.config['BOOKS_DIR'])
    bad = []
    for bid, title, fn, rel, ft in rows:
        p = os.path.join(books_dir, rel or fn)
        if not os.path.exists(p):
            bad.append({'id': bid, 'title': title or fn, 'filename': fn,
                        'file_type': ft or ''})
    scanned = len(rows)
    return jsonify({'success': True, 'books': bad, 'scanned': scanned,
                    'offset': offset, 'limit': limit,
                    'has_more': scanned == limit,
                    'next_offset': offset + scanned})


@bp.route('/api/admin/books/invalid/delete', methods=['POST'])
@login_required
def admin_books_invalid_delete():
    """ 清理失效记录：只删数据库记录（因其文件本就不存在）。
    二次确认源文件确实不存在才删，绝不删除磁盘上真实存在的文件。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in (data.get('ids') or []) if str(i).isdigit()]
    if not ids:
        return jsonify({'success': False, 'message': '未选择书籍'}), 400
    if len(ids) > 500:
        return jsonify({'success': False, 'message': '一次最多清理 500 条'}), 400
    books = Book.query.filter(Book.id.in_(ids)).all()
    books_dir = str(current_app.config['BOOKS_DIR'])
    n = 0
    for b in books:
        p = os.path.join(books_dir, b.relative_path or b.filename)
        if os.path.exists(p):
            continue
        db.session.delete(b)
        n += 1
    db.session.commit()
    log_action(current_user, '清理失效书籍', '%d 条' % n)
    return jsonify({'success': True, 'removed': n})


@bp.route('/api/admin/books/batch', methods=['POST'])
@login_required
def admin_books_batch():
    """ 批量操作：delete / move / tag。删除会连同磁盘文件一并清理；移动只改数据库归属。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in (data.get('ids') or []) if str(i).isdigit()]
    action = data.get('action')
    if not ids or action not in ('delete', 'move', 'tag'):
        return jsonify({'success': False, 'message': '参数不完整'}), 400
    if len(ids) > 500:
        return jsonify({'success': False, 'message': '单次最多 500 本'}), 400
    import os
    ok = 0
    failed = []
    touched = set()
    files_deleted = 0
    files_failed = []
    files_moved = 0
    files_skipped = 0
    if action == 'delete':
        for bid in ids:
            b = Book.query.get(bid)
            if not b:
                failed.append(bid); continue
            if b.category_id:
                touched.add(b.category_id)
            # 连磁盘文件 / 封面 / 阅读进度 / 书签一起清掉
            rep = _purge_book(b)
            if rep['file_deleted']:
                files_deleted += 1
            elif rep['errors']:
                files_failed.append(bid)
            ok += 1
    elif action == 'move':
        tgt = Category.query.get(data.get('category_id')) if data.get('category_id') else None
        if not tgt:
            return jsonify({'success': False, 'message': '目标分类不存在'}), 404
        move_files = bool(data.get('move_files'))
        for bid in ids:
            b = Book.query.get(bid)
            if not b:
                failed.append(bid); continue
            if b.category_id:
                touched.add(b.category_id)
            if move_files:
                mok, _mmsg, mskip = _move_book_file_to_category(b, tgt)
                if not mok:
                    failed.append(bid); continue
                if mskip:
                    files_skipped += 1
                else:
                    files_moved += 1
            b.category_id = tgt.id
            ok += 1
        touched.add(tgt.id)
    else:
        op = data.get('op') or 'add'
        tag = (data.get('tag') or '').strip()[:MAX_TAG_LEN]
        if not tag:
            return jsonify({'success': False, 'message': '标签不能为空'}), 400
        for bid in ids:
            b = Book.query.get(bid)
            if not b:
                failed.append(bid); continue
            cur = _parse_tags(b.tags)
            if op == 'add':
                if tag not in cur and len(cur) < MAX_TAGS:
                    cur.append(tag)
            elif op == 'remove':
                cur = [t for t in cur if t != tag]
            else:
                cur = [tag]
            b.tags = TAG_SEP.join(cur)[:MAX_TAGS_STR]
            ok += 1
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '操作失败：' + str(e)}), 500
    _invalidate_tags_cache()  # 批量操作可能改了标签
    # 重算受影响分类的书本数
    for cid in touched:
        c = Category.query.get(cid)
        if c:
            c.book_count = Book.query.filter_by(category_id=c.id).count()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    log_action(current_user, '书籍批量操作', '%s 共 %d 本' % (action, ok))
    return jsonify({'success': True, 'affected': ok, 'failed': failed,
                    'files_deleted': files_deleted, 'files_failed': files_failed,
                    'files_moved': files_moved, 'files_skipped': files_skipped})


# ============ 书籍整理工具（ 文件名规范化 /  分册归组 /  番外标记 /  拆分） ============
MAX_RENAME_BATCH = 200      # 单次重命名上限，避免一次性动用过多磁盘文件
MAX_SPLIT_BYTES = 32 * 1024 * 1024   # 拆分只处理 32MB 以内的纯文本
MAX_SPLIT_PARTS = 50


def _tidy_rows(offset=0, limit=800):
    """按 id 顺序分段取整理所需的轻量字段（只读）。"""
    return (db.session.query(Book.id, Book.title, Book.filename,
                             Book.file_type, Book.category_id, Book.tags)
            .order_by(Book.id.asc()).offset(offset).limit(limit).all())


@bp.route('/api/admin/books/rename-plan')
@login_required
def admin_books_rename_plan():
    """ 文件名智能整理（预览）：只读扫描，列出「建议文件名 ≠ 当前文件名」的书。

    只给方案不动盘；真正执行走 rename-apply。
    """
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    offset = max(0, request.args.get('offset', 0, type=int))
    limit = min(max(request.args.get('limit', 800, type=int), 1), 2000)
    rows = _tidy_rows(offset, limit)
    items = []
    for bid, title, fn, ft, cid, _tg in rows:
        sug = book_tools.suggest_filename(title, fn, ft)
        if not sug:
            continue
        items.append({'id': bid, 'title': title or '', 'file_type': ft or '',
                      'old_name': sug['old_name'], 'new_name': sug['new_name'],
                      'reasons': sug['reasons']})
    scanned = len(rows)
    return jsonify({'success': True, 'items': items, 'scanned': scanned,
                    'offset': offset, 'next_offset': offset + scanned,
                    'has_more': scanned == limit})


@bp.route('/api/admin/books/rename-apply', methods=['POST'])
@login_required
def admin_books_rename_apply():
    """ 执行文件重命名：复用 _rename_book_file（保留原目录/扩展名，重名自动加序号）。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in (data.get('ids') or []) if str(i).isdigit()]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return jsonify({'success': False, 'message': '没有选中要重命名的书籍'}), 400
    if len(ids) > MAX_RENAME_BATCH:
        return jsonify({'success': False,
                        'message': '单次最多重命名 %d 本' % MAX_RENAME_BATCH}), 400
    renamed, skipped, failed = [], [], []
    for bid in ids:
        book = Book.query.get(bid)
        if book is None:
            failed.append({'id': bid, 'message': '书籍不存在'})
            continue
        sug = book_tools.suggest_filename(book.title, book.filename, book.file_type)
        if not sug:
            skipped.append({'id': bid, 'message': '文件名已规范，无需变更'})
            continue
        stem = os.path.splitext(sug['new_name'])[0][:200]
        okr, msg, newrel = _rename_book_file(book, stem)
        if not okr:
            failed.append({'id': bid, 'message': msg})
            continue
        if not newrel:
            skipped.append({'id': bid, 'message': msg})
            continue
        book.filename = os.path.basename(newrel)
        book.relative_path = newrel
        renamed.append({'id': bid, 'old_name': sug['old_name'],
                        'new_name': os.path.basename(newrel)})
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '保存失败：' + str(e)}), 500
    if renamed:
        log_action(current_user, '整理书籍文件名', '%d 本' % len(renamed))
    return jsonify({'success': True, 'renamed': renamed, 'skipped': skipped,
                    'failed': failed, 'renamed_count': len(renamed),
                    'message': '已重命名 %d 本，跳过 %d 本，失败 %d 本'
                               % (len(renamed), len(skipped), len(failed))})


@bp.route('/api/admin/books/series')
@login_required
def admin_books_series():
    """ /  分册归组预览：按「上下册 / 第N部 / 第N章」把同系列的书聚在一起（只读）。

    只出分组清单；合并仍走已有的 /api/admin/books/merge（默认保留原件）。
    """
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    kind = request.args.get('kind', 'volume')
    if kind not in ('volume', 'chapter'):
        return jsonify({'success': False, 'message': 'kind 只能是 volume 或 chapter'}), 400
    rows = (db.session.query(Book.id, Book.title, Book.filename, Book.file_type)
            .order_by(Book.id.asc()).all())
    data = [{'id': r[0], 'title': r[1], 'filename': r[2], 'file_type': r[3]}
            for r in rows]
    groups = [g for g in book_tools.group_series(data) if g['kind'] == kind]
    return jsonify({'success': True, 'kind': kind, 'scanned': len(data),
                    'total_groups': len(groups), 'groups': groups})


@bp.route('/api/admin/books/auto-tags')
@login_required
def admin_books_auto_tags():
    """ 番外/前传/短篇等建议标签（只读预览）。

    只给建议不写库；执行沿用已有的批量打标签接口（人工确认后调用）。
    """
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    offset = max(0, request.args.get('offset', 0, type=int))
    limit = min(max(request.args.get('limit', 800, type=int), 1), 2000)
    include_series = request.args.get('include_series') == '1'
    only_missing = request.args.get('only_missing') == '1'
    # 系列名标签：按 group_series 全库扫一遍（与 /api/admin/books/series 同成本），
    # 只给「同系列 >= 2 本」的组员建议系列名标签——比逐本判断误标率低得多。
    series_map = {}
    if include_series:
        all_rows = (db.session.query(Book.id, Book.title, Book.filename)
                    .order_by(Book.id.asc()).all())
        data = [{'id': r[0], 'title': r[1], 'filename': r[2], 'file_type': ''}
                for r in all_rows]
        for g in book_tools.group_series(data, min_size=2, max_groups=400):
            base = (g['base'] or '')[:20]
            for m in g['books']:
                series_map[m['id']] = base
    rows = _tidy_rows(offset, limit)
    items = []
    for bid, title, fn, ft, cid, tags_str in rows:
        ex = book_tools.extra_tags(title, fn)
        qt = book_tools.quality_tags(title, fn)
        tags = list(ex) + list(qt)
        kinds = {}
        for t in ex:
            kinds[t] = 'extra'
        for t in qt:
            kinds[t] = 'quality'
        if bid in series_map:
            tags = tags + [series_map[bid]]
            kinds[series_map[bid]] = 'series'
        seen = set()
        tags = [t for t in tags if not (t in seen or seen.add(t))]
        if not tags:
            continue
        if only_missing:
            existing = set(_parse_tags(tags_str))
            if set(tags) <= existing:
                continue
        items.append({'id': bid, 'title': title or fn or '', 'filename': fn or '',
                      'file_type': ft or '', 'tags': tags, 'kinds': kinds})
    scanned = len(rows)
    return jsonify({'success': True, 'items': items, 'scanned': scanned,
                    'offset': offset, 'next_offset': offset + scanned,
                    'has_more': scanned == limit})


@bp.route('/api/admin/books/auto-tags/apply', methods=['POST'])
@login_required
def admin_books_auto_tags_apply():
    """批量应用建议标签：body {'items':[{'id':..,'tags':['..']}]}，一律追加去重。

    服务端一次事务落库，替代前端按标签循环调用 batch 接口的 N 次请求。
    """
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    if not items or len(items) > 1000:
        return jsonify({'success': False, 'message': 'items 需在 1~1000 条之间'}), 400
    ok = 0
    unchanged = 0
    failed = []
    for it in items:
        bid = it.get('id')
        tags = []
        for t in (it.get('tags') or []):
            t = str(t).strip()[:MAX_TAG_LEN]
            if t:
                tags.append(t)
        b = Book.query.get(bid) if isinstance(bid, int) else None
        if not b or not tags:
            failed.append(bid)
            continue
        cur = _parse_tags(b.tags)
        changed = False
        for t in tags:
            if t not in cur and len(cur) < MAX_TAGS:
                cur.append(t)
                changed = True
        if changed:
            b.tags = TAG_SEP.join(cur)[:MAX_TAGS_STR]
            ok += 1
        else:
            unchanged += 1
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '写入失败：' + str(e)}), 500
    _invalidate_tags_cache()
    log_action(current_user, '书籍批量操作', '自动标签应用 %d 本' % ok)
    return jsonify({'success': True, 'affected': ok, 'unchanged': unchanged,
                    'failed': failed})


@bp.route('/api/admin/book/<int:book_id>/split', methods=['POST'])
@login_required
def admin_book_split(book_id):
    """ 书籍拆分：把一个纯文本按章节或按长度拆成多本。

    **默认 dry_run=true 只返回方案**，必须显式 dry_run=false 才写盘；
    写盘过程中若任一步失败，会删掉本次已生成的分册，不留半成品。
    """
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无书籍管理权限'}), 403
    book = Book.query.get_or_404(book_id)
    ft = (book.file_type or '').lower()
    if ft and ft not in ('txt', 'md', 'log', 'cn'):
        return jsonify({'success': False, 'message': '仅支持纯文本（txt/md）拆分'}), 400
    data = request.get_json(silent=True) or {}
    mode = data.get('mode') or 'chapter'
    if mode not in ('chapter', 'size'):
        return jsonify({'success': False, 'message': 'mode 只能是 chapter 或 size'}), 400
    try:
        parts = int(data.get('parts') or 2)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'parts 必须是数字'}), 400
    if parts < 2 or parts > MAX_SPLIT_PARTS:
        return jsonify({'success': False,
                        'message': '份数需在 2~%d 之间' % MAX_SPLIT_PARTS}), 400

    src = _book_abs_path(book)
    if not src or not os.path.exists(src):
        return jsonify({'success': False, 'message': '磁盘上找不到原文件'}), 404
    try:
        if os.path.getsize(src) > MAX_SPLIT_BYTES:
            return jsonify({'success': False,
                            'message': '文件超过 %dMB，建议先用其它工具分卷'
                                       % (MAX_SPLIT_BYTES // 1048576)}), 400
        with open(src, 'rb') as f:
            raw = f.read()
    except Exception as e:
        return jsonify({'success': False, 'message': '读取失败：' + str(e)}), 500

    text, enc = broken_files.decode_best(raw)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    plan = book_tools.plan_split(text, mode, parts)
    if len(plan) < 2:
        return jsonify({'success': False,
                        'message': '没有找到可用切分点（文件过小或章节标记不足）'}), 400

    base_title = os.path.splitext(book.title or book.filename or 'book')[0]
    segs = [{'index': s['index'],
             'title': '%s（%d/%d）' % (base_title, s['index'], len(plan)),
             'chars': s['chars'], 'hint': s['hint']} for s in plan]
    dry = data.get('dry_run', True)
    if isinstance(dry, str):
        dry = dry.strip().lower() not in ('0', 'false', 'no', '')
    if dry:
        return jsonify({'success': True, 'dry_run': True, 'encoding': enc,
                        'book_id': book.id, 'source': book.filename,
                        'total_chars': len(text), 'segments': segs})

    out_dir = os.path.dirname(src)
    ext = os.path.splitext(src)[1] or '.txt'
    books_dir = str(current_app.config['BOOKS_DIR'])
    created, created_books = [], []
    try:
        for seg, s in zip(segs, plan):
            safe = book_tools.safe_stem(seg['title'], 80) or ('%s_%d' % (base_title, seg['index']))
            cand = os.path.join(out_dir, safe + ext)
            n = 2
            while os.path.exists(cand):
                cand = os.path.join(out_dir, '%s_%d%s' % (safe, n, ext))
                n += 1
            payload = text[s['start']:s['end']]
            with open(cand, 'w', encoding='utf-8') as fp:
                fp.write(payload)
            created.append(cand)
            try:
                rel = os.path.relpath(cand, books_dir)
            except Exception:
                rel = os.path.basename(cand)
            nb = Book(filename=os.path.basename(cand), title=seg['title'],
                      author=book.author, file_type=(book.file_type or 'TXT'),
                      file_size=len(payload.encode('utf-8')), relative_path=rel,
                      initial=BookUtils.get_title_initial(seg['title']),
                      category_id=book.category_id,
                      description='由《%s》拆分生成（%d/%d）'
                                  % (base_title, seg['index'], len(segs)))
            db.session.add(nb)
            created_books.append(nb)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        for p in created:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return jsonify({'success': False, 'message': '拆分失败并已回滚：' + str(e)}), 500
    try:
        if book.category_id:
            cat = Category.query.get(book.category_id)
            if cat:
                cat.book_count = Book.query.filter_by(category_id=cat.id).count()
                db.session.commit()
    except Exception:
        db.session.rollback()
    log_action(current_user, '拆分书籍', '《%s》→ %d 本' % (base_title, len(created_books)))
    return jsonify({'success': True, 'dry_run': False, 'created': len(created_books),
                    'books': [{'id': b.id, 'title': b.title, 'filename': b.filename}
                              for b in created_books],
                    'message': '已生成 %d 本（原书保留）' % len(created_books)})


@bp.route('/admin/export-meta')
@login_required
def admin_export_meta():
    """导出书籍元数据为 CSV（流式生成，避免一次性全部读入内存）。"""
    if not _require_cap('export'):
        flash('无导出权限', 'error')
        return redirect(url_for('main.library'))
    import csv, io as _io
    from flask import Response, stream_with_context
    cats = {c.id: c.path for c in Category.query.all()}

    def generate():
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(['id', 'title', 'author', 'filename', 'format', 'size_bytes',
                    'category_path', 'tags', 'upload_date', 'last_read'])
        yield '\ufeff' + buf.getvalue()   # BOM，便于 Excel 正确识别中文
        buf.seek(0); buf.truncate(0)
        for b in Book.query.yield_per(500):
            w.writerow([b.id, b.title or '', b.author or '', b.filename,
                        b.file_type or '', b.file_size or 0,
                        cats.get(b.category_id, ''), b.tags or '',
                        b.upload_date.strftime('%Y-%m-%d %H:%M:%S') if b.upload_date else '',
                        b.last_read.strftime('%Y-%m-%d %H:%M:%S') if b.last_read else ''])
            if buf.tell() > 65536:
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)
        yield buf.getvalue()

    # 流式响应必须在请求上下文内迭代：生成器里访问 db 是在请求结束后才执行的，
    # 不包 stream_with_context 会抛 "Working outside of application context"，导出直接失败。
    log_action(current_user, '导出元数据', 'CSV（全库）')
    return Response(stream_with_context(generate()), mimetype='text/csv; charset=utf-8', headers={
        'Content-Disposition': 'attachment; filename="books_metadata.csv"'})


# ============ 子管理员授权模型（最高管理员授权最多 3 名） ============
MAX_SUB_ADMINS = 3


@bp.route('/admin/sub-admins')
@login_required
def admin_sub_admins():
    if not current_user.is_admin:
        flash('仅最高管理员可访问', 'error')
        return redirect(url_for('main.library'))
    subs = User.query.filter_by(is_sub_admin=True).order_by(User.granted_at.desc()).all()
    eligible = User.query.filter(
        User.is_admin.is_(False),
        User.is_active.is_(True), User.is_sub_admin.is_(False),
    ).order_by(User.created_at.desc()).all()
    return render_template('sub_admins.html', subs=subs, eligible=eligible,
                           max_sub_admins=MAX_SUB_ADMINS)


@bp.route('/api/admin/sub-admins', methods=['GET'])
@login_required
def api_sub_admins():
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': '仅最高管理员可访问'}), 403
    subs = User.query.filter_by(is_sub_admin=True).order_by(User.granted_at.desc()).all()
    eligible = User.query.filter(
        User.is_admin.is_(False),
        User.is_active.is_(True), User.is_sub_admin.is_(False),
    ).order_by(User.created_at.desc()).all()
    def _u(u):
        return {'id': u.id, 'username': u.username, 'email': u.email}
    return jsonify({
        'success': True, 'count': len(subs), 'max': MAX_SUB_ADMINS,
        'subs': [dict(_u(s), can_import=bool(s.can_import), can_export=bool(s.can_export),
                      can_manage_books=bool(s.can_manage_books),
                      granted_at=s.granted_at.strftime('%Y-%m-%d %H:%M') if s.granted_at else '')
                 for s in subs],
        'eligible': [_u(u) for u in eligible],
    })


@bp.route('/api/admin/sub-admin/grant', methods=['POST'])
@login_required
def grant_sub_admin():
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': '仅最高管理员可授权'}), 403
    data = request.get_json(silent=True) or {}
    user = User.query.get(data.get('user_id')) if data.get('user_id') else None
    if not user:
        return jsonify({'success': False, 'message': '用户不存在'}), 404
    if user.is_admin:
        return jsonify({'success': False, 'message': '该账号不可授权为子管理员'}), 400
    if not user.is_active:
        return jsonify({'success': False, 'message': '该账号已停用'}), 400
    can_import = bool(data.get('can_import'))
    can_export = bool(data.get('can_export'))
    can_manage_books = bool(data.get('can_manage_books'))
    if not (can_import or can_export or can_manage_books):
        return jsonify({'success': False, 'message': '请至少授予一项功能权限'}), 400
    if not user.is_sub_admin:
        cur = User.query.filter_by(is_sub_admin=True).count()
        if cur >= MAX_SUB_ADMINS:
            return jsonify({'success': False,
                            'message': '子管理员已达上限（最多 %d 名）' % MAX_SUB_ADMINS}), 409
    user.is_sub_admin = True
    user.can_import = can_import
    user.can_export = can_export
    user.can_manage_books = can_manage_books
    user.granted_by = current_user.id
    user.granted_at = datetime.utcnow()
    db.session.commit()
    log_action(current_user, '授权子管理员',
               '%s（导入:%s 导出:%s 管理:%s）' % (user.username, can_import, can_export, can_manage_books))
    return jsonify({'success': True})


@bp.route('/api/admin/sub-admin/revoke', methods=['POST'])
@login_required
def revoke_sub_admin():
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': '仅最高管理员可操作'}), 403
    data = request.get_json(silent=True) or {}
    user = User.query.get(data.get('user_id')) if data.get('user_id') else None
    if not user:
        return jsonify({'success': False, 'message': '用户不存在'}), 404
    if not user.is_sub_admin:
        return jsonify({'success': False, 'message': '该账号不是子管理员'}), 400
    user.is_sub_admin = False
    user.can_import = False
    user.can_export = False
    user.can_manage_books = False
    user.granted_by = None
    user.granted_at = None
    db.session.commit()
    log_action(current_user, '撤销子管理员', user.username)
    return jsonify({'success': True})


# ============ 分类管理（B10：重命名 / 合并；纯 DB 操作不影响磁盘文件） ============
@bp.route('/admin/categories')
@login_required
def admin_categories():
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))
    cats = Category.query.order_by(Category.path).all()
    return render_template('admin_categories.html', categories=cats)


@bp.route('/api/admin/category/<int:cat_id>/rename', methods=['POST'])
@login_required
def admin_category_rename(cat_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': '需要管理员权限'}), 403
    cat = Category.query.get_or_404(cat_id)
    data = request.get_json(silent=True) or {}
    new_name = (data.get('name') or '').strip()
    if not new_name or '/' in new_name:
        return jsonify({'success': False, 'message': '名称不能为空且不能含 /'}), 400
    old_path = cat.path
    new_path = (cat.parent.path + '/' + new_name) if cat.parent else new_name
    if new_path != old_path and Category.query.filter_by(path=new_path).first():
        return jsonify({'success': False, 'message': '已存在同名分类'}), 409
    cat.name = new_name
    cat.path = new_path
    old_prefix = (old_path + '/') if old_path else ''
    new_prefix = new_path + '/'
    for child in Category.query.filter(Category.path.like(old_prefix + '%')).all():
        child.path = new_prefix + child.path[len(old_prefix):]
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '重命名失败：' + str(e)}), 500
    log_action(current_user, '重命名分类', old_path + ' -> ' + new_path)
    return jsonify({'success': True, 'path': new_path})


@bp.route('/api/admin/category/<int:cat_id>/move', methods=['POST'])
@login_required
def admin_category_move(cat_id):
    """ 移动分类：修改父级归属（不动磁盘），递归重建子树 path 与 level。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无权限'}), 403
    cat = Category.query.get_or_404(cat_id)
    data = request.get_json(silent=True) or {}
    raw = data.get('parent_id')
    if raw in (None, '', 'null'):
        new_parent = None
        new_parent_id = None
    else:
        try:
            new_parent_id = int(raw)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'parent_id 非法'}), 400
        if new_parent_id == cat.id:
            return jsonify({'success': False, 'message': '不能移动到自身'}), 400
        new_parent = Category.query.get(new_parent_id)
        if not new_parent:
            return jsonify({'success': False, 'message': '目标分类不存在'}), 404
        if new_parent.path == cat.path or new_parent.path.startswith(cat.path + '/'):
            return jsonify({'success': False, 'message': '不能移动到自身的子分类'}), 400
    old_path = cat.path
    old_level = cat.level
    new_path = (new_parent.path + '/' + cat.name) if new_parent else cat.name
    if new_path != old_path and Category.query.filter_by(path=new_path).first():
        return jsonify({'success': False, 'message': '目标位置已存在同名分类'}), 409
    cat.parent_id = new_parent_id
    cat.level = (new_parent.level + 1) if new_parent else 1
    cat.path = new_path
    old_prefix = old_path + '/'
    new_prefix = new_path + '/'
    delta = cat.level - old_level
    for child in Category.query.filter(Category.path.like(old_prefix + '%')).all():
        child.path = new_prefix + child.path[len(old_prefix):]
        child.level = child.level + delta
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '移动失败：' + str(e)}), 500
    log_action(current_user, '移动分类', old_path + ' -> ' + new_path)
    return jsonify({'success': True, 'path': new_path})


@bp.route('/api/admin/category/batch-move', methods=['POST'])
@login_required
def admin_category_batch_move():
    """ 批量移动多个分类到同一目标（不动磁盘），逐棵重建子树 path/level。"""
    if not _require_cap('manage_books'):
        return jsonify({'success': False, 'message': '无权限'}), 403
    data = request.get_json(silent=True) or {}
    ids = [int(x) for x in (data.get('ids') or []) if str(x).isdigit()]
    raw = data.get('parent_id')
    if raw in (None, '', 'null'):
        new_parent = None
        new_parent_id = None
    else:
        try:
            new_parent_id = int(raw)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'parent_id 非法'}), 400
        new_parent = Category.query.get(new_parent_id)
        if not new_parent:
            return jsonify({'success': False, 'message': '目标分类不存在'}), 404
    # 整体成环检测：目标不能是任一待移动分类或其子树（用 path 前缀，纯 DB 查询，可靠）
    moving_ids = set()
    for cid in ids:
        c = Category.query.get(cid)
        if not c:
            continue
        moving_ids.add(c.id)
        for ch in Category.query.filter(Category.path.like(c.path + '/%')).all():
            moving_ids.add(ch.id)
    if new_parent_id in moving_ids:
        return jsonify({'success': False, 'message': '目标分类不能是待移动分类或其子分类'}), 400
    results = []
    for cid in ids:
        c = Category.query.get(cid)
        if not c:
            results.append({'id': cid, 'ok': False, 'msg': '分类不存在'}); continue
        old_path = c.path
        old_level = c.level
        new_path = (new_parent.path + '/' + c.name) if new_parent else c.name
        if new_path != old_path and Category.query.filter_by(path=new_path).first():
            results.append({'id': cid, 'ok': False, 'msg': '目标位置已存在同名分类'}); continue
        c.parent_id = new_parent_id
        c.level = (new_parent.level + 1) if new_parent else 1
        c.path = new_path
        old_prefix = old_path + '/'
        new_prefix = new_path + '/'
        delta = c.level - old_level
        for child in Category.query.filter(Category.path.like(old_prefix + '%')).all():
            child.path = new_prefix + child.path[len(old_prefix):]
            child.level = child.level + delta
        results.append({'id': cid, 'ok': True, 'path': new_path})
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '批量移动失败：' + str(e)}), 500
    moved = sum(1 for r in results if r['ok'])
    log_action(current_user, '批量移动分类', '%d 个 -> %s' % (moved, new_parent.path if new_parent else '根'))
    return jsonify({'success': True, 'moved': moved, 'total': len(ids), 'results': results})


@bp.route('/api/admin/category/<int:cat_id>/merge', methods=['POST'])
@login_required
def admin_category_merge(cat_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': '需要管理员权限'}), 403
    src = Category.query.get_or_404(cat_id)
    data = request.get_json(silent=True) or {}
    target_id = data.get('target_id')
    if not target_id:
        return jsonify({'success': False, 'message': '请选择目标分类'}), 400
    target = Category.query.get(target_id)
    if not target:
        return jsonify({'success': False, 'message': '目标分类不存在'}), 404
    if target.id == src.id:
        return jsonify({'success': False, 'message': '不能合并到自身'}), 400
    # 收集源子树（含自身）
    subtree = []
    stack = [src]
    while stack:
        node = stack.pop()
        subtree.append(node)
        stack.extend(node.children)
    if target in subtree:
        return jsonify({'success': False, 'message': '不能合并到自身的子分类'}), 400
    subtree_ids = [c.id for c in subtree]
    # 子树内全部书改挂到目标分类
    Book.query.filter(Book.category_id.in_(subtree_ids)).update(
        {Book.category_id: target.id}, synchronize_session=False)
    # 先删深层（避免父先于子删除触发外键约束）
    for c in sorted(subtree, key=lambda x: (-x.level, -x.id)):
        db.session.delete(c)
    target.book_count = Book.query.filter_by(category_id=target.id).count()
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '合并失败：' + str(e)}), 500
    log_action(current_user, '合并分类', src.path + ' -> ' + target.path)
    return jsonify({'success': True})


# ============ 书籍标签 ============
# 复用 Book 上早已声明但一直闲置的 tags 列，逗号分隔存储
TAG_SEP = ','
MAX_TAGS = 8
MAX_TAG_LEN = 12
MAX_TAGS_STR = 200


def _parse_tags(value):
    """把 'a,b' 解析成 ['a','b']（去空白、去重、丢空项）。"""
    out = []
    if not value:
        return out
    for t in str(value).split(TAG_SEP):
        t = t.strip()
        if t and t not in out:
            out.append(t)
    return out


def _serialize_tags(tags):
    return TAG_SEP.join(tags)[:MAX_TAGS_STR]


@bp.route('/api/book/<int:book_id>/tags', methods=['GET'])
@login_required
def api_get_tags(book_id):
    book = Book.query.get_or_404(book_id)
    return jsonify({
        'success': True,
        'tags': _parse_tags(book.tags),
        'can_edit': bool(current_user.is_admin or current_user.can_manage_books),
    })


@bp.route('/api/book/<int:book_id>/tags', methods=['POST'])
@login_required
def api_set_tags(book_id):
    """op: add / remove / set"""
    if not (current_user.is_admin or current_user.can_manage_books):
        return jsonify({'success': False, 'message': '无编辑标签权限'}), 403
    book = Book.query.get_or_404(book_id)
    data = request.get_json(silent=True) or {}
    op = data.get('op') or 'set'
    tags = _parse_tags(book.tags)

    if op == 'add':
        for raw in str(data.get('tag') or '').split(TAG_SEP):
            t = raw.strip()[:MAX_TAG_LEN]
            if t and t not in tags:
                tags.append(t)
    elif op == 'remove':
        t = str(data.get('tag') or '').strip()
        tags = [x for x in tags if x != t]
    elif op == 'set':
        new = []
        for raw in str(data.get('tags') or '').split(TAG_SEP):
            t = raw.strip()[:MAX_TAG_LEN]
            if t and t not in new:
                new.append(t)
        tags = new
    else:
        return jsonify({'success': False, 'message': '未知操作'}), 400

    if len(tags) > MAX_TAGS:
        return jsonify({'success': False, 'message': '最多 %d 个标签' % MAX_TAGS}), 400

    book.tags = _serialize_tags(tags)
    db.session.commit()
    _invalidate_tags_cache()
    return jsonify({'success': True, 'tags': tags})


# ---- 读多写少的聚合结果缓存：/api/tags（300s TTL + 写操作即时失效）与
# /api/library-overview 的全局聚合部分（60s TTL，阅读状态始终按用户实时算）。
_TAGS_CACHE_TTL = 300.0
_OVERVIEW_CACHE_TTL = 60.0
_tags_cache = {'ts': 0.0, 'data': None}
_overview_cache = {'ts': 0.0, 'data': None}


def _invalidate_tags_cache():
    """任何会改变 book.tags / 删书的写操作后调用，下次请求即时重算。"""
    _tags_cache['ts'] = 0.0
    _tags_cache['data'] = None


@bp.route('/api/tags', methods=['GET'])
@login_required
def api_tags():
    """常用标签及计数（只扫有标签的书；结果缓存，写操作即时失效）。"""
    now = time.time()
    if _tags_cache['data'] is not None and now - _tags_cache['ts'] < _TAGS_CACHE_TTL:
        return jsonify({'success': True, 'tags': _tags_cache['data'], 'cached': True})
    rows = db.session.execute(db.text(
        "SELECT tags FROM book WHERE tags IS NOT NULL AND tags != ''"
    )).fetchall()
    counter = {}
    for (t,) in rows:
        for x in _parse_tags(t):
            counter[x] = counter.get(x, 0) + 1
    top = sorted(counter.items(), key=lambda kv: -kv[1])[:30]
    data = [{'name': k, 'count': v} for k, v in top]
    _tags_cache['ts'] = now
    _tags_cache['data'] = data
    return jsonify({'success': True, 'tags': data})


# ============ 健康检查 ============
@bp.route('/health')
def health():
    return jsonify({'status': 'ok'})

# ============ 分页 API（优化性能） ============
@bp.route('/api/books/page')
@login_required
def api_books_page():
    """分页获取书籍列表"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    page = max(1, int(page))
    per_page = max(1, min(200, int(per_page)))
    category = request.args.get('category', 'all')
    letter = request.args.get('letter', 'all')
    format_filter = request.args.get('format', 'all')
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '').strip()
    tag = request.args.get('tag', '').strip()
    
    query = Book.query
    
    # 分类筛选：含所有层级的下级分类（单次查询 + 内存展开，见 _descendant_category_ids）
    if category != 'all':
        category_obj = Category.query.filter_by(path=category).first()
        if category_obj:
            query = query.filter(Book.category_id.in_(_descendant_category_ids(category_obj)))
    
    # 格式筛选（大小写不敏感：库里有个别小写残留行，等值比较会让它消失）
    if format_filter and format_filter != 'all':
        query = query.filter(Book.file_type.in_(_file_type_variants(format_filter)))
    
    # 搜索（书名/作者/文件名/标签；搜索框占位文案承诺了标签，实际命中才不撒谎）
    if search:
        query = query.filter(
            db.or_(
                Book.title.ilike(f'%{search}%'),
                Book.author.ilike(f'%{search}%'),
                Book.filename.ilike(f'%{search}%'),
                Book.tags.ilike(f'%{search}%')
            )
        )
    
    # 标签筛选：按逗号分隔的整段匹配，避免子串误命中
    if tag:
        query = query.filter(db.or_(
            Book.tags == tag,
            Book.tags.like(tag + ',%'),
            Book.tags.like('%,' + tag + ',%'),
            Book.tags.like('%,' + tag),
        ))

    # 阅读状态筛选：未读 / 在读 / 已读
    if status in ('unread', 'reading', 'finished'):
        rp2 = db.aliased(ReadingProgress)
        if status == 'unread':
            query = query.outerjoin(
                rp2, db.and_(rp2.book_id == Book.id, rp2.user_id == current_user.id)
            ).filter(db.or_(rp2.id.is_(None), rp2.status.is_(None), rp2.status == 'unread'))
        else:
            query = query.join(
                rp2, db.and_(rp2.book_id == Book.id, rp2.user_id == current_user.id)
            ).filter(rp2.status == status)

    # 字母筛选：改用入库时算好的 initial（中文按拼音首字母），走 ix_book_initial 索引。
    # 原实现用 title LIKE 'A%' / GLOB，与首页按 initial 聚合出来的按钮计数完全对不上：
    # 实测 initial='Y' 有 5197 本而 title LIKE 'Y%' 只有 1 本；'#' 用的
    # GLOB '[^A-Za-z0-9]*' 更会把全部中文都匹配进来 —— 点字母后本数与列表严重不符。
    if letter not in ('all', 'random', ''):
        if letter == 'Other':
            query = query.filter(db.or_(Book.initial == 'Other', Book.initial.is_(None)))
        elif letter == 'Num':
            query = query.filter(Book.initial == 'Num')
        elif len(letter) == 1 and letter.isalpha():
            query = query.filter(Book.initial == letter.upper())
    
    # 总数（直接 count 主键，避免 Query.count() 的子查询包装在 6 万行上多一跳）
    total = query.with_entities(db.func.count(Book.id)).scalar() or 0

    # 搜索统计：命中书的格式分布 + 分类分布，供前端展示
    search_stats = None
    if search:
        fmt_rows = (query.with_entities(Book.file_type, db.func.count(Book.id))
                    .group_by(Book.file_type)
                    .order_by(db.func.count(Book.id).desc()).limit(8).all())
        # 分类分布：派生新 query 做 join，避免污染下方 books 查询
        cat_q = query.join(Category, Book.category_id == Category.id)
        cat_rows = (cat_q.with_entities(Category.name, Category.path, db.func.count(Book.id))
                    .group_by(Category.id)
                    .order_by(db.func.count(Book.id).desc()).limit(6).all())
        search_stats = {
            'formats': [{'type': (r[0] or '未知'), 'count': r[1]} for r in fmt_rows],
            'categories': [{'name': (r[0] or '未分类'), 'path': r[1], 'count': r[2]} for r in cat_rows]
        }

    # 显式排序（书架排序方式：书名/作者/时间/进度），优先于 tab
    sort = request.args.get('sort', '').strip()
    order = request.args.get('order', 'asc').strip().lower()
    # db.asc / db.desc 是「接收列参数」的函数，只能这样用：db.asc(Book.title)
    def od(col):
        return db.desc(col) if order == 'desc' else db.asc(col)

    off = (page - 1) * per_page
    if sort == 'title':
        books = query.order_by(od(Book.title), Book.id.asc()).offset(off).limit(per_page).all()
    elif sort == 'author':
        books = query.order_by(od(Book.author), Book.title.asc()).offset(off).limit(per_page).all()
    elif sort == 'time':
        books = query.order_by(od(Book.upload_date), Book.id.desc()).offset(off).limit(per_page).all()
    elif sort == 'progress':
        # 按当前用户的阅读进度排序：外连接本人在该书上的进度记录
        rp = db.aliased(ReadingProgress)
        books = (query.outerjoin(rp, db.and_(rp.book_id == Book.id, rp.user_id == current_user.id))
                 .order_by(od(rp.progress), Book.title.asc()).offset(off).limit(per_page).all())
    elif sort == 'size':
        # 按文件大小排序（order=desc 时从大到小，asc 时从小到大）
        books = (query.order_by(od(Book.file_size), Book.title.asc())
                 .offset(off).limit(per_page).all())
    else:
        # Tab 排序（random/hot/new 由后端完成，前端不再自造数据）
        tab = request.args.get('tab', '')
        if tab == 'hot':
            books = (query.order_by(Book.read_count.desc(), Book.title.asc())
                     .offset(off).limit(per_page).all())
        elif tab == 'new':
            books = (query.order_by(Book.upload_date.desc(), Book.id.desc())
                     .offset(off).limit(per_page).all())
        elif tab == 'random':
            # SQLite RANDOM() 直接随机排序取样（避免把全库 ID 读进 Python 再洗牌）
            books = (query.order_by(db.func.random())
                     .offset(off).limit(per_page).all())
        else:
            books = query.order_by(Book.title.asc()).offset(off).limit(per_page).all()
    
    # 阅读进度与状态（按当前用户），供书卡展示
    book_ids = [b.id for b in books]
    prog_map = {}
    if book_ids:
        rows = (db.session.query(ReadingProgress.book_id, ReadingProgress.progress,
                                 ReadingProgress.status, ReadingProgress.favorite)
                .filter(ReadingProgress.user_id == current_user.id,
                        ReadingProgress.book_id.in_(book_ids)).all())
        prog_map = dict((r[0], r) for r in rows)

    def _status_of(b):
        rec = prog_map.get(b.id)
        if not rec:
            return 'unread'
        if rec[2]:
            return rec[2]
        return 'finished' if (rec[1] or 0) >= 0.98 else 'reading'

    return jsonify({
        'success': True,
        'books': [{
            'id': b.id,
            'title': b.title or b.filename,
            'author': b.author or '',
            'file_type': b.file_type or '未知',
            'file_size_str': BookUtils.format_file_size(b.file_size) if b.file_size else '0 B',
            'file_size': b.file_size or 0,
            'path': b.relative_path or b.filename,
            'read_count': b.read_count or 0,
            'progress': round(prog_map[b.id][1] or 0, 4) if b.id in prog_map else 0,
            'status': _status_of(b),
            'favorite': bool(prog_map[b.id][3]) if b.id in prog_map else False,
            'tags': _parse_tags(b.tags)[:3],
            'created_at': b.upload_date.strftime('%Y-%m-%d %H:%M') if b.upload_date else ''
        } for b in books],
        'total': total,
        'page': page,
        'per_page': per_page,
        'sort': sort,
        'order': order,
        'search_stats': search_stats
    })


@bp.route('/api/recommend/<int:book_id>')
@login_required
def get_recommendations(book_id):
    """获取相关阅读推荐（优先同系列下一部/集，其次同分类、同作者）"""
    book = Book.query.get_or_404(book_id)
    recommendations = []
    seen = set()

    def add(b, reason):
        if not b or b.id == book_id or b.id in seen:
            return
        seen.add(b.id)
        recommendations.append({
            'id': b.id,
            'title': b.title or b.filename,
            'author': b.author or '未知作者',
            'file_type': b.file_type,
            'reason': reason,
        })

    # 1) 同系列：下一部（order 更大）优先并升序（cur+1 最前），前作（order 更小）其次降序。
    #    不再限制「同分类 + 仅取 400 本」——那是续集（如第三集）被漏掉、列表被无关项填满的根因。
    #    改为按归一化系列名在全书名/文件名中 LIKE 收窄候选后精确匹配，跨分类也能命中。
    s = book_tools.series_of(book.title or book.filename or '')
    if s and s.get('base'):
        base = s['base']
        key = book_tools.normalize_series_key(base)
        cur_order = s.get('order') or 0
        if key:
            pat_pfx = base + '%'
            pat_sub = '%' + base + '%'
            cands = Book.query.filter(
                Book.id != book_id,
                db.or_(Book.title.like(pat_pfx), Book.title.like(pat_sub),
                       Book.filename.like(pat_pfx), Book.filename.like(pat_sub))
            ).limit(250).all()
            nxt, prev = [], []
            for b in cands:
                sb = book_tools.series_of(b.title or b.filename or '')
                if not sb or not sb.get('base'):
                    continue
                if book_tools.normalize_series_key(sb['base']) != key:
                    continue
                o = sb.get('order') or 0
                if o > cur_order:
                    nxt.append((o, b))
                elif o < cur_order:
                    prev.append((o, b))
            nxt.sort(key=lambda x: x[0])       # 升序：下一部（cur+1）排最前
            prev.sort(key=lambda x: -x[0])     # 降序：前作（cur-1）排最前
            for _, b in nxt:
                if len(recommendations) >= 6:
                    break
                add(b, '同系列·下一部')
            for _, b in prev:
                if len(recommendations) >= 6:
                    break
                add(b, '同系列·前作')

    # 2) 同作者
    if len(recommendations) < 6 and book.author:
        for b in Book.query.filter(
                Book.author == book.author,
                Book.id != book_id,
                Book.id.notin_([r['id'] for r in recommendations])).limit(6).all():
            if len(recommendations) >= 6:
                break
            add(b, '同作者')

    # 3) 同分类
    if len(recommendations) < 6 and book.category_id:
        for b in Book.query.filter(
                Book.category_id == book.category_id,
                Book.id != book_id,
                Book.id.notin_([r['id'] for r in recommendations])).limit(6).all():
            if len(recommendations) >= 6:
                break
            add(b, '同分类')

    # 4) 随机兜底（仅在同系列/同作者/同分类都很少时补缺，最多补到 6 本）
    if len(recommendations) < 6:
        for b in Book.query.filter(
                Book.id != book_id,
                Book.id.notin_([r['id'] for r in recommendations]))\
                .order_by(db.func.random()).limit(6).all():
            if len(recommendations) >= 6:
                break
            add(b, '猜你喜欢')

    return jsonify({
        'success': True,
        'recommendations': recommendations
    })
