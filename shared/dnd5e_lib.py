"""
Библиотека D&D 5e (SRD) из официального дистрибутива Foundry dnd5e.

Дистрибутив dnd5e — открытый код и открытые правила SRD, но его контент
лежит в LevelDB-паках Foundry. Здесь описано, как из архива (zip) или
распакованной папки dnd5e получить обычную библиотеку JSON:

    <корень данных>/library/dnd5e/monsters.json   — монстры (статблоки)
    <корень данных>/library/dnd5e/items.json      — предметы
    <корень данных>/library/dnd5e/spells.json     — заклинания
    <корень данных>/library/dnd5e/art/*.webp      — токены-арты (SRD/FA)
    <корень данных>/library/dnd5e/<тип>/*.webp    — токены по папкам существ
    <корень данных>/library/dnd5e/index.json      — что и откуда импортировано

LevelDB-паки читаются скриптом cli/tools/extract_pack.mjs на Node.js
(там же classic-level — родная привязка LevelDB; чистого Python-чтения
LevelDB нет, поэтому Node используется как «читальщик»).
"""
import html
import json
import os
import re
import unicodedata
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from difflib import get_close_matches
from pathlib import Path

from . import app_paths
from . import combat as combat_rules
from . import dnd5e_loc
from . import token_art

LIBRARY_DIRNAME = os.path.join("library", "dnd5e")
PACKS = ("monsters", "items", "spells")

# ------------------------------------------------------------------
# Локализация коротких кодов dnd5e
# ------------------------------------------------------------------

SIZE_RU = {"tiny": "Крошечный", "sm": "Маленький", "small": "Маленький",
           "med": "Средний", "medium": "Средний", "lg": "Большой",
           "large": "Большой", "huge": "Огромный",
           "grg": "Исполинский", "gargantuan": "Исполинский"}
SIZE_TOKEN_SCALE = {"tiny": 0.5, "sm": 0.75, "small": 0.75,
                    "med": 1.0, "medium": 1.0, "lg": 1.5, "large": 1.5,
                    "huge": 2.0, "grg": 3.0, "gargantuan": 3.0}

SCHOOL_RU = {"abj": "Ограждение", "conj": "Вызов", "div": "Прорицание",
             "ench": "Очарование", "evo": "Воплощение", "ill": "Иллюзия",
             "nec": "Некромантия", "trans": "Преобразование",
             "None": "—"}

DAMAGE_RU = {"acid": "кислота", "bludgeoning": "дробящий",
             "cold": "холод", "fire": "огонь", "force": "сила",
             "lightning": "молния", "necrotic": "некротический",
             "piercing": "колющий", "poison": "яд", "psychic": "psy",
             "radiant": "излучение", "slashing": "рубящий",
             "thunder": "звук"}

ABILITY_RU = {"str": "Сил", "dex": "Лов", "con": "Телос", "int": "Инт",
              "wis": "Мудр", "cha": "Хар"}
ABILITY_FULL_RU = {"str": "Силы", "dex": "Ловкости", "con": "Телосложения",
                   "int": "Интеллекта", "wis": "Мудрости",
                   "cha": "Харизмы"}

SKILL_RU = {"acr": "Акробатика", "ani": "Обращение с животными", "arc": "Магия",
            "ath": "Атлетика", "dec": "Обман", "his": "История",
            "ins": "Проницательность", "itm": "Запугивание",
            "inv": "Расследование", "med": "Медицина", "nat": "Природа",
            "prc": "Восприятие", "prf": "Выступление", "per": "Убеждение",
            "rel": "Религия", "slt": "Ловкость рук", "ste": "Скрытность",
            "sur": "Выживание"}

LANGUAGE_RU = {"aarakocra": "аарококра", "abyssal": "беззвёздная речь",
               "aquan": "акван", "auran": "ауран", "celestial": "небесный",
               "common": "общий", "deep": "глубинный", "draconic": "драконий",
               "dwarvish": "дварфийский", "elvish": "эльфийский",
               "giant": "великанский", "gith": "гит", "gnomish": "гномий",
               "goblin": "гоблинский", "gnoll": "гноллийский",
               "halfling": "полуросликов", "ignan": "игнан",
               "infernal": "инфернальный", "orc": "орочий",
               "primordial": "первородный", "sylvan": "сильвийский",
               "terran": "терран", "undercommon": "подземный"}

ACTIVATION_RU = {"action": "действие", "bonus": "бонусное действие",
                 "reaction": "реакция", "minute": "минута", "hour": "час",
                 "day": "день", "legendary": "легендарное действие",
                 "mythic": "мифическое действие", "lair": "действие в логове",
                 "special": "особое", "turnStart": "начало хода",
                 "turnEnd": "конец хода"}

DURATION_RU = {"inst": "мгновенно", "perm": "постоянно", "disp": "до развеивания",
               "conc": "концентрация", "time": "время", "minute": "минута",
               "hour": "час", "day": "день"}

#: Подтипы существ из паков: "any race", "shapechanger", "devil".
CREATURE_SUBTYPE_RU = {
    "any race": "любая раса", "any": "любой", "race": "раса",
    "shapechanger": "оборотень", "devil": "дьявол", "demon": "демон",
    "human": "человек", "goblinoid": "гоблиноид", "titan": "титан",
    "gnome": "гном", "gnoll": "гнолл", "grimlock": "гримлок",
    "elf": "эльф", "dwarf": "дварф", "kobold": "кобольд",
    "lizardfolk": "ящеролюд", "merfolk": "мерфолк", "orc": "орк",
    "sahuagin": "сахуагин", "custom": "особый",
}

CREATURE_TYPE_RU = {"aberration": "аберрация", "beast": "зверь",
                    "celestial": "небожитель", "construct": "конструкт",
                    "dragon": "дракон", "elemental": "стихия",
                    "fey": "фей", "fiend": "исчадие", "giant": "великан",
                    "humanoid": "гуманоид", "monstrosity": "монстр",
                    "ooze": "жидкотело", "plant": "растение",
                    "undead": "нежить"}

DENOM_RU = {"pp": "плат.", "gp": "зол.", "sp": "сер.", "cp": "мед."}

RARITY_RU = {"common": "обычное", "uncommon": "необычное",
             "rare": "редкое", "veryRare": "очень редкое",
             "legendary": "легендарное", "artifact": "артефакт"}

ITEM_TYPE_RU = {"weapon": "Оружие", "equipment": "Снаряжение",
                "consumable": "Расходник", "tool": "Инструмент",
                "loot": "Добыча", "feat": "Черта", "container": "Контейнер",
                "backpack": "Рюкзак", "base": "Эффект", "Item": "Категория",
                "enchantment": "Зачарование"}

#: Подтипы предметов: коды dnd5e (martialM) и слова (clothing, medium).
ITEM_SUBTYPE_RU = {
    "martialM": "Воинское оружие ближнего боя",
    "simpleM": "Простое оружие ближнего боя",
    "martialR": "Воинское оружие дальнего боя",
    "simpleR": "Простое оружие дальнего боя",
    "clothing": "Одежда", "light": "Лёгкий доспех",
    "medium": "Средний доспех", "heavy": "Тяжёлый доспех",
    "art": "Инструменты ремесленника", "ammo": "Боеприпасы",
    "music": "Музыкальный инструмент", "wondrous": "Чудесный предмет",
    "game": "Игровой набор", "trinket": "Безделушка",
    "wand": "Жезл", "rod": "Стержень", "ring": "Кольцо",
    "potion": "Зелье", "scroll": "Свиток", "poison": "Яд",
    "food": "Еда", "shield": "Щит", "focus": "Фокусировка заклинаний",
}

#: Настройка: в dnd5e это "required"/"optional" либо пусто.
ATTUNEMENT_RU = {"required": "требуется настройка",
                 "optional": "по желанию"}
ITEM_PROPERTY_RU = {
    "amm": "боеприпас", "fin": "фехтовальное", "fir": "огнестрельное",
    "foc": "фокусировка", "hvy": "тяжёлое", "lgt": "лёгкое",
    "lod": "перезарядка", "mgc": "магическое", "rch": "досягаемость",
    "rel": "особое", "ret": "метательное", "sil": "серебряное",
    "spc": "особое", "thr": "метательное", "two": "двуручное",
    "ver": "универсальное", "ada": "адаптивное", "bur": "взрывное",
    "def": "защитное", "dex": "ловкое", "ena": "энергетическое",
    "gra": "захват", "hnd": "ручное", "imp": "импровизированное",
    "recharge": "перезарядка", "special": "особое",
    # Логические свойства dnd5e: приходят как отдельные ключи, а не коды.
    "stealthDisadvantage": "помеха на Скрытность",
    "stealth_disadvantage": "помеха на Скрытность",
    "magical": "магическое", "mgc": "магическое",
    "attunement": "требует настройки", "attuned": "настроено",
    "weightlessContents": "невесомое содержимое",
}

# Названия и отдельные игровые термины из SRD сопоставлены с русской
# терминологией dnd.su. Исходный ключ сохраняется для поиска, а в интерфейсе
# показывается русское имя. Неизвестные homebrew-имена остаются нетронутыми.
NAME_RU = {
    "aboleth": "Аболет", "acid arrow": "Кислотная стрела",
    "chuul": "Чууль", "cloaker": "Плащевик",
    "gibbering mouther": "Бормочущий ротовик", "otyugh": "Отидж",
    "alchemy jug": "Алхимический сосуд", "animated armor": "Оживлённые доспехи",
    "arcane eye": "Магический глаз", "armor of agathys": "Доспех Агатиса",
    "awaken": "Пробуждение", "barkskin": "Дубовая кора",
    "black pudding": "Чёрный пудинг", "blink": "Мерцание",
    "burning hands": "Пылающие ладони", "chain lightning": "Цепная молния",
    "charm person": "Очарование личности", "cone of cold": "Конус холода",
    "counterspell": "Контрзаклинание", "cure wounds": "Лечение ран",
    "dancing lights": "Пляшущие огоньки", "darkness": "Тьма",
    "detect magic": "Обнаружение магии", "dispel magic": "Рассеивание магии",
    "fireball": "Огненный шар", "fire bolt": "Огненный снаряд",
    "goblin": "Гоблин", "grease": "Жир",
    "healing word": "Лечащее слово", "invisibility": "Невидимость",
    "mage armor": "Доспехи мага", "magic missile": "Волшебная стрела",
    "misty step": "Туманный шаг", "poisoned dagger": "Отравленный кинжал",
    "shield": "Щит", "sleep": "Усыпление",
    "spiritual weapon": "Духовное оружие", "thunderwave": "Громовая волна",
    "web": "Паутина", "witch bolt": "Ведьмин снаряд",
}

ALIGNMENT_RU = {
    "lawful good": "законно-добрый", "neutral good": "нейтрально-добрый",
    "chaotic good": "хаотично-добрый", "lawful neutral": "законно-нейтральный",
    "neutral": "нейтральный", "true neutral": "нейтральный",
    "chaotic neutral": "хаотично-нейтральный", "lawful evil": "законно-злой",
    "neutral evil": "нейтрально-злой", "chaotic evil": "хаотично-злой",
    "unaligned": "без мировоззрения",
    # Свободные формулировки из паков: «Any Good», «Any non-lawful».
    "any": "любое", "any alignment": "любое мировоззрение",
    "any good": "любое доброе", "any evil": "любое злое",
    "any evil alignment": "любое злое",
    "any chaotic": "любое хаотичное",
    "any lawful": "любое законное",
    "any neutral": "любое нейтральное",
    "any non-good": "любое недоброе",
    "any non-good alignment": "любое недоброе",
    "any non-lawful": "любое незаконное",
    "any non-lawful alignment": "любое незаконное",
    "any non-evil": "любое незлое",
}

SENSE_RU = {"darkvision": "тёмное зрение", "blindsight": "слепое зрение",
            "tremorsense": "чувство вибрации", "truesight": "истинное зрение"}

# Среды обитания из details.environment: SRD использует фиксированный набор,
# неизвестные (homebrew) значения остаются как есть.
ENVIRONMENT_RU = {
    "any": "любая", "arctic": "арктика", "coastal": "побережье",
    "desert": "пустыня", "forest": "лес", "grassland": "равнины",
    "hill": "холмы", "jungle": "джунгли", "mountain": "горы",
    "ocean": "океан", "plains": "равнины", "shadowfell": "полутень",
    "underdark": "подземье", "urban": "городская местность",
    "swamp": "болото", "planar": "планарная область",
    "underwater": "под водой",
}

