"""Хост панели Long Story Short на WebView2 (пакет pywebview).

Почему отдельный процесс: pywebview требует главного потока и своего GUI-цикла,
а приложение живёт на Qt. Поэтому панель поднимается как дочерний процесс,
а общение идёт построчным JSON:

    stdout: {"event": "status"|"roll"|"health"|"sheet"|"error", ...}
    stdin:  {"cmd": "close"} | {"cmd": "adjust_hp", "delta": -5}

Почему не QtWebEngine: в Qt 5.15 движок — Chromium 83, бандл листа LSS на нём
падает с SyntaxError и приложение не стартует. WebView2 — системный Chromium
(Edge), он лист открывает; проверено на этой машине: Chrome 155.

Что делает панель:
  * открывает лист как страницу верхнего уровня (CSP frame-ancestors не мешает);
  * ловит конверты SDK {__lssSheetSdk: 2, event: {type, payload}} — броски и хиты;
  * перехватывает ответы fetch/XHR листа, чтобы получить документ персонажа
    (jsonType: "character") — из него берутся характеристики, КД и описание;
  * умеет отправить в лист lss:command (правка хитов со стороны стола).
"""
import argparse
import json
import os
import sys
import threading
import time

from shared import app_paths

#: Точка входа листа: список персонажей, дальше пользователь выбирает сам.
SHEET_URL = "https://longstoryshort.app/iframe/characters/list/"

#: Скрипт, который ставится в страницу и переживает перезагрузки (ставится
#: заново по таймеру). Идемпотентен: window.__lssHostInstalled.
BOOTSTRAP_JS = r"""
(() => {
  if (!window.__lssHostInstalled) {
    window.__lssHostInstalled = true;
    window.__lssQueue = [];

    const push = (ev) => {
      if (window.__lssQueue.length < 200) {
        window.__lssQueue.push(ev);
      }
    };

    // 1. События SDK: лист кладёт их в window.parent, на верхнем уровне это он сам.
    window.addEventListener('message', (e) => {
      const d = e.data;
      if (d && typeof d === 'object' && d.__lssSheetSdk === 2 &&
          d.event && typeof d.event.type === 'string') {
        push({
          channel: 'sdk',
          type: d.event.type,
          payload: (d.event.payload && typeof d.event.payload === 'object')
              ? d.event.payload : null,
          origin: String(e.origin || '')
        });
      }
    });

    // 2. Документ персонажа: ответы fetch/XHR, где есть jsonType или vitality.
    const looksSheet = (txt) => {
      if (!txt || txt.length > 4000000) { return false; }
      return txt.indexOf('"jsonType"') >= 0 || txt.indexOf('"vitality"') >= 0;
    };
    const pushHttp = (url, txt) => {
      if (!looksSheet(txt)) { return; }
      let body = null;
      try { body = JSON.parse(txt); } catch (e) { return; }
      push({channel: 'http', url: String(url || ''), body: body});
    };

    try {
      const origFetch = window.fetch;
      if (origFetch) {
        window.fetch = function () {
          const p = origFetch.apply(this, arguments);
          try {
            p.then((res) => {
              const ct = (res.headers && res.headers.get('content-type')) || '';
              if (ct.indexOf('json') >= 0) {
                res.clone().text().then((txt) => pushHttp(res.url, txt))
                   .catch(() => {});
              }
            }).catch(() => {});
          } catch (e) {}
          return p;
        };
      }
    } catch (e) {}

    try {
      const origOpen = XMLHttpRequest.prototype.open;
      const origSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function (method, url) {
        this.__lssUrl = url;
        return origOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function () {
        this.addEventListener('load', () => {
          try { pushHttp(this.__lssUrl, this.responseText); } catch (e) {}
        });
        return origSend.apply(this, arguments);
      };
    } catch (e) {}

    // 3. Документ персонажа: страница знает свой id, токен входа лежит в куке
    // lss-jwt (значение в кавычках — это JSON-строка). Запрос идёт из самой
    // страницы, поэтому origin и токен — те же, что у листа.
    const sheetId = () => {
      const m = location.pathname.match(/\/characters\/(?:digital|builder)\/([0-9a-fA-F]{24})/);
      return m ? m[1] : '';
    };
    const authToken = () => {
      let token = '';
      document.cookie.split(';').forEach((part) => {
        const i = part.indexOf('=');
        if (i < 0) { return; }
        const key = part.slice(0, i).trim();
        if (key !== 'lss-jwt') { return; }
        let value = part.slice(i + 1).trim();
        try { value = JSON.parse(value); } catch (e) {}
        token = String(value || '').replace(/^"+|"+$/g, '');
      });
      return token;
    };
    window.__lssPullCharacter = (force) => {
      const id = sheetId();
      if (!id) { return 'no-id'; }
      const now = Date.now();
      if (!force && window.__lssPulledAt && now - window.__lssPulledAt < 4000) {
        return 'wait';
      }
      const token = authToken();
      if (!token) { return 'no-token'; }
      window.__lssPulledAt = now;
      window.__lssPullState = 'started';
      fetch('https://api.longstoryshort.app/character/' + id + '/', {
        credentials: 'omit',
        headers: {'Authorization': 'Bearer ' + token}
      }).then((res) => {
        if (!res.ok) { throw new Error('HTTP ' + res.status); }
        return res.json();
      }).then((doc) => {
        const sig = String(doc.updatedAt || '') + '|' + String(doc._id || '');
        window.__lssPullState = 'ok';
        window.__lssDocError = '';
        if (sig !== window.__lssLastSig) {
          window.__lssLastSig = sig;
          push({channel: 'api', url: location.href, body: doc});
        }
      }).catch((err) => {
        window.__lssPullState = 'error';
        window.__lssDocError = String(err);
      });
      return 'started';
    };

    window.__lssDrain = () => {
      return JSON.stringify(window.__lssQueue.splice(0, window.__lssQueue.length));
    };
  }
  return true;
})()
"""


