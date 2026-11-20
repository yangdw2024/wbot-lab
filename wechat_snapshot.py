"""Create a read-only, consistency-checked copy of one WeChat account's DB files."""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from wal_guard import verify_snapshot


class SnapshotError(RuntimeError):
    pass


def discover_account(root):
    root = Path(root).resolve(strict=True)
    candidates = []
    for account in root.iterdir():
        if not account.is_dir():
            continue
        storage = account / 'db_storage'
        contact = storage / 'contact' / 'contact.db'
        folder = storage / 'message'
        if not contact.is_file() or not folder.is_dir():
            continue
        shards = sorted(p for p in folder.iterdir() if p.is_file() and re.fullmatch(r'message_\d+\.db',p.name))
        if shards and any(p.stat().st_size > 4096 for p in shards):
            candidates.append((account, contact, shards))
    if len(candidates) != 1:
        raise SnapshotError('无法唯一确定有消息数据的微信账号目录；请核对本机账号')
    account, contact, shards = candidates[0]
    for path in [contact] + shards:
        if not path.resolve(strict=True).is_relative_to(account.resolve(strict=True)):
            raise SnapshotError('微信数据库路径超出所选账号目录')
    return account, contact, shards


def _stat(path):
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _source_state(databases):
    state = {}
    for path in databases:
        wal = Path(str(path) + '-wal')
        shm = Path(str(path) + '-shm')
        wal_stat = _stat(wal)
        shm_header = b''
        if wal_stat and wal_stat[0] > 0:
            if not shm.is_file():
                raise SnapshotError('微信数据库有 WAL 但缺少共享索引，无法验证快照')
            with shm.open('rb') as stream:
                shm_header = stream.read(96)
        state[str(path)] = {'db': _stat(path), 'wal': wal_stat, 'shm_header': shm_header}
    return state


def snapshot_account(source_root, session_dir, attempts=3):
    """Copy only contact + message shards; refuse unstable or unverifiable WALs."""
    account, contact, shards = discover_account(source_root)
    original = [contact] + shards
    session = Path(session_dir).resolve(strict=True)
    last_error = '微信数据库正在变化'
    for attempt in range(1, attempts + 1):
        destination_root = session / ('encrypted_snapshot_' + str(attempt))
        target_storage = destination_root / 'account_0001' / 'db_storage'
        try:
            before = _source_state(original)
            wal_states = {}
            for src in original:
                rel = src.relative_to(account / 'db_storage')
                dst = target_storage / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                wal_src = Path(str(src) + '-wal')
                wal_dst = Path(str(dst) + '-wal')
                if before[str(src)]['wal'] and before[str(src)]['wal'][0] > 0:
                    shutil.copyfile(wal_src, wal_dst)
                    wal_states[str(wal_dst)] = verify_snapshot(wal_dst, before[str(src)]['shm_header'])
            after = _source_state(original)
            current_shards = sorted(p for p in (account/'db_storage'/'message').iterdir()
                                    if p.is_file() and re.fullmatch(r'message_\d+\.db',p.name))
            if before != after or current_shards != shards:
                raise SnapshotError('复制期间微信数据库发生变化')
            return destination_root, 'account_0001', wal_states, len(shards)
        except (OSError, ValueError, SnapshotError) as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(0.15)
    raise SnapshotError('无法建立一致的微信数据快照：' + last_error)
