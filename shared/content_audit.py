"""Аудит справочника: что показывать игроку и как тратить предмет.

В items.json кроме настоящих предметов лежат служебные записи Foundry:

* ``Эффект`` — модификаторы, из которых собираются свойства магических
  предметов (КД, сопротивление, сила великана);
* ``Категория`` — заглушки паков (Оружие, Зелье, Доспех);
* ``Зачарование`` — под-активности вроде «масло остроты: оружие».

Это не предметы инвентаря. В справочнике и в выдаче они только мешают,
поэтому ``visible_items`` их скрывает. Исходный items.json не переписываем:
при повторном импорте SRD служебные записи нужны для сборки эффектов.

Второй аудит — расход. У 247 записей есть строка «заряды: N». Заряды
бывают у жезлов, посохов, колец, оружия и переиспользуемых чудесных
предметов; это не количество штук. Одноразовые подтипы (зелье, яд,
свиток, боеприпас, еда) по-прежнему тратят штуку из стака.
"""
import re

SERVICE_ITEM_TYPES = frozenset({"Эффект", "Категория", "Зачарование"})

#: Подтипы, которые расходуются штукой даже при строке «заряды: 1».
ONE_USE_SUBTYPES = frozenset({"Зелье", "Яд", "Свиток", "Боеприпасы", "Еда"})

_CHARGES_RE = re.compile(r"заряды\s*:\s*(\d+)", re.IGNORECASE)


def is_service_item(rec: dict) -> bool:
    """Служебная запись Foundry, а не предмет для инвентаря."""
    return str((rec or {}).get("itype") or "").strip() in SERVICE_ITEM_TYPES


def visible_items(records) -> list:
    """Записи, которые имеет смысл показывать в справочнике и выдавать."""
    return [rec for rec in (records or [])
            if isinstance(rec, dict) and not is_service_item(rec)]


def charges_max(rec: dict) -> int:
    """Максимум зарядов из строки «заряды: 7 (на рассвете 1d6 + 1)»."""
    rec = rec or {}
    try:
        explicit = int(rec.get("charges_max") or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit
    match = _CHARGES_RE.search(str(rec.get("uses") or ""))
    if not match:
        return 0
    try:
        return max(0, int(match.group(1)))
    except (TypeError, ValueError):
        return 0


def charges_recovery(rec: dict) -> str:
    """Текст восстановления зарядов: «после длинного отдыха 1d3»."""
    rec = rec or {}
    saved = str(rec.get("charges_recovery") or "").strip()
    if saved:
        return saved
    text = str(rec.get("uses") or "").strip()
    if not text:
        return ""
    # Всё после «заряды: N» в скобках — это условие восстановления.
    match = re.search(r"заряды\s*:\s*\d+\s*(?:\((.*)\))?", text,
                      re.IGNORECASE)
    if match and match.group(1):
        return match.group(1).strip()
    return ""


def is_chargeable(rec: dict) -> bool:
    """Тратит ли предмет заряд, оставаясь в инвентаре."""
    rec = rec or {}
    if not charges_max(rec):
        return False
    itype = str(rec.get("itype") or "").strip()
    subtype = str(rec.get("subtype") or "").strip()
    if itype == "Расходник" and subtype in ONE_USE_SUBTYPES:
        return False
    return True


def consumption_kind(rec: dict) -> str:
    """charge | quantity | none — чем платить за использование."""
    rec = rec or {}
    if is_chargeable(rec):
        return "charge"
    if str(rec.get("itype") or "").strip() == "Расходник":
        return "quantity"
    return "none"


def audit_summary(items, spells) -> dict:
    """Короткая сводка для Obsidian и тестов."""
    items = [r for r in (items or []) if isinstance(r, dict)]
    spells = [r for r in (spells or []) if isinstance(r, dict)]
    hidden = [r for r in items if is_service_item(r)]
    visible = [r for r in items if not is_service_item(r)]
    no_desc = [r for r in visible
               if not str(r.get("description") or "").strip()
               and not r.get("formulas") and not r.get("uses")]
    chargeable = [r for r in visible if is_chargeable(r)]
    quantity = [r for r in visible if consumption_kind(r) == "quantity"]
    return {
        "items_total": len(items),
        "items_visible": len(visible),
        "items_hidden_service": len(hidden),
        "items_visible_without_text": len(no_desc),
        "items_chargeable": len(chargeable),
        "items_quantity": len(quantity),
        "spells_total": len(spells),
        "spells_without_text": sum(
            1 for r in spells if not str(r.get("description") or "").strip()),
    }
