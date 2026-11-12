"""Сгенерировать звуки заклинаний, которых нет в assets/sounds/spells.

Звуки заклинаний в проекте — обычные WAV (mono, 16 бит, 44.1 кГц), имя
файла равно ключу отклика (``haste.wav`` → `fx: "haste"`). Для двух новых
заклинаний файлов не было, и каст проходил молча. Скрипт синтезирует их
процедурно, как ``_make_draw_icons.py`` рисует иконки: без внешних
библиотек и без скачанных ассетов.

  * ``haste.wav`` — короткий взлёт: два глиссандо вверх и воздушный шорох;
  * ``mage_armor.wav`` — тёплый аккорд с металлическим призвуком: пластины
    смыкаются вокруг цели;
  * ``hunters_mark.wav`` — шёпот ветра и два низких колокольчика: метка
    ложится на след;
  * ``divine_smite.wav`` — удар света: металлический звон и восходящий
    аккорд;
  * ``rage.wav`` — ярость: низкий рык с дрожанием, удар квинты и жар
    высокого шума;
  * ``book_page.wav`` (в папке звуков интерфейса, не в ``spells``) —
    шелест страницы: отклик на кнопку «Применить» в книге заклинаний.

Запуск:
    python3 tools/_make_spell_sounds.py
"""
import math
import os
import random
import struct
import wave

RATE = 44100
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "client", "assets", "sounds", "spells")


def _envelope(i: int, total: int, attack: float, decay: float) -> float:
    """Простая огибающая: быстрый вход и экспоненциальный спад."""
    t = i / RATE
    span = total / RATE
    a = min(1.0, t / max(0.001, attack))
    d = math.exp(-max(0.0, t - attack) * decay)
    # Мягкий конец: не даём волне оборваться щелчком.
    tail = min(1.0, max(0.0, (span - t) / 0.05))
    return a * d * tail


def _sine(phase: float) -> float:
    return math.sin(2.0 * math.pi * phase)


def _write(path: str, samples) -> None:
    peak = max(1e-9, max(abs(value) for value in samples))
    scale = 0.82 / peak
    frames = bytearray()
    for value in samples:
        level = int(max(-1.0, min(1.0, value * scale)) * 32767)
        frames += struct.pack("<h", level)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(bytes(frames))


def make_haste(seconds: float = 1.05):
    total = int(RATE * seconds)
    rng = random.Random(7)
    # Псевдошум: сумма несоизмеримых синусов. Прямой random-шум на слух
    # шипит, а такой набор даёт «воздух» без цифровой грязи.
    air_tones = [(rng.uniform(1800.0, 6200.0), rng.uniform(0.0, 1.0))
                 for _ in range(26)]
    out = []
    for i in range(total):
        t = i / RATE
        env = _envelope(i, total, 0.03, 3.4)
        # Два глиссандо: высокий голос стартует позже и обгоняет низкий.
        rise = min(1.0, t / 0.55)
        f1 = 300.0 + 470.0 * (rise ** 1.6)
        f2 = 470.0 + 1010.0 * (rise ** 2.1)
        glide = (_sine(f1 * t) * 0.34 + _sine(f2 * t) * 0.16)
        # Мягкое вибрато придаёт «живость».
        glide *= 0.9 + 0.1 * math.sin(2.0 * math.pi * 6.5 * t)
        air = 0.0
        burst = math.exp(-t * 2.2) * min(1.0, t / 0.08)
        for freq, phase in air_tones:
            air += _sine((freq + 220.0 * t) * t + phase)
        air = air / len(air_tones) * 0.55 * burst
        out.append(glide * env + air * env)
    return out


def make_mage_armor(seconds: float = 1.35):
    total = int(RATE * seconds)
    out = []
    chord = (130.81, 196.0, 261.63, 329.63)
    for i in range(total):
        t = i / RATE
        env = _envelope(i, total, 0.14, 2.4)
        # Тёплый аккорд с медленным биением: «смыкающиеся пластины».
        warm = 0.0
        for index, freq in enumerate(chord):
            detune = 1.0 + 0.0015 * math.sin(2.0 * math.pi * (0.7 + index * 0.13) * t)
            warm += _sine(freq * detune * t) * (0.30 / (index + 1))
        warm *= 0.9 + 0.1 * math.sin(2.0 * math.pi * 5.2 * t)
        # Металлический призвук в начале и второй, тише, — к середине.
        ping = 0.0
        for freq, start, level in ((1046.5, 0.0, 0.20), (1568.0, 0.16, 0.12),
                                   (2093.0, 0.30, 0.07)):
            local = t - start
            if local <= 0:
                continue
            ping += _sine(freq * local) * level * math.exp(-local * 6.5)
        out.append(warm * env + ping)
    return out


