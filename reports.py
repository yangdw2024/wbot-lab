"""Evidence-linked calendar reports over stored message records."""
from __future__ import annotations

from datetime import date, timedelta
from core import REPORT_TIMEZONE, topic_counts


def period_bounds(period, anchor):
    try:
        day = date.fromisoformat(str(anchor))
    except (TypeError, ValueError):
        raise ValueError('日期必须是 YYYY-MM-DD')
    if period == 'day':
        start, end = day, day + timedelta(days=1)
    elif period == 'week':
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=7)
    elif period == 'month':
        start = day.replace(day=1)
        end = date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)
    elif period == 'year':
        start, end = date(day.year, 1, 1), date(day.year + 1, 1, 1)
    else:
        raise ValueError('周期必须是 day、week、month 或 year')
    return start.isoformat(), end.isoformat()


def _group(group):
    if not isinstance(group, str) or not group.strip():
        raise ValueError('请指定群名，避免混合不同群聊')
    return group.strip()


def data_coverage(conn, group):
    group = _group(group)
    row = conn.execute("SELECT COUNT(*) AS total,MIN(ts) AS first,MAX(ts) AS last, "
                       "SUM(CASE WHEN content_status='provided_text' THEN 1 ELSE 0 END) AS text_count, "
                       "SUM(CASE WHEN content_status='nontext_metadata' THEN 1 ELSE 0 END) AS nontext_count "
                       "FROM messages WHERE group_name=?", (group,)).fetchone()
    undated = conn.execute('SELECT COUNT(*) FROM undated_messages WHERE group_name=?', (group,)).fetchone()[0]
    placeholders = conn.execute("SELECT COUNT(*) FROM messages WHERE group_name=? AND content_status='placeholder'", (group,)).fetchone()[0]
    return {'group': group, 'available_messages': row['total'],
            'available_from': row['first'], 'available_to': row['last'],
            'text_messages': row['text_count'] or 0, 'nontext_metadata_messages': row['nontext_count'] or 0,
            'undated_messages':undated,'placeholder_messages':placeholders,'completeness': 'unknown',
            'note': '仅表示本工具已收录的范围，不能证明覆盖该群全部历史；无有效时间的记录未纳入周期统计。'}


def period_report(conn, group, period, anchor):
    group = _group(group)
    start, end = period_bounds(period, anchor)
    params = (group, start + ' 00:00:00', end + ' 00:00:00')
    where = 'group_name=? AND ts>=? AND ts<?'
    sender_key = "CASE WHEN sender_identity IS NOT NULL AND sender_identity<>'' THEN 'id:'||sender_identity WHEN sender='未知发送者' THEN NULL ELSE 'name:'||sender END"
    totals = conn.execute('SELECT COUNT(*) AS total,COUNT(DISTINCT '+sender_key+') AS members,SUM(CASE WHEN ('+sender_key+') IS NULL THEN 1 ELSE 0 END) AS unknown,MIN(ts) AS first,MAX(ts) AS last FROM messages WHERE ' + where, params).fetchone()
    daily = [dict(row) for row in conn.execute('SELECT substr(ts,1,10) AS day,COUNT(*) AS count FROM messages WHERE '+where+' GROUP BY substr(ts,1,10) ORDER BY day',params)]
    ranking = [dict(row) for row in conn.execute('SELECT '+sender_key+' AS sender_key,MAX(sender) AS name,COUNT(*) AS count,COUNT(DISTINCT substr(ts,1,10)) AS active_days FROM messages WHERE '+where+' GROUP BY sender_key HAVING sender_key IS NOT NULL ORDER BY count DESC,name ASC LIMIT 20',params)]
    kinds = {row['kind']:row['n'] for row in conn.execute('SELECT kind,COUNT(*) AS n FROM messages WHERE '+where+' GROUP BY kind',params)}
    basis = {row['timestamp_basis']:row['n'] for row in conn.execute('SELECT timestamp_basis,COUNT(*) AS n FROM messages WHERE '+where+' GROUP BY timestamp_basis',params)}
    topics = topic_counts(conn.execute('SELECT id,ts,sender,content,content_status FROM messages WHERE '+where+' ORDER BY ts,id',params))
    contents = {row['content_status']: row['n'] for row in conn.execute(
        'SELECT content_status,COUNT(*) AS n FROM messages WHERE '+where+' GROUP BY content_status', params)}
    top = '、'.join(item['topic'] for item in topics[:3]) if topics else '没有命中预设关键词'
    summary = (f"在已收录的数据中，本周期有 {totals['total']} 条消息、"
               f"{totals['members']} 个可区分的发送者标识或显示名；主要关键词类别：{top}。")
    return {
        'group': group, 'period': period, 'anchor': str(anchor),
        'start': start, 'end_exclusive': end, 'timezone': REPORT_TIMEZONE,
        'coverage': data_coverage(conn, group),
        'totals': {'messages': totals['total'], 'sender_names': totals['members'],
                   'text_messages': contents.get('provided_text', 0),
                   'nontext_metadata_messages': contents.get('nontext_metadata', 0),
                   'placeholder_messages': contents.get('placeholder', 0),
                   'unknown_sender_messages':totals['unknown'] or 0,
                   'active_days': len(daily), 'first': totals['first'], 'last': totals['last']},
        'daily': daily, 'ranking': ranking, 'ranking_limited': totals['members'] > len(ranking),
        'kinds': kinds, 'timestamp_basis': basis, 'topics': topics, 'summary': summary,
        'caveats': [
        '统计基于已收录且具有有效时间的消息，完整性未知；不包含屏幕 OCR 文字。',
            '无时区输入按 Asia/Singapore 的本地时间处理；旧数据的时间依据可能未知。',
            '有来源标识时按标识区分，其他情况按显示名统计；身份无法识别的消息单独计数。话题为固定关键词匹配且可能多重命中。',
            '话题示例是截取片段，使用消息 ID 和时间回查完整原文。',
            '关键词只统计可用文字消息；图片、视频、表情等仅保留消息或结构化信息，媒体内容未解析。',
        ],
    }


