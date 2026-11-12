"""Собрать папку выпуска: программа, справочник и пустая папка данных.

Зачем отдельный скрипт. Путь выпуска — `G:\\001VTT\\mvp\\Berkut beta 2.1`, то есть
с кириллицей. Внутри .bat такая строка читается кодовой страницей консоли:
с `chcp 65001` cmd теряет место в файле и переменная остаётся пустой, без
него путь превращается в мусорные символы, и robocopy уезжает в папку с
искажённым именем. Python читает исходник как UTF-8 всегда, поэтому путь
живёт здесь, а .bat остаётся целиком латинским.

Что делает шаг:

* докладывает в собранную папку `core.json` и `library` (справочник);
* создаёт пустую `data`;
* переносит всё в папку выпуска, **не трогая** `data` — там миры,
  автосохранения и настройки игрока;
* убирает из папки выпуска файлы, которых в новой сборке нет (раньше это
  делал `robocopy /MIR`).

Запуск: python tools/collect_release.py
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STAGE = os.path.join(ROOT, "build", "dist", "Berkut VTT beta 2.1")
RELEASE = r"G:\001VTT\mvp\Berkut beta 2.1"
EXE_NAME = "Berkut VTT.exe"


def prepare_stage() -> None:
    """Дополнить собранную папку тем, что не кладёт PyInstaller."""
    core_src = os.path.join(ROOT, "installer", "core.json")
    if os.path.isfile(core_src):
        shutil.copy2(core_src, os.path.join(STAGE, "core.json"))
    library_src = os.path.join(ROOT, "library")
    library_dst = os.path.join(STAGE, "library")
    if os.path.isdir(library_src):
        if os.path.isdir(library_dst):
            shutil.rmtree(library_dst)
        shutil.copytree(library_src, library_dst)
    os.makedirs(os.path.join(STAGE, "data"), exist_ok=True)


def sync_tree(src: str, dst: str) -> None:
    """Сделать dst копией src, кроме папки data (пользовательские файлы)."""
    os.makedirs(dst, exist_ok=True)
    for name in sorted(os.listdir(src)):
        if name == "data":
            continue
        source = os.path.join(src, name)
        target = os.path.join(dst, name)
        if os.path.isdir(source):
            if os.path.isfile(target):
                os.remove(target)
            sync_tree(source, target)
        else:
            if os.path.isdir(target):
                shutil.rmtree(target)
            shutil.copy2(source, target)
    for name in sorted(os.listdir(dst)):
        if name == "data":
            continue
        target = os.path.join(dst, name)
        if not os.path.exists(os.path.join(src, name)):
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)


def main() -> int:
    if not os.path.isfile(os.path.join(STAGE, EXE_NAME)):
        print(f"[ERROR] Нет собранной программы: {STAGE}\\{EXE_NAME}")
        return 1
    prepare_stage()
    os.makedirs(os.path.join(RELEASE, "data"), exist_ok=True)
    sync_tree(STAGE, RELEASE)
    exe = os.path.join(RELEASE, EXE_NAME)
    if not os.path.isfile(exe):
        print(f"[ERROR] Программа не попала в выпуск: {exe}")
        return 1
    print(f"Программа: {exe}")
    print(f"Данные:    {os.path.join(RELEASE, 'data')} (не тронуты)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
