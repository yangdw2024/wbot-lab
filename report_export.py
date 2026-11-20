"""Generate a dated hierarchy of evidence-linked local Markdown reports."""
from __future__ import annotations

import hashlib
import html
import re
import secrets
from datetime import datetime
from pathlib import Path

from reports import data_coverage, period_bounds, period_report
from core import DATA_ROOT

ROOT = Path(__file__).resolve().parent


def _escape(value):
    text = html.escape(str(value).replace('\r', ' ').replace('\n', ' '), quote=False)
    return re.sub(r'([\\`*_{}\[\]()#+.!|>\-])', r'\\\1', text)


def _markdown(report):
    c, total = report['coverage'], report['totals']
    lines = [
        '# ' + _escape(report['group']) + ' · ' + _escape(report['period']) + ' 报告',
        '',
        f"- 周期：{report['start']} 至 {report['end_exclusive']}（不含结束日期）",
        '- 时区：' + report['timezone'],
        f"- 已收录范围：{c['available_from']} 至 {c['available_to']}；完整性未知。",
        '- ' + _escape(report['summary']),
        '', '## 统计', '',
        f"- 消息：{total['messages']} 条；发送者显示名：{total['sender_names']}；有记录日期：{total['active_days']} 天。",
        f"- 可用文字消息：{total['text_messages']} 条；非文字元数据：{total['nontext_metadata_messages']} 条；占位记录：{total['placeholder_messages']} 条。",
        '', '## 消息类型', '',
        *[f"- {_escape(kind)}：{count} 条" for kind, count in sorted(report['kinds'].items())],
        '', '## 关键词话题与证据', '',
    ]
    if not report['topics']:
        lines.append('没有命中预设关键词。')
    for topic in report['topics']:
        lines.append(f"### {_escape(topic['topic'])}（{topic['count']} 条）")
        lines.append('')
        for example in topic['examples']:
            lines.append(f"- 消息 #{example['id']} · {_escape(example['ts'])} · {_escape(example['sender'])}：{_escape(example['content'])}")
        lines.append('')
    lines += ['## 每日数量', '']
    lines.extend(f"- {item['day']}：{item['count']} 条" for item in report['daily'])
    lines += ['', '## 活跃显示名', '']
    lines.extend(f"- {_escape(item['name'])}：{item['count']} 条，{item['active_days']} 天" for item in report['ranking'])
    if report['ranking_limited']:
        lines.append('- 仅显示前 20 个显示名。')
    lines += ['', '## 口径与限制', '']
    lines.extend('- ' + _escape(item) for item in report['caveats'])
    lines.append('')
    return '\n'.join(lines)


def export_all_reports(conn, group, destination=None):
    """Write reports for active periods under a new directory, using one DB snapshot."""
    if conn.in_transaction:
        raise ValueError('数据库已有未提交事务，无法建立报告快照')
    conn.execute('BEGIN')
    folder = None
    try:
        coverage = data_coverage(conn, group)
        if not coverage['available_messages']:
            raise ValueError('该群没有已收录消息，暂时无法生成周期报告')
        days = [row[0] for row in conn.execute(
            'SELECT DISTINCT substr(ts,1,10) FROM messages WHERE group_name=? ORDER BY 1', (group.strip(),))]
        periods = {name: {} for name in ('day','week','month','year')}
        for day in days:
            for name in periods:
                start, end = period_bounds(name, day)
                periods[name][start] = (day, end)
        slug = re.sub(r'[^A-Za-z0-9._-]+', '_', group).strip('._-')[:36] or 'group'
        slug += '_' + hashlib.sha256(group.encode('utf-8')).hexdigest()[:8]
        parent = Path(destination) if destination is not None else DATA_ROOT / 'reports'
        folder = parent / slug / (datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+secrets.token_hex(3))
        folder.mkdir(parents=True, exist_ok=False)
        labels = {'day':'01_日报','week':'02_周报','month':'03_月报','year':'04_年报'}
        index = ['# 周期报告索引', '', '- 群：' + _escape(group),
                 f"- 已收录范围：{coverage['available_from']} 至 {coverage['available_to']}；完整性未知。",
                 '- 仅为有已收录消息的周期生成报告；没有文件不代表该周期没有讨论。',
                 '- 内容是可回查的规则统计，并非大模型语义总结。', '']
        counts = {}
        for kind, entries in periods.items():
            sub = folder / labels[kind]
            sub.mkdir()
            index += ['## ' + labels[kind], '']
            counts[kind] = 0
            for start, (anchor, _) in sorted(entries.items()):
                report = period_report(conn, group, kind, anchor)
                filename = start + '.md'
                with (sub / filename).open('x', encoding='utf-8', newline='\n') as output:
                    output.write(_markdown(report))
                index.append(f"- [{start}]({labels[kind]}/{filename}) · {report['totals']['messages']} 条")
                counts[kind] += 1
            index.append('')
        with (folder / 'README_报告索引.md').open('x', encoding='utf-8', newline='\n') as output:
            output.write('\n'.join(index))
        with (folder / '完成标记.txt').open('x', encoding='utf-8', newline='\n') as output:
            output.write('周期报告已生成；请检查数据覆盖范围与口径。\n')
        return {'path': str(folder), 'counts': counts, 'coverage': coverage}
    except Exception as exc:
        if folder is not None:
            raise RuntimeError('报告生成未完成，已保留目录供检查：' + str(folder) + '；' + str(exc)) from exc
        raise
    finally:
        conn.rollback()
