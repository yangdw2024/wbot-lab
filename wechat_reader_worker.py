"""Local, read-only WeChat snapshot worker. Output is consumed only by parent."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import types
from pathlib import Path

import wechat_crypto_guard as crypto_guard
from core import DATA_ROOT
from wechat_snapshot import snapshot_account
from wechat_snapshot import SnapshotError
from wal_guard import WalError

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor' / 'wechat_db_source' / 'db.py'
VENDOR_SHA256 = 'e67f961e006f8b1333bfdd54d0709d03060a0b8035852aba41d68b2c827970bd'
DEFAULT_SOURCE = os.environ.get('WBOT_WECHAT_DIR', '').strip()
STAGE = '初始化'


class ReadError(RuntimeError):
    pass


class QuietLogger:
    def __init__(self): self.warnings = 0
    def debug(self,*args,**kwargs): pass
    def info(self,*args,**kwargs): pass
    def warning(self,*args,**kwargs): self.warnings += 1
    def error(self,*args,**kwargs): self.warnings += 1
    def critical(self,*args,**kwargs): self.warnings += 1


def _offline_guard(event, args):
    if event.startswith('socket.'):
        raise PermissionError('历史读取工作进程不允许网络访问')


def _load_vendor():
    if not VENDOR.is_file() or hashlib.sha256(VENDOR.read_bytes()).hexdigest() != VENDOR_SHA256:
        raise ReadError('已审查的数据库读取模块缺失或校验不一致')
    package = types.ModuleType('wechatauto')
    package.__path__ = [str(VENDOR.parent)]
    logger = types.ModuleType('wechatauto.logger')
    logger.wxlog = QuietLogger()
    sys.modules['wechatauto'] = package
    sys.modules['wechatauto.logger'] = logger
    spec = importlib.util.spec_from_file_location('wechatauto.db', VENDOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules['wechatauto.db'] = module
    spec.loader.exec_module(module)
    return module, logger.wxlog


def _restricted_db(module, wal_states):
    class RestrictedDB(module.WeChatDB):
        def _save_keys(self):
            return None
        def _stable_key_dirs(self):
            return []
        def _try_other_accounts(self):
            return False
        def _select_account_by_keys(self, candidates):
            return False
        def _all_key_candidates(self):
            return set()
        def extract_master_key(self):
            return None
        def _auto_diagnose_key_failure(self, rel):
            return None
        def _decrypt_file(self, src, dst, key):
            return crypto_guard.decrypt_database(src,dst,key,module._decrypt_page)
        def _merge_wal(self, dst, wal_path, key, from_frame):
            if from_frame != 0:
                raise ReadError('增量 WAL 状态不适用于一次性快照')
            state=wal_states.get(str(Path(wal_path)))
            if state is None:
                raise ReadError('缺少 WAL 提交范围校验结果')
            with Path(str(wal_path)[:-4]).open('rb') as source:
                first_page=source.read(4096)
            return crypto_guard.apply_committed_wal(dst,wal_path,key,first_page,state,module._decrypt_page)
        def _open(self, rel):
            conn=super()._open(rel)
            src=self._db_path(rel)
            state=wal_states.get(str(Path(src+'-wal')))
            if state is not None:
                stamp=Path(self.workdir)/(rel.replace(os.sep,'__')+'.stamp')
                try:
                    applied=int(stamp.read_text(encoding='utf-8').split(',')[-1])
                except Exception:
                    conn.close()
                    raise ReadError('无法核验 WAL 合并状态')
                if applied != state['committed_frames']:
                    conn.close()
                    raise ReadError('WAL 合并不完整，已停止读取')
            return conn
    return RestrictedDB


def _read_group(db, group, logger):
    contact_rel = next((rel for rel,path,_ in db._db_files if Path(path).name == 'contact.db'),None)
    if not contact_rel:
        raise ReadError('快照中缺少联系人库')
    contact = db._open(contact_rel)
    try:
        matches=contact.execute("SELECT username,nick_name,remark FROM contact WHERE username LIKE '%@chatroom' AND (nick_name=? OR remark=?)",(group,group)).fetchall()
        if len(matches) != 1:
            raise ReadError('目标群名未能唯一匹配，请检查群名或账号')
        group_id=matches[0]['username']
        if not isinstance(group_id,str) or not group_id.endswith('@chatroom'):
            raise ReadError('目标群会话标识无效')
        tables=db._msg_conns(group_id)
        if not tables:
            raise ReadError('目标群在本机消息分片中没有可验证的记录')
        try:
            expected=0
            for conn,table in tables:
                if not re.fullmatch(r'Msg_[0-9a-fA-F]{32}',table):
                    raise ReadError('消息表名称无法验证')
                expected += conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            maps=db._shard_maps(tables)
            raw=db._shard_rows(tables,'',order_ext='ORDER BY sort_seq ASC,local_id ASC')
            if len(raw) != expected or logger.warnings:
                raise ReadError('消息分片读取不完整，已停止导入')
            rows=[db._msg_row_to_dict(row,maps) for row in raw]
        finally:
            for conn,_ in tables:
                conn.close()
        names={}
        for row in rows:
            username=row.get('sender_username')
            if not username:
                continue
            if username not in names:
                found=contact.execute('SELECT nick_name,remark FROM contact WHERE username=? LIMIT 1',(username,)).fetchone()
                label=(found['remark'] or found['nick_name']) if found else ''
                names[username]=label or ('未命名成员_'+hashlib.sha256(username.encode('utf-8')).hexdigest()[:8])
            row['sender_name']=names[username]
        return group_id,rows,expected,len(tables)
    finally:
        contact.close()


def _safe_cleanup(session, base):
    session=Path(session).resolve(strict=True)
    base=Path(base).resolve(strict=True)
    if session.parent != base or not session.name.startswith('session_'):
        raise ReadError('临时目录路径未通过安全校验')
    shutil.rmtree(session)


def run(group, source_root=DEFAULT_SOURCE):
    global STAGE
    if sys.platform != 'win32':
        raise ReadError('仅支持 Windows 微信')
    if not isinstance(group,str) or not group.strip() or len(group)>120:
        raise ReadError('目标群名无效')
    if not source_root or not Path(source_root).is_absolute():
        raise ReadError('请通过 --wechat-dir 指定微信数据的绝对路径')
    base=DATA_ROOT/'runtime'/'wechat_tmp'
    base.mkdir(parents=True,exist_ok=True)
    base=base.resolve(strict=True)
    if not base.is_relative_to(DATA_ROOT):
        raise ReadError('临时目录必须位于指定的数据目录内')
    session=Path(tempfile.mkdtemp(prefix='session_',dir=str(base))).resolve()
    result=None
    try:
        STAGE='临时环境'
        for name in ('tmp','appdata','localappdata','profile'):
            (session/name).mkdir()
        os.environ.update({'TMP':str(session/'tmp'),'TEMP':str(session/'tmp'),'TMPDIR':str(session/'tmp'),
                           'APPDATA':str(session/'appdata'),'LOCALAPPDATA':str(session/'localappdata'),
                           'USERPROFILE':str(session/'profile'),'WECHATAUTO_KEYS_DIR':str(session/'no_key_cache')})
        tempfile.tempdir=str(session/'tmp')
        STAGE='数据库快照'
        snapshot,account,wal_states,shard_count=snapshot_account(source_root,session)
        STAGE='加载读取模块'
        module,logger=_load_vendor()
        RestrictedDB=_restricted_db(module,wal_states)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            STAGE='读取并验证密钥'
            db=RestrictedDB(db_dir=str(snapshot),account=account,workdir=str(session/'decrypted'),
                            keys_file=str(session/'no_key_cache'/'keys.json'))
            if db.unkeyed:
                raise ReadError('部分数据库密钥无法验证；未读取或导入聊天记录')
            STAGE='匹配目标群并读取分片'
            group_id,rows,expected,matched=_read_group(db,group.strip(),logger)
        result={'group':group.strip(),'group_id':group_id,'rows':rows,'source_rows':expected,
                'source_shards':shard_count,'matched_shards':matched,
                'wal_committed_frames':sum(s['committed_frames'] for s in wal_states.values())}
    finally:
        try:
            _safe_cleanup(session,base)
        except Exception as exc:
            raise ReadError('临时副本清理失败，请检查 E 盘目录：'+str(session)) from exc
    return result


def main():
    sys.addaudithook(_offline_guard)
    try:
        request=json.loads(sys.stdin.read())
        result=run(request.get('group',''))
        sys.stdout.write(json.dumps({'ok':True,'result':result},ensure_ascii=False))
    except ReadError as exc:
        sys.stdout.write(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False))
        sys.exit(1)
    except (SnapshotError,WalError,crypto_guard.CryptoError) as exc:
        sys.stdout.write(json.dumps({'ok':False,'error':'快照或数据库校验失败：'+str(exc)},ensure_ascii=False))
        sys.exit(1)
    except Exception as exc:
        frames=traceback.extract_tb(exc.__traceback__)
        location=' > '.join(Path(f.filename).name+':'+str(f.lineno)+':'+f.name for f in frames[-8:]) if frames else '未知位置'
        sys.stdout.write(json.dumps({'ok':False,'error':'本地读取未完成（'+type(exc).__name__+'；阶段：'+STAGE+'；位置：'+location+'），未导入。'},ensure_ascii=False))
        sys.exit(1)


if __name__=='__main__':
    main()
