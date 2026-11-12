"""Ядро боя D&D 5e: инициатива, атака, спасбросок, урон и состояния токена.

Чистая логика: без Qt и без сети. Сервер вызывает эти функции, поэтому
результат боя считается один раз и одинаково видно всем.

Правила (как в D&D 5e / Foundry dnd5e):
  * инициатива = d20 + модификатор Ловкости;
  * атака попадает, если d20 + бонус >= КД цели; natural 20 — всегда попадание
    и критическое попадание, natural 1 — всегда промах;
  * при критическом попадании кости урона бросаются вдвое (модификаторы — нет);
  * спасбросок: d20 + бонус >= Сл (DC) — успех.
"""
import random as _random
import re

from shared import conditions as conditions_rules
from shared import dice as dice_ext

#: Диапазон значений, который мы принимаем от клиентов (защита от мусора).
MAX_BONUS = 40
MIN_BONUS = -10
MAX_HP = 9999
#: Хиты не уходят в минус: на нуле персонаж начинает кидать спасброски
#: от смерти, а не «умирает в −12».
MIN_HP = 0

CONDITION_LIMIT = 20

# --- Спасброски от смерти (D&D 5e, домашнее правило стола) -------------
#: Сложность спасброска от смерти: d20 без модификаторов, 10 и выше — успех.
DEATH_SAVE_DC = 10
#: Провалов, после которых персонаж мёртв.
DEATH_SAVE_FAILURES = 3
#: Успехов, после которых персонаж стабилизирован.
DEATH_SAVE_SUCCESSES = 3
#: Сколько кружков максимум: сначала три, при нерешительном счёте — до пяти.
DEATH_SAVE_SLOTS = 5
DEATH_SAVE_MIN_SLOTS = 3

DEATH_SUCCESS = "success"
DEATH_FAILURE = "failure"
#: Пустой кружок: спасбросок ещё не брошен.
DEATH_EMPTY = ""
_DEATH_VALUES = (DEATH_EMPTY, DEATH_SUCCESS, DEATH_FAILURE)


class CombatError(ValueError):
    """Неверные входные данные боевой команды."""


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _as_int(value, name, lo=None, hi=None):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CombatError(f"{name}: нужно целое число") from exc
    if lo is not None and number < lo:
        raise CombatError(f"{name}: не меньше {lo}")
    if hi is not None and number > hi:
        raise CombatError(f"{name}: не больше {hi}")
    return number


# ----------------------------------------------------------------------
# Броски
# ----------------------------------------------------------------------

def d20(mode=None, rng=None) -> dict:
    """Бросок d20 с преимуществом/помехой. Возвращает {'kept', 'rolls', 'mode'}."""
    if mode not in (None, "adv", "dis"):
        raise CombatError(f"неизвестный режим броска: {mode!r}")
    rnd = rng or _random
    rolls = [rnd.randint(1, 20)]
    if mode:
        rolls.append(rnd.randint(1, 20))
        kept = max(rolls) if mode == "adv" else min(rolls)
    else:
        kept = rolls[0]
    return {"kept": kept, "rolls": rolls, "mode": mode}


def _mode_of(advantage=False, disadvantage=False):
    """Преимущество и помеха гасят друг друга — как в 5e."""
    if advantage and not disadvantage:
        return "adv"
    if disadvantage and not advantage:
        return "dis"
    return None


def roll_initiative(dex_mod: int, advantage: bool = False, rng=None) -> dict:
    """Инициатива: d20 + модификатор Ловкости.

    Ключи 'd20' и 'roll' дублируют одно и то же число: первый читают тесты,
    второй — сервер. Так одна функция обслуживает оба вызова.
    """
    dex_mod = _as_int(dex_mod, "модификатор Ловкости", -5, 10)
    roll = d20("adv" if advantage else None, rng)
    return {
        "d20": roll["kept"],
        "roll": roll["kept"],
        "kept": roll["kept"],
        "mod": dex_mod,
        "total": roll["kept"] + dex_mod,
        "mode": roll["mode"],
    }


