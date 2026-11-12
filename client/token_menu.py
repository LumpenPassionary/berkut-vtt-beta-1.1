"""Пункты контекстного меню токена — отдельно от холста и по ролям.

Меню собиралось прямо в ``game_canvas.show_context_menu`` вперемешку с
рисованием, картами и сеткой, и там же решалось, что показывать игроку, а
что актёру. Из-за этого у персонажа игрока оставались «Карточка актёра» и
«Карточка существа»: они не проверяли роль и открывали вторую запись тех
же чисел, хотя документ персонажа — лист LSS.

Теперь меню токена живёт здесь и спрашивает роль у ``shared/token_kind``,
а холст только рисует и рассылает сигналы:

* общие действия — переименовать, удалить, глазок, хиты и КД,
  атака, спасбросок, состояния — есть у всех;
* карточка актёра (``Карточка актёра…``) — только у актёров: в ней
  теперь и характеристики, и атаки, и спасброски;
* у персонажа игрока карточек нет: его лист открывается вкладкой левой
  панели, поэтому в меню его нет вовсе.

Функция ничего не вызывает в момент сборки: действия подключены к
сигналам холста, так что меню можно собрать и проверить без ``exec_``.
"""
from PyQt5.QtWidgets import QAction

from shared import combat as combat_rules
from shared import conditions as conditions_rules
from shared import token_kind
from client.death_saves import skull_icon


def fill_menu(menu, canvas, token):
    """Добавить в ``menu`` действия над ``token`` (холст шлёт сигналы).

    Возвращает ``menu`` — удобно для тестов и для составных меню.
    """
    token_id = token.id

    act = QAction("Переименовать токен", menu)
    act.triggered.connect(lambda: canvas.rename_token(token_id))
    menu.addAction(act)

    # Размера здесь нет: «увеличить/уменьшить» меняло токен рывками и
    # упиралось в предел холста. Точный размер в клетках задаётся в
    # карточке актёра и на странице листа персонажа (мастеру).
    act = QAction("Удалить токен", menu)
    act.triggered.connect(
        lambda: canvas.remove_token_requested.emit(token_id))
    menu.addAction(act)

    # Глазок: скрыть/показать токен для всей сессии.
    eye = QAction("Показать токен" if not token.visible else "Скрыть токен",
                  menu)
    eye.triggered.connect(
        lambda: canvas.request_token_visibility(token_id))
    menu.addAction(eye)

    # Мёртвого нельзя ни атаковать, ни двигать — зато мастер может вернуть
    # его к жизни. Пункт есть только у мёртвого токена и только у мастера.
    if (getattr(canvas, "is_dm", False)
            and combat_rules.death_save_state(
                getattr(token, "death_saves", None))["dead"]):
        revive = QAction(skull_icon(), "Воскресить (ДМ)…", menu)
        revive.setToolTip("Вернуть мёртвого к жизни: хиты 1, отметки смерти сняты")
        revive.triggered.connect(
            lambda: canvas.token_revive_requested.emit(token_id))
        menu.addAction(revive)

    hp = QAction("Хиты и КД…", menu)
    hp.triggered.connect(lambda: canvas.token_hp_requested.emit(token_id))
    menu.addAction(hp)

    # Карточки — только у актёров: у персонажа игрока документ — лист LSS,
    # и вторая карточка с теми же числами только путала (см. token_kind).
    if token_kind.has_actor_sheet(token):
        sheet = QAction("Карточка актёра… (редакт.)", menu)
        sheet.setToolTip(
            "Редактировать характеристики, атаки и защиту существа")
        sheet.triggered.connect(
            lambda: canvas.token_actor_sheet_requested.emit(token_id))
        menu.addAction(sheet)

    # Атаковать может только тот, кто действует: мёртвого и парализованного
    # (недееспособного, ошеломлённого, без сознания) сервер не пропустит.
    # Пункт остаётся видимым, но выключенным — так видно причину.
    dead = combat_rules.death_save_state(
        getattr(token, "death_saves", None))["dead"]
    can_act, why = conditions_rules.can_act(
        getattr(token, "conditions", None), dead=dead,
        hp=getattr(token, "hp", None))
    atk = QAction("Атаковать этим токеном…", menu)
    atk.setToolTip("Выбрать цель и бросить атаку" if can_act
                   else f"Не может действовать: {why}")
    atk.setEnabled(can_act)
    atk.triggered.connect(lambda: canvas.token_attack_requested.emit(token_id))
    menu.addAction(atk)

    sav = QAction("Спасбросок…", menu)
    sav.setToolTip("Бонус берётся с листа персонажа или из статблока")
    sav.triggered.connect(lambda: canvas.token_save_requested.emit(token_id))
    menu.addAction(sav)

    cond = QAction("Состояния…", menu)
    active = conditions_rules.describe(getattr(token, "conditions", None))
    cond.setToolTip("Правила всех состояний — в окне «Состояния»")
    if active:
        # В меню не влезет полный текст правил, но видно, что уже наложено
        # и как это влияет на бой.
        cond.setText("Состояния…  (" + ", ".join(
            conditions_rules.normalize(getattr(token, "conditions", None))) + ")")
        cond.setToolTip(active)
    cond.triggered.connect(
        lambda: canvas.token_conditions_requested.emit(token_id))
    menu.addAction(cond)
    return menu
