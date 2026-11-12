"""Стек отмены для операций с картой и токенами.

Каждое действие хранит не снимок всего состояния, а пару «как откатить».
Откат идёт теми же серверными командами, что и обычная операция, поэтому сервер
принимает его как обычное действие и состояние остаётся согласованным у всех
участников. Снимок всего состояния не подходит: он перезатёр бы работу других
участников и разослал бы всем устаревшие данные.

Стек локальный: у мастера свой, у игрока свой. Отменяется только то, что этот
клиент сделал сам.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Action:
    """Одно обратимое действие.

    label     — описание для интерфейса, например «удаление токена «Гоблин»»
    undo      — что выполнить при отмене
    redo      — что выполнить при повторе
    merge_key — ключ склейки. Нужен для перетаскивания: за одно движение мыши
                приходит много промежуточных позиций, и в истории должна
                остаться одна запись, а не сотня.
    """
    label: str
    undo: Callable[[], None]
    redo: Callable[[], None]
    merge_key: Optional[str] = None
    kind: str = 'other'
    data: Dict[str, Any] = field(default_factory=dict)


class UndoStack:
    """История действий с ограничением глубины и склейкой однотипных шагов."""

    def __init__(self, limit: int = 60):
        self.limit = max(1, int(limit))
        self._done: List[Action] = []
        self._undone: List[Action] = []
        self._listeners: List[Callable[[], None]] = []
        # Пока идёт откат или повтор, новые действия не должны писать историю,
        # иначе откат сам себя занесёт в стек и получится петля.
        self._suspended = 0

    # --- подписка для обновления кнопок интерфейса ---
    def add_listener(self, callback: Callable[[], None]):
        self._listeners.append(callback)

    def _notify(self):
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                pass

    @property
    def busy(self) -> bool:
        """Идёт откат или повтор — запись в историю приостановлена."""
        return self._suspended > 0

    # --- состояние ---
    @property
    def can_undo(self) -> bool:
        return bool(self._done)

    @property
    def can_redo(self) -> bool:
        return bool(self._undone)

    @property
    def undo_label(self) -> str:
        return self._done[-1].label if self._done else ''

    @property
    def redo_label(self) -> str:
        return self._undone[-1].label if self._undone else ''

    @property
    def depth(self) -> int:
        return len(self._done)

    def clear(self):
        self._done.clear()
        self._undone.clear()
        self._notify()

    # --- запись ---
    def push(self, action: Action, merge: bool = True):
        """Добавить действие. Новое действие всегда сбрасывает ветку повтора."""
        if self.busy:
            return
        self._undone.clear()
        if (merge and action.merge_key and self._done
                and self._done[-1].merge_key == action.merge_key):
            # Склеиваем: оставляем исходную отмену и берём новый повтор.
            previous = self._done[-1]
            previous.redo = action.redo
            previous.label = action.label
            self._notify()
            return
        self._done.append(action)
        if len(self._done) > self.limit:
            self._done.pop(0)
        self._notify()

    # --- отмена и повтор ---
    def undo(self) -> Optional[str]:
        if not self._done:
            return None
        action = self._done.pop()
        self._suspended += 1
        try:
            action.undo()
        except Exception:
            # Откатить не вышло — возвращаем запись, чтобы шаг не потерялся.
            self._done.append(action)
            raise
        finally:
            self._suspended -= 1
        self._undone.append(action)
        self._notify()
        return action.label

    def redo(self) -> Optional[str]:
        if not self._undone:
            return None
        action = self._undone.pop()
        self._suspended += 1
        try:
            action.redo()
        except Exception:
            self._undone.append(action)
            raise
        finally:
            self._suspended -= 1
        self._done.append(action)
        self._notify()
        return action.label
