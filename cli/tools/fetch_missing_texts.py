"""Дозагрузить с dnd.su описания заклинаний и предметов, которых ещё нет.

Берёт компактный список из shared/dnd5e_strings.json, ищет страницу по slug
в sitemap.xml и складывает найденный русский текст в dnd5e_loc_data.json
(раздел embedded). Страницы кэшируются, повторные прогоны в сеть не ходят.

    python3 cli/tools/fetch_missing_texts.py [--limit N] [--section spells]

Почему отдельный скрипт: основной fetch_dnd5e_loc.py собирает словарь от
монстров и берёт только встроенные в них заклинания. Отдельные страницы
/spells/ и /items/ он не обходит, поэтому 60% описаний оставались английскими.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

from shared import dnd5e_loc  # noqa: E402

USER_AGENT = "Mozilla/5.0 (dnd5e-loc-import; local VTT library)"
SITEMAP_URL = "https://dnd.su/sitemap.xml"
CACHE_DIR = os.path.join(BASE, "shared", "loc_cache")

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_HEADING_RE = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.S | re.I)
_SECTION_RE = re.compile(r"<section[^>]*>(.*?)</section>", re.S | re.I)
_DESC_RE = re.compile(r'<div[^>]*itemprop="description"[^>]*>(.*?)</div>', re.S | re.I)


def plain(fragment):
    import html
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", fragment or ""))).strip()


def parse_page(page_html):
    """Страница заклинания/предмета -> (русское имя, текст) либо None.

    Текст лежит в <div itemprop="description">. Раньше парсер искал
    <section> с заголовком «Описание» — в текущей вёрстке dnd.su такого
    больше нет, поэтому все страницы считались неразобранными.
    """
    title = _TITLE_RE.search(page_html)
    if not title:
        return None
    ru_name = plain(title.group(1)).split(" / ")[0].strip()

    block = _DESC_RE.search(page_html)
    if not block:
        return None
    paragraphs = [plain(p) for p in _P_RE.findall(block.group(1))]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return None
    return ru_name, " ".join(paragraphs)


class Fetcher:
    def __init__(self, cache_dir, delay=0.35):
        self.cache_dir = cache_dir
        self.delay = delay
        os.makedirs(cache_dir, exist_ok=True)

    def get(self, url, cache_name):
        path = os.path.join(self.cache_dir, cache_name)
        if os.path.isfile(path):
            with open(path, encoding='utf-8') as f:
                return f.read()
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                html = response.read().decode('utf-8', 'replace')
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f'  сеть: {exc}')
            return ''
        time.sleep(self.delay)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)
        return html


def slugs_from_sitemap(sitemap_html, category):
    """slug -> url для раздела /spells/ или /items/."""
    out = {}
    pattern = (rf"<loc>(https://dnd\.su/{category}/\d+-[a-z0-9_\-]+/?)</loc>")
    for url in re.findall(pattern, sitemap_html):
        slug = url.rstrip('/').split('/')[-1].split('-', 1)[-1]
        out.setdefault(slug.replace('_', ' '), url)
    return out


def match_slug(name, table):
    """Найти url по имени: точный slug, затем все слова имени в slug."""
    slug = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", name.lower())).strip()
    if slug in table:
        return table[slug]
    words = slug.split()
    candidates = [u for s, u in table.items() if all(w in s.split() for w in words)]
    if len(candidates) == 1:
        return candidates[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='сколько страниц максимум')
    ap.add_argument('--section', default='', help='spells | items')
    ap.add_argument('--delay', type=float, default=0.35)
    args = ap.parse_args()

    strings_path = os.path.join(BASE, 'shared', 'dnd5e_strings.json')
    with open(strings_path, encoding='utf-8') as f:
        strings = json.load(f)['sections']

    data_path = os.path.join(BASE, 'shared', 'dnd5e_loc_data.json')
    with open(data_path, encoding='utf-8') as f:
        data = json.load(f)
    embedded = data.setdefault('embedded', {})

    fetcher = Fetcher(CACHE_DIR, delay=args.delay)
    sitemap = fetcher.get(SITEMAP_URL, 'sitemap.xml')
    tables = {
        'spells': slugs_from_sitemap(sitemap, 'spells'),
        'items': slugs_from_sitemap(sitemap, 'items'),
    }
    print(f'sitemap: заклинаний {len(tables["spells"])}, предметов {len(tables["items"])}')

    sections = [args.section] if args.section else ['spells', 'items']
    budget = args.limit or 10 ** 9
    added = missing = 0

    for section in sections:
        records = strings.get(section, {})
        todo = [n for n in records if not embedded.get(dnd5e_loc._norm_key(n))]
        print(f'\n{section}: нужен текст для {len(todo)} записей')
        for name in todo:
            if added >= budget:
                break
            url = match_slug(name, tables[section])
            if not url:
                missing += 1
                continue
            cache_name = ('spell_' if section == 'spells' else 'item_') + re.sub(
                r'[^a-z0-9]+', '_', url.rstrip('/').split('/')[-1]) + '.html'
            parsed = parse_page(fetcher.get(url, cache_name))
            if not parsed:
                missing += 1
                continue
            embedded[dnd5e_loc._norm_key(name)] = {
                'name': parsed[0], 'text': parsed[1]}
            added += 1
            if added % 25 == 0:
                print(f'  ... {added} добавлено')
                with open(data_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

    with open(data_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    print(f'\nдобавлено: {added} | без страницы или не разобрано: {missing}')
    print(f'всего embedded: {len(embedded)}')


if __name__ == '__main__':
    main()
