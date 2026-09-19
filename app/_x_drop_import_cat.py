# -*- coding: utf-8 -*-
"""一次性清理：删除空的书库「导入」分类（探针产物）。用完即删。"""
import sys

sys.path.insert(0, "/app")

from app import create_app          # noqa: E402
from app import db                  # noqa: E402
from app.models import Book, Category  # noqa: E402

app = create_app()
with app.app_context():
    cats = Category.query.filter_by(name="导入").all()
    print("找到「导入」分类 %d 个" % len(cats))
    for c in cats:
        n = Book.query.filter_by(category_id=c.id).count()
        print("  id=%s path=%s 书数=%d" % (c.id, c.path, n))
        if n == 0:
            db.session.delete(c)
            db.session.commit()
            print("  已删除（空分类）")
        else:
            print("  保留（非空，不动）")
    print("剩余分类数:", Category.query.count())
