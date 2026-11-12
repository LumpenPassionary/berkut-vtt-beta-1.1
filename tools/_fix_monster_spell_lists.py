"""Дописать в справочник потерянные списки заклинаний у монстров.

При импорте SRD вложенные пункты особенностей потерялись: текст обрывался
на двоеточии («У сфинкса подготовлены следующие заклинания жреца:»), и в
карточке справочника не было ни одного заклинания. Этот скрипт:

1. находит особенности, чей текст кончается двоеточием;
2. берёт английское описание той же особенности из открытого SRD-API
   (https://www.dnd5eapi.co) — там вложенные пункты сохранены;
3. переводит названия заклинаний по уже существующему словарю
   ``library/names_spells_ru.json`` (он же используется остальным столом);
4. дописывает пункты в русский текст особенности.

Скрипт идемпотентен: если пункты уже есть (текст не кончается двоеточием),
запись не трогается. Сеть нужна только при запуске скрипта, игра работает
с готовым ``monsters.json``.

Запуск:
    python3 tools/_fix_monster_spell_lists.py
"""
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBRARY = os.path.join(os.path.dirname(os.path.dirname(ROOT)), "Core",
                       "library", "dnd5e")
MONSTERS = os.path.join(LIBRARY, "monsters.json")
SPELL_NAMES = os.path.join(ROOT, "library", "names_spells_ru.json")
API = "https://www.dnd5eapi.co/api/2014/monsters/{}"

#: Подписи уровней ячеек в списке заклинаний: «1st level (4 slots)».
_LEVEL_LABELS = {
    "cantrips (at will)": "Заговоры (неограниченно)",
    "cantrips": "Заговоры",
    "0 level": "Заговоры",
    "at will": "Неограниченно",
    "1/day each": "1/день каждое",
    "1/day": "1/день",
    "3/day each": "3/день каждое",
    "3/day": "3/день",
    "2/day each": "2/день каждое",
    "2/day": "2/день",
    "1st level": "1-й уровень",
    "2nd level": "2-й уровень",
    "3rd level": "3-й уровень",
    "4th level": "4-й уровень",
    "5th level": "5-й уровень",
    "6th level": "6-й уровень",
    "7th level": "7-й уровень",
    "8th level": "8-й уровень",
    "9th level": "9-й уровень",
}

