"""Collect user-authorized, publicly visible Xiaohongshu notes through normal Edge pages.

Uses the existing, separate Playwright QA environment; no model/embedding API.
No login profiles, private API, request signing, or access-control bypass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error, sync_playwright

# 【修改】采集器与离线校验共用精简格式，避免重新生成重复摘录。
if __package__:
    from .note_format import render
else:
    from note_format import render

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
AUTHORIZATION = "用户于2026-11-15在本次项目对话中确认：作者都同意了。依据用户确认处理，未另行向作者核验。"
ALIASES = {
    "北京": ["故宫", "什刹海", "颐和园", "天坛"],
    "天津": ["五大道", "津湾", "天津之眼"],
    "秦皇岛": ["北戴河", "阿那亚", "山海关", "渔田七里海"],
    "太原": ["晋祠", "钟楼街", "山西博物院"], "大同": ["云冈", "悬空寺", "华严寺"],
    "哈尔滨": ["中央大街", "冰雪大世界", "圣索菲亚"], "沈阳": ["沈阳故宫", "张学良旧居"],
    "大连": ["星海广场", "东港", "金石滩", "梭鱼湾"], "延吉": ["延边", "朝鲜族民俗园"],
    "上海": ["迪士尼", "外滩", "武康路"], "南京": ["夫子庙", "中山陵", "玄武湖"],
    "苏州": ["拙政园", "平江路", "山塘街"], "无锡": ["鼋头渚", "拈花湾", "惠山古镇"],
    "杭州": ["西湖", "灵隐", "龙井"], "宁波": ["天一阁", "天一广场", "东钱湖"],
    "绍兴": ["鲁迅故里", "安昌古镇"], "黄山": ["宏村", "屯溪", "西递"],
    "济南": ["趵突泉", "大明湖"], "青岛": ["八大关", "栈桥", "崂山"],
    "烟台": ["蓬莱", "养马岛"], "威海": ["那香海", "刘公岛", "火炬八街"],
    "厦门": ["鼓浪屿", "集美", "沙坡尾"], "泉州": ["开元寺", "蟳埔", "西街"],
    "景德镇": ["陶溪川", "御窑", "雕塑瓷厂"], "武汉": ["黄鹤楼", "汉口", "武昌"],
    "长沙": ["橘子洲", "岳麓山", "五一广场"], "郑州": ["河南博物院", "只有河南"],
    "洛阳": ["龙门石窟", "老君山", "白马寺"], "开封": ["清明上河园", "万岁山"],
    "广州": ["北京路", "永庆坊", "沙面"], "深圳": ["西涌", "大鹏", "企鹅岛"],
    "桂林": ["阳朔", "兴坪", "龙脊", "漓江"], "北海": ["涠洲岛", "银滩"],
    "海口": ["骑楼老街", "云洞图书馆"], "三亚": ["蜈支洲", "亚龙湾", "海棠湾"],
    "成都": ["春熙路", "宽窄巷子", "都江堰", "青城山"], "重庆": ["洪崖洞", "解放碑", "李子坝"],
    "贵阳": ["黔灵山", "甲秀楼", "青岩"], "昆明": ["滇池", "斗南", "翠湖"],
    "大理": ["洱海", "喜洲", "双廊"], "丽江": ["玉龙雪山", "束河", "白沙古镇"],
    "拉萨": ["布达拉宫", "大昭寺", "八廓街"], "林芝": ["巴松措", "鲁朗", "雅鲁藏布", "南迦巴瓦"],
    "西安": ["兵马俑", "华清宫", "大雁塔"], "兰州": ["中山桥", "甘肃省博物馆"],
    "西宁": ["塔尔寺", "青海省博物馆"], "银川": ["镇北堡", "西夏陵", "贺兰山"],
    "敦煌": ["莫高窟", "鸣沙山", "月牙泉"], "乌鲁木齐": ["大巴扎", "金泉商城", "天山天池"],
    "喀什": ["帕米尔", "塔县", "白沙湖", "喀什古城"],
}


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in {"www.rednote.com", "rednote.com", "www.xiaohongshu.com", "xiaohongshu.com"}:
        raise ValueError("只接受小红书/rednote HTTPS笔记链接")
    if not re.search(r"/(?:discovery/item|explore|search_result)/[0-9a-f]{24}", parts.path):
        raise ValueError("不是具体笔记链接")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def extract(page, candidate: dict, *, preserve_source_origin: bool = False, navigate: bool = True) -> dict:
    url = canonical(candidate["url"])
    visit_url = (candidate['url'] if preserve_source_origin else url).replace('/en/', '/zh/')
    if 'xiaohongshu.com' in url and not preserve_source_origin:
        note_id = re.search(r"/([0-9a-f]{24})$", url).group(1)
        visit_url = f"https://www.rednote.com/discovery/item/{note_id}"
    response = page.goto(visit_url, wait_until="domcontentloaded", timeout=25000) if navigate else None
    try:
        page.locator("#detail-desc").wait_for(state="visible", timeout=8000)
    except Error:
        if not navigate or preserve_source_origin or "xiaohongshu.com" not in url:
            raise
        # Follow the site's observed public rednote note route when the old domain
        # redirects to its homepage. This is an ordinary public page navigation.
        note_id = re.search(r"/([0-9a-f]{24})$", url).group(1)
        response = page.goto(f"https://www.rednote.com/discovery/item/{note_id}", wait_until="domcontentloaded", timeout=25000)
        page.locator("#detail-desc").wait_for(state="visible", timeout=8000)
    # inner_text is the exact visible DOM text; do not strip/rewrite the original body.
    body = page.locator("#detail-desc").inner_text()
    title = page.locator("#detail-title").inner_text()
    metadata = page.evaluate("""() => {
        const content = document.querySelector('.note-content');
        const authorRoot = document.querySelector('#noteContainer .author, .note-detail-mask .author, .author-container') || document;
        const author = [...authorRoot.querySelectorAll('a[href*="/user/profile/"], a[href*="/user/"]')]
            .filter(e => e.innerText.trim() && !['我','主页','首页'].includes(e.innerText.trim())).map(e => ({name: e.innerText.trim(), url:e.href}));
        const images = [...document.querySelectorAll('.swiper-slide img, .note-slider img, .media-container img')]
            .map(e => ({url:e.currentSrc || e.src, alt:e.alt || ''})).filter(e => e.url);
        const unique = images.filter((e,i,a) => a.findIndex(x=>x.url===e.url)===i);
        const videos = [...document.querySelectorAll('video')].map(e=>({url:e.currentSrc||e.src||'',poster:e.poster||''}));
        return {authors:author, displayed_date: content?.querySelector('.date')?.innerText || '',
            images:unique, videos, image_indicators:[...document.querySelectorAll('[class*="pagination"], [class*="fraction"]')].map(e=>e.innerText).filter(Boolean)};
    }""")
    city = candidate["city"]
    if len(body.strip()) < 220:
        raise ValueError(f"正文仅{len(body)}字，可能主要内容在图片/视频，暂不计入")
    if not any(word in title + "\n" + body for word in [city, *ALIASES.get(city, [])]):
        raise ValueError("未在正文或标题识别到目标城市/景点，需要人工核对")
    return {
        "city": city, "source_url": candidate['url'] if preserve_source_origin else url, "resolved_url": page.url,
        "note_id": re.search(r"([0-9a-f]{24})(?:\?|$)", url).group(1),
        "title": title, "body": body, "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "body_characters": len(body), "http_status": response.status if response else None,
        "collected_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "authorization": AUTHORIZATION, "preferred_theme": candidate.get("theme", "旅游经验"),
        **metadata,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--limit-per-city", type=int, default=3)
    args = parser.parse_args()
    candidates = json.loads(args.candidates.read_text(encoding="utf-8-sig"))
    output = ROOT / "knowledge"
    output.mkdir(exist_ok=True)
    counts = Counter()
    occupied = defaultdict(set)
    existing_ids = set()
    for path in output.glob("*.md"):
        match = re.match(r"(.+?)_(\d\d)_([0-9a-f]{24})_", path.name)
        if match:
            counts[match[1]] += 1
            occupied[match[1]].add(int(match[2]))
            existing_ids.add(match[3])
    excluded_path = HERE / 'excluded_notes.json'
    excluded_ids = {item['note_id'] for item in json.loads(excluded_path.read_text(encoding='utf-8'))} if excluded_path.exists() else set()
    report = json.loads(args.report.read_text(encoding="utf-8")) if args.report.exists() else []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 960}, locale="zh-CN")
            for candidate in candidates:
                city = candidate["city"]
                url = candidate["url"]
                note_match = re.search(r"/([0-9a-f]{24})(?:\?|$)", url)
                if counts[city] >= args.limit_per_city or (note_match and note_match[1] in existing_ids | excluded_ids):
                    continue
                if any(r.get("url") == url for r in report):
                    continue
                try:
                    record = extract(page, candidate)
                    number = next(i for i in range(1, args.limit_per_city+1) if i not in occupied[city])
                    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', record["title"])[:38].rstrip(' .')
                    name = f'{city}_{number:02d}_{record["note_id"]}_{safe_title}.md'
                    document = render(record, number)
                    assert '<!-- original-body:start -->\n' + record["body"] + '\n<!-- original-body:end -->' in document
                    save_json(HERE / "source_records" / (record["note_id"] + ".json"), record)
                    (output / name).write_text(document, encoding="utf-8", newline="\n")
                    counts[city] += 1
                    occupied[city].add(number)
                    existing_ids.add(record["note_id"])
                    item = {"city": city, "url": url, "status": "已保存页面正文", "file": name,
                            "title": record["title"], "body_characters": len(record["body"]),
                            "body_sha256": record["body_sha256"], "media_complete": False}
                except (Error, ValueError) as error:
                    item = {"city": city, "url": url, "status": "未计入", "reason": str(error).splitlines()[0]}
                report.append(item)
                save_json(args.report, report)
                print(json.dumps(item, ensure_ascii=False), flush=True)
                # A single normal navigation at a time, with a small fixed pacing interval.
                page.wait_for_timeout(1200)
        finally:
            browser.close()
    print(json.dumps({"saved_per_city": dict(counts)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
