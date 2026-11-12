"""Области действия заклинаний: геометрия и правила спасброска.

Описание в справочнике SRD человеческое, но структура в нём повторяется:
«сфера радиусом 20 футов», «15-футовый конус», «линия длиной 100 футов и
шириной 5 футов». Этот модуль достаёт из текста форму и размер, чтобы
клиент мог включить шаблон, а сервер — посчитать, кто попал в область.

Ключевые заклинания заданы явно по `source_name`: у них область важнее
любого совпадения слов, а ошибка в радиусе Fireball ломает весь бой.
Остальные разбираются по описанию; если форма не найдена — области нет и
заклинание остаётся обычным спасброском по одной цели.
"""
import re

FEET_PER_CELL = 5

#: source_name -> параметры области. half — половина урона при успехе.
_KNOWN = {
    "Fireball": {"shape": "circle", "size_ft": 20, "half": True},
    "Burning Hands": {"shape": "cone", "size_ft": 15, "half": True},
    "Lightning Bolt": {"shape": "line", "size_ft": 100, "width_ft": 5,
                       "half": True},
    "Shatter": {"shape": "circle", "size_ft": 10, "half": True},
    "Cone of Cold": {"shape": "cone", "size_ft": 60, "half": True},
    "Circle of Death": {"shape": "circle", "size_ft": 60, "half": True},
    "Otiluke's Freezing Sphere": {"shape": "circle", "size_ft": 60,
                                  "half": True},
}

#: Русские названия на случай записи без source_name.
_KNOWN_RU = {
    "огненный шар": "Fireball",
    "пылающие ладони": "Burning Hands",
    "молния": "Lightning Bolt",
    "раскол": "Shatter",
    "конус холода": "Cone of Cold",
    "круг смерти": "Circle of Death",
    "ледяная сфера": "Otiluke's Freezing Sphere",
}

_CIRCLE_RE = re.compile(
    r"(?:радиусом|радиусе)\s*(\d+)\s*фут", re.IGNORECASE)
_CIRCLE_EN_RE = re.compile(
    r"(?:radius|sphere)\D{0,30}?(\d+)\s*(?:-|\s)?foot", re.IGNORECASE)
_CONE_RE = re.compile(
    r"(?:(\d+)\s*(?:-|\s)?футов\w*\s+конус|конус\w*\s+(?:до\s+)?(\d+)\s*фут)",
    re.IGNORECASE)
_CONE_EN_RE = re.compile(
    r"(\d+)\s*(?:-|\s)?foot\s+cone", re.IGNORECASE)
_LINE_RE = re.compile(
    r"лини\w*\D{0,25}?(\d+)\s*фут\w*\D{0,20}?ширин\w*\s*(\d+)\s*фут",
    re.IGNORECASE)
_LINE_EN_RE = re.compile(
    r"line\D{0,25}?(\d+)\s*feet\D{0,20}?(\d+)\s*feet", re.IGNORECASE)


def _description(rec: dict) -> str:
    return str((rec or {}).get("description") or "")


def _known(rec: dict):
    key = str(rec.get("source_name") or "").strip()
    if key in _KNOWN:
        return _KNOWN[key]
    name = str(rec.get("name") or "").strip().casefold().replace("ё", "е")
    canonical = _KNOWN_RU.get(name)
    if canonical:
        return _KNOWN[canonical]
    for needle, canonical in _KNOWN_RU.items():
        if needle in name:
            return _KNOWN[canonical]
    return None


def area_for_spell(rec: dict) -> dict:
    """Вернуть {'shape', 'size_ft', 'width_ft', 'half_on_success'} или {}.

    У круга `width_ft` нет, у линии он равен 5 по умолчанию. Наличие
    `half_on_success` не значит «урон всегда режется»: это правило читается
    только при успешном спасброске.
    """
    rec = rec if isinstance(rec, dict) else {}
    known = _known(rec)
    text = _description(rec)
    if known:
        spec = dict(known)
    else:
        spec = {}
        match = _CIRCLE_RE.search(text) or _CIRCLE_EN_RE.search(text)
        if match:
            spec = {"shape": "circle", "size_ft": int(match.group(1))}
        else:
            match = _LINE_RE.search(text) or _LINE_EN_RE.search(text)
            if match:
                spec = {"shape": "line", "size_ft": int(match.group(1)),
                        "width_ft": int(match.group(2))}
            else:
                match = _CONE_RE.search(text) or _CONE_EN_RE.search(text)
                if match:
                    size = match.group(1) or match.group(2)
                    spec = {"shape": "cone", "size_ft": int(size)}
    if not spec:
        return {}
    size = int(spec.get("size_ft") or 0)
    if size < FEET_PER_CELL or size > 120:
        return {}
    out = {"shape": spec["shape"], "size_ft": size}
    if spec.get("width_ft"):
        out["width_ft"] = max(FEET_PER_CELL, int(spec["width_ft"]))
    low = text.casefold()
    out["half_on_success"] = bool(
        spec.get("half") or "половину" in low or "half" in low)
    return out


def is_area_spell(rec: dict) -> bool:
    return bool(area_for_spell(rec))
