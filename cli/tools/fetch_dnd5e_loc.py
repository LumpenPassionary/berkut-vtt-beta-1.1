# -*- coding: utf-8 -*-
"""Собрать русскую локализацию монстров с dnd.su.

Страницы бестиария dnd.su (Monster Manual на русском) содержат русские
названия монстров, особенности и атаки с полными правиловыми текстами.
Скрипт сопоставляет монстроров SRD-пака с их страницами по slug из
sitemap.xml, парсит заголовки/тексты особенностей и пишет единый файл
shared/dnd5e_loc_data.json, который подхватывает shared/dnd5e_loc при
нормализации библиотеки.

Сопоставление «английский заголовок особенностей -> русский» строится
автоматически (по монстрам, где остаётся единственная непарная пара,
голосование встречаемостью) плюс ручные пары из loc_glossary.json на
случаи, когда алгоритм не может решить однозначно.

Использование (из корня проекта):
    python cli/tools/fetch_dnd5e_loc.py --source G:/.../foundry
    python cli/tools/fetch_dnd5e_loc.py --source ... --refresh   # без кэша

Страницы кэшируются рядом с результатом (loc_cache/), чтобы повторные
прогоны не ходили в сеть.
"""
import argparse
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from shared import dnd5e_lib  # noqa: E402

USER_AGENT = "Mozilla/5.0 (dnd5e-loc-import; local VTT library)"
SITEMAP_URL = "https://dnd.su/sitemap.xml"


# ------------------------------------------------------------------
# Сеть с кэшем на диске
# ------------------------------------------------------------------

class Fetcher:
    def __init__(self, cache_dir, refresh=False):
        self.cache_dir = cache_dir
        self.refresh = refresh
        os.makedirs(cache_dir, exist_ok=True)

    def get(self, url, cache_name):
        path = os.path.join(self.cache_dir, cache_name)
        if not self.refresh and os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read().decode("utf-8", "replace")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        time.sleep(0.25)    # вежливая пауза между запросами
        return body


# ------------------------------------------------------------------
# Извлечение пака монстров (через существующий Node-экстрактор)
# ------------------------------------------------------------------

def load_pack_docs(source):
    node = dnd5e_lib._find_node()
    pack_dir = os.path.join(source, "packs", "monsters")
    if not os.path.isdir(pack_dir):
        raise SystemExit(f"Не найден пак: {pack_dir}")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="dnd5e_loc_") as tmp:
        out = os.path.join(tmp, "monsters.jsonl")
        dnd5e_lib._run_extractor(node, pack_dir, out)
        return dnd5e_lib._read_jsonl(out)


# ------------------------------------------------------------------
# Поиск страницы монстра
# ------------------------------------------------------------------

def normalize_name(name):
    text = re.sub(r"\(([^)]*)\)", " ", str(name or "").lower())
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def bestiary_map(sitemap_html):
    urls = re.findall(
        r"<loc>(https://dnd\.su/bestiary/(\d+)-([a-z0-9_\-]+)/)</loc>",
        sitemap_html)
    by_slug = {}
    for url, _num, slug in urls:
        by_slug[slug.replace("_", " ")] = url
    return by_slug


def find_page(name, by_slug):
    key = normalize_name(name)
    if key in by_slug:
        return by_slug[key]
    # Скобки в именах («Giant Rat (Diseased)») и хвосты-уточнения
    # на dnd.su часто пишутся иначе — ищем по началу slug.
    candidates = sorted(s for s in by_slug if s.startswith(key + " "))
    if len(candidates) == 1:
        return by_slug[candidates[0]]
    exact = [s for s in by_slug if key and (key in s)]
    if len(exact) == 1:
        return by_slug[exact[0]]
    return None


# ------------------------------------------------------------------
# Разбор страницы монстра
# ------------------------------------------------------------------

_SECTION_RE = re.compile(
    r"<li class=\"subsection desc\">(.*?)</li>", re.S)