def emit(payload: dict):
    """Строка события для родительского процесса."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def route(event: dict) -> list:
    """Очередь событий страницы → события нашего протокола.

    Чистая функция без окна: её можно проверить тестом без браузера.
    Неизвестное молча отбрасывается — лист может присылать что угодно.
    """
    out = []
    if not isinstance(event, dict):
        return out
    origin = str(event.get("origin") or "")
    # Чужие конверты (не с сайта LSS) не принимаем: страница листа может
    # открыть сторонний iframe, и его сообщения не должны попадать в чат.
    if origin and "longstoryshort.app" not in origin:
        return out

    if event.get("channel") in ("http", "api"):
        body = event.get("body")
        if isinstance(body, dict):
            out.append({"event": "sheet", "url": str(event.get("url") or ""),
                        "body": body})
        return out

    kind = str(event.get("type") or "")
    payload = event.get("payload")
    if kind == "lss:roll" and isinstance(payload, dict):
        out.append({"event": "roll", "payload": payload})
    elif kind == "dnd:health" and isinstance(payload, dict):
        out.append({"event": "health", "payload": payload})
    elif kind == "lss:manifest":
        out.append({"event": "manifest",
                    "payload": payload if isinstance(payload, dict) else {}})
    return out


def profile_dir() -> str:
    """Папка профиля WebView2: там живут куки входа в LSS."""
    path = os.path.join(app_paths.home_dir(), "lss_profile")
    os.makedirs(path, exist_ok=True)
    return path


class Panel:
    """Окно листа и мост между страницей и родительским процессом."""

    def __init__(self, webview, url: str, dump: bool = False):
        self.webview = webview
        self.url = url
        self.dump = dump
        self.stop = threading.Event()
        self.window = webview.create_window(
            "Long Story Short — лист персонажа", url,
            width=1200, height=900, min_size=(900, 600))
        self.log_path = os.path.join(profile_dir(), "lss_capture.log")

    def log(self, text: str):
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Команды из стола
    # ------------------------------------------------------------------
    def send_hp_delta(self, delta: int):
        """Попросить лист изменить хиты (lss:command, экспериментально)."""
        delta = int(delta)
        script = (
            "(() => { const m = {__lssSheetSdk: 2, event: {type: 'lss:command',"
            " payload: {op: 'adjust', capabilityId: 'hp', delta: %d}}};"
            " window.postMessage(m, location.origin); return true; })()" % delta)
        try:
            self.window.evaluate_js(script)
            emit({"event": "sent", "delta": delta})
        except Exception as exc:      # окно закрыто — команду отправлять некуда
            emit({"event": "error", "text": f"команда не отправлена: {exc}"})

    def listen_stdin(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            name = cmd.get("cmd")
            if name == "close":
                break
            if name == "adjust_hp":
                try:
                    self.send_hp_delta(int(cmd.get("delta") or 0))
                except (TypeError, ValueError):
                    pass
            if name == "goto" and cmd.get("url"):
                try:
                    self.window.load_url(str(cmd["url"]))
                except Exception:
                    pass
        self.stop.set()
        try:
            self.window.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Опрос страницы
    # ------------------------------------------------------------------
    def worker(self):
        last_url = None
        last_doc_error = ""
        errors = 0
        closed = getattr(getattr(self.window, "events", None), "closed", None)
        while not self.stop.is_set():
            if closed is not None:
                try:
                    if closed.is_set():
                        break
                except Exception:
                    closed = None
            time.sleep(0.7)
            try:
                self.window.evaluate_js(BOOTSTRAP_JS)
                # Документ персонажа перезапрашивается раз в несколько секунд:
                # правки игрока прямо в листе должны доезжать до токена.
                self.window.evaluate_js(
                    "window.__lssPullCharacter && window.__lssPullCharacter(false)")
                doc_error = self.window.evaluate_js(
                    "String(window.__lssDocError || '')")
                raw = self.window.evaluate_js(
                    "window.__lssDrain ? window.__lssDrain() : '[]'")
            except Exception:
                # Окно ещё не готово или уже закрыто — ждём следующий тик.
                errors += 1
                if errors > 120:
                    emit({"event": "error", "text": "панель не отвечает"})
                    break
                continue
            errors = 0
            if doc_error and doc_error != last_doc_error:
                last_doc_error = str(doc_error)
                emit({"event": "error", "text": "Лист не прочитан: " + str(doc_error)})
            if raw:
                try:
                    events = json.loads(raw)
                except (TypeError, ValueError):
                    events = []
                for event in events:
                    for message in route(event):
                        if message["event"] == "sheet":
                            self.log("sheet: " + str(message.get("url"))[:300])
                        emit(message)
            try:
                url = self.window.evaluate_js("location.href")
            except Exception:
                url = None
            if url and url != last_url:
                last_url = url
                emit({"event": "status", "url": str(url)})
                if self.dump:
                    # Сайт — React-приложение: сразу после загрузки body пуст.
                    # Смотрим дважды, чтобы отличить «ещё рисуется» от «пусто».
                    for delay in (8, 15):
                        threading.Timer(delay, self.dump_dom).start()
        self.stop.set()

    def dump_dom(self):
        """Диагностика: что реально нарисовано на странице.

        Нужна для проверки панели на живой машине: URL может быть верным,
        а приложение внутри — пустым (так было в QtWebEngine).
        """
        script = """
