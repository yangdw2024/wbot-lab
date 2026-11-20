"""W-BOT Lab: offline, read-only chat analytics core. Python 3.10+, stdlib only."""
from __future__ import annotations
import csv, hashlib, io, json, os, re, secrets, sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
_data_root = os.environ.get('WBOT_DATA_DIR', '').strip()
if not _data_root or not Path(_data_root).is_absolute():
    raise RuntimeError('请通过 --data-dir 或 WBOT_DATA_DIR 指定绝对路径作为数据目录')
DATA_ROOT = Path(_data_root).resolve()
DB_PATH = DATA_ROOT / 'data' / 'messages.sqlite3'
REPORT_TIMEZONE = 'Asia/Singapore'

TOPICS = {
    '模型与推理': ['GPT','ChatGPT','OpenAI','Claude','Gemini','Qwen','DeepSeek','模型','推理','Astra','Sonnet','Kimi','Grok','豆包','通义千问','大模型','上下文','提示词'],
    '额度与订阅': ['额度','订阅','套餐','会员','付费','价格','重置','token','限额','配额','续费','账号'],
    '编程与 Agent': ['Codex','Agent','MCP','Python','Git','代码','脚本','API','调试','仓库','部署','编程'],
    '科研与模拟': ['GROMACS','Q-Chem','Gaussian','分子','模拟','结晶','PM6','受体','论文','数据分析'],
    '视频与创作': ['视频','剪辑','图片','绘图','Blender','ComfyUI','生成','封面'],
    '系统与硬件': ['硬盘','显卡','内存','Windows','Linux','电脑','驱动','磁盘','网络','服务器'],
}

AI_TERMS = ['ChatGPT','GPT','Claude','Gemini','DeepSeek','Qwen','Kimi','Grok','豆包',
            'Codex','Agent','MCP','API','模型','额度','订阅','重置','价格','视频','图片']


