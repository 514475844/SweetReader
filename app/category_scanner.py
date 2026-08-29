import os
from pathlib import Path
from datetime import datetime
from flask import current_app
from app.models import db, Category, Book
from app.utils import BookUtils

class CategoryScanner:

    @classmethod
    def _update_category_counts(cls):
        from app.models import Category, Book
        
        def count_books_recursive(cat):
            total = Book.query.filter_by(category_id=cat.id).count()
            for child in cat.children:
                total += count_books_recursive(child)
            return total
        
        all_cats = Category.query.all()
        for cat in all_cats:
            cat.book_count = count_books_recursive(cat)
        db.session.commit()

    SUPPORTED_EXTS = {
        '.epub', '.pdf', '.mobi', '.azw3', '.txt',
        '.doc', '.docx', '.rtf', '.odt',
        '.fb2', '.cbz', '.cbr'
    }
    BATCH_SIZE = 500

    @classmethod
    def scan_and_sync(cls, books_dir=None, force=False):
        if books_dir is None:
            books_dir = Path(current_app.config['BOOKS_DIR'])

        books_dir = Path(books_dir)
        if not books_dir.exists():
            return {'categories_added': 0, 'books_updated': 0, 'errors': []}

        result = {'categories_added': 0, 'books_updated': 0, 'books_skipped': 0, 'errors': []}

        existing_categories = {c.path: c for c in Category.query.all()}
        existing_books = {}
        for b in Book.query.all():
            key = f"{b.category_id}_{b.filename}"
            existing_books[key] = b

        cls._scan_directory_fast(
            books_dir, None, '',
            result, existing_categories, existing_books, force
        )

        cls._cleanup_orphans(books_dir, result)
        cls._update_category_counts()
        return result

    @classmethod
    def _scan_directory_fast(cls, path, parent_category, relative_path,
                             result, existing_categories, existing_books, force):
        path = Path(path)

        try:
            items = list(path.iterdir())
        except PermissionError:
            result['errors'].append(f"无法读取目录: {path}")
            return

        subdirs = []
        files = []

        for item in items:
            if item.is_dir() and not item.name.startswith('.'):
                subdirs.append(item)
            elif item.is_file() and item.suffix.lower() in cls.SUPPORTED_EXTS:
                files.append(item)

        if files or subdirs:
            category = cls._get_or_create_category_fast(
                path.name, parent_category, relative_path,
                result, existing_categories
            )

            if files:
                cls._sync_books_batch(
                    files, category, relative_path,
                    result, existing_books, force
                )

            for subdir in subdirs:
                new_relative = f"{relative_path}/{subdir.name}" if relative_path else subdir.name
                cls._scan_directory_fast(
                    subdir, category, new_relative,
                    result, existing_categories, existing_books, force
                )

    @classmethod
    def _get_or_create_category_fast(cls, name, parent, relative_path,
                                     result, existing_categories):
        full_path = f"{parent.get_full_path()}/{name}" if parent else name

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
                book = Book(
                    filename=filename,
                    title=Path(filename).stem,
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
            db.session.commit()
            category.book_count = Book.query.filter_by(category_id=category.id).count()
            db.session.commit()

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
                        book.author = metadata.get('author', '未知作者')
                        book.description = metadata.get('description', '')
                        if metadata.get('cover'):
                            BookUtils.save_cover(metadata['cover'], book.id)
                        book.metadata_parsed = True
            except Exception as e:
                pass
            db.session.commit()

    @classmethod
    def _cleanup_orphans(cls, books_dir, result):
        all_categories = Category.query.all()
        for category in all_categories:
            full_path = books_dir / category.path.replace('/', os.sep)
            if not full_path.exists():
                for book in category.books:
                    db.session.delete(book)
                db.session.delete(category)
                db.session.commit()

        orphan_books = Book.query.filter_by(category_id=None).all()
        for book in orphan_books:
            db.session.delete(book)
        if orphan_books:
            db.session.commit()
