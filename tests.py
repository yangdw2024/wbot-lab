import json, tempfile, unittest
from pathlib import Path
from core import connect,import_data,groups,stats,daily_report,member_profile,search,parse_records
from mcp_server import handle

class WbotTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'test.sqlite3')
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def test_import_dedupe_stats(self):
  data='time,sender,content,group\n2026-11-05 11:00,小陈,使用 GPT 与 Codex,演示\n2026-11-05 12:00,小苏,检查额度与订阅,演示\n'
  one=import_data(self.db,'x.csv',data);self.assertEqual(one['added'],2)
  two=import_data(self.db,'x.csv',data);self.assertEqual(two['added'],0)
  s=stats(self.db,'演示');self.assertEqual(s['total'],2);self.assertEqual(s['members'],2)
  self.assertEqual(len(daily_report(self.db,'演示','2026-11-05')['topics'])>=1,True)
  self.assertEqual(member_profile(self.db,'演示','小陈')['count'],1)
  self.assertEqual(len(search(self.db,'演示','GPT')['messages']),1)
  self.assertEqual(len(search(self.db,'演示','%')['messages']),0)
 def test_txt_multiline(self):
  data='[2026-11-06 11:32] 小李: 你好\n这是续行\n[2026-11-06 12:00] 小王: 好的'
  parsed,errs=parse_records('x.txt',data)
  self.assertEqual(len(parsed),2);self.assertIn('续行',parsed[0]['content']);self.assertFalse(errs)
 def test_json_and_errors(self):
  data=json.dumps([{'timestamp':'2026-11-06T11:15:20','author':'测试','message':'hi'}])
  out=import_data(self.db,'x.json',data);self.assertEqual(out['added'],1)
 def test_mcp_protocol(self):
  info=handle({'method':'initialize'});self.assertIn('serverInfo',info)
  tools=handle({'method':'tools/list'});self.assertEqual(len(tools['tools']),8)
 def test_bad_input(self):
  with self.assertRaises(ValueError):parse_records('data.exe','foo')

if __name__=='__main__':unittest.main(verbosity=2)
