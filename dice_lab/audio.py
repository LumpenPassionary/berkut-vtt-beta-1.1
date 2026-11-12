"""Звук ударов кубиков: синтеза нет, играются присланные записи.

Pygame mixer — тот же звуковой слой, что уже используется в основном
приложении стола. Если аудиоустройства нет, модуль тихо отключается:
бросок и результат продолжают работать.

События приходят из физики как скачки скорости тел после шага Bullet.
Это дешёвый аналог contact force events Rapier: для удара достаточно
резкого изменения скорости, а не точной силы контакта.
"""
import math
import os
import random
import sys
import time
from array import array
from dataclasses import dataclass

os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')

SOUND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'assets', 'sounds')

_IMPACT_SOUND_FILES = {
    'single': 'single_die.mp3',        # один кубик по столу
    'tumble': 'dice_tumble.mp3',       # несколько кубиков, вариант 2
    'fall': 'dice_fall.mp3',           # шум падающих костей
    'floor': 'dice_floor.mp3',         # тяжёлое падение на пол
}

#: Все варианты критов. Ведущие нули в именах — это всё та же
#: натуральная 1: «1», «01», «001», «00001».
_CRIT_SOUND_FILES = {
    'crit_fail': 'crit_fail.mp3',
    'crit_success': 'crit_success.mp3',
    'crit_fail_1_10': 'crit_fail_1_10.mp3',
    'crit_fail_01_10': 'crit_fail_01_10.mp3',
    'crit_fail_001_10': 'crit_fail_001_10.mp3',
    'crit_fail_00001_10': 'crit_fail_00001_10.mp3',
    'crit_fail_fahhh_10': 'crit_fail_fahhh_10.mp3',
    'crit_fail_1_2': 'crit_fail_1_2.mp3',
    'crit_fail_01_2': 'crit_fail_01_2.mp3',
    'crit_success_20_2': 'crit_success_20_2.mp3',
    'crit_success_20_30': 'crit_success_20_30.mp3',
}

SOUND_FILES = {**_IMPACT_SOUND_FILES, **_CRIT_SOUND_FILES}

#: Треки, у которых для анимации нужен ритм: сетка ударов считается
#: из самой записи, чтобы цифра 20 танцевала именно под этот бит.
RHYTHM_SOUNDS = ('crit_success_20_2',)
BEAT_WINDOW_SECONDS = 0.010
BEAT_MIN_GAP = 0.28

#: Все варианты крита играют с одинаковой вероятностью.
#: Раньше редкие записи включались с шансом 2–30%, и за вечер можно было
#: ни разу не услышать «фахх» или танец. Теперь выбор равномерный: у
#: провала восемь записей (по 12.5%), у успеха три (по 33%).
CRIT_FAIL_KEYS = (
    'crit_fail',
    'crit_fail_1_10', 'crit_fail_01_10', 'crit_fail_001_10',
    'crit_fail_00001_10', 'crit_fail_fahhh_10',
    'crit_fail_1_2', 'crit_fail_01_2',
)
CRIT_SUCCESS_KEYS = ('crit_success', 'crit_success_20_30',
                     'crit_success_20_2')

_IMPACT_COOLDOWN = 0.06             # на один кубик
_MAX_IMPACTS_PER_BATCH = 3


@dataclass(frozen=True)
class CriticalCue:
    kind: str
    key: str
    special: bool
    duration: float
    beats: tuple = ()


def detect_beats(sound, frequency=44100, channels=2):
    """Времена ударов записи в секундах.

    Запись уже загружена в mixer, поэтому читаем её сырые сэмплы: окна по
    10 мс, громкость окна, прирост громкости. Удар — локальный максимум
    прироста выше среднего. Сторонние библиотеки не нужны (только array),
    numpy для этого не обязателен.
    """
    if sound is None or frequency <= 0 or channels <= 0:
        return ()
    try:
        raw = sound.get_raw()
    except Exception:
        return ()
    if not raw:
        return ()
    samples = array('h')
    samples.frombytes(raw[:len(raw) - len(raw) % 2])
    if sys.byteorder == 'big':
        samples.byteswap()
    hop = max(1, int(round(frequency * BEAT_WINDOW_SECONDS)))
    stride = hop * channels
    count = len(samples) // stride
    if count < 4:
        return ()
    envelope = []
    for block in range(count):
        start = block * stride
        total = 0
        for index in range(start, start + stride):
            sample = samples[index]
            total += sample * sample
        envelope.append(math.sqrt(total / stride))
    onset = [0.0] * count
    for index in range(1, count):
        onset[index] = max(0.0, envelope[index] - envelope[index - 1])
    smoothed = [
        (onset[max(0, index - 1)] + onset[index]
         + onset[min(count - 1, index + 1)]) / 3.0
        for index in range(count)
    ]
    mean = sum(smoothed) / count
    spread = math.sqrt(sum((value - mean) ** 2
                           for value in smoothed) / count)
    threshold = mean + 0.8 * spread
    peaks = []
    for index in range(1, count - 1):
        value = smoothed[index]
        if (value >= smoothed[index - 1] and value > smoothed[index + 1]
                and value > threshold):
            peaks.append((index * hop / float(frequency), value))
    beats = []
    for moment, value in peaks:
        if not beats or moment - beats[-1][0] >= BEAT_MIN_GAP:
            beats.append((moment, value))
        elif value > beats[-1][1]:
            beats[-1] = (moment, value)
    return tuple(round(moment, 3) for moment, _value in beats)