def messages_by_range(conn, group, start, end, *, limit=100, cursor=None, snapshot_max_id=None):
    group = _group(group)
    try:
        first, last = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    except (TypeError, ValueError):
        raise ValueError('起止日期必须是 YYYY-MM-DD')
    if first >= last:
        raise ValueError('结束日期必须晚于开始日期；结束日期不包含在范围内')
    try:
        limit = min(max(int(limit), 1), 200)
    except (TypeError, ValueError):
        raise ValueError('limit 必须是整数')
    params = [group, first.isoformat() + ' 00:00:00', last.isoformat() + ' 00:00:00']
    where = 'group_name=? AND ts>=? AND ts<?'
    if snapshot_max_id is None:
        snapshot_max_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE '+where, params).fetchone()[0]
    try:
        snapshot_max_id = int(snapshot_max_id)
    except (TypeError, ValueError):
        raise ValueError('snapshot_max_id 必须是非负整数')
    if snapshot_max_id < 0:
        raise ValueError('snapshot_max_id 必须是非负整数')
    base_where = where + ' AND id<=?'
    base_params = params + [snapshot_max_id]
    total = conn.execute('SELECT COUNT(*) FROM messages WHERE '+base_where, base_params).fetchone()[0]
    if cursor is not None:
        if not isinstance(cursor, dict) or not isinstance(cursor.get('ts'), str):
            raise ValueError('cursor 必须包含 ts 和 id')
        try:
            cursor_id = int(cursor['id'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('cursor 必须包含 ts 和 id')
        base_where += ' AND (ts>? OR (ts=? AND id>?))'
        base_params += [cursor['ts'], cursor['ts'], cursor_id]
    rows = [dict(row) for row in conn.execute(
        'SELECT id,group_name,sender,sender_identity,ts,content,source_content,kind,source,original_timestamp,timestamp_basis,content_status FROM messages WHERE '+base_where+' ORDER BY ts,id LIMIT ?',
        base_params + [limit + 1])]
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = {'ts': rows[-1]['ts'], 'id': rows[-1]['id']} if has_more and rows else None
    return {'group': group, 'start': first.isoformat(), 'end_exclusive': last.isoformat(),
            'snapshot_max_id': snapshot_max_id, 'total': total, 'messages': rows,
            'next_cursor': next_cursor, 'coverage': data_coverage(conn, group)}
