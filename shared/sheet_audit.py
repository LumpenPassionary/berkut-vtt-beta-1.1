"""Разбор листа по правилам D&D 5e: что записано словами, но не в полях.

Игрок пишет в «Снаряжении» «воинское оружие (длинный меч)», в заметках —
«у меня есть кинжал», а во вкладке оружия пусто. Мастеру или самому игроку
приходится вручную заводить строку. Этот модуль читает свободный текст
листа и добавляет то, что правила позволяют однозначно восстановить:

* **оружие** — таблица SRD: кубик урона, тип, характеристика, свойства;
* **классовые особенности** — по классу и уровню из ``info`` (Второе дыхание
  воина, Скрытая атака плута…): стандартная кнопка с формулой из правил;
* **расовые черты** — тёмное зрение и прочее, что даёт раса.

Почему отдельный модуль: правила — это данные, а не разметка интерфейса.
Здесь нет Qt и сети, поэтому таблицы можно проверить тестами, а сервер и
клиент считают одинаково.

Что модуль **не** делает: не выдумывает числа, которых нет в правилах, и не
трогает текст листа. Если оружие не из таблицы SRD (артефакт «Меч Зариэль»),
оно останется как есть: догадка испортила бы лист.
"""
from dataclasses import dataclass, field
import re
from typing import Dict, List, Optional

from shared import lss_format as lss

#: Версия разбора: растёт, когда меняются таблицы. В листе хранится, что уже
#: добавлено, поэтому удалённое человеком обратно не приезжает.
#: 2 — строки-действия («Наложение рук» без урона) стали особенностями.
AUDIT_VERSION = 2

# ----------------------------------------------------------------------
# Таблица оружия SRD
# ----------------------------------------------------------------------
# dice — кубик урона из правил; dmg_type — по-русски, как в листах LSS;
# finesse — можно бить Ловкостью; ranged — только Ловкостью; versatile —
# двуручный хват даёт другой кубик (в лист пишем основной).
WEAPONS: Dict[str, dict] = {
    "club": {"ru": "Дубинка", "en": "club", "dice": "1d4",
             "dmg_type": "дробящий", "light": True, "simple": True},
    "dagger": {"ru": "Кинжал", "en": "dagger", "dice": "1d4",
               "dmg_type": "колющий", "finesse": True, "light": True,
               "thrown": True, "simple": True},
    "greatclub": {"ru": "Палица", "en": "greatclub", "dice": "1d8",
                  "dmg_type": "дробящий", "two_handed": True, "simple": True},
    "handaxe": {"ru": "Топорик", "en": "handaxe", "dice": "1d6",
                "dmg_type": "рубящий", "light": True, "thrown": True,
                "simple": True},
    "mace": {"ru": "Булава", "en": "mace", "dice": "1d6",
             "dmg_type": "дробящий", "simple": True},
    "quarterstaff": {"ru": "Боевой посох", "en": "quarterstaff", "dice": "1d6",
                     "dmg_type": "дробящий", "versatile": "1d8",
                     "simple": True},
    "sickle": {"ru": "Серп", "en": "sickle", "dice": "1d4",
               "dmg_type": "рубящий", "light": True, "simple": True},
    "spear": {"ru": "Копьё", "en": "spear", "dice": "1d6",
              "dmg_type": "колющий", "thrown": True, "versatile": "1d8",
              "simple": True},
    "light_crossbow": {"ru": "Лёгкий арбалет", "en": "light crossbow",
                       "dice": "1d8", "dmg_type": "колющий", "ranged": True,
                       "simple": True},
    "dart": {"ru": "Дротик", "en": "dart", "dice": "1d4",
             "dmg_type": "колющий", "finesse": True, "thrown": True,
             "ranged": True, "simple": True},
    "shortbow": {"ru": "Короткий лук", "en": "shortbow", "dice": "1d6",
                 "dmg_type": "колющий", "ranged": True, "simple": True},
    "sling": {"ru": "Праща", "en": "sling", "dice": "1d4",
              "dmg_type": "дробящий", "ranged": True, "simple": True},
    "battleaxe": {"ru": "Боевой топор", "en": "battleaxe", "dice": "1d8",
                  "dmg_type": "рубящий", "versatile": "1d10"},
    "flail": {"ru": "Цеп", "en": "flail", "dice": "1d8",
              "dmg_type": "дробящий"},
    "glaive": {"ru": "Глефа", "en": "glaive", "dice": "1d10",
               "dmg_type": "рубящий", "two_handed": True, "reach": True},
    "greataxe": {"ru": "Секира", "en": "greataxe", "dice": "1d12",
                 "dmg_type": "рубящий", "two_handed": True},
    "greatsword": {"ru": "Двуручный меч", "en": "greatsword", "dice": "2d6",
                   "dmg_type": "рубящий", "two_handed": True},
    "halberd": {"ru": "Алебарда", "en": "halberd", "dice": "1d10",
                "dmg_type": "рубящий", "two_handed": True, "reach": True},
    "longsword": {"ru": "Длинный меч", "en": "longsword", "dice": "1d8",
                  "dmg_type": "рубящий", "versatile": "1d10"},
    "maul": {"ru": "Кувалда", "en": "maul", "dice": "2d6",
             "dmg_type": "дробящий", "two_handed": True},
    "morningstar": {"ru": "Моргенштерн", "en": "morningstar", "dice": "1d8",
                    "dmg_type": "колющий"},
    "pike": {"ru": "Пика", "en": "pike", "dice": "1d10",
             "dmg_type": "колющий", "two_handed": True, "reach": True},
    "rapier": {"ru": "Рапира", "en": "rapier", "dice": "1d8",
               "dmg_type": "колющий", "finesse": True},
    "scimitar": {"ru": "Скимитар", "en": "scimitar", "dice": "1d6",
                 "dmg_type": "рубящий", "finesse": True, "light": True},
    "shortsword": {"ru": "Короткий меч", "en": "shortsword", "dice": "1d6",
                   "dmg_type": "колющий", "finesse": True, "light": True},
    "war_pick": {"ru": "Клевец", "en": "war pick", "dice": "1d8",
                 "dmg_type": "колющий"},
    "warhammer": {"ru": "Боевой молот", "en": "warhammer", "dice": "1d8",
                  "dmg_type": "дробящий", "versatile": "1d10"},
    "whip": {"ru": "Хлыст", "en": "whip", "dice": "1d4",
             "dmg_type": "рубящий", "finesse": True, "reach": True},
    "heavy_crossbow": {"ru": "Тяжёлый арбалет", "en": "heavy crossbow",
                       "dice": "1d10", "dmg_type": "колющий", "ranged": True},
    "longbow": {"ru": "Длинный лук", "en": "longbow", "dice": "1d8",
                "dmg_type": "колющий", "ranged": True},
}

#: Порядок разбора: длинные названия первыми, иначе «Копьё» съело бы
#: «Дротик копьё» и «Длинный меч» — «Меч».
_WEAPON_ORDER = sorted(WEAPONS, key=lambda key: -len(WEAPONS[key]["ru"]))

