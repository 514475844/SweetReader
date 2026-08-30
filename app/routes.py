from flask import Blueprint, render_template, request, jsonify, send_file, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from pathlib import Path
from datetime import datetime
import json

from app.models import db, User, InviteCode, Book, Category, ReadingProgress, UserTheme
from app.utils import BookUtils
from app.category_scanner import CategoryScanner

bp = Blueprint('main', __name__)

# ============ 内置编码检测 ============
def detect_encoding(raw_bytes):
    encodings = ['utf-8', 'gbk', 'gb18030', 'big5', 'shift-jis', 'euc-kr', 'gb2312']
    for enc in encodings:
        try:
            raw_bytes.decode(enc)
            return enc
        except:
            continue
    return 'utf-8'

def fix_bom(raw_bytes):
    if raw_bytes.startswith(b'\xef\xbb\xbf'):
        return raw_bytes[3:]
    return raw_bytes

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
    categories = Category.query.filter_by(parent_id=None).order_by(Category.sort_order).all()
    total_books = Book.query.count()
    total_categories = Category.query.count()
    return render_template('index.html',
                         books=books, categories=categories,
                         total_books=total_books, total_categories=total_categories)

# ============ 登录/注册 ============
@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.library'))
    if request.method == 'POST':
        login_input = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter(db.or_(User.username == login_input, User.email == login_input)).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('main.library'))
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
        if User.query.filter_by(username=username).first():
            flash('用户名已被使用', 'error')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('该邮箱已注册', 'error')
            return render_template('register.html')
        if password != confirm_password:
            flash('两次输入的密码不一致', 'error')
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
@bp.route('/api/books')
@login_required
def api_books():
    books = Book.query.all()
    return jsonify([{
        'id': b.id,
        'title': b.title or b.filename,
        'author': b.author or '',
        'file_type': b.file_type or '未知',
        'file_size_str': BookUtils.format_file_size(b.file_size) if b.file_size else '0 B'
    } for b in books])

@bp.route('/api/all-books')
@login_required
def api_all_books():
    books = Book.query.order_by(Book.title.asc()).all()
    return jsonify([{
        'id': b.id,
        'title': b.title or b.filename,
        'author': b.author or '',
        'file_type': b.file_type or '未知',
        'file_size_str': BookUtils.format_file_size(b.file_size) if b.file_size else '0 B',
        'path': b.relative_path or b.filename
    } for b in books])

@bp.route('/api/categories')
@login_required
def api_categories():
    mode = request.args.get('mode', 'tree')
    if mode == 'flat':
        categories = Category.query.order_by(Category.path).all()
        return jsonify([{
            'id': cat.id,
            'name': cat.name,
            'full_path': cat.get_full_path(),
            'level': cat.level,
            'book_count': cat.book_count,
            'path': cat.path
        } for cat in categories])
    else:
        categories = Category.query.filter_by(parent_id=None).order_by(Category.sort_order).all()
        def build_tree(cat):
            return {
                'id': cat.id,
                'name': cat.name,
                'path': cat.path,
                'level': cat.level,
                'book_count': cat.book_count,
                'children': [build_tree(child) for child in sorted(cat.children, key=lambda x: x.sort_order)]
            }
        return jsonify([build_tree(cat) for cat in categories])

# ============ 阅读 ============
@bp.route('/read/<int:book_id>')
@login_required
def read_book(book_id):
    book = Book.query.get_or_404(book_id)
    book.last_read = datetime.utcnow()
    db.session.commit()
    return render_template('reader.html', book=book)

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
    if ext == '.epub':
        return send_file(file_path, mimetype='application/epub+zip')
    elif ext == '.pdf':
        return send_file(file_path, mimetype='application/pdf')
    elif ext == '.txt':
        try:
            with open(file_path, 'rb') as f:
                raw = f.read()
            raw = fix_bom(raw)
            encoding = detect_encoding(raw)
            try:
                text = raw.decode(encoding)
            except:
                text = raw.decode(encoding, errors='replace')
            from flask import Response
            return Response(text, mimetype='text/plain; charset=utf-8')
        except Exception as e:
            return jsonify({'error': f'读取失败: {str(e)}'}), 500
    else:
        return send_file(file_path)

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
    books = Book.query.filter_by(category_id=category.id).all()
    breadcrumb = []
    current = category
    while current:
        breadcrumb.insert(0, {'name': current.name, 'path': current.path})
        current = current.parent
    return render_template('category.html', category=category, books=books, breadcrumb=breadcrumb)

# ============ 管理员 ============
@bp.route('/admin/sync', methods=['GET', 'POST'])
@login_required
def sync_categories():
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))
    result = CategoryScanner.scan_and_sync()
    flash(f"✅ 同步完成！新增分类 {result['categories_added']} 个，更新书籍 {result['books_updated']} 本", 'success')
    return redirect(url_for('main.library'))

@bp.route('/admin/invite', methods=['GET', 'POST'])
@login_required
def admin_invite():
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        invite = InviteCode.create_for_email(email, current_user.id)
        flash(f'✅ 邀请码已生成: {invite.code}', 'success')
    invites = InviteCode.query.order_by(InviteCode.created_at.desc()).all()
    return render_template('admin_invite.html', invites=invites)