(() => {
  const meta = (el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type') || '',
    name: el.getAttribute('name') || '',
    id: el.getAttribute('id') || '',
    aria: el.getAttribute('aria-label') || '',
    ph: el.getAttribute('placeholder') || '',
    value: (el.value === undefined ? '' : String(el.value)).slice(0, 40),
  });
  const nodes = Array.from(document.querySelectorAll('input,select,textarea'));
  return {
    url: location.href,
    title: document.title,
    text: document.body.innerText.slice(0, 600),
    inputs: nodes.length,
    fields: nodes.slice(0, 80).map(meta),
  };
})()
"""
        try:
            info = self.window.evaluate_js(script)
        except Exception as exc:
            emit({"event": "dom", "error": str(exc)})
            return
        if isinstance(info, dict):
            emit({"event": "dom",
                  "url": str(info.get("url") or ""),
                  "title": str(info.get("title") or ""),
                  "inputs": int(info.get("inputs") or 0),
                  "text": str(info.get("text") or ""),
                  "fields": info.get("fields") or []})

    def start(self):
        threading.Thread(target=self.listen_stdin, daemon=True).start()
        threading.Thread(target=self.worker, daemon=True).start()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Панель листа Long Story Short")
    parser.add_argument("--url", default=SHEET_URL)
    parser.add_argument("--dump", action="store_true",
                        help="напечатать содержимое страницы для диагностики")
    args = parser.parse_args(argv)

    try:
        import webview
    except ImportError:
        emit({"event": "error",
              "text": "Нужен пакет pywebview: pip install pywebview"})
        return 2

    try:
        panel = Panel(webview, args.url, dump=args.dump)
    except Exception as exc:
        emit({"event": "error", "text": f"окно не создано: {exc}"})
        return 3

    emit({"event": "ready", "profile": profile_dir()})
    webview.start(panel.start, gui="edgechromium", debug=False,
                  private_mode=False, storage_path=profile_dir())
    panel.stop.set()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main())