class DiceAudio:
    def __init__(self, volume=0.65):
        self.enabled = True
        self.volume = max(0.0, min(1.0, volume))
        self.available = False
        self._pygame = None
        self._sounds = {}
        self._beats = {}
        self._last_impact = {}
        self.rng = random.Random()
        self._load()

    def _load(self):
        try:
            import pygame

            if pygame.mixer.get_init() is None:
                pygame.mixer.init(frequency=44100, size=-16, channels=2,
                                  buffer=512)
            pygame.mixer.set_num_channels(24)
            for key, filename in SOUND_FILES.items():
                path = os.path.join(SOUND_DIR, filename)
                if os.path.isfile(path):
                    self._sounds[key] = pygame.mixer.Sound(path)
            if self._sounds:
                self._pygame = pygame
                self.available = True
                # Сетку ударов считаем один раз при загрузке: во время
                # броска разбор записи задержал бы старт анимации.
                info = pygame.mixer.get_init() or (44100, -16, 2)
                if info[1] == -16:      # detect_beats читает int16
                    for key in RHYTHM_SOUNDS:
                        if key in self._sounds:
                            self._beats[key] = detect_beats(
                                self._sounds[key], info[0], info[2])
        except Exception:
            self.available = False

    # --- настройки ----------------------------------------------------
    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if not self.enabled and self._pygame is not None:
            self._pygame.mixer.stop()

    def set_volume(self, volume):
        self.volume = max(0.0, min(1.0, float(volume)))

    # --- воспроизведение ---------------------------------------------
    def _play(self, key, volume):
        if not self.enabled or not self.available:
            return
        sound = self._sounds.get(key)
        if sound is None:
            return
        channel = sound.play()
        if channel is not None:
            channel.set_volume(max(0.0, min(1.0, volume * self.volume)))

    def start_throw(self, count):
        """Короткий шум броска для набора из нескольких кубиков."""
        if count > 1:
            self._play('tumble' if count <= 10 else 'fall', 0.42)
        self._last_impact.clear()

    def play_critical(self, kind):
        """Проиграть крит и вернуть информацию для анимации.

        Все записи крита равновероятны: редкий вариант заменяет обычный
        звук, а не накладывается на него. Особую длинную анимацию даёт
        только трек с танцем (crit_success_20_2).
        """
        if kind == 'success':
            key = self.rng.choice(CRIT_SUCCESS_KEYS)
            # Длинный танец есть только у записи с ритмом; остальные
            # криты успеха звучат с обычной короткой анимацией.
            special = key in RHYTHM_SOUNDS
            volume = 0.95 if special else 0.9
        else:
            key = self.rng.choice(CRIT_FAIL_KEYS)
            special = key != 'crit_fail'
            volume = 0.95 if special else 0.9
        self._play(key, volume)
        duration = self._duration(key)
        if special and duration <= 0:
            duration = 6.0
        return CriticalCue(kind=kind, key=key, special=special,
                           duration=duration,
                           beats=self.beats_for(key))

    def beats_for(self, key):
        """Сетка ударов трека; пусто, если у записи нет ритма."""
        return self._beats.get(key, ())

    def _duration(self, key):
        sound = self._sounds.get(key)
        if sound is None:
            return 0.0
        try:
            return float(sound.get_length())
        except Exception:
            return 0.0

    def play_impacts(self, impacts, dice_count=1):
        if not self.enabled or not self.available or not impacts:
            return
        now = time.monotonic()
        accepted = []
        for impact in sorted(impacts, key=lambda item: item.speed,
                             reverse=True):
            last = self._last_impact.get(impact.die, -10.0)
            if now - last < _IMPACT_COOLDOWN:
                continue
            self._last_impact[impact.die] = now
            accepted.append(impact)
            if len(accepted) >= _MAX_IMPACTS_PER_BATCH:
                break
        for impact in accepted:
            loudness = 0.22 + min(1.0, impact.speed / 9.0) * 0.68
            if impact.surface == 'floor' and impact.speed > 5.0:
                key = 'floor' if dice_count > 2 else 'single'
            elif impact.speed > 7.0:
                key = 'fall'
            else:
                key = 'single'
            self._play(key, loudness)

    def close(self):
        if self._pygame is not None:
            try:
                self._pygame.mixer.stop()
            except Exception:
                pass
        self.available = False
