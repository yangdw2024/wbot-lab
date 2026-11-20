"""Daily, local-only group sync and report generation while the app runs."""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core import DATA_ROOT, connect
from report_export import export_all_reports
from wechat_source import TARGET_GROUP, sync_target_group

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo('Asia/Singapore')
RUN_AT = time(0, 10)


class AutoReports:
    def __init__(self, state_path=None, sync_fn=None, connect_fn=None, export_fn=None, clock=None):
        self.path = Path(state_path) if state_path else DATA_ROOT / 'runtime' / 'auto_report_state.json'
        self.sync_fn = sync_fn or sync_target_group
        self.connect_fn = connect_fn or connect
        self.export_fn = export_fn or export_all_reports
        self.clock = clock or (lambda: datetime.now(TZ))
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self.running = False
        self.load_error = None
        self.state = {'version': 1, 'enabled': os.environ.get('WBOT_AUTO_REPORTS') == '1',
                      'group': TARGET_GROUP, 'last_success_date': None,
                      'last_attempt': None, 'last_success': None,
                      'last_error': None, 'retry_after': None}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(loaded, dict) or loaded.get('version') != 1 or not isinstance(loaded.get('enabled'), bool):
                    raise ValueError('状态文件格式无效')
                if loaded.get('group', '') != TARGET_GROUP:
                    raise ValueError('本数据目录的自动更新已绑定另一个群；请使用独立数据目录')
                for key in self.state:
                    if key in loaded:
                        self.state[key] = loaded[key]
                if self.state['last_success_date'] is not None:
                    date.fromisoformat(self.state['last_success_date'])
                if self.state['retry_after'] is not None:
                    parsed = datetime.fromisoformat(self.state['retry_after'])
                    if parsed.utcoffset() is None:
                        raise ValueError('重试时间缺少时区')
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self.load_error = '自动更新状态无法读取，请检查 E 盘状态文件：' + str(exc)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + '.' + secrets.token_hex(5) + '.tmp')
        with temp.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(self.state, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temp, self.path)

    def status(self):
        with self.lock:
            return {**self.state, 'enabled': self.state['enabled'] and not self.load_error,
                    'running': self.running, 'schedule': '00:10', 'timezone': 'Asia/Singapore',
                    'group': TARGET_GROUP, 'load_error': self.load_error,
                    'note': '程序运行且微信登录时每日更新；关闭或休眠后重新运行会补做当天任务。'}

    def set_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('enabled 必须是布尔值')
        with self.lock:
            if self.load_error:
                raise ValueError(self.load_error)
            if enabled and (not TARGET_GROUP or not os.environ.get('WBOT_WECHAT_DIR', '').strip()):
                raise ValueError('请启动时指定 --group 和 --wechat-dir 后再开启自动更新')
            self.state['enabled'] = enabled
            self._save()
        self.wake_event.set()
        return self.status()

    def run_if_due(self):
        now = self.clock()
        if now.utcoffset() is None:
            raise ValueError('调度时间必须包含时区')
        now = now.astimezone(TZ)
        today = now.date().isoformat()
        with self.lock:
            last = self.state['last_success_date']
            retry = self.state['retry_after']
            if (self.load_error or not TARGET_GROUP or not os.environ.get('WBOT_WECHAT_DIR', '').strip() or
                    not self.state['enabled'] or self.running or
                    now.time().replace(tzinfo=None) < RUN_AT or (last and last >= today) or
                    (retry and now < datetime.fromisoformat(retry))):
                return False
            self.running = True
            self.state['last_attempt'] = now.isoformat(timespec='seconds')
            try:
                self._save()
            except Exception:
                self.running = False
                raise
        try:
            result = self.sync_fn(TARGET_GROUP)
            if result.get('conflicts', 0):
                raise RuntimeError('同步发现内容冲突，已保留现有数据，请人工检查后重试')
            db = self.connect_fn()
            try:
                report = self.export_fn(db, TARGET_GROUP)
            finally:
                db.close()
            with self.lock:
                previous = dict(self.state)
                self.state['last_success_date'] = today
                self.state['last_success'] = {
                    'finished_at': self.clock().astimezone(TZ).isoformat(timespec='seconds'),
                    'source_rows': result['source_rows'], 'added': result['added'],
                    'duplicates': result['duplicates'], 'coverage': result['coverage'],
                    'report_path': report['path'], 'report_counts': report['counts']}
                self.state['last_error'] = None
                self.state['retry_after'] = None
                try:
                    self._save()
                except Exception:
                    self.state = previous
                    raise
        except Exception as exc:
            with self.lock:
                self.state['last_error'] = str(exc)
                self.state['retry_after'] = (self.clock().astimezone(TZ) + timedelta(minutes=30)).isoformat(timespec='seconds')
                self._save()
        finally:
            with self.lock:
                self.running = False
        return True

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.run_if_due()
            except Exception as exc:
                with self.lock:
                    self.load_error = '自动更新调度失败：' + str(exc)
            self.wake_event.wait(30)
            self.wake_event.clear()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._loop, name='daily-group-reports', daemon=True)
            self.thread.start()

    def stop(self, timeout=2):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=timeout)