@bp.route('/admin/users')
@login_required
def admin_users():
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin_users.html', users=users)

@bp.route('/admin/user/<int:user_id>/toggle', methods=['POST'])
@login_required
def toggle_user(user_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    user = User.query.get_or_404(user_id)
    user.is_active = not user.is_active
    db.session.commit()
    return jsonify({'success': True})

@bp.route('/admin/user/<int:user_id>/delete', methods=['DELETE'])
@login_required
def delete_user(user_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    return jsonify({'success': True})

@bp.route('/admin/user/<int:user_id>/reset-password', methods=['POST'])
@login_required
def reset_user_password(user_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    user = User.query.get_or_404(user_id)
    data = request.json
    user.set_password(data.get('password', ''))
    db.session.commit()
    return jsonify({'success': True})

@bp.route('/admin/user/create', methods=['POST'])
@login_required
def create_user():
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    data = request.json
    user = User(username=data['username'], email=data['email'], is_admin=data.get('is_admin', False))
    user.set_password(data['password'])
    db.session.add(user)
    db.session.commit()
    return jsonify({'success': True, 'message': '用户创建成功'})

@bp.route('/admin/logs')
@login_required
def admin_logs():
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))
    logs = []
    try:
        with open('/app/logs/actions.log', 'r', encoding='utf-8') as f:
            logs = f.readlines()[-100:]
    except:
        pass
    return render_template('admin_logs.html', logs=logs)

# ============ 设置 ============
@bp.route('/settings')
@login_required
def settings_page():
    return render_template('settings.html')

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
            current_user.set_password(new_password)
            db.session.commit()
            flash('✅ 密码修改成功！请重新登录', 'success')
            return redirect(url_for('main.logout'))
        elif action == 'update_profile':
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip()
            current_user.username = username
            current_user.email = email
            db.session.commit()
            flash('✅ 个人信息已更新', 'success')
    return render_template('profile.html')

@bp.route('/api/user/theme', methods=['GET', 'POST'])
@login_required
def user_theme():
    theme = UserTheme.query.filter_by(user_id=current_user.id).first()
    if not theme:
        theme = UserTheme(user_id=current_user.id)
        db.session.add(theme)
        db.session.commit()
    if request.method == 'POST':
        data = request.json
        for key in ['theme', 'font_size', 'line_spacing', 'page_margin']:
            if key in data:
                setattr(theme, key, data[key])
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({
        'theme': theme.theme,
        'font_size': theme.font_size,
        'line_spacing': theme.line_spacing,
        'page_margin': theme.page_margin
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
    data = request.json
    progress_value = data.get('progress', 0)
    location = data.get('location', '')
    
    progress = ReadingProgress.query.filter_by(
        user_id=current_user.id, 
        book_id=book_id
    ).first()
    
    if not progress:
        progress = ReadingProgress(
            user_id=current_user.id,
            book_id=book_id,
            progress=progress_value,
            last_location=location
        )
        db.session.add(progress)
    else:
        progress.progress = progress_value
        progress.last_location = location
        progress.updated_at = datetime.utcnow()
    
    db.session.commit()
    return jsonify({'success': True, 'message': '进度已保存'})

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
    category = request.args.get('category', 'all')
    letter = request.args.get('letter', 'all')
    format_filter = request.args.get('format', 'all')
    search = request.args.get('search', '').strip()
    
    query = Book.query
    
    # 分类筛选
    if category != 'all':
        category_obj = Category.query.filter_by(path=category).first()
        if category_obj:
            # 获取该分类及其子分类的所有书籍
            category_ids = [category_obj.id]
            for child in category_obj.children:
                category_ids.append(child.id)
            query = query.filter(Book.category_id.in_(category_ids))
    
    # 格式筛选
    if format_filter != 'all':
        query = query.filter(Book.file_type == format_filter)
    
    # 搜索
    if search:
        query = query.filter(
            db.or_(
                Book.title.ilike(f'%{search}%'),
                Book.author.ilike(f'%{search}%')
            )
        )
    
    # 字母筛选（按标题首字母）
    if letter != 'all' and letter != 'random':
        if letter == 'Num':
            # 数字开头
            query = query.filter(Book.title.op('GLOB')('[0-9]*'))
        elif letter == 'Other':
            # 特殊字符开头（非字母、非数字）
            query = query.filter(Book.title.op('GLOB')('[^A-Za-z0-9]*'))
        else:
            # 字母开头
            query = query.filter(Book.title.ilike(f'{letter}%'))
    
    # 总数
    total = query.count()
    
    # 分页
    books = query.order_by(Book.title.asc()).offset((page - 1) * per_page).limit(per_page).all()
    
    return jsonify({
        'success': True,
        'books': [{
            'id': b.id,
            'title': b.title or b.filename,
            'author': b.author or '',
            'file_type': b.file_type or '未知',
            'file_size_str': BookUtils.format_file_size(b.file_size) if b.file_size else '0 B',
            'path': b.relative_path or b.filename
        } for b in books],
        'total': total,
        'page': page,
        'per_page': per_page
    })
