"""
Конфигурация CLI: singleton-файл config.json (аналог .fvttrc.yml у Foundry).

Файл лежит в домашней папке данных приложения: путь из core.json рядом
с игрой, либо $DND_VTT_HOME/config.json, либо ~/.dnd_vtt/config.json.
Ключи:

  dataPath           — корень данных: тут лежат миры (worlds/<id>/world.json);
                       по умолчанию — сама домашняя папка;
  installPath        — куда запускать игру: папка с исходниками (main.py)
                       либо путь к DnD_VirtualTable.exe;
  currentPackageId   — «рабочий мир» для команд package unpack/pack;
  currentPackageType — тип пакета (пока только World).

Любой путь можно переключить на свой файл флагом `vtt --config <файл>`
(как `fvtt --config` у Foundry): локальный конфиг накладывается поверх
основного.
"""
import json
import os
import shutil
import tempfile

from shared import world_store


class Config:
    """Единственный экземпляр на процесс — как Config.instance в fvtt."""

    _instance = None

    def __init__(self, path: str = None):
        self.config_path = path or world_store.get_config_path()
        self._config = {}
        if not os.path.exists(self.config_path):
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            self._write()
        else:
            with open(self.config_path, encoding="utf-8") as f:
                self._config = json.load(f)

    @classmethod
    def instance(cls) -> "Config":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls, path: str = None) -> "Config":
        """Новый экземпляр (нужно тестам и смене --config)."""
        cls._instance = cls(path)
        return cls._instance

    # ------------------------------------------------------------------

    def get_all(self) -> dict:
        return dict(self._config)

    def get(self, key: str):
        return self._config.get(key)

    def set(self, key: str, value):
        self._config[key] = value
        self._write()

    def load_local_conf(self, config_file: str):
        """Наложить локальный конфиг поверх текущего (флаг --config)."""
        if not os.path.exists(config_file):
            return
        with open(config_file, encoding="utf-8") as f:
            conf = json.load(f)
        self.config_path = config_file
        for key, value in conf.items():
            self._config[key] = value

    def data_root(self) -> str:
        """Корень данных: dataPath из конфига, иначе домашняя папка."""
        configured = self.get("dataPath")
        if configured:
            return os.path.abspath(os.path.expanduser(configured))
        return world_store.home_dir()

    # ------------------------------------------------------------------

    def _write(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.config_path),
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)
            shutil.move(tmp, self.config_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
