"""Картинки токенов справочника: имя существа -> webp-файл.

В Foundry связь «актёр -> картинка токена» задана в
``json/fa-token-mapping.json`` по внутреннему id актёра. У нас картинки
разложены папками по типу существа (``library/dnd5e/dragon/RedDragonAdult.webp``),
поэтому роль такого индекса играет само имя файла: приводим имена к одному
виду и ищем совпадение.

Правила повторяют то, как Foundry называет файлы токенов:

* ``Adult White Dragon`` -> ``WhiteDragonAdult.webp`` (возраст уходит в конец);
* ``Swarm of Ravens`` -> ``SwarmRavens.webp`` (предлог не пишется);
* ``Giant Rat (Diseased)`` -> ``GiantRat.webp`` (уточнение в скобках);
* ``Succubus/Incubus`` -> ``Succubus.webp`` (берётся первая форма).

Папка ``thumbs`` пропускается: там уменьшенные копии для листов, а на холсте
нужен полноразмерный токен.
"""
import os
import re
import unicodedata

IMAGE_EXTENSIONS = (".webp", ".png", ".jpg", ".jpeg")
EXCLUDED_DIRS = {"thumbs"}

# Имена, которые в Foundry исторически не совпадают с именем актёра.
ALIASES = {
    "airelemental": "windelemental",
    "barbeddevil": "barbed",
    "homunculus": "homonculus",
    "cat": "catorange",
    "crab": "craborange",
}

_CACHE = {}


def normalize_token_name(value) -> str:
    """Ключ сравнения: нижний регистр, без пробелов и знаков препинания."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = text.replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def token_keys(name) -> list:
    """Варианты ключа для имени актёра (от точного к более общему).

    Первым идёт прямое совпадение, затем — варианты без пояснений и с
    перестановкой частей имени. Порядок важен: он же порядок поиска.
    """
    raw = str(name or "")
    keys = []

    def add(key):
        if key and key not in keys:
            keys.append(key)

    add(normalize_token_name(raw))
    # «Giant Rat (Diseased)» -> «Giant Rat».
    no_paren = re.sub(r"\([^)]*\)", " ", raw)
    add(normalize_token_name(no_paren))
    # «Swarm of Ravens» -> «Swarm Ravens».
    no_of = re.sub(r"\bof\b", " ", raw, flags=re.I)
    add(normalize_token_name(no_of))
    add(normalize_token_name(re.sub(r"\([^)]*\)|\bof\b", " ", raw, flags=re.I)))

    # «Adult White Dragon» -> «White Dragon Adult».
    dragon = re.match(r"^\s*(adult|ancient|young|wyrmling)\s+(.+?)\s+dragon\s*$",
                      raw, flags=re.I)
    if dragon:
        add(normalize_token_name(
            f"{dragon.group(2)} Dragon {dragon.group(1)}"))

    # «Succubus/Incubus» -> «Succubus».
    for part in re.split(r"[/,]", raw):
        add(normalize_token_name(part))

    return keys


def library_files(root: str) -> dict:
    """{нормализованное имя файла: путь относительно root} для всех картинок."""
    root = os.path.abspath(root)
    cached = _CACHE.get(root)
    if cached is not None:
        return cached

    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d.casefold() not in EXCLUDED_DIRS)
        for filename in sorted(filenames):
            if not filename.casefold().endswith(IMAGE_EXTENSIONS):
                continue
            relative = os.path.relpath(os.path.join(dirpath, filename), root)
            key = normalize_token_name(os.path.splitext(filename)[0])
            files.setdefault(key, relative.replace(os.sep, "/"))
    _CACHE[root] = files
    return files


def clear_cache():
    """Сбросить кэш сканирования (нужен тестам и после дозаливки картинок)."""
    _CACHE.clear()


def resolve(root: str, record: dict, files: dict = None):
    """Абсолютный путь к картинке записи либо None.

    Сначала используется явный ``img`` из записи (так работал импорт SRD),
    затем — поиск по имени и его вариантам. Если файла нет в этом корне,
    возвращается None: вызывающий код пробует следующий корень библиотеки.
    """
    img = (record or {}).get("img")
    if img:
        path = img if os.path.isabs(img) else os.path.join(root, img)
        if os.path.isfile(path):
            return path

    files = files if files is not None else library_files(root)
    source = (record or {}).get("source_name") or (record or {}).get("name") or ""
    for key in token_keys(source):
        relative = files.get(key)
        if relative is None:
            alias = ALIASES.get(key)
            relative = files.get(alias) if alias else None
        if relative:
            return os.path.join(root, *relative.split("/"))
    return None
