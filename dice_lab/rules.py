"""Правила результата для обычного броска, преимущества и помехи.

Модуль не знает ни о PyQt, ни о Bullet: на вход приходят уже прочитанные
значения кубиков. Поэтому правило можно проверить отдельно от физики и UI.

Преимущество и помеха в D&D 5e — это ровно два d20, из которых берётся
больший или меньший результат. Модификатор прибавляется после выбора.
Критический эффект включается только для одного d20 или для пары
преимущества/помехи — в смешанных наборах его нет.
"""
MODE_NORMAL = 'normal'
MODE_ADVANTAGE = 'advantage'
MODE_DISADVANTAGE = 'disadvantage'

CRIT_FAIL = 'fail'
CRIT_SUCCESS = 'success'

MODES = (MODE_NORMAL, MODE_ADVANTAGE, MODE_DISADVANTAGE)

MODE_TITLES = {
    MODE_NORMAL: 'Обычный бросок',
    MODE_ADVANTAGE: 'Преимущество',
    MODE_DISADVANTAGE: 'Помеха',
}

MODE_HINTS = {
    MODE_NORMAL: '',
    MODE_ADVANTAGE: '2d20, берётся больший результат',
    MODE_DISADVANTAGE: '2d20, берётся меньший результат',
}


def resolve_total(values, modifier=0, mode=MODE_NORMAL):
    """Вернуть (итог, индекс выбранного кубика).

    Для обычного броска суммируются все значения, индекс равен ``None``.
    Для преимущества/помехи значений должно быть ровно два; возвращается
    выбранный d20 и его индекс. При равных значениях выбирается первый.
    """
    values = list(values)
    modifier = int(modifier)
    if mode == MODE_NORMAL:
        return sum(values) + modifier, None
    if mode not in (MODE_ADVANTAGE, MODE_DISADVANTAGE):
        raise ValueError(f'Неизвестный режим броска: {mode}')
    if len(values) != 2:
        raise ValueError('Для преимущества и помехи нужно ровно два d20')
    if mode == MODE_ADVANTAGE:
        chosen = max(range(2), key=values.__getitem__)
    else:
        chosen = min(range(2), key=values.__getitem__)
    return values[chosen] + modifier, chosen


def critical_kind(values, sides, mode=MODE_NORMAL, chosen=None):
    """Вернуть ``CRIT_SUCCESS``/``CRIT_FAIL`` или ``None``.

    Эффект включается только для одного d20 в обычном режиме и для
    преимущества/помехи. В смешанных наборах (d20 + другие кубики) и при
    обычных 2d20 критического эффекта нет.
    """
    values = list(values)
    sides = list(sides)
    if mode in (MODE_ADVANTAGE, MODE_DISADVANTAGE):
        if (len(values) != 2 or len(sides) != 2
                or any(sides_value != 20 for sides_value in sides)
                or chosen not in (0, 1)):
            return None
        value = values[chosen]
    elif mode == MODE_NORMAL:
        if len(values) != 1 or len(sides) != 1 or sides[0] != 20:
            return None
        value = values[0]
    else:
        raise ValueError(f'Неизвестный режим броска: {mode}')
    if value == 20:
        return CRIT_SUCCESS
    if value == 1:
        return CRIT_FAIL
    return None