#: Названия разделов листа, где перечислено снаряжение. Сюда не попадают
#: заметки самого оружия: там описания артефактов, из которых «длинный меч»
#: вытаскивать нельзя.
EQUIPMENT_BLOCKS = ("equipment", "prof", "attacks", "items", "feats",
                    "features", "notes-1", "notes-2", "notes-3", "notes-4",
                    "notes-5", "notes-6", "notes-left", "notes-right",
                    "background", "appearance", "personality", "ideals",
                    "bonds", "flaws")

# ----------------------------------------------------------------------
# Классовые особенности
# ----------------------------------------------------------------------
# kind: heal — лечение с формулой; roll — бросок кубика в чат; info — только
# правило текстом (кнопка не нужна, это чип). formula считается из уровня,
# но только там, где правила действительно дают формулу.
CLASS_FEATURES: List[dict] = [
    {"key": "second_wind", "klass": ("воин", "fighter", "боец"),
     "level": 1, "name": "Второе дыхание", "kind": "heal",
     "formula": "1d10+{level}", "target_mode": "self", "action": "bonus",
     "recharge": "short",
     "text": "Бонусным действием восстановить 1к10 + уровень воина хитов. "
             "Один раз до короткого или долгого отдыха."},
    {"key": "action_surge", "klass": ("воин", "fighter", "боец"),
     "level": 2, "name": "Действие всплеск", "kind": "info",
     "recharge": "short",
     "text": "Один раз до короткого или долгого отдыха — дополнительное "
             "действие в свой ход (с 17 уровня дважды)."},
    {"key": "indomitable", "klass": ("воин", "fighter", "боец"),
     "level": 9, "name": "Неукротимость", "kind": "info",
     "recharge": "long",
     "text": "Перебросить провальный спасбросок, добавив бонус мастерства "
             "(с 13 уровня — два раза, с 17 — три)."},
    {"key": "sneak_attack", "klass": ("плут", "rogue"),
     "level": 1, "name": "Скрытая атака", "kind": "info",
     "formula": "{sneak}d6", "action": "passive",
     # Урон скрытой атаки сервер добавляет сам при попадании, поэтому
     # кнопки-броска нет: отдельный бросок прибавлял бы кубики в чат мимо
     # хитов цели. В подсказке — почему кости уже учтены.
     "text": "Раз за ход дополнительный урон 1к6 за каждые два уровня плута, "
             "если есть преимущество или рядом союзник; с фехтовальным или "
             "дальнобойным оружием и без помехи. Приложение добавляет эти "
             "кости к урону само."},
    {"key": "cunning_action", "klass": ("плут", "rogue"),
     "level": 2, "name": "Хитрое действие", "kind": "info",
     "text": "Бонусным действием Рывок, Отход или Засада."},
    {"key": "rage", "klass": ("варвар", "barbarian"),
     "level": 1, "name": "Ярость", "kind": "info", "recharge": "long",
     "text": "+2 к урону от Силы, сопротивление дробящему, колющему и "
             "рубящему; бонусным действием, 2 раза до долгого отдыха."},
    {"key": "reckless_attack", "klass": ("варвар", "barbarian"),
     "level": 2, "name": "Безрассудная атака", "kind": "info",
     "text": "Преимущество на свои атаки Силы до начала следующего хода; "
             "атаки по вам тоже с преимуществом."},
    {"key": "martial_arts", "klass": ("монах", "monk"),
     "level": 1, "name": "Боевые искусства", "kind": "roll",
     "formula": "{martial}", "action": "bonus",
     "text": "Бонусным действием безоружный удар или удар монашеским "
             "оружием; кубик растёт с уровнем (к4 → к10)."},
    {"key": "ki", "klass": ("монах", "monk"),
     "level": 2, "name": "Ци", "kind": "info", "recharge": "short",
     "text": "Очки ци (уровень монаха) на Шквал ударов, Терпеливую защиту "
             "и Шаг ветра."},
    {"key": "bardic_inspiration", "klass": ("бард", "bard"),
     "level": 1, "name": "Вдохновение барда", "kind": "roll",
     "formula": "{inspiration}", "action": "bonus", "recharge": "long",
     "text": "Бонусным действием дать союзнику кубик вдохновения; он "
             "добавит его к проверке, атаке или спасброску."},
    {"key": "lay_on_hands", "klass": ("паладин", "paladin"),
     "level": 1, "name": "Наложение рук", "kind": "heal_pool",
     "action": "action", "recharge": "long",
     "text": "Запас лечения 5 × уровень паладина. Действием передать касанием "
             "любое количество хитов; 5 хитов из запаса снимают один яд или "
             "болезнь. Запас возвращается на долгом отдыхе."},
    {"key": "divine_smite", "klass": ("паладин", "paladin"),
     "level": 2, "name": "Божественная кара", "kind": "smite",
     "action": "free",
     "text": "При попадании рукопашным оружием потратить ячейку: 2к8 "
             "излучением и +1к8 за каждый круг выше первого (предел 5к8). "
             "По нежити и исчадиям — ещё 1к8 (предел 6к8). Ячейка тратится "
             "только при попадании."},
    {"key": "divine_sense", "klass": ("паладин", "paladin"),
     "level": 3, "name": "Божественное чувство", "kind": "info",
     "recharge": "long",
     "text": "Действием чуять исчадий, небожителей и нежить в радиусе "
             "60 футов до конца следующего хода."},
    {"key": "divine_health", "klass": ("паладин", "paladin"),
     "level": 3, "name": "Божественное здоровье", "kind": "info",
     "text": "Иммунитет к болезням."},
    {"key": "aura_of_protection", "klass": ("паладин", "paladin"),
     "level": 6, "name": "Аура защиты", "kind": "info",
     "text": "Вы и союзники в пределах 10 футов добавляете модификатор "
             "Харизмы к спасброскам (с 18 уровня — 30 футов)."},
    {"key": "paladin_channel", "klass": ("паладин", "paladin"),
     "level": 3, "name": "Божественный канал", "kind": "info",
     "recharge": "short",
     "text": "Изгнание нежити или эффект клятвы; один раз до короткого "
             "отдыха (с 6 уровня — дважды)."},
    {"key": "channel_divinity", "klass": ("жрец", "cleric"),
     "level": 2, "name": "Божественный канал", "kind": "info",
     "recharge": "short",
     "text": "Изгнание нежити и эффект домена; один раз до короткого отдыха "
             "(с 6 уровня — дважды)."},
    {"key": "wild_shape", "klass": ("друид", "druid"),
     "level": 2, "name": "Дикое обличье", "kind": "info", "recharge": "short",
     "text": "Действием превратиться в зверя; два раза до короткого отдыха."},
    {"key": "arcane_recovery", "klass": ("волшебник", "wizard"),
     "level": 1, "name": "Магическое восстановление", "kind": "info",
     "recharge": "long",
     "text": "Раз до долгого отдыха восстановить ячейки суммарным уровнем "
             "до половины уровня волшебника (округление вверх)."},
    {"key": "favored_enemy", "klass": ("следопыт", "ranger"),
     "level": 1, "name": "Избранный враг", "kind": "info",
     "text": "Избранный враг и тип местности: преимущество на проверки "
             "Выживания и Интеллекта о них."},
    {"key": "sorcery_points", "klass": ("чародей", "sorcerer"),
     "level": 2, "name": "Очки чародейства", "kind": "info",
     "text": "Очки (уровень чародея) на метамагию и восстановление ячеек."},
    {"key": "eldritch_invocations", "klass": ("колдун", "warlock"),
     "level": 1, "name": "Таинственные воззвания", "kind": "info",
     "text": "Воззвания колдуна: постоянные магические эффекты."},
    # --- следующие ступени классов: пассивные правила без броска ---
    {"key": "extra_attack_fighter", "klass": ("воин", "fighter", "боец"),
     "level": 5, "name": "Дополнительная атака (воин)", "kind": "info",
     "text": "Действием «Атака» можно бить дважды (с 11 уровня — трижды, "
             "с 20 — четырежды)."},
    {"key": "extra_attack_paladin", "klass": ("паладин", "paladin"),
     "level": 5, "name": "Дополнительная атака (паладин)", "kind": "info",
     "text": "Действием «Атака» можно бить дважды."},
    {"key": "extra_attack_ranger", "klass": ("следопыт", "ranger"),
     "level": 5, "name": "Дополнительная атака (следопыт)", "kind": "info",
     "text": "Действием «Атака» можно бить дважды."},
    {"key": "extra_attack_barbarian", "klass": ("варвар", "barbarian"),
     "level": 5, "name": "Дополнительная атака (варвар)", "kind": "info",
     "text": "Действием «Атака» можно бить дважды."},
    {"key": "extra_attack_monk", "klass": ("монах", "monk"),
     "level": 5, "name": "Дополнительная атака (монах)", "kind": "info",
     "text": "Действием «Атака» можно бить дважды."},
    {"key": "expertise", "klass": ("плут", "rogue", "бард", "bard"),
     "level": 1, "name": "Компетентность", "kind": "info",
     "text": "Двойной бонус мастерства для двух навыков (у барда — с 3 "
             "уровня, для трёх)."},
    {"key": "evasion_rogue", "klass": ("плут", "rogue", "монах", "monk"),
     "level": 7, "name": "Увёртливость", "kind": "info",
     "text": "При успешном спасброске от эффекта с половиной урона урон "
             "не получается вовсе, при провале — половина."},
    {"key": "reliable_talent", "klass": ("плут", "rogue"),
     "level": 11, "name": "Надёжный талант", "kind": "info",
     "text": "В проверках навыков с владением меньше 10 на к20 не выпадает."},
    {"key": "blindsense", "klass": ("плут", "rogue"),
     "level": 14, "name": "Слепое чувство", "kind": "info",
     "text": "Вы ощущаете скрытых и невидимых существ в пределах 10 футов."},
    {"key": "slippery_mind", "klass": ("плут", "rogue"),
     "level": 15, "name": "Скользкий ум", "kind": "info",
     "text": "Владение спасбросками Мудрости."},
    {"key": "elusive", "klass": ("плут", "rogue"),
     "level": 18, "name": "Неуловимость", "kind": "info",
     "text": "Атаки по вам не получают преимущества, пока вы не "
             "недееспособны."},
    {"key": "stroke_of_luck", "klass": ("плут", "rogue"),
     "level": 20, "name": "Удар удачи", "kind": "info", "recharge": "short",
     "text": "Если проверка провалена, можно превратить её в успех раз "
             "до короткого отдыха."},
    {"key": "aura_of_courage", "klass": ("паладин", "paladin"),
     "level": 10, "name": "Аура отваги", "kind": "info",
     "text": "Вы и союзники в 10 футах не могут стать испуганными."},
    {"key": "improved_smite", "klass": ("паладин", "paladin"),
     "level": 11, "name": "Улучшенная кара", "kind": "info",
     "text": "Каждая рукопашная атака оружием наносит +1к8 излучением "
             "без траты ячейки."},
    {"key": "cleansing_touch", "klass": ("паладин", "paladin"),
     "level": 14, "name": "Очищающее касание", "kind": "info",
     "text": "Действием снять одно заклинание с себя или союзника."},
    {"key": "natural_explorer", "klass": ("следопыт", "ranger"),
     "level": 1, "name": "Исследователь природы", "kind": "info",
     "text": "Избранная местность: удвоенный бонус мастерства на проверки "
             "Интеллекта и Мудрости о ней."},
    {"key": "land_stride", "klass": ("следопыт", "ranger"),
     "level": 8, "name": "Шаг по земле", "kind": "info",
     "text": "Трудная местность не замедляет; растения не мешают; "
             "преимущество на спасброски от сдерживающих растений."},
    {"key": "vanish", "klass": ("следопыт", "ranger"),
     "level": 14, "name": "Исчезновение", "kind": "info",
     "text": "Бонусным действием Скрыться; вас нельзя выследить "
             "немагически."},
    {"key": "feral_senses", "klass": ("следопыт", "ranger"),
     "level": 18, "name": "Звериные чувства", "kind": "info",
     "text": "Вы замечаете скрытых и невидимых существ в 30 футах."},
    {"key": "jack_of_all_trades", "klass": ("бард", "bard"),
     "level": 2, "name": "Мастер на все руки", "kind": "info",
     "text": "Половина бонуса мастерства к проверкам без владения."},
    {"key": "song_of_rest", "klass": ("бард", "bard"),
     "level": 2, "name": "Песнь отдыха", "kind": "roll",
     "formula": "{song}", "recharge": "short",
     "text": "Короткий отдых: союзники восстанавливают дополнительный кубик "
             "хитов (к6 на 2 уровне, растёт до к12)."},
    {"key": "countercharm", "klass": ("бард", "bard"),
     "level": 6, "name": "Контрочарование", "kind": "info",
     "text": "Действием дать союзникам преимущество на спасброски от "
             "испуга и очарования."},
    {"key": "magical_secrets", "klass": ("бард", "bard"),
     "level": 10, "name": "Магические тайны", "kind": "info",
     "text": "Выберите два заклинания любых классов (ещё два на 14 и 18)."},
    {"key": "divine_strike", "klass": ("жрец", "cleric"),
     "level": 8, "name": "Божественный удар", "kind": "roll",
     "formula": "1d8",
     "text": "Один раз в ход рукопашная атака наносит +1к8 урона домена "
             "(с 14 уровня — 2к8)."},
    {"key": "fast_movement", "klass": ("варвар", "barbarian", "монах", "monk"),
     "level": 5, "name": "Быстрое передвижение", "kind": "info",
     "text": "Скорость +10 футов без тяжёлого доспеха (у монаха растёт "
             "с уровнем)."},
    {"key": "feral_instinct", "klass": ("варвар", "barbarian"),
     "level": 7, "name": "Звериный инстинкт", "kind": "info",
     "text": "Преимущество на инициативу; в ярости нельзя застать врасплох."},
    {"key": "brutal_critical", "klass": ("варвар", "barbarian"),
     "level": 9, "name": "Жестокий крит", "kind": "info",
     "text": "При крите бросайте ещё один кубик урона оружием."},
    {"key": "relentless_rage", "klass": ("варвар", "barbarian"),
     "level": 11, "name": "Неукротимая ярость", "kind": "info",
     "text": "При падении до 0 хитов в ярости остаётся 1 хит (раз за ярость)."},
    {"key": "stunning_strike", "klass": ("монах", "monk"),
     "level": 5, "name": "Ошеломляющий удар", "kind": "info",
     "text": "1 очко ци: цель делает спасбросок Телосложения или ошеломлена "
             "до конца следующего хода."},
    {"key": "ki_empowered_strikes", "klass": ("монах", "monk"),
     "level": 6, "name": "Удары, усиленные ци", "kind": "info",
     "text": "Безоружные удары считаются магическими."},
    {"key": "stillness_of_mind", "klass": ("монах", "monk"),
     "level": 7, "name": "Спокойствие разума", "kind": "info",
     "text": "Действием снять с себя испуг или очарование."},
    {"key": "purity_of_body", "klass": ("монах", "monk"),
     "level": 10, "name": "Чистота тела", "kind": "info",
     "text": "Иммунитет к болезням и яду."},
    {"key": "pact_magic", "klass": ("колдун", "warlock"),
     "level": 1, "name": "Магия договора", "kind": "info",
     "text": "Ячейки колдуна восстанавливаются на коротком отдыхе; все они "
             "одного круга."},
    {"key": "mystic_arcanum", "klass": ("колдун", "warlock"),
     "level": 11, "name": "Мистический аркан", "kind": "info",
     "text": "Одно заклинание 6 круга, раз в день без ячейки (дальше 7, 8, 9)."},
    {"key": "spell_mastery", "klass": ("волшебник", "wizard"),
     "level": 18, "name": "Мастерство заклинаний", "kind": "info",
     "text": "Выберите заклинание 1 и 2 круга — их можно кастовать без ячейки."},
    {"key": "timeless_body", "klass": ("друид", "druid"),
     "level": 18, "name": "Вечное тело", "kind": "info",
     "text": "Вы стареете медленнее и не можете умереть от старости."},
]

