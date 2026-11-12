"""Инвентарь и заклинания персонажа — по образцу листа Foundry dnd5e.

Как это устроено в Foundry: предметы и заклинания — отдельные документы
(Item), привязанные к актёру. Мастер перетаскивает предмет из справочника
на лист игрока, и он появляется в инвентаре. Игрок жмёт на предмет и либо
бросает атаку, либо тратит расходник. Ячейки заклинаний — отдельный счётчик
у актёра, а не у заклинания.

Здесь та же модель, но в словарях: чтобы лист оставался обычным JSON и
переживал сохранение в файл LSS.

Структура (внутри листа, ключ vtt):

    vtt: {
      inventory: [ {id, name, itype, qty, equipped, weight, price,
                    damage, heal, save_dc, attack_bonus, uses, ...}, ... ],
      spells:    [ {id, name, level, school, prepared, damage, save,
                    attack, range, description, ...}, ... ],
      slots:     {"1": {"max": 4, "used": 1}, ...},
      conditions: [ {name, rounds} ],
    }

Чистые функции, без Qt: их можно проверять отдельно от интерфейса.
"""
import json
import re
import unicodedata
import uuid

from shared import content_audit

#: Сколько ячеек у персонажа по уровням заклинаний по умолчанию
DEFAULT_SLOTS = {1: 4, 2: 3, 3: 2, 4: 1, 5: 0, 6: 0, 7: 0, 8: 0, 9: 0}

#: Сетка инвентаря: один предмет — одна ячейка, количество лежит в стаке.
INVENTORY_SLOTS = 20

CONDITIONS_RU = (
    "Ослеплён", "Очарован", "Оглох", "Испуган", "Схвачен", "Недееспособен",
    "Невидим", "Парализован", "Окаменел", "Отравлен", "Сбит с ног",
    "Опутан", "Ошеломлён", "Без сознания", "Истощён",
    # Скрытность — не состояние SRD, а положение за столом: существа не
    # видно, его атаки идут с преимуществом, а по нему самому — с помехой.
    # В списке она последняя: порядок прежних состояний не сдвигается.
    "Скрыт",
)


def _slot_key(level) -> str:
    return str(int(level))


def ensure(sheet: dict) -> dict:
    """Гарантировать, что в листе есть блок инвентаря и заклинаний."""
    block = sheet.setdefault("vtt", {})
    block.setdefault("inventory", [])
    block.setdefault("spells", [])
    if "slots" not in block:
        block["slots"] = {str(lvl): {"max": mx, "used": 0}
                          for lvl, mx in DEFAULT_SLOTS.items()}
    block.setdefault("conditions", [])
    # Каждый предмет занимает ровно одну ячейку сетки. Порядок списка —
    # порядок ячеек; slot хранится в листе, чтобы позиция пережила сохранение.
    for index, item in enumerate(block["inventory"]):
        if not isinstance(item, dict):
            continue
        item["slot"] = index
        item.setdefault("slots", 1)
        _sync_charges(item)
    return block


def _sync_charges(item: dict):
    """Добавить/поправить заряды у старой записи инвентаря.

    items.json хранит только строку «заряды: 7 (на рассвете 1d6 + 1)».
    По ней заводим charges_max/charges, чтобы расход и отдых работали
    и для листов, созданных до появления зарядов. Одноразовые подтипы
    (зелье, свиток, еда, боеприпас, яд) зарядов не получают.
    """
    if not content_audit.is_chargeable(item):
        for key in ("charges", "charges_max", "charges_recovery"):
            item.pop(key, None)
        return
    maximum = content_audit.charges_max(item)
    item["charges_max"] = maximum
    try:
        current = int(item.get("charges", maximum))
    except (TypeError, ValueError):
        current = maximum
    item["charges"] = max(0, min(maximum, current))
    recovery = content_audit.charges_recovery(item)
    if recovery:
        item["charges_recovery"] = recovery
    else:
        item.pop("charges_recovery", None)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ----------------------------------------------------------------------
# Предметы
# ----------------------------------------------------------------------

