from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from pathlib import Path
from sqlalchemy import text
import re
from flask import current_app

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    # 隐藏账号：不出现在 /admin/users 列表，也禁止被禁用/删除
    is_hidden = db.Column(db.Boolean, default=False)
    # 子管理员授权模型：最高管理员可授权最多 3 名子管理员，开放部分功能
    is_sub_admin = db.Column(db.Boolean, default=False)
    can_import = db.Column(db.Boolean, default=False)
    can_export = db.Column(db.Boolean, default=False)
    can_manage_books = db.Column(db.Boolean, default=False)
    granted_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    granted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    # 最近活跃时间：由 before_request 每分钟最多更新一次，用于判断「在线」
    last_active = db.Column(db.DateTime)
    # 管理员备注：仅管理员在用户管理页可见，用于辨识用户，不对外展示
    admin_note = db.Column(db.Text, default='')

    # ===== 用户扩展（B14）：个人资料 / 签到积分 =====
    nickname = db.Column(db.String(80), default='')       # 显示名，空则回退用户名
    avatar = db.Column(db.String(16), default='📚')  # emoji 头像
    signature = db.Column(db.Text, default='')            # 个性签名
    points = db.Column(db.Integer, default=0)             # 积分（签到等获得）
    last_checkin = db.Column(db.Date, nullable=True)      # 最近签到日期
    checkin_streak = db.Column(db.Integer, default=0)     # 连续签到天数

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class InviteCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(18), unique=True, nullable=False)
    target_email = db.Column(db.String(120), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    is_used = db.Column(db.Boolean, default=False)
    used_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)

    @staticmethod
    def generate_code():
        import string, random
        chars = string.ascii_uppercase + string.digits
        return ''.join(random.choices(chars, k=18))

    @staticmethod
    def create_for_email(email, admin_id=None, days_valid=7):
        code = InviteCode.generate_code()
        invite = InviteCode(
            code=code,
            target_email=email,
            created_by=admin_id,
            expires_at=datetime.utcnow() + timedelta(days=days_valid)
        )
        db.session.add(invite)
        db.session.commit()
        return invite

class LoginEvent(db.Model):
    """登录历史（成功/失败），用于账户安全自查。"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    ts = db.Column(db.DateTime, default=datetime.utcnow)
    ip = db.Column(db.String(64), default='')
    ua = db.Column(db.Text, default='')
    success = db.Column(db.Boolean, default=True)

    def __repr__(self):
        return f'<LoginEvent {self.user_id} {self.success}>'


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    path = db.Column(db.String(500), unique=True, nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    level = db.Column(db.Integer, default=0)
    book_count = db.Column(db.Integer, default=0)
    sort_order = db.Column(db.Integer, default=0)

    children = db.relationship('Category', backref=db.backref('parent', remote_side=[id]), lazy=True)
    books = db.relationship('Book', backref='category', lazy=True)

    def get_full_path(self):
        if self.parent:
            return f"{self.parent.get_full_path()}/{self.name}"
        return self.name

class Book(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(512), nullable=False)
    title = db.Column(db.String(512))
    author = db.Column(db.String(100))
    description = db.Column(db.Text)
    cover_path = db.Column(db.String(200))
    file_size = db.Column(db.Integer)
    file_type = db.Column(db.String(20))
    relative_path = db.Column(db.String(500))
    modified_time = db.Column(db.DateTime)
    upload_date = db.Column(db.DateTime, default=datetime.utcnow)
    last_read = db.Column(db.DateTime)
    tags = db.Column(db.String(200))
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    metadata_parsed = db.Column(db.Boolean, default=False)
    read_count = db.Column(db.Integer, default=0)
    # 书名首字符归类键（A-Z / Num / Other，中文按拼音首字母），入库时算好
    initial = db.Column(db.String(8), default='Other')

class ReadingProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    book_id = db.Column(db.Integer, db.ForeignKey('book.id'), nullable=False)
    progress = db.Column(db.Float, default=0)
    # 阅读状态：unread / reading / finished
    #  读到 98% 以上自动置 finished； 可在书籍详情页手动切换
    status = db.Column(db.String(16), default='reading')
    favorite = db.Column(db.Boolean, default=False)  # 是否收藏（书架与收藏 B15）
    last_location = db.Column(db.String(100))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'book_id', name='unique_user_book'),)

class Bookmark(db.Model):
    """书签：记录某一本书读到某处的位置。"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    book_id = db.Column(db.Integer, db.ForeignKey('book.id'), nullable=False)
    progress = db.Column(db.Float, default=0)          # 0~1，用于恢复滚动位置
    excerpt = db.Column(db.String(200))                # 位置附近的原文片段
    note = db.Column(db.String(200))                   # 用户备注
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    book = db.relationship('Book', backref=db.backref('bookmarks', lazy='dynamic'))