def make_hunters_mark(seconds: float = 1.15):
    """Шёпот ветра и два низких колокольчика: метка ложится на след."""
    total = int(RATE * seconds)
    rng = random.Random(11)
    air_tones = [(rng.uniform(900.0, 3400.0), rng.uniform(0.0, 1.0))
                 for _ in range(22)]
    out = []
    for i in range(total):
        t = i / RATE
        env = _envelope(i, total, 0.18, 2.0)
        # Ветер: шумовые тона, медленно сползающие вниз — «тень пробежала».
        air = 0.0
        for freq, phase in air_tones:
            air += _sine((freq - 260.0 * t) * t + phase)
        air = air / len(air_tones) * 0.30
        # Два колокольчика: пятая и октава, второй позже и тише.
        chime = 0.0
        for freq, start, level in ((196.0, 0.02, 0.26),
                                   (293.66, 0.22, 0.16)):
            local = t - start
            if local <= 0:
                continue
            chime += (_sine(freq * local) * level
                      + _sine(freq * 2.01 * local) * level * 0.22) \
                * math.exp(-local * 3.4)
        out.append(air * env + chime)
    return out


def make_divine_smite(seconds: float = 0.95):
    """Удар света: металлический звон и восходящий аккорд."""
    total = int(RATE * seconds)
    out = []
    chord = (261.63, 329.63, 392.0, 523.25)
    for i in range(total):
        t = i / RATE
        env = _envelope(i, total, 0.008, 4.2)
        # Светлый аккорд с быстрым восходом — «небо отозвалось».
        warm = 0.0
        rise = min(1.0, t / 0.22)
        for index, freq in enumerate(chord):
            warm += _sine(freq * (0.94 + 0.06 * rise) * t) * (0.22 / (index + 1))
        # Звон: три высокие частоты с разным затуханием.
        ping = 0.0
        for freq, level, decay in ((1046.5, 0.28, 5.2),
                                   (1568.0, 0.18, 6.4),
                                   (2637.0, 0.10, 8.0)):
            ping += _sine(freq * t) * level * math.exp(-t * decay)
        # Короткий «удар» в самом начале: низкий импульс.
        thump = 0.0
        if t < 0.12:
            thump = _sine(70.0 * t) * 0.35 * math.exp(-t * 26.0)
        out.append(warm * env + ping + thump)
    return out


def make_rage(seconds: float = 1.25):
    """Ярость: низкий рык, восходящая квинта и жар в конце."""
    total = int(RATE * seconds)
    rng = random.Random(19)
    air_tones = [(rng.uniform(300.0, 1600.0), rng.uniform(0.0, 1.0))
                 for _ in range(18)]
    out = []
    for i in range(total):
        t = i / RATE
        env = _envelope(i, total, 0.05, 2.1)
        # Низкий рык: две близкие частоты, которые медленно ползут вверх.
        rise = min(1.0, t / 0.7)
        base = 74.0 * (1.0 + 0.16 * rise)
        growl = (_sine(base * t) * 0.40
                 + _sine(base * 1.5 * t) * 0.20
                 + _sine(base * 2.02 * t) * 0.10)
        # Грубый призвук: амплитудная модуляция на 17 Гц — «дрожание».
        growl *= 0.82 + 0.18 * math.sin(2.0 * math.pi * 17.0 * t)
        # Жар: высокий шумовой слой, разгорающийся к концу.
        heat = 0.0
        for freq, phase in air_tones:
            heat += _sine((freq + 520.0 * t) * t + phase)
        heat = heat / len(air_tones) * 0.16 * min(1.0, t / 0.45)
        # Удар квинты в начале — «кровь прилила».
        stab = 0.0
        if 0.02 < t < 0.42:
            local = t - 0.02
            stab = (_sine(146.83 * local) * 0.16
                    + _sine(220.0 * local) * 0.09) * math.exp(-local * 5.2)
        out.append(growl * env + heat * env + stab)
    return out