def attack_roll(bonus: int, target_ac: int, mode=None, rng=None) -> dict:
    """Бросок атаки против КД цели.

    Возвращает {'hit', 'crit', 'miss_auto', 'd20', 'total', 'bonus', 'target_ac'}.
    """
    bonus = _as_int(bonus, "бонус атаки", MIN_BONUS, MAX_BONUS)
    target_ac = _as_int(target_ac, "КД цели", 0, 40)
    roll = d20(mode, rng)
    natural = roll["kept"]
    total = natural + bonus
    crit = natural == 20
    auto_miss = natural == 1
    hit = crit or (not auto_miss and total >= target_ac)
    return {
        "hit": bool(hit),
        "crit": bool(crit),
        "miss_auto": bool(auto_miss),
        "d20": natural,
        "rolls": roll["rolls"],
        "mode": mode,
        "total": total,
        "bonus": bonus,
        "target_ac": target_ac,
    }


def saving_throw(bonus: int, dc: int, mode=None, rng=None) -> dict:
    """Спасбросок: d20 + бонус против Сл.

    В 5e натуральные 20 и 1 не дают автоуспеха/автопровала на спасбросках:
    считается только сумма. Правило «20 всегда» относится к атакам и
    смертельным спасброскам, а не к обычным.
    """
    bonus = _as_int(bonus, "бонус спасброска", MIN_BONUS, MAX_BONUS)
    dc = _as_int(dc, "Сл спасброска", 1, 40)
    roll = d20(mode, rng)
    natural = roll["kept"]
    total = natural + bonus
    success = total >= dc
    return {
        "success": bool(success),
        "d20": natural,
        "rolls": roll["rolls"],
        "mode": mode,
        "total": total,
        "bonus": bonus,
        "dc": dc,
    }


def damage_roll(formula: str, crit: bool = False, rng=None) -> dict:
    """Бросок урона. При критическом попадании кости бросаются дважды.

    Модификаторы (+3 и т.п.) при крите НЕ удваиваются — как в 5e.
    Возвращает {'formula', 'total', 'breakdown'}.
    """
    try:
        terms = dice_ext.parse_formula(formula)
    except dice_ext.DiceError as exc:
        raise CombatError(str(exc)) from exc
    rnd = rng or _random
    total = 0
    parts = []
    formula_parts = []
    for term in terms:
        if term[0] == "mod":
            total += term[1]
            parts.append(f"{term[1]:+d}")
            formula_parts.append(f"{term[1]:+d}")
            continue
        _, n, sides = term
        n_eff = n * 2 if crit else n
        rolls = [rnd.randint(1, sides) for _ in range(n_eff)]
        total += sum(rolls)
        parts.append(f"{n_eff}d{sides}({', '.join(map(str, rolls))})")
        # Формулу собираем ту, что реально бросалась: «2d6» при крите -> «4d6».
        formula_parts.append(f"{n_eff}d{sides}")
    text = " ".join(parts)
    if crit:
        text = "крит: " + text
    rolled_formula = "".join(formula_parts) or formula
    return {"formula": rolled_formula, "total": total, "breakdown": text}


# ----------------------------------------------------------------------
# Урон и лечение
# ----------------------------------------------------------------------

def apply_hp_change(hp, hp_max, amount: int, heal: bool):
    """Новое значение хитов. Урон не опускает ниже нуля, лечение — выше максимума.

    hp, hp_max могут быть None (старые токены без статблока): тогда лечение
    ограничено только MAX_HP.
    """
    amount = abs(int(amount))
    current = int(hp) if hp is not None else 0
    if heal:
        limit = int(hp_max) if hp_max else MAX_HP
        return min(limit, current + amount)
    return max(MIN_HP, current - amount)


def resistance_factor(damage_type: str, resistances, immunities, vulnerabilities) -> float:
    """Множитель урона по типу: 0 — иммунитет, 0.5 — сопротивление, 2 — уязвимость."""
    dtype = (damage_type or "").casefold()
    if dtype and dtype in {str(x).casefold() for x in immunities or []}:
        return 0.0
    if dtype and dtype in {str(x).casefold() for x in vulnerabilities or []}:
        return 2.0
    if dtype and dtype in {str(x).casefold() for x in resistances or []}:
        return 0.5
    return 1.0


# ----------------------------------------------------------------------
# Состояния токена (условия на карте, а не только на листе)
# ----------------------------------------------------------------------

