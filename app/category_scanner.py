import os
from pathlib import Path
from datetime import datetime
from flask import current_app
from app.models import db, Category, Book
from app.utils import BookUtils

class CategoryScanner:

    @classmethod
    def _update_category_counts(cls):
        """[B5] 一次 GROUP BY 取代 O(n^2) 递归 COUNT。

        旧实现对每个分类递归跑一次 COUNT，分类一多就是灾难级复杂度。
        新实现：先取每个分类的直属书数，再按 level 从深到浅累加到父级。
        """
        from sqlalchemy import func
        from app.models import Category, Book

        all_cats = Category.query.all()
        if not all_cats:
            return

        direct = {c.id: 0 for c in all_cats}
        for cid, n in db.session.query(
                Book.category_id, func.count(Book.id)
        ).group_by(Book.category_id).all():
            if cid in direct:
                direct[cid] = n

        by_level = {}
        for c in all_cats:
            by_level.setdefault(c.level or 0, []).append(c)
        for lvl in sorted(by_level.keys(), reverse=True):
            for c in by_level[lvl]:
                if c.parent_id and c.parent_id in direct:
                    direct[c.parent_id] += direct[c.id]

        for c in all_cats:
            c.book_count = direct[c.id]
        db.session.commit()

    SUPPORTED_EXTS = {
        '.epub', '.pdf', '.mobi', '.azw3', '.txt',
        '.doc', '.docx', '.rtf', '.odt',
        '.fb2', '.cbz', '.cbr'
    }
    BATCH_SIZE = 500
    _pending = 0

    @classmethod
    def scan_and_sync(cls, books_dir=None, force=False, incremental=True,
                      result_out=None):
        """扫描书库并同步到数据库。

        result_out: 传入一个外部 dict 时，扫描过程会实时写入其中，
        供 /api/scan-progress 轮询进度（B3 异步扫描用）。
        """
        if books_dir is None:
            books_dir = Path(current_app.config['BOOKS_DIR'])

        books_dir = Path(books_dir)
        if not books_dir.exists():
            return {'categories_added': 0, 'books_updated': 0, 'errors': []}

        init = {'categories_added': 0, 'books_updated': 0, 'books_skipped': 0,
                'symlinks_skipped': 0, 'categories_removed': 0,
                'categories_scanned': 0, 'categories_estimate': 0,
                'errors': []}
        if result_out is None:
            result = dict(init)
        else:
            result_out.clear()
            result_out.update(init)
            result = result_out
        cls._pending = 0

        existing_categories = {c.path: c for c in Category.query.all()}
        # 进度预估：首次扫描时用 0，之后用上次分类数
        result['categories_estimate'] = len(existing_categories) or 1
        existing_books = {}
        for b in Book.query.all():
            key = f"{b.category_id}_{b.filename}"
            existing_books[key] = b

        # [B2] 原先这里再全表遍历一次拼 existing_files（数十万次字符串拼接），
        # 而它只服务于那个 key 写错的增量判断。已移除，增量统一在
        # _sync_books_batch 里按 category.id 命中。
        cls._scan_directory_fast(
            books_dir, None, '',
            result, existing_categories, existing_books, force, incremental, None
        )

        # [B4] 收尾：冲刷批量提交的剩余部分
        db.session.commit()
        cls._pending = 0

        cls._cleanup_orphans(books_dir, result)
        cls._update_category_counts()
        return result

    @classmethod
    def _scan_directory_fast(cls, path, parent_category, relative_path,
                             result, existing_categories, existing_books, force,
                             incremental=True, existing_files=None):
        path = Path(path)

        try:
            items = list(path.iterdir())
        except PermissionError:
            result['errors'].append(f"无法读取目录: {path}")
            return

        subdirs = []
        files = []

        for item in items:
            # [P0 修复] 跳过符号链接目录。书库里可能存在循环软链，
            # is_dir() 对软链返回 True，若不跳过将无限递归直接栈溢出。
            if item.is_dir() and item.is_symlink():
                result['symlinks_skipped'] = result.get('symlinks_skipped', 0) + 1
                continue
            if item.is_dir() and not item.name.startswith('.'):
                subdirs.append(item)
            elif item.is_file() and item.suffix.lower() in cls.SUPPORTED_EXTS:
                # [B2] 此处原先用 `parent_category.id` 拼 key，而实际写入用的是
                # 本目录新建的 `category.id`，两者不一致导致增量永不命中。
                # 增量判断统一交给 _sync_books_batch（那里用的是正确的 category.id）。
                files.append(item)

        if files or subdirs:
            category = cls._get_or_create_category_fast(
                path.name, parent_category, relative_path,
                result, existing_categories
            )
            result['categories_scanned'] = result.get('categories_scanned', 0) + 1

            if files:
                cls._sync_books_batch(
                    files, category, relative_path,
                    result, existing_books, force
                )

            for subdir in subdirs:
                new_relative = f"{relative_path}/{subdir.name}" if relative_path else subdir.name
                cls._scan_directory_fast(
                    subdir, category, new_relative,
                    result, existing_categories, existing_books, force, incremental, existing_files
                )

    @classmethod
    def _get_or_create_category_fast(cls, name, parent, relative_path,
                                     result, existing_categories):
        # [P0 修复] path 必须是相对 books_dir 的路径。
        # 旧实现用 `parent.get_full_path() + name`，根目录 books_dir 本身也被建了
        # 分类(path='books')，于是子分类变成 'books/xxx'；_cleanup_orphans 再拼
        # books_dir/'books/xxx' -> /app/books/books/xxx 永不存在 -> 全库误删。
        # 根目录 relative_path 为空，对应 path=''，即 books_dir 本身。
        full_path = relative_path if relative_path else ''

        category = existing_categories.get(full_path)

        if not category:
            category = Category(
                name=name,
                path=full_path,
                parent_id=parent.id if parent else None,
                level=parent.level + 1 if parent else 0
            )
            db.session.add(category)
            db.session.flush()
            existing_categories[full_path] = category
            result['categories_added'] += 1

        return category

    @classmethod
    def _sync_books_batch(cls, file_paths, category, relative_path,
                          result, existing_books, force):
        books_to_add = []
        books_to_update = []

        for file_path in file_paths:
            filename = file_path.name
            key = f"{category.id}_{filename}"

            book = existing_books.get(key)
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime)

            if book:
                if not force and book.modified_time and book.modified_time >= mtime:
                    result['books_skipped'] += 1
                    continue
                book.file_size = file_path.stat().st_size
                book.modified_time = mtime
                books_to_update.append(book)
            else:
                title = Path(filename).stem
                book = Book(
                    filename=filename,
                    title=title,
                    initial=BookUtils.get_title_initial(title),
                    category_id=category.id,
                    relative_path=f"{relative_path}/{filename}" if relative_path else filename,
                    file_size=file_path.stat().st_size,
                    file_type=BookUtils.get_file_type(filename),
                    modified_time=mtime,
                    metadata_parsed=False
                )
                books_to_add.append(book)
                existing_books[key] = book
                result['books_updated'] += 1

        if books_to_add:
            db.session.add_all(books_to_add)

        if books_to_add or books_to_update:
            # [B4] 旧实现每个目录 2 次 commit，目录与书籍一多
            # 就是上万次提交。改为 flush 拿 id、按 BATCH_SIZE 批量提交。
            # book_count 不再在这里算，统一由 _update_category_counts 收尾。
            db.session.flush()
            cls._pending += len(books_to_add) + len(books_to_update)
            if cls._pending >= cls.BATCH_SIZE:
                db.session.commit()
                cls._pending = 0

        if len(books_to_add) > 0:
            cls._parse_metadata_batch(books_to_add[:50])

    @classmethod
    def _parse_metadata_batch(cls, books):
        books_dir = Path(current_app.config['BOOKS_DIR'])
        for book in books:
            if book.metadata_parsed:
                continue
            try:
                file_path = books_dir / book.relative_path
                if file_path.exists() and book.filename.endswith('.epub'):
                    metadata = BookUtils.extract_epub_metadata(file_path)
                    if metadata:
                        book.title = metadata.get('title', book.title)
                        # 标题可能被元数据改写，归类键要跟着重算
                        book.initial = BookUtils.get_title_initial(book.title)
                        book.author = metadata.get('author', '未知作者')
                        book.description = metadata.get('description', '')
                        if metadata.get('cover'):
                            BookUtils.save_cover(metadata['cover'], book.id)
                        book.metadata_parsed = True
            except Exception as e:
                pass
        # [B4] 旧实现每本书 1 次 commit，书籍一多就是数万次提交
        db.session.commit()

    @classmethod
    def _cleanup_orphans(cls, books_dir, result):
        all_categories = Category.query.all()
        total = len(all_categories)
        doomed = []

        for category in all_categories:
            # [P0 修复] path 为空表示 books_dir 根目录本身
            rel = (category.path or '').replace('/', os.sep)
            full_path = books_dir if not rel else (books_dir / rel)
            if full_path.exists():
                continue
            # [P0 修复续] 目录不在，但分类下还有文件真实存在的书（导入的书、
            # 用户自建的逻辑分类）——这是逻辑分类而非目录分类。删分类会连坐
            # 删书，而磁盘文件其实好好的，属于纯数据损失。保留它们。
            if cls._has_live_books(category, books_dir):
                continue
            doomed.append(category)

        # [P0 熔断] 误杀保护：待删分类超过总数 50% 时判定为路径异常，
        # 放弃清理并记错，宁可留下脏数据也绝不批量删书。
        if total and len(doomed) > total * 0.5:
            result.setdefault('errors', []).append(
                f"[GUARD] 疑似路径异常：{len(doomed)}/{total} 个分类被判为孤儿，"
                f"已中止清理（阈值 50%）"
            )
            return

        # [C3] 没开外键级联，删书前必须手动清进度，否则留下悬空 reading_progress
        from app.models import ReadingProgress

        for category in doomed:
            for book in category.books:
                ReadingProgress.query.filter_by(book_id=book.id).delete(
                    synchronize_session=False)
                db.session.delete(book)
            db.session.delete(category)
            result['categories_removed'] = result.get('categories_removed', 0) + 1
        if doomed:
            db.session.commit()

        orphan_books = Book.query.filter_by(category_id=None).all()
        # [P0 修复续] 同理：文件还在的书一律不删，只回收文件确实没了的行
        dead = [b for b in orphan_books
                if not cls._book_file_exists(b, books_dir)]
        for book in dead:
            ReadingProgress.query.filter_by(book_id=book.id).delete(
                synchronize_session=False)
            db.session.delete(book)
        if dead:
            db.session.commit()

    @staticmethod
    def _book_file_exists(book, books_dir):
        """书籍文件在磁盘上是否真实存在。"""
        rel = (book.relative_path or book.filename or '')
        if not rel:
            return False
        try:
            return (books_dir / rel).exists()
        except Exception:
            return False

    @classmethod
    def _has_live_books(cls, category, books_dir):
        """分类下是否存在文件仍然在磁盘上的书。"""
        try:
            for b in category.books:
                if cls._book_file_exists(b, books_dir):
                    return True
        except Exception:
            # 取不到就当“有书”，宁可留着脏数据也不删
            return True
        return False
