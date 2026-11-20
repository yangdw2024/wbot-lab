"""Regression checks using synthetic data and an isolated SQLite database."""
import datetime as dt
import asyncio
import http.client
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TEST_BASE = Path(os.environ.get('WBOT_TEST_ROOT', str(ROOT / 'runtime' / 'test-suite'))).resolve()
os.environ['WBOT_DATA_DIR'] = str(TEST_BASE / 'isolated-data')
os.environ['WBOT_GROUP'] = '合成测试群'
os.environ['WBOT_WECHAT_DIR'] = str(TEST_BASE / 'synthetic-wechat')
os.environ['WBOT_AUTO_REPORTS'] = '1'
sys.path.insert(0, str(ROOT))
import core
import local_app
import auto_reports
import reports
import report_export
import wechat_ingest
import wal_guard
import wechat_snapshot
import wechat_crypto_guard as crypto_guard
import wechat_reader_worker
import wechat_source
import wechat_capture
from PIL import Image

TEST_TMP = ROOT / 'runtime' / 'tests'
TEST_TMP.mkdir(parents=True, exist_ok=True)


class CoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.db = core.connect(Path(self.tmp.name) / 'test.sqlite3')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_long_content_survives_dedupe_and_export_import(self):
        records = [{'time': '2026-01-01 12:00:00', 'sender': '合成用户',
                    'group': '测试群', 'content': 'X' * 30000 + suffix}
                   for suffix in ('A', 'B')]
        first = core.import_data(self.db, 'sample.json', json.dumps(records, ensure_ascii=False))
        self.assertEqual((first['added'], first['duplicates']), (2, 0))
        exported = core.export_messages(self.db, '测试群')
        self.assertEqual([len(m['content']) for m in exported['messages']], [30001, 30001])
        restored = core.connect(Path(self.tmp.name) / 'restored.sqlite3')
        try:
            result = core.import_data(restored, 'export.json', json.dumps(exported, ensure_ascii=False))
            self.assertEqual(result['added'], 2)
            self.assertEqual([m['content'] for m in core.export_messages(restored)['messages']],
                             [m['content'] for m in exported['messages']])
        finally:
            restored.close()

    def test_statistics_and_export_over_ten_thousand(self):
        base = dt.datetime(2026, 1, 2)
        rows = [('test-' + str(i), '测试群', '尾部成员' if i == 10000 else '前部成员',
                 (base + dt.timedelta(seconds=i)).strftime('%Y-%m-%d %H:%M:%S'),
                 '合成内容 ' + str(i), 'text', '2026-01-02', 'test') for i in range(10001)]
        with self.db:
            self.db.executemany('INSERT INTO messages(digest,group_name,sender,ts,content,kind,imported_at,source) VALUES (?,?,?,?,?,?,?,?)', rows)
        self.assertEqual(core.stats(self.db, '测试群')['total'], 10001)
        self.assertFalse(core.stats(self.db, '测试群')['truncated'])
        self.assertEqual(core.daily_report(self.db, '测试群', '2026-01-02')['total'], 10001)
        self.assertEqual(core.member_profile(self.db, '测试群', '尾部成员')['count'], 1)
        self.assertEqual(len(core.export_messages(self.db, '测试群')['messages']), 10001)

    def test_ocr_lines_are_separate_from_messages(self):
        saved = core.save_screen_capture(self.db, '测试群', '2026-11-20T08:30:00+08:00', ['GPT 讨论', 'GPT 讨论', '100% 测试'])
        self.assertEqual(saved['lines'], 3)
        self.assertEqual(core.stats(self.db)['total'], 0)
        summary = core.screen_summary(self.db, '测试群')
        self.assertEqual((summary['captures'], summary['lines']), (1, 3))
        self.assertEqual(summary['topics'][0]['count'], 2)
        self.assertIn('不等于消息数', summary['note'])
        self.assertEqual(core.screen_search(self.db, '测试群', '%')['total'], 1)
        self.assertEqual(len(core.screen_search(self.db, '测试群', '', 0, 2)['lines']), 2)
        backup = core.backup_database(self.db, Path(self.tmp.name) / 'backups')
        restored = core.sqlite3.connect(backup['path'])
        try:
            self.assertEqual(restored.execute('SELECT count(*) FROM screen_lines').fetchone()[0], 3)
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            restored.close()

    def test_timezones_and_whitespace_are_preserved(self):
        item = {'time':'2026-01-01T00:15:00+09:00','sender':'合成用户',
                'group':'测试群','content':'  第一行\n  第二行  '}
        core.import_data(self.db, 'time.json', json.dumps([item], ensure_ascii=False))
        row = core.export_messages(self.db)['messages'][0]
        self.assertEqual(row['ts'], '2025-12-31 23:15:00')
        self.assertEqual(row['original_timestamp'], item['time'])
        self.assertEqual(row['timestamp_basis'], 'offset_converted')
        self.assertEqual(row['content'], item['content'])

    def test_calendar_reports_and_snapshot_pagination(self):
        self.assertEqual(reports.period_bounds('week','2026-01-01'),('2025-12-29','2026-01-05'))
        self.assertEqual(reports.period_bounds('month','2024-02-20'),('2024-02-01','2024-03-01'))
        self.assertEqual(reports.period_bounds('year','2026-11-20'),('2026-01-01','2027-01-01'))
        records = [
            {'time':'2025-12-31 23:00','sender':'甲','group':'测试群','content':'GPT 讨论'},
            {'time':'2026-01-01 08:00','sender':'乙','group':'测试群','content':'Codex 代码'},
            {'time':'2026-01-02 09:00','sender':'甲','group':'测试群','content':'模型更新'},
            {'time':'2026-01-05 09:00','sender':'丙','group':'测试群','content':'下周记录'},
            {'time':'2026-01-01 08:00','sender':'丁','group':'其他群','content':'不可混入'},
        ]
        core.import_data(self.db,'records.json',json.dumps(records,ensure_ascii=False))
        week=reports.period_report(self.db,'测试群','week','2026-01-01')
        self.assertEqual(week['totals']['messages'],3)
        self.assertEqual(week['coverage']['available_messages'],4)
        self.assertEqual(week['coverage']['completeness'],'unknown')
        self.assertTrue(all(e['id'] for t in week['topics'] for e in t['examples']))
        page1=reports.messages_by_range(self.db,'测试群','2025-12-29','2026-01-05',limit=2)
        self.assertEqual(len(page1['messages']),2)
        self.assertIsNotNone(page1['next_cursor'])
        new_record={'time':'2025-12-30 10:00','sender':'后加入','group':'测试群','content':'追加'}
        core.import_data(self.db,'extra.json',json.dumps([new_record],ensure_ascii=False))
        page2=reports.messages_by_range(self.db,'测试群','2025-12-29','2026-01-05',limit=2,
                  cursor=page1['next_cursor'],snapshot_max_id=page1['snapshot_max_id'])
        self.assertEqual((page1['total'],page2['total'],len(page2['messages'])),(3,3,1))
        self.assertIsNone(page2['next_cursor'])
        self.assertEqual(len({m['id'] for m in page1['messages']+page2['messages']}),3)

    def test_batch_reports_are_separate_and_evidence_linked(self):
        records=[{'time':day+' 10:00','sender':'测试者','group':'测试群','content':'GPT 样例'}
                 for day in ('2025-12-31','2026-01-01','2026-01-07')]
        core.import_data(self.db,'records.json',json.dumps(records,ensure_ascii=False))
        result=report_export.export_all_reports(self.db,'测试群',Path(self.tmp.name)/'reports')
        self.assertEqual(result['counts'],{'day':3,'week':2,'month':2,'year':2})
        folder=Path(result['path'])
        self.assertTrue((folder/'完成标记.txt').is_file())
        annual=(folder/'04_年报'/'2026-01-01.md').read_text(encoding='utf-8')
        self.assertIn('2 条消息',annual)
        self.assertIn('消息 #',annual)
        self.assertIn('完整性未知',annual)
        self.assertFalse(self.db.in_transaction)

    def test_report_escapes_html_from_message_text(self):
        records=[{'time':'2026-01-01 10:00','sender':'测试者', 'group':'测试群',
                  'content':'GPT <script>alert(1)</script> & 其他'}]
        core.import_data(self.db,'synthetic.json',json.dumps(records,ensure_ascii=False))
        result=report_export.export_all_reports(self.db,'测试群',Path(self.tmp.name)/'reports')
        output=(Path(result['path'])/'04_年报'/'2026-01-01.md').read_text(encoding='utf-8')
        self.assertNotIn('<script>',output)
        self.assertIn('&lt;script&gt;',output)
        self.assertIn('&amp;',output)

    def test_wechat_rows_use_source_identity_and_preserve_undated(self):
        base={'shard':'message_0.db','local_id':1,'sort_seq':1,'type':'文本',
              'create_time':1767225600,'sender_username':'合成用户','content':'相同文本'}
        second=dict(base,local_id=2,sort_seq=2)
        other_shard=dict(base,shard='message_1.db')
        no_time=dict(base,local_id=3,sort_seq=3,create_time=0,type='图片',content='')
        rows=[base,second,other_shard,no_time]
        result=wechat_ingest.ingest_wechat_rows(self.db,'测试群','synthetic@chatroom',rows)
        self.assertEqual((result['added'],result['undated']),(3,1))
        self.assertEqual(core.stats(self.db,'测试群')['total'],3)
        self.assertEqual(reports.data_coverage(self.db,'测试群')['undated_messages'],1)
        self.assertEqual(wechat_ingest.ingest_wechat_rows(self.db,'测试群','synthetic@chatroom',rows)['duplicates'],4)
        changed=dict(base,content='发生变化')
        self.assertEqual(wechat_ingest.ingest_wechat_rows(self.db,'测试群','synthetic@chatroom',[changed])['conflicts'],1)
        self.assertEqual(core.export_messages(self.db,'测试群')['messages'][0]['ts'],'2026-01-01 08:00:00')
        with self.assertRaises(ValueError):
            wechat_ingest.ingest_wechat_rows(self.db,'测试群','synthetic@chatroom',
                [dict(base,local_id=99),{'local_id':100}])
        self.assertEqual(core.stats(self.db,'测试群')['total'],3)

    def test_media_metadata_is_counted_but_excluded_from_topics(self):
        base={'shard':'message_0.db','local_id':1,'sort_seq':1,'type':'文本',
              'create_time':1767225600,'sender_username':'synthetic','content':'GPT 正文'}
        media=dict(base,local_id=2,sort_seq=2,type='图片',content='<xml>GPT 图片元数据</xml>')
        wechat_ingest.ingest_wechat_rows(self.db,'测试群','synthetic@chatroom',[base,media])
        report=reports.period_report(self.db,'测试群','year','2026-01-01')
        self.assertEqual(report['totals']['messages'],2)
        self.assertEqual(report['totals']['text_messages'],1)
        self.assertEqual(report['totals']['nontext_metadata_messages'],1)
        self.assertEqual(report['coverage']['nontext_metadata_messages'],1)
        self.assertEqual(report['topics'][0]['count'],1)
        exported=core.export_messages(self.db,'测试群')
        other=core.connect(Path(self.tmp.name)/'media-restored.sqlite3')
        try:
            self.assertEqual(core.import_data(other,'export.json',json.dumps(exported,ensure_ascii=False))['added'],2)
            self.assertEqual(reports.data_coverage(other,'测试群')['nontext_metadata_messages'],1)
        finally:
            other.close()

    def test_verified_sender_prefix_preserves_original_and_deduplicates(self):
        base={'shard':'message_0.db','local_id':1,'sort_seq':1,'type':'文本',
              'create_time':1767225600,'sender_username':'synthetic-user',
              'content':'synthetic-user:\nGPT 正文'}
        unknown=dict(base,local_id=2,sort_seq=2,sender_username='different',
                     content='other-user:\n原文必须保留')
        media=dict(base,local_id=3,sort_seq=3,type='图片',content='synthetic-user:\n[图片]')
        rows=[base,unknown,media]
        self.assertEqual(wechat_ingest.ingest_wechat_rows(self.db,'测试群','synthetic@chatroom',rows)['added'],3)
        records=core.export_messages(self.db,'测试群')['messages']
        self.assertEqual(records[0]['content'],'GPT 正文')
        self.assertEqual(records[0]['source_content'],base['content'])
        self.assertEqual(records[1]['content'],unknown['content'])
        self.assertIsNone(records[1]['source_content'])
        self.assertEqual(records[2]['content_status'],'placeholder')
        self.assertEqual(wechat_ingest.ingest_wechat_rows(self.db,'测试群','synthetic@chatroom',rows)['duplicates'],3)
        restored=core.connect(Path(self.tmp.name)/'source-restored.sqlite3')
        try:
            core.import_data(restored,'export.json',json.dumps({'messages':records},ensure_ascii=False))
            self.assertEqual(core.export_messages(restored,'测试群')['messages'][0]['source_content'],base['content'])
        finally:
            restored.close()