def token_conditions(token: dict) -> list:
    """Список состояний токена, создаётся по требованию."""
    conds = token.get("conditions")
    if conds is None:
        conds = []
        token["conditions"] = conds
    return conds


def add_condition(token: dict, name: str, rounds=None) -> dict:
    name = str(name or "").strip()[:40]
    if not name:
        raise CombatError("пустое состояние")
    conds = token_conditions(token)
    for cond in conds:
        if cond.get("name") == name:
            cond["rounds"] = rounds
            return cond
    if len(conds) >= CONDITION_LIMIT:
        raise CombatError("слишком много состояний на токене")
    if rounds is not None:
        rounds = _as_int(rounds, "раунды", 1, 999)
    entry = {"name": name, "rounds": rounds}
    conds.append(entry)
    return entry


def remove_condition(token: dict, name: str) -> bool:
    conds = token_conditions(token)
    for i, cond in enumerate(conds):
        if cond.get("name") == name:
            conds.pop(i)
            return True
    return False


def tick_conditions(token: dict, rounds: int = 1) -> list:
    """Прошло раундов: временные состояния уменьшаются, истёкшие уходят.

    Возвращает список имён состояний, которые истекли.
    """
    conds = token_conditions(token)
    expired = []
    keep = []
    for cond in conds:
        left = cond.get("rounds")
        if left is None:
            keep.append(cond)
            continue
        left -= rounds
        if left > 0:
            cond["rounds"] = left
            keep.append(cond)
        else:
            expired.append(cond.get("name"))
    token["conditions"] = keep
    return expired


# ----------------------------------------------------------------------
# Типы урона: ключи dnd5e и русские названия сводятся к одному виду
# ----------------------------------------------------------------------

#: Канонический ключ -> русская подпись для интерфейса. Порядок как в dnd5e.
DAMAGE_TYPES = {
    "acid": "кислота", "bludgeoning": "дробящий", "cold": "холод",
    "fire": "огонь", "force": "силовое поле", "lightning": "электричество",
    "necrotic": "некротический", "piercing": "колющий", "poison": "яд",
    "psychic": "психический", "radiant": "излучение", "slashing": "рубящий",
    "thunder": "звук",
}

#: Русские написания, которые встречаются в карточках dnd.su и в листах.
_DAMAGE_ALIASES = {
    "кислота": "acid", "кислотный": "acid",
    "дробящий": "bludgeoning", "дробящее": "bludgeoning", "дробящ": "bludgeoning",
    "холод": "cold", "холодный": "cold",
    "огонь": "fire", "огненный": "fire", "огн": "fire",
    "силовое поле": "force", "силовой": "force", "сила": "force",
    "электричество": "lightning", "молния": "lightning", "электрический": "lightning",
    "некротический": "necrotic", "некроз": "necrotic",
    "колющий": "piercing", "колющ": "piercing",
    "яд": "poison", "отравление": "poison", "ядовитый": "poison",
    "психический": "psychic", "психика": "psychic",
    "излучение": "radiant", "свет": "radiant", "лучистый": "radiant",
    "рубящий": "slashing", "рубящ": "slashing",
    "звук": "thunder", "звуковой": "thunder", "гром": "thunder",
}


def normalize_damage_type(value) -> str:
    """Привести тип урона к ключу dnd5e ("fire"). Неизвестное — пустая строка.

    Пустая строка, а не исключение: карточки приходят из внешней библиотеки,
    и один незнакомый тип не должен ломать весь бой.
    """
    if not value:
        return ""
    text = str(value).strip().casefold().replace("ё", "е")
    if text in DAMAGE_TYPES:
        return text
    if text in _DAMAGE_ALIASES:
        return _DAMAGE_ALIASES[text]
    # «урон огнём», «fire damage» -> ищем известное слово внутри строки
    for key, label in DAMAGE_TYPES.items():
        if key in text or label in text:
            return key
    return ""


def damage_type_label(value) -> str:
    """Русская подпись типа урона для интерфейса."""
    return DAMAGE_TYPES.get(normalize_damage_type(value), "")