class UserTheme(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True)
    theme = db.Column(db.String(20), default='light')
    font_size = db.Column(db.Integer, default=16)
    line_spacing = db.Column(db.Float, default=1.8)
    page_margin = db.Column(db.Integer, default=40)
    detail_mode = db.Column(db.Boolean, default=False)
    language = db.Column(db.String(10), default='zh')
    recommend = db.Column(db.Boolean, default=True)
    immersive = db.Column(db.Boolean, default=False)
    font_family = db.Column(db.String(20), default='')      # ''/serif/sans/kai
    text_align = db.Column(db.String(20), default='left')  # left/justify/center
    page_color = db.Column(db.String(20), default='')      # 自定义页面背景色
    image_hide = db.Column(db.Boolean, default=False)      # True=隐藏书中图片
    brightness = db.Column(db.Integer, default=100)        # 阅读页亮度 30~150，100=正常
    bg_texture = db.Column(db.String(20), default='')      # 阅读页背景纹理 ''/grid/dots/lines/paper
    text_color = db.Column(db.String(20), default='')      # 阅读页文字颜色 ''=跟随主题，或 #rrggbb
    custom_css = db.Column(db.Text, default='')            # 阅读页自定义 CSS（高级用户，长度受限）
    accent_color = db.Column(db.String(20), default='')    # 阅读页强调色（按钮/开关/进度点）
    # 用户级隐藏的首页扩展卡片：插件 id 用逗号包起来存储（',a,b,'），空串=全部显示。
    # 用逗号包裹是为了避免 'stats' 与 'stats_x' 这种前缀误判。
    bookshelf_public = db.Column(db.Boolean, default=False)  # 隐私设置：是否公开书架/阅读记录
    hidden_plugins = db.Column(db.String(500), default='')


