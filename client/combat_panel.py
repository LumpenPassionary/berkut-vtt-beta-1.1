"""Боевые диалоги: атака по токену, спасбросок и состояния.

Всё, что мастер делает за столом во время боя, собрано здесь, чтобы
main_window оставался про layout и сеть.

Диалоги ничего не отправляют сами: они собирают данные и отдают их через
сигналы, а решение — слать ли это в сеть — принимает главное окно. Так
диалоги можно проверять без сервера.
"""
import os

from PyQt5.QtCore import QSize, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFormLayout, QFrame, QGridLayout,
                             QHBoxLayout, QLabel, QPushButton, QScrollArea,
                             QSpinBox, QTextBrowser, QVBoxLayout, QWidget)

from shared import combat as combat_rules
from shared import conditions as conditions_rules
from shared import sheet_items
from shared import token_stats
from client.death_saves import DeathSavesBar


def _token_label(token) -> str:
    """Подпись токена в списках: имя, хиты и КД, если они есть."""
    parts = [token.name]
    if token.hp is not None:
        hp = str(token.hp) + (f"/{token.hp_max}" if token.hp_max else "")
        parts.append(f"ХП {hp}")
    if token.ac is not None or getattr(token, "active_effects", None):
        # Эффекты вроде Щита меняют КД, и в списке цели должно быть то же
        # число, по которому сервер будет сравнивать бросок атаки.
        parts.append(f"КД {token_stats.token_ac(token)}")
    return "  ·  ".join(parts)


