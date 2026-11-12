"""Эффекты предметов: что накладывает зелье, масло или яд.

Механика одна и та же у десятков записей справочника, поэтому эффект
описывается данными, а не отдельным кодом на каждое зелье:

    {'type': 'resist', 'damage_type': 'fire', 'rounds': 600,
     'label': 'Сопротивление огню', 'duration': '1 час'}

Сервер понимает безопасный набор типов (`resist`, `condition`, `temp_hp`,
`speed`, `note`), поэтому клиент не может наложить произвольное поле на
токен. `note` — это видимый чип «эффект действует», механику которого ещё
предстоит автоматизировать (полёт, дыхание под водой, чтение мыслей).

Исходные данные зелий:

  * сопротивление — 10 видов урона, 1 час;
  * сила великана — 21/23/25/27/29 на 1 час;
  * героизм — 10 временных хитов и «Благословение» на 1 час;
  * невидимость — состояние «Невидим» на 1 час;
  * скорость — ×2 перемещения на 1 минуту (10 раундов);
  * лечение — отдельная ветка `UseAction.kind == 'heal'`.

Модуль не знает про Qt и сеть: его можно проверить без окна.
"""
from __future__ import annotations

from shared import combat as combat_rules

HOUR_ROUNDS = 600
MINUTE_ROUNDS = 10

_RESIST_TYPES = (
    ("кислот", "acid"),
    ("холод", "cold"),
    ("огн", "fire"),
    ("сил", "force"),
    ("электрич", "lightning"),
    ("некрот", "necrotic"),
    ("психи", "psychic"),
    ("излучен", "radiant"),
    ("гром", "thunder"),
    ("яд", "poison"),
)

_GIANT_STRENGTH = (
    ("холмов", 21),
    ("каменн", 23),
    ("ледян", 23),
    ("огненн", 25),
    ("облачн", 27),
    ("штормов", 29),
)


def _text(rec) -> str:
    rec = rec or {}
    return " ".join(str(rec.get(key) or "") for key in
                    ("name", "source_name", "description")).casefold()


def _effect(kind, label, rounds, **extra) -> dict:
    effect = {"type": kind, "label": label, "rounds": int(rounds)}
    effect.update({key: value for key, value in extra.items()
                   if value not in (None, "")})
    return effect


def resistance_type(rec) -> str:
    """Тип урона зелья сопротивления или пустая строка."""
    text = _text(rec)
    if "сопротивлен" not in text:
        return ""
    for needle, damage_type in _RESIST_TYPES:
        if needle in text:
            return damage_type
    return ""


def giant_strength(rec):
    """Значение Силы зелья великана или None."""
    name = str((rec or {}).get("name") or "").casefold()
    if "зель" not in name or "сил" not in name:
        return None
    for needle, value in _GIANT_STRENGTH:
        if needle in name:
            return value
    return None


def is_speed(rec) -> bool:
    name = str((rec or {}).get("name") or "").casefold()
    return "зель" in name and "скорост" in name