_HEADING_RE = re.compile(
    r"<h3 class=\"subsection-title\">(.*?)</h3>", re.S)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
# Заголовок особенностей на dnd.su встречается в двух написаниях:
#   <p><strong><em>Тонкий нюх.</em></strong> текст</p>
#   <p><em><strong>Наскок.</strong> </em> текст</p>
# (плюс варианты со style-атрибутом на strong).
_FEATURE_RES = (
    re.compile(r"^<strong(?:\s[^>]*)?>\s*<em>(.*?)</em>[\s.]*</strong>\s*"
               r"(.*)$", re.S),
    # Текст фичи может начинаться ещё внутри <em> — тогда он доходит до
    # конца абзаца («<em><strong>Укус.</strong> Рукопашная атака…</em>: …»).
    re.compile(r"^<em>\s*<strong(?:\s[^>]*)?>(.*?)</strong>\s*(.*)$", re.S),
)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)

# Секции страницы, где лежат особенности и атаки. Всё остальное («Описание»,
# «Логово», «Местные эффекты», таблицы статблока) — не фичи и в локализацию
# особенностей не берётся.
_FEATURE_SECTIONS = {
    "действия", "бонусные действия", "реакции", "реакция",
    "особенности", "легендарные действия", "действия логова",
    "действия в логове",
}
# Страховка: заголовки из таблицы статблока не должны стать «особенностью».
_NOT_FEATURE_TITLES = {
    "языки", "чувства", "опасность", "характеристики", "местность",
    "бонус мастерства", "сопротивления", "иммунитеты", "уязвимости",
    "размер", "тип", "мировоззрение", "навыки", "другие черты и действия",
    "другие действия", "умения", "условия",
}


def _clean_foundry_macros(text):
    """Убрать Foundry-викимакросы из текста, сохранив читаемую метку."""
    if not text:
        return text
    # Сохраняем видимое название из {label} внутри [[/item ...]]{label}
    def _item_repl(m):
        s = m.group(0)
        lbl = re.search(r"\{([^}]+)\}\s*$", s)
        if lbl:
            return lbl.group(1)
        # Альтернативный вид: [[/item uuid]] без скобки метки — удаляем макрос
        return ""

    # [[/item ...]]{...} или просто [[/item ...]]
    text = re.sub(r"\[\[/item[^\]]*\]\](?:\{[^}]+\})?", _item_repl, text, flags=re.I)
    # [[lookup ...]] — часто мусор; пробуем выдернуть последнее слово как имя? нет, обычно пустой смысл
    text = re.sub(r"\[\[lookup[^\]]*\]\]", "", text, flags=re.I)
    # [[/roll ...]] и прочие [[...]]
    text = re.sub(r"\[\[/[^\]]*\]\]", "", text, flags=re.I)
    text = re.sub(r"\[\[[^\]]*\]\]", "", text, flags=re.I)
    # [/save ...], [/damage ...], [/heal ...]
    text = re.sub(r"\[/\w+[^\]]*\]", "", text, flags=re.I)
    # Множественные пробелы/переносы
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _plain(fragment):
    text = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = _clean_foundry_macros(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_embedded_page(page_html):
    """Страница заклинания/предмета dnd.su -> (русское имя, текст)."""
    title = _TITLE_RE.search(page_html)
    if not title:
        return None
    ru_name = _plain(title.group(1)).split(" / ")[0].strip()
    paragraphs = []
    for section in _SECTION_RE.findall(page_html):
        heading = _HEADING_RE.search(section)
        if heading and _plain(heading.group(1)).casefold() not in {
                "описание"}:
            continue
        for para in _P_RE.findall(section):
            text = _plain(para)
            if text:
                paragraphs.append(text)
        if paragraphs:
            break
    if not paragraphs:
        return None
    return ru_name, " ".join(paragraphs)


def parse_monster_page(page_html):
    """-> (русское имя, [(русский заголовок, текст), ...])."""
    title = _TITLE_RE.search(page_html)
    ru_name = ""
    if title:
        ru_name = _plain(title.group(1)).split(" / ")[0].strip()
    features = []
    for section in _SECTION_RE.findall(page_html):
        heading = _HEADING_RE.search(section)
        if heading:
            heading_text = _plain(heading.group(1)).casefold()
            # Примечания и лор-разделы к особенностям не относятся
            # (без h3 идут собственно «Особенности» — их берём).
            if heading_text not in _FEATURE_SECTIONS:
                continue
        section_label = (_plain(heading.group(1)) if heading else "")
        in_entry = False    # уже внутри записи (фичи или вступления секции)
        for para in _P_RE.findall(section):
            para = para.strip()
            m = next((rx.match(para) for rx in _FEATURE_RES
                      if rx.match(para)), None)
            if m:
                name = _plain(m.group(1))
                # «Порабощение (3/день)» — условие использования не часть имени
                name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip(" .")
                if name.casefold() in _NOT_FEATURE_TITLES:
                    continue
                features.append([name, _plain(m.group(2))])
                in_entry = True
            elif para:
                if not in_entry:
                    # Первый безымянный абзац секции — её вступление
                    # («Дракон может совершить 3 легендарных действия…»).
                    # Безымянным его делать нельзя: текст приклеился бы
                    # к чужой особенности из предыдущей секции.
                    if not section_label:
                        continue
                    features.append([section_label, _plain(para)])
                    in_entry = True
                else:
                    features[-1][1] = _plain(features[-1][1] + " " + para)
    return ru_name, features


# ------------------------------------------------------------------
# Сопоставление английских заголовков с русскими
# ------------------------------------------------------------------

def norm_key(text):
    # Скобочные уточнения («(3/день)», «(Vampire Form Only)») — не часть
    # заголовка: у EN и RU они записаны по-разному и только мешают паре.
    # «ё» и «е» в словарях встречаются в одном и том же слове.
    text = re.sub(r"\([^)]*\)", " ", str(text).casefold()).replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^a-zа-яе0-9 ]+", " ",
                                       text)).strip()