#: Формулы, которые зависят от уровня: одна таблица вместо ветвлений в коде.
MARTIAL_ARTS = ((1, "1d4"), (5, "1d6"), (11, "1d8"), (17, "1d10"))
INSPIRATION_DIE = ((1, "1d6"), (5, "1d8"), (10, "1d10"), (15, "1d12"))
SONG_OF_REST = ((2, "1d6"), (9, "1d8"), (13, "1d10"), (17, "1d12"))


# ----------------------------------------------------------------------
# Расовые черты
# ----------------------------------------------------------------------
RACES: Dict[str, dict] = {
    "dwarf": {"aliases": ("дварф", "гном дварф", "dwarf", "дворф"),
              "traits": [("Тёмное зрение", 60, "info"),
                         ("Дварфская стойкость", 0, "info")],
              "text": "Дварфская стойкость: преимущество на спасброски от яда "
                      "и сопротивление урону ядом."},
    "elf": {"aliases": ("эльф", "elf", "полуэльф"),
            "traits": [("Тёмное зрение", 60, "info"),
                       ("Обострённые чувства", 0, "info")],
            "text": "Эльф: тёмное зрение 60 фт, преимущество на спасброски "
                    "от очарования, магия не усыпляет."},
    "half_orc": {"aliases": ("полуорк", "half-orc", "half orc"),
                 "traits": [("Тёмное зрение", 60, "info"),
                            ("Непреклонное упорство", 0, "info")],
                 "text": "Полуорк: тёмное зрение 60 фт, при падении до 0 хитов "
                         "остаётся 1 хит (раз до долгого отдыха)."},
    "gnome": {"aliases": ("гном", "gnome"),
              "traits": [("Тёмное зрение", 60, "info"),
                         ("Гномья хитрость", 0, "info")],
              "text": "Гном: тёмное зрение 60 фт, преимущество на спасброски "
                      "от магии против Интеллекта, Мудрости и Харизмы."},
    "tiefling": {"aliases": ("тифлинг", "tiefling"),
                 "traits": [("Тёмное зрение", 60, "info"),
                            ("Адское сопротивление", 0, "info")],
                 "text": "Тифлинг: тёмное зрение 60 фт, сопротивление огню."},
    "halfling": {"aliases": ("полурослик", "halfling"),
                 "traits": [("Везучий", 0, "info")],
                 "text": "Полурослик: переброс единицы на d20 в проверке, "
                         "атаке или спасброске."},
    "dragonborn": {"aliases": ("драконорождённый", "dragonborn"),
                   "traits": [("Дыхание дракона", 0, "info")],
                   "text": "Драконорождённый: дыхание дракона и сопротивление "
                           "урону своей стихии."},
    "human": {"aliases": ("человек", "human"), "traits": [], "text": ""},
}


