"""Единое действие использования предмета или заклинания.

По образцу Activity в Foundry: и заклинание, и зелье описываются одним
объектом ``UseAction``. Дальше пайплайн один:

    проверка → выбор цели → расход → бросок → применение → чат

Модуль ничего не знает о Qt и сети: он только собирает действие из записи
листа, проверяет расход и списывает его. Поэтому правила можно тестировать
без окна и сервера.
"""
from dataclasses import dataclass, asdict, field, fields
import re
from typing import Any, Dict, List, Optional

from shared import sheet_items
from shared import content_audit
from shared import combat as combat_rules
from shared import item_effects
from shared import spell_effects
from shared import spells


#: Виды действий. `pool` — заклинание с пулом хитов (Сон, Цветной шар),
#: `revive` — возврат к жизни, `dispel` — развеивание чужой магии,
#: `save_target` — одиночный спасбросок (Священное пламя, Злая насмешка).
#: Снятие состояний (Восстановление) идёт видом `buff` с полем `cures`.
KINDS = ("attack", "save", "save_target", "damage", "heal", "darts",
         "utility", "heal_pool", "pool", "revive", "dispel")
TARGET_MODES = ("self", "selected", "ally", "template", "area")

_HEAL_RE = re.compile(r"(лечен|heal)", re.IGNORECASE)
_FORMULA_RE = re.compile(r"(\d*d\d+(?:\s*[+-]\s*\d+)?)")
_ABILITY_RE = (
    ("сил", "str"), ("ловк", "dex"), ("телос", "con"),
    ("вынос", "con"), ("интел", "int"), ("мудр", "wis"),
    ("харизм", "cha"), ("обаян", "cha"),
)


@dataclass
class UseAction:
    """Одно механическое действие: чем пользуются, что делают, куда тратят."""

    #: spell | item
    source: str
    #: attack | save | damage | heal | utility
    kind: str
    name: str
    formula: str = ""
    #: self | selected | ally | area | template
    target_mode: str = "selected"
    #: Область заклинания: {'shape': 'circle', 'size_ft': 20, ...}.
    #: Пустой словарь — обычное заклинание по одной цели.
    area: Dict[str, Any] = field(default_factory=dict)
    half_on_success: bool = False
    #: {'type': 'spell_slot', 'level': 1, 'prepared': True}
    #: или {'type': 'item_quantity'} / {'type': 'none'}
    consumption: Dict[str, Any] = field(default_factory=dict)
    #: Дистанция заклинания в футах (0 — не ограничиваем: неизвестна).
    #: Сервер сверяет с ней центр области, чтобы клиент не «дотянулся»
    #: Огненным шаром через всю карту.
    range_ft: int = 0
    save: str = ""
    ability: str = "dex"
    dc: Optional[int] = None
    attack_bonus: Optional[int] = None
    damage_type: str = ""
    effects: List[dict] = field(default_factory=list)
    source_id: str = ""
    item_id: str = ""
    spell_id: str = ""
    #: токен, на чьей странице нажали «Применить» (у мастера — адресно)
    token_id: str = ""
    #: цель, выбранная перетаскиванием предмета на токен; важнее выделения
    target_id: str = ""
    #: Ключ заклинания в реестре `shared.spells` (fireball, magic_missile…).
    #: Пустой — заклинание разбирается по тексту, как раньше.
    spell_key: str = ""
    #: Ключ отклика: анимация и звук на клиенте.
    fx: str = ""
    #: Реакция (Щит): применять можно и вне своего хода.
    reaction: bool = False
    #: Волшебная стрела: сколько снарядов и какой кубик у каждого.
    darts: int = 0
    dart_formula: str = ""
    #: Благословение: сколько существ и в каком радиусе выбирать.
    targets_max: int = 0
    radius_ft: int = 0
    #: Цепная молния: сколько разрядов уходит от первой цели и в каком
    #: радиусе от неё выбираются второстепенные цели (SRD: 3 и 30 футов,
    #: +1 разряд за круг ячейки выше шестого).
    chain: int = 0
    chain_radius_ft: int = 0
    #: Пул лечения (Массовое исцеление): сколько хитов распределяет каст.
    pool: int = 0
    #: Состояния, которые заклинание снимает (Восстановление, Защита от яда).
    cures: List[str] = field(default_factory=list)
    #: Возврат к жизни: с какими хитами поднимается цель.
    revive_hp: int = 0
    #: Снятие состояний (Малое восстановление): только одно из списка.
    cures_one: bool = False
    #: Концентрация и длительность эффекта в раундах.
    concentration: bool = False
    duration_rounds: int = 0
    activation: str = ""
    #: Классовая особенность, а не предмет/заклинание: divine_smite,
    #: lay_on_hands. Сервер по этому ключу знает, что именно проверять.
    feature_key: str = ""
    #: Наложение рук: снять отравление (стоит 5 хитов запаса).
    cure: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data) -> "UseAction":
        payload = dict(data or {})
        allowed = {f.name for f in fields(UseAction)}
        return UseAction(**{key: value for key, value in payload.items()
                            if key in allowed})


