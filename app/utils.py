import os
import unicodedata
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
    def get_title_initial(title):
        """书名首字符归类键：A-Z / Num / Other。

        中文取拼音首字母（如《斗罗大陆》-> D），拉丁字母按字面，数字归 Num，
        其余（标点、日文假名、韩文等）归 Other。
        归类键在入库时算好存进 Book.initial，避免每次查询实时计算。
        """
        if not title:
            return 'Other'
        # NFKC 归一化：全角字母/数字（Ａ １）转半角，避免产生非法归类键
        try:
            text = unicodedata.normalize('NFKC', title).strip()
        except Exception:
            text = title.strip()
        if not text:
            return 'Other'

        # 大量中文小说文件名带符号前缀（《 【 [ ( 等），首字符常常是标点。
        # 直接取首字符会把 69% 的书全归到 Other，所以跳过前缀符号，
        # 在前若干个字符里找第一个"有意义"的字符（字母 / 数字 / 汉字）。
        for ch in text[:12]:
            if ('a' <= ch <= 'z') or ('A' <= ch <= 'Z'):
                return ch.upper()
            if ch.isdigit():
                return 'Num'
            if not ('\u4e00' <= ch <= '\u9fff'):
                continue          # 标点、符号、日文假名等，跳过
            try:
                from pypinyin import lazy_pinyin, Style
                res = lazy_pinyin(ch, style=Style.FIRST_LETTER)
                if res and res[0]:
                    letter = res[0][0].upper()
                    # 生僻字会被 pypinyin 原样返回，必须校验落在 A-Z
                    if len(letter) == 1 and 'A' <= letter <= 'Z':
                        return letter
            except Exception:
                pass
        return 'Other'

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
        # 相对路径会依赖进程工作目录（cron/脚本里八成写到别处去了），
        # 这里改用 Flask 的 static_folder 绝对路径
        from flask import current_app
        try:
            base = Path(current_app.static_folder or 'static')
        except Exception:
            base = Path('static')
        cover_dir = base / 'covers'
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
