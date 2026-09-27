#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A7 隔离实验室：真机复现「导入书籍 → 全量扫描 → 分组丢失/重复」。

完全隔离：DB 是生产库的只读副本（sqlite3 backup 拷到 /tmp），
BOOKS_DIR 指向 /tmp 下的临时目录。生产库与  目录零影响。

用法：docker exec sweetreader python3 /_lab_a7.py [label]
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, '/app')
os.chdir('/app')

LAB = '/tmp/a7lab'
PROD_DB = '/app/instance/sweetreader.db'


def prep():
    shutil.rmtree(LAB, ignore_errors=True)
    os.makedirs(LAB + '/instance', exist_ok=True)
    os.makedirs(LAB + '/books/玄幻', exist_ok=True)
    os.makedirs(LAB + '/books/都市', exist_ok=True)
    # 模拟真实书库：两个目录各两本书
    for d, names in (('玄幻', ['斗破苍穹.txt', '完美世界.txt']),
                     ('都市', ['重生之都市.txt', '校园高手.txt'])):
        for n in names:
            with open('%s/books/%s/%s' % (LAB, d, n), 'w', encoding='utf-8') as f:
                f.write('第一章 测试内容\n' * 50)
    # 生产 DB 只读副本
    src = sqlite3.connect('file:%s?mode=ro' % PROD_DB, uri=True)
    dst = sqlite3.connect(LAB + '/lab.db')
    src.backup(dst)
    dst.close()
    src.close()
    # 清空书目相关表，只留一个干净的小书库，避免 6 万行干扰判断
    con = sqlite3.connect(LAB + '/lab.db')
    con.executescript('DELETE FROM book; DELETE FROM category; DELETE FROM reading_progress;')
    con.commit()
    con.close()
    print('lab ready: db=%d bytes' % os.path.getsize(LAB + '/lab.db'))


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else 'run'
    prep()

    os.environ['INSTANCE_DIR'] = LAB + '/instance'
    os.environ['BOOKS_DIR'] = LAB + '/books'
    os.environ['DATABASE_URI'] = 'sqlite:///%s/lab.db' % LAB
    os.environ['SECRET_KEY'] = 'lab-secret-key'

    from app import create_app
    from app.models import db, Book, Category, User
    from app.category_scanner import CategoryScanner

    app = create_app()
    User.check_password = lambda self, p: True

    with app.app_context():
        # 先扫一遍建立基线分类
        r0 = CategoryScanner.scan_and_sync()
        print('[基线扫描] %s' % json.dumps(
            {k: v for k, v in r0.items() if k != 'errors'}, ensure_ascii=False))
        cats = [(c.id, c.name, c.path, c.book_count) for c in Category.query.all()]
        print('[基线分类] %s' % json.dumps(cats, ensure_ascii=False))
        print('[基线书数] %d' % Book.query.count())

    c = app.test_client()
    r = c.post('/login', data={'username': '514475844', 'password': 'x'})
    print('[登录] status=%s loc=%s' % (r.status_code, r.headers.get('Location')))

    # ---- 导入两本书（不勾自动分类）----
    up = {'files': [(io.BytesIO('导入测试书A 第一章\n'.encode('utf-8') * 30),
                     '导入测试书A.txt'),
                    (io.BytesIO('导入测试书B 第一章\n'.encode('utf-8') * 30),
                     '导入测试书B.txt')],
          'auto_create_category': ''}
    r = c.post('/api/admin/import', data=up, content_type='multipart/form-data')
    body = r.get_data(as_text=True)
    print('[导入] status=%s body=%s' % (r.status_code, body[:600]))

    with app.app_context():
        imp = Category.query.filter_by(name='导入').first()
        print('[导入后] 导入分类: %s' % (
            None if not imp else 'id=%d path=%r level=%s parent=%s' % (
                imp.id, imp.path, imp.level, imp.parent_id)))
        for b in Book.query.filter(Book.filename.like('导入测试书%')).all():
            p = os.path.join(LAB, 'books', b.relative_path or b.filename)
            print('  row id=%-4d cat=%-4s filename=%-18r rel=%-25r on_disk=%s'
                  % (b.id, b.category_id, b.filename, b.relative_path,
                     os.path.exists(p)))
        after_import = Book.query.count()

    # ---- 全量扫描 ----
    with app.app_context():
        t0 = time.time()
        r1 = CategoryScanner.scan_and_sync(force=False, incremental=True)
        print('[扫描] %.1fs %s' % (time.time() - t0, json.dumps(
            {k: v for k, v in r1.items() if k != 'errors'}, ensure_ascii=False)))
        if r1.get('errors'):
            print('[扫描错误] %s' % json.dumps(r1['errors'][:5], ensure_ascii=False))

        print('[扫描后] 书总数 %d (导入前基线 %d)' % (Book.query.count(), after_import))
        rows = Book.query.filter(Book.filename.like('导入测试书%')).all()
        print('[扫描后] 导入书行数 = %d  <-- 期望 2' % len(rows))
        for b in rows:
            cat = Category.query.get(b.category_id)
            p = os.path.join(LAB, 'books', b.relative_path or b.filename)
            print('  row id=%-4d cat=%-4s catname=%-10r catpath=%-14r rel=%-25r on_disk=%s'
                  % (b.id, b.category_id, cat.name if cat else '?',
                     cat.path if cat else '?', b.relative_path, os.path.exists(p)))

        imp = Category.query.filter_by(name='导入').first()
        if imp:
            n = Book.query.filter_by(category_id=imp.id).count()
            print('[扫描后] 导入分类 id=%d path=%r 下书数=%d  <-- 期望 2'
                  % (imp.id, imp.path, n))
            for b in Book.query.filter_by(category_id=imp.id).all():
                print('     - %r' % b.filename)
        else:
            print('[扫描后] 导入分类不存在！<-- 分组丢失')

        # 重复检测：同 (category_id, filename)
        dup = db.session.query(
            Book.category_id, Book.filename, db.func.count(Book.id)
        ).group_by(Book.category_id, Book.filename).having(
            db.func.count(Book.id) > 1).all()
        print('[扫描后] 同分类同名重复 = %d  <-- 期望 0' % len(dup))
        for d in dup:
            print('     dup cat=%s name=%r n=%d' % d)

        # ---- 第二轮：压缩包 + 自动创建分类 ----
        buf = io.BytesIO()
        import zipfile
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('玄幻/打包玄幻书.txt', '打包测试\n' * 20)
            z.writestr('新分类/子目录/打包子书.txt', '打包测试\n' * 20)
        buf.seek(0)
        up2 = {'files': [(buf, 'batch.zip')], 'auto_create_category': '1'}
        r = c.post('/api/admin/import', data=up2, content_type='multipart/form-data')
        print('[导入-zip] status=%s body=%s' % (r.status_code,
                                                r.get_data(as_text=True)[:500]))

        for b in Book.query.filter(Book.filename.like('打包%')).all():
            cat = Category.query.get(b.category_id)
            p = os.path.join(LAB, 'books', b.relative_path or b.filename)
            print('  row id=%-4d cat=%-4s catname=%-12r catpath=%-22r rel=%-30r on_disk=%s'
                  % (b.id, b.category_id, cat.name if cat else '?',
                     cat.path if cat else '?', b.relative_path, os.path.exists(p)))

        t0 = time.time()
        r2 = CategoryScanner.scan_and_sync()
        print('[扫描2] %.1fs %s' % (time.time() - t0, json.dumps(
            {k: v for k, v in r2.items() if k != 'errors'}, ensure_ascii=False)))

        total = Book.query.count()
        print('[扫描2后] 书总数 %d  <-- 期望 8 (4基线+2导入+2打包)' % total)
        dup2 = db.session.query(
            Book.category_id, Book.filename, db.func.count(Book.id)
        ).group_by(Book.category_id, Book.filename).having(
            db.func.count(Book.id) > 1).all()
        print('[扫描2后] 同分类同名重复 = %d  <-- 期望 0' % len(dup2))
        for d in dup2:
            print('     dup cat=%s name=%r n=%d' % d)
        print('[扫描2后] 分类清单:')
        for cc in Category.query.order_by(Category.id).all():
            n = Book.query.filter_by(category_id=cc.id).count()
            print('   id=%-3d name=%-12r path=%-24r parent=%-4s lvl=%s books=%d'
                  % (cc.id, cc.name, cc.path, cc.parent_id, cc.level, n))
        imp = Category.query.filter_by(name='导入').first()
        if imp:
            print('[扫描2后] 导入分类 id=%d path=%r 下书数=%d（含子分类）'
                  % (imp.id, imp.path, Book.query.filter_by(category_id=imp.id).count()))

        # 磁盘文件完整性
        disk = []
        for root, _, fs in os.walk(LAB + '/books'):
            for f in fs:
                disk.append(os.path.relpath(os.path.join(root, f), LAB + '/books'))
        print('[磁盘] books 下文件 %d 个: %s' % (len(disk), sorted(disk)))
        db.session.rollback()

    print('=== LAB %s DONE (生产库与  未触碰) ===' % label)


if __name__ == '__main__':
    main()
