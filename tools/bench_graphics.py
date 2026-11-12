"""Замер графики стола: сколько стоит кадр со светом и стенами.

Запуск (из корня проекта):

    QT_QPA_PLATFORM=offscreen python tools/bench_graphics.py
    QT_QPA_PLATFORM=offscreen python tools/bench_graphics.py --lights 40 --walls 200

Зачем это отдельным файлом, а не тестом: числа зависят от машины, и держать
их в тестах нельзя — они падали бы на каждом ноутбуке по-своему. Здесь же
печатается отчёт: холодная сборка маски, кадр в покое, кадр при
панорамировании, стоимость многоугольников света тремя путями (все стены
циклом, все стены матрицей, только ближние стены) и отчёт о памяти.
"""
import argparse
import os
import statistics
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DND_VTT_HOME", tempfile.mkdtemp(prefix="bench_gfx_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication

from shared import hwinfo
from shared import lighting as lighting_rules
from shared import walls as wall_rules
from shared.models import MapData
from client.game_canvas import GameCanvas


def build_scene(lights: int, walls: int, size=(1600, 900)):
    """Карта с сеткой стен и источниками света: типичный большой стол."""
    cell = 20.0  # 5 футов в мировых единицах
    cols = int(size[0] / cell) + 60
    rows = int(size[1] / cell) + 60
    map_data = MapData(id="bench", name="Замер", image_path="",
                       width=cols * cell, height=rows * cell,
                       grid_cols=cols, grid_rows=rows, pos_x=0.0, pos_y=0.0,
                       grid_visible=True, time_of_day="night", lights=[])
    # Стены: решётка комнат. Ровно `walls` отрезков.
    wall_list = []
    step = max(3, int((cols / max(1, walls ** 0.5)) ))
    used = 0
    row = 2
    while used < walls:
        y = row * cell * 4
        x = 1
        while used < walls and x < cols - 1:
            wall_list.append({"id": "w%d" % used, "points": [
                [x * cell, y], [(x + step) * cell, y]],
                "closed": True})
            used += 1
            x += step * 2
        row += 1
        if row > rows:
            break
    map_data.walls = wall_list
    lights_data = []
    for index in range(lights):
        lights_data.append({
            "id": "l%d" % index, "name": "Факел %d" % index,
            "x": float((index % 6) * 220 + 120),
            "y": float((index // 6) * 180 + 120),
            "radius_ft": 30.0, "color": "#ffb457", "intensity": 0.55,
            "enabled": True, "kelvin": 1800,
        })
    map_data.lights = lights_data
    canvas = GameCanvas()
    canvas.resize(*size)
    canvas.set_lighting_enabled(True)
    canvas.set_graphics({"rays": 220, "shadows": True, "antialiasing": True})
    canvas.set_map(map_data)
    canvas.scale = 1.0
    canvas.offset_x = 0.0
    canvas.offset_y = 0.0
    return canvas, map_data


def frame_ms(canvas, repeats=12):
    """Сколько идёт кадр: `grab()` рисует всё, что видит мастер."""
    times = []
    for _ in range(repeats):
        start = time.perf_counter()
        canvas.grab()
        times.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(times), max(times)


def polygon_costs(canvas, map_data):
    """Стоимость многоугольников света тремя путями (сумма на все источники)."""
    segments = canvas.wall_sight_segments()
    lights = [light for light in lighting_rules.active_lights(map_data)]
    radii = [canvas.light_radius_world(light) for light in lights]

    def run_all(pure_python):
        saved = wall_rules._visibility_polygon_numpy
        if pure_python:
            wall_rules._visibility_polygon_numpy = lambda *a, **k: None
        try:
            start = time.perf_counter()
            for light, radius in zip(lights, radii):
                wall_rules.visibility_polygon(light["x"], light["y"], radius,
                                              segments, rays=220)
            return (time.perf_counter() - start) * 1000.0
        finally:
            wall_rules._visibility_polygon_numpy = saved

    index = wall_rules.SegmentIndex(segments)

    def run_near():
        start = time.perf_counter()
        for light, radius in zip(lights, radii):
            near = index.near(light["x"], light["y"], radius)
            wall_rules.visibility_polygon(light["x"], light["y"], radius,
                                          near, rays=220)
        return (time.perf_counter() - start) * 1000.0

    near_total = run_near()
    return {
        "walls": len(segments),
        "lights": len(lights),
        "all_python": run_all(True),
        "all_numpy": run_all(False),
        "near": near_total,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lights", type=int, default=24)
    parser.add_argument("--walls", type=int, default=120)
    args = parser.parse_args()

    app = QApplication.instance() or QApplication(sys.argv)
    canvas, map_data = build_scene(args.lights, args.walls)

    # Холодная сборка: правка геометрии сбрасывает все кэши света.
    canvas.invalidate_walls_cache()
    start = time.perf_counter()
    canvas.grab()
    cold = (time.perf_counter() - start) * 1000.0

    idle, idle_max = frame_ms(canvas)

    # Панорамирование: камера едет по 40 мировых единиц за кадр.
    pan = []
    rebuilds = 0
    signature = canvas._night_mask_signature
    for _ in range(40):
        canvas.offset_x -= 40.0
        start = time.perf_counter()
        canvas.grab()
        pan.append((time.perf_counter() - start) * 1000.0)
        if canvas._night_mask_signature != signature:
            rebuilds += 1
            signature = canvas._night_mask_signature

    polygons = polygon_costs(canvas, map_data)
    canvas.set_graphics({"rays": 220, "shadows": True, "antialiasing": True})
    canvas.grab()
    stats = canvas.graphics_report()
    app.processEvents()

    print("=== Сцена: светов %d, стен %d, окно %dx%d" % (
        args.lights, args.walls, canvas.width(), canvas.height()))
    print("холодная сборка маски: %.0f мс" % cold)
    print("кадр в покое: медиана %.1f мс, худший %.1f мс" % (idle, idle_max))
    print("кадр при панорамировании: медиана %.1f мс, худший %.1f мс, "
          "пересборок маски %d из 40"
          % (statistics.median(pan), max(pan), rebuilds))
    print("--- многоугольники света (%d источников, %d отрезков):" % (
        polygons["lights"], polygons["walls"]))
    print("    все стены, Python-цикл: %6.0f мс" % polygons["all_python"])
    print("    все стены, матрица:    %6.0f мс" % polygons["all_numpy"])
    print("    ближние стены:         %6.0f мс" % polygons["near"])
    print("--- память")
    print(hwinfo.report_text(buffers=stats["buffers"],
                             frame_ms=stats["frame_ms"]))


if __name__ == "__main__":
    main()
