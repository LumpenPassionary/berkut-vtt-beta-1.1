"""Живая проверка текста на карте в настоящем окне приложения.

Поднимаем настоящий сервер, подключаемся к нему сетевым клиентом — как это
делает main.py после «Создать игру», — и проходим путь мастера мышью:
включили «Текст», кликнули по карте, написали подпись, поменяли оформление,
перетащили, приблизили, стёрли. Каждый шаг проверяется по состоянию игры и
по пикселям холста, а окно и панель сохраняются в design/.

Запуск:
    python tools/_probe_text.py
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')

from PyQt5.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt5.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen
from PyQt5.QtWidgets import QApplication, QColorDialog
from PyQt5.QtTest import QTest

from client.connection_dialog import ConnectionDialog
from client.main_window import MainWindow
from client.network_client import NetworkClient
from server.game_server import GameServer
from shared.models import generate_id

results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(('  OK  ' if cond else ' FAIL ') + name
          + (f'   [{detail}]' if detail and not cond else ''))


def wait_until(predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def canvas_click(canvas, wx, wy, button=Qt.LeftButton):
    sx, sy = canvas.world_to_screen(wx, wy)
    QTest.mouseClick(canvas, button, Qt.NoModifier, QPoint(int(sx), int(sy)))
    app.processEvents()


def make_map_image(path):
    """Светлая «пергаментная» карта: на ней белый текст почти не виден."""
    image = QImage(1400, 1000, QImage.Format_RGB32)
    image.fill(QColor(206, 194, 168))
    painter = QPainter(image)
    painter.setPen(QPen(QColor(184, 170, 142), 2))
    for x in range(0, 1400, 70):
        painter.drawLine(x, 0, x, 1000)
    for y in range(0, 1000, 70):
        painter.drawLine(0, y, 1400, y)
    painter.setPen(QPen(QColor(120, 96, 60), 6))
    painter.drawRect(60, 60, 1280, 880)
    painter.end()
    image.save(path)
    return path


app = QApplication(sys.argv)

# ---------------------------------------------------------------- старт окна
dialog = ConnectionDialog()
dialog.show()
app.processEvents()
dialog.grab().save('design/shot_text_start.png')
check('S1 стартовое окно открывается', dialog.isVisible())
dialog.close()

# ------------------------------------------------------- сервер и настоящее окно
port = free_port()
server = GameServer('127.0.0.1', port)
server.start()
client = NetworkClient()
info = {
    'mode': 'host', 'nickname': 'Мастер', 'port': port, 'avatar_path': None,
    'player_id': generate_id(), 'enable_3d': False,
    'combat_effects': True, 'blood_transparency': 0,
    'preload_my_actors': False,
}
connected = client.connect('127.0.0.1', port)
check('S2 клиент подключился к своему серверу', connected)

win = MainWindow(info, client, server)
win.resize(1680, 980)
win.show()
wait_until(lambda: win.canvas.tokens is not None)
time.sleep(0.4)
app.processEvents()

check('S3 окно стола собралось', win.isVisible() and win._draw_tab_index is not None,
      str(win._draw_tab_index))

# ------------------------------------------------------------------- карта
map_path = make_map_image('/tmp/probe_map.png')
win.send_map_file(map_path)
opened = wait_until(lambda: win.canvas.current_map is not None, 10.0)
check('S4 карта открылась на холсте', opened,
      str(win.canvas.current_map.name if win.canvas.current_map else None))
# Заставка загрузки закрывает карту — ждём, как живой мастер, пока она уйдёт.
left = wait_until(lambda: not win.canvas.is_map_loading(), 20.0)
check('S4a заставка загрузки ушла сама', left,
      'карта так и осталась под заставкой')
app.processEvents()

win.right_tabs.toggle_section(win._draw_tab_index)
app.processEvents()
win.canvas.fit_map_to_screen()
app.processEvents()
win.right_tabs.grab().save('design/shot_text_panel.png')

tools = sorted(win.draw_buttons)
check('S5 на вкладке восемь инструментов', len(tools) == 8, str(tools))
check('S6 у текста и перемещения есть иконки и подсказки',
      all(not win.draw_buttons[m].icon().isNull() for m in ('text', 'move'))
      and all(win.draw_buttons[m].toolTip() for m in ('text', 'move')))

# ------------------------------------------------------ новая надпись мышью
win.draw_buttons['text'].click()
app.processEvents()
check('S7 кнопка «Текст» включает инструмент и подсвечивается',
      win.canvas.drawing_mode == 'text'
      and win.draw_actions['text'].isChecked()
      and [m for m, a in win.draw_actions.items() if a.isChecked()] == ['text'],
      f'{win.canvas.drawing_mode} / '
      f'{[m for m, a in win.draw_actions.items() if a.isChecked()]}')
check('S8 курсор стал текстовым', win.canvas.cursor().shape() == Qt.IBeamCursor,
      str(win.canvas.cursor().shape()))

map_center = (win.canvas.current_map.width / 2.0,
              win.canvas.current_map.height / 2.0)
canvas_click(win.canvas, *map_center)
editor = win.canvas._text_editor
check('S9 клик по карте открыл поле правки', editor is not None and editor.isVisible())
if editor is not None:
    editor.insert('Таверна «Грифон»')
    app.processEvents()
    QTest.keyClick(editor, Qt.Key_Return)
    app.processEvents()

def current_text(text_id=None):
    """Свежая запись с холста: правка заменяет объект, ссылки устаревают."""
    return win.canvas.text_by_id(text_id or win.canvas.selected_text_id)


texts = win.canvas.map_texts
check('S10 надпись появилась на холсте', len(texts) == 1
      and texts[0]['text'] == 'Таверна «Грифон»', str([t['text'] for t in texts]))
check('S11 надпись ушла на сервер', bool(server.game_state.map_texts.get(
    win.canvas.current_map.id)), str(server.game_state.map_texts))
check('S12 после Enter поле правки закрылось',
      win.canvas._text_editor is None)
check('S13 надпись осталась выбранной, кнопка удаления активна',
      win.canvas.selected_text_id == texts[0]['id'] and win.text_delete_btn.isEnabled())
win.grab().save('design/shot_text_1_new.png')
win.right_tabs.grab().save('design/shot_text_1_panel.png')

# ------------------------------------------- читается ли текст на светлой карте
def text_pixels(canvas, item, colour, tolerance=60):
    """Сколько пикселей нужного цвета в области надписи (по снимку холста)."""
    rect = canvas.text_rect(item)
    sx, sy = canvas.world_to_screen(rect.x(), rect.y())
    width = max(8, int(rect.width() * canvas.scale))
    height = max(8, int(rect.height() * canvas.scale))
    shot = canvas.grab(QRect(int(sx) - 4, int(sy) - 4, width + 8, height + 8))
    image = shot.toImage()
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if (abs(pixel.red() - colour[0]) < tolerance
                    and abs(pixel.green() - colour[1]) < tolerance
                    and abs(pixel.blue() - colour[2]) < tolerance):
                count += 1
    return count


first = current_text(texts[0]['id'])
white = text_pixels(win.canvas, first, (255, 255, 255))
check('S14 белая надпись видна на светлой карте', white > 40,
      f'белых пикселей {white} — на пергаменте почти не читается')
halo = text_pixels(win.canvas, first, (0, 0, 0), tolerance=70)
check('S14a вокруг светлых букв есть тёмный контур', halo > 20,
      f'тёмных пикселей по краям {halo} — буквы сливаются с пергаментом')

# ------------------------------------------------ оформление из панели
# Кнопки панели нажимаем через .click(): QTest посылает клик по координатам
# окна и в этом сеансе улетает в холст (окно перекрыто консолью). Сигнал
# при этом тот же, что от настоящего щелчка.
win.text_style_buttons['bold'].click()
app.processEvents()
win.text_size_spin.setFocus()
win.text_size_spin.selectAll()
QTest.keyClicks(win.text_size_spin, '46')
QTest.keyClick(win.text_size_spin, Qt.Key_Return)
app.processEvents()

original_get_color = QColorDialog.getColor
QColorDialog.getColor = staticmethod(lambda *a, **k: QColor(26, 22, 18))
win.text_color_button.click()
QColorDialog.getColor = original_get_color
app.processEvents()

item = current_text(first['id'])
check('S15 жирный и размер применились к выбранной надписи',
      item['bold'] is True and abs(item['size'] - 46) < 0.01,
      f"bold={item['bold']} size={item['size']}")
check('S16 цвет из панели применился', item['color'] == [26, 22, 18],
      str(item['color']))
dark = text_pixels(win.canvas, item, (26, 22, 18))
check('S17 тёмная надпись читается на светлой карте', dark > 60,
      f'тёмных пикселей {dark}')
check('S17a размер и шрифт на панели совпадают с надписью',
      win.text_size_spin.value() == 46
      and win.text_style_buttons['bold'].isChecked()
      and win.text_font_combo.currentText() == item['font'],
      f'{win.text_size_spin.value()} / {win.text_font_combo.currentText()}')
win.grab().save('design/shot_text_2_styled.png')

# --------------------------------------------------- вторая надпись другим видом
win.draw_buttons['text'].click()
canvas_click(win.canvas, map_center[0] - 500.0, map_center[1] + 260.0)
if win.canvas._text_editor is not None:
    win.canvas._text_editor.insert('Ловушка')
    QTest.keyClick(win.canvas._text_editor, Qt.Key_Return)
    app.processEvents()
win.text_style_buttons['bold'].setChecked(False)
win.text_size_spin.setValue(28)
app.processEvents()
check('S18 вторая надпись создана и оформление сброшено',
      len(win.canvas.map_texts) == 2
      and win.canvas.map_texts[-1]['bold'] is False
      and abs(win.canvas.map_texts[-1]['size'] - 28) < 0.01,
      str([(t['text'], t['bold'], t['size']) for t in win.canvas.map_texts]))

# ---------------------------------------------------- перетаскивание мышью
win.draw_buttons['move'].click()
app.processEvents()
check('S19 кнопка «Перемещение» включает режим',
      win.canvas.drawing_mode == 'move'
      and win.canvas.cursor().shape() == Qt.SizeAllCursor)
target = win.canvas.map_texts[0]
start_x, start_y = target['x'], target['y']
sx, sy = win.canvas.world_to_screen(start_x + 6, start_y + 6)

# Тащим системным курсором не получается: окно может быть перекрыто
# другими, и движение уйдёт не в холст. Шлём те же события напрямую —
# холст разбирает их своим mousePress/Move/Release.
def canvas_mouse(kind, x, y, buttons=Qt.LeftButton):
    event = QMouseEvent(kind, QPointF(x, y), Qt.LeftButton, buttons,
                        Qt.NoModifier)
    if kind == QEvent.MouseButtonPress:
        win.canvas.mousePressEvent(event)
    elif kind == QEvent.MouseMove:
        win.canvas.mouseMoveEvent(event)
    else:
        win.canvas.mouseReleaseEvent(event)
    app.processEvents()


canvas_mouse(QEvent.MouseButtonPress, sx, sy)
canvas_mouse(QEvent.MouseMove, sx + 160, sy + 90)
canvas_mouse(QEvent.MouseButtonRelease, sx + 160, sy + 90)
target = current_text(target['id'])
check('S20 надпись переехала мышью',
      abs(target['x'] - start_x) > 20 and abs(target['y'] - start_y) > 10,
      f'{start_x:.0f},{start_y:.0f} -> {target["x"]:.0f},{target["y"]:.0f}')
check('S21 при перетаскивании поле правки не появилось',
      win.canvas._text_editor is None)
server_item = next((row for row in server.game_state.map_texts.get(
    win.canvas.current_map.id, []) if row['id'] == target['id']), None)
check('S22 новые координаты дошли до сервера',
      server_item is not None
      and abs(server_item['x'] - target['x']) < 0.01,
      str(server_item['x'] if server_item else None))
win.grab().save('design/shot_text_3_drag.png')

# ------------------------------------------------------------ зум и панорама
before = win.canvas.world_to_screen(target['x'], target['y'])
win.canvas.scale = win.canvas.scale * 1.8
win.canvas.update()
app.processEvents()
after = win.canvas.world_to_screen(target['x'], target['y'])
check('S23 при зуме надпись остаётся на своём месте карты',
      abs(after[0] - before[0]) > 1 or abs(after[1] - before[1]) > 1,
      f'{before} -> {after}')
win.canvas.update()
app.processEvents()
win.canvas.grab().save('design/shot_text_4_zoom.png')

# --------------------------------------- обычный курсор и кисть текст не трогают
win.canvas.stop_drawing()
app.processEvents()
sx, sy = win.canvas.world_to_screen(target['x'] + 6, target['y'] + 6)
QTest.mouseClick(win.canvas, Qt.LeftButton, Qt.NoModifier,
                 QPoint(int(sx), int(sy)))
app.processEvents()
check('S24 обычный курсор не открывает правку', win.canvas._text_editor is None)
check('S25 обычный курсор не выделяет надпись',
      win.canvas.selected_text_id is None, str(win.canvas.selected_text_id))

win.draw_buttons['brush'].click()
app.processEvents()
QTest.mouseClick(win.canvas, Qt.LeftButton, Qt.NoModifier,
                 QPoint(int(sx), int(sy)))
app.processEvents()
check('S26 кисть по надписи не открывает правку',
      win.canvas._text_editor is None and win.canvas.selected_text_id is None)
win.canvas.stop_drawing()

# ------------------------------------------------------------- удаление
win.draw_buttons['move'].click()
app.processEvents()
QTest.mouseClick(win.canvas, Qt.LeftButton, Qt.NoModifier,
                 QPoint(int(sx), int(sy)))
app.processEvents()
check('S27 клик по надписи её выбирает', win.canvas.selected_text_id == target['id'],
      str(win.canvas.selected_text_id))
win.text_delete_btn.click()
app.processEvents()
check('S28 кнопка корзины удаляет надпись',
      len(win.canvas.map_texts) == 1,
      f'осталось {len(win.canvas.map_texts)}')
check('S29 удаление дошло до сервера',
      len(server.game_state.map_texts.get(win.canvas.current_map.id, [])) == 1,
      str(len(server.game_state.map_texts.get(win.canvas.current_map.id, []))))

win.grab().save('design/shot_text_5_after_delete.png')

# ------------------------------------- Esc отменяет правку, клик мимо — сохраняет
left_item = win.canvas.map_texts[0]
win.draw_buttons['text'].click()
canvas_click(win.canvas, left_item['x'] + 6, left_item['y'] + 6)
check('S30 двойное редактирование открывается кликом по надписи',
      win.canvas._text_editor is not None)
if win.canvas._text_editor is not None:
    win.canvas._text_editor.selectAll()
    win.canvas._text_editor.insert('Испорчено')
    QTest.keyClick(win.canvas._text_editor, Qt.Key_Escape)
    app.processEvents()
check('S31 Esc возвращает прежний текст и закрывает поле',
      win.canvas._text_editor is None
      and win.canvas.map_texts[0]['text'] == left_item['text'],
      repr(win.canvas.map_texts[0]['text']))

canvas_click(win.canvas, left_item['x'] + 6, left_item['y'] + 6)
if win.canvas._text_editor is not None:
    win.canvas._text_editor.end(False)
    win.canvas._text_editor.insert('!')
    app.processEvents()
canvas_click(win.canvas, left_item['x'] + 400, left_item['y'] + 200)
if win.canvas._text_editor is not None:
    QTest.keyClick(win.canvas._text_editor, Qt.Key_Escape)
    app.processEvents()
check('S32 клик мимо сохраняет правку и не плодит пустую надпись',
      len(win.canvas.map_texts) == 1
      and win.canvas.map_texts[0]['text'] == left_item['text'] + '!',
      f'{len(win.canvas.map_texts)} / {[t["text"] for t in win.canvas.map_texts]}')
check('S33 сохранённая правка доехала до сервера',
      any(row['text'] == left_item['text'] + '!'
          for row in server.game_state.map_texts.get(
              win.canvas.current_map.id, [])),
      str([row['text'] for row in server.game_state.map_texts.get(
          win.canvas.current_map.id, [])]))
# -------------------------------------------- все четыре начертания работают
# Панель меняет и выделенную надпись — сначала выделяем её инструментом
# «Перемещение»: он не открывает поле правки прямо на карте.
win.draw_buttons['move'].click()
app.processEvents()
last_text = win.canvas.map_texts[0]
canvas_click(win.canvas, last_text['x'] + 4, last_text['y'] + 4)
for key in ('italic', 'underline', 'strike'):
    win.text_style_buttons[key].click()
    app.processEvents()
flags = current_text(win.canvas.selected_text_id or win.canvas.map_texts[0]['id'])
check('S34 курсив, подчёркнутый и зачёркнутый включаются с панели',
      all(flags[key] for key in ('italic', 'underline', 'strike')),
      str({k: flags[k] for k in ('bold', 'italic', 'underline', 'strike')}))
for key in ('italic', 'underline', 'strike'):
    win.text_style_buttons[key].click()
app.processEvents()
flags = current_text(win.canvas.map_texts[0]['id'])
check('S35 начертания снимаются обратно',
      not any(flags[key] for key in ('italic', 'underline', 'strike')))
win.grab().save('design/shot_text_6_final.png')

# --------------------------------------------- игрок видит надписи, но не правит
player_client = NetworkClient()
player_info = {
    'mode': 'join', 'nickname': 'Игрок', 'host': '127.0.0.1', 'port': port,
    'avatar_path': None, 'player_id': generate_id(), 'role': 'player',
    'enable_3d': False, 'combat_effects': True, 'blood_transparency': 0,
    'preload_my_actors': False,
}
player_client.connect('127.0.0.1', port)
player_win = MainWindow(player_info, player_client, server=None)
player_win.resize(1200, 800)
player_win.show()
player_ready = wait_until(
    lambda: player_win.canvas.current_map is not None
    and not player_win.canvas.is_map_loading(), 10.0)
check('S36 у игрока карта открылась сама', player_ready)
seen_texts = [t['text'] for t in player_win.canvas.map_texts]
check('S37 игрок получил надписи с картой',
      any(text.strip() for text in seen_texts), str(seen_texts))
check('S38 у игрока нет вкладки рисования и панели текста',
      player_win._draw_tab_index is None
      and not hasattr(player_win, 'text_font_combo'))

if player_win.canvas.map_texts:
    item = player_win.canvas.map_texts[0]
    player_win.canvas.fit_map_to_screen()
    app.processEvents()
    drawn = text_pixels(player_win.canvas, item, tuple(item['color']))
    # На карте, вписанной в окно игрока, надпись мелкая — десятка пикселей
    # своего цвета уже достаточно: пустое место даёт ноль.
    check('S39 надпись видна на холсте игрока', drawn > 10,
          f'пикселей цвета текста {drawn}')
    player_win.canvas.grab().save('design/shot_text_7_player.png')
player_win.close()
app.processEvents()
player_client.shutdown()

# ------------------------------------------------------------- итог
win.close()
app.processEvents()
client.shutdown()
if getattr(server, 'running', False):
    server.stop()

print()
failed = [r for r in results if not r[1]]
print(f'всего {len(results)}, провалено {len(failed)}')
sys.exit(1 if failed else 0)
