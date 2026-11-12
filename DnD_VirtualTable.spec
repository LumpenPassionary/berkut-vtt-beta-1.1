# -*- mode: python ; coding: utf-8 -*-
"""Сборка в один исполняемый файл: pyinstaller DnD_VirtualTable.spec"""

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # Иконки интерфейса и ресурсы 3D-кубиков должны попасть внутрь .exe:
    # иначе в собранной игре пропадают иконки рейки и череп спасбросков.
    datas=[('client/assets', 'client/assets'),
           ('dice_lab/assets', 'dice_lab/assets'),
           # Без этого файла в .exe пропадает русская локализация SRD.
           ('shared/dnd5e_loc_data.json', 'shared')],
    hiddenimports=[
        'PyQt5', 'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets',
        'pygame', 'pygame.mixer',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5.QtMultimedia', 'PyQt5.QtWebEngineWidgets',
              'PyQt5.QtQuick', 'PyQt5.QtQml'],
    cipher=None,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='DnD_VirtualTable',
    # Значок приложения (орёл с D20) — тот же, что у окон игры.
    icon='client/assets/icons/app.ico',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