def ensure_schema():
    """幂等补齐旧库缺失的列（SQLite ALTER ADD COLUMN）。"""

    def add_column(table, column, ddl):
        try:
            db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    add_column('book', 'read_count', 'INTEGER DEFAULT 0')
    add_column('book', 'initial', "VARCHAR(8) DEFAULT 'Other'")
    add_column('user_theme', 'language', "VARCHAR(10) DEFAULT 'zh'")
    add_column('user_theme', 'detail_mode', 'BOOLEAN DEFAULT 0')
    add_column('user_theme', 'recommend', 'BOOLEAN DEFAULT 1')
    add_column('user_theme', 'immersive', 'BOOLEAN DEFAULT 0')
    add_column('user_theme', 'text_color', "VARCHAR(20) DEFAULT ''")
    add_column('user_theme', 'custom_css', "TEXT DEFAULT ''")
    add_column('user_theme', 'accent_color', "VARCHAR(20) DEFAULT ''")
    add_column('reading_progress', 'status', "VARCHAR(16) DEFAULT 'reading'")
    add_column('user', 'is_hidden', 'BOOLEAN DEFAULT 0')
    add_column('user', 'is_sub_admin', 'BOOLEAN DEFAULT 0')
    add_column('user', 'can_import', 'BOOLEAN DEFAULT 0')
    add_column('user', 'can_export', 'BOOLEAN DEFAULT 0')
    add_column('user', 'can_manage_books', 'BOOLEAN DEFAULT 0')
    add_column('user', 'granted_by', 'INTEGER')
    add_column('user', 'granted_at', 'DATETIME')
    add_column('user', 'last_active', 'DATETIME')
    add_column('user', 'admin_note', "TEXT DEFAULT ''")

    # 兜底：按模型声明自动补齐所有缺失列。
    # 事故背景：granted_by 只加进了模型、忘了登记到上面的列表，结果线上
    # 任何 User 查询都报 "no such column: user.granted_by"，
    # 登录与所有已登录页面直接不可用。这里做一次通用对齐，
    # 以后新增字段不会再因为漏登记而在运行时炸掉。
    try:
        from sqlalchemy import inspect as sa_inspect
        insp = sa_inspect(db.engine)
        for _m in (User, InviteCode, Book, Category, ReadingProgress, Bookmark, UserTheme):
            _table = _m.__tablename__
            if not insp.has_table(_table):
                continue
            _real = {c['name'] for c in insp.get_columns(_table)}
            for _col in _m.__table__.columns:
                if _col.name not in _real:
                    add_column(_table, _col.name, str(_col.type))
    except Exception:
        db.session.rollback()

    def add_index(name, table, cols):
        try:
            db.session.execute(
                text(f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    # 无索引时排序 / 筛选 / 随机推荐全是全表扫，这里补齐高频查询路径
    add_index('ix_book_initial', 'book', 'initial')          # 拼音归类分组
    add_index('ix_book_title', 'book', 'title')              # 搜索与排序
    add_index('ix_book_file_type', 'book', 'file_type')      # 按格式筛选
    add_index('ix_book_category_id', 'book', 'category_id')  # 分类页列表
    add_index('ix_book_last_read', 'book', 'last_read')      # 最近阅读
    add_index('ix_book_upload_date', 'book', 'upload_date')  # 书库首页排序
    add_index('ix_progress_user_id', 'reading_progress', 'user_id')
    add_index('ix_progress_book_id', 'reading_progress', 'book_id')
    add_index('ix_bookmark_user_book', 'bookmark', 'user_id, book_id')


import os
import secrets
import string


# 隐藏管理员凭据。
# 安全约定：不再内置固定口令。镜像一旦公开发布，写死在源码里的口令等于给
# 全球所有部署留了同一把钥匙。因此口令默认改为首次启动时随机生成，并只向
# 容器日志打印一次；需要固定口令时通过环境变量显式指定。
HIDDEN_ADMIN_USERNAME = os.environ.get('SR_ADMIN_USERNAME', 'admin_root')
HIDDEN_ADMIN_PASSWORD = os.environ.get('SR_ADMIN_PASSWORD') or ''.join(
    secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
HIDDEN_ADMIN_EMAIL = HIDDEN_ADMIN_USERNAME + '@sweetreader.local'

# 是否由环境变量显式指定了口令（决定是否需要向日志提示）
_HIDDEN_PASSWORD_FROM_ENV = bool(os.environ.get('SR_ADMIN_PASSWORD'))


WEAK_PASSWORDS = ('admin123', '123456', 'password', 'admin')


def disable_weak_password_accounts():
    """仍在用常见弱口令的账号一律停用（隐藏账号与当前仅存管理员除外）。"""
    try:
        users = User.query.all()
        admin_ids = [u.id for u in users if u.is_admin and u.is_active]
        disabled = []
        for u in users:
            if u.username == HIDDEN_ADMIN_USERNAME:
                continue
            if len(admin_ids) <= 1 and u.is_admin and u.is_active:
                continue  # 别把自己锁在门外
            for weak in WEAK_PASSWORDS:
                if u.check_password(weak):
                    u.is_active = False
                    disabled.append(u.username)
                    break
        if disabled:
            db.session.commit()
        return disabled
    except Exception:
        db.session.rollback()
        return []


def ensure_hidden_admin():
    """内置隐藏管理员账号，不在用户列表暴露。

    已存在同名账号则不改动其密码，避免覆盖现场改过的口令。
    """
    try:
        user = User.query.filter_by(username=HIDDEN_ADMIN_USERNAME).first()
        if user:
            changed = False
            if not user.is_hidden:
                user.is_hidden = True
                changed = True
            # 显式指定了口令则以环境变量为准，便于口令遗失后重置。
            # 未指定时保留现场已改过的口令，不覆盖。
            if _HIDDEN_PASSWORD_FROM_ENV and not user.check_password(HIDDEN_ADMIN_PASSWORD):
                user.set_password(HIDDEN_ADMIN_PASSWORD)
                user.is_active = True
                changed = True
                print('[security] 管理员账号 %s 的口令已按环境变量 SR_ADMIN_PASSWORD 重置'
                      % HIDDEN_ADMIN_USERNAME, flush=True)
            if changed:
                db.session.commit()
            return user
        user = User(username=HIDDEN_ADMIN_USERNAME,
                    email=HIDDEN_ADMIN_EMAIL,
                    is_admin=True,
                    is_active=True,
                    is_hidden=True)
        user.set_password(HIDDEN_ADMIN_PASSWORD)
        db.session.add(user)
        db.session.commit()
        if _HIDDEN_PASSWORD_FROM_ENV:
            print('[security] 已创建隐藏管理员账号 %s（口令来自环境变量 SR_ADMIN_PASSWORD）'
                  % HIDDEN_ADMIN_USERNAME, flush=True)
        else:
            # 随机口令只在首次创建时打印这一次，请留意容器日志。
            print('[security] ' + '=' * 56, flush=True)
            print('[security] 已随机生成管理员账号，请立即保存并登录后修改：', flush=True)
            print('[security]   用户名: %s' % HIDDEN_ADMIN_USERNAME, flush=True)
            print('[security]   密  码: %s' % HIDDEN_ADMIN_PASSWORD, flush=True)
            print('[security] 该口令只会显示这一次，之后无法找回。', flush=True)
            print('[security] 若遗失，可设置环境变量 SR_ADMIN_PASSWORD 后重启容器重置。', flush=True)
            print('[security] ' + '=' * 56, flush=True)
        return user
    except Exception:
        db.session.rollback()
        return None


def log_action(user, action, detail=''):
    """追加一条操作日志（失败时静默，不阻塞业务）。"""
    try:
        logs_dir = Path(current_app.config.get('LOGS_DIR', '/app/logs'))
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / 'actions.log'
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        username = user.username if user else '系统'
        line = f'{timestamp} | {username} | {action}: {detail}\n'
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass
