"""Local chat analysis service. Project files and runtime data remain on D/E."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core import (
    connect, groups, stats, daily_report, member_profile, search, import_data,
    export_messages, save_screen_capture, screen_summary, screen_search, backup_database,
)
from reports import data_coverage, period_report
from report_export import export_all_reports
from wechat_source import sync_target_group, TARGET_GROUP
from auto_reports import AutoReports

ROOT = Path(__file__).resolve().parent
MAX_BODY = 15 * 1024 * 1024
SESSION_TOKEN = secrets.token_urlsafe(32)


class CaptureJobs:
    """One user-triggered, in-memory preview at a time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.job = None

    def start(self, group, delay):
        if not isinstance(group, str) or not group.strip() or len(group) > 120:
            raise ValueError('请填写完整群名')
        try:
            delay = int(delay)
        except (TypeError, ValueError):
            raise ValueError('倒计时必须是 5 到 30 秒')
        if not 5 <= delay <= 30:
            raise ValueError('倒计时必须是 5 到 30 秒')
        with self.lock:
            if self.job and self.job['status'] in ('waiting', 'capturing', 'recognizing', 'saving'):
                raise ValueError('已有采集任务正在运行，请等待结束')
            job = {'id': secrets.token_urlsafe(18), 'group': group.strip(),
                   'status': 'waiting', 'delay': delay}
            self.job = job
        threading.Thread(target=self._run, args=(job, delay), daemon=True).start()
        return {'id': job['id'], 'status': job['status'], 'delay': delay}

    def _phase(self, job, phase):
        with self.lock:
            if self.job is job:
                job['status'] = phase

    def _run(self, job, delay):
        try:
            time.sleep(delay)
            self._phase(job, 'capturing')
            from wechat_capture import capture_current_window
            result = capture_current_window(job['group'], lambda phase: self._phase(job, phase))
            with self.lock:
                if self.job is job:
                    job['result'] = result
                    job['status'] = 'ready'
        except Exception as exc:
            with self.lock:
                if self.job is job:
                    job['status'] = 'error'
                    job['error'] = str(exc)

    def get(self, job_id):
        with self.lock:
            if not self.job or not secrets.compare_digest(self.job['id'], str(job_id)):
                raise ValueError('采集任务不存在或已被新任务替换')
            return dict(self.job)

    def save(self, job_id, lines):
        with self.lock:
            if not self.job or not secrets.compare_digest(self.job['id'], str(job_id)):
                raise ValueError('采集任务不存在或已被新任务替换')
            if self.job['status'] != 'ready':
                raise ValueError('采集结果未就绪或已经保存')
            job = self.job
            job['status'] = 'saving'
        try:
            db = connect()
            try:
                saved = save_screen_capture(db, job['group'], job['result']['captured_at'], lines)
            finally:
                db.close()
        except Exception:
            self._phase(job, 'ready')
            raise
        self._phase(job, 'saved')
        return saved


JOBS = CaptureJobs()


class ReportJobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.job = None

    def start(self, group):
        if not isinstance(group, str) or not group.strip() or len(group) > 120:
            raise ValueError('请填写完整群名')
        with self.lock:
            if self.job and self.job['status'] == 'running':
                raise ValueError('已有报告生成任务正在运行')
            job = {'id': secrets.token_urlsafe(18), 'group': group.strip(), 'status': 'running'}
            self.job = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return {'id': job['id'], 'status': 'running'}

    def _run(self, job):
        db = None
        try:
            db = connect()
            result = export_all_reports(db, job['group'])
            with self.lock:
                if self.job is job:
                    job['result'] = result
                    job['status'] = 'ready'
        except Exception as exc:
            with self.lock:
                if self.job is job:
                    job['status'] = 'error'
                    job['error'] = str(exc)
        finally:
            if db is not None:
                db.close()

    def get(self, job_id):
        with self.lock:
            if not self.job or not secrets.compare_digest(self.job['id'], str(job_id)):
                raise ValueError('报告任务不存在或已被新任务替换')
            return dict(self.job)


REPORT_JOBS = ReportJobs()


class HistoryJobs:
    """Explicit user-triggered local history synchronization."""
    def __init__(self):
        self.lock = threading.Lock()
        self.job = None

    def start(self, group):
        if not TARGET_GROUP or group != TARGET_GROUP:
            raise ValueError('请先在启动时指定目标群，且只能读取该群')
        with self.lock:
            if self.job and self.job['status'] == 'running':
                raise ValueError('已有微信群同步正在运行')
            job = {'id': secrets.token_urlsafe(18), 'status': 'running'}
            self.job = job
        threading.Thread(target=self._run, args=(job, group), daemon=True).start()
        return {'id': job['id'], 'status': 'running'}

    def _run(self, job, group):
        try:
            result = sync_target_group(group)
            with self.lock:
                if self.job is job:
                    job['result'], job['status'] = result, 'ready'
        except Exception as exc:
            with self.lock:
                if self.job is job:
                    job['error'], job['status'] = str(exc), 'error'

    def get(self, job_id):
        with self.lock:
            if not self.job or not secrets.compare_digest(self.job['id'], str(job_id)):
                raise ValueError('同步任务不存在或已被新任务替换')
            return dict(self.job)


HISTORY_JOBS = HistoryJobs()
AUTO_REPORTS = AutoReports()


def open_isolated_browser(url):
    """Launch an existing browser with its profile and cache in this project."""
    from wechat_capture import RUNTIME
    candidates = [
        Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
        Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
    ]
    browser = next((path for path in candidates if path.is_file()), None)
    if browser is None:
        print('未找到已安装的 Chrome 或 Edge，请自行打开本机地址。')
        return
    profile = RUNTIME / 'browser_profile'
    cache = RUNTIME / 'browser_cache'
    profile.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    args = [str(browser), '--user-data-dir=' + str(profile), '--disk-cache-dir=' + str(cache),
            '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
            '--disable-component-update', '--disable-sync', '--disable-extensions',
            '--disable-crash-reporter', '--disable-breakpad', url]
    try:
        subprocess.Popen(args, cwd=str(ROOT), env=os.environ.copy())
    except OSError as exc:
        print('启动浏览器失败，请自行打开本机地址：', exc)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if '--quiet' not in sys.argv:
            # Do not log search strings or group names embedded in query strings.
            sys.stderr.write('%s %s\n' % (self.command, urlparse(self.path).path))

    def _security_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

    def respond(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._security_headers()
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _valid_host(self):
        value = self.headers.get('Host', '')
        try:
            parsed = urlparse('http://' + value)
            port = parsed.port
        except ValueError:
            return False
        return (parsed.hostname in ('127.0.0.1', 'localhost')
                and port == self.server.server_address[1]
                and not parsed.username and not parsed.password)

    def _allow_post(self):
        port = self.server.server_address[1]
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            self.respond({'error': '请求来源不允许'}, 403)
            return False
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            self.respond({'error': '请求必须使用 application/json'}, 415)
            return False
        token = self.headers.get('X-WBOT-Token', '')
        if not secrets.compare_digest(token, SESSION_TOKEN):
            self.respond({'error': '会话验证失败，请刷新页面'}, 403)
            return False
        return True

    def do_GET(self):
        if not self._valid_host():
            return self.respond({'error': '无效的本机地址'}, 403)
        p = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(p.query).items()}
        if p.path == '/api/session':
            return self.respond({'token': SESSION_TOKEN, 'group': TARGET_GROUP})
        if p.path == '/api/wechat/status':
            try:
                return self.respond(JOBS.get(q.get('id', '')))
            except Exception as exc:
                return self.respond({'error': str(exc)}, 400)
        if p.path == '/api/reports/status':
            try:
                return self.respond(REPORT_JOBS.get(q.get('id', '')))
            except Exception as exc:
                return self.respond({'error': str(exc)}, 400)
        if p.path == '/api/history/status':
            try:
                return self.respond(HISTORY_JOBS.get(q.get('id', '')))
            except Exception as exc:
                return self.respond({'error': str(exc)}, 400)
        if p.path == '/api/auto/status':
            return self.respond(AUTO_REPORTS.status())
        if p.path.startswith('/api/'):
            db = None
            try:
                db = connect()
                if p.path == '/api/groups':
                    result = groups(db)
                elif p.path == '/api/stats':
                    result = stats(db, q.get('group', ''))
                elif p.path == '/api/daily':
                    result = daily_report(db, q.get('group', ''), q.get('day', ''))
                elif p.path == '/api/member':
                    result = member_profile(db, q.get('group', ''), q.get('member', ''))
                elif p.path == '/api/search':
                    result = search(db, q.get('group', ''), q.get('keyword', ''),
                                    q.get('member', ''), q.get('day', ''),
                                    min(int(q.get('limit', '100')), 500), int(q.get('offset', '0')))
                elif p.path == '/api/export':
                    result = export_messages(db, q.get('group', ''), q.get('day', ''))
                elif p.path == '/api/wechat/summary':
                    result = screen_summary(db, q.get('group', ''))
                elif p.path == '/api/wechat/search':
                    result = screen_search(db, q.get('group', ''), q.get('keyword', ''),
                                           q.get('offset', '0'), q.get('limit', '100'))
                elif p.path == '/api/coverage':
                    result = data_coverage(db, q.get('group', ''))
                elif p.path == '/api/report':
                    result = period_report(db, q.get('group', ''), q.get('period', ''), q.get('anchor', ''))
                else:
                    return self.respond({'error': '接口不存在'}, 404)
                return self.respond(result)
            except Exception as exc:
                return self.respond({'error': str(exc)}, 400)
            finally:
                if db is not None:
                    db.close()
        static = {'/': ('index.html', 'text/html'), '/style.css': ('style.css', 'text/css'),
                  '/app.js': ('app.js', 'application/javascript')}
        item = static.get(p.path)
        if not item:
            return self.respond({'error': '页面不存在'}, 404)
        name, content_type = item
        data = (ROOT / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self._security_headers()
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self._valid_host():
            return self.respond({'error': '无效的本机地址'}, 403)
        if self.path not in ('/api/import', '/api/demo', '/api/wechat/start', '/api/wechat/save', '/api/backup', '/api/reports/start', '/api/history/start', '/api/auto/config', '/api/shutdown'):
            return self.respond({'error': '接口不存在'}, 404)
        if not self._allow_post():
            return
        db = None
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > MAX_BODY:
                return self.respond({'error': '请求大小超出限制，最大 15 MB'}, 413)
            obj = json.loads(self.rfile.read(length))
            if not isinstance(obj, dict):
                raise ValueError('请求必须是 JSON 对象')
            if self.path == '/api/wechat/start':
                return self.respond(JOBS.start(obj.get('group', ''), obj.get('delay', 10)))
            if self.path == '/api/wechat/save':
                return self.respond(JOBS.save(obj.get('id', ''), obj.get('lines')))
            if self.path == '/api/reports/start':
                return self.respond(REPORT_JOBS.start(obj.get('group', '')))
            if self.path == '/api/history/start':
                return self.respond(HISTORY_JOBS.start(obj.get('group', '')))
            if self.path == '/api/auto/config':
                return self.respond(AUTO_REPORTS.set_enabled(obj.get('enabled')))
            if self.path == '/api/shutdown':
                with HISTORY_JOBS.lock, REPORT_JOBS.lock, JOBS.lock:
                    if ((HISTORY_JOBS.job and HISTORY_JOBS.job['status'] == 'running') or
                            (REPORT_JOBS.job and REPORT_JOBS.job['status'] == 'running') or
                            (JOBS.job and JOBS.job['status'] in ('waiting','capturing','recognizing','saving'))):
                        raise ValueError('请等待正在进行的手动任务结束，再退出工具')
                self.respond({'message': '服务正在退出；进行中的自动任务会先完成。'})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == '/api/backup':
                db = connect()
                return self.respond(backup_database(db))
            if self.path == '/api/demo':
                filename = 'sample.csv'
                data = (ROOT / filename).read_text(encoding='utf-8')
            else:
                filename = str(obj.get('filename', ''))
                data = obj.get('data', '')
                if not isinstance(data, str) or len(data.encode('utf-8')) > MAX_BODY:
                    raise ValueError('数据必须是文本，最大 15 MB')
            db = connect()
            return self.respond(import_data(db, filename, data))
        except Exception as exc:
            return self.respond({'error': str(exc)}, 400)
        finally:
            if db is not None:
                db.close()


def main():
    from wechat_capture import prepare_runtime
    prepare_runtime()
    host = '127.0.0.1'
    try:
        port = int(os.environ.get('WBOT_PORT', '8765'))
    except ValueError:
        raise SystemExit('WBOT_PORT 必须是 1 至 65535 的整数')
    if not 1 <= port <= 65535:
        raise SystemExit('WBOT_PORT 必须是 1 至 65535 的整数')
    db = connect()
    db.close()
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print('所选端口可能被占用：', exc)
        sys.exit(1)
    print(f'\nW-BOT Lab 已启动：http://127.0.0.1:{port}')
    print('请在浏览器打开上面的本机地址；按 Ctrl+C 退出。\n')
    AUTO_REPORTS.start()
    if '--browser' in sys.argv:
        open_isolated_browser(f'http://{host}:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        AUTO_REPORTS.stop(timeout=310)
        server.server_close()


if __name__ == '__main__':
    main()
