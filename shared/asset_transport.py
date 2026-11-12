"""Bounded-memory binary file transfer on a separate TCP connection.
The game connection carries only metadata. No external service/extra port.
"""
import hashlib
import json
import os
import re
import socket
import tempfile

CHUNK = 256 * 1024
MAX_ASSET_BYTES = int(os.environ.get('VTT_MAX_ASSET_MB', '512')) * 1024 * 1024

# Белый список: по сети ездят только картинки и аудио, которые стол умеет
# показать. Исполняемые файлы, скрипты, html и архивы не принимаются даже
# от мастера: расширение — не единственная защита (см. verify_content),
# но без белого списка клиент мог бы получить .exe под видом ресурса.
IMAGE_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.jfif', '.gif', '.bmp', '.webp',
})
AUDIO_EXTENSIONS = frozenset({'.mp3', '.wav', '.ogg', '.oga', '.flac', '.m4a'})
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | AUDIO_EXTENSIONS

#: Порядок распознавания: сначала конкретные форматы, потом общие RIFF.
_DETECT_ORDER = ('.png', '.jpg', '.gif', '.bmp', '.webp',
                 '.wav', '.flac', '.ogg', '.mp3', '.m4a')

#: Первые байты файла для каждого семейства. Проверка ловит главный случай:
#: исполняемый файл или скрипт, переименованный в .png.
_MAGIC = {
    '.png': (b'\x89PNG\r\n\x1a\n',),
    '.jpg': (b'\xff\xd8\xff',),
    '.jpeg': (b'\xff\xd8\xff',),
    '.jfif': (b'\xff\xd8\xff',),
    '.gif': (b'GIF87a', b'GIF89a'),
    '.bmp': (b'BM',),
    '.webp': (b'RIFF',),
    '.wav': (b'RIFF',),
    '.flac': (b'fLaC',),
    '.ogg': (b'OggS',),
    '.oga': (b'OggS',),
    '.mp3': (b'ID3', b'\xff\xfb', b'\xff\xf3', b'\xff\xf2', b'\xff\xfa'),
    '.m4a': (b'ftyp',),
}


def extension(name) -> str:
    """Расширение из имени файла маленькими буквами (без пути).

    Берём только basename: имя из сети может содержать «..\\» и слэши,
    но путь из него не строится — файл в кэше называется по SHA-256.
    """
    if not isinstance(name, str):
        return ''
    return os.path.splitext(os.path.basename(name))[1].lower()


def recv_exact(sock, count):
    buf = bytearray()
    while len(buf) < count:
        part = sock.recv(min(CHUNK, count - len(buf)))
        if not part:
            raise ConnectionError('Transfer interrupted')
        buf.extend(part)
    return buf


def send_json(sock, data):
    raw = json.dumps(data).encode('utf-8')
    if len(raw) > 65536:
        raise ValueError('Metadata too large')
    sock.sendall(len(raw).to_bytes(8, 'big') + raw)


def recv_json(sock):
    size = int.from_bytes(recv_exact(sock, 8), 'big')
    if not 0 < size <= 65536:
        raise ValueError('Invalid metadata size')
    return json.loads(recv_exact(sock, size))


def digest_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for part in iter(lambda: f.read(CHUNK), b''):
            h.update(part)
    return h.hexdigest()


def validate(meta):
    """Проверить описание ресурса: хэш, размер и допустимый тип."""
    if not isinstance(meta, dict):
        raise ValueError('Invalid resource metadata')
    digest = meta.get('sha256')
    size = meta.get('size')
    name = meta.get('name', '')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('Invalid SHA-256')
    if type(size) is not int or not 0 <= size <= MAX_ASSET_BYTES:
        raise ValueError('File exceeds VTT_MAX_ASSET_MB')
    if not isinstance(name, str) or len(name) > 255:
        raise ValueError('Invalid file name')
    ext = extension(name)
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError('Unsupported resource type: ' + (ext or 'без расширения'))
    return digest, size


def cache_path(root, meta):
    digest, _ = validate(meta)
    # Имя строится только из хэша и проверенного расширения — путь из сети
    # никогда не используется как путь.
    ext = extension(meta.get('name'))
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, digest + ext)


def cached(root, meta):
    path = cache_path(root, meta)
    if os.path.isfile(path) and os.path.getsize(path) == meta['size']:
        if digest_file(path) == meta['sha256']:
            return path
    return None


