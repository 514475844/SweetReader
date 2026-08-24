#!/bin/bash

echo "🚀 启动 SweetReader..."

# 停止并删除旧容器
docker rm -f sweetreader 2>/dev/null

# 运行容器
docker run -d \
  --name sweetreader \
  --restart unless-stopped \
  -p 5000:5000 \
  -v /mnt/sata1-1/SweetReader/app:/app/app \
  -v /mnt/sata1-1/SweetReader/templates:/app/templates \
  -v /mnt/sata1-1/SweetReader/static:/app/static \
  -v /mnt/sata1-1/SweetReader/books:/app/books \
  -v /mnt/sata1-1/SweetReader/instance:/app/instance \
  -v /mnt/sata1-1/SweetReader/run.py:/app/run.py \
  -v /mnt/sata1-1/SweetReader/requirements.txt:/app/requirements.txt \
  -w /app \
  -e BOOKS_DIR=/app/books \
  -e SECRET_KEY=sweet-reader-secret-key \
  python:3.11-slim \
  sh -c "pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && pip install --no-cache-dir -r requirements.txt && python run.py"

# 等待容器启动
echo "⏳ 等待依赖安装完成..."
sleep 15

# 检查 Flask 是否安装成功
echo "🔍 检查依赖..."
docker exec sweetreader python -c "import flask" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "✅ 依赖安装完成"
else
    echo "⏳ 依赖还在安装，再等10秒..."
    sleep 10
fi

# 自动初始化数据库
echo "📦 初始化数据库..."
docker exec sweetreader python -c "
from app import create_app
from app.models import db
app = create_app()
with app.app_context():
    db.create_all()
    print('✅ 数据库表创建成功')
" 2>/dev/null

if [ $? -ne 0 ]; then
    echo "⏳ 容器还没准备好，再等5秒重试..."
    sleep 5
    docker exec sweetreader python -c "
from app import create_app
from app.models import db
app = create_app()
with app.app_context():
    db.create_all()
    print('✅ 数据库表创建成功')
" 2>/dev/null
fi

# 自动创建管理员
echo "👤 创建管理员..."
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
" 2>/dev/null

echo ""
echo "🎉 SweetReader 已启动！"
echo "🔗 访问: http://192.168.10.198:5000"
echo "👤 管理员: admin / admin123"
echo ""
echo "📋 查看日志: docker logs -f sweetreader"
