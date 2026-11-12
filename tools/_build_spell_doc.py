"""Собрать страницу Obsidian: как в столе работает КАЖДОЕ заклинание.

Страница не пишется руками: 319 записей разошлись бы с кодом через неделю.
Скрипт берёт таблицу SRD и ручные правила из `shared.spells`, строит по ним
план применения — тот же, что видят клиент и сервер, — и переводит план на
человеческий язык: чем бросают, по какой площади, что накладывается и каким
образом заклинание применяют за столом.

Запуск:
    python tools/_build_spell_doc.py
"""
from __future__ import annotations

import io
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import combat as combat_rules         # noqa: E402
from shared import spell_srd                      # noqa: E402
from shared import spells                         # noqa: E402
from shared import use_action                     # noqa: E402

OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "..", "Documentation", "Карточки",
    "Реестр заклинаний — как работает каждое.md")

#: Лист-образец: Жрец 9 с Мудростью 18. Нужен только для чисел в описании
#: (модификатор лечения, Сл спасброска), сами правила от листа не зависят.
SHEET = {
    "info": {"charClass": {"value": "Жрец 9"}, "level": {"value": 9}},
    "stats": {"wis": {"score": 18}, "int": {"score": 18}},
    "spellsInfo": {"base": {"code": "wis"}},
    "vtt": {},
}

SHAPE_RU = {"circle": "сфера", "cone": "конус", "line": "линия",
            "square": "куб"}
SCHOOL_RU = {
    "abjuration": "ограждение", "conjuration": "вызов",
    "divination": "прорицание", "enchantment": "очарование",
    "evocation": "воплощение", "illusion": "иллюзия",
    "necromancy": "некромантия", "transmutation": "преобразование",
}


def rounds_label(count) -> str:
    """«1 раунд», «10 раундов» — по-русски, а не «1 раунд(ов)»."""
    try:
        value = int(count)
    except (TypeError, ValueError):
        return ""
    if value % 10 == 1 and value % 100 != 11:
        return f"{value} раунд"
    if value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        return f"{value} раунда"
    return f"{value} раундов"


def damage_label(dtype) -> str:
    from shared import combat
    if not dtype:
        return ""
    return combat.damage_type_label(dtype).casefold()


def plan_for(slug: str, rule: dict) -> dict:
    """План заклинания так, как его увидят клиент и сервер."""
    rec = {
        "name": rule.get("ru") or rule.get("n") or slug,
        "source_name": rule.get("n") or "",
        "level": int(rule.get("lvl") or 0),
        "range": "",
        "lss_ref": "",
        "id": "",
    }
    return spells.plan(rec, SHEET)


#: Русские имена ручных правил, которых нет в таблице SRD (Сглаз есть в
#: PHB, но не в открытой SRD, поэтому в таблице его нет). Без этой подписи
#: заклинание выглядело бы незарегистрированным.
EXTRA_MANUAL = {
    "hex": {"name": "Сглаз", "en": "Hex", "level": 1,
            "school": "enchantment"},
    # Кары-заклинания паладина: их нет ни в одной открытой таблице, поэтому
    # числа записаны в `shared.spells.SMITE_SPELLS`.
    "thunderous_smite": {"name": "Громовая кара", "en": "Thunderous Smite",
                         "level": 1, "school": "evocation"},
    "wrathful_smite": {"name": "Злобная кара", "en": "Wrathful Smite",
                       "level": 1, "school": "evocation"},
    "searing_smite": {"name": "Пылающая кара", "en": "Searing Smite",
                      "level": 1, "school": "evocation"},
    # Остальные кары паладина: Клеймящая есть в SRD, три следующие — только
    # в PHB, поэтому числа для них записаны в SMITE_SPELLS.
    "blinding_smite": {"name": "Ослепляющая кара", "en": "Blinding Smite",
                       "level": 3, "school": "evocation"},
    "staggering_smite": {"name": "Потрясающий удар",
                         "en": "Staggering Smite", "level": 4,
                         "school": "enchantment"},
    "banishing_smite": {"name": "Изгоняющая кара", "en": "Banishing Smite",
                        "level": 5, "school": "abjuration"},
    # Удары следопыта: PHB, но в открытой таблице их тоже нет.
    "ensnaring_strike": {"name": "Опутывающий удар",
                         "en": "Ensnaring Strike", "level": 1,
                         "school": "conjuration"},
    "hail_of_thorns": {"name": "Град шипов", "en": "Hail of Thorns",
                       "level": 1, "school": "conjuration"},
    # Удар Зефира — 1 круг следопыта из «Xanathar»: в открытой таблице
    # его тоже нет.
    "zephyr_strike": {"name": "Удар Зефира", "en": "Zephyr Strike",
                      "level": 1, "school": "transmutation"},
}


