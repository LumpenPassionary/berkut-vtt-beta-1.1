"""Скриншот вкладки «Рисование» в правой панели мастера.

Запуск:
    QT_QPA_PLATFORM=offscreen python tools/_shot_draw.py
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication

from client.main_window import MainWindow
from shared.models import MapData

app = QApplication(sys.argv)


class Net:
    connected = True

    def send_message(self, *a):
        pass

    def set_message_callback(self, *a):
        pass

    def disconnect(self):
        pass


win = MainWindow({'player_id': 'dm', 'nickname': 'DM', 'mode': 'host',
                  'role': 'dm', 'avatar_path': None}, Net(), server=None)
win.resize(1500, 900)
win.canvas.set_map(MapData(id='m', name='M', image_path='', width=800,
                           height=600, grid_cols=20, grid_rows=20,
                           img_width=800, img_height=600))
win.show()
app.processEvents()

# Открываем последнюю вкладку и включаем кисть, чтобы видно было подсветку.
win.right_tabs.toggle_section(win._draw_tab_index)
win.draw_actions['brush'].trigger()
win.canvas.drawing_color = (196, 90, 74)
win._refresh_color_button()
app.processEvents()

panel = win.right_tabs
shot = panel.grab()
out = os.path.join('design', 'shot_draw_tab.png')
shot.save(out)
print('saved', out, shot.width(), shot.height())
win.hide()
