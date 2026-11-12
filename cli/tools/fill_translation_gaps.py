"""Дополнить словари перевода: типы существ, папки и пропущенные монстры.

Остатки после основного импорта — это в основном служебные ярлыки паков
(«Beast», «Undead», «9th Level»), которые в интерфейсе всё равно видны как
заголовки разделов. Плюс несколько настоящих монстров, которых не было
на dnd.su под точным именем.

Запуск: python3 cli/tools/fill_translation_gaps.py
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

# Типы существ и заголовки разделов. Берём терминологию dnd.su.
TYPES_RU = {
    'Aberration': 'Аберрация',
    'Beast': 'Зверь',
    'Celestial': 'Небожитель',
    'Construct': 'Конструкт',
    'Dragon': 'Дракон',
    'Elemental': 'Элементаль',
    'Fey': 'Фея',
    'Fiend': 'Исчадие',
    'Giant': 'Великан',
    'Humanoid': 'Гуманоид',
    'Monstrosity': 'Чудовище',
    'Ooze': 'Слизь',
    'Plant': 'Растение',
    'Undead': 'Нежить',
    'Summons': 'Призывы',
    'Supplemental Items': 'Дополнительные предметы',
}

# Уровни заклинаний: в паках это папки «1st Level» … «9th Level».
LEVELS_RU = {
    'Cantrip': 'Заговор',
    '1st Level': '1-й уровень',
    '2nd Level': '2-й уровень',
    '3rd Level': '3-й уровень',
    '4th Level': '4-й уровень',
    '5th Level': '5-й уровень',
    '6th Level': '6-й уровень',
    '7th Level': '7-й уровень',
    '8th Level': '8-й уровень',
    '9th Level': '9-й уровень',
}

# Монстры, которых не удалось сопоставить автоматически.
MONSTERS_RU = {
    'Tiny Animated Object': 'Крошечный одушевлённый предмет',
    'Small Animated Object': 'Маленький одушевлённый предмет',
    'Medium Animated Object': 'Средний одушевлённый предмет',
    'Large Animated Object': 'Большой одушевлённый предмет',
    'Huge Animated Object': 'Огромный одушевлённый предмет',
    'Deep Gnome': 'Глубинный гном',
    'Half-Red Dragon Veteran': 'Ветеран-полудракон (красный)',
    'Swarm of Beetles': 'Рой жуков',
    'Swarm of Centipedes': 'Рой многоножек',
    'Swarm of Wasps': 'Рой ос',
}


def norm_key(text):
    """Ключ как в dnd5e_loc._norm_key: без пунктуации, ё->е, один пробел."""
    import re
    text = re.sub(r"\([^)]*\)", " ", str(text or "").casefold()).replace("ё", "е")
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-zа-я0-9 ]+", " ", text)).strip()


def main():
    target = os.path.join(BASE, 'shared', 'dnd5e_loc_data.json')
    with open(target, encoding='utf-8') as f:
        data = json.load(f)

    titles = data.setdefault('titles', {})
    added = 0
    for source in (TYPES_RU, LEVELS_RU):
        for en, ru in source.items():
            key = norm_key(en)
            if key not in titles:
                titles[key] = ru.lower() if ru[:1].isupper() else ru
                added += 1

    monsters = data.setdefault('monsters', {})
    for en, ru in MONSTERS_RU.items():
        key = norm_key(en)
        if key not in monsters:
            monsters[key] = {'name': ru, 'features': {}, 'url': ''}
            added += 1

    with open(target, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

    print(f'добавлено записей: {added}')
    print(f'titles: {len(titles)} | monsters: {len(monsters)}')


if __name__ == '__main__':
    main()