# ----------------------------------------------------------------------
# Разбор текста справочника
# ----------------------------------------------------------------------
def formula_from(text) -> str:
    """Вытащить формулу кубика: «1d8 лечение» → «1d8»."""
    match = _FORMULA_RE.search(str(text or ""))
    if match:
        return match.group(1).replace(" ", "")
    # Плоское число тоже допустимо, но только если это всё поле:
    # «70» → «70», а «70 футов» — уже дистанция, не эффект.
    plain = str(text or "").strip()
    if re.fullmatch(r"\d+", plain):
        return plain
    return ""


def is_healing(text) -> bool:
    return bool(_HEAL_RE.search(str(text or "")))


def range_feet(text) -> int:
    """Дистанция из карточки: «150 футов» → 150, «1 миля» → 5280.

    Неизвестная или пустая строка даёт 0: тогда сервер дистанцию не
    ограничивает, чтобы заклинание без поля `range` не отказывало.
    """
    match = re.search(r"(\d+)\s*(фут|feet|foot|мил|mile)",
                      str(text or ""), re.IGNORECASE)
    if not match:
        return 0
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return 0
    unit = match.group(2).casefold()
    if unit.startswith("мил") or unit.startswith("mile"):
        value *= 5280
    return max(0, min(5280, value))


def is_speed_potion(rec: dict) -> bool:
    """Зелье скорости: у него нет ни формулы, ни спасброска."""
    name = str((rec or {}).get("name") or "").strip().casefold()
    if not name:
        return False
    if name in ("potion of speed", "haste potion"):
        return True
    return "зель" in name and "скорост" in name


def is_consumable(rec: dict) -> bool:
    """Тратится ли штука из стака (зелье, свиток, еда, боеприпас)."""
    return content_audit.consumption_kind(rec) == "quantity"


def can_apply_to_token(rec: dict) -> bool:
    """Можно ли применить предмет перетаскиванием на токен.

    Тащим только расходники: зелья, масла, яды, алхимический огонь. Оружие
    и снаряжение остаются в инвентаре — меч, брошенный на токен, не должен
    неожиданно открывать диалог атаки вместо лечения.
    """
    if not isinstance(rec, dict) or str(rec.get("itype") or "") != "Расходник":
        return False
    action = from_item(rec)
    if action.kind == "utility":
        return bool(action.effects)
    return action.kind in ("heal", "attack")


def save_ability(text) -> str:
    """Характеристика спасброска из строки справочника."""
    value = str(text or "").casefold()
    for needle, ability in _ABILITY_RE:
        if needle in value:
            return ability
    return "dex"


def spellcasting_modifier(sheet: dict) -> int:
    """Модификатор базовой характеристики заклинаний листа."""
    from shared import lss_format as lss
    base = ((sheet.get("spellsInfo") or {}).get("base") or {}).get("code") or "int"
    stats = sheet.get("stats", {})
    return lss.modifier((stats.get(base) or {}).get("score", 10))