class AttackDialog(QDialog):
    """Атака одного токена по другому.

    Мастер выбирает атакующего и цель, вписывает бонус атаки, формулу урона и
    его тип. Бросок делает сервер: клиент только передаёт намерение, поэтому
    подменить результат нельзя.
    """

    attackRequested = pyqtSignal(dict)
    #: игрок выбрал в этом окне оружие для быстрой атаки (attacker_id, weapon_id)
    quickAttackChanged = pyqtSignal(str, str)

    def __init__(self, tokens, attacker_id=None, target_id=None, parent=None,
                 attacks=None, attacks_provider=None, smite_provider=None):
        super().__init__(parent)
        self.setWindowTitle("Атака")
        self.setMinimumWidth(460)
        tokens = list(tokens or [])
        self._tokens = {tok.id: tok for tok in tokens}
        # Готовые атаки из листа персонажа и статблока: выбор вместо ручного ввода.
        # attacks_provider(token_id) позволяет пересобрать список при смене
        # атакующего — иначе у второго токена показывались удары первого.
        self._attacks = list(attacks or [])
        self._attacks_provider = attacks_provider
        # provider(token_id, target_id) -> {'levels': [{'level','dice',...}],
        # 'extra': bool, 'reason': str}. У монстра кары нет — тогда None.
        self._smite_provider = smite_provider

        form = QFormLayout(self)

        self.attacker_combo = QComboBox()
        self.target_combo = QComboBox()
        for token in tokens:
            self.attacker_combo.addItem(_token_label(token), token.id)
            self.target_combo.addItem(_token_label(token), token.id)
        self._select(self.attacker_combo, attacker_id)
        self._select(self.target_combo, target_id)
        form.addRow("Атакует:", self.attacker_combo)
        form.addRow("Цель:", self.target_combo)

        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip(
            "Атаки из листа персонажа и статблока: выбрать — заполнит бонус и урон")
        self.preset_combo.currentIndexChanged.connect(self._apply_preset)
        form.addRow("Атака:", self.preset_combo)

        self.bonus_spin = QSpinBox()
        self.bonus_spin.setRange(-20, 30)
        self.bonus_spin.setValue(0)
        self.bonus_spin.setToolTip(
            "Бонус атаки: модификатор характеристики + бонус мастерства")
        form.addRow("Бонус атаки:", self.bonus_spin)

        self.damage_edit = QComboBox()
        self.damage_edit.setEditable(True)
        self.damage_edit.setToolTip(
            "Формула урона: «2d6+3», «1d8» или пусто, если урон не нужен")
        for formula in ("1d4", "1d6", "1d8", "1d10", "1d12", "2d6", "2d6+3"):
            self.damage_edit.addItem(formula)
        form.addRow("Урон:", self.damage_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItem("— без типа —", "")
        for key, label in combat_rules.DAMAGE_TYPES.items():
            self.type_combo.addItem(label, key)
        self.type_combo.setToolTip(
            "Тип урона нужен, чтобы сработали сопротивления цели")
        form.addRow("Тип урона:", self.type_combo)

        # Божественная кара: только у паладина и только в рукопашной атаке.
        # Кубик и допустимые круги считает общий модуль правил; сервер
        # пересчитает всё сам и спишет ячейку только при попадании.
        self.smite_combo = QComboBox()
        self.smite_combo.addItem("— без кары —", 0)
        self.smite_combo.setEnabled(False)
        self.smite_combo.setToolTip(
            "Божественная кара: потратить ячейку при попадании рукопашным "
            "оружием — 2к8 излучением и +1к8 за круг выше первого")
        self.smite_row_label = QLabel("Божественная кара:")
        form.addRow(self.smite_row_label, self.smite_combo)

        # Оружие для быстрой атаки (Q) выбирается здесь же: вместо того чтобы
        # искать галочку на листе, мастер/игрок отмечает нужное оружие в том
        # окне, где и так выбирает атаку.
        self.quick_check = QCheckBox("Оружие быстрой атаки (Q)")
        self.quick_check.setEnabled(False)
        self.quick_check.setToolTip(
            "Запомнить оружие для Q: нажмите Q и кликните по цели")
        form.addRow("", self.quick_check)

        flags = QHBoxLayout()
        self.adv_check = QCheckBox("Преимущество")
        self.dis_check = QCheckBox("Недостаток")
        flags.addWidget(self.adv_check)
        flags.addWidget(self.dis_check)
        form.addRow("", flags)
        hint = QLabel("Состояния цели и атакующего учитываются автоматически.")
        hint.setObjectName("hint_label")
        hint.setWordWrap(True)
        form.addRow("", hint)

        # Почему атака невозможна: мёртвый или недееспособный атакующий,
        # мёртвая цель. Кнопка «ОК» выключена — сервер всё равно откажет,
        # но лучше видеть причину до броска, а не после отказа.
        self.unavailable = QLabel("")
        self.unavailable.setObjectName("attack_unavailable")
        self.unavailable.setWordWrap(True)
        self.unavailable.setStyleSheet("color: #d08770;")
        self.unavailable.setVisible(False)
        form.addRow("", self.unavailable)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._buttons = buttons

        self._fill_attack_list()
        # Сигнал подключаем после создания preset_combo: иначе первое же
        # переключение атакующего вызывало _reload_attacks до его появления.
        self.attacker_combo.currentIndexChanged.connect(self._reload_attacks)
        self.attacker_combo.currentIndexChanged.connect(self._sync_availability)
        self.attacker_combo.currentIndexChanged.connect(self._reload_smite)
        self.target_combo.currentIndexChanged.connect(self._sync_availability)
        self.target_combo.currentIndexChanged.connect(self._reload_smite)
        self.preset_combo.currentIndexChanged.connect(self._reload_smite)
        # Список заполняем для того атакующего, который уже выбран. Без этого
        # окно, открытое без предварительно выбранного токена, показывало
        # пустой список: атаки собирались для None, а не для первого в списке.
        if self._attacks_provider is not None:
            self._reload_attacks()
        else:
            self._select_preferred()
        self._reload_smite()
        self._sync_availability()

    def _dead(self, token) -> bool:
        return bool(token is not None and combat_rules.death_save_state(
            getattr(token, "death_saves", None))["dead"])

    def _can_act(self, token):
        """Может ли токен действовать: (да/нет, причина)."""
        if token is None:
            return False, "токен не выбран"
        conditions = token_stats.token_conditions(token, None)
        return conditions_rules.can_act(
            conditions, dead=self._dead(token),
            hp=getattr(token, "hp", None))

    def _sync_availability(self, *_):
        """Выключить «ОК», если атакующий не действует или цель мертва."""
        issues = []
        attacker = self._tokens.get(self.attacker_combo.currentData())
        target = self._tokens.get(self.target_combo.currentData())
        allowed, why = self._can_act(attacker)
        if not allowed:
            issues.append(f"«{attacker.name if attacker else '—'}» "
                          f"не может действовать: {why}")
        if self._dead(target):
            issues.append(f"«{target.name}» мёртв — его нельзя сделать целью")
        self.unavailable.setText("  ".join(f"✖ {i}" for i in issues))
        self.unavailable.setVisible(bool(issues))
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(not issues)

    def _select_preferred(self):
        """Отметить в списке атаку токена: entry['chosen'] или первую.

        Атаку с ``chosen`` выставляет окно (collect_token_attacks): это
        оружие, которое игрок выбрал на странице листа для быстрой атаки.
        """
        if not self._attacks:
            return
        index = 1
        for i, entry in enumerate(self._attacks):
            if isinstance(entry, dict) and entry.get("chosen"):
                index = i + 1        # нулевой пункт — «вручную»
                break
        self.preset_combo.setCurrentIndex(index)
        # Если пункт уже был текущим, сигнал не сработал — применяем сами.
        self._apply_preset()

    def _fill_attack_list(self):
        """Заполнить список атак и подставить первую — без ручного ввода."""
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("— вручную —", None)
        for entry in self._attacks:
            label = entry.get("name") or "атака"
            bits = []
            if entry.get("attack_bonus") is not None:
                bits.append(f"{int(entry['attack_bonus']):+d}")
            if entry.get("damage"):
                bits.append(str(entry["damage"]))
            if bits:
                label += "  (" + ", ".join(bits) + ")"
            self.preset_combo.addItem(label, entry)
        self.preset_combo.blockSignals(False)

    def _reload_attacks(self, *_):
        """Атакующий сменился: показываем удары именно этого токена."""
        if not self._attacks_provider:
            return
        token_id = self.attacker_combo.currentData()
        try:
            self._attacks = list(self._attacks_provider(token_id) or [])
        except Exception:
            # Список уже показанных атак не теряем: пустой список выглядел
            # так, будто у токена вообще нет оружия.
            pass
        self._fill_attack_list()
        self._select_preferred()

    def _reload_smite(self, *_):
        """Собрать допустимые круги кары для атакующего и цели.

        Кара возможна только в рукопашной атаке: по дальнобойному оружию
        список пуст, и причина написана в подсказке, а не скрыта.
        """
        if not hasattr(self, "smite_combo"):
            return
        provider = self._smite_provider
        if provider is None:
            # У монстра кары нет: строка не должна занимать место в окне.
            self.smite_combo.setVisible(False)
            self.smite_row_label.setVisible(False)
            return
        self.smite_combo.setVisible(True)
        self.smite_row_label.setVisible(True)
        try:
            info = provider(self.attacker_combo.currentData(),
                            self.target_combo.currentData()) or {}
        except Exception:
            info = {}
        levels = list(info.get("levels") or [])
        extra = bool(info.get("extra"))
        entry = self.preset_combo.currentData()
        ranged = bool(info.get("ranged")) or bool(
            isinstance(entry, dict) and entry.get("ranged"))
        current = self.smite_combo.currentData()
        self.smite_combo.blockSignals(True)
        self.smite_combo.clear()
        self.smite_combo.addItem("— без кары —", 0)
        for entry in levels:
            level = int(entry.get("level") or 0)
            dice = str(entry.get("dice") or "")
            if not level or not dice:
                continue
            label = f"{level} круг — {dice}"
            if extra:
                label += " (+1к8: нежить или исчадие)"
            self.smite_combo.addItem(label, level)
        index = self.smite_combo.findData(current)
        self.smite_combo.setCurrentIndex(index if index >= 0 else 0)
        self.smite_combo.blockSignals(False)
        if ranged:
            reason = "Божественная кара работает только в рукопашной атаке"
            enabled = False
        else:
            reason = str(info.get("reason") or "")
            enabled = bool(levels)
        self.smite_combo.setEnabled(enabled)
        self.smite_combo.setToolTip(
            reason if not enabled
            else "Божественная кара: ячейка тратится только при попадании")

    def select_smite(self, level=1) -> bool:
        """Выбрать круг кары (для кнопки особенности на листе)."""
        if not hasattr(self, "smite_combo"):
            return False
        index = self.smite_combo.findData(int(level or 1))
        if index <= 0:
            index = 1 if self.smite_combo.count() > 1 else 0
        self.smite_combo.setCurrentIndex(index)
        return index > 0

    def _apply_preset(self, *_):
        """Выбор атаки из списка заполняет бонус, формулу урона и его тип."""
        entry = self.preset_combo.currentData()
        # Галочка быстрой атаки — у оружия листа (vtt.quick_weapon) и у удара
        # статблока (statblock.quick_attack). У ручного ввода id нет, поэтому
        # запоминать нечего.
        can_pick = bool(isinstance(entry, dict) and entry.get("quick_selectable")
                        and entry.get("id"))
        self.quick_check.blockSignals(True)
        self.quick_check.setEnabled(can_pick)
        self.quick_check.setChecked(bool(can_pick and entry.get("quick")))
        self.quick_check.setToolTip(
            "Запомнить удар для Q: нажмите Q и кликните по цели"
            if can_pick else
            "Сначала выберите оружие или удар: ручной ввод не запоминается")
        self.quick_check.blockSignals(False)
        if not entry:
            return
        if entry.get("attack_bonus") is not None:
            try:
                self.bonus_spin.setValue(int(entry["attack_bonus"]))
            except (TypeError, ValueError):
                pass
        if entry.get("damage"):
            # Формула могла прийти с типом внутри («1d8+3 колющий»):
            # в поле урона ему не место, тип покажем соседним списком.
            formula, parsed_type = combat_rules.split_damage_text(
                entry["damage"])
            self.damage_edit.setEditText(
                formula if parsed_type else str(entry["damage"]))
        else:
            parsed_type = ""
        # Тип урона меняется вместе с оружием. Нет типа — сбрасываем на
        # «— без типа —»: иначе после огненного меча винтовка без типа
        # продолжала считаться огненной и упиралась в сопротивление цели.
        dtype = combat_rules.normalize_damage_type(
            entry.get("damage_type") or parsed_type)
        index = self.type_combo.findData(dtype) if dtype else 0
        self.type_combo.setCurrentIndex(index if index >= 0 else 0)

    @staticmethod
    def _select(combo, token_id):
        if not token_id:
            return
        index = combo.findData(token_id)
        if index >= 0:
            combo.setCurrentIndex(index)

    def accept(self):
        """ОК: бросок атаки, а если отмечено — ещё и оружие для быстрой атаки.

        ``attackRequested`` нужен карточке актёра: она открывает это окно
        сама и шлёт бросок по сигналу. Главное окно берёт payload после
        ``exec_``, поэтому оно сигнал не слушает и двойного броска нет.
        """
        entry = self.preset_combo.currentData()
        if (self.quick_check.isChecked() and isinstance(entry, dict)
                and entry.get("id") and entry.get("quick_selectable")):
            self.quickAttackChanged.emit(str(self.attacker_combo.currentData()),
                                         str(entry["id"]))
        can_send = self._buttons.button(QDialogButtonBox.Ok).isEnabled()
        super().accept()
        if can_send:
            self.attackRequested.emit(self.payload())

    def payload(self) -> dict:
        entry = self.preset_combo.currentData()
        ranged = False
        if isinstance(entry, dict):
            ranged = bool(entry.get("ranged"))
        smite = 0
        if self.smite_combo.isEnabled():
            try:
                smite = int(self.smite_combo.currentData() or 0)
            except (TypeError, ValueError):
                smite = 0
        # Тип мог быть напечатан в поле урона («1d10+6 колющий»): сервер
        # ждёт чистую формулу и отдельный ключ типа, иначе сопротивление
        # цели не сработает, а бросок урона споткнётся о слово.
        damage, parsed_type = combat_rules.split_damage_text(
            self.damage_edit.currentText())
        return {
            'attacker_id': self.attacker_combo.currentData(),
            'target_id': self.target_combo.currentData(),
            'attack_bonus': self.bonus_spin.value(),
            'damage': damage,
            'damage_type': (self.type_combo.currentData()
                            or parsed_type or ''),
            'advantage': self.adv_check.isChecked(),
            'disadvantage': self.dis_check.isChecked(),
            'ranged': ranged,
            # Название оружия нужно серверу для правил: скрытая атака плута
            # идёт толко фехтовальным или дальнобойным оружием.
            'weapon': (str(entry.get("name") or "")
                       if isinstance(entry, dict) else ""),
            'smite_slot': smite,
            # Характеристика нужна правилам: ярость добавляет урон только
            # к рукопашной атаке Силой, а не к фехтованию Ловкостью.
            'ability': (str(entry.get("ability") or "")
                        if isinstance(entry, dict) else ""),
        }


class SaveDialog(QDialog):
    """Спасбросок токена против Сл.

    Бонус здесь только показывается: сервер берёт его с листа персонажа или
    из статблока существа. Ручное значение нужно для случаев, когда листа нет
    под рукой.
    """

    saveRequested = pyqtSignal(dict)
    #: спасбросок от смерти — те же сигналы, что у листа и карточки
    deathSaveRollRequested = pyqtSignal(str)
    deathSaveSetRequested = pyqtSignal(str, int, str)
    deathReviveRequested = pyqtSignal(str)
    deathStabilizeRequested = pyqtSignal(str)

    def __init__(self, tokens, token_id=None, ability="dex", dc=None,
                 sheet_lookup=None, parent=None, can_revive=False):
        super().__init__(parent)
        self.setWindowTitle("Спасбросок")
        self.setMinimumWidth(460)
        self._tokens = {tok.id: tok for tok in (tokens or [])}
        self._sheet_lookup = sheet_lookup or (lambda tok: None)
        self._can_revive = bool(can_revive)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        form = QFormLayout()
        form.setSpacing(8)
        root.addLayout(form)

        self.token_combo = QComboBox()
        for token in (tokens or []):
            self.token_combo.addItem(_token_label(token), token.id)
        if token_id:
            index = self.token_combo.findData(token_id)
            if index >= 0:
                self.token_combo.setCurrentIndex(index)
        self.token_combo.currentIndexChanged.connect(self._refresh_bonus)
        form.addRow("Кто бросает:", self.token_combo)

        self.ability_combo = QComboBox()
        for key in token_stats.ABILITIES:
            self.ability_combo.addItem(token_stats.ABILITY_LABELS[key], key)
        index = self.ability_combo.findData(ability)
        if index >= 0:
            self.ability_combo.setCurrentIndex(index)
        self.ability_combo.currentIndexChanged.connect(self._refresh_bonus)
        form.addRow("Характеристика:", self.ability_combo)

        self.bonus_spin = QSpinBox()
        self.bonus_spin.setRange(-20, 30)
        form.addRow("Бонус спасброска:", self.bonus_spin)

        self.dc_spin = QSpinBox()
        self.dc_spin.setRange(0, 99)
        self.dc_spin.setValue(int(dc) if dc else 15)
        self.dc_spin.setSpecialValueText("нет Сл")
        form.addRow("Сложность (Сл):", self.dc_spin)

        self.auto_check = QCheckBox("Брать бонус автоматически с листа/статблока")
        self.auto_check.setChecked(True)
        self.auto_check.toggled.connect(self._refresh_bonus)
        form.addRow("", self.auto_check)

        flags = QHBoxLayout()
        self.adv_check = QCheckBox("Преимущество")
        self.dis_check = QCheckBox("Недостаток")
        flags.addWidget(self.adv_check)
        flags.addWidget(self.dis_check)
        form.addRow("", flags)

        # Что состояния делают со спасброском: автопровал или помеха.
        # Иначе мастер не понимал, почему сервер не бросил кости.
        self.effects = QLabel("")
        self.effects.setObjectName("save_effects")
        self.effects.setWordWrap(True)
        form.addRow("", self.effects)

        # На нуле хитов обычный спасбросок — не главное действие: под формой
        # показываем те же кружки смерти, что на листе и на токене. Раньше
        # панель «Спасбросок» умалчивала о них, и мастер искал их в карточке.
        self.death_bar = DeathSavesBar()
        self.death_bar.rollRequested.connect(self.deathSaveRollRequested.emit)
        self.death_bar.setRequested.connect(self.deathSaveSetRequested.emit)
        self.death_bar.reviveRequested.connect(self.deathReviveRequested.emit)
        self.death_bar.stabilizeRequested.connect(
            self.deathStabilizeRequested.emit)
        root.addWidget(self.death_bar)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.token_combo.currentIndexChanged.connect(self._sync_death_bar)
        self.token_combo.currentIndexChanged.connect(self._refresh_bonus)
        self._refresh_bonus()
        self._sync_death_bar()

    def _current_token(self):
        return self._tokens.get(self.token_combo.currentData())

    def _refresh_bonus(self, *_):
        token = self._current_token()
        if token is None:
            return
        ability = self.ability_combo.currentData()
        if self.auto_check.isChecked():
            bonus = token_stats.token_save_bonus(
                token, self._sheet_lookup(token), ability)
            self.bonus_spin.setValue(bonus)
            self.bonus_spin.setEnabled(False)
        else:
            self.bonus_spin.setEnabled(True)
        self._refresh_effects(token, ability)

    def _refresh_effects(self, token, ability):
        """Подсказка о состоянии токена для этого спасброска."""
        conditions = token_stats.token_conditions(
            token, self._sheet_lookup(token))
        if not conditions:
            self.effects.setText("")
            self.effects.setVisible(False)
            return
        mods = conditions_rules.save_modifiers(conditions, ability)
        label = token_stats.ABILITY_LABELS.get(ability, ability)
        if mods["auto_fail"]:
            text = (f"<span style='color:#d08770'>Автопровал спасброска "
                    f"{label}: состояние не даёт двигаться.</span>")
        elif mods["disadvantage"]:
            text = (f"<span style='color:#d9a441'>Помеха на спасбросок "
                    f"{label} (состояние).</span>")
        else:
            text = ""
        summary = conditions_rules.describe(conditions)
        if summary:
            text += ("<br>" if text else "") + f"<span style='color:#8e94a3'>{summary}</span>"
        self.effects.setText(text)
        self.effects.setVisible(bool(text))

    def _sync_death_bar(self, *_):
        """Показать спасброски от смерти, если у выбранного токена 0 хитов."""
        token = self._current_token()
        if token is None:
            self.death_bar.set_token_id(None)
            self.death_bar.setVisible(False)
            return
        self.death_bar.set_token_id(token.id)
        self.death_bar.set_death_state(
            getattr(token, 'death_saves', None), getattr(token, 'hp', None),
            can_edit=True, can_revive=self._can_revive)

    def set_death_state(self, token_id, results, hp):
        """Обновить кружки в открытом окне после ответа сервера."""
        if token_id != self.token_combo.currentData():
            return
        token = self._current_token()
        if token is not None:
            token.death_saves = list(results or [])
            token.hp = hp
        self._sync_death_bar()

    def payload(self) -> dict:
        dc = self.dc_spin.value()
        return {
            'token_id': self.token_combo.currentData(),
            'ability': self.ability_combo.currentData(),
            'dc': dc if dc > 0 else None,
            'bonus': self.bonus_spin.value(),
            'advantage': self.adv_check.isChecked(),
            'disadvantage': self.dis_check.isChecked(),
        }


CONDITIONS_QSS = """
#cond_hint { color: #8e94a3; font-size: 9pt; }
#cond_count { color: #c8a45e; font-weight: 700; }
#cond_rules {
    background: #141722;
    border: 1px solid #2c3244;
    border-radius: 8px;
    padding: 6px 8px;
    color: #d8d6d2;
    font-size: 9pt;
}
#save_effects { color: #b9c0d0; font-size: 9pt; }
QPushButton#cond_toggle {
    text-align: left;
    padding: 7px 10px;
    border: 1px solid #363d50;
    border-radius: 8px;
    background: #191c26;
    color: #d8d6d2;
    font-weight: 500;
}
QPushButton#cond_toggle:hover {
    background: #232838;
    border-color: #4b5368;
}
QPushButton#cond_toggle:checked {
    background: #3a2f18;
    border-color: #c8a45e;
    color: #f3efe6;
}
QPushButton#cond_clear {
    background: transparent;
    border: 1px dashed #4b5368;
    color: #b9c0d0;
    padding: 5px 12px;
}
QPushButton#cond_clear:hover { border-color: #c8a45e; color: #e8e6e0; }
QPushButton#cond_ok {
    background: #c8a45e;
    border: 1px solid #c8a45e;
    color: #17130b;
    font-weight: 700;
    padding: 7px 20px;
}
QPushButton#cond_ok:hover { background: #d8b56e; }
"""


class ConditionsDialog(QDialog):
    """Состояния на токене: те же пятнадцать, что и на листе персонажа.

    Каждое состояние — большая кнопка-переключатель с иконкой. Раньше это
    был QListWidget с чекбоксами, и Qt переключал галочку только по клику
    ровно в квадратик: клик по названию («Отравлен») лишь выделял строку,
    поэтому панель выглядела так, будто ничего не накладывает.
    """

    conditionsChanged = pyqtSignal(dict)

    def __init__(self, token_id, current=None, parent=None, immune=None):
        super().__init__(parent)
        self.setWindowTitle("Состояния")
        self.setMinimumWidth(430)
        self.setStyleSheet(CONDITIONS_QSS)
        self._token_id = token_id
        # Иммунитет токена: такие состояния выключены, как в карточке.
        self._immune = set(conditions_rules.normalize_immunities(immune or []))
        # Активные состояния сравниваем по канону: «Истощён 3» на токене
        # должно отмечать галочку «Истощён», а не выглядеть снятым.
        active = {conditions_rules.canonical(n) for n in (current or [])}
        active.discard(None)
        self._exhaustion_level = max(
            1, conditions_rules.exhaustion_level(current))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(9)
        hint = QLabel(
            "Нажмите на состояние, чтобы включить или снять его. "
            "Отмеченные сразу работают в бою: меняют броски атаки и "
            "спасброски, а недееспособные не действуют и не двигаются. "
            "Иконки появляются на токене сразу.")
        hint.setObjectName("cond_hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._checks = {}
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(7)
        grid.setVerticalSpacing(7)
        for index, name in enumerate(sheet_items.CONDITIONS_RU):
            button = QPushButton(name)
            button.setObjectName("cond_toggle")
            button.setCheckable(True)
            if name in self._immune:
                button.setChecked(False)
                button.setEnabled(False)
                button.setToolTip(
                    f"«{name}»: у существа иммунитет к этому состоянию")
                self._checks[name] = button
                grid.addWidget(button, index // 2, index % 2)
                continue
            button.setChecked(name in active)
            icon = self._condition_icon(name)
            if not icon.isNull():
                button.setIcon(icon)
                button.setIconSize(QSize(18, 18))
            rules = conditions_rules.rules_text(name)
            button.setToolTip(
                f"<b>{name}</b><br>{rules.replace(chr(10), '<br>')}"
                if rules else f"Состояние «{name}»")
            button.toggled.connect(self._update_count)
            button.clicked.connect(
                lambda _checked=False, n=name: self._show_rules(n))
            self._checks[name] = button
            grid.addWidget(button, index // 2, index % 2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(grid_host)
        layout.addWidget(scroll, 1)

        # Что именно делает состояние — прямо в окне: без этого мастер
        # накладывал «Опутан» и не знал, что у токена теперь скорость 0.
        self.rules = QTextBrowser()
        self.rules.setObjectName("cond_rules")
        self.rules.setFixedHeight(94)
        self.rules.setFrameShape(QFrame.NoFrame)
        layout.addWidget(self.rules)

        # Уровень истощения: правила у него ступенчатые (1–6), и один
        # переключатель без уровня не имел бы смысла.
        level_row = QHBoxLayout()
        level_row.setSpacing(6)
        level_text = QLabel("Уровень истощения:")
        level_text.setObjectName("cond_hint")
        level_row.addWidget(level_text)
        self.exhaustion_spin = QSpinBox()
        self.exhaustion_spin.setRange(1, 6)
        self.exhaustion_spin.setValue(self._exhaustion_level)
        self.exhaustion_spin.setToolTip(
            "1 — помеха на проверки; 3 — на атаки и спасброски; "
            "5 — скорость 0; 6 — смерть")
        self.exhaustion_spin.valueChanged.connect(self._on_exhaustion_level)
        level_row.addWidget(self.exhaustion_spin)
        level_row.addStretch(1)
        layout.addLayout(level_row)

        footer = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setObjectName("cond_count")
        footer.addWidget(self.count_label)
        footer.addStretch(1)
        clear = QPushButton("Снять все")
        clear.setObjectName("cond_clear")
        clear.setToolTip("Снять с токена все состояния")
        clear.clicked.connect(self._clear_all)
        footer.addWidget(clear)
        layout.addLayout(footer)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.Ok).setObjectName("cond_ok")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_count()
        # При открытии показываем правила первого уже наложенного состояния:
        # так видно, что «Парализован» — это ещё и крит в упор. Берём из
        # selected(), чтобы у истощения был виден его уровень.
        chosen = self.selected()
        self._show_rules(chosen[0] if chosen else None)

    def _show_rules(self, name):
        """Показать текст правил выбранного состояния."""
        if not name:
            self.rules.setHtml(
                "<span style='color:#8e94a3'>Наведите на состояние или "
                "нажмите его, чтобы прочитать правила.</span>")
            return
        rules = conditions_rules.rules_text(name)
        summary = conditions_rules.CONDITION_SUMMARY.get(
            conditions_rules.canonical(name), "")
        html = f"<b>{name}</b>"
        if summary:
            html += f"<br><i style='color:#8e94a3'>{summary}</i>"
        if rules:
            html += "<br>" + rules.replace("\n", "<br>")
        self.rules.setHtml(html)

    @staticmethod
    def _condition_icon(name):
        """Иконка состояния из набора проекта (ленивый импорт: без цикла)."""
        try:
            from PyQt5.QtGui import QIcon
            from client.game_canvas import CONDITION_ICON_DIR, condition_icon_key
            key = condition_icon_key(name)
            if not key:
                return QIcon()
            path = os.path.join(CONDITION_ICON_DIR, key + ".png")
            return QIcon(path) if os.path.isfile(path) else QIcon()
        except Exception:
            from PyQt5.QtGui import QIcon
            return QIcon()

    def _on_exhaustion_level(self, level):
        """Сменить уровень истощения: состояние остаётся включённым."""
        self._exhaustion_level = max(1, int(level))
        self._update_count()
        if self._checks["Истощён"].isChecked():
            self._show_rules(f"Истощён {self._exhaustion_level}")

    def _clear_all(self):
        for button in self._checks.values():
            button.setChecked(False)

    def _update_count(self, *_):
        chosen = self.selected()
        text = f"Выбрано: {len(chosen)}"
        # Сразу видно, что токен потерял способность действовать или
        # двигаться: это главное следствие состояний в бою.
        can_act, why = conditions_rules.can_act(chosen)
        if not can_act:
            text += f"  ·  не может действовать ({why})"
        elif not conditions_rules.can_move(chosen)[0]:
            text += "  ·  скорость 0"
        self.count_label.setText(text)

    def selected(self) -> list:
        chosen = []
        for name, button in self._checks.items():
            if not button.isChecked():
                continue
            if name == "Истощён" and self._exhaustion_level > 1:
                chosen.append(f"Истощён {self._exhaustion_level}")
            else:
                chosen.append(name)
        return chosen

    def payload(self) -> dict:
        return {'token_id': self._token_id, 'conditions': self.selected()}

