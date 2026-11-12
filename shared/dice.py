"""
Кубы с модификаторами — как в D&D 5e: «2d6+3», «1d20+5 adv», «dc 15».

Формула: слагаемые вида NdS (например 2d6, d20) и постоянные слагаемые
(+3, -2). Поддерживаются те же грани, что и в игре:
d2 d3 d4 d6 d8 d10 d12 d20 d100.

Режимы adv/dis действуют на единственный d20: бросается два раза,
берётся больший (adv) или меньший (dis). Результат — словарь с полным
разбором, чтобы чат мог показать «1d20(17, 4→17)+5 = 22».

Общий для клиента и сервера: клиент бросает, сервер проверяет форму
сообщения перед рассылкой.
"""
import random
import re

ALLOWED_SIDES = (2, 3, 4, 6, 8, 10, 12, 20, 100)
MAX_DICE = 100          # потолок кубиков в одном слагаемом
MAX_TERMS = 10          # потолок слагаемых

_TERM_RE = re.compile(r"^(?:(\d*)d(\d+)|([+-]?\d+))$", re.IGNORECASE)


class DiceError(ValueError):
    """Формула не разобрана или запрещена."""


def parse_formula(expr: str):
    """Разобрать формулу в список слагаемых.

    Возвращает [('die', n, sides), ..., ('mod', value), ...].
    """
    expr = str(expr).replace(" ", "").lower()
    if not expr:
        raise DiceError("Пустая формула. Пример: 1d20+5")
    # Отделяем минусы: вставляем + перед каждым «-» для единого разбора
    expr = re.sub(r"(?<!^)(?=-)", "+", expr)
    terms = []
    for raw in expr.split("+"):
        m = _TERM_RE.match(raw)
        if not m:
            raise DiceError(f"Не разобран кусок формулы: {raw!r}")
        if m.group(1) is not None:
            n = int(m.group(1)) if m.group(1) else 1
            sides = int(m.group(2))
            if n < 1 or n > MAX_DICE:
                raise DiceError(f"Число кубиков вне диапазона 1..{MAX_DICE}")
            if sides not in ALLOWED_SIDES:
                raise DiceError(
                    "Поддерживаются: d2 d3 d4 d6 d8 d10 d12 d20 d100")
            terms.append(("die", n, sides))
        else:
            terms.append(("mod", int(m.group(3))))
        if len(terms) > MAX_TERMS:
            raise DiceError(f"Слишком много слагаемых (максимум {MAX_TERMS})")
    if not any(t[0] == "die" for t in terms):
        raise DiceError("В формуле нет кубиков. Пример: 2d6+1")
    return terms


def roll(expr: str, mode=None, rng: random.Random = None) -> dict:
    """Бросить формулу. mode: None | 'adv' | 'dis' (только для d20)."""
    if mode not in (None, "adv", "dis"):
        raise DiceError(f"Неизвестный режим: {mode!r}")
    terms = parse_formula(expr)
    if mode:
        dies = [t for t in terms if t[0] == "die"]
        if len(dies) != 1 or dies[0][1:] != (1, 20):
            raise DiceError(
                "adv/dis работают только с одним кубиком d20. "
                "Пример: 1d20+5 adv")
    rnd = rng or random
    dice_reports = []
    total = 0
    for term in terms:
        if term[0] == "mod":
            total += term[1]
            continue
        _, n, sides = term
        rolls = []
        kept = []
        for _ in range(n):
            pair = [rnd.randint(1, sides)]
            if mode and sides == 20:
                pair.append(rnd.randint(1, sides))
                kept.append(max(pair) if mode == "adv" else min(pair))
            else:
                kept.append(pair[0])
            rolls.append(pair)
        total += sum(kept)
        dice_reports.append({"n": n, "sides": sides, "rolls": rolls,
                             "kept": kept, "mode": mode})
    return {
        "formula": _canonical_formula(terms),
        "mode": mode,
        "dice": dice_reports,
        "total": total,
    }


def roll_or_flat(expr) -> dict:
    """Бросок формулы — или плоское число, если кубиков нет: «70» → 70.

    Нужно там, где правило лечит фиксированное число хитов (Полное
    исцеление — ровно 70), а весь остальной код ждёт результат броска
    в том же виде: формула, кости, итог.
    """
    text = str(expr or "").strip()
    if re.fullmatch(r"\d+", text):
        return {"formula": text, "mode": None, "dice": [],
                "total": int(text)}
    return roll(text)


def _canonical_formula(terms) -> str:
    parts = []
    for index, t in enumerate(terms):
        if t[0] == "die":
            n, sides = t[1], t[2]
            name = f"{n}d{sides}" if n != 1 else f"d{sides}"
            parts.append(name if index == 0 else f"+{name}")
        else:
            parts.append(f"{t[1]:+d}")
    joined = "".join(parts)
    return joined[1:] if joined.startswith("+") else joined


def format_roll(result: dict) -> str:
    """Человекочитаемый разбор: «d20(17, 4→17)+5 = 22»."""
    parts = []
    for d in result["dice"]:
        if d["mode"]:
            shown = ", ".join(
                (f"{max(pair)}→{keep}" if d["mode"] == "adv"
                 else f"{min(pair)}→{keep}")
                for pair, keep in zip(d["rolls"], d["kept"]))
        else:
            shown = ", ".join(str(pair[0]) for pair in d["rolls"])
        name = f"{d['n']}d{d['sides']}" if d["n"] != 1 else f"d{d['sides']}"
        parts.append(f"{name}({shown})")
    for t in parse_formula(result["formula"]):
        if t[0] == "mod":
            parts.append(f"{t[1]:+d}")
    return "".join(parts)


def check(total: int, dc) -> bool:
    """Пройти ли проверку: total >= dc."""
    return int(total) >= int(dc)