def spell_dc(sheet: dict) -> int:
    """Сл заклинаний: 8 + мастерство + модификатор базовой характеристики."""
    from shared import lss_format as lss
    return 8 + lss.proficiency_bonus(sheet) + spellcasting_modifier(sheet)


def spell_attack_bonus(sheet: dict) -> int:
    from shared import lss_format as lss
    base = ((sheet.get("spellsInfo") or {}).get("base") or {}).get("code") or "int"
    stats = sheet.get("stats", {})
    return (lss.modifier((stats.get(base) or {}).get("score", 10))
            + lss.proficiency_bonus(sheet))


#: Классы, которые ГОТОВЯТ заклинания: без галочки «подготовлено» не колдуют.
_PREPARED_CASTERS = ("cleric", "druid", "wizard", "paladin")
#: Классы, которые ЗНАЮТ заклинания: галочка подготовки им не нужна
#: (бард, чародей, колдун, следопыт).
_KNOWN_CASTERS = ("bard", "sorcerer", "warlock", "ranger")


def needs_preparation(sheet: dict) -> bool:
    """Требует ли этот персонаж подготовки заклинаний (5e).

    Раньше галочка требовалась всем: у следопыта, барда или чародея
    заклинание с листа не срабатывало вовсе, если в листе не была
    отмечена подготовка. В 5e же готовят только жрец, друид, волшебник и
    паладин; остальные знают свою книгу. Заговоры (0 круг) доступны всегда.

    Если класс неизвестен, действуем по-старому — требуем подготовку:
    лист без класса всё равно нечем проверить.
    """
    try:
        from shared import class_features
        levels = class_features.class_levels(sheet or {})
    except Exception:
        return True
    casters = [key for key in levels
               if key in _PREPARED_CASTERS or key in _KNOWN_CASTERS]
    if not casters:
        return True
    return any(key in _PREPARED_CASTERS for key in casters)


def _slot_free(sheet: dict, level: int) -> bool:
    for probe in range(int(level), 10):
        cells = sheet_items.slots_for(sheet, probe)
        if int(cells.get("used", 0)) < int(cells.get("max", 0)):
            return True
    return False


def _registered_note(spec: dict) -> str:
    """Подпись действия: правило плюс круг потраченной ячейки."""
    text = _registered_note_core(spec)
    if spec.get("slot_level"):
        text += f" · ячейка {spec['slot_level']} круга"
    return text


