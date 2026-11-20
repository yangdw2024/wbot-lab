"""Verify SQLCipher page authentication before decoding a local snapshot."""
from __future__ import annotations

import hashlib
import hmac
import struct
from pathlib import Path

from wal_guard import inspect_wal

PAGE_SIZE = 4096
RESERVED = 80


class CryptoError(ValueError):
    pass


def mac_key(key, first_page):
    if len(key) not in (32,48) or len(first_page) != PAGE_SIZE:
        raise CryptoError('数据库密钥或首页长度无效')
    salt = key[32:48] if len(key) == 48 else first_page[:16]
    return hashlib.pbkdf2_hmac('sha512',key[:32],bytes(v ^ 0x3a for v in salt),2,dklen=32)


def verify_page(page, page_number, mac):
    if len(page) != PAGE_SIZE or page_number <= 0:
        raise CryptoError('数据库页长度或页号无效')
    start = 16 if page_number == 1 else 0
    payload = page[start:PAGE_SIZE-RESERVED+16] + struct.pack('<I',page_number)
    expected = hmac.new(mac,payload,hashlib.sha512).digest()
    if not hmac.compare_digest(expected,page[PAGE_SIZE-64:]):
        raise CryptoError('数据库页完整性校验失败')


def decrypt_database(src, dst, key, decrypt_page):
    src, dst = Path(src), Path(dst)
    size = src.stat().st_size
    if size < PAGE_SIZE or size % PAGE_SIZE:
        raise CryptoError('数据库文件长度不是完整页')
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open('rb') as source, dst.open('wb') as target:
        first = source.read(PAGE_SIZE)
        mac = mac_key(key, first)
        source.seek(0)
        for number in range(1, size//PAGE_SIZE+1):
            page = source.read(PAGE_SIZE)
            verify_page(page,number,mac)
            plain = decrypt_page(key,page,number)
            if len(plain) != PAGE_SIZE:
                raise CryptoError('数据库页解密长度异常')
            if number == 1 and plain[:16] != b'SQLite format 3\x00':
                raise CryptoError('数据库首页解密后格式无效')
            target.write(plain)


def apply_committed_wal(dst, wal_path, key, first_page, state, decrypt_page):
    """Apply only verified committed frames and the committed database size."""
    count = int(state['committed_frames'])
    if count == 0:
        return 0
    verified = inspect_wal(wal_path, frame_limit=count)
    if verified['db_pages'] != state['db_pages'] or verified['frame_checksum'] != state['frame_checksum']:
        raise CryptoError('WAL 内容与快照校验结果不一致')
    mac = mac_key(key, first_page)
    with Path(wal_path).open('rb') as wal, Path(dst).open('r+b') as target:
        wal.seek(32)
        for _ in range(count):
            header = wal.read(24)
            page = wal.read(PAGE_SIZE)
            if len(header) != 24 or len(page) != PAGE_SIZE:
                raise CryptoError('WAL 帧不完整')
            page_number = struct.unpack('>I',header[:4])[0]
            verify_page(page,page_number,mac)
            plain = decrypt_page(key,page,page_number)
            if len(plain) != PAGE_SIZE or (page_number == 1 and plain[:16] != b'SQLite format 3\x00'):
                raise CryptoError('WAL 页面解密失败')
            target.seek((page_number-1)*PAGE_SIZE)
            target.write(plain)
        target.truncate(int(state['db_pages'])*PAGE_SIZE)
    return count