# ----------------------------------------------------------------------
# Таблица доспехов SRD
# ----------------------------------------------------------------------
# base — базовый КД; dex — сколько модификатора Ловкости допускает доспех
# (None — без ограничения, 2 — средний доспех, 0 — тяжёлый). Щит даёт +2 и
# складывается с любым доспехом — так в правилах.
ARMOR: Dict[str, dict] = {
    "padded": {"name": "Стёганый доспех", "base": 11, "dex": None,
               "kind": "light", "bulky": True,
               "aliases": ("стёганый доспех", "стеганый доспех", "стёганый",
                           "padded armor")},
    "leather": {"name": "Кожаный доспех", "base": 11, "dex": None,
                "kind": "light", "bulky": False,
                "aliases": ("кожаный доспех", "кожаная броня", "кожаный",
                            "leather armor")},
    "studded": {"name": "Проклёпанный кожаный доспех", "base": 12,
                "dex": None, "kind": "light", "bulky": False,
                "aliases": ("проклёпанный кожаный доспех",
                            "проклепанный кожаный доспех",
                            "проклёпанный кожаный", "studded leather")},
    "hide": {"name": "Шкурный доспех", "base": 12, "dex": 2,
             "kind": "medium", "bulky": False,
             "aliases": ("шкурный доспех", "шкурная броня", "шкурный",
                         "hide armor")},
    "chain_shirt": {"name": "Кольчужная рубаха", "base": 13, "dex": 2,
                    "kind": "medium", "bulky": False,
                    "aliases": ("кольчужная рубаха", "кольчужная рубашка",
                                "chain shirt")},
    "scale": {"name": "Чешуйчатый доспех", "base": 14, "dex": 2,
              "kind": "medium", "bulky": True,
              "aliases": ("чешуйчатый доспех", "чешуйчатая броня",
                          "чешуйчатый", "scale mail")},
    "breastplate": {"name": "Кираса", "base": 14, "dex": 2,
                    "kind": "medium", "bulky": False,
                    "aliases": ("нагрудник", "кираса", "breastplate")},
    "half_plate": {"name": "Полулаты", "base": 15, "dex": 2,
                   "kind": "medium", "bulky": True,
                   "aliases": ("полулат", "полулаты", "half plate")},
    "ring_mail": {"name": "Кольчатый доспех", "base": 14, "dex": 0,
                  "kind": "heavy", "bulky": True,
                  "aliases": ("кольчатый доспех", "кольчатая броня",
                              "ring mail")},
    "chain_mail": {"name": "Кольчуга", "base": 16, "dex": 0,
                   "kind": "heavy", "bulky": True,
                   "aliases": ("кольчужный доспех", "кольчуга",
                               "chain mail")},
    "splint": {"name": "Пластинчатый доспех", "base": 17, "dex": 0,
               "kind": "heavy", "bulky": True,
               "aliases": ("пластинчатый доспех", "пластинчатая броня",
                           "пластинчатый", "splint armor")},
    "plate": {"name": "Латы", "base": 18, "dex": 0, "kind": "heavy",
              "bulky": True,
              "aliases": ("полные латы", "латы", "plate armor")},
}

