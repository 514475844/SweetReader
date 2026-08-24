#!/bin/bash

echo "🍭 SweetReader 一键初始化"
echo "=========================="

# 创建数据库表
echo "📦 创建数据库表..."
docker exec sweetreader python -c "
from app import create_app
from app.models import db
app = create_app()
with app.app_context():
    db.create_all()
    print('✅ 数据库表创建成功')
"

# 创建管理员
echo "👤 创建管理员账号..."
docker exec sweetreader python -c "
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

# 检查管理员
echo "📋 当前用户列表:"
docker exec sweetreader python -c "
from app import create_app
from app.models import db, User
app = create_app()
with app.app_context():
    users = User.query.all()
    for u in users:
        print(f'  - {u.username} ({u.email}) 管理员: {u.is_admin}')
    if not users:
        print('  (没有用户)')
"

echo ""
echo "🎉 初始化完成！"
echo "🔗 访问: http://192.168.10.198:5000"
echo "👤 管理员: admin / admin123"