def _registered_note_core(spec: dict) -> str:
    """Короткая подпись действия для чата и статус-строки."""
    name = str(spec.get("name") or "Заклинание")
    kind = str(spec.get("kind") or "")
    if kind == "pool":
        # Сон и Цветной шар: пул хитов, а не урон со спасброском.
        state = next((str(effect.get("condition") or "")
                      for effect in (spec.get("effects") or [])
                      if isinstance(effect, dict)
                      and effect.get("condition")), "")
        text = f"{name} — пул {spec.get('formula')} хитов"
        if state:
            text += f": {state.casefold()}"
        return text
    if kind == "revive":
        return (f"{name} — возвращает к жизни "
                f"с {int(spec.get('revive_hp') or 1)} хитом")
    if kind == "dispel":
        return f"{name} — снимает эффекты заклинаний с цели"
    if kind == "heal":
        if int(spec.get("pool") or 0):
            # Массовое исцеление: 700 хитов на всех выбранных, каждый
            # лечится до полного, остаток идёт следующим.
            return (f"{name} — пул {int(spec['pool'])} хитов: каждый лечится "
                    "до полного, остаток идёт следующим")
        text = f"{name} — лечение {spec.get('formula')}"
        if int(spec.get("targets_max") or 0) > 1:
            # Массовое лечение: в чате видно, сколько существ поднимает
            # одна ячейка, иначе оно выглядело бы лечением одной цели.
            text += f" до {int(spec['targets_max'])} целей"
        return text
    if kind == "darts":
        return (f"{name} — {spec.get('darts')} снаряда по "
                f"{spec.get('dart_formula')}, попадают всегда")
    if spec.get("key") == "shield":
        return f"{name} — реакция: +5 КД до начала следующего хода"
    if spec.get("key") == "bless":
        return (f"{name} — концентрация, до {spec.get('targets_max')} существ: "
                "+1d4 к атакам и спасброскам")
    if spec.get("key") == "haste":
        return (f"{name} — концентрация, до {spec.get('rounds')} раундов: "
                "×2 движение, +2 КД, преимущество на Ловкость")
    if spec.get("key") == "mage_armor":
        return (f"{name} — КД 13 + модификатор Ловкости "
                f"({spec.get('duration') or '8 часов'})")
    if spec.get("key") == "hunters_mark":
        return (f"{name} — концентрация до {spec.get('duration') or '1 часа'}: "
                "+1к6 к урону по помеченной цели")
    # Универсальные правила из таблицы SRD: подпись собирается из эффектов,
    # иначе каждое новое заклинание молчало бы в чате «эффект».
    labels = []
    for effect in spec.get("effects") or []:
        if not isinstance(effect, dict):
            continue
        text = str(effect.get("label") or "").strip()
        # Не повторяем имя заклинания в каждом эффекте: «Метка — Метка».
        if text.startswith(name):
            text = text[len(name):].lstrip(" :—-»")
        if text and text not in labels:
            labels.append(text)
    if labels:
        return f"{name} — " + "; ".join(labels[:3]) + (
            " и др." if len(labels) > 3 else "")
    if spec.get("save") and kind in ("save", "save_target", "buff"):
        from shared.lss_format import ABILITY_RU
        ability = str(spec.get("save") or "")
        text = (f"{name} — спасбросок "
                f"{ABILITY_RU.get(ability, ability).casefold()}"
                + (f" (Сл {spec.get('dc')})" if spec.get("dc") else ""))
        if kind == "save_target" and spec.get("formula"):
            text += (f": провал — {spec['formula']}"
                     + (", успех — половина" if spec.get("half_on_success")
                        else ", успех — ничего"))
        return text
    if kind == "area":
        shapes = {"circle": "сфера", "cone": "конус", "cube": "куб",
                  "line": "линия", "cylinder": "цилиндр"}
        shape = shapes.get(str(spec.get("shape") or "circle"), "область")
        return (f"{name} — {spec.get('formula')} уроном, "
                f"{shape} {spec.get('size_ft')} фут")
    if kind == "attack":
        return f"{name} — атака заклинанием {spec.get('formula')}"
    return f"{name} — эффект"