def damage_type_of(rec: dict) -> str:
    """Тип урона предмета: «1d6 рубящий» -> ``slashing``.

    В справочнике тип лежит внутри текста формулы, а не отдельным полем.
    Нормализация общая с боем: иначе сопротивление цели не сработает.
    """
    from shared.combat import normalize_damage_type
    parts = [str(x) for x in (rec.get("formulas") or [])]
    parts.append(str(rec.get("activity_damage") or ""))
    return normalize_damage_type(" ".join(parts))


def item_from_library(rec: dict, qty: int = 1) -> dict:
    """Запись справочника -> строка инвентаря."""
    entry = {
        "id": new_id("item"),
        "source_id": rec.get("id") or "",
        "name": rec.get("name", "?"),
        "itype": rec.get("itype", ""),
        "subtype": rec.get("subtype", ""),
        "qty": max(1, int(qty)),
        "equipped": False,
        # что предмет умеет: урон, лечение, спасбросок
        "formulas": list(rec.get("formulas") or []),
        "activity_damage": rec.get("activity_damage") or "",
        "damage_type": rec.get("damage_type") or damage_type_of(rec),
        "slot": 0,
        "slots": 1,
        "heal": rec.get("heal") or "",
        "save_dc": rec.get("save_dc"),
        "attack_bonus": rec.get("attack_bonus"),
        "uses": rec.get("uses") or "",
        "weight": rec.get("weight") or "",
        "price": rec.get("price") or "",
        "rarity": rec.get("rarity") or "",
        # Настройка: требуется ли предмету и включена ли сейчас (лимит 3 — в item_rules)
        "attunement": rec.get("attunement") or "",
        "attuned": False,
        "description": rec.get("description") or "",
        "img": rec.get("img") or "",
    }
    _sync_charges(entry)
    return entry


def total_weight(sheet: dict) -> float:
    """Суммарный вес с учётом количества."""
    total = 0.0
    for item in ensure(sheet)["inventory"]:
        try:
            weight = float(str(item.get("weight", "0")).split()[0])
        except (ValueError, IndexError):
            continue
        total += weight * int(item.get("qty", 1) or 1)
    return round(total, 1)


def add_item(sheet: dict, rec: dict, qty: int = 1) -> dict:
    """Положить предмет в инвентарь (складываем одинаковые)."""
    block = ensure(sheet)
    source = rec.get("id") or rec.get("source_id")
    for item in block["inventory"]:
        if source and item.get("source_id") == source:
            item["qty"] = int(item.get("qty", 1)) + qty
            return item
    entry = item_from_library(rec, qty)
    # qty мог прийти внутри записи справочника — уважаем его тоже
    entry["qty"] = max(1, int(qty or entry.get("qty") or 1))
    entry["slot"] = len(block["inventory"])
    block["inventory"].append(entry)
    return entry


def move_item(sheet: dict, item_id: str, slot: int) -> bool:
    """Переставить предмет в ячейку сетки (перетаскивание)."""
    block = ensure(sheet)
    inventory = block["inventory"]
    old = next((i for i, item in enumerate(inventory)
                if item.get("id") == item_id), None)
    if old is None:
        return False
    try:
        target = max(0, min(len(inventory) - 1, int(slot)))
    except (TypeError, ValueError):
        return False
    item = inventory.pop(old)
    inventory.insert(target, item)
    for index, entry in enumerate(inventory):
        entry["slot"] = index
        entry.setdefault("slots", 1)
    return True


def inventory_slots_used(sheet: dict) -> int:
    """Сколько ячеек занято: один предмет — одна ячейка, стак — тоже одна."""
    return len(ensure(sheet)["inventory"])


def remove_item(sheet: dict, item_id: str, qty: int = None) -> bool:
    """Убрать предмет; qty=None — весь стак. True, если что-то изменилось."""
    block = ensure(sheet)
    for i, item in enumerate(block["inventory"]):
        if item.get("id") != item_id:
            continue
        if qty is None or int(item.get("qty", 1)) <= qty:
            block["inventory"].pop(i)
        else:
            item["qty"] = int(item.get("qty", 1)) - qty
        return True
    return False


