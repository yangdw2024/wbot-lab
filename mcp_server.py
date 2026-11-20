"""MCP tool server over standard JSON-RPC 2.0 stdio (newline-delimited). No dependencies."""
import json, sys
from core import connect, groups, stats, daily_report, member_profile, search
from reports import data_coverage, period_report, messages_by_range

TOOLS=[
 {'name':'list_groups','description':'列出本地已导入群及消息量','inputSchema':{'type':'object','properties':{}}},
 {'name':'get_group_stats','description':'查询群消息统计、成员排名、日期趋势和关键词话题','inputSchema':{'type':'object','properties':{'group':{'type':'string','description':'群名（不填为全部）'}}}},
 {'name':'get_daily_report','description':'获取指定日期的可追溯群聊日报','inputSchema':{'type':'object','properties':{'group':{'type':'string'},'day':{'type':'string','description':'YYYY-MM-DD'}},'required':['group','day']}},
 {'name':'get_member_profile','description':'查询指定成员发言统计与原文证据（不做心理推断）','inputSchema':{'type':'object','properties':{'group':{'type':'string'},'member':{'type':'string'}},'required':['group','member']}},
 {'name':'search_messages','description':'在本地数据库中按关键词、成员和日期检索聊天原文','inputSchema':{'type':'object','properties':{'group':{'type':'string'},'keyword':{'type':'string'},'member':{'type':'string'},'day':{'type':'string'},'limit':{'type':'integer','minimum':1,'maximum':200},'offset':{'type':'integer','minimum':0}}}},
 {'name':'get_data_coverage','description':'查看指定群已收录的时间范围；不代表完整历史','inputSchema':{'type':'object','properties':{'group':{'type':'string'}},'required':['group']}},
 {'name':'get_period_report','description':'按日、周、月或年统计已收录消息并返回消息 ID 引用；这是规则统计而非语义推断','inputSchema':{'type':'object','properties':{'group':{'type':'string'},'period':{'type':'string','enum':['day','week','month','year']},'anchor':{'type':'string','description':'周期内任一天，YYYY-MM-DD'}},'required':['group','period','anchor']}},
 {'name':'get_messages_by_range','description':'按日期范围分页读取消息原文；结束日期不包含在范围内，使用返回的 cursor 与 snapshot_max_id 继续读取','inputSchema':{'type':'object','properties':{'group':{'type':'string'},'start':{'type':'string'},'end':{'type':'string'},'limit':{'type':'integer','minimum':1,'maximum':200},'cursor':{'type':'object','properties':{'ts':{'type':'string'},'id':{'type':'integer'}},'required':['ts','id']},'snapshot_max_id':{'type':'integer','minimum':0}},'required':['group','start','end']}}
]

def run_tool(name,args):
    db=connect()
    try:
        if name=='list_groups':return groups(db)
        if name=='get_group_stats':return stats(db,args.get('group',''))
        if name=='get_daily_report':return daily_report(db,args.get('group',''),args.get('day',''))
        if name=='get_member_profile':return member_profile(db,args.get('group',''),args.get('member',''))
        if name=='search_messages':return search(db,args.get('group',''),args.get('keyword',''),args.get('member',''),args.get('day',''),min(int(args.get('limit',50)),200),args.get('offset',0))
        if name=='get_data_coverage':return data_coverage(db,args.get('group',''))
        if name=='get_period_report':return period_report(db,args.get('group',''),args.get('period',''),args.get('anchor',''))
        if name=='get_messages_by_range':return messages_by_range(db,args.get('group',''),args.get('start',''),args.get('end',''),limit=args.get('limit',100),cursor=args.get('cursor'),snapshot_max_id=args.get('snapshot_max_id'))
        raise ValueError('未知工具 '+name)
    finally:
        db.close()

def handle(req):
    method=req.get('method');params=req.get('params') or {}
    if method=='initialize':return {'protocolVersion':'2024-11-05','capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'wbot-lab-readonly','version':'0.1.0'}}
    if method=='ping':return {}
    if method=='tools/list':return {'tools':TOOLS}
    if method=='tools/call':
        try:
            result=run_tool(params.get('name'),params.get('arguments') or {})
            return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}
        except Exception as exc:return {'content':[{'type':'text','text':str(exc)}],'isError':True}
    if method and method.startswith('notifications/'):return None
    raise ValueError('不支持的方法 '+str(method))

def main():
    # stdout is reserved for MCP JSON-RPC frames.
    for stream in (sys.stdin,sys.stdout):
        if hasattr(stream,'reconfigure'): stream.reconfigure(encoding='utf-8')
    for line in sys.stdin:
        req=None
        try:
            req=json.loads(line)
            if not isinstance(req,dict): raise ValueError('请求必须是 JSON 对象')
            result=handle(req)
            if 'id' not in req or result is None:continue
            ans={'jsonrpc':'2.0','id':req['id'],'result':result}
        except json.JSONDecodeError:
            ans={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'JSON 解析失败'}}
        except Exception as exc:
            ans={'jsonrpc':'2.0','id':req.get('id') if isinstance(req,dict) else None,'error':{'code':-32603,'message':str(exc)}}
        sys.stdout.write(json.dumps(ans,ensure_ascii=False)+'\n');sys.stdout.flush()
if __name__=='__main__':main()
