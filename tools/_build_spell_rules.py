"""Сборка правил всех заклинаний SRD из открытого API.

Источник: Open5e, документ `wotc-srd` (SRD 5.1, 319 заклинаний).
Скрипт не входит в приложение: он один раз читает тексты правил и пишет
`shared/spell_srd.py` — таблицу, по которой клиент и сервер считают урон,
спасброски, области и эффекты.

Почему таблица, а не разбор на лету: текст заклинания неизменен, а разбор
регулярками на каждом касте — это разные ответы у клиента и сервера. Один
зафиксированный разбор + ручные поправки в `shared/spells.py` дают
одинаковые числа везде.

Запуск (нужна сеть):
    python tools/_build_spell_rules.py
"""
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "shared" / "spell_srd.py"
API = ("https://api.open5e.com/v1/spells/"
       "?document__slug=wotc-srd&limit=400")

ABILITY = {
    "strength": "str", "dexterity": "dex", "constitution": "con",
    "intelligence": "int", "wisdom": "wis", "charisma": "cha",
}
DAMAGE_TYPES = (
    ("acid", "acid"), ("cold", "cold"), ("fire", "fire"),
    ("lightning", "lightning"), ("thunder", "thunder"), ("poison", "poison"),
    ("necrotic", "necrotic"), ("radiant", "radiant"), ("force", "force"),
    ("psychic", "psychic"), ("bludgeoning", "bludgeoning"),
    ("piercing", "piercing"), ("slashing", "slashing"),
)
CONDITIONS = ("blinded", "charmed", "deafened", "frightened", "grappled",
              "incapacitated", "invisible", "paralyzed", "petrified",
              "poisoned", "prone", "restrained", "stunned", "unconscious")
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10}
DICE_RE = re.compile(r"(\d{1,2})d(\d{1,2})(?:\s*([+-])\s*(\d{1,2}))?")
SHAPE_RE = re.compile(
    r"(\d{1,3})[- ]foot(?:[- ]radius)?[ -]?(sphere|radius|cone|cube|"
    r"cylinder|line|square|hemisphere)", re.IGNORECASE)
ROUNDS_RE = re.compile(r"(\d+)\s*(round|minute|hour|day)", re.IGNORECASE)


def fetch():
    request = urllib.request.Request(
        API, headers={"User-Agent": "Mozilla/5.0 (Berkut VTT rules)"})
    with urllib.request.urlopen(request, timeout=60) as handle:
        return json.load(handle)["results"]


def first_formula(text):
    match = DICE_RE.search(text or "")
    if not match:
        return "", ""
    count, sides, sign, flat = match.groups()
    formula = f"{count}d{sides}"
    if sign and flat:
        formula += f"{sign}{flat}"
    return formula, match.group(0)


def damage_type(text):
    low = (text or "").casefold()
    for key, value in DAMAGE_TYPES:
        if re.search(r"\b" + key + r"\b", low):
            return value
    return ""


def is_healing(desc):
    low = desc.casefold()
    return bool(re.search(r"(regain|restore|regains|restores)[^.]{0,40}"
                          r"hit points", low))