#: Сокращения, которыми карточки и импорт dnd5e пишут тип в формуле урона
#: («1d8+4 руб.»). Без них строка урона не давала тип, и сопротивление
#: цели молча не срабатывало.
_DAMAGE_ABBREVIATIONS = {
    "руб": "slashing", "кол": "piercing", "дроб": "bludgeoning",
    "огн": "fire", "хол": "cold", "эл": "lightning",
    "некр": "necrotic", "псих": "psychic", "изл": "radiant",
    "сил": "force", "кисл": "acid", "звук": "thunder", "яд": "poison",
}


def _damage_words() -> list:
    """Слова, которыми в тексте записывают тип урона: [(слово, ключ), …].

    Сначала длинные: «силовое поле» должно сработать раньше «сил».
    """
    words = {}
    for key, label in DAMAGE_TYPES.items():
        words[key] = key
        words[label] = key
    for word, key in _DAMAGE_ALIASES.items():
        words[word] = key
    for word, key in _DAMAGE_ABBREVIATIONS.items():
        words[word] = key
    return sorted(words.items(), key=lambda item: -len(item[0]))


def _damage_word_pattern(word: str) -> str:
    """Слово целиком: «кол» внутри «колющий» типом не считается.

    Точка после сокращения («1d8+4 руб.») съедается вместе со словом:
    иначе в формуле остался бы одинокий разделитель.
    """
    return (r"(?<![a-zа-я])" + re.escape(word) + r"(?![a-zа-я])"
            + r"\s*\.?")


def damage_type_from_text(text) -> str:
    """Тип урона, упомянутый в тексте: «1d8+4 руб.» -> ``slashing``.

    Карточки импортируют урон строкой вместе с типом («1d8+4 рубящий»,
    «8d6 огонь»), а не отдельным полем. Ищем и полные названия, и
    сокращения, иначе тип терялся и цель получала урон без сопротивления.
    """
    source = str(text or "").casefold().replace("ё", "е")
    if not source.strip():
        return ""
    key = normalize_damage_type(source)
    if key:
        return key
    for word, key in _damage_words():
        if re.search(_damage_word_pattern(word), source):
            return key
    return ""