def _from_registered_spell(spell: dict, sheet: dict, token_id: str,
                           spec: dict) -> "UseAction":
    """Собрать действие для заклинания из реестра `shared.spells`.

    Формулы, дистанция и область берутся из правила, а не из текста
    описания: так «1d4 + модификатор» не зависит от формулировки русского
    перевода. Урон из записи справочника сохраняется, если он разобран.
    """
    rule_kind = str(spec.get("kind") or "utility")
    # Вид действия в UseAction исторический: область спасброска — это
    # `save` (её обрабатывает шаблон на карте), а не отдельный `area`.
    kind = "save" if rule_kind == "area" else rule_kind
    level = int(spec.get("level") or 0)
    name = str(spec.get("name") or "Заклинание")
    formula = str(spec.get("formula") or "")
    shape = str(spec.get("shape") or "")
    area = {}
    if kind in ("save", "pool") and shape:
        # Область нужна и урону по площади, и пулу: Сон — сфера 20 футов,
        # Цветной шар — конус 15 футов. Без неё клиент не открыл бы шаблон.
        # Форма пустая — шаблона у заклинания нет вовсе (одиночный
        # спасбросок): придумывать сферу в 20 футов нельзя, под неё
        # попали бы соседние токены, а правила говорят про одну цель.
        area = {"shape": shape,
                "size_ft": int(spec.get("size_ft") or 20)}
        if spec.get("half_on_success"):
            area["half_on_success"] = True
    dc = None
    if kind in ("save", "save_target"):
        dc = spell.get("save_dc")
        if dc is None:
            dc = spell_dc(sheet)
    bonus = None
    if kind == "attack":
        bonus = spell.get("attack_bonus")
        if bonus is None:
            bonus = spell_attack_bonus(sheet)
    effects = [dict(effect) for effect in (spec.get("effects") or [])
               if isinstance(effect, dict)]
    if not effects and formula and kind in ("heal", "save", "darts", "attack"):
        effects = [{"type": kind, "formula": formula}]
    return UseAction(
        source="spell",
        kind=kind,
        name=name,
        formula=formula,
        target_mode=str(spec.get("target_mode") or "selected"),
        area=area,
        # Половина урона при успехе бывает и у одиночного спасброска
        # (Увядание): её берём из правила, а не из разобранной зоны.
        half_on_success=bool(area.get("half_on_success")
                             or spec.get("half_on_success")),
        consumption=({"type": "spell_slot", "level": level,
                      "prepared": bool(spell.get("prepared"))}
                     if level else {"type": "none"}),
        range_ft=int(spec.get("range_ft") or 0),
        save=str(spec.get("save") or ""),
        ability=save_ability(spec.get("save") or spell.get("save")),
        dc=int(dc) if dc is not None else None,
        attack_bonus=int(bonus) if bonus is not None else None,
        damage_type=str(spec.get("damage_type") or ""),
        effects=effects,
        source_id=str(spell.get("source_id") or ""),
        spell_id=str(spell.get("id") or ""),
        token_id=token_id or "",
        spell_key=str(spec.get("key") or ""),
        fx=str(spec.get("fx") or ""),
        reaction=bool(spec.get("reaction")),
        darts=int(spec.get("darts") or 0),
        dart_formula=str(spec.get("dart_formula") or ""),
        targets_max=int(spec.get("targets_max") or 0),
        radius_ft=int(spec.get("radius_ft") or 0),
        chain=int(spec.get("chain") or 0),
        chain_radius_ft=int(spec.get("chain_radius_ft") or 0),
        pool=int(spec.get("pool") or 0),
        # Состояния, которые заклинание снимает: сервер проверяет их
        # по записи листа, а не по присланному списку.
        cures=[str(value) for value in (spec.get("cures") or ())],
        cures_one=bool(spec.get("cures_one")),
        revive_hp=int(spec.get("revive_hp") or 0),
        concentration=bool(spec.get("concentration")),
        duration_rounds=int(spec.get("rounds") or 0),
        activation=str(spec.get("activation") or spell.get("activation") or ""),
        # Особые заклинания (Сон, Туманный шаг) объясняют правило в note:
        # это честнее, чем молча применить к одному существу пул хитов.
        note=str(spec.get("note") or _registered_note(spec)),
    )