def align_titles(pack_en_by_monster, pages_ru_by_monster, glossary):
    """Вернуть {EN-заголовок: RU-заголовок} и список сомнительных пар.

    Оценка пары (e, r): по всем монстрам, где встречается английский
    заголовок e, суммируется доля монстров, у которых есть и русский r —
    с весом 1/(|E|·|R|), чтобы страницы с малым числом заголовков решали
    сильнее. Затем жадный проход по частоте EN: каждому английскому
    заголовку достаётся свободный русский с лучшим баллом. Ручной словарь
    (glossary) занимает пары первым. Сомнительные случаи (перевес второго
    места меньше половины первого) уходят в отчёт на ручную проверку.
    """
    known = {norm_key(k): norm_key(v) for k, v in glossary.items()}

    def free_ru(monster):
        used = set(norm_key(v) for v in known.values())
        out, seen = [], set()
        for ru, _text in pages_ru_by_monster.get(monster, []):
            if norm_key(ru) in used or norm_key(ru) in seen:
                continue
            seen.add(norm_key(ru))
            out.append(ru)
        return out

    # Шаг 1: ручной словарь + волны «единственная пара»: если у монстра
    # осталась ровно одна непереведённая EN и одна непарная RU — пара
    # доказана и обогащает словарь для остальных монстров.
    changed = True
    while changed:
        changed = False
        for monster in sorted(pack_en_by_monster):
            if monster not in pages_ru_by_monster:
                continue
            en_free = [e for e in dict.fromkeys(pack_en_by_monster[monster])
                       if norm_key(e) and norm_key(e) not in known]
            ru_free = free_ru(monster)
            if len(en_free) == 1 and len(ru_free) == 1:
                known[norm_key(en_free[0])] = norm_key(ru_free[0])
                changed = True

    # Шаг 2: для остатка — статистика по долям P(r|e): среди всех русских
    # заголовков, встреченных вместе с EN-заголовком e, берётся тот, чья
    # взвешенная доля наибольша.
    scores = defaultdict(Counter)
    en_freq = Counter()
    for monster, ens in pack_en_by_monster.items():
        rus = pages_ru_by_monster.get(monster)
        if not rus:
            continue
        en_set = list(dict.fromkeys(e for e in ens if norm_key(e)))
        ru_set = list(dict.fromkeys(r for r, _ in rus if norm_key(r)))
        if not en_set:
            continue
        weight = 1.0 / (len(en_set) * max(len(ru_set), 1))
        for e in en_set:
            en_freq[e] += 1
        for e in en_set:
            for r in ru_set:
                scores[e][norm_key(r)] += weight

    ru_labels = {}
    for rus in pages_ru_by_monster.values():
        for r, _text in rus:
            ru_labels.setdefault(norm_key(r), r)
    for ru_key in known.values():
        ru_labels.setdefault(ru_key, ru_key)

    used_ru = set(known.values())
    suspicious = []
    for en, _freq in en_freq.most_common():
        if norm_key(en) in known:
            continue
        total = sum(scores[en].values())
        if not total:
            suspicious.append((en, "нет кандидатов", [], []))
            continue
        candidates = sorted(
            ((count / total, rk) for rk, count in scores[en].items()
             if rk not in used_ru), reverse=True)
        if not candidates:
            suspicious.append((en, "нет кандидатов", [], []))
            continue
        best_share, best_ru = candidates[0]
        second = candidates[1][0] if len(candidates) > 1 else 0.0
        known[norm_key(en)] = best_ru
        used_ru.add(best_ru)
        if best_share < 0.4 or second > 0.6 * best_share:
            suspicious.append((en, "сомнительная пара",
                               [(round(s, 3), ru_labels.get(rk, rk))
                                for s, rk in candidates[:3]], []))

    unresolved = []
    for monster in sorted(pack_en_by_monster):
        if monster not in pages_ru_by_monster:
            unresolved.append((monster, "нет страницы",
                               list(dict.fromkeys(
                                   pack_en_by_monster[monster])), []))
            continue
        en_free = [e for e in dict.fromkeys(pack_en_by_monster[monster])
                   if norm_key(e) not in known]
        ru_rest = free_ru(monster)
        if en_free or ru_rest:
            unresolved.append((monster, "не выровнено", en_free, ru_rest))
    return known, unresolved, suspicious


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True,
                    help="Папка установленной системы dnd5e (с packs/monsters)")
    ap.add_argument("--out", default=os.path.join(ROOT, "shared",
                                                  "dnd5e_loc_data.json"))
    ap.add_argument("--cache", default=None)
    ap.add_argument("--glossary", default=os.path.join(
        HERE, "loc_glossary.json"))
    ap.add_argument("--refresh", action="store_true",
                    help="Не использовать кэш страниц")
    ap.add_argument("--report", default=None,
                    help="куда записать отчёт о непокрытых заголовках")
    args = ap.parse_args()

    cache = args.cache or os.path.join(os.path.dirname(args.out),
                                       "loc_cache")
    fetcher = Fetcher(cache, refresh=args.refresh)

    docs = load_pack_docs(os.path.abspath(args.source))
    npcs = [d for d in docs if d.get("type") == "npc"]
    items_by = {}
    for d in docs:
        m = dnd5e_lib._PACK_CHILD_RE.match(d.get("_packKey") or "")
        if m:
            key = (m.group(1), m.group(2))
            old = items_by.get(key)
            if old is None or ((d.get("system") or {}).get("activities")
                               and not (old.get("system") or {}).get(
                                   "activities")):
                items_by[key] = d

    print(f"Монстров в паке: {len(npcs)}")
    sitemap_html = fetcher.get(SITEMAP_URL, "sitemap.xml")
    by_slug = bestiary_map(sitemap_html)
    print(f"Страниц бестиария в sitemap: {len(by_slug)}")

    glossary = {}
    if os.path.isfile(args.glossary):
        with open(args.glossary, encoding="utf-8") as f:
            glossary = json.load(f).get("feature_titles", {})

    monsters = {}
    pack_en = {}
    pages_ru = {}
    missing_pages = []
    for doc in sorted(npcs, key=lambda d: (d.get("name") or "").lower()):
        name = doc.get("name") or ""
        url = find_page(name, by_slug)
        if not url:
            missing_pages.append(name)
            continue
        cache_name = "page_" + re.sub(r"[^a-z0-9]+", "_",
                                      url.rstrip("/").split("/")[-1]) + ".html"
        page_html = fetcher.get(url, cache_name)
        ru_name, ru_features = parse_monster_page(page_html)
        if not ru_name:
            missing_pages.append(name + " (страница не разобрана)")
            continue

        # Заголовки особенностей/атак монстра из пака
        en_titles = []
        for item_id in (doc.get("items") or []):
            item = items_by.get((doc.get("_id"), item_id))
            if not item:
                continue
            item_name = item.get("name") or ""
            if item.get("type") in ("feat", "weapon"):
                en_titles.append(item_name)
        pack_en[name] = en_titles
        pages_ru[name] = ru_features
        monsters[norm_key(name)] = {
            "url": url,
            "name": ru_name,
            "features": {norm_key(ru): {"name": ru, "text": text}
                         for ru, text in ru_features},
        }

    print(f"Найдено страниц: {len(monsters)}; без страниц: "
          f"{len(missing_pages)}")
    if missing_pages:
        print("  без страниц:", ", ".join(missing_pages[:20]))

    known, unresolved, suspicious = align_titles(pack_en, pages_ru,
                                                 glossary)
    covered = sum(1 for m in pack_en for t in dict.fromkeys(pack_en[m])
                  if norm_key(t) in known)
    total = sum(len(dict.fromkeys(pack_en[m])) for m in pack_en)
    print(f"Заголовков особенностей: {total}, переведено: {covered}")

    # ------------------------------------------------------------------
    # Встроенные в монстров заклинания и предметы: их описания на страницах
    # dnd.su (/spells/, /items/) — иначе в карточках монстров остаются
    # английские тексты призванных заклинаний и снаряжения.
    # ------------------------------------------------------------------
    spell_pack_names = {d.get("name") for d in docs
                        if d.get("type") == "spell"}
    # Достаточно тех встроенных записей, что реально есть у монстров пака:
    embedded_names = {
        it.get("name")
        for a in npcs for iid in (a.get("items") or [])
        if (it := items_by.get((a.get("_id"), iid))) and it.get("name")
        and it.get("type") != "feat"
    }

    def _slugs(category):
        slugs = {}
        pattern = (rf"<loc>(https://dnd\.su/{category}/\d+-[a-z0-9_\-]+/?)"
                   rf"</loc>")
        for url in re.findall(pattern, sitemap_html):
            slug = url.rstrip("/").split("/")[-1].split("-", 1)[-1]
            slugs.setdefault(slug.replace("_", " "), url)
        return slugs

    spell_slugs, item_slugs = _slugs("spells"), _slugs("items")
    embedded = {}
    embedded_miss = []
    for name in sorted(embedded_names):
        is_spell = name in spell_pack_names
        slug = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ",
                                           name.lower())).strip()
        url = (spell_slugs if is_spell else item_slugs).get(slug)
        if not url:
            # Запасной поиск: у dnd.su имя книги попадает в slug
            # («Acid Arrow» -> melf_s_acid_arrow) — берём единственный slug,
            # где все слова имени присутствуют.
            words = slug.split()
            candidates = [u for s, u in
                          (spell_slugs if is_spell else item_slugs).items()
                          if all(w in s.split() for w in words)]
            if len(candidates) == 1:
                url = candidates[0]
        if not url:
            embedded_miss.append(name)
            continue
        cache_name = ("spell_" if is_spell else "item_") + re.sub(
            r"[^a-z0-9]+", "_", url.rstrip("/").split("/")[-1]) + ".html"
        page_html = fetcher.get(url, cache_name)
        parsed = parse_embedded_page(page_html)
        if parsed:
            embedded[norm_key(name)] = {"name": parsed[0], "text": parsed[1]}
    print(f"Встроенных заклинаний/предметов локализовано: {len(embedded)} "
          f"(без страницы: {len(embedded_miss)})")
    if embedded_miss:
        with open(os.path.join(cache, "embedded_miss.txt"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(embedded_miss))

    glossary_miss = []

    # Связать переведённые заголовки с текстами страниц
    texts = {}
    for monster_name, ru_features in pages_ru.items():
        key = norm_key(monster_name)
        en_titles = list(dict.fromkeys(pack_en.get(monster_name, [])))
        ru_by_key = {norm_key(ru): (ru, text)
                     for ru, text in ru_features}
        for en in en_titles:
            ru = known.get(norm_key(en))
            if not ru:
                continue
            hit = ru_by_key.get(norm_key(ru))
            if hit:
                texts[(key, norm_key(en))] = {"name": hit[0],
                                              "text": hit[1]}
    feature_texts = {}
    for (monster_key, en_key), value in texts.items():
        feature_texts.setdefault(monster_key, {})[en_key] = value

    # Монстр-локальные привязки: если у монстра остался ровно один
    # непереведённый английский заголовок и один непарный русский — пара
    # доказана для этого монстра (в общий словарь не обобщается: там
    # такая связь может быть неверна для остальных).
    local_added = 0
    for monster_name, ru_features in pages_ru.items():
        mkey = norm_key(monster_name)
        if monster_name not in pages_ru:
            continue
        page_keys = {norm_key(r) for r, _t in ru_features}
        # EN-заголовки, у которых для этого монстра нет своего русского
        # текста: либо не переведены вовсе, либо глобальная пара с
        # другим формулированием («Огненное дыхание» vs «Оружия дыхания»).
        en_unserved = [e for e in dict.fromkeys(
            pack_en.get(monster_name, []))
            if not norm_key(e) or norm_key(e) not in known
            or known.get(norm_key(e)) not in page_keys]
        used_ru = set(known.values())
        ru_free, seen = [], set()
        for ru, _text in ru_features:
            rk = norm_key(ru)
            if not rk or rk in used_ru or rk in seen:
                continue
            seen.add(rk)
            ru_free.append(ru)
        if len(en_unserved) == 1 and len(ru_free) == 1 \
                and norm_key(en_unserved[0]) not in feature_texts.get(
                    mkey, {}):
            ru = ru_free[0]
            text = next(t for r, t in ru_features if r == ru)
            feature_texts.setdefault(mkey, {})[norm_key(en_unserved[0])] = {
                "name": ru, "text": text}
            local_added += 1
    feature_texts = {k: v for k, v in feature_texts.items() if v}
    print(f"Монстр-локальных привязок: {local_added}")

    # Глобальные тексты особенностей: мода текстов по разным монстрам —
    # закрывает фичи монстров, у которых нет собственной страницы.
    feature_global = {}
    texts_by_en = defaultdict(Counter)
    for monster_key, feats in feature_texts.items():
        for en_key, record in feats.items():
            if record.get("text"):
                texts_by_en[en_key][record["text"]] += 1
    for en_key, counter in texts_by_en.items():
        feature_global[en_key] = counter.most_common(1)[0][0]

    for data in monsters.values():
        data.pop("features_by_ru", None)

    for en_key in glossary:
        nk = norm_key(en_key)
        if not any(nk in feats for feats in feature_texts.values()):
            glossary_miss.append(en_key)
    if glossary_miss:
        miss_path = os.path.join(cache, "glossary_miss.txt")
        with open(miss_path, "w", encoding="utf-8") as f:
            for en_key in glossary_miss:
                f.write(f"{en_key} -> {glossary[en_key]}\n")
        print(f"Пары словаря без текста: {len(glossary_miss)} -> {miss_path}")

    # ------------------------------------------------------------------
    # Двойные ключи: запись регистрируется и под английским ключом пака,
    # и под русским ключом, который увидит клиент после авто-локализации
    # (эквиваленты обязаны давать одинаковый key с любой стороны хоста).
    # ------------------------------------------------------------------

    # Имена монстров: EN-ключ пака + RU-ключ-альяс на ту же запись.
    monsters_out = {}
    for mkey, rec in monsters.items():
        monsters_out[mkey] = rec
        ru_key = norm_key(rec.get("name"))
        if ru_key:
            monsters_out.setdefault(ru_key, rec)

    # Общий словарь заголовков: EN->RU плюс RU->RU, чтобы фолбэк имени
    # работал и для русского представителя класса.
    titles_out = dict(known)
    for ru_val in known.values():
        titles_out.setdefault(ru_val, ru_val)

    # Тексты фич под всеми комбинациями ключей «монстр × заголовок»
    # (обе стороны каждой размерности).
    feature_texts_out = {}
    global_ru = defaultdict(set)
    for m_en, feats in feature_texts.items():
        m_rus = norm_key(monsters.get(m_en, {}).get("name"))
        for f_en, rec in feats.items():
            f_rus = norm_key(rec.get("name"))
            if f_rus:
                global_ru[f_en].add(f_rus)
            for mk in dict.fromkeys(k for k in (m_en, m_rus) if k):
                for fk in dict.fromkeys(k for k in (f_en, f_rus) if k):
                    feature_texts_out.setdefault(mk, {})[fk] = rec

    # Глобальные тексты: EN-ключ и все наблюдавшиеся RU-заголовки класса.
    feature_global_out = {}
    for f_en, text in feature_global.items():
        feature_global_out[f_en] = text
        for rk in global_ru.get(f_en, ()):
            feature_global_out.setdefault(rk, text)

    # Встроенные заклинания/предметы: EN-имя пака + RU-имя страницы.
    embedded_out = {}
    for en_key, rec in embedded.items():
        embedded_out[en_key] = rec
        ru_key = norm_key(rec.get("name"))
        if ru_key:
            embedded_out.setdefault(ru_key, rec)

    payload = {
        "source": "dnd.su",
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "titles": titles_out,
        "monsters": monsters_out,
        "feature_texts": feature_texts_out,
        "feature_global": feature_global_out,
        "embedded": embedded_out,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"Записано: {args.out} "
          f"({os.path.getsize(args.out) // 1024} КБ, "
          f"монстров: {len(monsters_out)}, локализованных фич: "
          f"{sum(len(v) for v in feature_texts_out.values())})")

    if suspicious:
        report2 = args.report or os.path.join(cache, "suspicious.txt")
        if args.report:
            report2 = os.path.splitext(args.report)[0] + "_suspicious.txt"
        with open(report2, "w", encoding="utf-8") as f:
            for en, reason, cands, _ in suspicious:
                f.write(f"{en} [{reason}]\n")
                for score, ru in cands:
                    f.write(f"  {score}: {ru}\n")
        print(f"Сомнительных пар: {len(suspicious)} -> {report2}")
    if unresolved:
        report = args.report or os.path.join(cache, "unresolved.txt")
        with open(report, "w", encoding="utf-8") as f:
            for monster, reason, ens, rus in unresolved:
                f.write(f"{monster} [{reason}]\n")
                for en in ens:
                    f.write(f"  EN: {en}\n")
                for ru in rus:
                    f.write(f"  RU: {ru}\n")
        print(f"Не выровнено заголовков у монстров: {len(unresolved)} "
              f"-> {report}")


if __name__ == "__main__":
    main()
