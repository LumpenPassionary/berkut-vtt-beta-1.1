# Client package
"""Клиент виртуального стола «Berkut VTT dnd5e»."""
import os

from PyQt5.QtGui import QIcon

#: Значок приложения: орёл с D20. Лежит рядом с иконками интерфейса и
#: попадает внутрь .exe тем же правилом PyInstaller, что и остальной арт.
APP_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "icons", "app.png")


def app_icon() -> QIcon:
    """Иконка приложения: окна стола, стартового окна и 3D-лотка."""
    if os.path.isfile(APP_ICON_PATH):
        return QIcon(APP_ICON_PATH)
    return QIcon()
