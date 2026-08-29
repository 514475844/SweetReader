from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import re

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

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
    filename = db.Column(db.String(200), nullable=False)
    title = db.Column(db.String(200))
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

class ReadingProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    book_id = db.Column(db.Integer, db.ForeignKey('book.id'), nullable=False)
    progress = db.Column(db.Float, default=0)
    last_location = db.Column(db.String(100))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'book_id', name='unique_user_book'),)

class UserTheme(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True)
    theme = db.Column(db.String(20), default='light')
    font_size = db.Column(db.Integer, default=16)
    line_spacing = db.Column(db.Float, default=1.8)
    page_margin = db.Column(db.Integer, default=40)
    detail_mode = db.Column(db.Boolean, default=False)
    language = db.Column(db.String(10), default='zh')