#: Готовые формулировки языков из паков: переводим целыми фразами.
#  Иначе в карточке оставалось «understands Common but can't speak».
LANGUAGE_PHRASE_RU = {
    "any one language (usually common)": "любой один язык (обычно общий)",
    "any one language (usually common.)": "любой один язык (обычно общий)",
    "any one language": "любой один язык",
    "any two languages": "любые два языка",
    "any two": "любые два языка",
    "any four languages": "любые четыре языка",
    "5 other languages": "пять других языков",
    "any 6 languages": "любые шесть языков",
    "(any 6 languages)": "(любые шесть языков)",
    "all": "все",
    "custom": "особый",
    "telepathy 120 ft.": "телепатия 120 фт",
    "telepathy 60 ft. (works only with creatures that understand abyssal)":
        "телепатия 60 фт (только с существами, понимающими беззвёздную речь)",
    "the languages it knew in life": "языки, которые оно знало при жизни",
    "languages it knew in life": "языки, которые оно знало при жизни",
    "any languages it knew in life": "любые языки, которые оно знало при жизни",
    "understand all languages it knew in life but can't speak":
        "понимает все языки, которые знало при жизни, но не говорит",
    "understands all languages it knew in life but can't speak":
        "понимает все языки, которые знало при жизни, но не говорит",
    "understands the languages it knew in life but can't speak":
        "понимает языки, которые знало при жизни, но не говорит",
    "understands the languages you know": "понимает известные вам языки",
    "understands the languages of its creator but can't speak":
        "понимает языки своего создателя, но не говорит",
    "understands the languages of its creator but cannot speak":
        "понимает языки своего создателя, но не говорит",
    "one language known by its creator": "один язык, известный его создателю",
    "all languages known to its summoner": "все языки, известные призвавшему",
    "understands common but can't speak": "понимает общий, но не говорит",
    "understands common but doesn't speak it": "понимает общий, но не говорит",
    "understands common but can’t speak": "понимает общий, но не говорит",
    "understands infernal but can't speak it":
        "понимает инфернальный, но не говорит на нём",
    "understands abyssal, common, and infernal but can't speak":
        "понимает беззвёздную речь, общий и инфернальный, но не говорит",
    "understands abyssal, celestial, infernal, and primordial but can't speak":
        "понимает беззвёздную речь, небесный, инфернальный и первородный, но не говорит",
    "understands celestial, common, elvish, and sylvan but can't speak":
        "понимает небесный, общий, эльфийский и сильвийский, но не говорит",
    "understands common and giant but can't speak":
        "понимает общий и великанский, но не говорит",
    "understands draconic but can't speak":
        "понимает драконий, но не говорит",
    "understands deep speech but can't speak":
        "понимает глубокую речь, но не говорит",
    "understands but can't speak": "понимает, но не говорит",
    "understands commands given in any language but can't speak":
        "понимает приказы на любом языке, но не говорит",
    "cannot speak": "не говорит",
    "can't speak": "не говорит",
    "cant": "не говорит",
    "(can't speak in boar form)": "(не говорит в форме кабана)",
    "(can't speak in wolf form)": "(не говорит в форме волка)",
    "(can't speak in rat form)": "(не говорит в форме крысы)",
    "(can't speak in bear form)": "(не говорит в форме медведя)",
    "(can't speak in tiger form)": "(не говорит в форме тигра)",
    "sphinx": "сфинксов", "worg": "воргов", "otyugh": "отиджей",
    "sahuagin": "сахуагин", "winter wolf": "зимних волков",
    "giant owl; understands common, elvish, and sylvan but can't speak them":
        "гигантская сова; понимает общий, эльфийский и сильвийский, но не говорит на них",
    "giant elk; understands common, elvish, and sylvan but can't speak them":
        "гигантский лось; понимает общий, эльфийский и сильвийский, но не говорит на них",
    "giant eagle; common and auran (understands but cannot speak)":
        "гигантский орёл; общий и ауран (понимает, но не говорит)",
    "blink dog;understands sylvan but can't speak it":
        "блинк-пёс; понимает сильвийский, но не говорит на нём",
}