class FakeOcr:
    def __init__(self, header):
        self.header = header
        self.calls = 0

    def predict(self, pixels):
        self.calls += 1
        if self.calls == 1:
            return [{'rec_texts': [self.header], 'rec_scores': [0.97],
                     'rec_polys': [[[400, 15], [750, 15], [750, 40], [400, 40]]]}]
        return [{'rec_texts': ['测试模型', '另一行'], 'rec_scores': [0.95, 0.55],
                 'rec_polys': [[[10, 20], [110, 20], [110, 40], [10, 40]],
                               [[10, 70], [110, 70], [110, 90], [10, 90]]]}]


class VisionLogicTests(unittest.TestCase):
    def test_target_title_required_and_ocr_stays_a_preview(self):
        image = Image.new('RGB', (1200, 800), 'white')
        result = wechat_capture.extract_visible_snippets(image, '合成测试群',
                    engine=FakeOcr('合成测试群 (394)'), main_window=True)
        self.assertEqual([line['text'] for line in result['lines']], ['测试模型', '另一行'])
        self.assertIn('不是消息发送时间', result['note'])
        with self.assertRaises(wechat_capture.CaptureError):
            wechat_capture.extract_visible_snippets(image, '其他群聊',
                    engine=FakeOcr('合成测试群 (394)'), main_window=True)


