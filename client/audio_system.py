"""
Аудиосистема: независимые музыкальный поток и каналы звуковых эффектов.

Реализовано только на pygame.mixer, без QtMultimedia — это убирает
зависимость от libpulse (из-за которой приложение падало при запуске)
и упрощает сборку в один .exe.

Музыка идёт отдельным потоком (pygame.mixer.music), поэтому запуск
звукового эффекта НЕ останавливает музыку — они звучат одновременно.
Громкость музыки и громкость эффектов настраиваются независимо и
у каждого игрока свои значения.
"""
import os
import time
import logging
from typing import Dict, List, Optional
from PyQt5.QtCore import QObject, pyqtSignal, QTimer


class AudioSystem(QObject):
    """Аудиоплеер: музыка + множество одновременных звуков"""

    music_track_changed = pyqtSignal(str)   # имя текущего трека
    music_error = pyqtSignal(str)
    music_stopped = pyqtSignal()            # плейлист закончился (без повтора)

    def __init__(self):
        super().__init__()

        # last_error объявляем ДО try: раньше он затирался ниже пустой строкой,
        # поэтому настоящая причина сбоя терялась.
        self.last_error = ""
        self.available = False
        try:
            import pygame
            pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=1024)
            pygame.mixer.init()
            # mixer.init() может не бросить исключение, а просто не поднять устройство
            if pygame.mixer.get_init() is None:
                raise RuntimeError("звуковое устройство недоступно (mixer не инициализирован)")
            # 16 каналов: хватит на несколько эффектов одновременно
            pygame.mixer.set_num_channels(16)
            self.pygame = pygame
            self.available = True
        except Exception as e:
            self.last_error = f"аудио недоступно: {e}"
            logging.error(f"Не удалось инициализировать аудио: {e}")

        # Звуковые эффекты: id -> Sound
        self.sounds: Dict[str, object] = {}
        # id -> канал, на котором эффект играет прямо сейчас. Нужен, чтобы
        # повторный запуск того же звука ПЕРЕзапускал его, а не накладывался
        # сам на себя.
        self._sound_channels: Dict[str, object] = {}
        # Музыкальный плейлист: список путей
        self.playlist: List[str] = []
        self.playlist_names: List[str] = []
        self.current_index = 0

        self.music_volume = 0.7
        self.sound_volume = 0.7
        self.music_playing = False
        self.repeat_playlist = True   # повтор по умолчанию включён

        # Позиция воспроизведения. pygame.mixer.music.get_pos() считает время
        # от момента play() и НЕ учитывает перемотку, поэтому считаем сами:
        # _seek_offset — докуда перемотали, _play_started_at — когда стартовали.
        self._seek_offset = 0.0
        self._play_started_at = None
        self._duration = 0.0

        # Периодическая проверка окончания трека для авто-перехода
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(500)
        self._watchdog.timeout.connect(self._check_track_end)

    # ------------------------------------------------------------------
    # Звуковые эффекты
    # ------------------------------------------------------------------
    def load_sound(self, file_path: str, sound_id: str, name: str = None) -> bool:
        """Загрузить звуковой эффект.

        False означает, что звук НЕ загружен, причина лежит в last_error.
        Вызывающий код обязан проверить результат: иначе кнопка «Играть»
        молча ничего не сделает.
        """
        label = name or os.path.basename(file_path)
        if not self.available:
            self.last_error = (f"аудио недоступно ({self.last_error or 'нет звукового устройства'}), "
                               f"эффект «{label}» не загружен")
            logging.error(self.last_error)
            return False
        if not os.path.exists(file_path):
            self.last_error = f"файл эффекта не найден: {file_path}"
            logging.error(self.last_error)
            return False
        try:
            sound = self._decode_sound(file_path)
            sound.set_volume(self.sound_volume)
            self.sounds[sound_id] = sound
            return True
        except Exception as e:
            self.last_error = (f"не удалось декодировать «{label}»: {e}. "
                               f"Для эффектов поддерживаются WAV/OGG/FLAC")
            logging.error(f"Ошибка загрузки звука {file_path}: {e}")
            return False

    def _decode_sound(self, file_path: str):
        """Декодировать файл в pygame.Sound.

        mixer.Sound читает WAV/OGG/FLAC, но НЕ читает mp3. Раньше mp3-эффект
        просто молча не загружался. Теперь для mp3 есть запасной путь: через
        ffmpeg конвертируем во временный WAV — если ffmpeg есть в системе.
        """
        try:
            return self.pygame.mixer.Sound(file_path)
        except Exception:
            if not file_path.lower().endswith('.mp3'):
                raise
        converted = self._mp3_to_wav(file_path)
        if not converted:
            raise RuntimeError(
                "mp3 напрямую не декодируется, а ffmpeg для конвертации не найден")
        return self.pygame.mixer.Sound(converted)

    def _mp3_to_wav(self, file_path: str) -> Optional[str]:
        """Сконвертировать mp3 во временный wav. None, если ffmpeg недоступен.

        Два правила, без которых это место портило игру:

        * **Готовый wav переиспользуется.** Раньше ffmpeg запускался на
          каждую загрузку того же файла (переподключение, повторное
          проигрывание) — интерфейс замирал на всё время конвертации.
        * **Консоль не показывается.** Из оконной сборки (без своей
          консоли) subprocess по умолчанию открывал окно ``ffmpeg.exe`` на
          каждый файл — со стороны это выглядело как «всплывающие окошки»
          в бою и при открытии карточек.
        """
        import shutil
        import tempfile
        import subprocess
        out = os.path.join(tempfile.gettempdir(),
                           f"vtt_sfx_{abs(hash(os.path.abspath(file_path)))}.wav")
        try:
            if os.path.getsize(out) > 0:
                return out
        except OSError:
            pass
        if not shutil.which('ffmpeg'):
            return None
        # Окно консоли ffmpeg не нужно ни игроку, ни мастеру — только
        # готовый файл. CREATE_NO_WINDOW есть только в Windows.
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        kwargs = {'stdout': subprocess.DEVNULL,
                  'stderr': subprocess.DEVNULL,
                  'stdin': subprocess.DEVNULL}
        if flags:
            kwargs['creationflags'] = flags
        try:
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error',
                            '-i', file_path, out], check=True, timeout=30,
                           **kwargs)
            return out if os.path.exists(out) else None
        except Exception as e:
            logging.error(f"ffmpeg не смог сконвертировать {file_path}: {e}")
            # Недоделанный файл нельзя оставлять: следующий вызов принял бы
            # его за готовый кэш и отдал pygame битым.
            try:
                if os.path.exists(out):
                    os.remove(out)
            except OSError:
                pass
            return None

    def remove_sound(self, sound_id: str):
        """Остановить и выгрузить эффект.

        Сначала глушим канал: иначе уже играющий звук продолжал бы звучать
        даже после того, как его удалили из списка.
        """
        channel = self._sound_channels.pop(sound_id, None)
        if channel is not None:
            try:
                channel.stop()
            except Exception:
                pass
        self.sounds.pop(sound_id, None)

    def play_sound(self, sound_id: str, volume: Optional[float] = None) -> bool:
        """Проиграть эффект РОВНО ОДИН раз, не затрагивая музыку.

        loops=0 — ключевое: звук не зацикливается и не встаёт в очередь.
        Повторное нажатие по тому же эффекту перезапускает его с начала:
        сначала глушим предыдущий канал, иначе два нажатия наложились бы
        друг на друга и звук «двоился».
        """
        if not self.available:
            return False
        sound = self.sounds.get(sound_id)
        if not sound:
            return False
        vol = self.sound_volume if volume is None else volume
        sound.set_volume(max(0.0, min(1.0, vol)))
        old = self._sound_channels.get(sound_id)
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        channel = sound.play(loops=0)
        self._sound_channels[sound_id] = channel
        return True

    def set_sound_volume(self, volume: float):
        """Своя громкость эффектов у каждого пользователя"""
        self.sound_volume = max(0.0, min(1.0, volume))
        for sound in self.sounds.values():
            sound.set_volume(self.sound_volume)

    # ------------------------------------------------------------------
    # Музыка и плейлист
    # ------------------------------------------------------------------
    def add_music(self, file_path: str, name: str = None) -> bool:
        """Добавить трек в плейлист"""
        if not os.path.exists(file_path):
            return False
        self.playlist.append(file_path)
        self.playlist_names.append(name or os.path.basename(file_path))
        return True

    def remove_music(self, index: int):
        """Удалить трек из плейлиста"""
        if 0 <= index < len(self.playlist):
            was_current = (index == self.current_index)
            self.playlist.pop(index)
            self.playlist_names.pop(index)
            if index < self.current_index:
                self.current_index -= 1
            if self.current_index >= len(self.playlist):
                self.current_index = max(0, len(self.playlist) - 1)
            if not self.playlist:
                self.stop_music()
            # если удалили играющий трек — перезапустить следующий
            if was_current and self.music_playing and self.playlist:
                self.play_music(self.current_index)

    def move_music(self, from_index: int, to_index: int):
        """Переместить трек в очереди"""
        if not (0 <= from_index < len(self.playlist)):
            return
        to_index = max(0, min(len(self.playlist) - 1, to_index))
        path = self.playlist.pop(from_index)
        name = self.playlist_names.pop(from_index)
        self.playlist.insert(to_index, path)
        self.playlist_names.insert(to_index, name)
        if self.current_index == from_index:
            self.current_index = to_index
        elif from_index < self.current_index <= to_index:
            self.current_index -= 1
        elif to_index <= self.current_index < from_index:
            self.current_index += 1

    def clear_playlist(self):
        self.stop_music()
        self.playlist.clear()
        self.playlist_names.clear()
        self.current_index = 0

    def play_music(self, index: Optional[int] = None):
        """Начать воспроизведение музыки с указанного индекса (или текущего)"""
        if not self.available or not self.playlist:
            self.music_error.emit(self.last_error or "Нет доступных треков или звукового устройства")
            return False
        if index is not None:
            if index != self.current_index:
                self._seek_offset = 0.0   # другой трек — начинаем с начала
            self.current_index = max(0, min(len(self.playlist) - 1, index))

        try:
            path = self.playlist[self.current_index]
            self.pygame.mixer.music.load(path)
            self.pygame.mixer.music.set_volume(self.music_volume)
            start_at = self._seek_offset if self._seek_offset > 0 else 0.0
            if start_at > 0:
                # start= поддержан для mp3/ogg; для wav pygame поднимет исключение,
                # тогда просто играем с начала (кнопка перемотки станет неактивной).
                try:
                    self.pygame.mixer.music.play(start=start_at)
                    self.seek_supported = True
                except Exception:
                    self.pygame.mixer.music.play()
                    self._seek_offset = 0.0
                    self.seek_supported = False
            else:
                self.pygame.mixer.music.play()
                self.seek_supported = True
            self.music_playing = True
            self._play_started_at = time.monotonic() - self._seek_offset
            self._duration = self._measure_duration(path)
            self._watchdog.start()
            self.music_track_changed.emit(self.playlist_names[self.current_index])
            return True
        except Exception as e:
            self.music_playing = False
            self._play_started_at = None
            self._watchdog.stop()
            self.music_error.emit(str(e))
            logging.error(f"Ошибка воспроизведения музыки: {e}")
            return False

    def stop_music(self):
        """Остановить музыку (эффекты продолжают играть)"""
        if self.available:
            self.pygame.mixer.music.stop()
        self.music_playing = False
        self._watchdog.stop()

    def next_track(self):
        """Следующий трек (с зацикливанием если повтор включён)"""
        if not self.playlist:
            return
        next_index = (self.current_index + 1) % len(self.playlist)
        # Если повтор выключен и дошли до конца — остановиться
        if not self.repeat_playlist and next_index == 0:
            self.stop_music()
            self.music_stopped.emit()
            return
        self.current_index = next_index
        self.play_music(self.current_index)

    def set_music_volume(self, volume: float):
        """Своя громкость музыки у каждого пользователя"""
        self.music_volume = max(0.0, min(1.0, volume))
        if self.available:
            self.pygame.mixer.music.set_volume(self.music_volume)

    def set_repeat(self, enabled: bool):
        """Включить/выключить повтор плейлиста"""
        self.repeat_playlist = enabled

    def get_position(self) -> float:
        """Текущая позиция воспроизведения в секундах.

        pygame.mixer.music.get_pos() считает время от последнего play() и не
        учитывает перемотку, поэтому при перемотке считаем от _play_started_at.
        """
        if self._play_started_at is None:
            return self._seek_offset
        return max(0.0, min(self.get_duration() or 1e9,
                            self._seek_offset + (time.monotonic() - self._play_started_at)))

    def get_duration(self) -> float:
        """Длительность текущего трека в секундах (0, если неизвестна)"""
        return self._duration

    def _measure_duration(self, path: str) -> float:
        """Определить длительность файла.

        Сначала пробуем прочитать как Sound (работает для wav/ogg). Для mp3
        этот способ часто недоступен, поэтому есть запасной вариант через
        mutagen, если он установлен.
        """
        try:
            sound = self.pygame.mixer.Sound(path)
            length = float(sound.get_length())
            if length > 0:
                return length
        except Exception:
            pass
        try:
            from mutagen import File as MutagenFile
            audio = MutagenFile(path)
            if audio is not None and audio.info is not None:
                return float(audio.info.length)
        except Exception:
            pass
        return 0.0

    def seek_music(self, seconds: float) -> bool:
        """Перемотать текущий трек на указанную секунду.

        Возвращает False, если формат перемотку не поддерживает — интерфейс
        в этом случае показывает подсказку, а не молча ничего не делает.
        """
        if not self.available or not self.playlist or not self.music_playing:
            return False
        seconds = max(0.0, float(seconds))
        duration = self.get_duration()
        if duration > 0:
            seconds = min(seconds, max(0.0, duration - 0.5))
        try:
            # set_pos поддерживается для mp3/ogg, для wav поднимет исключение
            self.pygame.mixer.music.set_pos(seconds)
            self._seek_offset = seconds
            self._play_started_at = time.monotonic()
            self.seek_supported = True
            return True
        except Exception:
            # Фолбэк: перезапускаем трек с нужного места
            try:
                path = self.playlist[self.current_index]
                self.pygame.mixer.music.load(path)
                self.pygame.mixer.music.set_volume(self.music_volume)
                self.pygame.mixer.music.play(start=seconds)
                self._seek_offset = seconds
                self._play_started_at = time.monotonic()
                self.seek_supported = True
                return True
            except Exception as e:
                self.seek_supported = False
                logging.warning(f"Перемотка недоступна для этого файла: {e}")
                return False

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------
    def _check_track_end(self):
        """Автоматически включать следующий трек когда текущий закончился"""
        if not self.available or not self.music_playing:
            return
        if not self.pygame.mixer.music.get_busy():
            if self.playlist:
                self.next_track()
            else:
                self.music_playing = False
                self._watchdog.stop()
