"""Validate SQLite WAL checksums and identify the last committed frame."""
from __future__ import annotations

import struct
import sys
from pathlib import Path


class WalError(ValueError):
    pass


def _checksum(data, state, byte_order):
    if len(data) % 8:
        raise WalError('WAL 校验数据长度无效')
    first, second = state
    words = struct.iter_unpack(byte_order + 'II', data)
    for left, right in words:
        first = (first + left + second) & 0xffffffff
        second = (second + right + first) & 0xffffffff
    return first, second


def inspect_wal(path, expected_page_size=4096, frame_limit=None):
    """Return a valid committed prefix; never interpret uncommitted tail frames."""
    path = Path(path)
    size = path.stat().st_size
    if size == 0:
        return {'committed_frames': 0, 'db_pages': None, 'prefix_bytes': 0,
                'valid_frames': 0, 'tail_ignored': False}
    if size < 32:
        raise WalError('WAL 文件头不完整')
    with path.open('rb') as f:
        header = f.read(32)
        magic, version, page_size = struct.unpack('>III', header[:12])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000:
            raise WalError('WAL 文件头格式无法验证')
        if page_size != expected_page_size:
            raise WalError('WAL 页大小与预期不一致')
        order = '<' if magic == 0x377f0682 else '>'
        state = _checksum(header[:24], (0, 0), order)
        if state != struct.unpack('>II', header[24:32]):
            raise WalError('WAL 文件头校验失败')
        salt = header[16:24]
        valid = committed = 0
        committed_size = None
        invalid_tail = False
        while frame_limit is None or valid < frame_limit:
            frame_header = f.read(24)
            if not frame_header:
                break
            page = f.read(page_size)
            if len(frame_header) != 24 or len(page) != page_size:
                invalid_tail = True
                break
            page_number, db_pages = struct.unpack('>II', frame_header[:8])
            if page_number == 0 or frame_header[8:16] != salt:
                invalid_tail = True
                break
            candidate = _checksum(frame_header[:8], state, order)
            candidate = _checksum(page, candidate, order)
            if candidate != struct.unpack('>II', frame_header[16:24]):
                invalid_tail = True
                break
            state = candidate
            valid += 1
            if db_pages:
                committed, committed_size = valid, db_pages
        if invalid_tail and valid == 0 and size > 32:
            raise WalError('WAL 首帧无法验证，不能判断已提交范围')
        if frame_limit is not None and (valid != frame_limit or committed != frame_limit):
            raise WalError('WAL 未覆盖共享索引记录的完整提交范围')
        return {'committed_frames': committed, 'db_pages': committed_size,
                'prefix_bytes': 32 + committed * (24 + page_size),
                'valid_frames': valid, 'tail_ignored': invalid_tail or valid > committed,
                'frame_checksum': state}


def inspect_shm_header(header, wal_header, expected_page_size=4096):
    """Validate both native-endian WAL-index headers and return committed mxFrame."""
    if len(header) < 96 or len(wal_header) < 32:
        raise WalError('WAL 共享索引或文件头不完整')
    first, second = header[:48], header[48:96]
    if first != second:
        raise WalError('WAL 共享索引正在变化')
    order = '<' if sys.byteorder == 'little' else '>'
    if _checksum(first[:40], (0,0), order) != struct.unpack(order+'II', first[40:48]):
        raise WalError('WAL 共享索引校验失败')
    version = struct.unpack(order+'I', first[:4])[0]
    page_size = struct.unpack(order+'H', first[14:16])[0]
    if version != 3007000 or first[12] != 1 or page_size != expected_page_size:
        raise WalError('WAL 共享索引格式无法验证')
    if first[32:40] != wal_header[16:24]:
        raise WalError('WAL 和共享索引的代次不一致')
    return {'mx_frame':struct.unpack(order+'I',first[16:20])[0],
            'db_pages':struct.unpack(order+'I',first[20:24])[0],
            'frame_checksum':struct.unpack(order+'II',first[24:32])}


def verify_snapshot(wal_path, shm_header):
    """Cross-check a copied WAL against the captured SQLite WAL-index header."""
    wal_path=Path(wal_path)
    if not wal_path.exists() or wal_path.stat().st_size == 0:
        if shm_header:
            raise WalError('存在共享索引但 WAL 文件缺失')
        return {'committed_frames':0,'db_pages':None,'prefix_bytes':0,'tail_ignored':False}
    with wal_path.open('rb') as f:
        wal_header=f.read(32)
    if not shm_header:
        raise WalError('无法确认 WAL 共享索引的提交范围')
    info=inspect_shm_header(shm_header,wal_header)
    if info['mx_frame']==0:
        return {'committed_frames':0,'db_pages':info['db_pages'],'prefix_bytes':0,'tail_ignored':True}
    result=inspect_wal(wal_path,frame_limit=info['mx_frame'])
    if result['db_pages'] != info['db_pages'] or result['frame_checksum'] != info['frame_checksum']:
        raise WalError('WAL 与共享索引的提交状态不一致')
    return result
