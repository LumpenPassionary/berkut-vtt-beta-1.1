"""
Network client for D&D Virtual Tabletop
"""
import socket
import threading
import logging
import queue
import time
from shared import asset_transport as at
from shared.assets import get_cache_dir
from typing import Optional, Callable
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.protocol import Protocol, MessageType


class NetworkClient:
    """Network client handler"""
    
    def __init__(self):
        self.socket: Optional[socket.socket] = None
        self.connected = False
        self.running = False
        self.message_callback: Optional[Callable] = None
        self.send_lock = threading.Lock()
        self.transfer_token = None
        self.transfer_ready = threading.Event()
        self.uploads = queue.Queue(maxsize=128)
        self.downloads = queue.Queue(maxsize=512)
        self.address = None
        self.cache_root = get_cache_dir()
        # Сетевые потоки обязаны уметь останавливаться. Иначе при закрытии
        # окна рабочий поток ещё держит колбэк окна и может вызвать его после
        # удаления Qt-объекта — это не Python-исключение, а падение в sip.
        self._callback_lock = threading.Lock()
        self._stopping = threading.Event()
        self._worker_threads = []
        self._receive_thread = None
        self._start_workers()
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        
    def _start_workers(self):
        """Запустить фоновые загрузку/выгрузку, если они ещё не живут."""
        if any(t.is_alive() for t in self._worker_threads):
            return
        self._stopping.clear()
        self._worker_threads = []
        for target, name in ((self._upload_worker, 'vtt-upload'),
                             (self._download_worker, 'vtt-download')):
            thread = threading.Thread(target=target, daemon=True, name=name)
            self._worker_threads.append(thread)
            thread.start()

    def connect(self, host: str, port: int) -> bool:
        """Connect to server"""
        try:
            self._start_workers()
            # Новое соединение получает новый transfer_token из FULL_STATE.
            # Старый не должен разрешить загрузку до рукопожатия.
            self.transfer_ready.clear()
            self.transfer_token = None
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((host, port))
            self.address = (host, port)
            self.connected = True
            self.running = True
            
            # Start receive thread
            self._receive_thread = threading.Thread(
                target=self.receive_messages, daemon=True, name='vtt-receive')
            self._receive_thread.start()
            
            logging.info(f"Connected to {host}:{port}")
            return True
            
        except Exception as e:
            logging.error(f"Connection failed: {e}")
            return False

    def _wake_workers(self):
        """Разбудить рабочих, спящих на очереди или на transfer_ready."""
        self.transfer_ready.set()

    def _join_threads(self, timeout: float):
        """Подождать сетевые потоки, но не подвешивать интерфейс."""
        current = threading.current_thread()
        threads = [t for t in (self._worker_threads + [self._receive_thread])
                   if t is not None and t is not current and t.is_alive()]
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def disconnect(self):
        """Disconnect from server and stop background workers."""
        self.running = False
        self.connected = False
        # Будим upload/download: они не должны висеть на очереди после
        # разрыва. Сами потоки выйдут по _stopping.
        self._stopping.set()
        self._wake_workers()
        sock = self.socket
        self.socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass          # соединение уже разорвано — это нормально
            try:
                sock.close()
            except OSError:
                pass
        self._join_threads(0.5)
        logging.info("Disconnected")

    def shutdown(self):
        """Полная остановка перед закрытием окна.

        Сначала снимаем колбэк окна, потом будим и дожидаемся потоки.
        Без этого поздний ASSET_READY или ERROR мог прийти в уже удалённый
        MainWindow и уронить процесс в sip.cp313-win_amd64.pyd.
        """
        with self._callback_lock:
            self.message_callback = None
        self._stopping.set()
        self.running = False
        self.connected = False
        self._wake_workers()
        sock = self.socket
        self.socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._join_threads(1.0)
        
    def send_message(self, msg_type: MessageType, data: dict):
        """Send message to server"""
        if not self.connected:
            return
            
        if data.get('asset_upload'):
            pending = dict(data)
            try:
                self.uploads.put_nowait((msg_type, pending))
            except queue.Full:
                self._error('Upload queue is full')
            return
        try:
            message = Protocol.encode_message(msg_type, data)
            # Сервер принимает команды не больше 4 МБ (защита его памяти).
            # Лучше сказать об этом здесь, чем получить молчаливый разрыв.
            if len(message) - Protocol.HEADER_SIZE > Protocol.MAX_CLIENT_MESSAGE_SIZE:
                self._error('Сообщение слишком большое и не отправлено')
                return
            with self.send_lock:
                sock = self.socket
                if sock is None:
                    return
                sock.sendall(message)
        except Exception as e:
            logging.error(f"Error sending message: {e}")
            self.disconnect()
            
    def receive_full_message(self) -> Optional[bytes]:
        """Receive complete message"""
        try:
            # Receive header
            header = bytearray()
            while len(header) < Protocol.HEADER_SIZE:
                chunk = self.socket.recv(Protocol.HEADER_SIZE - len(header))
                if not chunk:
                    return None
                header += chunk
            
            # Get message length
            msg_length = int.from_bytes(header, byteorder='big')
            
            if not 0 < msg_length <= Protocol.MAX_MESSAGE_SIZE:
                logging.error(f"Message too large: {msg_length}")
                return None
            
            # Receive full message
            data = bytearray()
            while len(data) < msg_length:
                chunk = self.socket.recv(min(8192, msg_length - len(data)))
                if not chunk:
                    return None
                data += chunk
                
            return data
            
        except Exception as e:
            if not self._stopping.is_set():
                logging.error(f"Error receiving message: {e}")
            return None
            
    def receive_messages(self):
        """Receive messages from server"""
        while self.running and not self._stopping.is_set():
            try:
                data = self.receive_full_message()
                if not data:
                    break
                    
                msg_type, msg_data = Protocol.decode_message(data)
                
                if msg_type == MessageType.FULL_STATE:
                    # Токен приходит не в каждом FULL_STATE. Затирать рабочий
                    # нельзя: иначе после загрузки мира все передачи файлов
                    # отбивались как «Not authenticated».
                    token = msg_data.get('transfer_token')
                    if token:
                        self.transfer_token = token
                    self.transfer_ready.set()
                self._notify(msg_type, msg_data)
                manifest = (msg_data.get('game_state') or {}).get('asset_manifest', {})
                if msg_data.get('asset_ref'):
                    manifest = {msg_data['id']: msg_data['asset_ref']}
                for asset_id, meta in manifest.items():
                    if self._stopping.is_set():
                        break
                    try:
                        self.downloads.put_nowait((asset_id, meta))
                    except queue.Full:
                        self._error('Download queue is full; reconnect to retry')
                    
            except Exception as e:
                if self.running:
                    logging.error(f"Error in receive loop: {e}")
                break
                
        self.disconnect()
        
    def set_message_callback(self, callback: Callable):
        """Set callback for received messages"""
        with self._callback_lock:
            self.message_callback = callback

    def _notify(self, msg_type, data):
        """Отдать сообщение окну, только пока оно ещё живо.

        Колбэк читаем под замком: shutdown снимает его раньше, чем начинает
        ждать потоки. Поэтому поздний ответ сети уже не дойдёт до удалённого
        MainWindow.
        """
        if self._stopping.is_set():
            return
        with self._callback_lock:
            callback = self.message_callback
        if callback is None or self._stopping.is_set():
            return
        try:
            callback(msg_type, data)
        except Exception:
            logging.exception('Network callback failed')

    def _error(self, message):
        logging.error(message)
        self._notify(MessageType.ERROR, {'message': str(message)})

    def _upload_worker(self):
        while True:
            if self._stopping.is_set():
                break
            try:
                msg_type, data = self.uploads.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self._stopping.is_set():
                    break
                if not self.transfer_ready.wait(30) or not self.connected:
                    raise ConnectionError('Not connected')
                source = data.pop('asset_upload')['local_path']
                # Имя могло врать о формате (PNG, сохранённый как .jpg).
                # Приводим расширение к содержимому, иначе сервер откажет
                # в приёме, хотя картинка совершенно нормальная.
                name = at.normalized_name(source, os.path.basename(source))
                meta = {'sha256': at.digest_file(source),
                        'size': os.path.getsize(source), 'name': name}
                at.transfer(self.address, self.transfer_token, meta, self.cache_root, source)
                data['asset_ref'] = meta
                self.send_message(msg_type, data)
            except Exception as exc:
                self._error('Upload: ' + str(exc))
            finally:
                self.uploads.task_done()

    def _download_worker(self):
        while True:
            if self._stopping.is_set():
                break
            try:
                asset_id, meta = self.downloads.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self._stopping.is_set() or not self.connected:
                    continue
                path = at.transfer(self.address, self.transfer_token, meta, self.cache_root)
                self._notify(MessageType.ASSET_READY,
                             {'id': asset_id, 'path': path})
            except Exception as exc:
                self._error('Download: ' + str(exc) + '; reconnect to retry')
            finally:
                self.downloads.task_done()