# ----------------------------------------------------------------------
# Сборка действия
# ----------------------------------------------------------------------
def from_spell(spell: dict, sheet: dict = None, token_id: str = "",
               slot_level=None) -> UseAction:
    """Заклинание листа → UseAction.

    В справочнике лечение лежит в поле damage с пометкой «лечение»:
    «1d8 лечение». Поэтому вид действия определяется по тексту, а не по
    отдельному полю, иначе Cure Wounds считался бы уроном.
    """
    sheet = sheet or {}
    registered = spells.plan(spell, sheet, slot_level=slot_level)
    if registered:
        return _from_registered_spell(spell, sheet, token_id, registered)
    name = str(spell.get("name") or "?")
    raw = str(spell.get("damage") or "")
    level = int(spell.get("level") or 0)
    formula = formula_from(raw)
    healing = is_healing(raw)
    has_save = bool(spell.get("save"))
    has_attack = bool(spell.get("attack"))
    description = str(spell.get("description") or "").casefold()

    area = spell_effects.area_for_spell(spell) if not healing else {}
    if healing:
        kind = "heal"
        target_mode = "ally"
        # В справочнике формула записана без модификатора характеристики
        # («1d4 лечение»), хотя в тексте он есть. Без него «Лечащее слово»
        # лечило на 1к4 вместо 1к4+5 у паладина с Харизмой 20.
        if (formula and not re.search(r"[+-]\s*\d", formula)
                and ("модификатор" in description
                     or "modifier" in description)):
            mod = spellcasting_modifier(sheet)
            if mod:
                formula = f"{formula}{mod:+d}"
    elif has_save:
        kind = "save"
        target_mode = "area" if area else "selected"
    elif has_attack:
        kind = "attack"
        target_mode = "selected"
    elif formula:
        kind = "damage"
        target_mode = "selected"
    else:
        kind = "utility"
        target_mode = "self"

    dc = spell.get("save_dc")
    if dc is None and has_save:
        dc = spell_dc(sheet)
    bonus = spell.get("attack_bonus")
    if bonus is None and has_attack:
        bonus = spell_attack_bonus(sheet)

    note = f"{name} — " + {
        "heal": "лечение", "save": "спасбросок",
        "attack": "атака", "damage": "урон", "utility": "эффект",
    }[kind]
    return UseAction(
        source="spell",
        kind=kind,
        name=name,
        formula=formula,
        target_mode=target_mode,
        consumption=({"type": "spell_slot", "level": level,
                      "prepared": bool(spell.get("prepared"))}
                     if level else {"type": "none"}),
        save=str(spell.get("save") or ""),
        ability=save_ability(spell.get("save")),
        dc=int(dc) if dc is not None else None,
        attack_bonus=int(bonus) if bonus is not None else None,
        damage_type=combat_rules.damage_type_from_text(raw),
        effects=[{"type": kind, "formula": formula}] if formula else [],
        source_id=str(spell.get("source_id") or ""),
        spell_id=str(spell.get("id") or ""),
        token_id=token_id or "",
        range_ft=range_feet(spell.get("range")),
        area=area,
        half_on_success=bool(area.get("half_on_success")) if area else False,
        note=note,
    )


def from_feature(feature: dict, sheet: dict = None,
                 token_id: str = "") -> UseAction:
    """Классовая особенность листа → UseAction.

    Сейчас так работает только Наложение рук: у него есть запас хитов,
    который тратится не ячейкой и не предметом. Кара открывает окно атаки
    со своим выбором круга, поэтому в UseAction не превращается.
    """
    sheet = sheet or {}
    feature = feature if isinstance(feature, dict) else {}
    key = str(feature.get("key") or "").strip()
    name = str(feature.get("name") or "Особенность")
    note = name
    if key == "lay_on_hands":
        from shared import class_features
        left = class_features.lay_on_hands_left(sheet)
        note = f"{name}: запас {left}"
        return UseAction(
            source="feature", kind="heal_pool", name=name,
            target_mode="ally",
            consumption={"type": "none"},
            feature_key=key,
            token_id=token_id or "",
            note=note,
        )
    return UseAction(
        source="feature", kind=str(feature.get("kind") or "utility"),
        name=name, formula=str(feature.get("formula") or ""),
        target_mode="self" if feature.get("target_mode") == "self" else "ally",
        consumption={"type": "none"},
        feature_key=key, token_id=token_id or "",
        note=f"{name}: {feature.get('text') or ''}".strip(),
    )