ARMOR_ORDER = sorted(ARMOR, key=lambda key: -max(
    len(alias) for alias in ARMOR[key]["aliases"]))

#: Щит — не доспех: его +2 складывается с любым доспехом или без него.
SHIELD_BONUS = 2


# ----------------------------------------------------------------------
# Результаты разбора
# ----------------------------------------------------------------------
@dataclass
class ArmorFinding:
    """Собранный по правилам КД: доспех, Ловкость, щит и разбивка."""

    name: str
    base: int
    dex: int
    shield: int
    total: int
    breakdown: str
    kind: str = ""
    found: bool = True


@dataclass
class WeaponFinding:
    """Оружие, найденное в тексте листа, но не в списке оружия."""

    key: str
    name: str
    dice: str
    dmg_type: str
    ability: str
    properties: List[str] = field(default_factory=list)
    snippet: str = ""


@dataclass
class FeatureFinding:
    """Классовая особенность, которой ещё нет в листе."""

    key: str
    name: str
    klass: str
    level: int
    kind: str
    formula: str = ""
    action: str = ""
    recharge: str = ""
    text: str = ""
    #: Имя строки оружия, которая была этим действием: её надо убрать,
    #: иначе вместо кнопки останется пустая атака без урона.
    replaces_weapon: str = ""


@dataclass
class TraitFinding:
    """Раса персонажа и её черты."""

    key: str
    race: str
    name: str
    darkvision: int = 0
    kind: str = "info"
    text: str = ""


@dataclass
class Report:
    """Что найдено и что из этого ещё не записано в лист."""

    weapons: List[WeaponFinding] = field(default_factory=list)
    features: List[FeatureFinding] = field(default_factory=list)
    traits: List[TraitFinding] = field(default_factory=list)
    armor: Optional[ArmorFinding] = None

    @property
    def changed(self) -> bool:
        return bool(self.weapons or self.features or self.traits)


# ----------------------------------------------------------------------
# Чтение листа
# ----------------------------------------------------------------------
def _cell(value):
    """Значение ячейки LSS: ``{"value": …}`` или как есть."""
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def sheet_text(sheet: dict) -> str:
    """Свободный текст листа, где перечислено снаряжение и заметки."""
    if not isinstance(sheet, dict):
        return ""
    parts = []
    texts = sheet.get("text") if isinstance(sheet.get("text"), dict) else {}
    for key in texts:
        name = str(key)
        if name not in EQUIPMENT_BLOCKS and not name.startswith("notes"):
            continue
        block = lss.text_block(sheet, name)
        if block:
            parts.append(block)
    return "\n".join(parts)


def existing_weapons(sheet: dict) -> set:
    """Названия оружия, уже записанного в ``weaponsList`` (без регистра)."""
    names = set()
    weapons = (sheet or {}).get("weaponsList")
    if not isinstance(weapons, list):
        return names
    for weapon in weapons:
        if not isinstance(weapon, dict):
            continue
        name = str(_cell(weapon.get("name")) or "").strip()
        if name:
            names.add(_normalize(name))
    return names


def applied_weapons(sheet: dict) -> set:
    """Оружие, добавленное разбором раньше: удалённое человеком не возвращаем."""
    block = (sheet or {}).get("vtt") if isinstance(sheet, dict) else None
    audit = (block or {}).get("audit") if isinstance(block, dict) else None
    names = (audit or {}).get("weapons") if isinstance(audit, dict) else None
    return {_normalize(name) for name in (names or []) if str(name).strip()}


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().casefold())


def _stat_mod(sheet: dict, ability: str) -> int:
    """Модификатор характеристики листа (без обращения к приватному коду)."""
    entry = (sheet.get("stats") or {}).get(ability)
    if isinstance(entry, dict):
        entry = entry.get("score")
    try:
        return lss.modifier(entry)
    except (TypeError, ValueError):
        return 0


def _ability_for(rules: dict, sheet: dict) -> str:
    """Какой характеристикой бьёт оружие по правилам.

    Фехтовальное (finesse) — лучшая из Силы и Ловкости, дальнобойное —
    Ловкость, остальное — Сила. Это выбор правила, а не догадка.
    """
    if rules.get("ranged"):
        return "dex"
    if rules.get("finesse"):
        return "dex" if _stat_mod(sheet, "dex") >= _stat_mod(sheet, "str") else "str"
    return "str"


def _properties(rules: dict) -> List[str]:
    labels = []
    if rules.get("finesse"):
        labels.append("фехтовальное")
    if rules.get("light"):
        labels.append("лёгкое")
    if rules.get("two_handed"):
        labels.append("двуручное")
    if rules.get("thrown"):
        labels.append("метательное")
    if rules.get("reach"):
        labels.append("досягаемость 10 фт")
    if rules.get("ranged"):
        labels.append("дальнобойное")
    if rules.get("versatile"):
        labels.append(f"универсальное {rules['versatile']}")
    return labels


def _snippet(text: str, alias: str) -> str:
    """Строка листа, в которой упомянуто оружие (для подсказки)."""
    for line in text.splitlines():
        if alias in line.casefold():
            return line.strip()[:120]
    return ""