def find_item(sheet: dict, item_id: str):
    for item in ensure(sheet)["inventory"]:
        if item.get("id") == item_id:
            return item
    return None


def consume(sheet: dict, item_id: str) -> bool:
    """Потратить одну штуку расходника. False, если предмета нет."""
    item = find_item(sheet, item_id)
    if not item:
        return False
    qty = int(item.get("qty", 1))
    if qty <= 1:
        return remove_item(sheet, item_id)
    item["qty"] = qty - 1
    return True


def consume_charge(sheet: dict, item_id: str) -> bool:
    """Потратить один заряд, не убирая предмет из инвентаря."""
    item = find_item(sheet, item_id)
    if not item or not item.get("charges_max"):
        return False
    try:
        charges = int(item.get("charges", item.get("charges_max", 0)))
    except (TypeError, ValueError):
        return False
    if charges <= 0:
        return False
    item["charges"] = charges - 1
    return True


# ----------------------------------------------------------------------
# Заклинания
# ----------------------------------------------------------------------

def _ascii_key(value) -> str:
    """Ключ сравнения названий: без регистра, знаков и служебных «or».

    LSS пишет «Antipathy or Sympathy», справочник — «Antipathy/Sympathy».
    """
    text = str(value or "")
    bracketed = re.search(r"\[([^\]]+)\]", text)
    if bracketed:
        text = bracketed.group(1)
    text = (unicodedata.normalize("NFKD", text)
            .encode("ascii", "ignore").decode("ascii").lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b(?:or|and|the)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _lss_ref_text(ref) -> str:
    """Ссылка из книги LSS -> строка (id, слаг или название)."""
    if isinstance(ref, dict):
        for key in ("id", "slug", "name", "source"):
            if ref.get(key):
                return str(ref[key]).strip()
        return ""
    return str(ref or "").strip()


def _catalog_by_name(records) -> dict:
    """Индекс справочника: нормализованное английское имя -> запись."""
    index = {}
    for rec in records or []:
        if not isinstance(rec, dict) or rec.get("kind", "spell") != "spell":
            continue
        for value in (rec.get("source_name"), rec.get("name")):
            key = _ascii_key(value)
            if key:
                index.setdefault(key, rec)
    return index


def _lss_official(ref: str, edition: str = ""):
    """Найти заклинание LSS по id и вернуть (круг, английское имя, слаг)."""
    try:
        from shared.lss_spell_index import LSS_SPELLS
    except ImportError:
        return None
    order = []
    for value in (edition, "2014", "2024"):
        value = str(value or "")
        if value and value not in order:
            order.append(value)
    for value in order:
        found = (LSS_SPELLS.get(value) or {}).get(ref)
        if found:
            slug, english, level = found
            return int(level), english, slug
    return None


def _find_catalog_spell(ref: str, edition: str, index: dict):
    """Ссылка LSS -> запись нашего справочника (или None)."""
    official = _lss_official(ref, edition)
    if official:
        level, english, slug = official
        rec = index.get(_ascii_key(english))
        if rec is not None:
            return rec
        # Слаг нужен для «Antipathy or Sympathy» и других расхождений.
        rec = index.get(_ascii_key(slug.replace("-", " ")))
        if rec is not None:
            return rec
        return None
    # Лист может хранить не id, а слаг/английское имя (старые и ручные
    # экспорты LSS). Сначала точное совпадение, потом по словам.
    key = _ascii_key(ref)
    if not key:
        return None
    rec = index.get(key)
    if rec is not None:
        return rec
    words = set(key.split())
    best = None
    best_score = 0.0
    for candidate, entry in index.items():
        other = set(candidate.split())
        if not other:
            continue
        score = len(words & other) / max(1, len(words | other))
        if score > best_score:
            best, best_score = entry, score
    return best if best_score >= 0.8 else None


def _lss_spell_entry(ref: str, edition: str, index: dict) -> dict:
    """Создать строку книги: полная запись справочника или заглушка."""
    official = _lss_official(ref, edition)
    rec = _find_catalog_spell(ref, edition, index)
    if rec is not None:
        entry = spell_from_library(rec)
        # Круг берём из листа: локальный справочник может быть другой
        # редакции, а подготовка и ячейки считаются по листу.
        if official:
            entry["level"] = int(official[0])
        return entry
    if official:
        level, english, slug = official
        name, source = english, f"lss:{slug}"
    elif re.fullmatch(r"[0-9a-fA-F]{16,32}", ref):
        # Чужое заклинание из личного гримуара: id без названия.
        return {}
    else:
        name, source, level = ref, f"lss:{ref}", 0
    return {
        "id": new_id("spell"), "source_id": source, "name": name,
        "level": int(level), "school": "", "prepared": False,
        "damage": "", "save": "", "save_dc": None, "attack": "",
        "attack_bonus": None, "range": "", "activation": "",
        "duration": "", "concentration": False, "ritual": False,
        "components": "", "scaling": "", "description":
        "Заклинание из книги Long Story Short; в локальном справочнике "
        "нет его текста.", "img": "",
    }


def _slot_number(node, default=0) -> int:
    """Число из LSS-ячейки: int или {value/filled}."""
    while isinstance(node, dict):
        node = node.get("value", node.get("filled", default))
    try:
        return max(0, int(node))
    except (TypeError, ValueError):
        return default


def _import_lss_slots(sheet: dict, block: dict) -> None:
    """Перенести счётчики кругов из data.spells / spellsPact в vtt.slots."""
    pools = []
    for source_key in ("spells", "spellsPact"):
        source = sheet.get(source_key)
        if not isinstance(source, dict):
            continue
        for key, node in source.items():
            match = re.fullmatch(r"slots?-(\d)", str(key))
            if not match or not isinstance(node, dict):
                continue
            level = int(match.group(1))
            if 1 <= level <= 9:
                pools.append((source_key, level, node))
    if not pools:
        return
    # Счётчики LSS — точная картина листа, а не добавка к заготовке
    # DEFAULT_SLOTS: иначе у клирика 3 уровня висели бы чужие 2/3 и 1/1.
    for level in range(1, 10):
        block["slots"][str(level)] = {"max": 0, "used": 0}
    for source_key, level, node in pools:
        cell = block["slots"].setdefault(str(level), {"max": 0, "used": 0})
        maximum = _slot_number(node.get("value")) + int(cell.get("max", 0) or 0)
        filled = _slot_number(node.get("filled")) + int(cell.get("used", 0) or 0)
        cell["max"] = maximum
        cell["used"] = min(maximum, filled)


def import_lss_spells(sheet: dict, records, book: dict = None,
                      edition: str = "") -> int:
    """Подтянуть книгу заклинаний из обёртки LSS в ``vtt.spells``.

    LSS хранит только ссылки (id/слаг), текст берём из локального
    справочника. Повторный вызов с той же книгой ничего не делает, поэтому
    обновление вкладки не плодит заклинания и не сбрасывает подготовку.
    """
    block = ensure(sheet)
    book = book if isinstance(book, dict) else block.get("lss_spells")
    if not isinstance(book, dict):
        return 0
    edition = str(edition or block.get("lss_edition") or "")
    signature = json.dumps(book, sort_keys=True, ensure_ascii=False,
                           default=str)
    if block.get("lss_spells_sig") == signature:
        return 0
    index = _catalog_by_name(records)

    refs = []
    for field, prepared in (("prepared", True), ("granted", True),
                            ("book", False)):
        for raw in book.get(field) or []:
            ref = _lss_ref_text(raw)
            if ref:
                refs.append((ref, prepared))
    seen = set()
    added = 0
    for ref, prepared in refs:
        key = ref.casefold()
        if key in seen:
            continue
        seen.add(key)
        entry = _lss_spell_entry(ref, edition, index)
        if not entry:
            continue
        source = entry.get("source_id")
        existing = next((s for s in block["spells"]
                         if source and s.get("source_id") == source), None)
        if existing is None:
            block["spells"].append(entry)
            existing = entry
            added += 1
        existing["prepared"] = bool(prepared
                                    or int(existing.get("level") or 0) == 0)
        existing["lss_ref"] = ref

    _import_lss_slots(sheet, block)
    block["lss_spells_sig"] = signature
    return added


def spell_from_library(rec: dict) -> dict:
    """Запись справочника -> строка книги заклинаний."""
    return {
        "id": new_id("spell"),
        "source_id": rec.get("id") or "",
        "name": rec.get("name", "?"),
        "level": int(rec.get("level") or 0),
        "school": rec.get("school") or "",
        "prepared": int(rec.get("level") or 0) == 0,   # заговоры всегда готовы
        "damage": rec.get("damage") or "",
        "save": rec.get("save") or "",
        "save_dc": rec.get("save_dc"),
        "attack": rec.get("attack") or "",
        "attack_bonus": rec.get("attack_bonus"),
        "range": rec.get("range") or "",
        "activation": rec.get("activation") or "",
        "duration": rec.get("duration") or "",
        "concentration": bool(rec.get("concentration")),
        "ritual": bool(rec.get("ritual")),
        "components": rec.get("components") or "",
        "scaling": rec.get("scaling") or "",
        "description": rec.get("description") or "",
        "img": rec.get("img") or "",
    }


def add_spell(sheet: dict, rec: dict) -> dict:
    """Добавить заклинание в книгу (повтор не плодим)."""
    block = ensure(sheet)
    source = rec.get("id") or rec.get("source_id")
    for spell in block["spells"]:
        if source and spell.get("source_id") == source:
            return spell
    entry = spell_from_library(rec)
    block["spells"].append(entry)
    return entry


def find_spell(sheet: dict, spell_id: str):
    for spell in ensure(sheet)["spells"]:
        if spell.get("id") == spell_id:
            return spell
    return None


def remove_spell(sheet: dict, spell_id: str) -> bool:
    block = ensure(sheet)
    for i, spell in enumerate(block["spells"]):
        if spell.get("id") == spell_id:
            block["spells"].pop(i)
            return True
    return False


def slots_for(sheet: dict, level: int) -> dict:
    """Состояние ячеек уровня: {'max': N, 'used': M}."""
    block = ensure(sheet)
    key = _slot_key(level)
    return block["slots"].setdefault(key, {"max": 0, "used": 0})


def can_cast(sheet: dict, spell: dict) -> tuple:
    """Можно ли применить заклинание: (да/нет, причина).

    Заговоры (уровень 0) не тратят ячейки. Ритуальные вне боя тоже.
    """
    level = int(spell.get("level") or 0)
    if level == 0:
        return True, ""
    if not spell.get("prepared"):
        return False, "Заклинание не подготовлено"
    cells = slots_for(sheet, level)
    if int(cells.get("used", 0)) >= int(cells.get("max", 0)):
        # ячейки выше уровнем тоже подходят (как в 5e), проверяем их
        for higher in range(level + 1, 10):
            up = slots_for(sheet, higher)
            if int(up.get("used", 0)) < int(up.get("max", 0)):
                return True, ""
        return False, f"Нет свободных ячеек {level} уровня"
    return True, ""


def spend_slot(sheet: dict, level: int) -> bool:
    """Потратить ячейку: сначала своего уровня, затем выше."""
    if int(level) == 0:
        return True
    cells = slots_for(sheet, level)
    if int(cells.get("used", 0)) < int(cells.get("max", 0)):
        cells["used"] = int(cells.get("used", 0)) + 1
        return True
    for higher in range(int(level) + 1, 10):
        up = slots_for(sheet, higher)
        if int(up.get("used", 0)) < int(up.get("max", 0)):
            up["used"] = int(up.get("used", 0)) + 1
            return True
    return False


def rest(sheet: dict, long: bool = True, heal: str = ""):
    """Отдых по правилам 5e.

    ``long=True`` — долгий отдых: все ячейки, все заряды особенностей и
    предметов, полные хиты, минус одна ступень истощения, снятие прочих
    состояний. ``long=False`` — короткий отдых: только то, что по правилам
    восстанавливается на коротком отдыхе (ячейки НЕ возвращаются: в 5e
    их даёт долгий отдых, а не короткий). ``heal="half"`` дополнительно
    поднимает хиты на половину максимума — так короткий отдых тратит
    кости хитов за игрока.
    """
    block = ensure(sheet)
    if long:
        for cells in block["slots"].values():
            cells["used"] = 0
    elif heal:
        vit = sheet.setdefault("vitality", {})
        try:
            hp_max = int((vit.get("hp-max") or {}).get("value") or 0)
            hp_now = int((vit.get("hp-current") or {}).get("value") or 0)
        except (TypeError, ValueError):
            hp_max = hp_now = 0
        if hp_max > 0:
            gained = (hp_max + 1) // 2
            vit.setdefault("hp-current", {})["value"] = min(
                hp_max, max(0, hp_now) + gained)
    if long:
        # Наложение рук восстанавливает весь запас только на долгом отдыхе —
        # короткий отдых его не возвращает (правила паладина).
        pool = block.get("lay_on_hands")
        if isinstance(pool, dict):
            pool["used"] = 0
        vit = sheet.setdefault("vitality", {})
        hp_max = (vit.get("hp-max") or {}).get("value") or 0
        if hp_max:
            vit.setdefault("hp-current", {})["value"] = hp_max
        # Долгий отдых в 5e снимает только ОДИН уровень истощения, а не
        # стирает его целиком. Остальные состояния к утру теряют срок,
        # поэтому их убираем, а ступени истощения уменьшаем на одну.
        from shared import conditions as conditions_rules
        left = []
        for entry in block["conditions"]:
            raw = entry.get("name") if isinstance(entry, dict) else entry
            if conditions_rules.canonical(raw) != "Истощён":
                continue
            level = conditions_rules.exhaustion_level([raw]) - 1
            if level < 1:
                continue
            left.append({"name": "Истощён" if level == 1
                         else f"Истощён {level}",
                         "rounds": (entry.get("rounds")
                                    if isinstance(entry, dict) else None)})
        block["conditions"] = left
        # Длинный отдых (обычно до рассвета) возвращает заряды жезлов,
        # посохов, колец и переиспользуемых чудесных предметов.
        for item in block["inventory"]:
            if item.get("charges_max"):
                item["charges"] = int(item["charges_max"])
    # Заряды классовых особенностей (Второе дыхание, Ярость, Ци, Вдохновение
    # барда) живут в `vtt.feature_uses` и возвращаются по правилу самой
    # особенности: короткий отдых — только «до короткого отдыха», долгий — всё.
    # Без этого Второе дыхание лечило бесконечно, а Ярость не кончалась.
    from shared import class_features
    class_features.restore_resources(sheet, long=long)
    if long and class_features.lay_on_hands_max(sheet):
        # Только паладину: иначе долгий отдых заводил бы пустой запас
        # лечения в листе каждого персонажа.
        class_features.lay_on_hands_restore(sheet)


# ----------------------------------------------------------------------
# Состояния (условия) — как эффекты в Foundry
# ----------------------------------------------------------------------

def add_condition(sheet: dict, name: str, rounds: int = None) -> dict:
    block = ensure(sheet)
    entry = {"name": name, "rounds": rounds}
    block["conditions"].append(entry)
    return entry


def remove_condition(sheet: dict, name: str) -> bool:
    block = ensure(sheet)
    for i, cond in enumerate(block["conditions"]):
        if cond.get("name") == name:
            block["conditions"].pop(i)
            return True
    return False


def tick_conditions(sheet: dict, rounds: int = 1):
    """Прошёл раунд: у временных состояний уменьшается счётчик."""
    block = ensure(sheet)
    keep = []
    for cond in block["conditions"]:
        left = cond.get("rounds")
        if left is None:
            keep.append(cond)
            continue
        left -= rounds
        if left > 0:
            cond["rounds"] = left
            keep.append(cond)
    block["conditions"] = keep
