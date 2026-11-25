"""Camada de dados do ccwatch-hub.

Portada do ccwatch.py (sem dependências). Lê apenas arquivos locais do Claude Code:
  ~/.claude.json                         -> conta logada + cache do /usage
  ~/.claude/projects/<proj>/<id>.jsonl   -> transcript (tokens)
  ~/.claude/projects/<proj>/<id>/custom-title.json -> nome do chat

Diferenças para o script de terminal:
  - cache por arquivo (só reprocessa o que mudou)
  - soma incremental do gasto acumulado de cada conversa
  - nada de ANSI: tudo devolve dicts prontos para JSON
"""

import datetime
import getpass
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
CFG = os.path.join(HOME, ".claude.json")
PROJ = os.path.join(HOME, ".claude", "projects")
CREDS = os.path.join(HOME, ".claude", ".credentials.json")
TAIL_BYTES = 600_000

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_REFRESH = 60.0
KEYCHAIN_SERVICES = ("Claude Code-credentials", "Claude Code")


# ---------- config / conta ----------

def load_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def account(cfg=None):
    oa = (cfg if cfg is not None else load_cfg()).get("oauthAccount") or {}
    return {
        "email": oa.get("emailAddress") or "?",
        "org": oa.get("organizationName") or "",
        "plan": oa.get("seatTier") or oa.get("billingType") or "",
    }


# ---------- descoberta de chats ----------

_CWD_RE = re.compile(rb'"cwd":"((?:[^"\\]|\\.)*)"')
_cwd_cache = {}


def read_cwd(path):
    """Pasta do projeto, lida do início do transcript (com cache)."""
    if path in _cwd_cache:
        return _cwd_cache[path]
    try:
        with open(path, "rb") as f:
            m = _CWD_RE.search(f.read(8192))
    except OSError:
        return None
    if not m:
        return None
    try:
        cwd = json.loads(b'"' + m.group(1) + b'"')
    except Exception:
        cwd = m.group(1).decode(errors="replace")
    if cwd.lower().startswith(HOME.lower()):
        cwd = "~" + cwd[len(HOME):]
    _cwd_cache[path] = cwd
    return cwd


_prompt_cache = {}


def first_prompt(path):
    """Primeira mensagem do usuário (para dar nome a chats sem título)."""
    if path in _prompt_cache:
        return _prompt_cache[path]
    found = None
    try:
        with open(path, "rb") as f:
            head = f.read(300_000)
        for raw in head.split(b"\n")[:200]:
            if b'"type":"user"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
                continue
            c = (d.get("message") or {}).get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            c = " ".join(str(c or "").split())
            if c and not c.startswith("<"):
                found = c[:70] + ("…" if len(c) > 70 else "")
                break
    except OSError:
        return None
    if found:                       # só guarda quando achou (o chat pode ainda estar começando)
        _prompt_cache[path] = found
    return found


def list_chats():
    out = []
    if not os.path.isdir(PROJ):
        return out
    for proj in os.listdir(PROJ):
        pdir = os.path.join(PROJ, proj)
        if not os.path.isdir(pdir):
            continue
        for name in os.listdir(pdir):
            if not name.endswith(".jsonl"):
                continue
            sid = name[:-6]
            path = os.path.join(pdir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size < 200:
                continue
            title = None
            tf = os.path.join(pdir, sid, "custom-title.json")
            if os.path.exists(tf):
                try:
                    with open(tf, encoding="utf-8") as f:
                        title = (json.load(f) or {}).get("customTitle")
                except Exception:
                    pass
            out.append({
                "id": sid, "path": path, "proj": proj,
                "cwd": read_cwd(path) or proj,
                "title": title or first_prompt(path) or f"(sem nome) {sid[:8]}",
                "named": bool(title), "mtime": st.st_mtime,
            })
    out.sort(key=lambda c: -c["mtime"])
    return out


def find_chat(chat_id):
    for c in list_chats():
        if c["id"] == chat_id:
            return c
    return None


# ---------- contexto e gasto (transcript) ----------

_MODEL_RE = re.compile(rb'"modelId":"([^"]+)","marketingName":"([^"]*)"')
_lock = threading.Lock()


def _read_tail_lines(path):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read()
    lines = data.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]
    return lines


_totals = {}


def _epoch(iso):
    try:
        return datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


_act_cache = {"at": 0.0, "val": None}


