# Установщик и папка программы

`BerkutVTT.iss` собирает установщик для текущего пользователя (без UAC).
Шаблон `core.json` кладёт данные в `data` рядом с `.exe`, поэтому папка
программы остаётся переносимой:

```text
Berkut VTT/
  Berkut VTT.exe
  core.json            {"home": "data"}
  data/                миры, автосохранения, кэш, настройки, app.log
    worlds/            ручные сохранения
    autosaves/         последние 10 автосохранений
  library/             справочник SRD, только чтение
  _internal/           библиотеки PyInstaller
```

Установщик **не трогает `data`** при обновлении. При удалении он спрашивает,
удалять ли сохранения; по умолчанию оставляет. Для автоматических проверок
есть ключ `/DELETEUSERDATA` — он удаляет `data` без вопроса. Если папка
программы недоступна на запись, приложение само переносит данные в
`%LOCALAPPDATA%\DnD VTT`.

## Сборка

Полная сборка (onedir + установщик) — из корня проекта:

```bat
build_release.bat
```

Скрипт собирает программу в `build\dist\Berkut VTT beta 1.0`, копирует
`library` и `core.json`, обновляет `G:\001VTT\mvp\Berkut VTT beta 1.0`
(папку `data` не трогает) и запускает Inno Setup. Установщик появляется в
`G:\001VTT\mvp\Berkut Installer\Berkut VTT Setup 1.0 beta.exe`.

Отдельно установщик можно пересобрать так:

```bat
"C:\Users\<user>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer\BerkutVTT.iss
```

Нужен Inno Setup 6: `winget install JRSoftware.InnoSetup`.

## Проверка установки

```bat
set SETUP=G:\001VTT\mvp\Berkut Installer\Berkut VTT Setup 1.0 beta.exe
%SETUP% /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=G:\vtt-test
```

Проверить `Berkut VTT.exe`, `library`, `data`; создать файл в `data\worlds`;
запустить установщик ещё раз (обновление) — файл должен остаться; запустить
`unins000.exe /VERYSILENT /SUPPRESSMSGBOXES` — программа удаляется, `data`
остаётся. Для проверки удаления данных:

```bat
unins000.exe /VERYSILENT /SUPPRESSMSGBOXES /DELETEUSERDATA
```