def _smite_of(spec: dict):
    """Эффект «ждёт попадания», если он есть у заклинания."""
    for effect in spec.get("effects") or ():
        if isinstance(effect, dict) and effect.get("type") == "smite":
            return effect
    return None


def _favor_of(spec: dict):
    """Божественное благоволение: +1к4 к каждому попаданию оружием."""
    for effect in spec.get("effects") or ():
        if isinstance(effect, dict) and effect.get("type") == "favor":
            return effect
    return None


def _zephyr_of(spec: dict):
    """Удар Зефира: преимущество на атаку, кубик и +30 футов скорости."""
    for effect in spec.get("effects") or ():
        if isinstance(effect, dict) and effect.get("type") == "zephyr":
            return effect
    return None


def conditions_of(spec: dict) -> list:
    out = []
    for effect in spec.get("effects") or ():
        if not isinstance(effect, dict):
            continue
        condition = effect.get("condition")
        if condition and condition not in out:
            out.append(str(condition))
    return out


def effect_labels(spec: dict) -> list:
    out = []
    for effect in spec.get("effects") or ():
        if not isinstance(effect, dict):
            continue
        if effect.get("type") == "condition":
            continue
        label = str(effect.get("label") or "").strip()
        if label and label not in out:
            out.append(label)
    return out