def normalize_language(value) -> str:
    """Один язык из пака -> русское название.

    Значения приходят и ключами Foundry («undercommon»), и готовыми
    фразами («understands Common but can't speak»). Неизвестное оставляем
    как есть: homebrew-запись не должна терять смысл.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    folded = text.casefold().replace("ё", "е")
    phrase = LANGUAGE_PHRASE_RU.get(folded)
    if phrase:
        return phrase
    word = re.sub(r"\([^)]*\)", "", folded).strip()
    if word in LANGUAGE_RU:
        return LANGUAGE_RU[word]
    for part in re.split(r"[,;]", word):
        part = part.strip()
        if part and part not in LANGUAGE_RU:
            break
    else:
        return ", ".join(LANGUAGE_RU[p.strip()]
                        for p in re.split(r"[,;]", word) if p.strip())
    return translate_description(text)


def alignment_text(value) -> str:
    """Мировоззрение из пака -> русское, с учётом «Any Good» и подобных."""
    text = str(value or "").strip()
    if not text:
        return ""
    folded = text.casefold().replace("ё", "е").strip()
    folded = re.sub(r"\s+alignment$", "", folded)
    translated = ALIGNMENT_RU.get(folded)
    if translated:
        return translated
    if "50%" in folded:
        return "нейтрально-добрый или нейтрально-злой (по 50%)"
    return text

CONDITION_RU = {
    "blinded": "ослеплённый", "charmed": "очарованный",
    "deafened": "оглохший", "exhaustion": "истощение",
    "frightened": "испуганный", "grappled": "схваченный",
    "incapacitated": "недееспособный", "invisible": "невидимый",
    "paralyzed": "парализованный", "petrified": "окаменевший",
    "poisoned": "отравленный", "prone": "сбитый с ног",
    "restrained": "опутанный", "stunned": "ошеломлённый",
    "unconscious": "без сознания",
}

SPELL_CLASS_RU = {
    "artificer": "изобретатель", "bard": "бард", "cleric": "жрец",
    "druid": "друид", "paladin": "паладин", "ranger": "следопыт",
    "sorcerer": "чародей", "warlock": "колдун", "wizard": "волшебник",
}

LANGUAGE_RU.update({"telepathy": "телепатия", "all": "все языки",
                    "any": "любой язык", "druidic": "друидический",
                    "thievescant": "воровской жаргон", "deep speech": "глубинная речь",
                    "undercommon": "подземный"})

SCHOOL_RU.update({"abj": "Ограждение", "conj": "Вызов", "evo": "Воплощение",
                  "trans": "Преобразование"})
DAMAGE_RU.update({"psychic": "психический", "thunder": "громовой",
                  "healing": "лечение"})

# Периоды восстановления зарядов предметов (system.uses.recovery[].period)
USES_RECOVERY_RU = {
    "sr": "после короткого отдыха", "lr": "после длинного отдыха",
    "day": "раз в день", "dawn": "на рассвете", "dusk": "в сумерках",
    "charge": "заряд", "recharge": "перезарядка",
}

CR_XP = {0: 10, 0.125: 25, 0.25: 50, 0.5: 100, 1: 200, 2: 450, 3: 700,
         4: 1100, 5: 1800, 6: 2300, 7: 2900, 8: 3900, 9: 5000, 10: 5900,
         11: 7200, 12: 8400, 13: 10000, 14: 11500, 15: 13000, 16: 15000,
         17: 18000, 18: 20000, 19: 22000, 20: 25000, 21: 33000, 22: 41000,
         23: 50000, 24: 62000, 25: 75000, 26: 90000, 27: 105000,
         28: 120000, 29: 135000, 30: 155000}


# ------------------------------------------------------------------
# Мелкие помощники
# ------------------------------------------------------------------

def ability_mod(value) -> int:
    """Модификатор характеристики из значения 3..30."""
    try:
        return (int(value) - 10) // 2
    except (TypeError, ValueError):
        return 0


def prof_bonus_from_cr(cr) -> int:
    """Бонус мастерства по Challenge Rating (правило 5e)."""
    try:
        cr = float(cr)
    except (TypeError, ValueError):
        return 2
    if cr <= 0:
        return 2
    if cr >= 29:
        return 9
    if cr >= 25:
        return 8
    if cr >= 21:
        return 7
    if cr >= 17:
        return 6
    if cr >= 9:
        return 4
    if cr >= 5:
        return 3
    return 2


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")

# ------------------------------------------------------------------
# Обогащения Foundry -> читаемый текст
# ------------------------------------------------------------------
# В описаниях dnd5e лежат «обогащения» Foundry: [[/item .id]]{claws},
# [[/save activity=...]], [[lookup @name lowercase]], @UUID[...]{метка},
# &Reference[...]. Foundry подставляет их при отображении (ссылки, броски,
# значения полей), у нас это мусор. Здесь каждый вид превращается в такой же
# читаемый текст, какой показал бы Foundry, а то, что восстановить не из чего,
# просто убирается — фраза при этом остаётся осмысленной.

_ENRICH_LOOKUP_RE = re.compile(r"\[\[lookup\s+([^\]]+)\]\](?:\{([^}]*)\})?")
_ENRICH_INLINE_RE = re.compile(r"\[\[/([a-z]+)\s*([^\]]*)\]\](?:\{([^}]*)\})?")
_ENRICH_DOC_RE = re.compile(r"@[A-Za-z]+\[[^\]]*\](?:\{([^}]*)\})?")
_ENRICH_REF_RE = re.compile(r"&[Rr]eference\[([^\]]*)\](?:\{([^}]*)\})?")
_ENRICH_ROLL_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
# В реальных паках встречается битая запись «[[lookup @x]}{метка}» —
# закрывающая квадратная скобка заменена на фигурную.
_ENRICH_BROKEN_RE = re.compile(
    r"\[\[(lookup|/[a-z]+)\s*([^\]\}]*)\]?\}(?:\{([^}]*)\})?")

_EN_ABILITY = {"str": "Strength", "dex": "Dexterity", "con": "Constitution",
               "int": "Intelligence", "wis": "Wisdom", "cha": "Charisma"}
_EN_TEMPLATE = {"cone": "cone", "cube": "cube", "cylinder": "cylinder",
                "line": "line", "radius": "radius", "sphere": "sphere",
                "wall": "wall"}
_EN_DURATION = {"minute": "minute", "min": "minute", "hour": "hour",
                "day": "day", "round": "round", "instant": "instant"}


def _ctx_activities(ctx: dict) -> dict:
    acts = (ctx or {}).get("activities") or {}
    return acts if isinstance(acts, dict) else {}


def _ctx_activity(ctx: dict, activity_id):
    if not activity_id:
        return None
    act = _ctx_activities(ctx).get(activity_id)
    return act if isinstance(act, dict) else None


def _ctx_first_activity(ctx: dict, kind: str):
    for act in _ctx_activities(ctx).values():
        if isinstance(act, dict) and act.get("type") == kind:
            return act
    return None


def _dc_value(raw):
    """Сл спасброска/проверки: число либо {value, calculation|macro}."""
    if isinstance(raw, dict):
        raw = raw.get("value")
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None    # «@attributes.spel.dc», "@mod" и прочие выражения
    return value


def _activity_range_label(act) -> str:
    rng = (act or {}).get("range") or {}
    if not isinstance(rng, dict):
        return ""
    if rng.get("special"):
        return str(rng["special"])
    value = rng.get("value")
    if value in (None, ""):
        return ""
    units = rng.get("units") or "ft"
    unit = "feet" if units == "ft" else units
    return f"{value} {unit}"


def _activity_template_label(act) -> str:
    target = (act or {}).get("target") or {}
    tpl = target.get("template") if isinstance(target, dict) else None
    if not isinstance(tpl, dict):
        return ""
    kind = _EN_TEMPLATE.get(tpl.get("type"))
    size = tpl.get("size")
    if kind and size not in (None, ""):
        return f"{size}-foot {kind}"
    return ""


def _activity_duration_label(act) -> str:
    dur = (act or {}).get("duration") or {}
    if not isinstance(dur, dict):
        return ""
    units = _EN_DURATION.get(dur.get("units") or "", "")
    value = dur.get("value")
    if value not in (None, "") and units:
        plural = "s" if str(value) != "1" and units == "minute" else ""
        return f"{value} {units}{plural}"
    return ""


def _apply_style(value, style) -> str:
    if style == "lowercase":
        return value.lower()
    if style == "uppercase":
        return value.upper()
    if style == "capitalize":
        return value[:1].upper() + value[1:]
    return value


def _resolve_lookup(config: str, fallback, ctx: dict) -> str:
    path = None
    style = None
    activity_id = None
    for token in config.split():
        if token.startswith("activity="):
            activity_id = token.split("=", 1)[1]
        elif token.startswith("@"):
            path = token
        elif token in ("lowercase", "uppercase", "capitalize"):
            style = token
    act = _ctx_activity(ctx, activity_id)
    value = None
    if path in ("@name", "@item.name"):
        value = (ctx or {}).get("name")
    elif path == "@details.type.value":
        value = (ctx or {}).get("ctype")
    elif path == "@resources.legact.label":
        value = "Legendary Actions"
    elif path == "@resources.legres.label":
        value = "Legendary Resistances"
    elif path == "@labels.description.range":
        value = _activity_range_label(act)
    elif path == "@labels.description.template":
        value = _activity_template_label(act)
    elif path == "@labels.duration":
        value = _activity_duration_label(act)
    elif path == "@item.uses.max":
        value = (ctx or {}).get("uses_max")
    if value in (None, ""):
        value = fallback
    if value in (None, ""):
        return ""
    return _apply_style(str(value), style)


def _attack_bonus(act, ctx: dict):
    """Бонус к попаданию из атакующей активности: фиксированный или от характеристик."""
    atk = (act or {}).get("attack") or {}
    bonus = str(atk.get("bonus") or "").strip()
    try:
        return int(bonus)
    except (TypeError, ValueError):
        pass
    mods = (ctx or {}).get("mods")
    pb = (ctx or {}).get("pb")
    ability = atk.get("ability")
    if mods is not None and ability in mods:
        return int(mods[ability]) + int(pb or 0)
    return None


def _attack_label(act, ctx: dict, extended: bool) -> str:
    bonus = _attack_bonus(act, ctx)
    if bonus is None:
        return ""
    if not extended:
        return f"+{bonus}"
    atk = (act or {}).get("attack") or {}
    classification = ((atk.get("type") or {}) if isinstance(atk.get("type"), dict)
                      else {}).get("classification") or ""
    value = ((atk.get("type") or {}) if isinstance(atk.get("type"), dict)
             else {}).get("value") or ""
    kind = value or "weapon"
    side = "Melee" if (classification == "melee"
                        or (not classification and atk.get("ability") != "dex")) \
        else "Ranged"
    head = f"{side} {kind.title()} Attack"
    rng = _activity_range_label(act)
    if rng:
        rng = f"reach {rng}" if side == "Melee" else rng
    parts = [f"+{bonus}"] + ([rng] if rng else [])
    return f"{head}: {', '.join(parts)}"


def _average_formula(formula: str):
    m = re.fullmatch(r"(\d+)d(\d+)([+-]\d+)?", formula.strip())
    if not m:
        return None
    count, sides = int(m.group(1)), int(m.group(2))
    bonus = int(m.group(3) or 0)
    return (count * (sides + 1)) // 2 + bonus


def _part_formula(part: dict, act, ctx: dict):
    """Часть урона/лечения -> (формула, типы) либо None, если собрать не из чего."""
    if not isinstance(part, dict):
        return None
    custom = (part.get("custom") or {}) if isinstance(part.get("custom"), dict) else {}
    formula = str(custom.get("formula") or part.get("formula") or "").strip()
    if not formula:
        try:
            count = int(part.get("number") or 0)
            sides = int(part.get("denomination") or 0)
        except (TypeError, ValueError):
            return None
        if not (count and sides):
            return None
        formula = f"{count}d{sides}"
        bonus = part.get("bonus")
        if bonus not in (None, ""):
            try:
                formula += f"{int(bonus):+d}"
            except (TypeError, ValueError):
                # @mod и прочие выражения: подставляем модификатор
                # характеристики активности, если он у нас есть.
                if str(bonus).strip() == "@mod":
                    ability = ((act or {}).get("save") or {}).get("ability")
                    if isinstance(ability, list):
                        ability = ability[0] if ability else None
                    ability = ability or ((act or {}).get("attack") or {}).get("ability")
                    mods = (ctx or {}).get("mods")
                    if mods and ability in mods:
                        formula += f"{int(mods[ability]):+d}"
    types = part.get("types") or []
    if isinstance(types, str):
        types = [types]
    return formula, [str(t) for t in types if t]


def _damage_text(kind: str, act, tokens: list, ctx: dict, label) -> str:
    """[[/damage ...]] / [[/heal ...]] -> «22 (4d8) fire damage» как в Foundry."""
    if label:
        return label
    extended = "extended" in tokens
    want_average = "average" in tokens or extended
    formulas = [t for t in tokens if re.fullmatch(r"\d*d\d+[+-]\d*", t)]
    type_tokens = [t for t in tokens
                   if re.fullmatch(r"[a-z][a-z/]*", t)
                   and t not in ("average", "extended", "temp")]

    entries = []
    if formulas:
        for formula in formulas:
            entries.append((formula, type_tokens))
    else:
        acts = act
        if acts is None:
            want = "heal" if kind in ("heal", "healing") else "damage"
            acts = _ctx_first_activity(ctx, want)
        system_key = "healing" if kind in ("heal", "healing") else "damage"
        block = (acts or {}).get(system_key) or (acts or {}).get("heal") or {}
        parts = block.get("parts") if isinstance(block, dict) else None
        if not parts and isinstance(block, dict):
            parts = [block]
        for part in parts or []:
            parsed = _part_formula(part, acts, ctx)
            if parsed:
                entries.append(parsed)
        if not entries and kind not in ("heal", "healing"):
            # Базовый урон предмета (damage.base) — там лежит урон атаки
            # монстров, когда активность оставлена без частей.
            base = (ctx.get("damage_base") or {}) if isinstance(ctx, dict) else {}
            if base.get("denomination"):
                parsed = _part_formula(base, acts, ctx)
                if parsed:
                    entries.append(parsed)
    if not entries:
        return ""

    rendered = []
    for formula, types in entries:
        type_text = " ".join(types)
        shown = formula
        if want_average:
            average = _average_formula(formula)
            if average is not None:
                shown = f"{average} ({formula})"
        text = f"{shown} {type_text}".strip()
        rendered.append(text)
    result = ", ".join(rendered)
    if extended:
        result = f"Hit: {result} damage"
    return result


def _resolve_inline(kind: str, args: str, label, ctx: dict) -> str:
    if label:
        return label
    activity_id = None
    tokens = []
    for token in args.split():
        if token.startswith("activity="):
            activity_id = token.split("=", 1)[1]
        elif token:
            tokens.append(token)
    act = _ctx_activity(ctx, activity_id)

    if kind == "item":
        return ""
    if kind == "attack":
        if act is None:
            act = _ctx_first_activity(ctx, "attack")
        return _attack_label(act, ctx, "extended" in tokens)
    if kind == "save":
        if act is None:
            act = _ctx_first_activity(ctx, "save")
        save = (act or {}).get("save") or {}
        abilities = save.get("ability")
        if isinstance(abilities, str):
            abilities = [abilities]
        dc = _dc_value(save.get("dc"))
        names = " or ".join(_EN_ABILITY.get(a, a) for a in (abilities or []) if a)
        if dc and names:
            return f"DC {dc} {names}"
        return f"DC {dc}" if dc else names
    if kind == "check":
        if act is None:
            act = _ctx_first_activity(ctx, "check")
        check = (act or {}).get("check") or {}
        ability = _EN_ABILITY.get(check.get("ability"), "")
        dc = _dc_value(check.get("dc"))
        if dc and ability:
            return f"DC {dc} {ability}"
        return f"DC {dc}" if dc else ability
    if kind in ("damage", "heal", "healing"):
        return _damage_text(kind, act, tokens, ctx, None)
    return ""    # неизвестная команда — убираем, фраза остаётся связной


def resolve_enrichments(text: str, ctx: dict = None) -> str:
    """[[...]]/&Reference/@UUID из Foundry -> читаемый текст (или пустота).

    ctx — {'name': имя существа/предмета, 'ctype': тип существа,
    'activities': activities документа, 'uses_max': предел зарядов,
    'mods': модификаторы характеристик, 'pb': бонус мастерства}: по ним
    восстанавливаются значения, которые Foundry подставляет из данных.
    """
    if not text or ("[[" not in text and "@" not in text and "eference[" not in text):
        return text or ""
    ctx = ctx or {}

    text = _ENRICH_LOOKUP_RE.sub(
        lambda m: _resolve_lookup(m.group(1), m.group(2), ctx), text)
    text = _ENRICH_INLINE_RE.sub(
        lambda m: _resolve_inline(m.group(1), m.group(2), m.group(3), ctx), text)
    text = _ENRICH_DOC_RE.sub(lambda m: m.group(1) or "", text)

    def _reference(match):
        if match.group(2):
            return match.group(2)
        expression = match.group(1).strip()
        # «condition=unconscious» или «incapacitated apply=false»
        first = expression.split()[0] if expression else ""
        return first.split("=", 1)[-1]
    text = _ENRICH_REF_RE.sub(_reference, text)
    text = _ENRICH_BROKEN_RE.sub(
        lambda m: (_resolve_lookup(m.group(1) + " " + m.group(2), m.group(3), ctx)
                   if m.group(1) == "lookup"
                   else _resolve_inline(m.group(1).lstrip("/"), m.group(2),
                                        m.group(3), ctx)),
        text)

    # Остатки: обычные броски [[3d6]] -> «3d6», нераскрытые команды уходят.
    text = _ENRICH_ROLL_RE.sub(
        lambda m: "" if m.group(1).lstrip().startswith("/") else m.group(1).strip(),
        text)

    # Убираем следы удалённого: «в  .», «а , », висячие предлоги и скобки.
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"\b(?:a|an|the)\s+(?=[,.;:!?])", "", text)
    text = re.sub(r"\b(?:in|of|to|on|at|by|from|within)\s+(?=[,.;:!?])", "", text)
    # Строки, целиком состоящие из точек (бывшие [[/attack extended]]), уходят.
    text = re.sub(r"(?m)^\s*(?:\.\s*)+$", "", text)
    text = re.sub(r"(?<!\.)\.\.(?!\.)", ".", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def strip_html(fragment: str, ctx: dict = None) -> str:
    """HTML-описание -> читаемый чистый текст для карточки в чате."""
    if not fragment:
        return ""
    text = re.sub(r"</p\s*>", "\n", str(fragment), flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = resolve_enrichments(text, ctx)
    text = "\n".join(_WS_RE.sub(" ", line).strip()
                     for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def damage_formula(part: dict) -> str:
    """Часть урона dnd5e {number,denomination,bonus,types} -> «1d8+4 руб.»"""
    try:
        n = int(part.get("number") or 0)
        sides = int(part.get("denomination") or 0)
    except (TypeError, ValueError):
        n, sides = 0, 0
    formula = f"{n}d{sides}" if n and sides else ""
    bonus = part.get("bonus")
    if bonus not in (None, ""):
        try:
            formula += f"{int(bonus):+d}"
        except (TypeError, ValueError):
            pass
    types = part.get("types") or []
    if isinstance(types, str):
        types = [types]
    names = ", ".join(DAMAGE_RU.get(t, t) for t in types if t)
    return f"{formula} {names}".strip()


def format_modifier(mod: int) -> str:
    return f"{mod:+d}"


# ------------------------------------------------------------------
# Словари названий из паков Foundry
# ------------------------------------------------------------------
# library/names_*.json — полные списки: заклинания 321, предметы 829.
# Это отдельные файлы, а не код: их удобно править и дополнять.
_LOC_NAMES = None


def _library_locale_dir() -> str:
    """Папка ``library`` с JSON-переводами.

    В исходниках это корень проекта, в собранном onedir — папка рядом с
    ``Berkut VTT.exe``. Раньше путь считался только от ``shared/__file__``,
    поэтому внутри ``.exe`` он указывал в ``_internal/library``, а реальная
    папка лежала рядом с exe: 90% названий оставались латиницей.
    """
    candidates = [
        os.path.join(app_paths.app_dir(), "library"),
        os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "library"),
        os.path.join(os.getcwd(), "library"),
    ]
    for folder in candidates:
        if any(os.path.isfile(os.path.join(folder, name))
               for name in ("names_monsters_ru.json",
                            "names_items_ru.json",
                            "names_spells_ru.json",
                            "descriptions_ru.json")):
            return folder
    return candidates[0]


def _load_library_names() -> dict:
    """Названия из library/names_*.json -> словарь {нормализованное: перевод}.

    Файлы лежат рядом с приложением, поэтому путь считаем от папки
    приложения, а не от текущего каталога.
    """
    global _LOC_NAMES
    if _LOC_NAMES is not None:
        return _LOC_NAMES
    out = {}
    folder = _library_locale_dir()
    for fname in ("names_spells_ru.json", "names_items_ru.json",
                  "names_monsters_ru.json"):
        try:
            with open(os.path.join(folder, fname), encoding="utf-8") as f:
                for en, ru_text in json.load(f).items():
                    out[unicodedata.normalize("NFKC", en).strip().casefold()] = ru_text
        except (OSError, ValueError):
            continue
    _LOC_NAMES = out
    return out


_LOC_DESCRIPTIONS = None


def _load_library_descriptions() -> dict:
    """Русские описания заклинаний и предметов с dnd.su.

    library/descriptions_ru.json: 315 заклинаний и 195 магических предметов с
    полными правиловыми текстами. Ключ — русское название, поэтому ищем по
    переводу, а не по английскому оригиналу.
    """
    global _LOC_DESCRIPTIONS
    if _LOC_DESCRIPTIONS is not None:
        return _LOC_DESCRIPTIONS
    out = {}
    folder = _library_locale_dir()
    try:
        with open(os.path.join(folder, "descriptions_ru.json"),
                  encoding="utf-8") as f:
            data = json.load(f)
        for group in ("spells", "items"):
            for ru_name, text in (data.get(group) or {}).items():
                out[ru_name.casefold()] = text
    except (OSError, ValueError):
        pass
    _LOC_DESCRIPTIONS = out
    return out


def translated_description(name: str) -> str:
    """Готовое русское описание по названию либо пустая строка.

    Ищем сначала по переведённому имени, потом по оригиналу: карточка может
    прийти как из пака Foundry (английское имя), так и уже переведённой.
    """
    table = _load_library_descriptions()
    if not table:
        return ""
    for key in (translate_name(name), name):
        if not key:
            continue
        text = table.get(str(key).casefold())
        if text:
            return text
    return ""


def translate_name(name: str) -> str:
    """Перевести английское имя SRD на русский терминологией dnd.su.

    Основной источник всех официальных локализованных имён — переведённый
    Compendium dnd5e. Таблица здесь покрывает частые имена, а fuzzy fallback
    позволяет продолжить отображать русское название даже в другой редакции
    набора, сохраняя исходное имя для поиска и данных.
    """
    if not name:
        return "?"
    original = str(name)
    normalized = unicodedata.normalize("NFKC", original).strip().casefold()

    # 1. Точное совпадение в ручном словаре (курируемые названия).
    translated = NAME_RU.get(normalized)
    if translated:
        return _match_case(original, translated)

    # 2. Полные словари заклинаний и предметов из паков Foundry.
    #    Идут ПЕРЕД общим словарём терминов: там «haste» переведено как
    #    «спешка», а в справочнике заклинание называется «Ускорение».
    #    Специальный словарь точнее общего, поэтому его приоритет выше.
    translated = _load_library_names().get(normalized)
    if translated:
        return translated

    # 3. Общий словарь терминов с dnd.su: 636 записей.
    #    Именно здесь лежат «claw», «bite», «multiattack» — без этого шага
    #    почти все атаки и черты оставались на английском.
    translated = dnd5e_loc.title(normalized)
    if translated:
        return _match_case(original, translated)

    # 4. Монстры: 638 записей с dnd.su.
    translated = dnd5e_loc.monster_name(normalized)
    if translated:
        return _match_case(original, translated)

    # 5. Варианты «Оружие +1/+2/+3»: базу переводим, суффикс оставляем.
    #    В словаре лежит только база — иначе пришлось бы держать 700 копий.
    match = re.match(r"^(.*?)\s*\+([0-9]+)$", normalized)
    if match:
        base = _load_library_names().get(match.group(1).strip())
        if not base:
            base = dnd5e_loc.title(match.group(1).strip())
        if base:
            return f"{_match_case(original, base)} +{match.group(2)}"

    # 6. Шаблоны составных названий предметов.
    #    В паках 119 таких вариантов («Vicious Longsword», «Belt of Fire Giant
    #    Strength»): держать их в словаре по одному незачем — базовые слова
    #    уже переведены, остаётся подставить их в шаблон.
    def _base(text):
        key = text.strip()
        return _load_library_names().get(key) or dnd5e_loc.title(key)

    def _agree(adjective, noun):
        """Согласовать прилагательное с русским существительным по роду.

        «Танцующий меч», но «танцующий» + «рапира» звучит неверно — нужен
        «танцующая рапира». Род определяем по окончанию перевода.
        """
        word = (noun or "").lower()
        if word.endswith(("а", "я")):
            return adjective[:-2] + "ая"
        if word.endswith(("о", "е")):
            return adjective[:-2] + "ее"
        return adjective

    def _restore_armor(text):
        """Вернуть слово «armor», которое срезал шаблон «X Armor of Resistance».

        Шаблон съедает «armor» и в базе остаётся «leather»; словарь знает только
        «leather armor». Если без «armor» перевода нет — пробуем с ним.
        """
        key = text.strip()
        if _load_library_names().get(key) or dnd5e_loc.title(key):
            return key
        return f"{key} armor"

    # Размер в скобках: «Dancing Lights (medium)» — это вариант того же
    # заклинания. Переводим базу, а размер подставляем русским словом.
    SIZE_RU = {"tiny": "крошечный", "small": "маленький",
               "medium": "средний", "large": "большой",
               "huge": "огромный", "gargantuan": "исполинский"}
    match = re.match(r"^(.*?)\s*\((tiny|small|medium|large|huge|gargantuan)\)$",
                     normalized)
    if match:
        base = (_load_library_names().get(match.group(1).strip())
                or dnd5e_loc.title(match.group(1).strip()))
        if base:
            return f"{_match_case(original, base)} ({SIZE_RU[match.group(2)]})"

    TEMPLATES = (
        (r"^spell scroll (\d)\w\w level$", lambda m: f"Свиток заклинания {m.group(1)} уровня"),
        (r"^spell scroll cantrip$", lambda m: "Свиток заговора"),
        (r"^belt of (\w+) giant strength$",
         lambda m: "Пояс силы {} великана".format(
             {"cloud": "облачного", "fire": "огненного", "frost": "ледяного",
              "hill": "холмового", "stone": "каменного",
              "storm": "штормового"}.get(m.group(1), m.group(1)))),
        (r"^(berserker|vicious) (.*)$",
         lambda m: f"{'Берсерк' if m.group(1) == 'berserker' else 'Жестокий'} "
                   f"{(_base(m.group(2)) or m.group(2)).lower()}"),
        (r"^dancing (.*)$",
         lambda m: f"{_agree('Танцующий', _base(m.group(1)) or m.group(1))} "
                   f"{(_base(m.group(1)) or m.group(1)).lower()}"),
        (r"^defender (.*)$", lambda m: f"Защитник ({(_base(m.group(1)) or m.group(1)).lower()})"),
        (r"^luck blade (.*)$", lambda m: f"Клинок удачи ({(_base(m.group(1)) or m.group(1)).lower()})"),
        (r"^(dragon slayer|giant slayer|holy avenger|frost brand|flame tongue) (.*)$",
         lambda m: "{} ({})".format(
             {"dragon slayer": "Убийца драконов", "giant slayer": "Убийца великанов",
              "holy avenger": "Святой мститель", "frost brand": "Морозный клинок",
              "flame tongue": "Пламенный язык"}[m.group(1)],
             (_base(m.group(2)) or m.group(2)).lower())),
        (r"^(.*) of (life stealing|sharpness|wounding)$",
         lambda m: "{} {}".format(
             _base(m.group(1)) or m.group(1),
             {"life stealing": "похищения жизни", "sharpness": "остроты",
              "wounding": "ран"}[m.group(2)])),
        (r"^(.*?)\s+(?:armor )?of resistance$",
         lambda m: f"{_base(_restore_armor(m.group(1))) or _restore_armor(m.group(1))} "
                   f"сопротивления"),
    )
    for pattern, build in TEMPLATES:
        match = re.match(pattern, normalized)
        if match:
            built = build(match)
            if built and built != normalized:
                return built

    # 7. «Доспех X сопротивления» и прочие составные названия.
    match = re.match(r"^(.*?)\s+(?:armor )?of resistance$", normalized)
    if match:
        base = _load_library_names().get(match.group(1).strip())
        if not base:
            base = dnd5e_loc.title(match.group(1).strip())
        if base:
            return f"{_match_case(original, base)} сопротивления"

    # 3. Русское имя не трогаем.
    if not re.search(r"[a-z]", normalized):
        return original

    # 4. Похожие написания: «Claws» против «claw», опечатки в паках.
    for source in (NAME_RU, ):
        match = get_close_matches(normalized, source.keys(), n=1, cutoff=0.93)
        if match:
            return _match_case(original, source[match[0]])
    return original


def _match_case(original: str, translated: str) -> str:
    """Подогнать регистр перевода под исходное имя.

    В словарях термины записаны строчными («когти»), а названия атак в
    карточке должны читаться как заголовок: «Когти». Без этого в интерфейсе
    появлялось «когти» посреди заголовка.
    """
    if not translated:
        return translated
    if original[:1].isupper():
        return translated[:1].upper() + translated[1:]
    return translated


def translate_description(text: str) -> str:
    """Перевести описание: готовый русский текст либо аккуратная замена.

    Раньше функция подставляла русские слова ПРЯМО ВНУТРЬ английских
    предложений: «You hurl a mote of огонь at a creature». Читать это
    невозможно, а выглядит хуже оригинала.

    Теперь два разных случая:

    * Описание уже на русском — не трогаем вовсе.
    * Описание английское — переводим только УСТОЯВШИЕСЯ термины целыми
      словосочетаниями («saving throw», «on a hit»), от длинных фраз к
      коротким. Остальной текст остаётся английским: смешанный абзац честнее
      машинной подстановки.

    Для заклинаний и магических предметов полный русский текст берётся из
    library/descriptions_ru.json через translated_description() — эта функция
    работает как запасной вариант.
    """
    if not text:
        return ""

    # Уже русский текст: доля кириллицы выше четверти — не трогаем.
    letters = [c for c in text if c.isalpha()]
    if letters:
        cyrillic = sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё")
        if cyrillic / len(letters) > 0.25:
            return text

    # Сначала длинные фразы: иначе «saving throw» развалится на части.
    replacements = (
        ("melee weapon attack", "рукопашная атака оружием"),
        ("ranged weapon attack", "дальнобойная атака оружием"),
        ("melee spell attack", "рукопашная атака заклинанием"),
        ("ranged spell attack", "дальнобойная атака заклинанием"),
        ("saving throw", "спасбросок"),
        ("on a hit", "при попадании"),
        ("failed save", "провалив спасбросок"),
        ("successful save", "преуспев в спасброске"),
        ("armor class", "Класс Доспеха"),
        ("natural armor", "природный доспех"),
        ("hit points", "хиты"),
        ("darkvision", "тёмное зрение"),
        ("blindsight", "слепое зрение"),
        ("tremorsense", "чувство вибрации"),
        ("truesight", "истинное зрение"),
        ("sleight of hand", "ловкость рук"),
        ("animal handling", "обращение с животными"),
    )
    result = text
    for en, ru in replacements:
        # Целые слова, без учёта регистра первой буквы.
        result = re.sub(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])",
                        ru, result, flags=re.IGNORECASE)

    # Одиночные термины: типы урона и состояния приходят списком, а не фразой.
    singles = {
        "acid": "кислота", "bludgeoning": "дробящий", "cold": "холод",
        "fire": "огонь", "force": "силовой", "lightning": "молния",
        "necrotic": "некротический", "piercing": "колющий", "poison": "яд",
        "psychic": "психический", "radiant": "излучение",
        "slashing": "рубящий", "thunder": "гром",
        "frightened": "испуганный", "charmed": "очарованный",
        "blinded": "ослеплённый", "deafened": "оглохший",
        "paralyzed": "парализованный", "petrified": "окаменевший",
        "stunned": "оглушённый", "unconscious": "бессознательный",
    }
    stripped = result.strip()
    low = stripped.casefold()
    if low in singles:
        ru_text = singles[low]
        # Сохраняем регистр первой буквы, как в оригинале.
        if stripped[:1].isupper():
            ru_text = ru_text[:1].upper() + ru_text[1:]
        return ru_text

    # «cold from nonmagical attacks»: тип урона стоит первым словом, дальше
    # идёт уточнение. Переводим тип, уточнение оставляем — это термин, а не
    # связный текст, и полуперевод здесь читается нормально.
    first, _, rest = stripped.partition(" ")
    first_ru = singles.get(first.casefold())
    if first_ru and rest:
        if first[:1].isupper():
            first_ru = first_ru[:1].upper() + first_ru[1:]
        return f"{first_ru} {rest}"
    return result
# ------------------------------------------------------------------
# Нормализация документов
# ------------------------------------------------------------------

def normalize_monster(doc: dict, items_by_id: dict, art: dict) -> dict:
    """Актёр dnd5e -> компактный статблок для справочника."""
    system = doc.get("system") or {}
    attrs = system.get("attributes") or {}
    details = system.get("details") or {}
    traits = system.get("traits") or {}

    cr = details.get("cr", 0)
    pb = prof_bonus_from_cr(cr)

    abilities = {}
    mods = {}
    for ab in ("str", "dex", "con", "int", "wis", "cha"):
        val = ((system.get("abilities") or {}).get(ab) or {}).get("value", 10)
        abilities[ab] = int(val or 10)
        mods[ab] = ability_mod(val)

    skills = []
    for key, s in (system.get("skills") or {}).items():
        prof = s.get("value") or 0
        if not prof:
            continue
        bonus = mods.get(s.get("ability"), 0) + int(pb * (2 if prof == 2 else 1))
        skills.append({"key": key,
                       "name": SKILL_RU.get(key, key.upper()),
                       "mod": bonus})

    # Восприятие есть у всех: пассивное значение по умолчанию
    perception = next((s for s in skills if s["key"] == "prc"), None)
    passive = 10 + (perception["mod"] if perception else mods.get("wis", 0))

    movement = attrs.get("movement") or {}
    speeds = []
    for key, label in (("walk", "ходьба"), ("fly", "полёт"),
                       ("swim", "плавание"), ("climb", "лазание"),
                       ("burrow", "копание")):
        try:
            v = int(movement.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            speeds.append(f"{label} {v} фт")

    hp = attrs.get("hp") or {}
    ac = attrs.get("ac") or {}
    try:
        ac_value = int(ac.get("flat") or 0) or None
    except (TypeError, ValueError):
        ac_value = None
    if ac_value is None:
        # КД без «плоского» значения считается из снаряжения (как у гоблина):
        # броня по типу + ловкость + щит.
        ac_value = _ac_from_equipment(items_by_id, doc.get("_id"), mods)
    try:
        # Настоящий 0 (у призываемых объектов вроде Arcane Hand) — это
        # значение, а не отсутствие данных: раньше «0 or None» делал
        # хиты None и ломало payload призыва.
        raw_value, raw_max = hp.get("value"), hp.get("max")
        hp_value = (int(raw_value) if raw_value not in (None, "") else None)
        hp_max = (int(raw_max) if raw_max not in (None, "") else None)
    except (TypeError, ValueError):
        hp_value = hp_max = None

    # Языки
    lang = traits.get("languages") or {}
    languages = [normalize_language(l) for l in (lang.get("value") or [])
                 if str(l or "").strip()]
    if lang.get("custom"):
        languages.append(translate_description(lang["custom"]))

    # Чувства dnd5e 6.x хранятся в attributes.senses, а в старых акторах —
    # в traits.senses. Читаем оба формата, не теряя данные старых паков.
    senses = []
    raw_senses = attrs.get("senses") or traits.get("senses") or {}
    if isinstance(raw_senses, dict):
        # Новые паки кладут чувства прямо в attributes.senses, старые —
        # вложенным словарём ranges. Второй вариант раньше печатался в
        # карточке как "ranges {'darkvision': None} фт".
        nested = raw_senses.get("ranges")
        if isinstance(nested, dict):
            raw_senses = {**raw_senses, **nested}
        for key, val in raw_senses.items():
            if key in ("units", "special", "ranges") or not val:
                continue
            try:
                distance = int(val)
            except (TypeError, ValueError):
                continue
            senses.append(f"{SENSE_RU.get(key, key)} {distance} фт")
        if raw_senses.get("special"):
            senses.append(translate_description(raw_senses["special"]))
    elif isinstance(raw_senses, list):
        senses = [translate_description(str(s)) for s in raw_senses if s]
    telepathy = ((lang.get("communication") or {}).get("telepathy") or {})
    if isinstance(telepathy, dict) and telepathy.get("value"):
        distance = telepathy["value"]
        units = telepathy.get("units") or "ft"
        unit_text = "футов" if units == "ft" else str(units)
        senses.append(f"телепатия {distance} {unit_text}")

    # Спасброски: поле abilities.*.proficient — 0 (нет), 1 (владение),
    # 2 (экспертность). Формула dnd5e: модификатор + БМ × уровень владения.
    saves = []
    for ability in ("str", "dex", "con", "int", "wis", "cha"):
        source_ability = (system.get("abilities") or {}).get(ability) or {}
        try:
            proficiency = int(source_ability.get("proficient") or 0)
        except (TypeError, ValueError):
            proficiency = 0
        if proficiency:
            bonus = mods[ability] + pb * proficiency
            # rank — ступень владения: 1 — обычное (+бонус мастерства),
            # 2 — мастерство (двойное). Карточка актёра показывает это
            # тремя состояниями галочки и не путает +2 с +4.
            saves.append({"key": ability, "name": ABILITY_RU[ability],
                          "mod": bonus, "rank": min(2, proficiency)})

    # Сопротивления и иммунитеты — часть traits в модели Foundry.
    def trait_values(trait, names):
        if isinstance(trait, dict):
            raw_values = trait.get("value") or []
            custom = trait.get("custom") or ""
        else:
            raw_values, custom = trait or [], ""
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        values = [names.get(value, value) for value in raw_values if value]
        if custom:
            values.append(translate_description(custom))
        return values

    damage_immunities = trait_values(traits.get("di"), DAMAGE_RU)
    damage_resistances = trait_values(traits.get("dr"), DAMAGE_RU)
    damage_vulnerabilities = trait_values(traits.get("dv"), DAMAGE_RU)
    condition_immunities = trait_values(traits.get("ci"), CONDITION_RU)

    # Атаки и особенности: встроенные предметы актёра
    bonuses = (system.get("bonuses") or {})
    attacks, features = [], []
    monster_source = doc.get("name", "?")
    for raw_item in (doc.get("items") or []):
        item_id = (raw_item.get("_id") if isinstance(raw_item, dict)
                   else raw_item)
        if not item_id:
            continue
        item = items_by_id.get((doc.get("_id"), item_id))
        if not item:
            continue
        isystem = item.get("system") or {}
        source_name = item.get("name", "?")
        text = strip_html((isystem.get("description") or {}).get("value"),
                          {"name": monster_source,
                           "ctype": (details.get("type") or {}).get("value"),
                           "activities": isystem.get("activities"),
                           "uses_max": (isystem.get("uses") or {}).get("max"),
                           "damage_base": ((isystem.get("damage") or {}).get("base")
                                           if isinstance(isystem.get("damage"), dict)
                                           else None),
                           "mods": mods, "pb": pb})
        # Сверенная с dnd.su локализация (первая партия) имеет приоритет
        # над словарным переводом; неизвестные записи не искажаются.
        loc_name = dnd5e_loc.feature_name(monster_source, source_name)
        loc_text = dnd5e_loc.feature_text(monster_source, source_name)
        entry = {"name": loc_name or translate_name(source_name),
                 "source_name": source_name,
                 "activation": _feature_activation(isystem),
                 "mechanics": _feature_mechanics(isystem, abilities, pb),
                 "text": (loc_text if loc_text is not None
                          else translate_description(text))}

        attack = _extract_attack(isystem, abilities, pb, bonuses)
        if attack:
            attack["name"] = entry["name"]
            # Описание («Хватает цель и…») — в подсказку карточки действия:
            # без него у атаки видны только числа.
            attack["text"] = entry["text"]
            attacks.append(attack)
        else:
            features.append(entry)

    ctype = (details.get("type") or {})
    ctype_name = ctype.get("value") or "?"
    ctype_name = CREATURE_TYPE_RU.get(ctype_name, ctype_name)
    if ctype.get("subtype"):
        subtype = ", ".join(
            CREATURE_SUBTYPE_RU.get(part.strip().casefold(), part.strip())
            for part in str(ctype["subtype"]).split(",") if part.strip())
        ctype_name += f" ({subtype})"

    return {
        "id": doc.get("_id"),
        "kind": "monster",
        "name": (dnd5e_loc.monster_name(doc.get("name", "?"))
                 or translate_name(doc.get("name", "?"))),
        "source_name": doc.get("name", "?"),
        "size": SIZE_RU.get(traits.get("size"), traits.get("size", "")),
        "size_code": traits.get("size") or "medium",
        "ctype": ctype_name,
        "alignment": alignment_text(details.get("alignment")),
        "cr": cr,
        "xp": CR_XP.get(cr if isinstance(cr, (int, float)) else 0, 0),
        "ac": ac_value,
        "hp": hp_value,
        "hp_max": hp_max,
        "hp_formula": hp.get("formula") or "",
        "speeds": speeds,
        "environment": _translate_environment(details.get("environment") or ""),
        "saves": saves,
        "damage_immunities": damage_immunities,
        "damage_resistances": damage_resistances,
        "damage_vulnerabilities": damage_vulnerabilities,
        "condition_immunities": condition_immunities,
        "legendary_actions": ((system.get("resources") or {}).get("legact") or {}).get("max"),
        "legendary_resistances": ((system.get("resources") or {}).get("legres") or {}).get("max"),
        "has_lair_actions": bool(((system.get("resources") or {}).get("lair") or {}).get("value")),
        "lair_initiative": ((system.get("resources") or {}).get("lair") or {}).get("initiative"),
        "abilities": abilities,
        "mods": mods,
        "skills": sorted(skills, key=lambda s: s["name"]),
        "passive": passive,
        "languages": languages,
        "senses": senses,
        "attacks": attacks,
        "features": features,
        "img": art.get(doc.get("_id")),
    }


def distance_text(distance, units="ft") -> str:
    """Дистанция атаки: «10 ft» -> «10 фт». Пустая дистанция — пустая строка."""
    if distance in (None, "", 0, "0"):
        return ""
    unit = str(units or "ft").strip().casefold().rstrip(".")
    unit = {"ft": "фт", "feet": "фт", "foot": "фт", "футов": "фт",
            "миля": "миль", "миль": "миль", "mile": "миль",
            "miles": "миль"}.get(unit, unit)
    return f"{distance} {unit}".strip()


def _extract_attack(isystem: dict, abilities: dict = None,
                    pb: int = 0, bonuses: dict = None):
    """Атака из активности dnd5e 6.x либо из старого формата.

    В dnd5e 6.x бонус атаки и урон хранятся без чисел («автовычисление»):
    бонус = модификатор характеристики + бонус мастерства, урон дополняется
    бонусом атаки актёра (mwak/rwak). Считаем так же.
    """
    abilities = abilities or {}

    # Новый формат: system.activities = {id: {type:'attack', ...}}
    for act in (isystem.get("activities") or {}).values():
        if not isinstance(act, dict) or act.get("type") != "attack":
            continue
        atk = act.get("attack") or {}
        # Характеристика атаки: явная либо сильнейшая из Сил/Лов (как в dnd5e)
        ability = atk.get("ability")
        if ability not in abilities:
            ability = max(("str", "dex"),
                          key=lambda a: ability_mod(abilities.get(a)))
        dmg_mod = ability_mod(abilities.get(ability))
        try:
            bonus = int(str(atk.get("bonus") or "").strip() or "")
        except ValueError:
            # Авто-бонус: характеристика + бонус мастерства
            bonus = dmg_mod + int(pb)
        # Урон: база предмета (с бонусом вида «2d6+10») либо части активности
        base = (isystem.get("damage") or {}).get("base") or {}
        dmg = act.get("damage") or {}
        parts = dmg.get("parts") or []
        if base.get("denomination"):
            deduped = [p for p in parts
                       if not (p.get("number") == base.get("number")
                               and p.get("denomination") == base.get("denomination"))]
            parts = [base] + deduped
        formulas = []
        for index, p in enumerate(parts):
            f = damage_formula(p)
            if not f:
                continue
            # dnd5e не хранит бонус урона монстров — это модификатор
            # характеристики; добавляем к основной физической части (первой),
            # как делает сама dnd5e в карточке существа
            if index == 0 and not p.get("bonus") and dmg_mod:
                kind = f.split(" ")[0]
                rest = f.split(" ", 1)[1] if " " in f else ""
                f = f"{kind}{dmg_mod:+d}" + (" " + rest if rest else "")
            formulas.append(f)
        rng = act.get("range") or {}
        reach = isystem.get("range") or {}
        distance = rng.get("value") or reach.get("reach") or reach.get("value")
        units = rng.get("units") or reach.get("units") or "ft"
        return {
            "bonus": bonus,
            "damage": "; ".join(f for f in formulas if f),
            # Тип урона хранится отдельным полем: в формуле ему не место,
            # но из неё он и берётся («1d8+4 руб.»).
            "damage_type": _formula_damage_type(formulas),
            "range": distance_text(distance, units),
            "activation": _activation_text(act.get("activation")),
        }
    # Старый формат: system.damage + system.activation у предмета
    dmg = isystem.get("damage") or {}
    base = dmg.get("base") if isinstance(dmg.get("base"), dict) else dmg
    if base and base.get("denomination"):
        formula = damage_formula(base)
        return {
            "bonus": None,
            "damage": formula,
            "damage_type": _formula_damage_type([formula]),
            "range": "",
            "activation": _activation_text(isystem.get("activation")),
        }
    return None


def _formula_damage_type(formulas) -> str:
    """Первый тип урона из формул частей: «1d8+4 руб.» -> ``slashing``."""
    for formula in formulas or ():
        _clean, kind = combat_rules.split_damage_text(formula)
        if kind:
            return kind
    return ""


def _ac_from_equipment(items_by_id: dict, actor_id: str, mods: dict) -> int:
    """КД из снаряжения: броня (по типу) + ловкость + щит.

    Тип брони в dnd5e 6.x лежит в system.type.value
    ('light' | 'medium' | 'heavy' | 'shield').
    """
    best = 10 + mods.get("dex", 0)
    shields = 0
    for (parent, _child), item in items_by_id.items():
        if parent != actor_id:
            continue
        isystem = item.get("system") or {}
        armor = isystem.get("armor")
        if not isinstance(armor, dict) or not armor.get("value"):
            continue
        atype = isystem.get("type")
        if isinstance(atype, dict):
            atype = atype.get("value") or ""
        if atype == "shield":
            shields += int(armor["value"])
            continue
        dex = mods.get("dex", 0)
        if atype == "light":
            allowed = dex
        elif atype == "heavy":
            allowed = 0
        else:
            allowed = min(dex, 2)
        candidate = int(armor["value"]) + max(0, allowed)
        if candidate > best:
            best = candidate
    return best + shields


def _translate_environment(value: str) -> str:
    """Среда обитания: фиксированный словарь SRD или свободный текст."""
    if not value:
        return ""
    translated = ENVIRONMENT_RU.get(str(value).strip().casefold())
    return translated if translated else translate_description(str(value))


def _feature_activation(isystem: dict) -> str:
    """Тип активации особенности монстра (легендарное/логово/перезарядка).

    dnd5e 6.x хранит активацию в system.activities[*].activation,
    а перезарядку — в system.uses.recovery[].period == 'recharge'.
    """
    for act in (isystem.get("activities") or {}).values():
        if isinstance(act, dict):
            text = _activation_text(act.get("activation"))
            if text:
                return text
    recovery = (isystem.get("uses") or {}).get("recovery") or []
    for entry in recovery:
        if isinstance(entry, dict) and entry.get("period") == "recharge":
            return "перезарядка"
    return ""


def _save_dc(dc, abilities: dict, pb: int):
    """DC спасброска activity dnd5e 6.x.

    В паке DC лежит как {calculation, formula}: формула обычно фиксированная
    число («14»), а при расчёте от характеристики dnd5e считает
    8 + бонус мастерства + модификатор характеристики.
    """
    if isinstance(dc, str):
        formula, calculation = dc, ""
    elif isinstance(dc, dict):
        formula = dc.get("formula") or ""
        calculation = dc.get("calculation") or ""
    else:
        formula, calculation = "", ""
    try:
        return int(str(formula).strip())
    except (TypeError, ValueError):
        pass
    if calculation in abilities:
        return 8 + pb + ability_mod(abilities[calculation])
    return None


def _feature_mechanics(isystem: dict, abilities: dict, pb: int) -> dict:
    """Механика activities особенности для кнопок бросков в карточке.

    save-активность -> способность и DC; damage/heal-активности -> формулы
    урона/лечения из damage.parts (терминология dnd.su).
    """
    abilities = abilities or {}
    saves, damages, healings = [], [], []
    for act in (isystem.get("activities") or {}).values():
        if not isinstance(act, dict):
            continue
        if act.get("type") == "save":
            save = act.get("save") or {}
            ability_ids = save.get("ability") or []
            if isinstance(ability_ids, str):
                ability_ids = [ability_ids]
            dc = _save_dc(save.get("dc"), abilities, pb)
            for ability in ability_ids:
                saves.append({
                    "ability": ABILITY_FULL_RU.get(ability,
                                                    str(ability).upper()),
                    "dc": dc,
                })
        for part in ((act.get("damage") or {}).get("parts") or []):
            formula = damage_formula(part)
            if formula:
                damages.append(formula)
        for part in ((act.get("heal") or {}).get("parts") or []):
            formula = damage_formula(part)
            if formula:
                healings.append("лечение: " + formula)
    return {"saves": saves,
            "damage": "; ".join(damages),
            "healing": "; ".join(healings)}


def _activation_text(activation) -> str:
    if not isinstance(activation, dict) or not activation.get("type"):
        return ""
    label = ACTIVATION_RU.get(activation["type"], activation["type"])
    try:
        value = int(activation.get("value") or 0)
    except (TypeError, ValueError):
        value = 0
    return f"{value}× {label}" if value > 1 else label


def _uses_text(isystem: dict) -> str:
    """Заряды/ограниченные использования предмета (system.uses, dnd5e 6.x).

    Например: «заряды: 7 (на рассвете 1d6 + 1)», «заряды: 1»,
    «перезарядка» (когда восстановление есть, а лимита нет).
    """
    uses = isystem.get("uses") or {}
    raw_max = str(uses.get("max") or "").strip()
    try:
        max_uses = str(int(raw_max)) if raw_max else ""
    except (TypeError, ValueError):
        max_uses = raw_max
    labels = []
    for entry in (uses.get("recovery") or []):
        if not isinstance(entry, dict):
            continue
        period = str(entry.get("period") or "")
        label = USES_RECOVERY_RU.get(period, period)
        formula = str(entry.get("formula") or "").strip()
        if entry.get("type") == "formula" and formula:
            label = f"{label} {formula}"
        labels.append(label)
    if max_uses:
        suffix = f" ({'; '.join(labels)})" if labels else ""
        return f"заряды: {max_uses}{suffix}"
    return "; ".join(labels)


def _item_activities(isystem: dict) -> dict:
    """Итоги activities предмета dnd5e 6.x для карточки.

    cast-активность несёт challenge (фиксированный Сл/бонус от свитка
    или предмета), save/attack — обычные activities. Лечение и урон
    берутся из healing/damage-активностей (healing — отдельный объект,
    а не список parts).
    """
    save_dc = None
    attack_bonus = None
    heals, damages = [], []
    for act in (isystem.get("activities") or {}).values():
        if not isinstance(act, dict):
            continue
        kind = act.get("type")
        if kind == "cast":
            challenge = (act.get("spell") or {}).get("challenge") or {}
            if save_dc is None and isinstance(
                    challenge.get("save"), (int, float)):
                save_dc = int(challenge["save"])
            bonus = str(challenge.get("attack") or "").strip()
            if attack_bonus is None and bonus:
                try:
                    attack_bonus = int(bonus)
                except ValueError:
                    pass
        elif kind == "save" and save_dc is None:
            save_dc = _save_dc((act.get("save") or {}).get("dc"), {}, 0)
        elif kind == "attack" and attack_bonus is None:
            atk = act.get("attack") or {}
            bonus = str(atk.get("bonus") or "").strip()
            if atk.get("flat") and bonus:
                try:
                    attack_bonus = int(bonus)
                except ValueError:
                    pass
        heal = act.get("healing") or act.get("heal") or {}
        heal_parts = (heal.get("parts")
                      if isinstance(heal.get("parts"), list) else [heal])
        for part in heal_parts:
            f = damage_formula(part) if isinstance(part, dict) else ""
            # Только формулы с костями: пустая часть даёт лишь тип урона
            if f and re.search(r"\d+d\d+", f):
                heals.append(f)
        if kind == "damage":
            for part in (act.get("damage") or {}).get("parts") or []:
                f = damage_formula(part)
                if f and re.search(r"\d+d\d+", f):
                    damages.append(f)
    return {"save_dc": save_dc,
            "attack_bonus": attack_bonus,
            "heal": "; ".join(heals),
            "damage": "; ".join(damages)}


def _item_subtype(raw) -> str:
    """Подтип предмета: код из dnd5e (martialM) -> русское название.

    В паках system.type.subtype может быть как строкой-кодом, так и
    объектом {"value": "martialM"}; оба варианта сводим к тексту.
    """
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("subtype") or ""
    text = str(raw or "").strip()
    if not text or text == "?":
        return ""
    return ITEM_SUBTYPE_RU.get(text, translate_name(text))


def normalize_item(doc: dict, art: dict) -> dict:
    """Предмет dnd5e -> компактная карточка."""
    system = doc.get("system") or {}
    damage = system.get("damage") or {}
    base = damage.get("base") if isinstance(damage.get("base"), dict) else {}
    rng = system.get("range") or {}
    weight = system.get("weight") or {}
    price = system.get("price") or {}

    parts = []
    if rng.get("reach"):
        parts.append(f"досягаемость {rng['reach']} фт")
    if rng.get("value"):
        long_ = f"/{rng['long']}" if rng.get("long") else ""
        parts.append(f"дальность {rng['value']}{long_} фт")

    props = []
    raw_props = system.get("properties") or {}
    if isinstance(raw_props, dict):
        props = [str(k) for k, v in raw_props.items() if v]
    elif isinstance(raw_props, list):
        props = [str(p) for p in raw_props if p]

    versatile = damage.get("versatile") or {}
    formulas = []
    is_healing = (base.get("types") or []) == ["healing"]
    if base.get("denomination"):
        text = damage_formula(base)
        if is_healing:
            # damage_formula уже добавляет тип «лечение» — не дублируем
            if text.endswith(" лечение"):
                text = text[:-len(" лечение")]
            formulas.append("лечение: " + text.strip())
        else:
            formulas.append("урон: " + text)
    if versatile.get("denomination"):
        formulas.append("двуруч.: " + damage_formula(versatile))

    activities = _item_activities(system)
    uses = _uses_text(system)
    # У расходников (зелья) heal-активность дублирует damage.base —
    # на карточке это одна строка, а не две.
    if (activities["heal"] and base.get("denomination")
            and activities["heal"] == damage_formula(base)):
        activities["heal"] = ""

    return {
        "id": doc.get("_id"),
        "kind": "item",
        "name": translate_name(doc.get("name", "?")),
        "source_name": doc.get("name", "?"),
        "itype": ITEM_TYPE_RU.get(doc.get("type"), doc.get("type", "")),
        "subtype": _item_subtype(system.get("type")),
        "formulas": formulas,
        "uses": uses,
        "save_dc": activities["save_dc"],
        "attack_bonus": activities["attack_bonus"],
        "heal": activities["heal"],
        "activity_damage": activities["damage"],
        "range": ", ".join(parts),
        "weight": (f"{weight.get('value')} фунт"
                   if weight.get("value") not in (None, "") else ""),
        "price": (f"{price.get('value')} {DENOM_RU.get(price.get('denomination'), '')}".strip()
                  if price.get("value") not in (None, "") else ""),
        "rarity": RARITY_RU.get(system.get("rarity") or "", ""),
        # Пустая настройка — это пустая строка, а не "?": иначе у
        # обычного факела в карточке стоял вопросительный знак.
        "attunement": ATTUNEMENT_RU.get(
            str(system.get("attunement") or "").strip(),
            translate_name(system.get("attunement") or "")),
        "properties": [ITEM_PROPERTY_RU.get(p, p) for p in props],
        # Готовый текст с dnd.su важнее подстрочной замены терминов:
        # в library/descriptions_ru.json лежат полные правила для магических
        # предметов, и они читаются как книга, а не как смесь языков.
        "description": (translated_description(doc.get("name") or "")
                        or translate_description(strip_html(
                            (system.get("description") or {}).get("value"),
                            {"name": doc.get("name"),
                             "activities": system.get("activities"),
                             "uses_max": (system.get("uses") or {}).get("max")}))),
        "img": art.get(doc.get("_id")),
    }


def normalize_spell(doc: dict, art: dict) -> dict:
    """Заклинание dnd5e -> компактная карточка.

    В dnd5e 6.x компоненты лежат в system.properties, а урон, спасбросок
    и атака — в первой активности (activities).
    """
    system = doc.get("system") or {}
    level = int(system.get("level") or 0)
    rng = system.get("range") or {}
    duration = system.get("duration") or {}
    activation = system.get("activation") or {}
    props = set(system.get("properties") or [])
    comp = [ru for key, ru in (("vocal", "В"), ("somatic", "С"),
                               ("material", "М")) if key in props]

    activities = [a for a in (system.get("activities") or {}).values()
                  if isinstance(a, dict)]
    primary = activities[0] if activities else {}

    save = primary.get("save") or {}
    ability = save.get("ability")
    if isinstance(ability, list):
        save_abilities = " или ".join(ABILITY_FULL_RU.get(a, a)
                                      for a in ability)
    else:
        save_abilities = ABILITY_FULL_RU.get(ability, "") if ability else ""
    # DC спасброска: фиксированная формула либо расчёт от заклинателя
    # (в паке calculation='spellcasting' — Сл зависит от уровня класса).
    save_dc = _save_dc(save.get("dc"), {}, 0)
    save_calculation = ""
    if isinstance(save.get("dc"), dict):
        save_calculation = save["dc"].get("calculation") or ""
    if save_abilities and save_dc is not None:
        save_text = f"{save_abilities}, Сл {save_dc}"
    elif save_abilities and save_calculation == "spellcasting":
        save_text = f"{save_abilities}, Сл заклинателя"
    else:
        save_text = save_abilities

    dmg = primary.get("damage") or {}
    formulas = [damage_formula(p) for p in (dmg.get("parts") or []) if p]
    # Лечение в dnd5e 6.x — объект healing (не parts); у старых паков — heal.parts
    heal = primary.get("healing") or primary.get("heal") or {}
    if isinstance(heal.get("parts"), list):
        heal_parts = heal["parts"]
    elif heal:
        heal_parts = [heal]
    else:
        heal_parts = []
    # Тип «healing» уже переводится как «лечение», отдельный префикс не нужен
    formulas += [h for h in (damage_formula(p) for p in heal_parts if p) if h]
    scaling = ""
    for p in (dmg.get("parts") or []) + heal_parts:
        mode = ((p.get("scaling") or {}).get("mode"))
        if mode in ("level", "cantrip"):
            scaling = "+ растёт с уровнем заклинания"
            break

    attack_text = ""
    attack_bonus = None
    if primary.get("type") == "attack":
        attack_text = "атака заклинанием"
        atk = primary.get("attack") or {}
        bonus = str(atk.get("bonus") or "").strip()
        if atk.get("flat") and bonus:
            try:
                attack_bonus = int(bonus)
                attack_text = f"атака заклинанием +{attack_bonus}"
            except ValueError:
                pass

    dur_text = ""
    if duration.get("value"):
        unit = DURATION_RU.get(duration.get("units"),
                               duration.get("units") or "")
        dur_text = f"{duration['value']} {unit}".strip()
    else:
        dur_text = DURATION_RU.get(duration.get("units"), "")

    return {
        "id": doc.get("_id"),
        "kind": "spell",
        "name": translate_name(doc.get("name", "?")),
        "source_name": doc.get("name", "?"),
        "level": level,
        "level_text": "заговор" if level == 0 else f"{level} уровень",
        "school": SCHOOL_RU.get(system.get("school") or "", ""),
        "activation": _activation_text(activation) or "",
        "range": (f"{rng.get('value')} {('футов' if (rng.get('units') or 'ft') == 'ft' else rng.get('units'))}".strip()
                  if rng.get("value") else (rng.get("special") or "")),
        "duration": dur_text,
        "components": ", ".join(comp),
        "materials": translate_description(
            (system.get("materials") or {}).get("value") or ""),
        "concentration": "concentration" in props,
        "ritual": "ritual" in props,
        "save": save_text,
        "save_dc": save_dc,
        "attack": attack_text,
        "attack_bonus": attack_bonus,
        "damage": "; ".join(f for f in formulas if f),
        "scaling": scaling,
        "classes": (", ".join(SPELL_CLASS_RU.get(c, translate_name(c))
                                for c in system["classes"])
                    if isinstance(system.get("classes"), list)
                    else SPELL_CLASS_RU.get(
                        system.get("classes"),
                        translate_name(system.get("classes") or ""))),
        "description": (translated_description(doc.get("name") or "")
                        or translate_description(strip_html(
                            (system.get("description") or {}).get("value"),
                            {"name": doc.get("name"),
                             "activities": system.get("activities"),
                             "uses_max": (system.get("uses") or {}).get("max")}))),
        "img": art.get(doc.get("_id")),
    }


# ------------------------------------------------------------------
# Импорт из дистрибутива
# ------------------------------------------------------------------

def library_dir(data_root: str) -> str:
    return os.path.join(data_root, LIBRARY_DIRNAME)


def _bundled_library_dir() -> str:
    """Библиотека рядом с приложением: ``library/dnd5e``.

    В исходниках и в собранном .exe путь один и тот же — папка приложения
    (``shared/app_paths.app_dir``), поэтому библиотеку достаточно положить
    рядом с ``main.py`` или с ``DnD_VirtualTable.exe``. ``cwd`` оставлен
    запасным вариантом для запуска из другого каталога.
    """
    for candidate in (os.path.join(app_paths.app_dir(), "library", "dnd5e"),
                      os.path.join(os.getcwd(), "library", "dnd5e")):
        if os.path.isdir(candidate):
            return candidate
    return ""


def library_write_dir(data_root: str) -> str:
    """Куда писать импорт библиотеки.

    Библиотека — часть проекта, а не пользовательских данных: картинки
    токенов лежат рядом с исходниками, в ``library/dnd5e``. Раньше импорт
    всегда писал в ``<корень данных>/library/dnd5e`` (по умолчанию
    ``~/.dnd_vtt``), поэтому 70+ МБ арта оказывались на диске C, хотя игра
    читала проектную копию.

    Порядок: ``DND_VTT_LIBRARY`` (явный путь) → проектная ``library/dnd5e``,
    если она есть и доступна для записи → корень данных.
    """
    env = os.environ.get("DND_VTT_LIBRARY")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    bundled = _bundled_library_dir()
    if bundled and os.access(bundled, os.W_OK):
        return bundled
    return library_dir(data_root)


def library_roots(data_root: str) -> list:
    """Корни, где искать данные библиотеки: сначала проект, потом данные.

    Картинки токенов лежат в проекте (папки по типу существа). Если рядом
    с игрой их нет, работает старый импорт в <корень данных>/library/dnd5e.
    """
    roots = []
    env = os.environ.get("DND_VTT_LIBRARY")
    if env:
        roots.append(os.path.abspath(os.path.expanduser(env)))
    bundled = _bundled_library_dir()
    if bundled:
        roots.append(bundled)
    local = library_dir(data_root)
    if os.path.abspath(local) not in (os.path.abspath(r) for r in roots):
        roots.append(local)
    return roots


def library_read_dir(data_root: str) -> str:
    """Откуда читать JSON библиотеки.

    Проектная копия — источник правды: если в ней есть ``monsters.json``,
    читаем её и не смотрим на старый импорт в папке данных. Раньше выигрывал
    тот файл, что новее, поэтому повторный импорт в ``~/.dnd_vtt`` мог
    незаметно переключить игру на арт с диска C.
    """
    for root in library_roots(data_root):
        if os.path.isfile(os.path.join(root, "monsters.json")):
            return root
    return library_write_dir(data_root)


def _find_node() -> str:
    node = shutil.which("node")
    if node:
        return node
    candidate = r"C:\Program Files\nodejs\node.exe"
    if os.path.isfile(candidate):
        return candidate
    raise RuntimeError(
        "Не найден Node.js. Он нужен один раз — прочитать LevelDB-паки "
        "dnd5e. Установите Node.js и повторите импорт.")


def _extractor_script() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "cli", "tools", "extract_pack.mjs")
    if not os.path.isfile(path):
        raise RuntimeError(f"Не найден экстрактор паков: {path}")
    if not os.path.isdir(os.path.join(os.path.dirname(path), "node_modules")):
        raise RuntimeError(
            "Не установлены утилиты чтения паков. Один раз выполните:\n"
            "  cd cli/tools && npm install")
    return path


def _read_jsonl(path: str) -> list:
    docs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    return docs


_PACK_CHILD_RE = re.compile(r"^!actors\.items!([^.]+)\.(.+)$")


def _split_pack(docs: list):
    """Актёры пака + индекс встроенных предметов {(родитель, предмет): doc}.

    Ключи пака вида !actors.items!<id актёра>.<id предмета>: один и тот же
    _id встречается у копий предмета у разных актёров, поэтому сверяемся
    именно по паре «актёр + предмет» — иначе атаки монстров смешиваются.
    """
    actors, items_by = [], {}
    for d in docs:
        if d.get("type") in ("npc", "character", "vehicle", "group"):
            actors.append(d)
            # В YAML-исходниках (архив GitHub) вложенные предметы лежат прямо
            # в массиве items объектами, а не ссылками на отдельные документы.
            # Раскладываем их в тот же индекс, что и LevelDB-пак.
            for child in (d.get("items") or []):
                if not isinstance(child, dict) or not child.get("_id"):
                    continue
                key = (d.get("_id"), child["_id"])
                old = items_by.get(key)
                if old is None or ((child.get("system") or {}).get("activities")
                                   and not (old.get("system") or {}).get("activities")):
                    items_by[key] = child
            continue
        m = _PACK_CHILD_RE.match(d.get("_packKey") or "")
        if not m:
            continue
        key = (m.group(1), m.group(2))
        old = items_by.get(key)
        # «Голая» копия без активностей не должна затирать полную
        if old is None or ((d.get("system") or {}).get("activities")
                           and not (old.get("system") or {}).get("activities")):
            items_by[key] = d
    return actors, items_by


def _archive_root_prefix(zf: zipfile.ZipFile) -> str:
    """Корневая папка внутри zip, если она есть.

    GitHub-архив распаковывается в «dnd5e-release-6.0.6/…», официальный
    релиз — сразу «packs/…». Возвращаем префикс с завершающим слэшем
    (или пустую строку), чтобы остальной код искал пути одинаково.
    """
    for name in zf.namelist():
        if "/packs/" in name:
            return name[:name.index("/packs/") + 1]
        if name.startswith("packs/"):
            return ""
    return ""


def collect_icons(source: str, data_root: str, log=None) -> dict:
    """Скопировать иконки предметов и заклинаний из дистрибутива.

    В паках у каждого предмета есть поле img вида «icons/weapons/...». Раньше
    импорт эти файлы не забирал вовсе — карточки оставались с пустой иконкой,
    хотя в Foundry у предметов и заклинаний картинка есть всегда.
    """
    source = os.path.abspath(source)
    is_zip = zipfile.is_zipfile(source)
    if not is_zip and not os.path.isdir(source):
        raise RuntimeError(f"Не найден дистрибутив dnd5e: {source}")

    out_dir = library_write_dir(data_root)
    art_dir = os.path.join(out_dir, "art")
    os.makedirs(art_dir, exist_ok=True)

    result = {}
    counts = {"copied": 0, "missing": 0}

    zf = zipfile.ZipFile(source) if is_zip else None
    try:
        prefix = _archive_root_prefix(zf) if is_zip else ""
        for kind in ("items", "spells"):
            path = os.path.join(out_dir, f"{kind}.json")
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                records = json.load(f)

            for rec in records:
                if rec.get("img"):
                    continue        # у монстров уже стоит своя картинка
                source_name = rec.get("source_name") or ""
                icon = _find_icon(zf, prefix, kind, source_name)
                if not icon:
                    counts["missing"] += 1
                    continue
                rel, blob = icon
                base = os.path.basename(rel)
                target = os.path.join(art_dir, base)
                if not os.path.isfile(target):
                    with open(target, "wb") as fh:
                        fh.write(blob)
                rec["img"] = os.path.join("art", base)
                result[(kind, source_name)] = rec["img"]
                counts["copied"] += 1

            with open(path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False)
    finally:
        if zf:
            zf.close()

    if log:
        log(f"Иконок скопировано: {counts['copied']}, без иконки: {counts['missing']}")
    return counts


def _find_icon(zf, prefix: str, kind: str, source_name: str):
    """Найти файл иконки для записи: (путь в архиве, содержимое) либо None."""
    if not source_name:
        return None
    # Ищем документ в паках по name: индекса «имя -> img» нет, поэтому
    # читаем только нужные YAML. Для скорости ограничиваемся одним проходом
    # по уже собранному индексу, если он есть.
    import yaml as _yaml
    for name in zf.namelist() if zf else []:
        if f"/packs/_source/{kind}/" not in name or not name.endswith(".yml"):
            continue
        try:
            doc = _yaml.safe_load(zf.read(name).decode("utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict) or doc.get("name") != source_name:
            continue
        img = doc.get("img")
        if not img:
            return None
        try:
            return img, zf.read(prefix + img)
        except KeyError:
            return None
    return None


def collect_art_only(source: str, data_root: str, log=None) -> dict:
    """Дособрать только картинки монстров, не переимпортируя библиотеку.

    Импорт SRD — долгая операция: читает паки через Node и заново собирает
    карточки. Если картинки не скопировались (например, из-за корневой папки
    в архиве), пересобирать всё заново незачем — достаточно разложить арты и
    обновить поле img в уже готовых записях.
    """
    source = os.path.abspath(source)
    is_zip = zipfile.is_zipfile(source)
    if not is_zip and not os.path.isdir(source):
        raise RuntimeError(f"Не найден дистрибутив dnd5e: {source}")

    out_dir = library_write_dir(data_root)
    monsters_path = os.path.join(out_dir, "monsters.json")
    if not os.path.isfile(monsters_path):
        raise RuntimeError("Библиотека не импортирована: нет monsters.json")

    with open(monsters_path, encoding="utf-8") as f:
        records = json.load(f)

    art_dir = os.path.join(out_dir, "art")
    counts = {"records": len(records), "with_art": 0, "missing": 0}

    zf = zipfile.ZipFile(source) if is_zip else None
    try:
        if is_zip:
            prefix = _archive_root_prefix(zf)
            try:
                mapping = json.loads(zf.read(
                    prefix + "json/fa-token-mapping.json").decode("utf-8"))
            except KeyError:
                raise RuntimeError("В архиве нет json/fa-token-mapping.json")
        else:
            prefix = ""
            mp = os.path.join(source, "json", "fa-token-mapping.json")
            with open(mp, encoding="utf-8") as f:
                mapping = json.load(f)

        needed = _build_art_map(mapping, "dnd5e.monsters")
        art = _collect_art(is_zip, zf, source, needed, art_dir,
                           log=log, prefix=prefix)
    finally:
        if zf:
            zf.close()

    for rec in records:
        rec_id = rec.get("id")
        if rec_id in art:
            rec["img"] = art[rec_id]
            counts["with_art"] += 1
        elif not rec.get("img"):
            counts["missing"] += 1

    with open(monsters_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)

    if log:
        log(f"Артов разложено: {counts['with_art']} из {counts['records']}")
        if counts["missing"]:
            log(f"Без картинки в дистрибутиве: {counts['missing']} "
                f"(в Foundry для них нет токенов)")
    return counts


def _build_art_map(mapping: dict, pack_name: str) -> dict:
    """{id актёра: относительный путь арта} из fa-token-mapping.json.

    Встречаются две формы: token — строка либо объект {texture: {src}}.
    """
    out = {}
    for doc_id, entry in (mapping.get(pack_name) or {}).items():
        if not isinstance(entry, dict):
            continue
        src = None
        token = entry.get("token")
        if isinstance(token, str):
            src = token
        elif isinstance(token, dict):
            src = (token.get("texture") or {}).get("src")
        src = src or entry.get("actor")
        if src and str(src).startswith("systems/dnd5e/tokens/"):
            out[doc_id] = str(src)
    return out


def _collect_art(source_is_zip: bool, zf: zipfile.ZipFile, root: str,
                 needed: dict, art_dir: str, log=None,
                 prefix: str = "") -> dict:
    """Скопировать нужные арты в библиотеку. Возвращает {doc_id: путь}.

    В архиве-релизе файлы лежат без префикса systems/dnd5e/, в
    распакованной системе — с ним: пробуем оба варианта.
    """
    result = {}
    misses = []
    os.makedirs(art_dir, exist_ok=True)

    # Имя файла в art/ должно быть уникальным: при совпадении basename из
    # разных папок второй арт молча перезапишет первый, и монстр получит
    # ЧУЖУЮ картинку. Поэтому занятые имена разводим префиксом папки.
    # (В 6.0.6 коллизий нет, но это защита на будущие версии.)
    used = {}
    for doc_id, rel in needed.items():
        base = os.path.basename(rel)
        owner = used.get(base)
        if owner is not None and owner != rel:
            folder = rel.rstrip("/").split("/")[-2:-1]
            base = f"{folder[0]}_{base}" if folder else base
        used.setdefault(base, rel)
        target = os.path.join(art_dir, base)
        rel_short = rel[len("systems/dnd5e/"):] if rel.startswith(
            "systems/dnd5e/") else rel
        try:
            if source_is_zip:
                written = False
                for member in (prefix + rel, prefix + rel_short,
                               rel, rel_short):
                    try:
                        with zf.open(member.replace("\\", "/")) as src, \
                                open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        written = True
                        break
                    except KeyError:
                        continue
                if not written:
                    raise OSError(rel)
            else:
                src_path = next((os.path.join(root, *p.split("/"))
                                 for p in (rel, rel_short)
                                 if os.path.isfile(os.path.join(root,
                                                                *p.split("/")))),
                                None)
                if not src_path:
                    raise OSError(rel)
                shutil.copyfile(src_path, target)
            result[doc_id] = os.path.join("art", base)
        except (KeyError, OSError):
            misses.append(rel)
    if log and misses:
        log(f"Артов не найдено в дистрибутиве: {len(misses)} "
            f"(токены без картинок)")
    return result


def _read_yaml_pack(pack_dir: str, out_file: str, log=None) -> int:
    """Прочитать пак из YAML-исходников (архив GitHub) в JSONL.

    Официальный релиз Foundry хранит паки в LevelDB — их читает Node-скрипт.
    Но архив с GitHub содержит те же данные обычными YAML-файлами, и гонять
    ради них Node незачем: достаточно разобрать YAML и записать JSONL в том
    же формате, что даёт экстрактор.
    """
    import yaml as _yaml

    count = 0
    with open(out_file, "w", encoding="utf-8") as out:
        for path in sorted(Path(pack_dir).rglob("*.yml")):
            try:
                doc = _yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(doc, dict):
                continue
            # У вложенных предметов актёра есть родитель: сохраняем путь,
            # чтобы _split_pack мог привязать их к монстру.
            if "items" in doc and isinstance(doc.get("items"), list):
                parent = doc.get("_id")
                for child in doc["items"]:
                    if isinstance(child, dict) and parent and child.get("_id"):
                        child.setdefault(
                            "_packKey", f"!actors.items!{parent}.{child['_id']}")
            out.write(json.dumps(doc, ensure_ascii=False) + "\n")
            count += 1
    if log:
        log(f"  YAML-файлов прочитано: {count}")
    return count


def _run_extractor(node: str, pack_dir: str, out_file: str, log=None):
    script = _extractor_script()
    proc = subprocess.run([node, script, pack_dir, out_file],
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Не удалось прочитать пак {pack_dir}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}")
    if log and proc.stdout.strip():
        log(proc.stdout.strip())


def import_dnd5e(source: str, data_root: str, log=None) -> dict:
    """Импортировать библиотеку SRD из архива/папки dnd5e.

    source — путь к dnd5e-release-*.zip либо к распакованной папке системы.
    """
    source = os.path.abspath(source)
    is_zip = zipfile.is_zipfile(source)
    if not is_zip and not os.path.isdir(source):
        raise RuntimeError(f"Не найден дистрибутив dnd5e: {source}")

    node = _find_node()
    out_dir = library_write_dir(data_root)
    os.makedirs(out_dir, exist_ok=True)

    counts = {}
    with tempfile.TemporaryDirectory(prefix="dnd5e_import_") as tmp:
        zf = zipfile.ZipFile(source) if is_zip else None
        try:
            # Архив с GitHub приходит с корневой папкой «dnd5e-release-6.0.6/»,
            # а официальный релиз — без неё. Определяем корень по первому
            # файлу и дальше ищем паки уже с учётом префикса: иначе импорт
            # падал с «нет пака packs/monsters» либо не находил ни одного файла.
            root_prefix = _archive_root_prefix(zf) if is_zip else ""
            mapping = {}
            if is_zip:
                try:
                    mapping = json.loads(zf.read(
                        root_prefix + "json/fa-token-mapping.json").decode("utf-8"))
                except KeyError:
                    pass
                for pack in PACKS:
                    # Два вида архивов:
                    #   официальный релиз — packs/<pack>/ (LevelDB, читаем Node)
                    #   архив с GitHub   — packs/_source/<pack>/ (исходные YAML)
                    # Раньше искался только первый вариант, поэтому импорт из
                    # GitHub-архива не находил ни одного файла.
                    for candidate in (f"{root_prefix}packs/{pack}/",
                                      f"{root_prefix}packs/_source/{pack}/"):
                        for info in zf.infolist():
                            if (info.filename.startswith(candidate)
                                    and not info.is_dir()):
                                rel = info.filename[len(candidate):]
                                target = os.path.join(tmp, "packs", pack,
                                                      rel.replace("/", os.sep))
                                os.makedirs(os.path.dirname(target),
                                            exist_ok=True)
                                with zf.open(info) as src, open(target, "wb") as dst:
                                    shutil.copyfileobj(src, dst)
            else:
                mp = os.path.join(source, "json", "fa-token-mapping.json")
                if os.path.isfile(mp):
                    with open(mp, encoding="utf-8") as f:
                        mapping = json.load(f)

            for pack in PACKS:
                pack_dir = (os.path.join(tmp, "packs", pack) if is_zip
                            else os.path.join(source, "packs", pack))
                if not os.path.isdir(pack_dir):
                    raise RuntimeError(f"В дистрибутиве нет пака packs/{pack}")
                jsonl = os.path.join(tmp, f"{pack}.jsonl")
                # YAML-исходники (GitHub-архив) читаем на месте; LevelDB
                # (официальный релиз) — через Node-экстрактор.
                yaml_files = list(Path(pack_dir).rglob("*.yml"))
                if yaml_files:
                    _read_yaml_pack(pack_dir, jsonl, log=log)
                else:
                    _run_extractor(node, pack_dir, jsonl, log=log)
                docs = _read_jsonl(jsonl)

                if pack == "monsters":
                    actors, items_by_id = _split_pack(docs)
                    art_needed = _build_art_map(mapping, "dnd5e.monsters")
                    art_dir = os.path.join(out_dir, "art")
                    art = _collect_art(bool(is_zip), zf, source, art_needed,
                                       art_dir, log=log,
                                       prefix=root_prefix)
                    records = [normalize_monster(a, items_by_id, art)
                               for a in actors]
                else:
                    art = {}
                    records = ([normalize_item(d, art) for d in docs
                                if d.get("type") not in
                                ("Actor", "class", "subclass",
                                 "race", "background")]
                               if pack == "items"
                               else [normalize_spell(d, art) for d in docs
                                     if d.get("type") in ("spell", None)])
                records = [r for r in records if r.get("name")]
                records.sort(key=lambda r: r["name"].lower())
                out_file = os.path.join(out_dir, f"{pack}.json")
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump(records, f, ensure_ascii=False)
                counts[pack] = len(records)
                if log:
                    log(f"{pack}: {len(records)} записей -> {out_file}")
        finally:
            if zf:
                zf.close()

    index = {
        "source": os.path.basename(source),
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": counts,
        "files": {k: f"{k}.json" for k in counts},
    }
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return counts


def refresh_record(record: dict) -> dict:
    """Обновить перевод в записи библиотеки по актуальным словарям.

    Библиотека импортируется один раз и лежит готовыми JSON. Раньше перевод
    «запекался» при импорте: пополнили словарь — а в справочнике всё равно
    старые имена, потому что файл не пересобирали. Теперь при каждой загрузке
    имя пересчитывается из source_name, поэтому правки словарей видны сразу.
    """
    source = record.get("source_name")
    if not source:
        return record
    fresh = translate_name(source)
    if fresh and fresh != "?":
        record["name"] = fresh
    # Описания: если для записи есть готовый русский текст — подставляем его.
    text = translated_description(source)
    if text and record.get("kind") in ("item", "spell"):
        record["description"] = text
    return record


def refresh_library(records: dict) -> dict:
    """Пересчитать переводы во всех разделах библиотеки."""
    for key in ("monsters", "items", "spells"):
        for record in records.get(key) or []:
            refresh_record(record)
    return records


def load_library(data_root: str) -> dict:
    """Загрузить библиотеку: {'monsters': [...], 'items': [...], 'spells': [...], 'index': {...}}."""
    out = {"monsters": [], "items": [], "spells": [], "index": {}}
    out_dir = library_read_dir(data_root)
    for key in ("monsters", "items", "spells"):
        path = os.path.join(out_dir, f"{key}.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                out[key] = json.load(f)
    refresh_library(out)
    index_path = os.path.join(out_dir, "index.json")
    if os.path.isfile(index_path):
        with open(index_path, encoding="utf-8") as f:
            out["index"] = json.load(f)
    return out


def art_path(data_root: str, record: dict):
    """Абсолютный путь к арту записи библиотеки (если он есть).

    Сначала проверяется явный ``img`` в каждом корне библиотеки, затем
    картинка подбирается по имени монстра: ``Adult White Dragon`` ->
    ``dragon/WhiteDragonAdult.webp``. Так работают токены, разложенные по
    папкам типов существ, без переимпорта справочника.
    """
    for root in library_roots(data_root):
        path = token_art.resolve(root, record)
        if path:
            return path
    return None
