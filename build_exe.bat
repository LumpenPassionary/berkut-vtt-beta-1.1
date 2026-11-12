@echo off
chcp 65001 >nul
title Сборка D&D Virtual Tabletop в .exe
cd /d "%~dp0"

set PY=
where python >nul 2>&1 && set PY=python
if "%PY%"=="" (where py >nul 2>&1 && set PY=py)
if "%PY%"=="" (
    echo [ОШИБКА] Python не найден. Установите с python.org и повторите.
    pause
    exit /b 1
)

echo Устанавливаю библиотеки и сборщик...
%PY% -m pip install --upgrade pip --quiet
%PY% -m pip install PyQt5 pygame panda3d==1.10.16 pyinstaller --quiet

echo.
echo Собираю один исполняемый файл (займёт 2-5 минут)...
%PY% -m PyInstaller --noconfirm --onefile --windowed ^
    --name "DnD_VirtualTable" ^
    --icon "client\assets\icons\app.ico" ^
    --hidden-import PyQt5.QtCore ^
    --hidden-import PyQt5.QtGui ^
    --hidden-import PyQt5.QtWidgets ^
    --hidden-import pygame ^
    --collect-submodules pygame ^
    --hidden-import dice_lab.app ^
    --collect-submodules dice_lab ^
    --collect-all panda3d ^
    --add-data "dice_lab\assets;dice_lab\assets" ^
    --add-data "client\assets;client\assets" ^
    main.py

echo.
if exist "dist\DnD_VirtualTable.exe" (
    rem Данные собранной игры лежат в папке data рядом с .exe, а не в
    rem профиле пользователя и не в core.json разработчика (там диск G:).
    rem Шаблон installer\core.json содержит относительный путь "data".
    if not exist "dist\data" mkdir "dist\data" >nul
    if exist "installer\core.json" (
        copy /y "installer\core.json" "dist\core.json" >nul
    ) else (
        echo {"home": "data"} > "dist\core.json"
    )
    if exist "library" xcopy "library" "dist\library" /E /I /Y >nul
    echo ГОТОВО! Файл: dist\DnD_VirtualTable.exe
    echo Рядом папки data (сохранения) и library (справочник).
    echo Его можно скопировать на другой компьютер и запускать без Python.
) else (
    echo Сборка не удалась, смотрите сообщения выше.
)
pause