def mechanics(spec: dict) -> str:  # noqa: C901
    """Что именно делает заклинание — одной фразой."""
    kind = str(spec.get("kind") or "utility")
    formula = str(spec.get("formula") or "")
    dtype = damage_label(spec.get("damage_type"))
    save = str(spec.get("save") or "")
    shapes = SHAPE_RU.get(str(spec.get("shape") or ""), "")
    size = int(spec.get("size_ft") or 0)
    parts = []
    if kind == "pool":
        parts.append(f"пул {formula} хитов")
        if conditions_of(spec):
            parts.append("состояние: " + ", ".join(conditions_of(spec)))
        parts.append("тратится на самых раненых")
    elif kind in ("save", "save_target", "area"):
        # «save_target» — тот же спасбросок, но по одной цели: в реестре он
        # читается так же, а шаблона на карте у него нет.
        # В плане спасбросок лежит кодом («wis»), а в старых записях —
        # словом («Мудрости»), поэтому сначала читаем код, потом слово:
        # иначе в реестре у каждого заклинания стояла бы «Ловкость».
        from shared import token_stats
        from shared.lss_format import ABILITY_RU
        ability = (save and (token_stats.normalize_ability(save)
                             or use_action.save_ability(save)))
        if ability:
            parts.append("спасбросок «" + ABILITY_RU.get(ability, ability) + "»")
        if formula:
            parts.append(f"{formula} урона" + (f" ({dtype})" if dtype else ""))
        if conditions_of(spec):
            parts.append("иначе " + ", ".join(conditions_of(spec)))
        if spec.get("half_on_success") and formula:
            parts.append("половина при успехе")
        # Правила, которые добавляет не таблица SRD, а ручное правило:
        # пол хитов и урезанный максимум у Урона, зомби у Перста смерти.
        if spec.get("hp_floor"):
            parts.append(f"урон не опускает хиты ниже "
                         f"{int(spec['hp_floor'])}")
        if spec.get("reduce_hp_max"):
            parts.append("при провале максимум хитов падает на полученный "
                         "урон на 1 час (лечение болезни возвращает)")
        if spec.get("zombie"):
            parts.append("убитый этим гуманоид встаёт зомби")
        if int(spec.get("chain") or 0):
            parts.append(f"разряды: до {int(spec['chain'])} существ в "
                         f"{int(spec.get('chain_radius_ft') or 30)} футах от "
                         "первой цели (+1 разряд за круг ячейки выше шестого); "
                         "урон бросается один раз, спасбросок — каждой цели")
        if spec.get("reaction"):
            parts.append("читается реакцией")
    elif kind == "attack":
        parts.append(f"атака заклинанием, {formula}" + (f" ({dtype})" if dtype else ""))
    elif kind == "heal":
        if int(spec.get("pool") or 0):
            parts.append(f"пул {int(spec['pool'])} хитов на всех выбранных")
        else:
            parts.append(f"лечение {formula}")
        if int(spec.get("targets_max") or 0) > 1:
            parts.append(f"до {int(spec['targets_max'])} целей")
        if spec.get("cures"):
            parts.append("снимает состояния")
    elif kind == "darts":
        parts.append(f"{spec.get('darts')} снаряда по {spec.get('dart_formula')}, "
                     "попадают всегда")
    elif kind == "revive":
        parts.append(f"возвращает к жизни с {int(spec.get('revive_hp') or 1)} хитом")
        if spec.get("cures"):
            parts.append("снимает состояния")
    elif kind == "dispel":
        parts.append("снимает эффекты заклинаний круга не выше ячейки, "
                     "от сильных — проверка")
    elif kind == "buff":
        labels = effect_labels(spec)
        #: Сказано ли уже, что именно накладывается: иначе в конце
        #: добавлялась бы безликая приписка «эффект на цель».
        described = False
        smite = _smite_of(spec)
        if smite is not None:
            # Кара или удар следопыта: сначала то, что добавится к удару,
            # потом последствие целиком — оно и есть главное правило.
            bits = []
            if smite.get("dice"):
                bits.append(
                    f"+{smite['dice']} к следующему удару"
                    + (f" ({damage_label(smite.get('damage_type'))})"
                       if smite.get("damage_type") else ""))
            note = str(smite.get("rider_note") or "").strip()
            if note:
                bits.append(note)
            parts.append("; ".join(bits) if bits
                         else "эффект на себя до первого удара")
            labels = []
            # Кара читается на себя: приписки «эффект на цель» у неё быть не
            # должно, иначе реестр описывал бы её как каст на чужой токен.
            described = True
        favor = _favor_of(spec)
        if favor is not None:
            parts.append(
                "пока держится, каждое попадание оружием — +"
                + str(favor.get("dice") or "1d4")
                + " излучением (на крите кость удваивается)")
            labels = []
            described = True
        zephyr = _zephyr_of(spec)
        if zephyr is not None:
            # Удар Зефира — не чип на цель, а «следующий удар»: пишем обе
            # части правила, иначе в реестре пропали бы 30 футов скорости.
            parts.append(
                "преимущество на бросок атаки оружием, +"
                + str(zephyr.get("dice") or "1d8")
                + (f" ({damage_label(zephyr.get('damage_type'))})"
                   if zephyr.get("damage_type") else "")
                + f" и +{int(zephyr.get('bonus_ft') or 0)} фт скорости "
                  "до конца хода")
            labels = []
            described = True
        mark = next((e for e in (spec.get("effects") or [])
                     if isinstance(e, dict) and e.get("type") == "mark"), None)
        if mark is not None:
            # У метки подпись — только название: смысл в кубике к урону.
            # Если у метки свой тип урона (Сглаз — некротический), пишем
            # его: иначе непонятно, чем именно бьёт добавка.
            mark_die = str(mark.get("die") or "1d6")
            mark_type = combat_rules.normalize_damage_type(
                mark.get("damage_type"))
            parts.append(f"{mark_die} к урону по помеченной цели"
                         + (f" ({combat_rules.damage_type_label(mark_type)})"
                            if mark_type else ""))
        elif labels:
            parts.append("; ".join(labels[:3]) + (" и др." if len(labels) > 3 else ""))
        elif not described:
            parts.append("эффект на цель")
    else:
        note = str(spec.get("note") or "")
        parts.append(note.replace(str(spec.get("name") or "") + " — ", "")
                     if note else "только подпись: мастер решает вручную")
    if shapes and size and kind in ("save", "area", "pool"):
        parts.append(f"{shapes} {size} фт")
    if spec.get("concentration"):
        parts.append("концентрация")
    if spec.get("ritual"):
        parts.append("ритуал")
    return ", ".join(part for part in parts if part)


