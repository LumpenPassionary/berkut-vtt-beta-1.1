"""Правила предметов (чистая логика, без Qt и сети).

  * Настройка (attunement): не больше ATTUNEMENT_LIMIT предметов одновременно.
  * Грузоподъёмность: Сила × 15 фунтов; сверх 2× — медленно, сверх 5× — недвижимо
    (правила 5e, «переносимый вес»). Здесь считаем только флаг перегруза.
  * Использование предмета: расход одной штуки и/или применение формулы
    (лечение / урон / спасбросок) — результат отдаёт сервер/холст.
"""
from shared import sheet_items as items
from shared import content_audit
from shared import dice as dice_ext

ATTUNEMENT_LIMIT = 3
CARRY_PER_STRENGTH = 15  # фунтов за единицу Силы


class ItemRuleError(ValueError):
    pass


def _item(sheet: dict, item_id: str):
    item = items.find_item(sheet, item_id)
    if item is None:
        raise ItemRuleError("предмета нет в инвентаре")
    return item


def attuned_items(sheet: dict) -> list:
    return [i for i in items.ensure(sheet)["inventory"] if i.get("attuned")]


def set_attuned(sheet: dict, item_id: str, on: bool) -> bool:
    """Включить/выключить настройку. Лимит 3 — при попытке сверх вернуть ошибку."""
    item = _item(sheet, item_id)
    if not on:
        item["attuned"] = False
        return False
    if item.get("attuned"):
        return True
    if not item.get("attunement"):
        raise ItemRuleError("предмет не требует настройки")
    if len(attuned_items(sheet)) >= ATTUNEMENT_LIMIT:
        raise ItemRuleError(f"нельзя настроиться больше чем на {ATTUNEMENT_LIMIT} предмета")
    item["attuned"] = True
    return True


def strength_score(sheet: dict) -> int:
    try:
        return int((sheet.get("stats", {}).get("str") or {}).get("score", 10))
    except (TypeError, ValueError):
        return 10


def carry_capacity(sheet: dict) -> float:
    return float(strength_score(sheet) * CARRY_PER_STRENGTH)


def carry_status(sheet: dict) -> dict:
    """Текущий вес относительно грузоподъёмности: 'ok' | 'slow' | 'immobile'."""
    weight = items.total_weight(sheet)
    cap = carry_capacity(sheet)
    if weight > cap * 5:
        state = "immobile"
    elif weight > cap * 2:
        state = "slow"
    else:
        state = "ok"
    return {"weight": weight, "capacity": cap, "state": state}


def use_item(sheet: dict, item_id: str, rng=None) -> dict:
    """Использовать предмет: расход штуки (для расходников) и бросок его эффекта.

    Возвращает {'name', 'consumed', 'remaining', 'effect': {'kind', 'formula', 'total'} | None}.
    Формула лечения/урона берётся из полей предмета: heal (лечение) или
    activity_damage / formulas (урон). Выбор — по приоритету: heal > урон.
    """
    item = _item(sheet, item_id)
    name = item.get("name", "?")
    effect = None
    heal = (item.get("heal") or "").strip()
    # У зелий из справочника формула лечения лежит в formulas
    # («лечение: 2d4+2»), а поле heal пустое. Без этого зелье считалось
    # оружием и наносило «урон» лечением.
    if not heal:
        for formula in item.get("formulas") or []:
            if str(formula).strip().casefold().startswith(("лечение", "heal")):
                heal = str(formula)
                break
    damage = (item.get("activity_damage") or "").strip()
    if not damage:
        for formula in item.get("formulas") or []:
            if str(formula).strip().casefold().startswith(("урон", "damage")):
                damage = str(formula)
                break
    if heal:
        effect = {"kind": "heal", "formula": _formula_from(heal)}
    elif damage:
        effect = {"kind": "damage", "formula": _formula_from(damage)}
    if effect:
        if not effect["formula"]:
            effect = None
        else:
            total = dice_ext.roll(effect["formula"], rng=rng)["total"]
            effect["total"] = total

    consumed = False
    kind = content_audit.consumption_kind(item)
    if kind == "charge":
        consumed = items.consume_charge(sheet, item_id)
    elif kind == "quantity":
        consumed = items.consume(sheet, item_id)
    remaining = (items.find_item(sheet, item_id) or {}).get("qty", 0)
    charges = (items.find_item(sheet, item_id) or {}).get("charges")
    return {"name": name, "consumed": consumed, "remaining": remaining,
            "charges": charges, "effect": effect}


def _formula_from(text: str) -> str:
    """Вытащить кубиковую формулу из текста: «лечение: 2d4+2» → «2d4+2»."""
    import re
    m = re.search(r"(\d*d\d+(?:\s*[+-]\s*\d+)?|\d+)", text)
    if not m:
        return ""
    return m.group(1).replace(" ", "")
