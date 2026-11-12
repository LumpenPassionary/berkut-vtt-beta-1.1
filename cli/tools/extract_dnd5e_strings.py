"""Выгрузить имена и описания из паков dnd5e в компактный JSON.

Зачем: паки Foundry — 60 МБ YAML. Для перевода нужны только имя и текст
описания, это в разы меньше. Результат кладём в проект, чтобы он не пропадал
вместе с /tmp.

    python3 cli/tools/extract_dnd5e_strings.py --source /tmp/dnd5e_src/x/packs/_source
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from shared import dnd5e_lib  # noqa: E402

# Разделы паков, которые нам нужны. Для правил 2024 (suffix 24) берём
# отдельно: это другой набор правил, его нельзя смешивать с редакцией 2014.
SECTIONS = (
    ('spells', 'spell'),
    ('items', 'item'),
    ('monsters', 'monster'),
)


def load_yaml(path):
    try:
        import yaml
    except ImportError:
        raise SystemExit('нужен pyyaml: pip install pyyaml')
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def clean_html(html_text, ctx=None):
    """Текст без тегов, с расшифровкой @-ссылок dnd5e."""
    try:
        return dnd5e_lib.strip_html(html_text or '', ctx or {})
    except Exception:
        return re.sub(r'<[^>]+>', ' ', html_text or '').strip()


def walk(section_dir, kind):
    """Собрать записи одного раздела."""
    out = {}
    if not os.path.isdir(section_dir):
        return out
    for root, _dirs, files in os.walk(section_dir):
        for name in sorted(files):
            if not name.endswith('.yml'):
                continue
            path = os.path.join(root, name)
            try:
                doc = load_yaml(path)
            except Exception as exc:
                print(f'  пропуск {name}: {exc}')
                continue
            if not isinstance(doc, dict):
                continue
            en_name = (doc.get('name') or '').strip()
            if not en_name:
                continue
            system = doc.get('system') or {}
            desc = ((system.get('description') or {}).get('value')
                    or system.get('description') or '')
            if isinstance(desc, dict):
                desc = desc.get('value', '')
            out[en_name] = {
                'kind': kind,
                'type': doc.get('type') or '',
                'text': clean_html(desc),
                'level': system.get('level'),
                'school': system.get('school'),
                'rarity': system.get('rarity') or '',
                'folder': os.path.basename(root),
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True, help='путь к packs/_source')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    base = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    out_path = args.out or os.path.join(base, 'shared', 'dnd5e_strings.json')

    result = {'source': 'foundryvtt/dnd5e release-6.0.6', 'sections': {}}
    total = 0
    for section, kind in SECTIONS:
        section_dir = os.path.join(args.source, section)
        records = walk(section_dir, kind)
        result['sections'][section] = records
        total += len(records)
        print(f'{section:12} {len(records):5} записей')

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, separators=(',', ':'))

    size = os.path.getsize(out_path)
    print(f'\nвсего {total} записей')
    print(f'{out_path}: {size:,} байт ({size / 1048576:.1f} МБ)')


if __name__ == '__main__':
    main()
