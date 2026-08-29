import os
from pathlib import Path
import ebooklib
from ebooklib import epub

class BookUtils:
    SUPPORTED_FORMATS = [
        '.epub', '.pdf', '.mobi', '.azw3', '.txt',
        '.doc', '.docx', '.rtf', '.odt',
        '.fb2', '.cbz', '.cbr'
    ]

    @staticmethod
    def extract_epub_metadata(file_path):
        try:
            book = epub.read_epub(file_path)
            metadata = {'title': None, 'author': None, 'description': None, 'cover': None}

            if book.get_metadata('DC', 'title'):
                metadata['title'] = book.get_metadata('DC', 'title')[0][0]
            if book.get_metadata('DC', 'creator'):
                metadata['author'] = book.get_metadata('DC', 'creator')[0][0]
            if book.get_metadata('DC', 'description'):
                metadata['description'] = book.get_metadata('DC', 'description')[0][0]

            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_COVER:
                    metadata['cover'] = item.get_content()
                    break

            if not metadata['title']:
                metadata['title'] = Path(file_path).stem
            if not metadata['author']:
                metadata['author'] = '未知作者'

            return metadata
        except Exception as e:
            return {'title': Path(file_path).stem, 'author': '未知作者', 'description': '', 'cover': None}

    @staticmethod
    def get_file_type(filename):
        ext = Path(filename).suffix.lower()
        type_map = {
            '.epub': 'EPUB', '.pdf': 'PDF', '.mobi': 'MOBI', '.azw3': 'AZW3',
            '.txt': 'TXT', '.doc': 'DOC', '.docx': 'DOCX',
            '.rtf': 'RTF', '.odt': 'ODT', '.fb2': 'FB2', '.cbz': 'CBZ', '.cbr': 'CBR'
        }
        return type_map.get(ext, '未知')

    @staticmethod
    def get_file_icon(filename):
        return ''

    @staticmethod
    def format_file_size(size):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    @staticmethod
    def save_cover(cover_data, book_id):
        if not cover_data:
            return None
        cover_dir = Path('static/covers')
        cover_dir.mkdir(parents=True, exist_ok=True)
        cover_path = cover_dir / f"{book_id}.jpg"
        try:
            with open(cover_path, 'wb') as f:
                f.write(cover_data)
            return str(cover_path)
        except Exception as e:
            return None

    @staticmethod
    def extract_docx_to_html(file_path):
        return {'success': False, 'error': 'DOCX 支持需要安装 python-docx'}

    @staticmethod
    def extract_doc_text(file_path):
        return {'success': False, 'error': 'DOC 支持需要安装 antiword'}
