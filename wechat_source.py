"""Parent-side coordination for the explicitly authorized local group import."""
from __future__ import annotations

import json
import contextlib
import msvcrt
import os
import subprocess
import sys
from pathlib import Path

from core import DATA_ROOT, connect
from reports import data_coverage
from wechat_ingest import ingest_wechat_rows

ROOT = Path(__file__).resolve().parent
TARGET_GROUP = os.environ.get('WBOT_GROUP', '').strip()


@contextlib.contextmanager
def _sync_lock():
    """Keep two user-triggered readers from sharing the temporary workspace."""
    lock = DATA_ROOT / 'runtime' / 'wechat_sync.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('已有微信群同步正在运行，请等待结束') from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def sync_target_group(group):
    if not TARGET_GROUP:
        raise ValueError('请先通过 --group 指定有权读取的群名')
    if group != TARGET_GROUP:
        raise ValueError('只能读取启动时明确指定的群')
    if not os.environ.get('WBOT_WECHAT_DIR', '').strip():
        raise ValueError('请先通过 --wechat-dir 指定微信本地数据目录')
    with _sync_lock():
        return _sync_locked(group)


def _sync_locked(group):
    temp_root = DATA_ROOT/'runtime'/'wechat_tmp'
    temp_root.mkdir(parents=True,exist_ok=True)
    if not temp_root.resolve().is_relative_to(DATA_ROOT):
        raise RuntimeError('临时目录超出指定数据目录')
    if any(p.is_dir() and p.name.startswith('session_') for p in temp_root.iterdir()):
        raise RuntimeError('发现上次未清理的临时目录，请先检查 runtime/wechat_tmp')
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    try:
        process = subprocess.run([sys.executable,'-B',str(ROOT/'wechat_reader_worker.py')],
                                 input=json.dumps({'group':group},ensure_ascii=False).encode('utf-8'),
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=str(ROOT),env=env,
                                 timeout=300,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('微信历史读取超时；请检查 runtime/wechat_tmp 是否有未清理的临时副本') from exc
    try:
        payload=json.loads(process.stdout.decode('utf-8'))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:
        raise RuntimeError('读取进程未返回有效结果；请检查临时目录') from exc
    if process.returncode != 0 or not payload.get('ok'):
        raise RuntimeError(payload.get('error') or '微信历史读取失败')
    result=payload['result']
    if result.get('group') != group or not str(result.get('group_id','')).endswith('@chatroom'):
        raise RuntimeError('读取结果的群身份无法确认，未导入')
    rows=result.get('rows')
    if not isinstance(rows,list) or len(rows) != result.get('source_rows'):
        raise RuntimeError('读取结果数量无法核对，未导入')
    db=connect()
    try:
        counts=ingest_wechat_rows(db,group,result['group_id'],rows)
        coverage=data_coverage(db,group)
    finally:
        db.close()
    return {'group':group,'source_rows':result['source_rows'],'source_shards':result['source_shards'],
            'matched_shards':result['matched_shards'],'wal_committed_frames':result['wal_committed_frames'],
            **counts,'coverage':coverage,
            'note':'只导入本机快照中的目标群；未同步到本机的历史、媒体正文及无法解析的信息可能缺失。'}
