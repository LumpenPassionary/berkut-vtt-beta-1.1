"""Настройки графики стола: качество теней и мелкие переключатели.

Модуль чистый — ни Qt, ни сети: одни и те же значения читает стартовое окно,
окно «Графика» в игре и сам холст, а тесты проверяют числа.

Почему это нужно: главная цена кадра — не картинка карты, а тени. Свет с
множеством стен строит многоугольник видимости лучами, и на каждый источник
требуется до сотни лучей. Когда источников десятки, а стен сотни, всё это
считает процессор, и стол начинает лагать. Поэтому «качество теней» — явная
настройка, а не зашитое число: на слабой машине мастер снижает его одним
пунктом меню, а не отказывается от освещения.
"""
import math

#: Сколько лучей выпускает источник и токен. Больше — точнее края теней,
#: дороже кадр. 220 лучей дают шаг 1.6° — на глаз неотличимо от «настоящего»
#: света; 90 — грубее, но вчетверо дешевле.
RAY_STEPS = (90, 140, 220)
DEFAULT_RAYS = 220

#: Названия шагов для интерфейса: мастер выбирает не число, а качество.
RAY_LABELS = {
    90: "Низкое — быстрее всего",
    140: "Среднее",
    220: "Высокое — плавные края теней",
}

#: Лучи для многоугольника видимости по умолчанию (совместимость со старыми
#: вызовами `walls.visibility_polygon`).
MIN_RAYS = min(RAY_STEPS)
MAX_RAYS = max(RAY_STEPS)

#: Движок отрисовки: «auto» — видеокарта, если она есть.
RENDERERS = ("auto", "gpu", "cpu")

#: Значения по умолчанию. Ключи — те же, что в settings.json.
DEFAULTS = {
    # Лучи теней: сколько их у источника и у зрения токена.
    "rays": DEFAULT_RAYS,
    # Сглаживание кромок света и теней.
    "antialiasing": True,
    # Тени от объектов карты (крыши, деревья): самая дорогая часть света.
    "shadows": True,
    # Отсвет «сверх силы» на дневной карте.
    "day_glow": True,
    # Движок: применится после перезапуска (класс виджета выбирается на старте).
    "renderer": "auto",
}


def normalize_rays(value, default: int = DEFAULT_RAYS) -> int:
    """Лучи теней: только из разрешённых шагов, иначе шаг по умолчанию."""
    try:
        rays = int(value)
    except (TypeError, ValueError):
        return int(default)
    if rays in RAY_STEPS:
        return rays
    # Ближайший разрешённый шаг: в файле мог остаться «240» от старой версии.
    return min(RAY_STEPS, key=lambda step: abs(step - rays))


def normalize(raw) -> dict:
    """Настройки графики из файла или сети: без мусора, все ключи на месте."""
    if not isinstance(raw, dict):
        return dict(DEFAULTS)
    renderer = str(raw.get("renderer") or DEFAULTS["renderer"]).strip().lower()
    if renderer not in RENDERERS:
        renderer = DEFAULTS["renderer"]
    return {
        "rays": normalize_rays(raw.get("rays", DEFAULTS["rays"])),
        "antialiasing": bool(raw.get("antialiasing", True)),
        "shadows": bool(raw.get("shadows", True)),
        "day_glow": bool(raw.get("day_glow", True)),
        "renderer": renderer,
    }


def ray_label(rays) -> str:
    """Подпись шага качества для интерфейса."""
    rays = normalize_rays(rays)
    return RAY_LABELS.get(rays, "%d лучей" % rays)


def frame_budget_ms(rays) -> float:
    """Ориентир по кадру: сколько примерно стоит маска с таким качеством.

    Числа сняты замером (`tools/bench_graphics.py`) на сцене из 24 источников
    и 120 стен; нужны только для подсказки в интерфейсе, поэтому это порядок
    величины, а не обещание.
    """
    rays = normalize_rays(rays)
    # Стоимость видимости растёт линейно по лучам; остальное — заливка маски.
    return round(6.0 + 34.0 * (rays / float(MAX_RAYS)), 1)


def format_bytes(size) -> str:
    """Байты в человеческий вид: «1.4 ГБ», «180 МБ», «512 КБ»."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(value) or math.isinf(value) or value < 0:
        return "—"
    for unit, step in (("ГБ", 1024.0 ** 3), ("МБ", 1024.0 ** 2), ("КБ", 1024.0)):
        if value >= step:
            return "%.1f %s" % (value / step, unit)
    return "%d Б" % int(value)
