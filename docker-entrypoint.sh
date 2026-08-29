#!/bin/sh
set -e

echo "📦 检查数据库..."

# 如果数据库不存在，初始化
if [ ! -f /app/instance/sweetreader.db ]; then
    echo "📦 数据库不存在，开始初始化..."
    python -c "
from app import create_app
from app.models import db
app = create_app()
with app.app_context():
    db.create_all()
    print('✅ 表创建完成')
"
    
    echo "👤 创建管理员账号..."
    python -c "
from app import create_app
from app.models import db, User
app = create_app()
with app.app_context():
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(username='admin', email='admin@sweetreader.com', is_admin=True)
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()
        print('✅ 管理员创建成功: admin / admin123')
    else:
        print('✅ 管理员已存在')
"
    echo "✅ 数据库初始化完成！"
else
    echo "✅ 数据库已存在，跳过初始化"
fi

# 启动应用
echo "🚀 启动 SweetReader..."
exec python run.py
