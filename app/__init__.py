from flask import Flask
from flask_login import LoginManager
from flask_migrate import Migrate
from app.models import db, User
import os

login_manager = LoginManager()

def create_app():
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sweet-reader-secret-key-change-me')
    app.config['BOOKS_DIR'] = os.environ.get('BOOKS_DIR', '/app/books')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///sweetreader.db'
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

    return app
