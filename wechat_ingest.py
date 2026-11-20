"""Normalize already-authorized WeChat rows; this module does not access WeChat."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core import REPORT_TIMEZONE


def _identity(group_id, row):
    if not isinstance(group_id, str) or not group_id.endswith('@chatroom'):
        raise ValueError('群会话标识无效，拒绝导入')
    shard, local_id, sequence = row.get('shard'), row.get('local_id'), row.get('sort_seq')
    if not isinstance(shard, str) or not shard or local_id is None or sequence is None:
        raise ValueError('来源缺少分片、消息编号或排序号，拒绝猜测去重')
    payload = json.dumps([group_id, shard, str(local_id), str(sequence), str(row.get('type',''))],
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _timestamp(value):
    if isinstance(value, bool):
        return None
    try:
        seconds = int(value)
        if seconds <= 0 or seconds > 253402300799:
            return None
        instant = datetime.fromtimestamp(seconds, tz=timezone.utc)
        local = instant.astimezone(ZoneInfo(REPORT_TIMEZONE))
        return (local.strftime('%Y-%m-%d %H:%M:%S'), instant.isoformat(timespec='seconds'))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def normalize_content(kind, value, username):
    """Remove a source sender prefix only when it exactly matches the sender."""
    source_content = None
    if isinstance(value, str) and username:
        prefix = str(username) + ':\n'
        if value.startswith(prefix):
            source_content, value = value, value[len(prefix):]
    if not isinstance(value, str) or not value.strip():
        return '[' + kind + '：无可用文本]', 'placeholder', source_content
    if value == '[' + kind + ']':
        return value, 'placeholder', source_content
    if kind in ('文本', 'text', '文字'):
        return value, 'provided_text', source_content
    if value.startswith('[') and value.endswith(']'):
        return value, 'placeholder', source_content
    return value, 'nontext_metadata', source_content


def ingest_wechat_rows(conn, group_name, group_id, rows):
    """Store a single group's rows by stable source identity, in one transaction.

    Rows without valid epoch seconds are retained separately, not assigned a
    fabricated date. The caller is responsible for verifying group identity,
    source coverage and any decrypted temporary storage.
    """
    if not isinstance(group_name, str) or not group_name.strip():
        raise ValueError('群名不能为空')
    added = duplicates = undated = conflicts = 0
    imported_at = datetime.now().astimezone().isoformat(timespec='seconds')
    with conn:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('来源记录格式错误，整批已回滚')
            identity = _identity(group_id, row)
            sender = row.get('sender_name') or row.get('sender_username') or '未知发送者'
            sender = str(sender).strip() or '未知发送者'
            username = row.get('sender_username')
            sender_identity = (hashlib.sha256(str(username).encode('utf-8')).hexdigest()
                               if username else None)
            kind = str(row.get('type') or '未知类型')
            value = row.get('content')
            content, content_status, source_content = normalize_content(kind, value, username)
            exists_dated = conn.execute('SELECT ts,content,kind FROM messages WHERE source_identity=?', (identity,)).fetchone()
            exists_undated = conn.execute('SELECT raw_timestamp,content,kind FROM undated_messages WHERE source_identity=?', (identity,)).fetchone()
            timestamp = _timestamp(row.get('create_time'))
            if exists_dated or exists_undated:
                same_dated = exists_dated and timestamp and exists_dated['ts']==timestamp[0] and exists_dated['content']==content and exists_dated['kind']==kind
                same_undated = exists_undated and not timestamp and exists_undated['raw_timestamp']==str(row.get('create_time','')) and exists_undated['content']==content and exists_undated['kind']==kind
                if same_dated or same_undated:
                    duplicates += 1
                else:
                    conflicts += 1
                continue
            if timestamp is None:
                conn.execute('INSERT INTO undated_messages(source_identity,group_name,sender,sender_identity,raw_timestamp,content,kind,content_status,source_content,reason) VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (identity,group_name.strip(),sender,sender_identity,str(row.get('create_time','')),content,kind,content_status,source_content,'时间缺失或无法可靠转换'))
                undated += 1
                continue
            ts, original = timestamp
            digest = hashlib.sha256(('wechat:' + identity).encode('ascii')).hexdigest()
            conn.execute('INSERT INTO messages(digest,group_name,sender,sender_identity,ts,content,kind,imported_at,source,original_timestamp,timestamp_basis,source_identity,content_status,source_content) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (digest,group_name.strip(),sender,sender_identity,ts,content,kind,imported_at,'wechat_local',original,'epoch_utc_converted',identity,content_status,source_content))
            added += 1
    return {'added': added, 'duplicates': duplicates, 'undated': undated, 'conflicts': conflicts}