def how_to_apply(spec: dict) -> str:
    """Каким путём заклинание применяют в интерфейсе."""
    kind = str(spec.get("kind") or "utility")
    if _smite_of(spec) is not None or _zephyr_of(spec) is not None:
        # Кара/удар следопыта и Удар Зефира читаются на себя, а работают
        # на следующем попадании: это не «эффект перетаскиванием».
        return "кнопка на листе → следующий удар"
    if _favor_of(spec) is not None:
        # Благоволение не сгорает: оно усиливает все удары до конца срока.
        return "кнопка на листе → все атаки оружием"
    if spec.get("shape") and kind in ("save", "area", "pool"):
        return "шаблон на карте"
    if kind in ("heal", "darts"):
        if int(spec.get("targets_max") or 0) > 1:
            return "массовое: выделенные цели"
        return "каст на токен (сервер)"
    if kind == "save_target":
        # Цель одна: клик по токену, спасбросок и урон — на сервере.
        # У Цепной молнии вторым шагом выбираются разряды (до 3 существ в
        # 30 футах от первой цели): это другой путь применения.
        if int(spec.get("chain") or 0):
            return "одиночный спасбросок + разряды (сервер)"
        return "одиночный спасбросок (сервер)"
    if kind in ("revive", "dispel"):
        return "каст на токен (сервер)"
    if kind == "attack":
        return "окно атаки"
    if kind == "buff" and spec.get("effects"):
        return "эффект перетаскиванием на токен"
    return "подпись, вручную"


def formula_note(spec: dict) -> str:
    """Рост от круга ячейки и длительность."""
    bits = []
    if int(spec.get("level") or 0):
        bits.append(f"{int(spec['level'])} круг")
    duration = str(spec.get("duration") or "")
    if duration:
        bits.append(duration)
    if spec.get("slot_level"):
        bits.append(f"ячейка {int(spec['slot_level'])} круга")
    return ", ".join(bits)


