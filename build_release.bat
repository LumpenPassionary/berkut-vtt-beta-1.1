@echo off
setlocal EnableExtensions
title Build Berkut VTT: onedir + installer

cd /d "%~dp0"

rem Папка выпуска содержит кириллицу, а .bat читается кодовой страницей
rem консоли: и chcp 65001, и обычный robocopy с таким путём портили копию.
rem Поэтому путь живёт в tools\collect_release.py (там UTF-8 всегда), а этот
rem скрипт остаётся целиком латинским.
set "DIST=%~dp0build\dist"
set "STAGE=%DIST%\Berkut VTT beta 2.1"
set "BUILD_WORK=%~dp0build\work"

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
    where py >nul 2>&1 && set "PY=py"
)
if not defined PY (
    echo [ERROR] Python not found. Install Python 3.9+ from python.org.
    goto :fail
)

echo [1/4] Checking dependencies...
%PY% -m pip install --disable-pip-version-check --quiet -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [ERROR] Could not install dependencies.
    goto :fail
)

echo [2/4] Building onedir...
if exist "%DIST%" rmdir /s /q "%DIST%"
%PY% -m PyInstaller --noconfirm --distpath "%DIST%" --workpath "%BUILD_WORK%" "%~dp0Berkut_VTT_onedir.spec"
if errorlevel 1 (
    echo [ERROR] PyInstaller failed.
    goto :fail
)

if not exist "%STAGE%\Berkut VTT.exe" (
    echo [ERROR] Missing "%STAGE%\Berkut VTT.exe".
    goto :fail
)

echo [3/4] Adding core.json, library, empty data and updating the release folder...
rem Копирование делает Python: он читает кириллический путь как UTF-8.
rem Старые файлы сборки убираются, а data пользователя остаётся на месте.
%PY% "%~dp0tools\collect_release.py"
if errorlevel 1 (
    echo [ERROR] Could not update the release folder. Close the game and retry.
    goto :fail
)

echo [4/4] Building installer...
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo [WARNING] Inno Setup 6 not found - installer was not built.
    echo Install it with: winget install JRSoftware.InnoSetup
    goto :done
)
"%ISCC%" "%~dp0installer\BerkutVTT.iss"
if errorlevel 1 (
    echo [ERROR] Inno Setup failed.
    goto :fail
)

:done
echo.
echo DONE.
echo   Release folder and installer paths are printed by collect_release.py
rem Cyrillic is avoided here on purpose: cmd shows it in the console codepage.
pause
exit /b 0

:fail
echo.
echo Build stopped. See messages above.
pause
exit /b 1
