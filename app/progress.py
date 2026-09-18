import sqlite3
from datetime import datetime

def save_reading_progress(user_id, book_id, progress, last_location=''):
    """保存阅读进度"""
    try:
        conn = sqlite3.connect('/app/instance/sweetreader.db')
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO reading_progress (user_id, book_id, progress, last_location, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, book_id) DO UPDATE SET
                progress = excluded.progress,
                last_location = excluded.last_location,
                updated_at = excluded.updated_at
        ''', (user_id, book_id, progress, last_location, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'保存进度失败: {e}')
        return False

def get_reading_progress(user_id, book_id):
    """获取阅读进度"""
    try:
        conn = sqlite3.connect('/app/instance/sweetreader.db')
        cur = conn.cursor()
        cur.execute('''
            SELECT progress, last_location, updated_at
            FROM reading_progress
            WHERE user_id = ? AND book_id = ?
        ''', (user_id, book_id))
        row = cur.fetchone()
        conn.close()
        if row:
            return {'progress': row[0], 'last_location': row[1], 'updated_at': row[2]}
        return None
    except Exception as e:
        print(f'获取进度失败: {e}')
        return None

def get_user_reading_list(user_id, limit=6):
    """获取用户继续阅读列表"""
    try:
        conn = sqlite3.connect('/app/instance/sweetreader.db')
        cur = conn.cursor()
        cur.execute('''
            SELECT b.id, b.title, b.author, b.file_path, b.file_type, rp.progress, rp.updated_at
            FROM reading_progress rp
            JOIN book b ON rp.book_id = b.id
            WHERE rp.user_id = ?
            ORDER BY rp.updated_at DESC
            LIMIT ?
        ''', (user_id, limit))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f'获取阅读列表失败: {e}')
        return []
