@echo off
chcp 65001 >nul
title Dice Lab
cd /d "%~dp0.."

set "PY=python"
where python >nul 2>&1 || set "PY=py"
where %PY% >nul 2>&1 || (
    echo Python не найден. Установите его с python.org и повторите.
    pause
    exit /b 1
)

%PY% dice_lab\main.py
if errorlevel 1 pause