def make_book_page(seconds: float = 0.42):
    """Шелест страницы: нажатие «Применить» в книге заклинаний.

    Не заклинание, а звук интерфейса, поэтому файл живёт рядом с ним, а не
    в папке ``spells``: короткий шорох бумаги и мягкий щелчок корешка.
    """
    total = int(RATE * seconds)
    rng = random.Random(23)
    # Бумага — широкий шумовой слой: много несоизмеримых тонов, у каждого
    # своё время появления, поэтому шорох не звучит ровным «пшш».
    tones = [(rng.uniform(1400.0, 7800.0), rng.uniform(0.0, 1.0),
              rng.uniform(0.02, 0.20)) for _ in range(34)]
    out = []
    for i in range(total):
        t = i / RATE
        # Шорох идёт волной: разгон к середине и спад к концу.
        swell = math.sin(math.pi * min(1.0, t / seconds)) ** 1.4
        paper = 0.0
        for freq, phase, start in tones:
            local = t - start
            if local <= 0:
                continue
            paper += _sine((freq - 900.0 * local) * local + phase)
        paper = paper / len(tones) * 0.95 * swell
        # Щелчок корешка в начале — «книга открылась».
        thump = 0.0
        if t < 0.09:
            thump = _sine(120.0 * t) * 0.22 * math.exp(-t * 42.0)
        # Тихий шелест бумаги о стол в конце.
        tail = 0.0
        if t > seconds * 0.62:
            local = t - seconds * 0.62
            tail = _sine(2100.0 * local) * 0.05 * math.exp(-local * 9.0)
        out.append((paper + thump + tail) * _envelope(i, total, 0.01, 0.6))
    return out


def make_lightning_breath(seconds: float = 1.15):
    """Электрическое дыхание: треск разрядов и ниспадающее шипение.

    У Бехира это главная атака, и звук должен отличаться от «молнии»
    заклинания: здесь широкий треск, будто воздух рвётся, а не одиночный
    удар. Основа — резкие шумовые щелчки поверх низкого гула.
    """
    total = int(RATE * seconds)
    rng = random.Random(31)
    crackle = [(rng.uniform(0.0, seconds), rng.uniform(1200.0, 7200.0),
                rng.uniform(0.10, 0.32)) for _ in range(46)]
    out = []
    for i in range(total):
        t = i / RATE
        env = _envelope(i, total, 0.02, 2.6)
        # Гул разряда: низкая пила, медленно сползающая вниз.
        hum = _sine((128.0 - 40.0 * t) * t) * 0.22
        # Треск: короткие вспышки на разных частотах.
        snap = 0.0
        for start, freq, level in crackle:
            local = t - start
            if local <= 0 or local > 0.09:
                continue
            snap += _sine(freq * local) * level * math.exp(-local * 46.0)
        # Шипение воздуха уходит к концу.
        hiss = _sine((3600.0 - 1400.0 * t) * t) * 0.05 * min(1.0, t / 0.1)
        out.append((hum + snap * 0.7 + hiss) * env)
    return out


def make_acid_breath(seconds: float = 0.95):
    """Кислота и проглатывание: влажное бурление и глухой удар.

    Звук для кислотного дыхания и проглатывания: пузыри и чавканье вместо
    сухого треска, чтобы за столом слышали, что это другая способность.
    """
    total = int(RATE * seconds)
    rng = random.Random(37)
    bubbles = [(rng.uniform(0.0, seconds), rng.uniform(90.0, 420.0),
                rng.uniform(0.10, 0.26)) for _ in range(28)]
    out = []
    for i in range(total):
        t = i / RATE
        env = _envelope(i, total, 0.06, 2.2)
        # Глухой удар в начале — кусок проглочен.
        thud = 0.0
        if t < 0.18:
            thud = _sine(64.0 * t) * 0.36 * math.exp(-t * 18.0)
        # Пузыри: короткие «блёп» с поднимающейся частотой.
        pop = 0.0
        for start, freq, level in bubbles:
            local = t - start
            if local <= 0 or local > 0.14:
                continue
            pop += (_sine((freq + 320.0 * local) * local) * level
                    * math.exp(-local * 22.0))
        # Влажный шорох фона.
        wet = _sine(170.0 * t) * 0.06 * (0.6 + 0.4 * math.sin(2 * math.pi * 7.0 * t))
        out.append((thud + pop * 0.8 + wet) * env)
    return out


