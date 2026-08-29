import chardet
import re

def detect_encoding(raw_bytes):
    result = chardet.detect(raw_bytes)
    encoding = result.get('encoding') if result else None
    if encoding and encoding.lower() in ['gb2312', 'gbk', 'gb18030', 'big5']:
        for enc in ['gbk', 'gb18030', 'big5', 'utf-8']:
            try:
                raw_bytes.decode(enc)
                return enc
            except:
                continue
    if encoding and encoding.lower() == 'utf-8':
        try:
            raw_bytes.decode('utf-8')
            return 'utf-8'
        except:
            pass
    common_encodings = ['utf-8', 'gbk', 'gb18030', 'big5', 'shift-jis', 'euc-kr', 'gb2312']
    for enc in common_encodings:
        try:
            raw_bytes.decode(enc)
            return enc
        except:
            continue
    return encoding or 'utf-8'

def fix_bom(raw_bytes):
    if raw_bytes.startswith(b'\xef\xbb\xbf'):
        return raw_bytes[3:]
    return raw_bytes