def find_weapons(sheet: dict) -> List[WeaponFinding]:
    """Оружие из текста листа, которого нет во вкладке оружия."""
    text = sheet_text(sheet)
    if not text.strip():
        return []
    # Рабочая копия: найденное название вычёркиваем, чтобы «Боевой молот»
    # не давал вторую строку «Молот», а «Длинный лук» — «Лук».
    searchable = text.casefold()
    known = existing_weapons(sheet) | applied_weapons(sheet)
    found: List[WeaponFinding] = []
    for key in _WEAPON_ORDER:
        rules = WEAPONS[key]
        alias = rules["ru"]
        if _normalize(alias) in known:
            continue
        pattern = r"(?<![а-яёa-z])" + re.escape(alias.casefold()) + r"(?![а-яёa-z])"
        match = re.search(pattern, searchable)
        if match is None:
            continue
        searchable = searchable.replace(match.group(0), " " * len(match.group(0)))
        # Строка — только для подсказки: в одной строке снаряжения бывает
        # несколько предметов («Кувалда, рапира»).
        line = _snippet(text, alias.casefold())
        line_folded = line.casefold()
        # Описание уже записанного артефакта («Меч Зариэль — этот длинный
        # меч…») — не повод заводить строку «Длинный меч».
        if any(name in line_folded for name in known
               if name and name != _normalize(alias) and len(name) > 3):
            continue
        # Длинный абзац — рассказ, а не список снаряжения.
        if len(line) > 200:
            continue
        found.append(WeaponFinding(
            key=key, name=alias, dice=rules["dice"],
            dmg_type=rules["dmg_type"],
            ability=_ability_for(rules, sheet),
            properties=_properties(rules), snippet=line))
    return found


def _level(sheet: dict) -> int:
    try:
        return max(0, int(lss.character_level(sheet)))
    except (TypeError, ValueError):
        return 0


def _class_name(sheet: dict) -> str:
    info = (sheet or {}).get("info") or {}
    return str(_cell(info.get("charClass")) or "").strip()


def _class_matches(entry: dict, klass: str) -> bool:
    folded = klass.casefold()
    return any(alias in folded for alias in entry["klass"])


def _scale(level: int, table) -> str:
    """Кубик из таблицы по уровню (берём последнюю подходящую ступень)."""
    value = table[0][1]
    for step, dice in table:
        if level >= step:
            value = dice
    return value


