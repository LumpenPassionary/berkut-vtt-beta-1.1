"""Скриншот листа персонажа с оружием и правкой хитов у мастера.

Запуск:
    QT_QPA_PLATFORM=offscreen python tools/_shot_sheet_weapons.py
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication

from client.main_window import MainWindow
from shared import lss_format as lss
from shared.models import MapData, Token


class Net:
    connected = True

    def send_message(self, *a):
        pass

    def set_message_callback(self, *a):
        pass

    def disconnect(self):
        pass


app = QApplication(sys.argv)
win = MainWindow({'player_id': 'dm', 'nickname': 'DM', 'mode': 'host',
                  'role': 'dm', 'avatar_path': None}, Net(), server=None)
win.resize(1500, 900)
win.canvas.set_map(MapData(id='m', name='M', image_path='', width=800,
                           height=600, grid_cols=20, grid_rows=20,
                           img_width=800, img_height=600))
token = Token(id='hero', name='Рингер', image_path='', x=200.0, y=200.0,
              kind='player', owner_id='p1', hp=76, hp_max=101, ac=18)
win.game_state.tokens[token.id] = token
win.canvas.tokens[token.id] = token

sheet = lss.new_sheet('РингерМУЛЬТИКЛАСС')
sheet['proficiency'] = 4
sheet['info']['charClass']['value'] = 'Следопыт, Плут, Воин'
sheet['info']['level']['value'] = 11
sheet['info']['race']['value'] = 'Человек'
for key, score in (('str', 7), ('dex', 20), ('con', 15), ('int', 11),
                   ('wis', 17), ('cha', 5)):
    sheet['stats'][key]['score'] = score
sheet['vitality']['hp-current']['value'] = 76
sheet['vitality']['hp-max']['value'] = 101
sheet['vitality']['ac']['value'] = 18
sheet['vitality']['speed']['value'] = 30
sheet['weaponsList'] = [
    {'id': 'weapon-1', 'name': {'value': 'Руже'},
     'dmg': {'value': '3d10+5'}, 'ability': 'dex', 'isProf': True},
    {'id': 'weapon-2', 'name': {'value': 'Кинжал'},
     'dmg': {'value': '1d4+[DEX]'}, 'ability': 'dex', 'isProf': True,
     'dmgType': {'value': 'колющий'}},
]
lss.set_attack_weapon(sheet, 'weapon-1')
lss.set_quick_weapon(sheet, 'weapon-2')
win.token_sheets[token.id] = sheet
win.sync_token_pages()
win.sheet_pages.set_sheet(token.id, sheet)
win.section_tabs.toggle_section(win._sheet_tab_index)
app.processEvents()

win.show()
app.processEvents()
tabs = win.section_tabs
if tabs.currentIndex() != win._sheet_tab_index:
    tabs.toggle_section(win._sheet_tab_index)
win.sheet_pages.select(token.id)
app.processEvents()
page = win.sheet_pages.sheet_widget(token.id)
app.processEvents()
out = os.path.join('design', 'shot_sheet_weapons.png')
shot = win.grab()
shot.save(out)
print('saved', out, shot.width(), shot.height())

# Окно атаки: оружие токена в списке вместо ручного ввода.
from client.combat_panel import AttackDialog

mob = Token(id='mob', name='Аболет', image_path='', x=500.0, y=300.0,
            kind='actor', hp=128, hp_max=135, ac=17)
win.game_state.tokens[mob.id] = mob
win.canvas.tokens[mob.id] = mob
lss.set_quick_weapon(sheet, 'weapon-2')
dialog = AttackDialog([token, mob], attacker_id=token.id, target_id=mob.id,
                      attacks=win.collect_token_attacks(token.id),
                      attacks_provider=win.collect_token_attacks, parent=win)
dialog.show()
app.processEvents()
out = os.path.join('design', 'shot_attack_weapons.png')
shot = dialog.grab()
shot.save(out)
print('saved', out, shot.width(), shot.height())
dialog.close()

# Окно атаки в духе LSS: разделы атаки, урона и заметок.
from client.attack_editor import AttackEditorDialog

edit = AttackEditorDialog(sheet, sheet['weaponsList'][0], win)
lss.add_weapon_bonus(sheet, sheet['weaponsList'][0], 'attack',
                     label='Стиль боя: стрельба', expr='2')
lss.set_weapon_notes(sheet['weaponsList'][0],
                     '1 В РАУНД: 3d10+5\n2 атаки (Следопыт)')
edit = AttackEditorDialog(sheet, sheet['weaponsList'][0], win)
edit.show()
app.processEvents()
out = os.path.join('design', 'shot_attack_editor.png')
shot = edit.grab()
shot.save(out)
print('saved', out, shot.width(), shot.height())
edit.close()
win.hide()