def activity():
    """Tokens por período (somando todos os chats): 24h de hora em hora e 7d de 6 em 6h."""
    now = time.time()
    if _act_cache["val"] and now - _act_cache["at"] < 10:
        return _act_cache["val"]
    off = time.localtime().tm_gmtoff or 0
    res = {}
    with _lock:
        msgs = [m for s in _totals.values() for m in s["msgs"].values()]
    for hours, step in ((24, 3600), (168, 21600)):
        n = hours * 3600 // step
        first = (int((now + off) // step) - n + 1) * step - off
        buckets = [{"t": first + i * step, "new": 0, "out": 0} for i in range(n)]
        for m in msgs:
            if len(m) < 4 or m[3] < first:
                continue
            i = int((m[3] - first) // step)
            if 0 <= i < n:
                buckets[i]["new"] += m[0]
                buckets[i]["out"] += m[2]
        res[str(hours)] = {"step": step, "buckets": buckets}
    _act_cache.update(at=now, val=res)
    return res


def scan_totals(path):
    """Gasto acumulado do chat inteiro. Lê só os bytes novos desde a última vez."""
    try:
        size = os.stat(path).st_size
    except OSError:
        return {"new_in": 0, "cache_read": 0, "out": 0, "msgs": 0, "model": ("", "")}
    with _lock:
        s = _totals.get(path)
        if s is None or size < s["off"]:
            s = _totals[path] = {"off": 0, "msgs": {}, "model": ("", "")}
        if size > s["off"]:
            with open(path, "rb") as f:
                f.seek(s["off"])
                data = f.read()
            end = data.rfind(b"\n")
            if end >= 0:
                chunk = data[:end + 1]
                s["off"] += end + 1
                for m in _MODEL_RE.finditer(chunk):
                    s["model"] = (m.group(1).decode(), m.group(2).decode())
                for raw in chunk.split(b"\n"):
                    if b'"usage"' not in raw or b'"assistant"' not in raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    if not u:
                        continue
                    key = msg.get("id") or d.get("uuid") or f"{s['off']}-{len(s['msgs'])}"
                    # a mesma mensagem pode aparecer em várias linhas: vale a última
                    s["msgs"][key] = (
                        (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                        u.get("cache_read_input_tokens") or 0,
                        u.get("output_tokens") or 0,
                        _epoch(d.get("timestamp")),
                    )
        vals = s["msgs"].values()
        return {
            "new_in": sum(v[0] for v in vals),
            "cache_read": sum(v[1] for v in vals),
            "out": sum(v[2] for v in vals),
            "msgs": len(s["msgs"]),
            "model": s["model"],
        }


_ctx_cache = {}


def scan_context(path):
    """Estado do contexto (lê o fim do transcript). Cacheado por tamanho/mtime."""
    try:
        st = os.stat(path)
    except OSError:
        return _empty_ctx()
    key = (st.st_size, st.st_mtime_ns)
    hit = _ctx_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    res = _scan_context(path)
    _ctx_cache[path] = (key, res)
    return res


def _empty_ctx():
    return {"used": 0, "limit": 200_000, "pct": 0.0, "model": "?", "model_name": "",
            "last_out": 0, "last_new": 0, "last_ts": None, "turns": 0, "effort": ""}


def _scan_context(path):
    res = _empty_ctx()
    mid, mname = scan_totals(path)["model"]
    if mid:
        res["model"], res["model_name"] = mid, mname
    try:
        lines = _read_tail_lines(path)
    except OSError:
        return res

    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue

        if not res["model_name"] and b'"type":"model"' in raw:
            ident = (d.get("attachment") or {}).get("identity") or {}
            if ident.get("modelId"):
                res["model"] = ident["modelId"]
                res["model_name"] = ident.get("marketingName") or ""

        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        msg = d.get("message") or {}
        u = msg.get("usage") or {}
        if not u:
            continue
        if res["used"] == 0:
            res["used"] = ((u.get("input_tokens") or 0)
                           + (u.get("cache_creation_input_tokens") or 0)
                           + (u.get("cache_read_input_tokens") or 0))
            res["last_out"] = u.get("output_tokens") or 0
            res["last_new"] = ((u.get("input_tokens") or 0)
                               + (u.get("cache_creation_input_tokens") or 0))
            res["last_ts"] = d.get("timestamp")
            res["effort"] = d.get("perTurnEffort") or d.get("effort") or ""
            if msg.get("model") and res["model"] == "?":
                res["model"] = msg["model"]
        res["turns"] += 1

    blob = (res["model"] or "") + " " + (res["model_name"] or "")
    if "[1m]" in blob or "1M" in blob:
        res["limit"] = 1_000_000
    if res["used"] > res["limit"]:
        res["limit"] = 1_000_000
    res["pct"] = round(res["used"] / res["limit"] * 100, 2) if res["limit"] else 0.0
    return res


def chat_summary(chat):
    t = scan_totals(chat["path"])
    return {
        "id": chat["id"], "title": chat["title"], "named": chat["named"],
        "cwd": chat["cwd"], "mtime": chat["mtime"],
        "ctx": scan_context(chat["path"]),
        "totals": {k: t[k] for k in ("new_in", "cache_read", "out", "msgs")},
    }


# ---------- usage (limites do plano) ----------

WINDOWS = (
    ("five_hour", "Sessão (5h)"),
    ("seven_day", "Semana (7d)"),
    ("seven_day_opus", "Semana Opus"),
    ("seven_day_sonnet", "Semana Sonnet"),
)


def _keychain_raw(service, acct):
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", acct, "-s", service, "-w"],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def _oauth_token(raw):
    try:
        return ((json.loads(raw) or {}).get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def read_token():
    """Token OAuth do Claude Code: arquivo de credenciais, depois Keychain (macOS)."""
    try:
        with open(CREDS, encoding="utf-8") as f:
            tok = _oauth_token(f.read())
            if tok:
                return tok
    except Exception:
        pass
    user = getpass.getuser()
    for service in KEYCHAIN_SERVICES:
        for acct in (user, ""):
            raw = _keychain_raw(service, acct)
            tok = _oauth_token(raw) if raw else None
            if tok:
                return tok
    return None


def fetch_usage_api():
    """Devolve (dados, erro, segundos_para_tentar_de_novo)."""
    tok = read_token()
    if not tok:
        return None, "sem token (faça login no Claude Code)", None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "ccwatch-hub",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "token expirado (abra o Claude Code)", None
        if e.code == 429:
            try:
                wait = int(e.headers.get("Retry-After") or 0) or None
            except ValueError:
                wait = None
            return None, "a API limitou as consultas (429)", wait
        return None, "HTTP %s" % e.code, None
    except Exception:
        return None, "sem rede", None
    if not isinstance(data, dict):
        return None, "resposta inválida", None
    util = data.get("utilization")
    return (util if isinstance(util, dict) else data), None, None


# Último valor bom fica no disco: ao reiniciar (ou se a API limitar), mostra o que já se sabia.
if sys.platform == "darwin":
    STATE_DIR = os.path.join(HOME, "Library", "Application Support", "ccwatch")
else:
    STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or HOME, "ccwatch")
USAGE_FILE = os.path.join(STATE_DIR, "usage.json")
LIVE_MAX_AGE = 150          # acima disso o dado deixa de ser "ao vivo"

_live = {"util": None, "at": 0.0, "err": None, "next": 0.0}
_live_lock = threading.Lock()


def _load_saved():
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d.get("util"), dict):
            with _live_lock:
                _live["util"], _live["at"] = d["util"], float(d["at"])
    except Exception:
        pass


def _save(util, at):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump({"util": util, "at": at}, f)
    except OSError:
        pass


HIST_FILE = os.path.join(STATE_DIR, "usage_history.json")
HIST_STEP = 300            # uma amostra a cada 5 min
HIST_KEEP = 8 * 86400      # guarda 8 dias
_hist = []
_hist_lock = threading.Lock()


def _load_hist():
    try:
        with open(HIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        with _hist_lock:
            _hist[:] = [r for r in data if isinstance(r, dict) and "t" in r]
    except Exception:
        pass


def _record_hist(util, at):
    row = {"t": at}
    for key, _ in WINDOWS:
        w = util.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            row[key] = float(w["utilization"])
    with _hist_lock:
        if _hist and at - _hist[-1]["t"] < HIST_STEP:
            return
        _hist.append(row)
        _hist[:] = [r for r in _hist if at - r["t"] <= HIST_KEEP]
        snap = list(_hist)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f)
    except OSError:
        pass


def _usage_worker():
    _load_saved()
    _load_hist()
    with _live_lock:
        util, at = _live["util"], _live["at"]
    if util is not None:
        _record_hist(util, at)
    fails = 0
    while True:
        with _live_lock:
            age = time.time() - _live["at"] if _live["util"] is not None else None
        if age is not None and age < USAGE_REFRESH and fails == 0:
            time.sleep(USAGE_REFRESH - age)             # dado salvo ainda fresco: não consulta já
        util, err, retry = fetch_usage_api()
        now = time.time()
        if util is not None:
            fails = 0
            wait = USAGE_REFRESH
            with _live_lock:
                _live["util"], _live["at"], _live["err"] = util, now, None
            _save(util, now)
            _record_hist(util, now)
        else:
            fails += 1
            wait = retry or min(300, USAGE_REFRESH * 2 ** (fails - 1))   # recua a cada falha
            with _live_lock:
                _live["err"] = err
        with _live_lock:
            _live["next"] = now + wait
        time.sleep(wait)


def start_usage_worker():
    threading.Thread(target=_usage_worker, daemon=True).start()


def _build_windows(util):
    out = []
    for key, label in WINDOWS:
        w = util.get(key)
        if not isinstance(w, dict) or w.get("utilization") is None:
            continue
        out.append({"label": label, "pct": float(w["utilization"]),
                    "resets_at": w.get("resets_at")})
    return out


def _history():
    with _hist_lock:
        return list(_hist)


def scan_usage(history=False):
    with _live_lock:
        util, at, err, nxt = _live["util"], _live["at"], _live["err"], _live["next"]
    retry_in = max(0, nxt - time.time()) if err and nxt else None
    if util is not None:
        age = time.time() - at
        return {"age": age, "windows": _build_windows(util), "live": age < LIVE_MAX_AGE,
                "err": err, "retry_in": retry_in, "history": _history() if history else []}
    cu = load_cfg().get("cachedUsageUtilization") or {}
    age = time.time() - cu["fetchedAtMs"] / 1000.0 if cu.get("fetchedAtMs") else None
    return {"age": age, "windows": _build_windows(cu.get("utilization") or {}),
            "live": False, "err": err, "retry_in": retry_in, "history": _history() if history else []}