def parse(entry):
    desc = str(entry.get("desc") or "")
    higher = str(entry.get("higher_level") or "")
    low = desc.casefold()

    formula, _matched = first_formula(desc)
    heal = is_healing(desc)
    dtype = damage_type(desc)
    save = ""
    for word, key in ABILITY.items():
        if re.search(r"\b" + word + r"\b saving throw", low):
            save = key
            break
    attack = "spell attack" in low
    auto = bool(re.search(r"(automatically hit|automatically strikes|"
                          r"hit automatically)", low))

    shape, size = "", 0
    area_match = SHAPE_RE.search(desc)
    if area_match:
        size = int(area_match.group(1))
        raw_shape = area_match.group(2).casefold()
        shape = "circle" if raw_shape in ("sphere", "radius", "hemisphere") \
            else raw_shape
        if shape == "square":
            shape = "cube"
    if not shape:
        # SRD пишет и так: «a line 100 feet long», «a 20-foot-radius
        # sphere», «a cone 30 feet». Без этого Молния теряла форму.
        for pattern, shape_group, size_group in (
                (r"\b(sphere|cone|cube|cylinder|line)\b[^.]{0,18}?(\d{1,3})"
                 r"[- ]?feet", 1, 2),
                (r"(\d{1,3})[- ]foot[- ](radius|long|wide|cone|cube|line)",
                 2, 1)):
            match = re.search(pattern, desc, re.IGNORECASE)
            if not match:
                continue
            raw_shape = match.group(shape_group).casefold()
            shape = ("circle" if raw_shape in ("sphere", "radius")
                     else raw_shape)
            size = int(match.group(size_group))
            break
    if shape == "square":
        shape = "cube"

    # Состояние — только когда текст действительно накладывает его:
    # «be paralyzed», «is blinded», «has the frightened condition».
    # Простое упоминание («invisible barrier», «immune to being charmed»)
    # состоянием не является — иначе Щит вешал невидимость, а Сон — испуг.
    conditions = []
    for name in CONDITIONS:
        pattern = (r"(?:(?:is|are|be|becomes?|becoming|has|have|fall|falls) "
                   r"(?:the )?(?:condition )?" + name + r"\b"
                   r"|" + name + r" condition)")
        if re.search(pattern, low):
            conditions.append(name)

    targets = 0
    count_match = re.search(r"up to (\w+) (?:creatures|allies|targets)",
                            low)
    if count_match:
        word = count_match.group(1)
        targets = WORDS.get(word) or (int(word) if word.isdigit() else 0)

    formula_up, per_level = "", ""
    up = re.search(r"increases by (\d+d\d+)(?: ?([+-]\d+)?) for each slot "
                   r"level above (\d)", higher.casefold())
    if up:
        formula_up = up.group(1)
        per_level = str(int(up.group(3)) + 1)

    rounds = 0
    duration = str(entry.get("duration") or "")
    round_match = ROUNDS_RE.search(duration)
    if round_match:
        value = int(round_match.group(1))
        unit = round_match.group(2).casefold()
        rounds = (value * 600 if unit.startswith("hour") else
                  value * 10 if unit.startswith("minute") else
                  value * 14400 if unit.startswith("day") else value)

    casting = str(entry.get("casting_time") or "").casefold()
    if "bonus" in casting:
        cast = "bonus"
    elif "reaction" in casting:
        cast = "reaction"
    elif "minute" in casting or "hour" in casting:
        cast = "long"
    else:
        cast = "action"

    ac_bonus = 0
    ac_base = 0
    for pattern in (
            r"\+(\d{1,2}) bonus to (?:its |your |the target's )?ac",
            r"bonus of \+(\d{1,2}) to (?:its |your )?ac",
            r"\+(\d{1,2}) to (?:its |your |the target's )?ac",
            r"(\d{1,2}) bonus to (?:its |your )?armor class"):
        ac_match = re.search(pattern, low)
        if ac_match:
            ac_bonus = int(ac_match.group(1))
            break
    base_match = re.search(r"(?:ac|armor class) (?:becomes|is|of) (\d{1,2})", low)
    if base_match:
        ac_base = int(base_match.group(1))

    raw_range = str(entry.get("range") or "")
    range_ft = int(entry.get("target_range_sort") or 0)
    if "touch" in raw_range.casefold():
        range_ft = 5

    rule = {
        "n": entry["name"],
        "lvl": int(entry.get("level_int") or 0),
        "school": str(entry.get("school") or "").casefold(),
        "range": range_ft,
        "ac_bonus": ac_bonus,
        "ac_base": ac_base,
        "cast": cast,
        "ritual": bool(entry.get("ritual") in ("yes", True)),
        "conc": bool(entry.get("requires_concentration")),
        "dur": str(entry.get("duration") or ""),
        "rounds": rounds,
        "dice": formula,
        "dtype": dtype,
        "save": save,
        "attack": attack,
        "auto": auto,
        "heal": heal,
        "shape": shape,
        "size": size,
        "targets": targets,
        "conditions": conditions,
        "half": bool(re.search(r"half as much damage|half the damage|"
                               r"takes half", low)),
        "temp": bool(re.search(r"temporary hit points", low)),
        "ac": bool(re.search(r"(bonus|armor class)", low)
                   and re.search(r"armor class", low)),
        "resist": [key for key, _value in DAMAGE_TYPES
                   if re.search(r"resistance to[^.]{0,40}\b" + key, low)],
        "modifier": bool(re.search(r"spellcasting ability modifier", low)),
        "up_dice": formula_up,
        "up_from": per_level,
    }
    if not rule["heal"] and rule["dice"] and rule["dtype"]:
        pass
    elif rule["heal"]:
        rule["dtype"] = ""
    return rule


def main():
    entries = fetch()
    entries.sort(key=lambda item: item["name"])
    rules = {}
    names_ru = {}
    ru_path = ROOT / "library" / "names_spells_ru.json"
    if ru_path.is_file():
        names_ru = json.loads(ru_path.read_text(encoding="utf-8"))

    for entry in entries:
        slug = str(entry.get("slug") or "").strip()
        if not slug:
            continue
        rules[slug] = parse(entry)
        # Названия храним рядом: по ним лист без source_name всё равно
        # найдёт правило, даже если игрок перевёл имя сам.
        rule = rules[slug]
        rule["ru"] = names_ru.get(rule["n"], "")

    problems = [slug for slug, rule in rules.items()
                if not rule["dice"] and rule["save"]]
    with OUT.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write('"""Механика заклинаний SRD 5.1 — сгенерировано '
                     'tools/_build_spell_rules.py.\n\n'
                     'Не правьте вручную: поправки к отдельным '
                     'заклинаниям живут в shared/spells.py.\n"""\n'
                     'import json\n\n'
                     'SPELLS = json.loads(r"""')
        handle.write(json.dumps(rules, ensure_ascii=False, indent=0,
                                sort_keys=True))
        handle.write('""")\n')
    print(f"записано {len(rules)} заклинаний → {OUT}")
    print(f"спасбросок без кубика (проверить вручную): {len(problems)}")
    for slug in problems[:20]:
        print("  ", slug, rules[slug]["n"])


if __name__ == "__main__":
    main()
