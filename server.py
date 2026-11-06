"""ccwatch: servidor local + janelas em modo app (Edge/Chrome).

Uso:  ccwatch.exe             (ou python server.py; build com build.bat)
Abre o hub; cada chat pode ser aberto em sua própria janela.
O servidor encerra sozinho quando todas as janelas são fechadas.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import core

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))   # _MEIPASS: dentro do .exe
WEB = os.path.join(BASE, "web")
if sys.platform == "darwin":
    _APPDATA = os.path.expanduser("~/Library/Application Support")
else:
    _APPDATA = os.environ.get("LOCALAPPDATA", BASE)
DATA = os.path.join(_APPDATA, "ccwatch")                                      # log e perfil do navegador
PORT = int(os.environ.get("CCWATCH_PORT", 8765))
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
IDLE_EXIT = 180          # segundos sem nenhuma janela consultando a API
HUB_CHATS = 60           # quantos chats recentes o hub acompanha
REFRESH = 2.0

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".ico": "image/x-icon", ".png": "image/png"}

state = {"chats": [], "ready": False, "last_hit": time.time()}


# ---------- varredura em segundo plano ----------

def refresher():
    while True:
        try:
            out = []
            for chat in core.list_chats()[:HUB_CHATS]:
                out.append(core.chat_summary(chat))
                if not state["ready"] and len(out) % 5 == 0:
                    state["chats"] = list(out)      # mostra progresso na 1ª passada
            state["chats"], state["ready"] = out, True
        except Exception:
            log(traceback.format_exc())
        time.sleep(REFRESH)


def log(msg):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "hub.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


# ---------- janelas ----------

def find_browser():
    if sys.platform == "darwin":
        home = os.path.expanduser("~")
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            for base in ("/Applications", os.path.join(home, "Applications")):
                p = os.path.join(base, app + ".app", "Contents", "MacOS", app)
                if os.path.exists(p):
                    return p
        return None
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    cands = [
        os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return shutil.which("msedge") or shutil.which("chrome")


def open_window(path, size=(560, 780)):
    if os.environ.get("CCWATCH_NO_WINDOW"):      # modo de teste: só a API
        return True
    exe = find_browser()
    if not exe:
        log("nenhum navegador encontrado (Edge/Chrome)")
        return False
    profile = os.path.join(DATA, "profile")
    subprocess.Popen([
        exe, f"--app=http://127.0.0.1:{PORT}{path}",
        f"--window-size={size[0]},{size[1]}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
    ])
    return True


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "ccwatch-hub"

    def log_message(self, *a):          # pythonw não tem stderr
        pass

    def _host_ok(self):
        return self.headers.get("Host", "") in HOSTS

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "host"}, 403)
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            state["last_hit"] = time.time()
            return self._api(url)
        return self._static(url.path)

    def do_POST(self):
        if not self._host_ok() or "json" not in self.headers.get("Content-Type", ""):
            return self._json({"error": "forbidden"}, 403)
        url = urlparse(self.path)
        if url.path != "/api/open":
            return self._json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        chat_id = body.get("id")
        if chat_id:
            chat = core.find_chat(str(chat_id))
            if not chat:
                return self._json({"error": "chat não encontrado"}, 404)
            ok = open_window(f"/chat.html?id={chat['id']}", (720, 440))
        else:
            ok = open_window("/", (1040, 640))
        return self._json({"ok": ok})

    def _api(self, url):
        if url.path == "/api/ping":
            return self._json({"app": "ccwatch-hub"})
        if url.path == "/api/chats":
            return self._json({"ready": state["ready"], "now": time.time(),
                               "chats": state["chats"],
                               "account": core.account(), "usage": core.scan_usage()})
        if url.path == "/api/chat":
            chat_id = (parse_qs(url.query).get("id") or [""])[0]
            chat = core.find_chat(chat_id)
            if not chat:
                return self._json({"error": "chat não encontrado"}, 404)
            return self._json({"now": time.time(), "chat": core.chat_summary(chat),
                               "account": core.account(), "usage": core.scan_usage()})
        return self._json({"error": "not found"}, 404)

    def _static(self, path):
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB, name))
        if not full.startswith(WEB + os.sep) or not os.path.isfile(full):
            self.send_response(404)
            self.end_headers()
            return
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def already_running():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1.5) as r:
            return json.load(r).get("app") == "ccwatch-hub"
    except Exception:
        return False


def idle_watchdog(httpd):
    while True:
        time.sleep(15)
        if time.time() - state["last_hit"] > IDLE_EXIT:
            httpd.shutdown()
            return


def main():
    if already_running():           # segunda execução: só abre outro hub
        open_window("/", (1040, 640))
        return 0
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        log(f"porta {PORT} ocupada: {e}")
        return 1
    core.start_usage_worker()
    threading.Thread(target=refresher, daemon=True).start()
    threading.Thread(target=idle_watchdog, args=(httpd,), daemon=True).start()
    open_window("/", (1040, 640))
    state["last_hit"] = time.time()
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log(traceback.format_exc())
        raise
