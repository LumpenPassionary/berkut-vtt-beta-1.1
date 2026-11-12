"""Горячие клавиши, не зависящие от раскладки.

В русской раскладке физическая клавиша Q присылает в Qt код буквы «й», а не
«Q». Поэтому обычный QShortcut("Q") и проверка event.key() == Qt.Key_Q
срабатывают только в английской раскладке. Здесь собраны две вещи:

* ``event_key`` — переводит событие клавиатуры в латинский Qt.Key по
  физической клавише (текст, код кириллицы или nativeVirtualKey Windows);
* ``install_shortcut`` — ставит QShortcut и на латинскую, и на кириллическую
  комбинацию, чтобы Ctrl+Z работал и как Ctrl+Я.
"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QShortcut


# Физическая латинская клавиша -> символ русской раскладки (ЙЦУКЕН).
RU_LAYOUT = {
    'q': 'й', 'w': 'ц', 'e': 'у', 'r': 'к', 't': 'е', 'y': 'н',
    'u': 'г', 'i': 'ш', 'o': 'щ', 'p': 'з',
    'a': 'ф', 's': 'ы', 'd': 'в', 'f': 'а', 'g': 'п', 'h': 'р',
    'j': 'о', 'k': 'л', 'l': 'д',
    'z': 'я', 'x': 'ч', 'c': 'с', 'v': 'м', 'b': 'и', 'n': 'т', 'm': 'ь',
    '[': 'х', ']': 'ъ', ';': 'ж', "'": 'э', ',': 'б', '.': 'ю',
}

# Символ русской раскладки -> Qt.Key той же физической клавиши.
RU_KEY = {ru: (Qt.Key_BracketLeft if lat == '['
               else Qt.Key_BracketRight if lat == ']'
               else ord(lat.upper()))
          for lat, ru in RU_LAYOUT.items()}


def layout_variants(sequence: str) -> list:
    """Все написания комбинации: латинское и русское.

    ``Ctrl+Z`` -> ``["Ctrl+Z", "Ctrl+Я", "Ctrl+я"]``. Оба регистра нужны:
    Qt присылает кириллицу без нормализации к верхнему регистру. Служебные
    клавиши (F11, Esc, Delete) не меняются и возвращаются одним вариантом.
    """
    parts = sequence.split('+')
    key = parts[-1]
    ru = RU_LAYOUT.get(key.lower())
    if ru is None or len(key) != 1:
        return [sequence]
    prefix = parts[:-1]
    variants = [sequence,
                '+'.join(prefix + [ru.upper()]),
                '+'.join(prefix + [ru.lower()])]
    # Без повторов: «F11» и «Esc» дают только исходный вариант.
    out = []
    for text in variants:
        if text not in out:
            out.append(text)
    return out


#: Названия модификаторов в тексте комбинации -> биты QKeySequence.
_MODIFIER_BITS = {
    'ctrl': int(Qt.ControlModifier),
    'alt': int(Qt.AltModifier),
    'shift': int(Qt.ShiftModifier),
    'meta': int(Qt.MetaModifier),
}


def _ru_lowercase_sequence(text: str):
    """QKeySequence для строчной кириллицы без Shift.

    QKeySequence("Й") всегда хранит КОД ПРОПИСНОЙ буквы (U+0419), а Windows
    присылает без Shift код строчной (U+0439). Совпадения нет, и хоткей
    молча не срабатывает. Поэтому для русской раскладки дополнительно
    собираем последовательность из кода строчной буквы и битов модификаторов.
    """
    parts = text.split('+')
    key = parts[-1]
    if len(key) != 1 or not ('А' <= key <= 'я' or key in ('Ё', 'ё')):
        return None
    mods = 0
    has_shift = False
    for part in parts[:-1]:
        bit = _MODIFIER_BITS.get(part.lower())
        if bit is None:
            return None
        mods |= bit
        has_shift = has_shift or part.lower() == 'shift'
    # При зажатом Shift Windows присылает прописную букву — её уже покрывает
    # обычный вариант из layout_variants.
    if has_shift:
        return None
    return QKeySequence(ord(key.lower()) | mods)


def install_shortcut(parent, sequence: str, slot, auto_repeat: bool = True):
    """Повесить QShortcut, срабатывающий в обеих раскладках.

    Возвращает основной (латинский) QShortcut — остальные живут как дети
    ``parent`` и хранятся в его списке ``_layout_shortcuts``.
    """
    shortcuts = []
    seen = set()
    for text in layout_variants(sequence):
        candidates = [QKeySequence(text)]
        lower = _ru_lowercase_sequence(text)
        if lower is not None:
            candidates.append(lower)
        for keys in candidates:
            # Одинаковые коды дали бы ambiguous shortcut и не сработал бы
            # ни один. Сравниваем именно коды, а не строку: у строчной и
            # прописной кириллицы toString может совпасть.
            ident = tuple(int(keys[i]) for i in range(keys.count()))
            if ident in seen:
                continue
            seen.add(ident)
            shortcut = QShortcut(keys, parent)
            shortcut.setAutoRepeat(auto_repeat)
            shortcut.activated.connect(slot)
            shortcuts.append(shortcut)
    if not hasattr(parent, '_layout_shortcuts'):
        parent._layout_shortcuts = []
    parent._layout_shortcuts.extend(shortcuts)
    return shortcuts[0]


def event_key(event) -> int:
    """Qt.Key физической клавиши независимо от текущей раскладки."""
    key = event.key()
    if Qt.Key_A <= key <= Qt.Key_Z:
        return key

    # В русской раскладке Qt присылает код кириллицы: «я» вместо Z.
    if 0x400 <= key <= 0x4FF:
        mapped = RU_KEY.get(chr(key).lower())
        if mapped is not None:
            return mapped

    # Запасной путь: текст события (полезен, если key() пришёл служебным).
    text = (event.text() or '').lower()
    if len(text) == 1:
        mapped = RU_KEY.get(text)
        if mapped is not None:
            return mapped

    # Windows сохраняет физическую клавишу в nativeVirtualKey даже при
    # русской раскладке: VK_Q = 0x51 = Qt.Key_Q.
    virtual = event.nativeVirtualKey()
    if 0x41 <= virtual <= 0x5A:
        return virtual

    return key
