"""Render compact knowledge documents without browser or model dependencies."""
from __future__ import annotations

START = '<!-- original-body:start -->\n'
END = '\n<!-- original-body:end -->'
COVERAGE = '仅保存页面可见文字正文；图片、视频、评论未转录。'


def extract_original(document: str) -> str:
    """Keep the original body's whitespace, including leading/trailing newlines."""
    _, start, remaining = document.partition(START)
    original, end, _ = remaining.rpartition(END)
    if not start or not end:
        raise ValueError('缺少原始正文边界标记')
    return original


# 【精简格式】来源信息只保留检索所需字段，详细采集记录仍在 source_records。
def render(record: dict, number: int) -> str:
    """Preserve the existing collector's render(record, number) interface."""
    authors = record.get('authors') or []
    author = authors[0].get('name') if authors else None
    date = record.get('displayed_date') or '未读取到；不推定年份或出游日期'
    header = '\n'.join([
        f'# {record["city"]}｜{record["title"]}', '',
        f'- **城市**：{record["city"]}',
        f'- **作者**：{author or "未读取到"}',
        f'- **页面显示时间**：{date}',
        f'- **原帖链接**：{record["source_url"]}', '',
        '## 原始正文', '',
    ])
    # 不 strip、改写或重新分段正文；只替换正文之外的包装信息。
    return header + '\n' + START + record['body'] + END + '\n\n> ' + COVERAGE + '\n'