def main() -> int:
    spells_table = spell_srd.SPELLS
    rows = []
    manual = []
    #: Строки заклинаний, которых нет в таблице SRD (Сглаз).
    extra_rows = []
    for slug, rule in spells_table.items():
        try:
            spec = plan_for(slug, rule)
        except Exception as exc:                     # pragma: no cover
            print(f"! {slug}: {exc}", file=sys.stderr)
            spec = {}
        name = rule.get("ru") or rule.get("n") or slug
        level = int(rule.get("lvl") or 0)
        key = str(spec.get("key") or "")
        is_manual = key in spells._RULES
        rows.append({
            "slug": slug, "name": name, "en": rule.get("n") or "",
            "level": level, "school": SCHOOL_RU.get(str(rule.get("school") or ""),
                                                   str(rule.get("school") or "")),
            "mechanics": mechanics(spec) if spec else "нет механики: только текст",
            "apply": how_to_apply(spec) if spec else "вручную",
            "note": formula_note(spec) if spec else "",
            "manual": is_manual,
        })
        if is_manual:
            manual.append((name, rule, spec))

    # Ручные правила, которых нет в таблице SRD: собираем их так же, но без
    # строки «по кругам» — круга в таблице для них не существует.
    in_table = {spells._manual_key(slug) for slug in spells_table}
    for key in spells._RULES:
        if key in in_table:
            continue
        info = EXTRA_MANUAL.get(key) or {"name": key, "en": "", "level": 0,
                                        "school": ""}
        rec = {"name": info["name"], "source_name": info.get("en") or "",
               "level": int(info.get("level") or 0)}
        try:
            spec = spells.plan(rec, SHEET)
        except Exception as exc:                     # pragma: no cover
            print(f"! {key}: {exc}", file=sys.stderr)
            continue
        if not spec:
            continue
        if key == "hex":
            # Порча — тоже очарование: у остальных правила слаги совпадают
            # с таблицей, и поле школы в _RULES не хранится, поэтому
            # рисуем его только там, где знаем.
            spec = dict(spec, school=info.get("school") or "")
        manual.append((info["name"],
                       {"ru": info["name"], "n": info.get("en") or "",
                        "lvl": int(info.get("level") or 0)}, spec))
        extra_rows.append({
            "slug": key, "name": info["name"], "en": info.get("en") or "",
            "level": int(info.get("level") or 0),
            "school": SCHOOL_RU.get(str(info.get("school") or ""),
                                    str(info.get("school") or "")),
            "mechanics": mechanics(spec), "apply": how_to_apply(spec),
            "note": formula_note(spec) + ", нет в таблице SRD",
            "manual": True,
        })

    sheet = spells.plan(
        {"name": "Огненный шар", "source_name": "Fireball", "level": 3}, SHEET)
    dc = spells.spell_save_dc(SHEET)
    bonus = spells.casting_modifier(SHEET)

    out = io.StringIO()
    write = out.write
    write("---\n")
    write("tags: [My-Function, Done, rules, content, spells]\n")
    write("status: Done\n")
    write("area: заклинания, реестр, механика, SRD 5.1\n")
    write("source: shared/spells.py, shared/spell_srd.py, tools/_build_spell_doc.py\n")
    write("updated: 2026-10-27\n")
    write("---\n")
    write("# Реестр заклинаний — как работает каждое\n\n")
    write("Страница собрана скриптом `tools/_build_spell_doc.py` из того же\n")
    write("реестра, по которому работают клиент и сервер: здесь нет ни одной\n")
    write("цифры, которой нет в коде. После правки правил её достаточно\n")
    write("пересобрать — вручную сверять 319 записей не нужно.\n\n")
    write(f"* Всего заклинаний в таблице SRD: **{len(spells_table)}**.\n")
    write(f"* Доведено вручную (свои правила, эффекты и отклик): "
          f"**{len(manual)}**.\n")
    write(f"* Остальные собираются из таблицы SRD автоматически: урон, лечение,\n")
    write(f"  спасбросок, область, состояния, сопротивление, временные хиты.\n")
    write(f"* Сл спасброска на листе-образце: **{dc}** "
          f"(8 + мастерство + модификатор {bonus:+d}).\n")
    write(f"* Огненный шар на том же листе: {sheet.get('formula')} — "
          "проверка, что реестр читается.\n")
    write("* Заговоры растут по УРОВНЮ ПЕРСОНАЖА, а не по ячейке: 5, 11 и 17\n")
    write("  уровень дают 2, 3 и 4 набора костей. Лист-образец 9 уровня,\n")
    write("  поэтому у заговоров в таблице показаны ДВЕ кости.\n\n")

    write("## Что означает колонка «Как»\n\n")
    write("* **шаблон на карте** — игрок ставит круг, конус, линию или куб;\n")
    write("  цели, спасброски и урон считает сервер по геометрии шаблона.\n")
    write("* **окно атаки** — бросок атаки заклинанием с критом и сопротивлением.\n")
    write("* **каст на токен (сервер)** — сервер берёт формулу из листа, проверяет\n")
    write("  дистанцию и меняет хиты; клиент не присылает результат.\n")
    write("* **эффект перетаскиванием на токен** — заклинание вешает чип\n")
    write("  (КД, сопротивление, состояние, метка, благословение).\n")
    write("* **одиночный спасбросок (сервер)** — цель одна (клик по токену на\n")
    write("  карте); спасбросок цели и кубик урона бросает сервер, провал\n")
    write("  даёт урон и последствие, успех — ничего либо половину.\n")
    write("* **одиночный спасбросок + разряды (сервер)** — цель одна, но после\n")
    write("  неё выбираются цели разрядов (Цепная молния: до трёх существ в\n")
    write("  30 футах ОТ ПЕРВОЙ цели, +1 за круг ячейки выше шестого); урон\n")
    write("  бросается один раз на всю цепочку, спасбросок — каждая цель свой.\n")
    write("* **массовое: выделенные цели** — одна ячейка, каждому свой бросок.\n")
    write("* **подпись, вручную** — механика требует решения мастера\n")
    write("  (телепорт, превращение, призыв), и игра честно об этом пишет.\n\n")

    write("## Пул хитов: сколько выпало и сколько ушло\n\n")
    write("Сон и Цветной шар бросают пул один раз. В карточке броска и в\n")
    write("журнале видно два числа: сколько хитов выпало и сколько из них\n")
    write("потрачено на цели. Цель засыпает, только если её текущие хиты\n")
    write("целиком помещаются в остаток пула; хиты при этом не убавляются —\n")
    write("это не урон, а порог, кого хватило усыпить.\n\n")

    write("## Содержание\n\n")
    manual_sorted = sorted(manual, key=lambda item: (int(item[1].get("lvl") or 0),
                                                     item[0]))
    for name, rule, _spec in manual_sorted:
        # Obsidian сам делает заголовкам якоря: ссылка — по тексту
        # заголовка, синтаксис {#id} он не знает и показал бы его буквально.
        write(f"* [[#{name}]] — {int(rule.get('lvl') or 0)} круг\n")
    write("\n")

    write("## Доведённые вручную — как именно\n\n")
    for name, rule, spec in manual_sorted:
        write(f"### {name}\n\n")
        write(f"**Механика:** {mechanics(spec)}.\n\n")
        write(f"**Как применять:** {how_to_apply(spec)}. ")
        if formula_note(spec):
            write(f"**Круг и длительность:** {formula_note(spec)}.\n\n")
        else:
            write("\n\n")
        note = str(spec.get("note") or "")
        if note and not note.startswith(name):
            write(f"**Правило за столом:** {note}.\n\n")
        effects = [effect for effect in (spec.get("effects") or [])
                   if isinstance(effect, dict)]
        if effects:
            write("**Что накладывается:**\n\n")
            for effect in effects:
                write(f"* {effect.get('label') or effect.get('type')} — "
                      + (rounds_label(effect.get("rounds")) or "-")
                      + (", концентрация" if effect.get("concentration") else "")
                      + ".\n")
            write("\n")
        if spec.get("cures"):
            write("**Снимает состояния:** "
                  + ", ".join(spells.condition_ru(value)
                              for value in spec["cures"]) + ".\n\n")
        if spec.get("pool_skip_note"):
            write("**Не действует на:** " + str(spec["pool_skip_note"])
                  + ".\n\n")

    if extra_rows:
        write("## Ручные правила вне таблицы SRD\n\n")
        write("| Заклинание | По-английски | Школа | Что делает | Как |\n")
        write("| --- | --- | --- | --- | --- |\n")
        for row in extra_rows:
            write(f"| {row['name']} | {row['en'] or '—'} | {row['school']} | "
                  f"{row['mechanics']} | {row['apply']} |\n")
        write("\n")

    write("## Все заклинания по кругам\n\n")
    by_level = defaultdict(list)
    for row in rows:
        by_level[row["level"]].append(row)
    for level in sorted(by_level):
        title = "Заговоры" if level == 0 else f"{level} круг"
        write(f"### {title}\n\n")
        write("| Заклинание | По-английски | Школа | Что делает | Как |\n")
        write("| --- | --- | --- | --- | --- |\n")
        for row in sorted(by_level[level], key=lambda item: item["name"]):
            mark = " ★" if row["manual"] else ""
            write(f"| {row['name']}{mark} | {row['en']} | {row['school']} | "
                  f"{row['mechanics']} | {row['apply']} |\n")
        write("\n")

    write("★ — заклинание с ручными правилами (раздел выше).\n\n")
    write("## Ограничения\n\n")
    write("* Цели по области выбирает сервер по сетке карты: без сетки (Ctrl+G)\n")
    write("  область не применяется.\n")
    write("* Пул Сна и Цветного шара растёт от круга ячейки (+2к8 и +2к10 за\n")
    write("  круг), но круг приходит с клиента — запас ячеек ведёт лист\n")
    write("  игрока, и сервер его не пересчитывает.\n")
    write("* Концентрация держится серверной проверкой Сл 10 или половины\n")
    write("  урона — бросок делает сервер и пишет результат в журнал.\n")
    write("* Превращения, призывы и телепорты не двигают токены сами — даётся\n")
    write("  подпись с правилом.\n")
    write("* Перехват чужого каста (Контрзаклинание) невозможен: у стола нет\n")
    write("  модели «действие в момент действия». Снимайте концентрацию в\n")
    write("  карточке токена.\n")
    write("* Дикие состояния вроде «истощение» ведутся пометкой на токене, а\n")
    write("  не счётчиком уровней.\n\n")
    write("## Чем это проверено\n\n")
    write("* `tests/test_spell_pool_effects.py` — пул Сна и Цветного шара,\n")
    write("  область без урона (Паутина), Полное исцеление, пул 700,\n")
    write("  Оживление, Развеивание, Малое восстановление и Защита от яда,\n")
    write("  усиление пула ячейкой и отклик в интерфейсе.\n")
    write("* `tests/test_spell_engine.py` — таблица SRD, планы, усиление ячейкой,\n")
    write("  спасбросок состояния сервером, анимации и звуки.\n")
    write("* `tests/test_class_features.py` — кара, наложение рук, метка,\n")
    write("  мультикласс, кнопки и перетаскивание.\n")
    write("* `tests/test_mass_heal.py` — массовое лечение по выделенным целям.\n\n")
    write("Связано: [[Заклинания и классовые особенности]],\n")
    write("[[Эффекты предметов и заклинаний]], [[Атака спасброски сопротивление]],\n")
    write("[[Броски и чат]], [[Карточки бросков на холсте]].\n")

    path = os.path.abspath(OUT)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(out.getvalue())
    print(f"Записано: {path}")
    print(f"Заклинаний: {len(rows)}, вручную: {len(manual)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