def connect(db_path=DB_PATH):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      digest TEXT UNIQUE NOT NULL,
      group_name TEXT NOT NULL,
      sender TEXT NOT NULL,
      ts TEXT NOT NULL,
      content TEXT NOT NULL,
      kind TEXT NOT NULL DEFAULT 'text',
      imported_at TEXT NOT NULL,
      source TEXT NOT NULL,
      original_timestamp TEXT,
      timestamp_basis TEXT NOT NULL DEFAULT 'legacy_unknown',
      source_identity TEXT,
      sender_identity TEXT,
      content_status TEXT NOT NULL DEFAULT 'provided_text',
      source_content TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_message_group_ts ON messages(group_name,ts);
    CREATE INDEX IF NOT EXISTS idx_message_group_sender ON messages(group_name,sender);
    CREATE TABLE IF NOT EXISTS screen_captures (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      group_name TEXT NOT NULL,
      captured_at TEXT NOT NULL,
      saved_at TEXT NOT NULL,
      source TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS screen_lines (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      capture_id INTEGER NOT NULL REFERENCES screen_captures(id),
      line_number INTEGER NOT NULL,
      content TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_screen_captures_group ON screen_captures(group_name,id);
    CREATE INDEX IF NOT EXISTS idx_screen_lines_capture ON screen_lines(capture_id,line_number);
    CREATE TABLE IF NOT EXISTS undated_messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source_identity TEXT UNIQUE NOT NULL,
      group_name TEXT NOT NULL,
      sender TEXT NOT NULL,
      sender_identity TEXT,
      raw_timestamp TEXT,
      content TEXT NOT NULL,
      kind TEXT NOT NULL,
      content_status TEXT NOT NULL,
      source_content TEXT,
      reason TEXT NOT NULL
    );
    ''')
    columns={row[1] for row in conn.execute('PRAGMA table_info(messages)')}
    for name,definition in [('original_timestamp','TEXT'),
                            ('timestamp_basis',"TEXT NOT NULL DEFAULT 'legacy_unknown'"),
                            ('source_identity','TEXT'),
                            ('sender_identity','TEXT'),
                            ('content_status',"TEXT NOT NULL DEFAULT 'provided_text'"),
                            ('source_content','TEXT')]:
        if name not in columns:
            try:
                conn.execute(f'ALTER TABLE messages ADD COLUMN {name} {definition}')
            except sqlite3.OperationalError as exc:
                if 'duplicate column name' not in str(exc).lower(): raise
    undated_columns={row[1] for row in conn.execute('PRAGMA table_info(undated_messages)')}
    if 'sender_identity' not in undated_columns:
        try:
            conn.execute('ALTER TABLE undated_messages ADD COLUMN sender_identity TEXT')
        except sqlite3.OperationalError as exc:
            if 'duplicate column name' not in str(exc).lower(): raise
    if 'source_content' not in undated_columns:
        try:
            conn.execute('ALTER TABLE undated_messages ADD COLUMN source_content TEXT')
        except sqlite3.OperationalError as exc:
            if 'duplicate column name' not in str(exc).lower(): raise
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_message_source_identity ON messages(source_identity) WHERE source_identity IS NOT NULL')
    return conn


def normalize_timestamp_info(value):
    s = str(value or '').strip()
    if not s: raise ValueError('缺少时间字段')
    s = s.replace('年','-').replace('月','-').replace('日','').replace('/','-').replace('T',' ')
    s = re.sub(r'\s+', ' ', s.strip())
    for fmt in ('%Y-%m-%d %H:%M:%S','%Y-%m-%d %H:%M','%Y-%m-%d'):
        try:
            parsed=datetime.strptime(s,fmt)
            basis='date_only' if fmt=='%Y-%m-%d' else 'naive_assumed_local'
            return parsed.strftime('%Y-%m-%d %H:%M:%S'),basis
        except ValueError: pass
    try:
        parsed=datetime.fromisoformat(s)
        if parsed.utcoffset() is not None:
            parsed=parsed.astimezone(ZoneInfo(REPORT_TIMEZONE))
            return parsed.strftime('%Y-%m-%d %H:%M:%S'),'offset_converted'
        return parsed.strftime('%Y-%m-%d %H:%M:%S'),'naive_assumed_local'
    except ValueError: raise ValueError(f'无法识别时间：{str(value)[:80]}')


def normalize_timestamp(value):
    return normalize_timestamp_info(value)[0]


def parse_records(filename, data):
    ext = Path(filename).suffix.lower()
    pre_errors=[]
    if ext == '.csv':
        reader = csv.DictReader(io.StringIO(data.lstrip('\ufeff')))
        if not reader.fieldnames: raise ValueError('CSV 无表头')
        raw = list(reader)
    elif ext == '.json':
        loaded = json.loads(data)
        raw = loaded.get('messages',[]) if isinstance(loaded,dict) else loaded
        if not isinstance(raw,list): raise ValueError('JSON 应为消息数组或 {"messages": [...]}')
    elif ext == '.txt':
        # Supported: [2026-11-06 14:22] 张三: 消息
        pattern = re.compile(r'^\s*\[?(\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)\]?\s+(.{1,80}?)\s*[:：]\s*(.*)$')
        raw=[]
        for line_number,line in enumerate(data.splitlines(),1):
            match=pattern.match(line)
            if match: raw.append({'time':match.group(1),'sender':match.group(2),'content':match.group(3)})
            elif raw: raw[-1]['content'] += '\n'+line
            elif line.strip(): pre_errors.append(f'第 {line_number} 行：首条消息前的内容无法识别')
        if not raw: raise ValueError('TXT 格式不匹配，请使用：[2026-11-06 14:22] 张三: 消息内容')
    else: raise ValueError('只支持 CSV、JSON、TXT')
    def pick(row, keys, preserve=False):
        for k in keys:
            v=row.get(k)
            if v is not None and str(v).strip(): return str(v) if preserve else str(v).strip()
        return ''
    parsed=[]; errors=pre_errors
    for i,row in enumerate(raw,1):
        try:
            if not isinstance(row,dict): raise ValueError('不是对象')
            original_timestamp=pick(row,['original_timestamp','time','ts','timestamp','datetime','date','时间','发送时间'])
            ts,timestamp_basis=normalize_timestamp_info(original_timestamp)
            if row.get('timestamp_basis')=='legacy_unknown' and 'original_timestamp' in row and row['original_timestamp'] is None:
                original_timestamp=None
                timestamp_basis='legacy_unknown'
            sender = pick(row,['sender','author','member','nickname','from','发送人','昵称','成员'])
            content = pick(row,['content','text','message','body','内容','消息'],preserve=True)
            source_content = row.get('source_content')
            if source_content is not None and not isinstance(source_content, str): raise ValueError('来源正文格式无效')
            group = pick(row,['group','group_name','chat','room','群聊','群名']) or '默认群聊'
            kind = pick(row,['kind','type','消息类型']) or 'text'
            content_status = row.get('content_status','provided_text')
            if content_status not in ('provided_text','placeholder','nontext_metadata'): raise ValueError('内容状态无效')
            if not sender: raise ValueError('缺少成员')
            if not content: raise ValueError('缺少内容')
            if any(len(v)>1000 for v in [sender,group,kind]): raise ValueError('字段超长')
            parsed.append({'ts':ts,'original_timestamp':original_timestamp,'timestamp_basis':timestamp_basis,'content_status':content_status,
                           'sender':sender,'content':content,'source_content':source_content,'group_name':group,'kind':kind})
        except Exception as e:
            errors.append(f'第 {i} 行：{e}')
    return parsed,errors


def import_data(conn, filename, data):
    parsed,errors = parse_records(filename,data)
    if not parsed: raise ValueError('没有有效消息：'+'；'.join(errors[:3]))
    added=0; imported_at=datetime.now().isoformat(timespec='seconds')
    with conn:
        for rec in parsed:
            raw=json.dumps([rec['group_name'],rec['sender'],rec['ts'],rec['content'],rec['kind']],ensure_ascii=False)
            digest=hashlib.sha256(raw.encode('utf-8')).hexdigest()
            cur=conn.execute('INSERT OR IGNORE INTO messages(digest,group_name,sender,ts,content,kind,imported_at,source,original_timestamp,timestamp_basis,content_status,source_content) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (digest,rec['group_name'],rec['sender'],rec['ts'],rec['content'],rec['kind'],imported_at,filename[:255],rec['original_timestamp'],rec['timestamp_basis'],rec['content_status'],rec['source_content']))
            added += cur.rowcount
    return {'parsed':len(parsed),'added':added,'duplicates':len(parsed)-added,'invalid':len(errors),'errors':errors[:8]}


def groups(conn):
    return [dict(row) for row in conn.execute('SELECT group_name as name, COUNT(*) as count, COUNT(DISTINCT sender) as members, MIN(ts) as from_time, MAX(ts) as to_time FROM messages GROUP BY group_name ORDER BY count DESC')]


def _query(conn, group='', day='', member='', keyword='', limit=10000, offset=0):
    cond=[]; values=[]
    if group: cond.append('group_name=?');values.append(group)
    if day: cond.append('substr(ts,1,10)=?');values.append(day)
    if member: cond.append('sender=?');values.append(member)
    if keyword: cond.append('content LIKE ? ESCAPE "\\"');values.append('%'+keyword.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
    where='WHERE '+ ' AND '.join(cond) if cond else ''
    sql='SELECT id,group_name,sender,sender_identity,ts,content,source_content,kind,source,original_timestamp,timestamp_basis,content_status FROM messages '+where+' ORDER BY ts ASC,id ASC'
    if limit is not None:
        sql += ' LIMIT ? OFFSET ?'
        values += [min(max(int(limit),1),10000), max(int(offset),0)]
    return [dict(r) for r in conn.execute(sql,values)]


def topic_counts(messages):
    c=Counter(); examples=defaultdict(list)
    for msg in messages:
        try:
            status = msg['content_status']
        except (KeyError, IndexError):
            status = 'provided_text'  # e.g. manually reviewed screen OCR lines
        if status != 'provided_text':
            continue
        txt=msg['content'].lower()
        for topic,words in TOPICS.items():
            if any(w.lower() in txt for w in words):
                c[topic]+=1
                if len(examples[topic])<3: examples[topic].append({'id':msg['id'],'sender':msg['sender'],'ts':msg['ts'],'content':msg['content'][:180]})
    return [{'topic':k,'count':v,'examples':examples[k]} for k,v in c.most_common()]


def stats(conn, group=''):
    msgs=_query(conn,group,limit=None)
    total=len(msgs)
    members=Counter(x['sender'] for x in msgs)
    daily=Counter(x['ts'][:10] for x in msgs)
    kind=Counter(x['kind'] for x in msgs)
    output=[{'name':k,'count':v,'share':round(v/total*100,2) if total else 0,'days':len({m['ts'][:10] for m in msgs if m['sender']==k})} for k,v in members.most_common()]
    return {'total':total,'members':len(members),'from':msgs[0]['ts'] if msgs else None,'to':msgs[-1]['ts'] if msgs else None,
        'ranking':output,'daily':[{'day':k,'count':v} for k,v in sorted(daily.items())],
        'topics':topic_counts(msgs),'kinds':dict(kind),'truncated':False}


def daily_report(conn, group, day):
    msgs=_query(conn,group,day=day,limit=None)
    by_sender=Counter(m['sender'] for m in msgs)
    by_hour=Counter(m['ts'][11:13] for m in msgs)
    topics=topic_counts(msgs)
    return {'day':day,'group':group,'total':len(msgs),'members':len(by_sender),
        'top_members':[{'name':k,'count':v} for k,v in by_sender.most_common(10)],
        'hours':[{'hour':h,'count':by_hour.get(f'{h:02d}',0)} for h in range(24)],
        'topics':topics,'samples':[{'id':m['id'],'ts':m['ts'],'sender':m['sender'],'content':m['content'][:220]} for m in msgs[:10]],
        'method':'关键词规则计数（可重复命中）；不是大模型推断。原文引用保留消息 ID。'}


def member_profile(conn, group, member):
    all_msgs=_query(conn,group,limit=None)
    msgs=[m for m in all_msgs if m['sender']==member]
    c=Counter(m['ts'][:10] for m in msgs)
    return {'member':member,'group':group,'count':len(msgs),'share':round(100*len(msgs)/len(all_msgs),2) if all_msgs else 0,
        'active_days':len(c),'daily':[{'day':k,'count':v} for k,v in sorted(c.items())],
        'topics':topic_counts(msgs),'recent':[{'id':m['id'],'ts':m['ts'],'content':m['content'][:220]} for m in msgs[-8:]][::-1],
        'method':'仅描述可核验的群聊行为，不推断成员性格、心理或身份。'}


def search(conn,group='',keyword='',member='',day='',limit=100,offset=0):
    limit=min(max(int(limit),1),10000)
    offset=max(int(offset),0)
    return {'messages':_query(conn,group,day,member,keyword,limit,offset),'limit':limit,'offset':offset}


def export_messages(conn,group='',day=''):
    return {'schema_version':1,'messages':_query(conn,group,day=day,limit=None),
            'note':'包含所选有日期消息，不含无日期记录、来源身份和屏幕识别记录；单次导入请求上限为 15 MB。请使用 SQLite 备份保存完整数据。'}


def save_screen_capture(conn, group, captured_at, lines):
    """Store reviewed OCR snippets separately from actual imported messages."""
    if not isinstance(group,str) or not group.strip() or len(group)>1000:
        raise ValueError('群名无效')
    if not isinstance(captured_at,str) or not captured_at:
        raise ValueError('采集时间无效')
    if not isinstance(lines,list) or not lines or len(lines)>1000:
        raise ValueError('请保留 1 至 1000 条经过核对的识别文本')
    if any(not isinstance(line,str) or not line.strip() or len(line)>100000 for line in lines):
        raise ValueError('识别文本为空、格式错误或单条超过 100000 字符')
    saved_at=datetime.now().astimezone().isoformat(timespec='seconds')
    with conn:
        cur=conn.execute('INSERT INTO screen_captures(group_name,captured_at,saved_at,source) VALUES (?,?,?,?)',
                         (group.strip(),captured_at,saved_at,'微信可见画面 OCR，保存前人工核对'))
        capture_id=cur.lastrowid
        conn.executemany('INSERT INTO screen_lines(capture_id,line_number,content) VALUES (?,?,?)',
                         [(capture_id,i,line.strip()) for i,line in enumerate(lines,1)])
    return {'capture_id':capture_id,'lines':len(lines),'captured_at':captured_at}


def screen_summary(conn, group):
    rows=[dict(r) for r in conn.execute('''
        SELECT l.id,l.capture_id,l.line_number,l.content,c.captured_at
        FROM screen_lines l JOIN screen_captures c ON c.id=l.capture_id
        WHERE c.group_name=? ORDER BY l.id DESC''',(group,))]
    capture_count=conn.execute('SELECT COUNT(*) FROM screen_captures WHERE group_name=?',(group,)).fetchone()[0]
    as_messages=[{'id':r['id'],'sender':'未识别','ts':r['captured_at'],'content':r['content']} for r in rows]
    keyword_counts=Counter()
    for row in rows:
        content=row['content'].casefold()
        for term in AI_TERMS:
            if term.casefold() in content:
                keyword_counts[term]+=1
    return {'group':group,'captures':capture_count,'lines':len(rows),
            'topics':topic_counts(as_messages),'keywords':[{'term':k,'count':v} for k,v in keyword_counts.most_common()],
            'recent':rows[:100],
            'note':'统计单位是人工核对后的屏幕识别文本行，不等于消息数；同一画面重复采集会重复计数。'}


def screen_search(conn, group, keyword='', offset=0, limit=100):
    offset=max(int(offset),0)
    limit=min(max(int(limit),1),200)
    clauses=['c.group_name=?']
    values=[group]
    if keyword:
        clauses.append('l.content LIKE ? ESCAPE "\\"')
        values.append('%'+keyword.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
    where=' AND '.join(clauses)
    base=' FROM screen_lines l JOIN screen_captures c ON c.id=l.capture_id WHERE '+where
    total=conn.execute('SELECT COUNT(*)'+base,values).fetchone()[0]
    rows=[dict(r) for r in conn.execute(
        'SELECT l.id,l.capture_id,l.line_number,l.content,c.captured_at'+base+
        ' ORDER BY l.id DESC LIMIT ? OFFSET ?',values+[limit,offset])]
    return {'lines':rows,'total':total,'offset':offset,'limit':limit}


def backup_database(conn, destination=None):
    """Create a verified SQLite online backup without overwriting prior files."""
    folder=Path(destination) if destination is not None else DATA_ROOT / 'backups'
    folder.mkdir(parents=True,exist_ok=True)
    name='wbot_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+secrets.token_hex(3)+'.sqlite3'
    path=folder / name
    with path.open('xb'):
        pass
    copy=sqlite3.connect(str(path))
    try:
        conn.backup(copy)
        check=copy.execute('PRAGMA integrity_check').fetchone()[0]
        if check!='ok': raise ValueError('备份完整性检查未通过：'+str(check))
    finally:
        copy.close()
    return {'path':str(path),'integrity':'ok'}