#: Немногочисленные не-заклинательные вложенные списки, которые тоже
#: потерялись при импорте (слабости вампира и подобное).
#: Словари для разбора дыхания драконов: вложенные пункты «Breath
#: Weapons» в SRD записаны прозой, а не списком.
_BREATH_NAMES = {
    "fire": "Огненное дыхание", "lightning": "Электрическое дыхание",
    "cold": "Холодное дыхание", "acid": "Кислотное дыхание",
    "poison": "Ядовитое дыхание", "sleep": "Усыпляющее дыхание",
    "breath": "Дыхание",
}
_DAMAGE_WORDS = {
    "fire": "огнём", "lightning": "электричеством", "cold": "холодом",
    "acid": "кислотой", "poison": "ядом", "thunder": "звуком",
    "necrotic": "некротической энергией", "radiant": "излучением",
    "force": "силой", "psychic": "психической энергией",
    "bludgeoning": "дробящим", "piercing": "колющим",
    "slashing": "рубящим",
}
_ABILITY_WORDS = {
    "strength": "Силы", "dexterity": "Ловкости",
    "constitution": "Телосложения", "intelligence": "Интеллекта",
    "wisdom": "Мудрости", "charisma": "Харизмы",
}
_MANUAL_BULLETS = {
    "tentacle attack or fling": [
        "По логову кракена проносится сильное течение. Все существа в "
        "пределах 60 футов от кракена должны преуспеть в спасброске Силы "
        "Сл 23, иначе их оттолкнёт на 60 футов от кракена, а при успехе — "
        "на 10 футов.",
        "Существа, находящиеся в воде в пределах 60 футов от кракена, "
        "получают уязвимость к урону электричеством до значения инициативы "
        "«20» следующего раунда.",
        "Вода в логове становится заряженной электричеством. Все существа в "
        "пределах 120 футов от кракена должны преуспеть в спасброске "
        "Телосложения Сл 23, получая 10 (3к6) урона электричеством при "
        "провале, или половину этого урона при успехе.",
    ],
    "vampire weaknesses": [
        "Запрет: вампир не может войти в жилище без приглашения одного из "
        "его обитателей.",
        "Вред от проточной воды: вампир получает 20 урона кислотой, если "
        "оканчивает свой ход в проточной воде.",
        "Кол в сердце: если в сердце вампира, находящегося в покое, вбить "
        "деревянный кол, он парализован, пока кол не вынут.",
        "Гибель на солнце: начиная свой ход под солнечным светом, вампир "
        "получает 20 урона излучением. В солнечном свете он совершает с "
        "помехой броски атаки и проверки характеристик.",
    ],
}


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def slug(text: str) -> str:
    """«Ancient Brass Dragon» -> «ancient-brass-dragon»."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).casefold()).strip("-")


def fetch_monster(slug_name: str) -> dict:
    url = API.format(slug_name)
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def api_features(record: dict) -> dict:
    """Все английские особенности записи API по имени."""
    out = {}
    for key in ("special_abilities", "actions", "reactions",
                "legendary_actions", "lair_actions"):
        for entry in record.get(key) or []:
            name = str(entry.get("name") or "").strip()
            if name:
                out[name.casefold()] = str(entry.get("desc") or "")
    return out


def bullet_lines(desc: str) -> list:
    """Вложенные пункты из английского описания.

    В SRD они записаны двумя способами: списком с дефисом («- Cantrips
    (at will): sacred flame, …») и строками-подписями без дефиса («At
    will: detect thoughts, …», «1/day each: …»). Второй вариант — это тоже
    пункты, и без него у врождённого колдовства список оставался пустым.
    """
    lines = []
    for raw in str(desc or "").splitlines():
        text = raw.strip()
        if text.startswith("- ") or text.startswith("• "):
            lines.append(text[2:].strip())
            continue
        # Строка-подпись: «At will: …», «3/day each: …», «1st level: …».
        if re.match(r"^[A-Z0-9][^:]{0,40}:", text):
            lines.append(text)
    return lines


def translate_label(label: str, slots: str) -> str:
    """Подпись пункта по-русски: «Заговоры (неограниченно)», «1-й уровень»."""
    key = label.strip().casefold()
    base = _LEVEL_LABELS.get(key)
    if base:
        return f"{base} ({slots})" if slots else base
    # «1st level (4 slots)» и любые похожие записи.
    match = re.match(r"(\d+)(?:st|nd|rd|th)\s*level", key)
    if match:
        level = match.group(1)
        text = f"{level}-й уровень"
        return f"{text} ({slots})" if slots else text
    return f"{label} ({slots})" if slots else label


def translate_line(line: str, spells: dict) -> str:
    """Один вложенный пункт: подпись переводим, заклинания — по словарю."""
    text = line.strip()
    match = re.match(r"(?P<label>[^:]+):\s*(?P<rest>.+)$", text)
    if not match:
        return translate_spells(text, spells)
    label = match.group("label").strip()
    rest = match.group("rest").strip()
    slots = ""
    slot_match = re.search(r"\(([^)]*)\)", label)
    if slot_match:
        slots = slot_match.group(1)
        # «(4 slots)» — это ячейки заклинаний: пишем по-русски и сокращённо,
        # иначе в карточке остаётся английское «slots».
        slots = re.sub(r"(\d+)\s*slots?", r"\1 яч.", slots)
        label = label[:slot_match.start()].strip()
    return (f"{translate_label(label, slots)}: "
            f"{translate_spells(rest, spells)}")


def translate_spells(text: str, spells: dict) -> str:
    parts = []
    # Словарь хранит английские имена с заглавных букв («Spare the Dying»),
    # а SRD пишет их со строчной: сравнение без регистра иначе оставляло
    # половину списка по-английски.
    shallow = {str(key).casefold(): value for key, value in spells.items()}
    for chunk in re.split(r",\s*", text):
        name = chunk.strip()
        if not name:
            continue
        parts.append(shallow.get(name.casefold(), name))
    return ", ".join(parts)


def breath_bullets(desc: str) -> list:
    """Пункты дыхания дракона из английской прозы SRD.

    В SRD это два абзаца вида «Fire Breath. The dragon exhales fire in an
    90-foot line that is 10 feet wide. Each creature in that line must make
    a DC 21 Dexterity saving throw, taking 63 (18d6) fire damage…». Нам
    нужны только числа и формы — из них собирается русская строка, которая
    сразу читается за столом и годится для особой атаки.
    """
    out = []
    body = str(desc or "")
    # Каждое дыхание начинается с «Xxx Breath.» или «Xxx Gas.».
    parts = re.split(r"\n(?=[A-Z][A-Za-z ]{2,30}\.\s)", body)
    for part in parts:
        match = re.match(r"(?P<name>[A-Z][A-Za-z ]{2,30})\.\s+(?P<text>.+)",
                         part.strip(), re.S)
        if not match:
            continue
        name = match.group("name").strip()
        text = " ".join(match.group("text").split())
        if "exhales" not in text and "breath" not in text.casefold():
            continue
        base = name.casefold().replace(" breath", "").strip()
        title = _BREATH_NAMES.get(base, name)
        shape_match = re.search(
            r"in (?:an? )?(\d+)-foot (line|cone)(?: that is (\d+) feet wide)?",
            text)
        if shape_match:
            size = shape_match.group(1)
            if shape_match.group(2) == "cone":
                area = f"конусом {size} футов"
            else:
                width = shape_match.group(3) or "5"
                area = (f"линией {size} футов длиной и {width} футов "
                        f"шириной")
        else:
            area = "областью"
        dc_match = re.search(
            r"DC (\d+) (Strength|Dexterity|Constitution|Intelligence|"
            r"Wisdom|Charisma) saving throw", text)
        ability = _ABILITY_WORDS.get(
            (dc_match.group(2).casefold() if dc_match else ""), "")
        damage_match = re.search(
            r"taking (\d+) \((\d+d\d+)\) (\w+) damage", text)
        if dc_match and damage_match:
            word = _DAMAGE_WORDS.get(damage_match.group(3).casefold(), "")
            line = (f"{title}. Дракон выдыхает {area}. Каждое существо в "
                    f"этой области должно совершить спасбросок {ability} "
                    f"Сл {dc_match.group(1)}, получая "
                    f"{damage_match.group(1)} "
                    f"({damage_match.group(2).replace('d', 'к')}) урона "
                    f"{word} при провале, или половину этого урона при "
                    f"успехе.")
        elif dc_match:
            # Дыхание без урона (сон): важно, что происходит при провале.
            minutes = re.search(r"for (\d+) minutes", text)
            effect = (f" иначе потеряет сознание на {minutes.group(1)} минут"
                      if minutes else "")
            line = (f"{title}. Дракон выдыхает {area}. Каждое существо в "
                    f"этой области должно преуспеть в спасброске {ability} "
                    f"Сл {dc_match.group(1)}{effect}.")
        else:
            line = f"{title}. Дракон выдыхает {area}."
        out.append(line)
    return out


def main() -> int:
    monsters = load_json(MONSTERS)
    spells = load_json(SPELL_NAMES)
    changed = 0
    missing = []
    for record in monsters:
        broken = [feature for feature in (record.get("features") or [])
                  if str(feature.get("text") or "").strip().endswith(":")]
        if not broken:
            continue
        english_name = str(record.get("source_name") or record.get("name"))
        manual_only = all(
            str(feature.get("source_name") or "").casefold()
            in _MANUAL_BULLETS for feature in broken)
        # У дыхания драконов нет своих пунктов в API-статблоке — только
        # один абзац, который разбирается отдельно.
        needs_api = any(
            str(feature.get("source_name") or "").casefold()
            not in _MANUAL_BULLETS for feature in broken)
        api_record = {}
        if needs_api:
            try:
                api_record = fetch_monster(slug(english_name))
            except Exception as exc:  # сеть/404 — запись остаётся как была
                missing.append((record.get("name"), str(exc)))
                continue
        features = api_features(api_record)
        for feature in broken:
            english = str(feature.get("source_name") or "").strip()
            desc = features.get(english.casefold()) or ""
            manual = _MANUAL_BULLETS.get(english.casefold())
            if manual:
                bullets = manual
            elif english.casefold() == "breath weapons":
                bullets = breath_bullets(desc)
            else:
                bullets = [translate_line(line, spells)
                           for line in bullet_lines(desc)]
            if not bullets:
                missing.append((record.get("name"), f"{english}: нет списка"))
                continue
            base = str(feature.get("text") or "").strip()
            feature["text"] = base + "\n" + "\n".join(
                f"• {line}" for line in bullets)
            changed += 1
        time.sleep(0.2)
    if changed:
        with open(MONSTERS, "w", encoding="utf-8") as handle:
            json.dump(monsters, handle, ensure_ascii=False)
    print(f"исправлено особенностей: {changed}")
    for name, reason in missing:
        print(f"  пропущено: {name} — {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
