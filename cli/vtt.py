#!/usr/bin/env python
"""
Консоль виртуального стола — аналог официального fvtt CLI для Foundry VTT.

Команды:
  vtt configure [get|set|path|view]   — конфигурация (config.json)
  vtt launch [--port N] [--data DIR]  — запустить игру с настройками
  vtt package [workon|clear|unpack|pack] — работа с мирами как с пакетами

Миры хранятся в <dataPath>/worlds/<id>/world.json. `package unpack`
разкладывает мир на файлы «по одному документу на файл» (удобно править
и держать в git), `package pack` собирает обратно.

Запуск:
  python cli/vtt.py --help
  python cli/vtt.py configure set installPath "G:/001VTT/mvp/альфа 2.3/vtt_dnd"
  python cli/vtt.py package workon my-world
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli.config import Config            # noqa: E402
from shared import world_store           # noqa: E402
from shared import dnd5e_lib             # noqa: E402

DEFAULT_PORT = 25565


# ------------------------------------------------------------------
# configure
# ------------------------------------------------------------------

def cmd_configure(args, config: Config) -> int:
    action = args.action

    if action == "get":
        print(config.get(args.key))
        return 0

    if action == "set":
        config.set(args.key, args.value)
        print(f"{args.key} = {args.value}")
        return 0

    if action == "path":
        print(config.config_path)
        return 0

    if action == "view":
        print("Текущая конфигурация:")
        for key, value in sorted(config.get_all().items()):
            print(f"  {key} = {value}")
        return 0

    # Проверка без действий — как `fvtt configure`
    install_path = config.get("installPath")
    if not install_path:
        print("installPath не задан. Задайте: "
              "`vtt configure set installPath <путь>` — папка с игрой "
              "(где лежит main.py) либо путь к DnD_VirtualTable.exe")
    data_path = config.get("dataPath")
    if not data_path:
        print("dataPath не задан — используется домашняя папка: "
              f"{world_store.home_dir()}\n"
              "Задать свою: `vtt configure set dataPath <путь>`")
    if install_path:
        print("Настройка завершена!")
    return 0


# ------------------------------------------------------------------
# launch
# ------------------------------------------------------------------

def resolve_launch_target(install_path: str):
    """Определить, как запускать игру: exe или исходники (main.py)."""
    install_path = os.path.abspath(os.path.expanduser(install_path))
    if os.path.isfile(install_path):
        return [install_path], os.path.dirname(install_path)
    main_py = os.path.join(install_path, "main.py")
    if os.path.isfile(main_py):
        return [sys.executable, main_py], install_path
    return None, install_path


def cmd_launch(args, config: Config) -> int:
    install_path = config.get("installPath")
    if not install_path:
        print("installPath не задан. Задайте: "
              "`vtt configure set installPath <путь>`")
        return 1

    argv_exe, cwd = resolve_launch_target(install_path)
    if argv_exe is None:
        print(f"Не найден ни {os.path.join(cwd, 'main.py')}, "
              "ни файл игры по installPath")
        return 1

    env = dict(os.environ)
    env["DND_VTT_DATA"] = config.data_root()
    if args.port:
        env["DND_VTT_PORT"] = str(args.port)

    print(f"Запуск: {' '.join(argv_exe)}  (данные: {env['DND_VTT_DATA']})")
    try:
        proc = subprocess.Popen(argv_exe, cwd=cwd, env=env)
    except OSError as exc:
        print(f"Не удалось запустить игру: {exc}")
        return 1
    code = proc.wait()
    print(f"Игра завершилась с кодом {code}")
    return code


# ------------------------------------------------------------------
# package
# ------------------------------------------------------------------

def current_package(config: Config):
    return config.get("currentPackageId"), config.get("currentPackageType")


def cmd_package(args, config: Config) -> int:
    pkg_id, pkg_type = current_package(config)
    if args.id:
        pkg_id = args.id
    if args.type:
        pkg_type = args.type

    action = args.action

    if action == "workon":
        return handle_workon(args, config, pkg_id, pkg_type)
    if action == "clear":
        config.set("currentPackageId", None)
        config.set("currentPackageType", None)
        print("Рабочий пакет сброшен")
        return 0
    if action == "unpack":
        return handle_unpack(args, config, pkg_id, pkg_type)
    if action == "pack":
        return handle_pack(args, config, pkg_id, pkg_type)

    # Без действия — показать текущий пакет (как в fvtt)
    if not pkg_id:
        print("Рабочий пакет не задан. Используйте `package workon <ид_мира>`")
        return 0
    print(f"Сейчас открыт {pkg_type or 'World'} {pkg_id}")
    return 0


def handle_workon(args, config: Config, pkg_id, pkg_type) -> int:
    """Переключиться на мир: запомнить в конфиге (как fvtt package workon)."""
    if pkg_type and pkg_type != "World":
        print(f"Тип {pkg_type} не поддерживается — эта игра знает только миры")
        return 1
    world_id = args.value
    if not world_id:
        print("Не указан идентификатор мира: `package workon <ид_мира>`")
        return 1

    data_root = config.data_root()
    try:
        world_store.sanitize_world_id(world_id)
    except ValueError as exc:
        print(exc)
        return 1
    if not os.path.isfile(world_store.world_path(data_root, world_id)):
        print(f"Мир «{world_id}» не найден в {os.path.join(data_root, 'worlds')}. "
              "Сначала соберите его: `package pack` — либо сохраните мир "
              "из игры кнопкой «Сохранить мир…».")
        return 1

    config.set("currentPackageId", world_id)
    config.set("currentPackageType", "World")
    print(f"Рабочий пакет: World {world_id}")
    return 0


def determine_paths(args, config: Config, pkg_id, pkg_type, operation):
    """Пути операции: явные --in/--out либо <dataPath>/worlds/<id>/..."""
    using_default = not (args.input_directory and args.output_directory)
    world_id = args.value or pkg_id
    if using_default and not world_id:
        print("Не задан мир. Используйте `package workon <ид_мира>`")
        return None, None
    data_root = config.data_root()

    try:
        world_store.sanitize_world_id(world_id)
    except ValueError as exc:
        print(exc)
        return None, None

    source_default = os.path.join(data_root, "worlds", world_id, "_source")
    world_default = world_store.world_path(data_root, world_id)

    if operation == "pack":
        source = args.input_directory or source_default
        world_file = args.output_directory and os.path.join(
            args.output_directory, world_store.WORLD_FILENAME) or world_default
    else:
        world_file = args.input_directory or world_default
        source = args.output_directory or source_default

    return (os.path.abspath(source), os.path.abspath(world_file))


def handle_unpack(args, config: Config, pkg_id, pkg_type) -> int:
    source, world_file = determine_paths(
        args, config, pkg_id, pkg_type, "unpack")
    if not source:
        return 1
    if not os.path.isfile(world_file):
        print(f"Файл мира не найден: {world_file}")
        return 1

    log = (lambda text: print(text)) if args.verbose else None
    print(f"Распаковка «{world_file}» → «{source}»")
    try:
        written = world_store.unpack_world(world_file, source,
                                           clean=args.clean, log=log)
    except (OSError, ValueError) as exc:
        print(f"Ошибка распаковки: {exc}")
        return 1
    print(f"Записано файлов: {len(written)}")
    return 0


def handle_pack(args, config: Config, pkg_id, pkg_type) -> int:
    source, world_file = determine_paths(
        args, config, pkg_id, pkg_type, "pack")
    if not source:
        return 1
    if not os.path.isdir(source):
        print(f"Папка с исходниками не найдена: {source}")
        return 1

    log = (lambda text: print(text)) if args.verbose else None
    print(f"Сборка «{source}» → «{world_file}»")
    try:
        world_store.pack_world(source, world_file, log=log)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Ошибка сборки: {exc}")
        return 1
    return 0


# ------------------------------------------------------------------
# library
# ------------------------------------------------------------------

def cmd_library(args, config: Config) -> int:
    """Библиотека D&D 5e (SRD) из дистрибутива Foundry dnd5e."""
    if args.library_action == "import-dnd5e":
        if not args.path:
            print("Укажите путь к dnd5e-release-*.zip или папке системы")
            return 1
        print(f"Импорт SRD-библиотеки из {args.path} …")
        try:
            counts = dnd5e_lib.import_dnd5e(
                args.path, config.data_root(), log=print)
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"Импорт не удался: {exc}")
            return 1
        print("Готово: " + ", ".join(f"{k} — {v}" for k, v in counts.items()))
        return 0

    if args.library_action == "list":
        lib = dnd5e_lib.load_library(config.data_root())
        index = lib.get("index") or {}
        if not any(lib[k] for k in ("monsters", "items", "spells")):
            print("Библиотека пуста. Импортируйте: "
                  "`vtt library import-dnd5e <путь к dnd5e.zip>`")
            return 0
        print(f"Источник: {index.get('source', '?')} "
              f"(от {str(index.get('imported_at', '?'))[:10]})")
        for key, label in (("monsters", "монстры"), ("items", "предметы"),
                           ("spells", "заклинания")):
            print(f"  {label}: {len(lib[key])}")
        return 0

    print("Команды library: import-dnd5e <путь>, list")
    return 0


# ------------------------------------------------------------------
# Точка входа
# ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vtt",
        description="Консоль виртуального стола D&D (аналог fvtt CLI)")
    parser.add_argument("--config", metavar="ФАЙЛ",
                        help="путь к локальному config.json")

    sub = parser.add_subparsers(dest="command")

    p_conf = sub.add_parser("configure", help="работа с конфигурацией")
    p_conf.add_argument("action", nargs="?",
                        choices=["get", "set", "path", "view"])
    p_conf.add_argument("key", nargs="?")
    p_conf.add_argument("value", nargs="?")

    p_launch = sub.add_parser("launch", help="запустить игру")
    p_launch.add_argument("--port", type=int,
                          help="порт сервера (по умолчанию 25565)")
    p_launch.add_argument("--data", dest="data_dir",
                          help="папка данных (пока не используется игрой)")

    p_lib = sub.add_parser("library", help="библиотека D&D 5e (SRD)")
    p_lib.add_argument("library_action", nargs="?",
                       choices=["import-dnd5e", "list"])
    p_lib.add_argument("path", nargs="?")

    p_pkg = sub.add_parser("package", help="работа с мирами")
    p_pkg.add_argument("action", nargs="?",
                       choices=["workon", "clear", "unpack", "pack"])
    p_pkg.add_argument("value", nargs="?")
    p_pkg.add_argument("--id", help="идентификатор мира (вместо workon)")
    p_pkg.add_argument("--type", help="тип пакета (только World)")
    p_pkg.add_argument("--in", "-in", dest="input_directory",
                       help="папка исходников (pack) или файл мира (unpack)")
    p_pkg.add_argument("--out", "-out", dest="output_directory",
                       help="файл мира (pack) или папка назначения (unpack)")
    p_pkg.add_argument("--clean", "-c", action="store_true",
                       help="при распаковке удалить папку назначения")
    p_pkg.add_argument("--verbose", "-v", action="store_true",
                       help="показывать каждый записанный файл")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = (Config.reset(args.config) if args.config
              else Config.instance())

    if args.command == "configure":
        if args.action == "set" and (args.key is None or args.value is None):
            parser.error("для set нужны ключ и значение")
        if args.action == "get" and args.key is None:
            parser.error("для get нужен ключ")
        return cmd_configure(args, config)
    if args.command == "launch":
        return cmd_launch(args, config)
    if args.command == "package":
        return cmd_package(args, config)
    if args.command == "library":
        return cmd_library(args, config)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
