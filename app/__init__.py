from flask import Flask
from flask_login import LoginManager
from flask_migrate import Migrate
from app.models import (db, User, ensure_schema, ensure_hidden_admin,
                        disable_weak_password_accounts)
import os
import secrets

login_manager = LoginManager()


def _resolve_secret_key(instance_path):
    """解析会话签名密钥。

    绝不回落到固定的默认字符串：公开发布的镜像里写一个人尽皆知的常量，
    等于允许任何人伪造 session cookie 直接拿到管理员身份。
    默认行为改为首次运行时随机生成并持久化到 instance/secret.key，
    这样既保证重启后登录状态不失效，又不会产生通用后门。
    """
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    key_file = os.path.join(instance_path, 'secret.key')
    try:
        with open(key_file, 'r', encoding='utf-8') as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(key_file, 'w', encoding='utf-8') as f:
            f.write(key)
        os.chmod(key_file, 0o600)
        print('[security] 已生成随机 SECRET_KEY 并保存至 %s' % key_file, flush=True)
    except OSError as e:
        print('[security] 无法持久化 SECRET_KEY（重启后需重新登录）：%s' % e, flush=True)
    return key

def create_app():
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    instance_path = os.environ.get('INSTANCE_DIR', '/app/instance')
    os.makedirs(instance_path, exist_ok=True)

    app.config['SECRET_KEY'] = _resolve_secret_key(instance_path)
    app.config['BOOKS_DIR'] = os.environ.get('BOOKS_DIR', '/app/books')
    app.config['LOGS_DIR'] = os.environ.get('LOGS_DIR', '/app/logs')

    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URI',
        f"sqlite:///{os.path.join(instance_path, 'sweetreader.db')}"
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

    db.init_app(app)
    migrate = Migrate(app, db)

    login_manager.init_app(app)
    login_manager.login_view = 'main.login'
    login_manager.login_message = '请先登录'

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from app import routes
    app.register_blueprint(routes.bp)

    # 注册已启用的插件蓝图（用户插件可拥有独立页面/路由/模板）
    from app.plugins import registry
    registry.load()
    for _bp in registry.blueprints() + registry.settings_blueprints():
        try:
            if _bp.name not in app.blueprints:
                app.register_blueprint(_bp)
        except Exception as e:
            print('[plugins] 注册蓝图失败 %s: %s' % (getattr(_bp, 'name', '?'), e), flush=True)

    with app.app_context():
        db.create_all()
        ensure_schema()
        ensure_hidden_admin()
        disable_weak_password_accounts()

    return app
