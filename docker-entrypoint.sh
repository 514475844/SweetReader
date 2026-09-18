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
    
# 注意：这里不再创建固定的默认弱口令账号。
# 管理员账号由应用在首次启动时生成（隐藏账号），凭据打印在容器日志中，
# 详见下方「获取初始密码」一节。

# 启动应用
echo "🚀 启动 SweetReader..."
exec python run.py