def make_frightful_presence(seconds: float = 1.4):
    """Ужасающее присутствие: диссонирующий низкий гул и дрожь."""
    total = int(RATE * seconds)
    out = []
    for i in range(total):
        t = i / RATE
        env = _envelope(i, total, 0.35, 1.5)
        # Тритонус: интервал, который звучит тревожно без объяснений.
        drone = (_sine(58.0 * t) * 0.30 + _sine(82.0 * t) * 0.22
                 + _sine(116.0 * t) * 0.10)
        drone *= 0.78 + 0.22 * math.sin(2 * math.pi * 4.3 * t)
        # Поднимающийся шёпот: страх нарастает к концу.
        whisper = _sine((520.0 + 260.0 * t) * t) * 0.05 * min(1.0, t / 0.7)
        out.append((drone + whisper) * env)
    return out


def make_special_attack(seconds: float = 0.75):
    """Общий отклик особой атаки без своего звука: выдох и удар.

    Лучше общий звук, чем тишина: игрок должен слышать, что способность
    вообще сработала, даже если для неё ещё нет отдельного файла.
    """
    total = int(RATE * seconds)
    out = []
    for i in range(total):
        t = i / RATE
        env = _envelope(i, total, 0.02, 2.8)
        whoosh = _sine((900.0 - 620.0 * t) * t) * 0.18 * min(1.0, t / 0.08)
        thud = _sine(88.0 * t) * 0.30 * math.exp(-t * 14.0)
        out.append((whoosh + thud) * env)
    return out


# Дверь здесь не синтезируется вовсе: `door_open.wav` и `door_close.wav` —
# готовые записи в assets/sounds (источник и лицензия — в SOURCES.md рядом).
# Генератор их не трогает: один раз перезаписав скрип, он терял подобранный
# звук при каждой пересборке. `make_door_close` оставлен только как запасной
# вариант на случай, если запись пропадёт.


def make_door_close(seconds: float = 0.5):
    """Дверь закрывается: глухой удар и щелчок защёлки."""
    total = int(RATE * seconds)
    out = []
    for i in range(total):
        t = i / RATE
        env = _envelope(i, total, 0.02, 3.2)
        thud = _sine(72.0 * t) * 0.42 * math.exp(-t * 16.0)
        # Защёлка — короткий металлический щелчок чуть позже удара.
        click_t = max(0.0, t - 0.09)
        click = ((_sine(1800.0 * click_t) * 0.10
                  + _sine(2600.0 * click_t) * 0.06)
                 * math.exp(-click_t * 60.0) if t >= 0.09 else 0.0)
        out.append((thud + click) * env)
    return out


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, samples in (("haste.wav", make_haste()),
                          ("mage_armor.wav", make_mage_armor()),
                          ("hunters_mark.wav", make_hunters_mark()),
                          ("divine_smite.wav", make_divine_smite()),
                          ("lightning_breath.wav", make_lightning_breath()),
                          ("acid_breath.wav", make_acid_breath()),
                          ("swallow.wav", make_acid_breath(0.8)),
                          ("frightful_presence.wav",
                           make_frightful_presence()),
                          ("special_attack.wav", make_special_attack()),
                          ("rage.wav", make_rage())):
        path = os.path.join(OUT_DIR, name)
        _write(path, samples)
        print(f"{name}: {os.path.getsize(path)} байт")
    # Звук интерфейса лежит на уровень выше, рядом с heal.wav: он не
    # относится ни к одному заклинанию.
    ui_dir = os.path.dirname(OUT_DIR)
    ui_path = os.path.join(ui_dir, "book_page.wav")
    _write(ui_path, make_book_page())
    print(f"book_page.wav: {os.path.getsize(ui_path)} байт")


if __name__ == "__main__":
    main()