def effects_for_item(rec) -> list:
    """Список эффектов, которые предмет накладывает при использовании.

    Лечение сюда не входит: у него своя ветка с броском и зелёным откликом.
    Для обычного снаряжения возвращается пустой список.
    """
    if not isinstance(rec, dict):
        return []
    # Только расходники: у кольца/плаща сопротивление пассивное, его нельзя
    # «выпить» кликом из инвентаря. Иначе предмет-кольцо давало бы бафф.
    if str(rec.get("itype") or "") != "Расходник":
        return []
    name = str(rec.get("name") or "").casefold()
    text = _text(rec)

    if is_speed(rec):
        return [_effect("speed", "Ускорение", MINUTE_ROUNDS,
                        duration="1 минута")]

    damage_type = resistance_type(rec)
    if damage_type:
        label = combat_rules.damage_type_label(damage_type).capitalize()
        return [_effect("resist", f"Сопротивление: {label.lower()}", HOUR_ROUNDS,
                        damage_type=damage_type, duration="1 час")]

    strength = giant_strength(rec)
    if strength:
        return [_effect("note", f"Сила {strength}", HOUR_ROUNDS,
                        duration="1 час",
                        note=f"Сила становится {strength} (механика — в работе)")]

    if "героизм" in name:
        return [
            _effect("temp_hp", "Героизм: +10 вр. хитов", HOUR_ROUNDS,
                    amount=10, duration="1 час"),
            _effect("note", "Благословение", HOUR_ROUNDS, duration="1 час",
                    note="Преимущество к атакам и спасброскам — в работе"),
        ]

    if "невидимост" in name:
        return [_effect("condition", "Невидимость", HOUR_ROUNDS,
                        condition="Невидим", duration="1 час")]

    if "сыворотка правды" in name:
        return [_effect("condition", "Не может лгать", HOUR_ROUNDS,
                        condition="Отравлен", duration="1 час")]

    if "приворотное" in name:
        return [_effect("condition", "Очарован", HOUR_ROUNDS,
                        condition="Очарован", duration="1 час")]

    if "антитоксин" in name:
        return [_effect("note", "Антитоксин", HOUR_ROUNDS, duration="1 час",
                        note="Преимущество на спасброски от яда — в работе")]

    if "полёт" in name or "полет" in name:
        return [_effect("note", "Полёт", HOUR_ROUNDS, duration="1 час",
                        note="Полёт со скоростью ходьбы — в работе")]

    if "лазан" in name:
        return [_effect("note", "Лазание", HOUR_ROUNDS, duration="1 час",
                        note="Скорость лазания — в работе")]

    if "дыхани" in name or "под водой" in name:
        return [_effect("note", "Дыхание под водой", HOUR_ROUNDS,
                        duration="1 час",
                        note="Дыхание под водой — в работе")]

    if "газообразн" in name:
        return [_effect("note", "Газообразная форма", HOUR_ROUNDS,
                        duration="1 час",
                        note="Газообразная форма — в работе")]

    if "чтени" in name and "мысл" in name:
        return [_effect("note", "Чтение мыслей", HOUR_ROUNDS,
                        duration="1 час",
                        note="Обнаружение мыслей — в работе")]

    if "ясновид" in name:
        return [_effect("note", "Ясновидение", HOUR_ROUNDS, duration="10 минут",
                        note="Сенсор ясновидения — в работе")]

    if "дружб" in name and "животн" in name:
        return [_effect("note", "Дружба с животными", HOUR_ROUNDS,
                        duration="1 час",
                        note="Дружба с животными — в работе")]

    if "роста" in name or "уменьшени" in name:
        return [_effect("note", "Изменение размера", HOUR_ROUNDS,
                        duration="1к4 часа",
                        note="Увеличение/уменьшение — в работе")]

    if "масло остроты" in name:
        return [_effect("note", "Оружие +3", HOUR_ROUNDS, duration="1 час",
                        note="Магическое оружие и +3 к атаке/урону — в работе")]

    if "масло скользкост" in name:
        return [_effect("note", "Скользкость", HOUR_ROUNDS, duration="1 час",
                        note="Свобода от захватов — в работе")]

    if "масло этеральност" in name or "эфир" in text and "масло" in name:
        return [_effect("note", "Этеральность", HOUR_ROUNDS, duration="1 час",
                        note="Эфирный план — в работе")]

    return []


def has_mechanics(effects) -> bool:
    """Есть ли среди эффектов хоть один, который сервер реально применяет."""
    return any((effect or {}).get("type") in
               ("resist", "condition", "temp_hp", "speed")
               for effect in (effects or []))


def summary(effects) -> str:
    """Короткая строка для системного сообщения и подсказки."""
    labels = [str(effect.get("label") or "") for effect in (effects or [])
              if isinstance(effect, dict) and effect.get("label")]
    return ", ".join(labels)