def split_damage_text(text) -> tuple:
    """Отделить формулу урона от типа: «1d10+6 колющий» -> («1d10+6», «piercing»).

    Карточка актёра показывает тип отдельным полем. Слово типа в поле
    урона раньше оставалось, и бросок урона разбирал «1d10+6 колющий»
    как мусор, а сопротивление цели не работало вовсе.

    Понимает и «1d4+2 колющий урон», и «1d4+2 (колющий)»: скобки и
    служебные слова вокруг типа не должны попадать в формулу.
    """
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    found = ""
    cleaned = raw
    for word, key in _damage_words():
        pattern = _damage_word_pattern(word)
        if not re.search(pattern, cleaned, re.IGNORECASE):
            continue
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
        found = found or key
    if not found:
        return raw, ""
    # Скобки остались от записи «1d6+2 (огонь)», а «урон»/«damage» — от
    # «1d6+2 огонь урон». В формуле этому не место.
    cleaned = re.sub(r"[()\[\]{}]", " ", cleaned)
    cleaned = re.sub(
        r"(?<![a-zа-я])(?:урон|урона|уроном|урону|тип|типа|damage|dmg|"
        r"или|or|and)(?![a-zа-я])", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;/+-|")
    return cleaned, found


def _norm_list(values) -> set:
    """Набор нормализованных типов: и "fire", и "огонь" дают "fire"."""
    result = set()
    for value in values or []:
        key = normalize_damage_type(value)
        if key:
            result.add(key)
    return result


def apply_resistance(amount: int, damage_type="", resist=None, immune=None,
                     vulnerable=None) -> dict:
    """Применить защиту цели к уже брошенному урону.

    Возвращает {'amount', 'factor', 'note'}. Без типа урона защита не работает:
    иначе «дробящий» превращался бы в «огненный» от одного совпадения строки.
    """
    amount = max(0, _as_int(amount, "урон", 0, MAX_HP))
    key = normalize_damage_type(damage_type)
    factor = 1.0
    note = ""
    if key and key in _norm_list(immune):
        factor, note = 0.0, "иммунитет"
    elif key and key in _norm_list(vulnerable):
        factor, note = 2.0, "уязвимость"
    elif key and key in _norm_list(resist):
        factor, note = 0.5, "сопротивление"
    # Дробное сопротивление округляем вниз, как в 5e.
    return {"amount": int(amount * factor), "factor": factor, "note": note}


# ----------------------------------------------------------------------
# Кубиковые формулы из карточек
# ----------------------------------------------------------------------

_FORMULA_RE = __import__("re").compile(r"(\d*)\s*[dк]\s*(\d+)\s*(?:([+-])\s*(\d+))?")


def _clean_formula(text) -> str:
    """Вытащить формулу из текста карточки: «урон: 2к6 + 3 рубящий» -> «2d6+3»."""
    if not text:
        return ""
    source = str(text).casefold().replace("ё", "е")
    match = _FORMULA_RE.search(source)
    if not match:
        return ""
    count = match.group(1) or "1"
    sides = match.group(2)
    formula = f"{count}d{sides}"
    if match.group(3):
        formula += f"{match.group(3)}{match.group(4)}"
    return formula


def clean_damage_formula(text) -> str:
    """Публичная обёртка: вытащить «2d6+3» из текста карточки."""
    return _clean_formula(text)


def _double_dice(formula: str) -> str:
    """Удвоить кости, оставив модификатор: «2d6+3» -> «4d6+3». Для крита."""
    import re
    source = str(formula or "").strip().lower()
    if not source:
        return ""
    out = []
    last = 0
    for match in re.finditer(r"(\d*)[dк](\d+)", source):
        out.append(source[last:match.start()])
        count = int(match.group(1) or 1)
        out.append(f"{count * 2}d{match.group(2)}")
        last = match.end()
    out.append(source[last:])
    return "".join(out)


def roll_damage(formula: str, critical: bool = False, crit=None,
                damage_type: str = "", resist=None, immune=None,
                vulnerable=None, rng=None) -> dict:
    """Бросок урона с критом и защитой цели.

    Неверная формула не бросает исключение: в поле 'error' лежит причина,
    а 'amount' = 0. Иначе мастер вводил бы «просто текст» и получал бы падение
    всего боя вместо понятной ошибки на карточке.
    """
    if crit is not None:
        critical = bool(crit)
    # Старые карточки и сохранения пишут тип прямо в формуле («1d8+4
    # рубящий»). Если отдельное поле пустое, берём тип из текста: иначе
    # сопротивление цели молча не срабатывало. Бросок всё равно идёт по
    # чистой формуле.
    if not damage_type:
        damage_type = damage_type_from_text(formula)
    formula = _clean_formula(formula) or str(formula or "").strip()
    base = {"formula": formula, "total": 0, "amount": 0, "breakdown": "",
            "critical": bool(critical), "error": ""}
    if not formula:
        return base
    try:
        rolled = damage_roll(formula, crit=bool(critical), rng=rng)
    except CombatError as exc:
        base["error"] = str(exc)
        return base
    protection = apply_resistance(rolled["total"], damage_type,
                                  resist=resist, immune=immune,
                                  vulnerable=vulnerable)
    base.update({
        "total": rolled["total"],
        # Показываем ту формулу, которая реально бросалась: при крите это
        # «4d6+3», а не исходное «2d6+3» — иначе в чате видно одно, а сумма
        # посчитана по другому.
        "formula": rolled.get("formula") or formula,
        "breakdown": rolled["breakdown"],
        "amount": protection["amount"],
        "factor": protection["factor"],
        "note": protection["note"],
        "damage_type": normalize_damage_type(damage_type),
    })
    return base


def _ac_value(target_ac):
    """КД из числа или мусора. Мусор и отсутствие КД = попадание засчитано."""
    if target_ac is None:
        return None
    try:
        value = int(target_ac)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > 40:
        return None
    return value


def roll_attack(bonus: int, target_ac=None, advantage: bool = False,
                disadvantage: bool = False, rng=None) -> dict:
    """Атака против КД. Ключ 'critical' — как в dnd5e, 'crit' — как в старом коде."""
    try:
        bonus = _as_int(bonus, "бонус атаки", -20, 30)
    except CombatError:
        bonus = 0
    ac = _ac_value(target_ac)
    mode = _mode_of(advantage, disadvantage)
    roll = d20(mode, rng)
    natural = roll["kept"]
    total = natural + bonus
    critical = natural == 20
    auto_miss = natural == 1
    if ac is None:
        hit = not auto_miss
    else:
        hit = critical or (not auto_miss and total >= ac)
    return {
        "hit": bool(hit),
        "crit": bool(critical),
        "critical": bool(critical),
        "miss_auto": bool(auto_miss),
        "d20": natural,
        "kept": natural,
        "rolls": roll["rolls"],
        "mode": mode,
        "total": total,
        "bonus": bonus,
        "target_ac": ac,
    }


def dice_detail_text(detail) -> str:
    """Значения кубиков из разбора эффекта: «1d4: 3».

    Нужно для строки в чате и журнале: «благословение +3» без кубика не
    объясняет, откуда взялось число. Принимает словарь вида
    {'die': '1d4', 'rolls': [[3]], 'total': 3}.
    """
    if not isinstance(detail, dict):
        return ""
    die = str(detail.get("die") or "")
    values = []
    for item in detail.get("rolls") or []:
        if isinstance(item, (list, tuple)):
            values.extend(str(v) for v in item)
        else:
            values.append(str(item))
    if not values:
        return ""
    return f"{die}: {', '.join(values)}".strip(": ")


def _two_d20(roll: dict):
    """Две кости броска с преимуществом/помехой или None.

    В `combat.d20` кости лежат плоским списком [7, 17], а `dice.roll` для
    режима adv/dis кладёт пары [[7, 17]] — читаем оба вида, иначе в журнале
    теряется отброшенная кость.
    """
    if not isinstance(roll, dict) or not roll.get("mode"):
        return None
    rolls = roll.get("rolls") or []
    if not rolls:
        return None
    first = rolls[0]
    if isinstance(first, (list, tuple)):
        return list(first)[:2] if len(first) > 1 else None
    if len(rolls) > 1:
        return [rolls[0], rolls[1]]
    return None


def d20_text(roll: dict) -> str:
    """«d20 12+5=17» или «d20 7, 17→17+5=22» при преимуществе/помехе.

    Отброшенная кость тоже видна: иначе за столом нельзя проверить, что
    сервер действительно взял большую (или меньшую) из двух.
    """
    roll = roll or {}
    natural = roll.get("d20", roll.get("kept", 0))
    bonus = roll.get("bonus", 0)
    total = roll.get("total", 0)
    pair = _two_d20(roll)
    if pair is not None:
        return f"d20 {pair[0]}, {pair[1]}→{natural}{bonus:+d}={total}"
    return f"d20 {natural}{bonus:+d}={total}"


def attack_text(attack: dict) -> str:
    """Строка для чата: «атака 17+5=22 против КД 15 — попадание (КРИТ)»."""
    attack = attack or {}
    natural = attack.get("d20", attack.get("kept", 0))
    bonus = attack.get("bonus", 0)
    total = attack.get("total", 0)
    ac = attack.get("target_ac")
    if attack.get("critical") or attack.get("crit"):
        verdict = "КРИТ"
    elif not attack.get("hit"):
        verdict = "промах" if not attack.get("miss_auto") else "промах (насмерть 1)"
    else:
        verdict = "попадание"
    pair = _two_d20(attack)
    if pair is not None:
        text = (f"атака (d20 {pair[0]}, {pair[1]}→{natural})"
                f"{bonus:+d}={total}")
    else:
        text = f"атака {natural}{bonus:+d}={total}"
    if ac is not None:
        text += f" против КД {ac}"
    return f"{text} — {verdict}"


def roll_save(bonus: int, dc=None, advantage: bool = False,
              disadvantage: bool = False, rng=None) -> dict:
    """Спасбросок против Сл. Без Сл ведёт себя как простой бросок d20+бонус."""
    try:
        bonus = _as_int(bonus, "бонус спасброска", -20, 30)
    except CombatError:
        bonus = 0
    try:
        dc = int(dc) if dc is not None else None
    except (TypeError, ValueError):
        dc = None
    if dc is not None:
        dc = max(1, min(40, dc))
    mode = _mode_of(advantage, disadvantage)
    roll = d20(mode, rng)
    natural = roll["kept"]
    total = natural + bonus
    if dc is None:
        success = None
    else:
        # В 5e натуральные 20 и 1 не дают автоуспеха/автопровала на
        # спасбросках (это правило только для атак и смертельных бросков).
        # Раньше 20 всегда считалась успехом — низкий спасбросок гоблина
        # проходил Сл 16, и область наносила половину вместо полного урона.
        success = total >= dc
    return {
        "success": success,
        "d20": natural,
        "kept": natural,
        "rolls": roll["rolls"],
        "mode": mode,
        "total": total,
        "bonus": bonus,
        "dc": dc,
        "natural": natural,
    }


# ----------------------------------------------------------------------
# Состояния: что они дают в бою
# ----------------------------------------------------------------------

def condition_flags(conditions) -> dict:
    """Свести состояния к флагам, которые понимает расчёт атаки и спасброска.

    Правила состояний собраны в ``shared/conditions.py``: там же текст для
    интерфейса и расчёт с учётом дистанции. Здесь — стабильный словарь для
    старого кода и тестов, чтобы одна и та же логика не разъезжалась.
    """
    return conditions_rules.flags(conditions)


def summarize_conditions(conditions) -> str:
    """Подпись состояний для строки токена: через запятую, без дублей."""
    seen = []
    for name in conditions or []:
        text = str(name or "").strip()
        if text and text not in seen:
            seen.append(text)
    return ", ".join(seen)


# ----------------------------------------------------------------------
# Атака одного токена по другому — полный расчёт
# ----------------------------------------------------------------------

def resolve_attack(attacker: dict, target: dict, bonus: int, damage: str,
                   damage_type: str = "", mode=None, rng=None) -> dict:
    """Полный расчёт атаки: бросок, попадание, урон, сопротивления.

    attacker/target — словари токенов (как Token.to_dict()). Возвращает
    описание результата; сам токен цели НЕ меняет — это делает сервер
    под своей блокировкой.
    """
    roll = attack_roll(bonus, target.get("ac") or 10, mode, rng)
    result = {"roll": roll, "damage": None, "hp_before": target.get("hp"),
              "hp_after": target.get("hp"), "factor": 1.0}
    if not roll["hit"]:
        return result
    dmg = damage_roll(damage, crit=roll["crit"], rng=rng) if damage else None
    if dmg is None:
        return result
    factor = resistance_factor(
        damage_type,
        target.get("resistances"), target.get("immunities"),
        target.get("vulnerabilities"))
    amount = int(dmg["total"] * factor)
    hp_before = target.get("hp")
    if hp_before is None:
        hp_before = target.get("hp_max") or 0
    result.update({
        "damage": dmg,
        "factor": factor,
        "amount": amount,
        "hp_before": hp_before,
        "hp_after": apply_hp_change(hp_before, target.get("hp_max"), amount, heal=False),
    })
    return result


# ----------------------------------------------------------------------
# Спасброски от смерти
#
# Персонаж на нуле хитов не умирает сразу: он кидает d20 против Сл 10 и
# отмечает успехи/провалы кружками на токене и в листе. Три провала —
# смерть, три успеха — стабилизация. Кружков сначала три; если три броска
# не дали вердикта (например, 2 провала и 1 успех), добавляется следующий —
# всего до пяти, как в домашнем правиле этого стола.
# ----------------------------------------------------------------------

def normalize_death_saves(value) -> list:
    """Привести отметки к списку из '', 'success' и 'failure'.

    Принимаем и русские подписи: старые миры и ручные правки могли
    сохранить «успех»/«провал». Мусор отбрасывается — один сломанный
    элемент не должен ломать весь счёт смерти.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        text = str(item or "").strip().casefold()
        if text in (DEATH_SUCCESS, "успех", "успешно", "+ ", "+"):
            out.append(DEATH_SUCCESS)
        elif text in (DEATH_FAILURE, "провал", "провалено", "-", "−"):
            out.append(DEATH_FAILURE)
        elif text == "":
            out.append(DEATH_EMPTY)
        if len(out) >= DEATH_SAVE_SLOTS:
            break
    return out


def _padded_death_saves(results) -> list:
    """Отметки с минимальным числом кружков: пустые добавляются справа."""
    out = normalize_death_saves(results)
    while len(out) < DEATH_SAVE_MIN_SLOTS:
        out.append(DEATH_EMPTY)
    return out


def death_save_slots(results) -> int:
    """Сколько кружков рисовать: три, а при нерешительном счёте — больше."""
    return len(_padded_death_saves(results))


def death_save_state(results) -> dict:
    """Состояние смерти токена по отметкам: жив, умирает, стабилен, мёртв."""
    results = _padded_death_saves(results)
    successes = results.count(DEATH_SUCCESS)
    failures = results.count(DEATH_FAILURE)
    dead = failures >= DEATH_SAVE_FAILURES
    stable = not dead and successes >= DEATH_SAVE_SUCCESSES
    return {
        "results": results,
        "successes": successes,
        "failures": failures,
        "dead": dead,
        "stable": stable,
        # «Умирает» — на нуле хитов и ещё нет вердикта: можно бросать дальше.
        "dying": not dead and not stable,
        "slots": len(results),
        "dc": DEATH_SAVE_DC,
    }


def roll_death_save(rng=None) -> dict:
    """Спасбросок от смерти: чистый d20, 10 и выше — успех.

    Модификаторы характеристик сюда не входят: в 5e спасбросок от смерти
    не получает бонусов, только сами кости.
    """
    roll = d20(None, rng)
    natural = roll["kept"]
    return {
        "d20": natural,
        "rolls": roll["rolls"],
        "dc": DEATH_SAVE_DC,
        "success": natural >= DEATH_SAVE_DC,
    }


def _grow_death_slots(results) -> list:
    """Все кружки заполнены, а вердикта нет — показать следующий.

    Пока есть место: после трёх бросков «2 провала и 1 успех» игроку нужно
    видеть, что бросок не последний.
    """
    state = death_save_state(results)
    if (state["dying"] and DEATH_EMPTY not in state["results"]
            and len(state["results"]) < DEATH_SAVE_SLOTS):
        state["results"].append(DEATH_EMPTY)
    return state["results"]


def add_death_save(results, success: bool):
    """Записать результат спасброска в первый свободный кружок.

    Возвращает (новые отметки, состояние). Стабильного персонажа новый урон
    снова вводит в смерть: успехи сбрасываются, провал остаётся — иначе
    добить стабилизированного было бы невозможно.
    """
    state = death_save_state(results)
    marks = list(state["results"])
    if state["dead"]:
        return marks, state
    if state["stable"]:
        marks = [DEATH_EMPTY for _ in marks]
    value = DEATH_SUCCESS if success else DEATH_FAILURE
    if DEATH_EMPTY in marks:
        marks[marks.index(DEATH_EMPTY)] = value
    elif len(marks) < DEATH_SAVE_SLOTS:
        marks.append(value)
    else:
        return marks, state
    marks = _grow_death_slots(marks)
    return marks, death_save_state(marks)


def set_death_save(results, index: int, success=None):
    """Поставить отметку в конкретный кружок.

    ``success=True`` — зелёный, False — красный, None — очистить кружок.
    Нужно, когда кубик бросили в Dice Lab или настоящими кубиками: игрок
    сам отмечает результат на листе. Мёртвого так не «оживить» — только
    воскрешение мастером.
    """
    state = death_save_state(results)
    marks = list(state["results"])
    try:
        index = int(index)
    except (TypeError, ValueError):
        return marks, state
    if state["dead"] or index < 0 or index >= len(marks):
        return marks, state
    if success is None:
        marks[index] = DEATH_EMPTY
    else:
        marks[index] = DEATH_SUCCESS if success else DEATH_FAILURE
    marks = _grow_death_slots(marks)
    return marks, death_save_state(marks)


def cycle_death_save(results, index: int):
    """Следующая ручная отметка кружка: пусто -> успех -> провал -> пусто."""
    state = death_save_state(results)
    marks = list(state["results"])
    try:
        index = int(index)
    except (TypeError, ValueError):
        return marks, state
    if index < 0 or index >= len(marks):
        return marks, state
    next_value = {DEATH_EMPTY: True, DEATH_SUCCESS: False,
                  DEATH_FAILURE: None}.get(marks[index], True)
    return set_death_save(marks, index, next_value)


def death_save_text(state: dict) -> str:
    """Короткая подпись для чата: «успехов 1, провалов 2»."""
    if state.get("dead"):
        return "мёртв"
    if state.get("stable"):
        return "стабилизирован"
    return (f"успехов {state.get('successes', 0)}, "
            f"провалов {state.get('failures', 0)}")
