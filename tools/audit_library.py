"""Аудит справочника предметов и заклинаний.

Запуск без изменений (только отчёт):

    python tools/audit_library.py

Записать очищенную библиотеку и отчёт:

    python tools/audit_library.py --write

Что считается мусором:

* точный дубль предмета: одинаковые имя, тип, подтип, редкость, вес,
  формулы, заряды и механика. Foundry-паки принесли такие записи несколько
  раз; в инвентаре они выглядят как несколько одинаковых ячеек;
* запись-заглушка «Безделушка» из таблицы d100 — у неё нет конкретного
  предмета, только описание-заглушка;
* запись без описания и без механики (например, «Тетива (10 футов)»).

Служебные записи (`Эффект`, `Категория`, `Зачарование`) не удаляются: по ним
собираются свойства магических предметов при повторном импорте SRD. Они
скрыты из интерфейса через `shared.content_audit.visible_items`.

Отдельно исправляются два заклинания, у которых описание принадлежало
другому заклинанию: Sleet Storm (Мокрый снег) и Arcane Hand (Магическая
рука). Это не «удалить», а починить текст, иначе игрок читает правила
Огненного шара у Мокрого снега и 5-го круга у Волшебной руки.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from shared import content_audit  # noqa: E402

ITEMS_PATH = os.path.join(ROOT, "library", "dnd5e", "items.json")
SPELLS_PATH = os.path.join(ROOT, "library", "dnd5e", "spells.json")
REMOVED_PATH = os.path.join(ROOT, "library", "dnd5e", "removed_items.json")
REPORT_PATH = os.path.join(ROOT, "library", "dnd5e", "audit_report.json")

#: Тексты, которые Foundry принёс не тому заклинанию. Исправляем по SRD.
SPELL_FIXES = {
    "Sleet Storm": {
        "name": "Мокрый снег",
        "school": "Вызов",
        "description": (
            "На землю в цилиндре с радиусом 40 футов и высотой 20 футов с "
            "центром в точке в пределах дистанции падает ледяной дождь и "
            "мокрый снег. Область сильно заслонена, а открытое пламя в ней "
            "гаснет.\n\n"
            "Земля в области покрыта скользким льдом и считается "
            "труднопроходимой местностью. Когда существо впервые за ход "
            "входит в область заклинания или начинает в ней ход, оно должно "
            "преуспеть в спасброске Ловкости, иначе упадёт сбитым с ног.\n\n"
            "Если существо начинает ход в области заклинания и "
            "концентрируется на заклинании, оно должно преуспеть в "
            "спасброске Телосложения против Сл ваших заклинаний, иначе "
            "теряет концентрацию."
        ),
    },
    "Arcane Hand": {
        "name": "Магическая рука",
        "school": "Воплощение",
        "description": (
            "Вы создаёте в точке, которую видите в пределах дистанции, "
            "сверкающую полупрозрачную руку Большого размера. Рука "
            "существует, пока заклинание активно, и подчиняется вашим "
            "командам.\n\n"
            "Рука — предмет с КД 20 и хитами, равными вашему максимуму хитов. "
            "Если её хиты опускаются до 0, заклинание оканчивается. У руки "
            "нет скорости, но она может перемещаться на 60 футов в ваш ход "
            "без затрат вашего перемещения.\n\n"
            "Действием вы можете управлять рукой одним из способов:\n\n"
            "Сжатая ладонь. Рука совершает рукопашную атаку заклинанием по "
            "существу в пределах 5 футов от себя. При попадании цель получает "
            "4к8 урона силовым полем.\n\n"
            "Хватающая ладонь. Рука пытается схватить существо Большого или "
            "меньшего размера в пределах 5 футов. Цель должна преуспеть в "
            "проверке Силы против Сл ваших заклинаний, иначе станет схваченной "
            "до окончания заклинания. Пока рука держит цель, вы можете "
            "бонусным действием сжать её, нанося 2к6 + модификатор вашей "
            "базовой характеристики дробящего урона.\n\n"
            "Мощная ладонь. Рука пытается толкнуть существо в пределах 5 "
            "футов. Цель должна преуспеть в проверке Силы против Сл ваших "
            "заклинаний, иначе отталкивается на 5 футов в выбранном вами "
            "направлении. Если она преуспевает, вы можете бонусным действием "
            "толкнуть её ещё раз.\n\n"
            "Защищающая ладонь. Рука встаёт между вами и существом по вашему "
            "выбору, давая вам укрытие на половину (+2 к КД) от этого "
            "существа.\n\n"
            "На больших уровнях. Если вы накладываете заклинание ячейкой "
            "6-го уровня или выше, урон сжатой ладони увеличивается на 2к8, а "
            "урон сжатия — на 2к6 за каждый уровень выше 5-го."
        ),
        # Урон в карточке не включаем: рука бьёт отдельной атакой заклинанием,
        # а прямой клик по цели наносил бы урон без броска.
        "damage": "",
    },
}


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _save(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))


def _text(rec):
    return str((rec or {}).get("description") or "").strip()


def item_signature(rec):
    """Механика предмета без id/названия источника.

    Два предмета с одинаковой сигнатурой — один и тот же предмет, даже если
    Foundry положил его в два пака. Название входит в сигнатуру: «Кувшин» и
    «Кувшин для воды» могут быть разными записями.
    """
    return (
        str(rec.get("name") or ""),
        str(rec.get("itype") or ""),
        str(rec.get("subtype") or ""),
        str(rec.get("rarity") or "").casefold(),
        str(rec.get("weight") or "").casefold(),
        tuple(str(v) for v in (rec.get("formulas") or [])),
        str(rec.get("uses") or "").casefold(),
        str(rec.get("heal") or "").casefold(),
        str(rec.get("activity_damage") or "").casefold(),
        # save_dc/attack_bonus не входят в сигнатуру: у одного и того же
        # предмета из разных паков одно поле могло быть пустым, а другое
        # заполненным. При слиянии оставляем запись с заполненным полем.
        tuple(str(v) for v in (rec.get("properties") or [])),
        str(rec.get("attunement") or "").casefold(),
    )


def is_placeholder(rec):
    """Запись, которая сама признаётся, что она заглушка."""
    return "заглушка" in _text(rec).casefold()


def is_empty(rec):
    """Ни описания, ни формулы, ни зарядов — игроку нечего применить."""
    return (not _text(rec) and not rec.get("formulas")
            and not rec.get("uses"))


def clean_items(records):
    """Разделить предметы на оставшиеся и удалённые.

    Служебные записи сохраняются на своих местах. Обычные записи
    группируются по механике; в группе остаётся запись с самым полным
    описанием, а порядок определяется первым вхождением.
    """
    service = []
    removed = []

    groups = collections.OrderedDict()
    for index, rec in enumerate(records or []):
        if not isinstance(rec, dict):
            continue
        if content_audit.is_service_item(rec):
            service.append((index, rec))
            continue
        if is_placeholder(rec) or is_empty(rec):
            removed.append({"reason": "заглушка" if is_placeholder(rec)
                            else "нет описания и механики", "item": rec})
            continue
        groups.setdefault(item_signature(rec), []).append((index, rec))

    kept_visible = []
    for rows in groups.values():
        # Лучшее описание, затем запись с картинкой, затем первая по порядку.
        rows = sorted(
            rows,
            key=lambda pair: (
                len(_text(pair[1])),
                pair[1].get("save_dc") is not None,
                pair[1].get("attack_bonus") is not None,
                bool(pair[1].get("img")),
                -pair[0],
            ),
            reverse=True,
        )
        keep_index, keep = rows[0]
        kept_visible.append((keep_index, keep))
        for _index, rec in rows[1:]:
            removed.append({"reason": "точный дубль", "item": rec})

    cleaned = [rec for _index, rec in sorted(service + kept_visible,
                                            key=lambda pair: pair[0])]
    return cleaned, removed


def fix_spells(spells):
    """Починить тексты, которые Foundry принёс не тому заклинанию."""
    fixed = []
    for rec in spells or []:
        patch = SPELL_FIXES.get(str(rec.get("source_name") or ""))
        if not patch:
            continue
        changed = False
        for key, value in patch.items():
            if rec.get(key) != value:
                rec[key] = value
                changed = True
        if changed:
            fixed.append(str(rec.get("source_name") or rec.get("name")))
    return fixed


def duplicate_names(items):
    """Одинаковые имена после очистки: обычно это разные ступени/варианты."""
    counter = collections.Counter(str(r.get("name") or "") for r in items)
    return {name: count for name, count in sorted(counter.items())
            if count > 1}


def build_report(items, spells, removed, fixed):
    visible = [r for r in items if not content_audit.is_service_item(r)]
    empty = [r.get("name") for r in visible if not _text(r)]
    dup_names = duplicate_names(visible)
    return {
        "updated": "2026-10-25",
        "items_total": len(items),
        "items_visible": len(visible),
        "items_service_hidden": len(items) - len(visible),
        "items_removed": len(removed),
        "items_removed_duplicates": sum(
            1 for row in removed if row["reason"] == "точный дубль"),
        "items_removed_placeholders": sum(
            1 for row in removed if row["reason"] == "заглушка"),
        "items_removed_empty": sum(
            1 for row in removed if row["reason"].startswith("нет описания")),
        "items_without_description": empty,
        "items_same_name_variants": dup_names,
        "spells_total": len(spells),
        "spells_without_description": [
            r.get("name") for r in spells if not _text(r)],
        "spells_fixed": fixed,
        "removed": removed,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="записать очищенные items.json/spells.json")
    args = parser.parse_args(argv)

    items = _load(ITEMS_PATH)
    spells = _load(SPELLS_PATH)
    cleaned, removed = clean_items(items)
    fixed = fix_spells(spells)
    report = build_report(cleaned, spells, removed, fixed)

    print(f"Предметов: {len(items)} -> {len(cleaned)} "
          f"(удалено {len(removed)})")
    print(f"  дублей: {report['items_removed_duplicates']}, "
          f"заглушек: {report['items_removed_placeholders']}, "
          f"пустых: {report['items_removed_empty']}")
    print(f"Видимых игроку: {report['items_visible']}, "
          f"служебных скрыто: {report['items_service_hidden']}")
    print(f"Без описания после очистки: {len(report['items_without_description'])}")
    print(f"Заклинаний: {len(spells)}; без описания: "
          f"{len(report['spells_without_description'])}; исправлено: "
          f"{', '.join(fixed) or '—'}")
    if report["items_same_name_variants"]:
        print("Одинаковые имена (разные варианты, не дубли): "
              + ", ".join(f"{name}×{count}" for name, count
                          in list(report["items_same_name_variants"].items())[:8])
              + (" …" if len(report["items_same_name_variants"]) > 8 else ""))

    if not args.write:
        print("\nЭто отчёт без изменений. Для записи: --write")
        return 0

    _save(ITEMS_PATH, cleaned)
    _save(SPELLS_PATH, spells)
    _save(REMOVED_PATH, removed)
    _save(REPORT_PATH, report)
    print(f"\nЗаписано: {ITEMS_PATH}, {SPELLS_PATH}")
    print(f"Удалённое сохранено для истории: {REMOVED_PATH}")
    print(f"Отчёт: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
