# -*- mode: python ; coding: utf-8 -*-
"""Сборка onedir: папка «Berkut VTT beta 2.1» с .exe и ресурсами.

Запуск:
    python -m PyInstaller --noconfirm Berkut_VTT_onedir.spec \
        --distpath build/dist --workpath build/work

Отличие от DnD_VirtualTable.spec: программа не упакована в один файл,
поэтому рядом с .exe лежат папки client/assets, dice_lab/assets и
служебные библиотеки. library и data добавляет build_release.bat.
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ('client/assets', 'client/assets'),
    ('dice_lab/assets', 'dice_lab/assets'),
    # Русские имена/тексты dnd.su: без этого файла в собранной игре
    # остаются только жёстко зашитые переводы, а справочник — на латинице.
    ('shared/dnd5e_loc_data.json', 'shared'),
]
binaries = []
hiddenimports = [
    'PyQt5', 'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets',
    'pygame', 'pygame.mixer',
    'dice_lab.app',
]

# Panda3D нужен только для 3D-кубиков, но подключается динамически:
# без collect_all в собранной игре пропадали модели и шейдеры.
for package in ('panda3d', 'dice_lab', 'pygame'):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Все собственные подпакеты: часть клиента подключается по имени.
for package in ('client', 'server', 'shared', 'cli'):
    hiddenimports += collect_submodules(package)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5.QtMultimedia', 'PyQt5.QtWebEngineWidgets',
              'PyQt5.QtQuick', 'PyQt5.QtQml', 'tkinter'],
    cipher=None,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Berkut VTT',
    icon='client/assets/icons/app.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Berkut VTT beta 2.1',
)
