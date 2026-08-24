from flask import Blueprint, render_template, request, jsonify, send_file, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from pathlib import Path
from datetime import datetime
import json

from app.models import db, User, InviteCode, Book, Category, ReadingProgress, UserTheme
from app.utils import BookUtils
from app.category_scanner import CategoryScanner

bp = Blueprint('main', __name__)

@bp.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('main.library'))
    return redirect(url_for('main.login'))

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.library'))

    if request.method == 'POST':
        login_input = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if not login_input or not password:
            flash('请输入用户名/邮箱和密码', 'error')
            return render_template('login.html')

        user = User.query.filter(
            db.or_(
                User.username == login_input,
                User.email == login_input
            )
        ).first()

        if user and user.check_password(password):
            if not user.is_active:
                flash('账号已被禁用，请联系管理员', 'error')
                return render_template('login.html')
            login_user(user)
            user.last_login = datetime.utcnow()
            db.session.commit()
            return redirect(url_for('main.library'))
        else:
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
            flash('无效的邀请码，请检查后重新输入', 'error')
            return render_template('register.html')

        if invite.expires_at and invite.expires_at < datetime.utcnow():
            flash('邀请码已过期，请联系管理员重新获取', 'error')
            return render_template('register.html')

        if invite.target_email != email:
            flash(f'该邀请码绑定的邮箱是 {invite.target_email}，请使用正确的邮箱', 'error')
            return render_template('register.html')

        if User.query.filter_by(username=username).first():
            flash('用户名已被使用，请换一个', 'error')
            return render_template('register.html')

        if User.query.filter_by(email=email).first():
            flash('该邮箱已注册', 'error')
            return render_template('register.html')

        valid, msg = User.validate_password(password)
        if not valid:
            flash(msg, 'error')
            return render_template('register.html')

        if password != confirm_password:
            flash('两次输入的密码不一致', 'error')
            return render_template('register.html')

        user = User(username=username, email=email, is_admin=False, is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        invite.is_used = True
        invite.used_by = user.id

        theme = UserTheme(user_id=user.id)
        db.session.add(theme)
        db.session.commit()

        flash('🎉 注册成功！请登录', 'success')
        return redirect(url_for('main.login'))

    return render_template('register.html')

@bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.login'))

@bp.route('/library')
@login_required
def library():
    theme = UserTheme.query.filter_by(user_id=current_user.id).first()
    if not theme:
        theme = UserTheme(user_id=current_user.id)
        db.session.add(theme)
        db.session.commit()

    books = Book.query.order_by(Book.upload_date.desc()).all()
    categories = Category.query.filter_by(parent_id=None).order_by(Category.sort_order).all()
    total_books = Book.query.count()
    total_categories = Category.query.count()

    progress_map = {}
    for p in ReadingProgress.query.filter_by(user_id=current_user.id).all():
        progress_map[p.book_id] = p.progress

    return render_template('index.html',
                         books=books, categories=categories,
                         total_books=total_books, total_categories=total_categories,
                         progress_map=progress_map, user_theme=theme)

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

@bp.route('/read/<int:book_id>')
@login_required
def read_book(book_id):
    book = Book.query.get_or_404(book_id)
    book.last_read = datetime.utcnow()
    db.session.commit()

    theme = UserTheme.query.filter_by(user_id=current_user.id).first()
    if not theme:
        theme = UserTheme(user_id=current_user.id)
        db.session.add(theme)
        db.session.commit()

    progress = ReadingProgress.query.filter_by(
        user_id=current_user.id,
        book_id=book.id
    ).first()

    return render_template('reader.html', book=book, progress=progress, user_theme=theme)

@bp.route('/api/books')
@login_required
def api_books():
    books = Book.query.all()
    return jsonify([{
        'id': b.id,
        'title': b.title or b.filename,
        'author': b.author or '',
        'file_type': b.file_type or '未知',
        'file_size': b.file_size,
        'file_size_str': BookUtils.format_file_size(b.file_size) if b.file_size else '0 B',
        'modified_time': b.modified_time.strftime('%Y-%m-%d %H:%M') if b.modified_time else '',
        'upload_date': b.upload_date.strftime('%Y-%m-%d'),
        'tags': b.tags or '',
        'icon': BookUtils.get_file_icon(b.filename),
        'category': b.category.get_full_path() if b.category else '未分类',
        'has_progress': ReadingProgress.query.filter_by(user_id=current_user.id, book_id=b.id).first() is not None
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
            'parent_id': cat.parent_id,
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
                'children': [build_tree(child) for child in cat.children.order_by(Category.sort_order).all()]
            }

        return jsonify([build_tree(cat) for cat in categories])

@bp.route('/api/progress/<int:book_id>', methods=['POST'])
@login_required
def update_progress(book_id):
    data = request.json
    progress_value = data.get('progress', 0)

    progress = ReadingProgress.query.filter_by(
        user_id=current_user.id,
        book_id=book_id
    ).first()

    if not progress:
        progress = ReadingProgress(
            user_id=current_user.id,
            book_id=book_id,
            progress=progress_value
        )
        db.session.add(progress)
    else:
        progress.progress = progress_value

    db.session.commit()
    return jsonify({'success': True})

@bp.route('/api/user/theme', methods=['POST'])
@login_required
def update_theme():
    data = request.json
    theme = UserTheme.query.filter_by(user_id=current_user.id).first()
    if not theme:
        theme = UserTheme(user_id=current_user.id)
        db.session.add(theme)

    if 'theme' in data:
        theme.theme = data['theme']
    if 'font_size' in data:
        theme.font_size = data['font_size']
    if 'line_spacing' in data:
        theme.line_spacing = data['line_spacing']
    if 'page_margin' in data:
        theme.page_margin = data['page_margin']
    if 'detail_mode' in data:
        theme.detail_mode = data['detail_mode']

    db.session.commit()
    return jsonify({'success': True})

@bp.route('/api/user/theme', methods=['GET'])
@login_required
def get_theme():
    theme = UserTheme.query.filter_by(user_id=current_user.id).first()
    if not theme:
        theme = UserTheme(user_id=current_user.id)
        db.session.add(theme)
        db.session.commit()

    return jsonify({
        'theme': theme.theme,
        'font_size': theme.font_size,
        'line_spacing': theme.line_spacing,
        'page_margin': theme.page_margin,
        'detail_mode': theme.detail_mode
    })

@bp.route('/admin/invite', methods=['GET', 'POST'])
@login_required
def admin_invite():
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        if not email:
            flash('请输入邮箱地址', 'error')
            return redirect(url_for('main.admin_invite'))

        existing = InviteCode.query.filter_by(target_email=email, is_used=False).first()
        if existing:
            flash(f'该邮箱已有未使用的邀请码: {existing.code}', 'warning')
            return redirect(url_for('main.admin_invite'))

        invite = InviteCode.create_for_email(email, current_user.id)
        flash(f'✅ 邀请码已生成: {invite.code} (有效期7天)', 'success')

    invites = InviteCode.query.order_by(InviteCode.created_at.desc()).all()
    return render_template('admin_invite.html', invites=invites)

@bp.route('/admin/sync')
@login_required
def sync_categories():
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))

    force = request.args.get('force', 'false') == 'true'
    result = CategoryScanner.scan_and_sync(force=force)

    flash(
        f"✅ 同步完成！新增分类 {result['categories_added']} 个，"
        f"更新书籍 {result['books_updated']} 本，"
        f"跳过 {result['books_skipped']} 本（未变更）",
        'success'
    )
    if result['errors']:
        for err in result['errors'][:5]:
            flash(f'⚠️ {err}', 'error')

    return redirect(url_for('main.library'))

@bp.route('/download/<int:book_id>')
@login_required
def download_book(book_id):
    book = Book.query.get_or_404(book_id)
    file_path = Path(current_app.config['BOOKS_DIR']) / book.relative_path

    if not file_path.exists():
        file_path = Path(current_app.config['BOOKS_DIR']) / book.filename

    if file_path.exists():
        return send_file(file_path, as_attachment=True, download_name=book.filename)

    flash('文件不存在', 'error')
    return redirect(url_for('main.library'))

@bp.route('/api/read/<int:book_id>')
@login_required
def get_book_content(book_id):
    book = Book.query.get_or_404(book_id)
    file_path = Path(current_app.config['BOOKS_DIR']) / book.relative_path

    if not file_path.exists():
        file_path = Path(current_app.config['BOOKS_DIR']) / book.filename

    if not file_path.exists():
        return jsonify({'error': '文件不存在'}), 404

    if file_path.suffix.lower() == '.epub':
        return send_file(file_path, mimetype='application/epub+zip')
    elif file_path.suffix.lower() == '.pdf':
        return send_file(file_path, mimetype='application/pdf')
    else:
        return send_file(file_path)

@bp.route('/delete/<int:book_id>', methods=['DELETE'])
@login_required
def delete_book(book_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403

    book = Book.query.get_or_404(book_id)

    file_path = Path(current_app.config['BOOKS_DIR']) / book.relative_path
    if file_path.exists():
        file_path.unlink()

    cover_path = Path('static/covers') / f"{book.id}.jpg"
    if cover_path.exists():
        cover_path.unlink()

    ReadingProgress.query.filter_by(book_id=book.id).delete()

    if book.category:
        book.category.book_count -= 1

    db.session.delete(book)
    db.session.commit()

    return jsonify({'success': True})

# ============ 用户管理 ============

@bp.route('/admin/users')
@login_required
def admin_users():
    """用户管理页面"""
    if not current_user.is_admin:
        flash('需要管理员权限', 'error')
        return redirect(url_for('main.library'))

    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin_users.html', users=users)


@bp.route('/admin/user/<int:user_id>/toggle', methods=['POST'])
@login_required
def toggle_user(user_id):
    """启用/禁用用户"""
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403

    if user_id == current_user.id:
        return jsonify({'success': False, 'error': '不能禁用自己'}), 400

    user = User.query.get_or_404(user_id)
    user.is_active = not user.is_active
    db.session.commit()

    return jsonify({'success': True, 'is_active': user.is_active})


@bp.route('/admin/user/<int:user_id>/delete', methods=['DELETE'])
@login_required
def delete_user(user_id):
    """删除用户"""
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403

    if user_id == current_user.id:
        return jsonify({'success': False, 'error': '不能删除自己'}), 400

    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()

    return jsonify({'success': True})


@bp.route('/admin/user/<int:user_id>/reset-password', methods=['POST'])
@login_required
def reset_user_password(user_id):
    """重置用户密码（管理员操作）"""
    if not current_user.is_admin:
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403

    user = User.query.get_or_404(user_id)
    data = request.json
    new_password = data.get('password', '')

    valid, msg = User.validate_password(new_password)
    if not valid:
        return jsonify({'success': False, 'error': msg}), 400

    user.set_password(new_password)
    db.session.commit()

    return jsonify({'success': True, 'message': '密码已重置'})


@bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    """个人资料页面 - 修改密码和基本信息"""
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'change_password':
            old_password = request.form.get('old_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')

            # 验证旧密码
            if not current_user.check_password(old_password):
                flash('当前密码错误', 'error')
                return render_template('profile.html')

            # 验证新密码强度
            valid, msg = User.validate_password(new_password)
            if not valid:
                flash(msg, 'error')
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

            # 检查用户名是否被占用
            existing = User.query.filter(
                User.username == username,
                User.id != current_user.id
            ).first()
            if existing:
                flash('用户名已被使用', 'error')
                return render_template('profile.html')

            # 检查邮箱是否被占用
            existing = User.query.filter(
                User.email == email,
                User.id != current_user.id
            ).first()
            if existing:
                flash('邮箱已被注册', 'error')
                return render_template('profile.html')

            current_user.username = username
            current_user.email = email
            db.session.commit()
            flash('✅ 个人信息已更新', 'success')

    return render_template('profile.html')