class WalGuardTests(unittest.TestCase):
    def test_real_sqlite_wal_and_uncommitted_tail(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            dbpath=Path(tmp)/'waltest.sqlite3'
            db=sqlite3.connect(str(dbpath))
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA wal_autocheckpoint=0')
                db.execute('CREATE TABLE t(x TEXT)')
                db.execute("INSERT INTO t VALUES ('committed')")
                db.commit()
                data=Path(str(dbpath)+'-wal').read_bytes()
                shm=Path(str(dbpath)+'-shm').read_bytes()[:96]
                wal=Path(tmp)/'snapshot.wal'
                wal.write_bytes(data)
                result=wal_guard.inspect_wal(wal)
                self.assertGreater(result['committed_frames'],0)
                snapshot=wal_guard.verify_snapshot(wal,shm)
                self.assertEqual(snapshot['committed_frames'],result['committed_frames'])
                page_size=struct.unpack('>I',data[8:12])[0]
                last=32+(result['valid_frames']-1)*(24+page_size)
                previous=struct.unpack('>II',data[last+16:last+24])
                page=data[last+24:last+24+page_size]
                frame_prefix=struct.pack('>II',1,0)
                order='<' if struct.unpack('>I',data[:4])[0]==0x377f0682 else '>'
                checksum=wal_guard._checksum(page,wal_guard._checksum(frame_prefix,previous,order),order)
                tail=frame_prefix+data[16:24]+struct.pack('>II',*checksum)+page
                wal.write_bytes(data+tail)
                after=wal_guard.inspect_wal(wal)
                self.assertEqual(after['committed_frames'],result['committed_frames'])
                self.assertTrue(after['tail_ignored'])
                first=bytearray(data[:32+24+page_size])
                first[32+16] ^= 1
                wal.write_bytes(first)
                with self.assertRaises(wal_guard.WalError): wal_guard.inspect_wal(wal)
            finally:
                db.close()

    def test_snapshot_only_copies_selected_files(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            base=Path(tmp); account=base/'source'/'account_x'/'db_storage'
            (account/'contact').mkdir(parents=True); (account/'message').mkdir()
            contact=sqlite3.connect(str(account/'contact'/'contact.db'))
            msg=sqlite3.connect(str(account/'message'/'message_0.db'))
            try:
                for db in (contact,msg):
                    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
                    db.execute('CREATE TABLE t(x TEXT)');db.commit()
                    db.execute('PRAGMA wal_checkpoint(FULL)')
                    db.execute("INSERT INTO t VALUES ('synthetic')");db.commit()
                session=base/'session';session.mkdir()
                result=wechat_snapshot.snapshot_account(base/'source',session)
                root,account_name,states,count=result
                self.assertEqual((account_name,count),('account_0001',1))
                self.assertTrue((root/account_name/'db_storage'/'message'/'message_0.db').is_file())
                self.assertEqual(len(states),2)
                self.assertTrue(all(x['committed_frames']>0 for x in states.values()))
            finally:
                contact.close();msg.close()


class CryptoGuardTests(unittest.TestCase):
    def test_page_mac_and_committed_wal_application(self):
        key=b'k'*32; salt=b's'*16
        first=bytearray(b'\x11'*4096);first[:16]=salt
        mac=crypto_guard.mac_key(key,first)
        def page(number):
            raw=bytearray(b'\x11'*4096)
            if number==1: raw[:16]=salt
            start=16 if number==1 else 0
            raw[4032:]=hmac.new(mac,bytes(raw[start:4032])+struct.pack('<I',number),hashlib.sha512).digest()
            return bytes(raw)
        p1,p2=page(1),page(2)
        crypto_guard.verify_page(p1,1,mac)
        damaged=bytearray(p2);damaged[100]^=1
        with self.assertRaises(crypto_guard.CryptoError): crypto_guard.verify_page(bytes(damaged),2,mac)
        prefix=struct.pack('>IIII',0x377f0682,3007000,4096,0)+b'12345678'
        state=wal_guard._checksum(prefix,(0,0),'<')
        wal=prefix+struct.pack('>II',*state)
        for number,commit,content in ((2,0,p2),(1,2,p1)):
            head=struct.pack('>II',number,commit)
            state=wal_guard._checksum(content,wal_guard._checksum(head,state,'<'),'<')
            wal+=head+b'12345678'+struct.pack('>II',*state)+content
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            path=Path(tmp)/'test.wal';path.write_bytes(wal)
            target=Path(tmp)/'plain.db';target.write_bytes(b'Z'*(4096*3))
            info=wal_guard.inspect_wal(path,frame_limit=2)
            def fake_decrypt(_key,_page,num):
                return (b'SQLite format 3\x00'+b'A'*(4096-16)) if num==1 else b'B'*4096
            self.assertEqual(crypto_guard.apply_committed_wal(target,path,key,p1,info,fake_decrypt),2)
            self.assertEqual(target.stat().st_size,8192)
            self.assertEqual(target.read_bytes()[4096:4100],b'BBBB')
            encrypted=Path(tmp)/'encrypted.db';encrypted.write_bytes(p1+p2)
            decrypted=Path(tmp)/'new-folder'/'decrypted.db'
            crypto_guard.decrypt_database(encrypted,decrypted,key,fake_decrypt)
            self.assertEqual(decrypted.stat().st_size,8192)


class SourceBoundaryTests(unittest.TestCase):
    def test_restricted_reader_disables_key_persistence_and_account_switch(self):
        fake=types.SimpleNamespace(WeChatDB=object,_decrypt_page=lambda *a:None)
        restricted=wechat_reader_worker._restricted_db(fake,{})()
        self.assertEqual(restricted._stable_key_dirs(),[])
        self.assertIsNone(restricted._save_keys())
        self.assertFalse(restricted._try_other_accounts())
        self.assertFalse(restricted._select_account_by_keys(set()))
        self.assertIsNone(restricted.extract_master_key())

    def test_cleanup_refuses_paths_outside_the_owned_session(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            base=Path(tmp)/'owned';base.mkdir()
            session=base/'session_synthetic';session.mkdir();(session/'temp.txt').write_text('synthetic')
            outside=Path(tmp)/'outside';outside.mkdir()
            with self.assertRaises(wechat_reader_worker.ReadError):
                wechat_reader_worker._safe_cleanup(outside,base)
            self.assertTrue(outside.is_dir())
            wechat_reader_worker._safe_cleanup(session,base)
            self.assertFalse(session.exists())

    def test_sync_only_imports_authorized_group_after_verified_worker(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            dbpath=Path(tmp)/'target.sqlite3'
            row={'shard':'message_0.db','local_id':1,'sort_seq':1,'type':'文本',
                 'create_time':1767225600,'sender_username':'synthetic-user','content':'synthetic'}
            payload={'ok':True,'result':{'group':wechat_source.TARGET_GROUP,'group_id':'synthetic@chatroom',
                     'rows':[row],'source_rows':1,'source_shards':1,'matched_shards':1,'wal_committed_frames':0}}
            fake=types.SimpleNamespace(returncode=0,stdout=json.dumps(payload).encode('utf-8'),stderr=b'')
            with mock.patch.object(wechat_source.subprocess,'run',return_value=fake), mock.patch.object(wechat_source,'connect',lambda:core.connect(dbpath)):
                result=wechat_source.sync_target_group(wechat_source.TARGET_GROUP)
                self.assertEqual((result['added'],result['source_rows']),(1,1))
                with self.assertRaises(ValueError): wechat_source.sync_target_group('其他群')
                self.assertEqual(wechat_source.sync_target_group(wechat_source.TARGET_GROUP)['duplicates'],1)


class AutoReportTests(unittest.TestCase):
    def test_auto_updates_require_explicit_opt_in_and_group_binding(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            state=Path(tmp)/'state.json'
            with mock.patch.dict(os.environ, {'WBOT_AUTO_REPORTS':'0'}):
                scheduler=auto_reports.AutoReports(state)
                self.assertFalse(scheduler.status()['enabled'])
                scheduler.set_enabled(True)
            with mock.patch.object(auto_reports,'TARGET_GROUP','另一个合成群'):
                reopened=auto_reports.AutoReports(state)
                self.assertFalse(reopened.status()['enabled'])
                self.assertIn('另一个群',reopened.status()['load_error'])

    def test_daily_catchup_persistence_retry_and_toggle(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            state=Path(tmp)/'state.json'
            current=[dt.datetime(2026,11,20,0,9,tzinfo=auto_reports.TZ)]
            calls=[]
            def sync(group):
                calls.append(group)
                if len(calls)==2: raise RuntimeError('synthetic offline')
                return {'source_rows':2,'added':1,'duplicates':1,'conflicts':0,'coverage':{'available_messages':2}}
            class DummyDB:
                def close(self): pass
            def export(db,group):
                return {'path':str(Path(tmp)/'reports'),'counts':{'day':1,'week':1,'month':1,'year':1}}
            args={'sync_fn':sync,'connect_fn':DummyDB,'export_fn':export,'clock':lambda:current[0]}
            scheduler=auto_reports.AutoReports(state,**args)
            self.assertFalse(scheduler.run_if_due())
            current[0]=current[0].replace(minute=10)
            self.assertTrue(scheduler.run_if_due())
            self.assertEqual(scheduler.status()['last_success_date'],'2026-11-20')
            self.assertFalse(scheduler.run_if_due())
            scheduler=auto_reports.AutoReports(state,**args)
            self.assertFalse(scheduler.run_if_due())
            current[0]+=dt.timedelta(days=1)
            self.assertTrue(scheduler.run_if_due())
            self.assertEqual(scheduler.status()['last_success_date'],'2026-11-20')
            self.assertIn('synthetic offline',scheduler.status()['last_error'])
            current[0]+=dt.timedelta(minutes=29)
            self.assertFalse(scheduler.run_if_due())
            current[0]+=dt.timedelta(minutes=1)
            self.assertTrue(scheduler.run_if_due())
            self.assertEqual(scheduler.status()['last_success_date'],'2026-11-21')
            scheduler.set_enabled(False)
            current[0]+=dt.timedelta(days=1)
            self.assertFalse(auto_reports.AutoReports(state,**args).run_if_due())
            scheduler.set_enabled(True)
            self.assertTrue(scheduler.run_if_due())
            self.assertEqual(len(calls),4)

    def test_corrupt_state_stops_without_overwriting(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            path=Path(tmp)/'state.json';path.write_text('broken',encoding='utf-8')
            scheduler=auto_reports.AutoReports(path)
            self.assertFalse(scheduler.status()['enabled'])
            self.assertFalse(scheduler.run_if_due())
            with self.assertRaises(ValueError): scheduler.set_enabled(True)
            self.assertEqual(path.read_text(encoding='utf-8'),'broken')


class HttpWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        cls.db_path = Path(cls.tmp.name) / 'web.sqlite3'
        cls.old_connect, cls.old_jobs, cls.old_auto = local_app.connect, local_app.JOBS, local_app.AUTO_REPORTS
        local_app.connect = lambda: core.connect(cls.db_path)
        local_app.JOBS = local_app.CaptureJobs()
        local_app.AUTO_REPORTS = auto_reports.AutoReports(Path(cls.tmp.name)/'auto_state.json')
        cls.server = local_app.ThreadingHTTPServer(('127.0.0.1', 0), local_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        local_app.connect, local_app.JOBS, local_app.AUTO_REPORTS = cls.old_connect, cls.old_jobs, cls.old_auto
        cls.tmp.cleanup()

    def request(self, method, path, data=None, headers=None):
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode('utf-8')
        headers = dict(headers or {})
        if body is not None:
            headers.setdefault('Content-Type', 'application/json')
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode('utf-8'))
        finally:
            conn.close()

    def test_secure_preview_review_save_flow(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('GET', '/app.js')
        static_response = conn.getresponse()
        self.assertEqual(static_response.status, 200)
        self.assertTrue(static_response.getheader('Content-Type').startswith('application/javascript'))
        static_response.read()
        conn.close()
        code, session = self.request('GET', '/api/session')
        self.assertEqual(code, 200)
        token = session['token']
        headers = {'X-WBOT-Token': token}
        self.assertTrue(self.request('GET','/api/auto/status')[1]['enabled'])
        self.assertEqual(self.request('POST','/api/auto/config',{'enabled':False},{})[0],403)
        self.assertFalse(self.request('POST','/api/auto/config',{'enabled':False},headers)[1]['enabled'])
        self.assertFalse(self.request('GET','/api/auto/status')[1]['enabled'])
        self.assertEqual(self.request('POST', '/api/demo', {}, {})[0], 403)
        self.assertEqual(self.request('POST', '/api/demo', {},
                         {'X-WBOT-Token': token, 'Origin': 'https://example.invalid'})[0], 403)
        self.assertEqual(self.request('POST', '/api/demo', {},
                         {'X-WBOT-Token': token, 'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('GET', '/api/groups', headers={'Host': 'example.invalid'})[0], 403)

        def fake_capture(group, on_phase):
            on_phase('recognizing')
            return {'group': group, 'captured_at': '2026-11-20T08:30:00+08:00',
                    'lines': [{'text': 'GPT 讨论', 'score': 0.9}], 'note': '合成测试'}

        with mock.patch.object(wechat_capture, 'capture_current_window', fake_capture):
            code, job = self.request('POST', '/api/wechat/start',
                                     {'group': '测试群', 'delay': 5}, headers)
            self.assertEqual(code, 200)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                _, status = self.request('GET', '/api/wechat/status?id=' + job['id'])
                if status['status'] in ('ready', 'error'):
                    break
                time.sleep(0.1)
            self.assertEqual(status['status'], 'ready', status)
            self.assertEqual(self.request('GET', '/api/wechat/summary?group=%E6%B5%8B%E8%AF%95%E7%BE%A4')[1]['lines'], 0)
            code, result = self.request('POST', '/api/wechat/save',
                                        {'id': job['id'], 'lines': ['GPT 讨论', '人工核对内容']}, headers)
            self.assertEqual((code, result['lines']), (200, 2))
            self.assertEqual(self.request('POST', '/api/wechat/save',
                             {'id': job['id'], 'lines': ['重复保存']}, headers)[0], 400)
            self.assertEqual(self.request('GET', '/api/wechat/summary?group=%E6%B5%8B%E8%AF%95%E7%BE%A4')[1]['lines'], 2)
            self.assertEqual(self.request('GET', '/api/wechat/search?group=%E6%B5%8B%E8%AF%95%E7%BE%A4&keyword=GPT')[1]['total'], 1)
            with mock.patch.object(local_app, 'backup_database',
                                   lambda db: core.backup_database(db, Path(self.tmp.name) / 'backups')):
                code, backup = self.request('POST', '/api/backup', {}, headers)
                self.assertEqual((code, backup['integrity']), (200, 'ok'))
                self.assertTrue(Path(backup['path']).is_file())
        self.assertEqual(self.request('POST', '/api/demo', {}, headers)[1]['added'], 426)
        self.assertEqual(self.request('POST', '/api/demo', {}, headers)[1]['added'], 0)
        fake_result={'group':wechat_source.TARGET_GROUP,'added':1,'duplicates':2,'conflicts':0,
                     'coverage':{'available_messages':3,'available_from':'2026-01-01','available_to':'2026-01-02'}}
        with mock.patch.object(local_app,'sync_target_group',return_value=fake_result):
            self.assertEqual(self.request('POST','/api/history/start',{'group':'其他群'},headers)[0],400)
            self.assertEqual(self.request('POST','/api/history/start',{'group':wechat_source.TARGET_GROUP}, {})[0],403)
            code, job=self.request('POST','/api/history/start',{'group':wechat_source.TARGET_GROUP},headers)
            self.assertEqual(code,200)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                _, status=self.request('GET','/api/history/status?id='+job['id'])
                if status['status'] in ('ready','error'): break
                time.sleep(0.05)
            self.assertEqual(status['status'],'ready',status)
            self.assertEqual(status['result']['duplicates'],2)
        with mock.patch.object(local_app, 'export_all_reports',
                               lambda db, group: report_export.export_all_reports(db,group,Path(self.tmp.name)/'reports')):
            code, job = self.request('POST','/api/reports/start',{'group':'DEMO · 科研工具交流群'},headers)
            self.assertEqual(code,200)
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                _, status=self.request('GET','/api/reports/status?id='+job['id'])
                if status['status'] in ('ready','error'): break
                time.sleep(0.1)
            self.assertEqual(status['status'],'ready',status)
            self.assertEqual(status['result']['counts']['day'],5)
            self.assertTrue((Path(status['result']['path'])/'完成标记.txt').is_file())

    def test_zz_shutdown_requires_session_token(self):
        self.assertEqual(self.request('POST','/api/shutdown',{}, {})[0],403)
        _, session=self.request('GET','/api/session')
        self.assertEqual(self.request('POST','/api/shutdown',{}, {'X-WBOT-Token':session['token']})[0],200)
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())


class McpProcessTests(unittest.TestCase):
    def test_invalid_frame_then_utf8_tools_list(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            for name in ('core.py', 'reports.py', 'mcp_server.py'):
                shutil.copy2(ROOT / name, Path(tmp) / name)
            env = os.environ.copy()
            env.pop('PYTHONIOENCODING', None)
            env.pop('PYTHONUTF8', None)
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            frames = b'not-json\n' + json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize'}).encode() + b'\n' + json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list'}).encode() + b'\n'
            proc = subprocess.run([sys.executable, '-B', str(Path(tmp) / 'mcp_server.py')],
                                  input=frames, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env, cwd=tmp, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors='replace'))
            responses = [json.loads(line) for line in proc.stdout.decode('utf-8').splitlines()]
            self.assertEqual(responses[0]['error']['code'], -32700)
            self.assertEqual(len(responses[2]['result']['tools']), 8)

    def test_real_mcp_sdk_period_report(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            for name in ('core.py', 'reports.py', 'mcp_server.py'):
                shutil.copy2(ROOT / name, Path(tmp) / name)
            async def run_client():
                params=StdioServerParameters(command=sys.executable,args=['-B',str(Path(tmp)/'mcp_server.py')],
                    cwd=tmp,env={'TMP':tmp,'TEMP':tmp,'PYTHONDONTWRITEBYTECODE':'1',
                                 'WBOT_DATA_DIR':str(Path(tmp)/'isolated-data')})
                async with stdio_client(params) as (reader,writer):
                    async with ClientSession(reader,writer,read_timeout_seconds=15) as session:
                        await session.initialize()
                        listing=await session.list_tools()
                        self.assertEqual(len(listing.tools),8)
                        response=await session.call_tool('get_period_report',{'group':'测试群','period':'year','anchor':'2026-11-20'})
                        self.assertFalse(response.is_error)
                        result=json.loads(response.content[0].text)
                        self.assertEqual((result['period'],result['totals']['messages']),('year',0))
                        self.assertEqual(result['coverage']['completeness'],'unknown')
            asyncio.run(run_client())


if __name__ == '__main__':
    unittest.main(verbosity=2)
