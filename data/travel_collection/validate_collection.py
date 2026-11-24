"""Verify actual saved notes and rebuild the collection plan, without any API calls."""
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

# 【修改】只校验简短来源信息与一份完整原文，不再要求八个冗余分类章节。
if __package__:
    from .note_format import extract_original, render
else:
    from note_format import extract_original, render

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    with (HERE / 'collection_plan.csv').open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        plan = list(reader)
    cities = list(dict.fromkeys(row['城市'] for row in plan))
    errors = []
    saved = []
    ids = set()
    slots = set()
    hashes = Counter()
    exclusions = {item['note_id'] for item in json.loads((HERE / 'excluded_notes.json').read_text(encoding='utf-8'))}
    for path in sorted((ROOT / 'knowledge').glob('*.md')):
        match = re.match(r'(.+?)_(\d\d)_([0-9a-f]{24})_', path.name)
        if not match:
            errors.append(f'{path.name}: 无法识别城市、序号或笔记ID')
            continue
        city, number, note_id = match.groups()
        number = int(number)
        try:
            record = json.loads((HERE / 'source_records' / (note_id + '.json')).read_text(encoding='utf-8'))
            document = path.read_text(encoding='utf-8')
            original = extract_original(document)
            digest = hashlib.sha256(original.encode('utf-8')).hexdigest()
            if original != record['body'] or digest != record['body_sha256']:
                errors.append(f'{path.name}: 原文或SHA-256不一致')
            if record['city'] != city or city not in cities or record['note_id'] != note_id:
                errors.append(f'{path.name}: 城市或笔记ID不一致')
            if number not in {1, 2, 3} or (city, number) in slots:
                errors.append(f'{path.name}: 城市内序号应为唯一的01、02、03')
            slots.add((city, number))
            if note_id in ids:
                errors.append(f'{path.name}: 同一笔记重复入库')
            if note_id in exclusions:
                errors.append(f'{path.name}: 被排除内容仍在正式知识库')
            if document != render(record, number):
                errors.append(f'{path.name}: 简短来源信息或精简文档格式与源记录不一致')
            ids.add(note_id)
            hashes[digest] += 1
            saved.append({'city': city, 'number': number, 'note_id': note_id, 'file': path.name,
                          'title': record['title'], 'url': record['source_url'],
                          'body_characters': len(original), 'document_characters': len(document),
                          'body_sha256': digest, 'body_matches_source': original == record['body'],
                          'media_complete': False})
        except (OSError, ValueError, KeyError, IndexError) as error:
            errors.append(f'{path.name}: {error}')
    counts = Counter(item['city'] for item in saved)
    for city, count in counts.items():
        if count > 3:
            errors.append(f'{city}: 超过3篇')
    duplicate_hashes = [digest for digest, count in hashes.items() if count > 1]
    if duplicate_hashes:
        errors.append('不同笔记出现相同正文：' + ','.join(duplicate_hashes))
    by_slot = {(item['city'], item['number']): item for item in saved}
    for row in plan:
        item = by_slot.get((row['城市'], int(row['序号'])))
        if item:
            row.update({'状态': '已保存页面正文；媒体待核验', '来源URL': item['url'], '文件名': item['file']})
        else:
            row.update({'状态': '待补齐', '来源URL': '', '文件名': ''})
    result = {'target': 150, 'saved': len(saved), 'complete_cities': sum(counts[city] == 3 for city in cities),
              'city_count': len(cities), 'missing': {city: 3-counts[city] for city in cities if counts[city] < 3},
              'body_preservation_checks_passed': not errors,
              'full_media_complete_notes': 0,
              'scope': '完整保存页面可见正文；图片OCR、视频、评论及整篇图文完整性未核验',
              'errors': errors, 'notes': saved}
    # 【保护】校验失败仅报告错误，保留上次通过校验的清单、状态和目录。
    if errors:
        print(json.dumps({key: value for key, value in result.items() if key != 'notes'}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    with (HERE / 'collection_plan.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(plan)
    (HERE / 'collection_status.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# 小红书旅游知识库采集目录', '',
             f'已保存页面正文 **{len(saved)}/150篇**，**{sum(counts[city] == 3 for city in cities)}/50城**各有3篇。', '',
             '每篇正文与源记录逐字核对并校验SHA-256。图片文字、视频和评论未转录，不能将正文完成数当作图文完整数。', '']
    for city in cities:
        lines.extend([f'## {city}（{counts[city]}/3）', ''])
        for item in sorted((item for item in saved if item['city'] == city), key=lambda item: item['number']):
            label = item['title'].replace('[', '［').replace(']', '］')
            lines.append(f'- [{label}](<../../knowledge/{item["file"]}>) · [原帖]({item["url"]})')
        if counts[city] < 3:
            lines.append(f'- 待补齐{3-counts[city]}篇。')
        lines.append('')
    (HERE / 'COLLECTION_INDEX.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'notes'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