def _matches_magic(head: bytes, ext: str) -> bool:
    """Похожи ли первые байты на формат ext."""
    expected = _MAGIC.get(ext)
    if not expected:
        return False
    if ext == '.webp':
        return head[:4] == b'RIFF' and head[8:12] == b'WEBP'
    if ext == '.m4a':
        return b'ftyp' in head
    if ext == '.wav':
        return head[:4] == b'RIFF' and head[8:12] == b'WAVE'
    return any(head.startswith(sig) for sig in expected)


def _read_head(path: str) -> bytes:
    try:
        with open(path, 'rb') as f:
            return f.read(16)
    except OSError:
        return b''


def detect_extension(path: str) -> str:
    """Определить формат файла по первым байтам.

    Расширение от пользователя — только подсказка: карты часто сохраняют
    как ``.jpg``, хотя внутри PNG или WebP. Поэтому тип определяет
    содержимое, а не имя файла. Неизвестный или неподдерживаемый формат
    даёт пустую строку.
    """
    head = _read_head(path)
    if not head:
        return ''
    for ext in _DETECT_ORDER:
        if _matches_magic(head, ext):
            return ext
    return ''


def normalized_name(path: str, name: str) -> str:
    """Имя с расширением, которое совпадает с содержимым файла.

    ``map.jpg`` с PNG внутри станет ``map.png`` — сервер и кэш получат
    правильный тип, а Qt/Pillow всё равно загружают картинку по содержимому.
    Звук и неизвестные форматы не трогаем.
    """
    ext = extension(name)
    if ext not in ALLOWED_EXTENSIONS:
        return name
    detected = detect_extension(path)
    if not detected or detected == ext:
        return name
    # .jpeg/.jfif — то же семейство JPEG, переименовывать не нужно.
    if detected == '.jpg' and ext in ('.jpeg', '.jfif'):
        return name
    return os.path.splitext(name)[0] + detected


def verify_content(path: str, ext: str) -> bool:
    """Похоже ли содержимое файла на заявленную группу типов.

    Внутри одной группы (картинки или звук) расширение может не совпадать:
    PNG с именем ``.jpg`` — всё ещё безопасная картинка, и такие файлы
    приходят от пользователей. Главное — не пропустить исполняемый файл
    или скрипт под видом ресурса.
    """
    head = _read_head(path)
    if not head:
        return False
    if ext in IMAGE_EXTENSIONS:
        return any(_matches_magic(head, image_ext)
                   for image_ext in IMAGE_EXTENSIONS)
    if ext in AUDIO_EXTENSIONS:
        return any(_matches_magic(head, audio_ext)
                   for audio_ext in AUDIO_EXTENSIONS)
    return False


def receive_file(sock, root, meta):
    target = cache_path(root, meta)
    _, left = validate(meta)
    fd, tmp = tempfile.mkstemp(prefix='.transfer-', dir=root)
    h = hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as f:
            while left:
                part = sock.recv(min(CHUNK, left))
                if not part:
                    raise ConnectionError('Transfer interrupted')
                f.write(part)
                h.update(part)
                left -= len(part)
        if h.hexdigest() != meta['sha256']:
            raise ValueError('SHA-256 mismatch')
        if not verify_content(tmp, extension(meta.get('name'))):
            raise ValueError('Содержимое не соответствует типу файла: '
                             + str(meta.get('name') or 'без имени'))
        os.replace(tmp, target)
        return target
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def send_file(sock, path, size):
    with open(path, 'rb') as f:
        left = size
        while left:
            part = f.read(min(CHUNK, left))
            if not part:
                raise IOError('File changed during transfer')
            sock.sendall(part)
            left -= len(part)


def transfer(address, token, meta, root, upload_path=None):
    validate(meta)
    if upload_path is None:
        hit = cached(root, meta)
        if hit:
            return hit
    with socket.create_connection(address, timeout=30) as sock:
        sock.settimeout(60)
        send_json(sock, {'type': 'asset_transfer', 'data': {
            'token': token, 'op': 'put' if upload_path else 'get', 'meta': meta}})
        reply = recv_json(sock)
        if not reply.get('ok'):
            raise IOError(reply.get('error', 'Resource unavailable'))
        if upload_path:
            if not reply.get('cached'):
                send_file(sock, upload_path, meta['size'])
                result = recv_json(sock)
                if not result.get('ok'):
                    raise IOError(result.get('error', 'Upload failed'))
            return upload_path
        return receive_file(sock, root, meta)