def from_item(item: dict, sheet: dict = None, token_id: str = "") -> UseAction:
    """Предмет инвентаря → UseAction.

    Зелье лечения, посох, свиток и оружие идут одним путём. Оружие
    открывает диалог атаки (kind=attack), зелье лечит (kind=heal),
    зелье скорости даёт эффект (kind=utility).
    """
    sheet = sheet or {}
    name = str(item.get("name") or "?")
    formulas = [str(x) for x in (item.get("formulas") or []) if str(x).strip()]
    heal_text = str(item.get("heal") or "")
    if not heal_text:
        heal_text = next((text for text in formulas if is_healing(text)), "")
    damage_text = str(item.get("activity_damage") or "")
    if not damage_text:
        damage_text = next((text for text in formulas
                            if not is_healing(text)), "")
    # Зелья, масла и яды описывают эффект данными: сопротивление, состояние,
    # временные хиты, скорость. Новое зелье из справочника не требует
    # отдельной ветки кода — достаточно добавить сюда правило разбора.
    effects = item_effects.effects_for_item(item)

    if heal_text:
        kind = "heal"
        formula = formula_from(heal_text)
        target_mode = "ally"
        effects = [{"type": "heal", "formula": formula}] if formula else []
    elif damage_text:
        # Любой урон предмета идёт через диалог атаки: там КД, крит и
        # сопротивление цели, а не «просто число в чат».
        kind = "attack"
        formula = formula_from(damage_text)
        target_mode = "selected"
        effects = [{"type": "damage", "formula": formula}] if formula else []
    elif item.get("save_dc"):
        kind = "save"
        formula = ""
        target_mode = "selected"
    elif effects:
        kind = "utility"
        formula = ""
        target_mode = "ally"
    else:
        kind = "utility"
        formula = ""
        target_mode = "self"
        effects = []

    bonus = item.get("attack_bonus")
    note = f"{name} — " + {
        "heal": "лечение", "save": "спасбросок",
        "attack": "атака", "damage": "урон", "utility": "эффект",
    }[kind]
    if kind == "utility" and effects:
        labels = item_effects.summary(effects)
        if labels:
            note = f"{name}: {labels}"
    if kind == "utility" and is_speed_potion(item):
        note = f"Зелье скорости: ×2 на 10 раундов ({name})"
    return UseAction(
        source="item",
        kind=kind,
        name=name,
        formula=formula,
        target_mode=target_mode,
        consumption={"type": (
            "item_charge" if content_audit.is_chargeable(item)
            else "item_quantity" if is_consumable(item)
            else "none")},
        save=str(item.get("save_dc") or ""),
        dc=int(item["save_dc"]) if item.get("save_dc") else None,
        attack_bonus=int(bonus) if bonus is not None else None,
        damage_type=combat_rules.damage_type_from_text(
            damage_text or " ".join(formulas)),
        effects=effects,
        source_id=str(item.get("source_id") or ""),
        item_id=str(item.get("id") or ""),
        token_id=token_id or "",
        range_ft=range_feet(item.get("range")),
        note=note,
    )


# ----------------------------------------------------------------------
# Проверка и расход
# ----------------------------------------------------------------------
def can_use(sheet: dict, action: UseAction) -> tuple:
    """Можно ли начать действие: (да/нет, причина)."""
    if sheet is None:
        return False, "Лист не загружен"
    if action.source == "spell":
        level = int((action.consumption or {}).get("level") or 0)
        if level:
            prepared = (action.consumption or {}).get("prepared")
            if prepared is False and needs_preparation(sheet):
                return False, "Заклинание не подготовлено"
            if not _slot_free(sheet, level):
                return False, f"Нет свободных ячеек {level} уровня"
    elif action.source == "item" and action.item_id:
        item = sheet_items.find_item(sheet, action.item_id)
        if item is None:
            return False, "Предмет не найден в инвентаре"
        kind = (action.consumption or {}).get("type")
        if kind == "item_quantity" and int(item.get("qty", 1)) < 1:
            return False, "Предмет закончился"
        if kind == "item_charge":
            try:
                charges = int(item.get("charges",
                                       item.get("charges_max", 0)) or 0)
            except (TypeError, ValueError):
                charges = 0
            if charges < 1:
                return False, "У предмета закончились заряды"
    return True, ""


def consume(sheet: dict, action: UseAction) -> tuple:
    """Списать расход. Вызывать только после выбора цели и проверки."""
    kind = (action.consumption or {}).get("type")
    if kind == "spell_slot":
        level = int((action.consumption or {}).get("level") or 0)
        if level and not sheet_items.spend_slot(sheet, level):
            return False, f"Нет свободных ячеек {level} уровня"
    elif kind == "item_quantity":
        if not sheet_items.consume(sheet, action.item_id):
            return False, "Предмет закончился"
    elif kind == "item_charge":
        if not sheet_items.consume_charge(sheet, action.item_id):
            return False, "У предмета закончились заряды"
    return True, ""