def _formula(entry: dict, level: int) -> str:
    template = entry.get("formula") or ""
    if not template:
        return ""
    if "{sneak}" in template:
        # Скрытая атака: 1к6 на 1 уровне и ещё 1к6 за каждые два уровня.
        return template.replace("{sneak}", str(max(1, (level + 1) // 2)))
    if "{martial}" in template:
        return template.replace("{martial}", _scale(level, MARTIAL_ARTS))
    if "{inspiration}" in template:
        return template.replace("{inspiration}", _scale(level, INSPIRATION_DIE))
    if "{song}" in template:
        return template.replace("{song}", _scale(level, SONG_OF_REST))
    return template.replace("{level}", str(max(1, level)))


def existing_features(sheet: dict) -> set:
    block = (sheet or {}).get("vtt") if isinstance(sheet, dict) else None
    features = (block or {}).get("features") if isinstance(block, dict) else None
    keys = set()
    for feature in features or []:
        if isinstance(feature, dict) and feature.get("key"):
            keys.add(str(feature["key"]))
    return keys


def _feature_mentioned(sheet: dict, entry: dict) -> bool:
    """Название особенности прямо упомянуто в заметках/снаряжении.

    Так ловится случай «у воина в заметках написано «Второе дыхание», а
    класс в шапке листа не заполнен».
    """
    text = sheet_text(sheet).casefold()
    if not text:
        return False
    alias = str(entry.get("name") or "").casefold()
    if not alias:
        return False
    pattern = r"(?<![а-яёa-z])" + re.escape(alias) + r"(?![а-яёa-z])"
    return re.search(pattern, text) is not None


def _weapon_has_damage(weapon: dict) -> bool:
    """Есть ли у строки оружия настоящий кубик урона.

    В листах LSS классовые действия пишут строкой оружия: «Наложение рук»,
    «Божественное чувство» с уроном «-». Это не оружие — у него нет
    кубика, и бить им нельзя.
    """
    raw = str(lss._cell_value((weapon or {}).get("dmg")) or "").strip()
    raw = raw.casefold().replace("ё", "е")
    return bool(raw) and raw not in ("-", "—", "–", "0", "нет", "—”")


def pseudo_action_weapons(sheet: dict) -> Dict[str, dict]:
    """Строки-действия в ``weaponsList``: имя -> запись оружия.

    У паладина «Наложение рук» и «Божественное чувство» годами жили как
    оружие с уроном «-». Это готовые кнопки особенностей: аудит переносит
    их в ``vtt.features``, иначе нажатие на такую строку открывало диалог
    атаки без урона.
    """
    names = {_normalize(entry.get("name")): entry["key"]
             for entry in CLASS_FEATURES}
    out = {}
    weapons = (sheet or {}).get("weaponsList")
    if not isinstance(weapons, list):
        return out
    for weapon in weapons:
        if not isinstance(weapon, dict):
            continue
        name = _normalize(str(lss._cell_value(weapon.get("name")) or ""))
        if name and name in names and not _weapon_has_damage(weapon):
            out[name] = weapon
    return out


def find_features(sheet: dict) -> List[FeatureFinding]:
    """Особенности класса: по уровню из правил и по упоминанию в тексте.

    Уровень берётся у конкретного класса, а не у всей суммы: у следопыта
    5 / плутa 3 / воина 3 «Второе дыхание» лечит 1к10+3, а скрытая атака
    даёт 2к6 — а не 1к10+11 и 6к6 от общего 11 уровня.
    """
    from shared import class_features
    klass = _class_name(sheet)
    level = _level(sheet)
    known = existing_features(sheet)
    # Строки оружия с настоящим уроном важнее особенности: если игрок сам
    # назвал меч «Божественной карой», это его оружие, а не дубль кнопки.
    # Строки без урона наоборот переносим в особенности.
    as_weapon = existing_weapons(sheet)
    pseudo = pseudo_action_weapons(sheet)
    # Одинаковые названия у разных классов (Божественный канал есть и у
    # жреца, и у паладина, и у плута «Хитрое действие» созвучно) — берём
    # одну запись, иначе в листе появлялись две кнопки с одной подписью.
    seen_names = {_normalize(str(f.get("name") or ""))
                  for f in ((sheet.get("vtt") or {}).get("features") or [])
                  if isinstance(f, dict)}
    out = []
    for entry in CLASS_FEATURES:
        if entry["key"] in known:
            continue
        name_key = _normalize(entry.get("name"))
        if name_key in as_weapon and name_key not in pseudo:
            continue
        class_key, class_level = class_features.best_class(
            sheet, entry.get("klass"))
        by_class = bool(class_key) and class_level > 0
        # Класс известен — уровень решает: «Аура защиты» не появляется
        # у паладина 2 уровня, даже если о ней написано в заметках.
        if by_class and class_level < int(entry.get("level") or 1):
            continue
        # Класса в листе нет — особенность не показываем. Исключение —
        # прямое упоминание в заметках, когда класс в шапке не заполнен:
        # иначе любое упоминание «Божественного канала» в описании
        # паладина давало бы вторую такую же кнопку от жреца.
        if class_key:
            if not by_class:
                if klass or not _feature_mentioned(sheet, entry):
                    continue
                class_level = level or int(entry.get("level") or 1)
                by_class = True
        elif klass or not _feature_mentioned(sheet, entry):
            continue
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        out.append(FeatureFinding(
            key=entry["key"], name=entry["name"],
            klass=klass if by_class else "",
            level=int(entry.get("level") or 1), kind=entry["kind"],
            formula=_formula(entry, class_level
                             or level or int(entry.get("level") or 1)),
            action=entry.get("action", ""),
            recharge=entry.get("recharge", ""),
            text=entry.get("text", ""),
            replaces_weapon=(name_key if name_key in pseudo else "")))
    return out


def race_traits(sheet: dict) -> List[TraitFinding]:
    """Расовые черты из ``info.race``: тёмное зрение и подсказки правил."""
    info = (sheet or {}).get("info") or {}
    race = str(_cell(info.get("race")) or "").strip()
    if not race:
        return []
    folded = race.casefold()
    known = existing_features(sheet)
    out = []
    for key, entry in RACES.items():
        if not any(alias in folded for alias in entry["aliases"]):
            continue
        if not entry["traits"]:
            return []
        # Одна запись на расу: кнопка-чип с правилами, а тёмное зрение
        # дополнительно записывается в лист числом.
        trait_key = f"race:{key}"
        darkvision = 0
        for name, feet, kind in entry["traits"]:
            if feet:
                darkvision = max(darkvision, feet)
        if trait_key not in known:
            out.append(TraitFinding(
                key=trait_key, race=race, name=f"{race}: черты расы",
                darkvision=darkvision, text=entry.get("text", "")))
        return out
    return []


def gear_text(sheet: dict) -> str:
    """Только снаряжение: там носят доспех и щит.

    В заметках может быть «латы остались в телеге» — это не надетый
    доспех, поэтому КД считается по снаряжению, а заметки — запасной
    вариант, если снаряжение пустое.
    """
    if not isinstance(sheet, dict):
        return ""
    texts = sheet.get("text") if isinstance(sheet.get("text"), dict) else {}
    parts = []
    for key in ("equipment", "items", "prof", "attacks"):
        if key in texts:
            block = lss.text_block(sheet, key)
            if block:
                parts.append(block)
    return "\n".join(parts)


def _find_armor_entry(text: str):
    """Первый доспех из таблицы SRD в тексте листа."""
    searchable = str(text or "").casefold()
    for key in ARMOR_ORDER:
        for alias in sorted(ARMOR[key]["aliases"], key=len, reverse=True):
            pattern = (r"(?<![а-яёa-z])" + re.escape(alias)
                       + r"(?![а-яёa-z])")
            if re.search(pattern, searchable):
                return key
    return None


def _has_word(text: str, word: str) -> bool:
    pattern = r"(?<![а-яёa-z])" + re.escape(word) + r"(?![а-яёa-z])"
    return re.search(pattern, str(text or "").casefold()) is not None


def _term(value: int, label: str) -> str:
    """Слогаемое в разбивке КД: «+ 2 Ловкость» или «− 1 Ловкость»."""
    if value >= 0:
        return f"+ {value} {label}"
    return f"− {abs(value)} {label}"


def armor_profile(sheet: dict) -> ArmorFinding:
    """КД по правилам 5e: доспех + Ловкость (с ограничением) + щит.

    Без доспехов: 10 + Ловкость; у монаха — ещё Мудрость, у варвара —
    Телосложение (Защита без доспехов). Щит прибавляется всегда, в том
    числе к бездоспешному КД.
    """
    sheet = sheet if isinstance(sheet, dict) else {}
    gear = gear_text(sheet)
    key = _find_armor_entry(gear) or _find_armor_entry(sheet_text(sheet))
    dex_mod = _stat_mod(sheet, "dex")
    vitality = sheet.get("vitality") or {}
    has_shield = (_cell(vitality.get("shield")) is True
                  or _has_word(gear, "щит"))
    shield = SHIELD_BONUS if has_shield else 0
    klass = _class_name(sheet).casefold()
    if key:
        entry = ARMOR[key]
        cap = entry["dex"]
        if cap is None:
            dex = dex_mod
        elif cap == 0:
            # Тяжёлый доспех не даёт прибавлять Ловкость вовсе: даже
            # отрицательный модификатор не вычитается — так в правилах.
            dex = 0
        else:
            dex = min(dex_mod, cap)
        total = entry["base"] + dex + shield
        expr = f"{entry['base']} доспех"
        if cap == 0 and dex_mod:
            expr += " (Ловкость не входит)"
        elif dex:
            suffix = " (предел доспеха)" if cap is not None and dex_mod > cap else ""
            expr += " " + _term(dex, "Ловкость") + suffix
        if shield:
            expr += " " + _term(shield, "щит")
        return ArmorFinding(name=entry["name"], base=entry["base"], dex=dex,
                            shield=shield, total=total, breakdown=expr,
                            kind=entry["kind"])
    extra = 0
    extra_label = ""
    if "монах" in klass or "monk" in klass:
        extra = _stat_mod(sheet, "wis")
        extra_label = "Мудрость"
        name = "Без доспехов (Защита без доспехов)"
    elif "варвар" in klass or "barbarian" in klass:
        extra = _stat_mod(sheet, "con")
        extra_label = "Телосложение"
        name = "Без доспехов (Защита без доспехов)"
    else:
        name = "Без доспехов"
    expr = "10" if not extra_label else "10 (Защита без доспехов)"
    expr += " " + _term(dex_mod, "Ловкость")
    if extra_label:
        expr += " " + _term(extra, extra_label)
    if shield:
        expr += " " + _term(shield, "щит")
    return ArmorFinding(name=name, base=10, dex=dex_mod, shield=shield,
                        total=10 + dex_mod + extra + shield,
                        breakdown=expr, kind="unarmored", found=False)


def audit(sheet: dict) -> Report:
    """Полный разбор листа: оружие, особенности, расы, доспех."""
    if not isinstance(sheet, dict):
        return Report()
    return Report(weapons=find_weapons(sheet),
                  features=find_features(sheet),
                  traits=race_traits(sheet),
                  armor=armor_profile(sheet))


# ----------------------------------------------------------------------
# Запись найденного в лист
# ----------------------------------------------------------------------
def apply_weapon(sheet: dict, finding: WeaponFinding) -> dict:
    """Добавить оружие в ``weaponsList`` в формате LSS."""
    weapon = lss.add_weapon(sheet, finding.name, finding.dice)
    weapon["ability"] = finding.ability
    weapon["isProf"] = True
    weapon["dmgType"] = {"value": finding.dmg_type}
    if finding.properties:
        lss.set_weapon_notes(
            weapon, "Свойства: " + ", ".join(finding.properties)
            + ". Добавлено разбором листа по правилам 5e.")
    else:
        lss.set_weapon_notes(
            weapon, "Добавлено разбором листа по правилам 5e.")
    return weapon


def apply_feature(sheet: dict, finding: FeatureFinding) -> dict:
    """Записать особенность в ``vtt.features`` — оттуда её берут кнопки."""
    block = sheet.setdefault("vtt", {})
    features = block.setdefault("features", [])
    record = {
        "key": finding.key,
        "name": finding.name,
        "class": finding.klass,
        "level": finding.level,
        "kind": finding.kind,
        "formula": finding.formula,
        "action": finding.action,
        "recharge": finding.recharge,
        "text": finding.text,
        "source": "rules",
    }
    features.append(record)
    if finding.key == "lay_on_hands":
        # Запас хитов нужен кнопке сразу: без него она показала бы «0/50»
        # и открыла бы окно с нулём. Храним только расход — максимум
        # считается из уровня паладина.
        pool = block.setdefault("lay_on_hands", {})
        if isinstance(pool, dict):
            pool.setdefault("used", 0)
    return record


def apply_trait(sheet: dict, finding: TraitFinding) -> dict:
    """Записать расовую черту: тёмное зрение — числом, правила — текстом."""
    if finding.darkvision:
        vitality = sheet.setdefault("vitality", {})
        current = _cell(vitality.get("darkvision")) or 0
        try:
            current = int(current)
        except (TypeError, ValueError):
            current = 0
        if current < finding.darkvision:
            vitality["darkvision"] = {"value": finding.darkvision}
    block = sheet.setdefault("vtt", {})
    features = block.setdefault("features", [])
    record = {
        "key": finding.key,
        "name": finding.name,
        "class": finding.race,
        "level": 0,
        "kind": "info",
        "formula": "",
        "action": "",
        "recharge": "",
        "text": finding.text,
        "source": "race",
    }
    features.append(record)
    return record


def apply_armor(sheet: dict, finding: ArmorFinding):
    """Записать КД по правилам, не стирая число, которое вписал человек.

    Если в листе уже стоит готовое число, оно главнее: там могут быть
    магические предметы и домашние правила. Тогда разбор только сохраняет
    разбивку в ``vtt.armor`` и предупреждает, если числа расходятся.

    Возвращает ``(изменилось, замечание для чата, КД записан в лист)``.
    """
    vitality = sheet.setdefault("vitality", {})
    block = sheet.setdefault("vtt", {})
    record = {"name": finding.name, "base": finding.base,
              "dex": finding.dex, "shield": finding.shield,
              "total": finding.total, "breakdown": finding.breakdown}
    stored = block.get("armor") if isinstance(block.get("armor"), dict) else {}
    changed = stored != record
    raw = _cell(vitality.get("ac"))
    empty = raw is None or raw == "" or (
        isinstance(raw, (int, float)) and not isinstance(raw, bool)
        and int(raw) == 0)
    current = _ac_number(sheet)
    # Число, которое записали мы сами (оно равно сохранённой разбивке),
    # должно меняться вместе с Ловкостью и доспехом — иначе после правки
    # характеристики в листе остался бы старый КД.
    ours = (current is not None and stored.get("total") is not None
            and current == int(stored.get("total")))
    if empty or (ours and current != finding.total):
        vitality["ac"] = {"value": finding.total}
        block["armor"] = record
        return True, "", True
    block["armor"] = record
    note = ""
    if finding.found and current is not None and current != finding.total:
        note = (f"КД по доспехам: {finding.total} ({finding.breakdown}), "
                f"в листе {current} — оставил число из листа")
    return changed, note, False


def _ac_number(sheet: dict):
    """КД листа числом, без правки листа (``None`` — не посчитать)."""
    raw = _cell((sheet.get("vitality") or {}).get("ac"))
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.lstrip("-+").isdigit():
            return int(text)
        return lss.numeric_formula(text, sheet)
    return None


def apply(sheet: dict, report: Report) -> dict:
    """Записать всё найденное. Возвращает короткую сводку для чата."""
    summary = {"weapons": [], "features": [], "traits": [], "armor": None,
               "notes": [], "changed": False}
    if not isinstance(sheet, dict):
        return summary
    profile = report.armor or armor_profile(sheet)
    _armor_changed, note, ac_written = apply_armor(sheet, profile)
    # Сама запись разбивки в vtt.armor — не повод рассылать лист: она
    # пересчитывается при каждом показе. Рассылаем, только если мы
    # действительно записали КД в пустое поле.
    if ac_written:
        summary["armor"] = profile
    if note:
        summary["notes"].append(note)
    if report.changed:
        block = sheet.setdefault("vtt", {})
        audit_block = block.setdefault("audit", {})
        added_weapons = list(audit_block.get("weapons") or [])
        for finding in report.weapons:
            apply_weapon(sheet, finding)
            summary["weapons"].append(finding.name)
            if finding.name not in added_weapons:
                added_weapons.append(finding.name)
        pseudo = pseudo_action_weapons(sheet)
        for finding in report.features:
            if finding.replaces_weapon:
                # Строка-действие больше не «оружие»: без урона она
                # открывала диалог атаки, где нечего бросать.
                weapon = pseudo.get(finding.replaces_weapon)
                if weapon is not None and lss.remove_weapon(sheet, weapon):
                    summary["notes"].append(
                        f"«{finding.name}» — особенность, а не оружие")
            apply_feature(sheet, finding)
            summary["features"].append(finding.name)
        for finding in report.traits:
            apply_trait(sheet, finding)
            summary["traits"].append(finding.name)
        audit_block["weapons"] = added_weapons
        audit_block["version"] = AUDIT_VERSION
    summary["changed"] = bool(summary["weapons"] or summary["features"]
                              or summary["traits"] or ac_written)
    return summary


def audit_and_apply(sheet: dict) -> dict:
    """Один вызов для клиента: разобрать лист и записать недостающее."""
    report = audit(sheet)
    return apply(sheet, report)


def summary_line(summary: dict) -> str:
    """Текст для чата: что программа добавила в лист сама."""
    parts = []
    if summary.get("weapons"):
        parts.append("оружие: " + ", ".join(f"«{name}»"
                                            for name in summary["weapons"]))
    if summary.get("features"):
        parts.append("особенности: " + ", ".join(summary["features"]))
    if summary.get("traits"):
        parts.append("раса: " + ", ".join(summary["traits"]))
    armor = summary.get("armor")
    if armor is not None:
        total = getattr(armor, "total", None)
        breakdown = getattr(armor, "breakdown", "")
        parts.append(f"КД по доспехам: {total} ({breakdown})")
    line = ("Лист разобран по правилам 5e — добавлено " + "; ".join(parts)
            if parts else "")
    notes = [str(note) for note in (summary.get("notes") or []) if note]
    if notes:
        line = (line + ". " if line else "") + ". ".join(notes)
    return line
