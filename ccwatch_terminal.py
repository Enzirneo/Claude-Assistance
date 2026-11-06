#!/usr/bin/env python3
"""ccwatch - monitor ao vivo de /context e /usage de um chat do Claude Code.

Sem dependencias. Le apenas arquivos locais do Claude Code:
  ~/.claude.json                         -> conta logada + cache do /usage
  ~/.claude/projects/<proj>/<id>.jsonl   -> transcript (tokens de contexto)
  ~/.claude/projects/<proj>/<id>/custom-title.json -> nome do chat
"""

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
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
CFG = os.path.join(HOME, ".claude.json")
PROJ = os.path.join(HOME, ".claude", "projects")
CREDS = os.path.join(HOME, ".claude", ".credentials.json")
TAIL_BYTES = 600_000
REFRESH = 2.0

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_REFRESH = 60.0            # segundos entre buscas na API
KEYCHAIN_SERVICES = ("Claude Code-credentials", "Claude Code")

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m",
    "cyan": "\033[36m", "grey": "\033[90m",
}


def paint(s, color):
    return f"{C[color]}{s}{C['reset']}"


# ---------- leitura de config ----------

def load_cfg():
    try:
        with open(CFG) as f:
            return json.load(f)
    except Exception:
        return {}


def account(cfg):
    oa = cfg.get("oauthAccount") or {}
    return {
        "email": oa.get("emailAddress") or "?",
        "org": oa.get("organizationName") or "",
        "plan": oa.get("seatTier") or oa.get("billingType") or "",
    }


# ---------- descoberta de chats ----------

_CWD_RE = re.compile(rb'"cwd":"([^"]*)"')


def read_cwd(path):
    """Pasta do projeto, lida do inicio do transcript."""
    try:
        with open(path, "rb") as f:
            m = _CWD_RE.search(f.read(8192))
    except OSError:
        return None
    if not m:
        return None
    cwd = m.group(1).decode().replace("\\/", "/")
    return cwd.replace(HOME, "~", 1) if cwd.startswith(HOME) else cwd


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
                    with open(tf) as f:
                        title = (json.load(f) or {}).get("customTitle")
                except Exception:
                    pass
            out.append({
                "id": sid, "path": path, "proj": proj,
                "cwd": read_cwd(path) or proj,
                "title": title or f"(sem nome) {sid[:8]}",
                "named": bool(title), "mtime": st.st_mtime,
            })
    out.sort(key=lambda c: -c["mtime"])
    return out


def pick_chat(chats, want=None):
    if want:
        low = want.lower()
        hits = [c for c in chats if low in c["title"].lower() or low in c["id"]]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            print(paint(f"Nenhum chat com '{want}'.", "red"))
            sys.exit(1)
        chats = hits

    show = chats[:25]
    print(paint("\nEscolha o chat:\n", "bold"))
    for i, c in enumerate(show, 1):
        age = human_ago(time.time() - c["mtime"])
        tag = paint(c["title"], "cyan") if c["named"] else paint(c["title"], "grey")
        print(f"  {i:>2}. {tag}  {paint(age + ' atras', 'dim')}")
        print(f"      {paint(c['cwd'], 'grey')}")
    while True:
        try:
            raw = input(paint("\nnumero > ", "bold")).strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)
        if raw.isdigit() and 1 <= int(raw) <= len(show):
            return show[int(raw) - 1]
        print(paint("numero invalido", "red"))


# ---------- contexto (transcript) ----------

def read_tail_lines(path):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read()
    lines = data.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]
    return lines


_MODEL_RE = re.compile(rb'"modelId":"([^"]+)","marketingName":"([^"]*)"')
_model_cache = {}


def detect_model(path):
    """Ultimo modelo declarado no transcript. Varre o arquivo todo, com cache."""
    try:
        st = os.stat(path)
    except OSError:
        return ("", "")
    key = (path, st.st_size)
    hit = _model_cache.get(path)
    if hit and hit[0] == key and time.time() - hit[1] < 30:
        return hit[2]
    found = ("", "")
    try:
        with open(path, "rb") as f:
            for m in _MODEL_RE.finditer(f.read()):
                found = (m.group(1).decode(), m.group(2).decode())
    except OSError:
        pass
    _model_cache[path] = (key, time.time(), found)
    return found


def scan_context(path):
    """Devolve o estado do contexto lendo o fim do transcript."""
    res = {"used": 0, "limit": 200_000, "model": "?", "model_name": "",
           "last_out": 0, "last_new": 0, "last_ts": None, "turns": 0,
           "effort": ""}
    mid, mname = detect_model(path)
    if mid:
        res["model"], res["model_name"] = mid, mname
    try:
        lines = read_tail_lines(path)
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
            att = d.get("attachment") or {}
            ident = att.get("identity") or {}
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
    if res["used"] > res["limit"]:          # rede de seguranca
        res["limit"] = 1_000_000
    return res


# ---------- usage (limites do plano) ----------

WINDOWS = (
    ("five_hour", "Sessao (5h)"),
    ("seven_day", "Semana (7d)"),
    ("seven_day_opus", "Semana Opus"),
    ("seven_day_sonnet", "Semana Sonnet"),
)


def _keychain_raw(service, account):
    """Le uma senha do Keychain do macOS. Devolve o texto cru ou None."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _oauth_token(raw):
    try:
        return ((json.loads(raw) or {}).get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def read_token():
    """Token OAuth do Claude Code: Keychain primeiro, arquivo depois."""
    user = getpass.getuser()
    for service in KEYCHAIN_SERVICES:
        for acct in (user, ""):
            raw = _keychain_raw(service, acct)
            if raw:
                tok = _oauth_token(raw)
                if tok:
                    return tok
    try:
        with open(CREDS) as f:
            return _oauth_token(f.read())
    except Exception:
        return None


def fetch_usage_api():
    """Busca a utilizacao ao vivo. Devolve (dict_de_janelas, erro)."""
    tok = read_token()
    if not tok:
        return None, "sem token (faca login no Claude Code)"
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "ccwatch",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "token expirado (abra o Claude Code)"
        return None, "HTTP %s" % e.code
    except Exception:
        return None, "sem rede"
    if not isinstance(data, dict):
        return None, "resposta invalida"
    util = data.get("utilization")
    return (util if isinstance(util, dict) else data), None


_live = {"util": None, "at": 0.0, "err": None}
_live_lock = threading.Lock()


def usage_worker():
    """Roda em segundo plano e mantem os limites frescos."""
    while True:
        util, err = fetch_usage_api()
        with _live_lock:
            if util is not None:
                _live["util"], _live["at"], _live["err"] = util, time.time(), None
            else:
                _live["err"] = err
        time.sleep(USAGE_REFRESH)


def start_usage_worker():
    threading.Thread(target=usage_worker, daemon=True).start()


def build_windows(util):
    out = []
    for key, label in WINDOWS:
        w = util.get(key)
        if not isinstance(w, dict) or w.get("utilization") is None:
            continue
        out.append({
            "label": label,
            "pct": float(w["utilization"]),
            "resets_at": w.get("resets_at"),
        })
    return out


def scan_usage(cfg):
    with _live_lock:
        util, at, err = _live["util"], _live["at"], _live["err"]
    if util is not None:
        return {"age": time.time() - at, "windows": build_windows(util),
                "live": True, "err": err}
    cu = cfg.get("cachedUsageUtilization") or {}
    age = time.time() - cu["fetchedAtMs"] / 1000.0 if cu.get("fetchedAtMs") else None
    return {"age": age, "windows": build_windows(cu.get("utilization") or {}),
            "live": False, "err": err}


# ---------- formatacao ----------

def human_ago(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}min"
    if sec < 86400:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}"
    return f"{sec // 86400}d"


def human_until(iso):
    if not iso:
        return "-"
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return "-"
    delta = (t - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "agora"
    return f"em {human_ago(delta)}"


def pct_color(p, warn, stop):
    return "green" if p < warn else ("yellow" if p < stop else "red")


def bar(pct, width=34, color="green"):
    pct = max(0.0, min(100.0, pct))
    fill = int(round(pct / 100 * width))
    return paint("█" * fill, color) + paint("░" * (width - fill), "grey")


def k(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


# ---------- render ----------

def render(chat, acct, ctx, usg):
    L = []
    W = 62
    L.append(paint("┌" + "─" * W + "┐", "grey"))
    head = f" {chat['title']} "
    L.append(paint("│", "grey") + paint(head.ljust(W), "bold") + paint("│", "grey"))
    sub = f" {acct['email']}  {acct['org']}"
    L.append(paint("│", "grey") + paint(sub[:W].ljust(W), "dim") + paint("│", "grey"))
    L.append(paint("└" + "─" * W + "┘", "grey"))
    L.append("")

    # --- contexto ---
    used, limit = ctx["used"], ctx["limit"]
    cpct = used / limit * 100 if limit else 0
    ccol = pct_color(cpct, 60, 80)
    model = ctx["model_name"] or ctx["model"]
    L.append(paint("CONTEXTO", "bold") + paint(f"   {model}", "dim"))
    L.append(f"  {bar(cpct, 34, ccol)} {paint(f'{cpct:5.1f}%', ccol)}")
    L.append(paint(f"  {k(used)} / {k(limit)} tokens    livre: {k(max(0, limit - used))}", "dim"))
    age = human_ago(time.time() - _ts_epoch(ctx["last_ts"])) + " atras" if ctx["last_ts"] else "-"
    L.append(paint(f"  ultimo turno: +{k(ctx['last_new'])} in / {k(ctx['last_out'])} out   ({age})", "dim"))
    L.append("")

    # --- usage ---
    L.append(paint("LIMITES DO PLANO", "bold"))
    if not usg["windows"]:
        L.append(paint("  sem dados em cache ainda", "grey"))
    worst = 0.0
    for w in usg["windows"]:
        p = w["pct"]
        worst = max(worst, p)
        col = pct_color(p, 70, 90)
        L.append(f"  {w['label']:<13} {bar(p, 22, col)} {paint(f'{p:5.1f}%', col)}"
                 + paint(f"  reset {human_until(w['resets_at'])}", "dim"))
    if usg["live"]:
        L.append(paint(f"  ao vivo · buscado ha {human_ago(usg['age'])}", "green"))
    elif usg["age"] is not None:
        stale = usg["age"] > 300
        L.append(paint(f"  cache do /usage: {human_ago(usg['age'])} atras"
                       + ("  (rode /usage no Claude Code p/ atualizar)" if stale else ""),
                       "yellow" if stale else "dim"))
    if not usg["live"]:
        L.append(paint(f"  ao vivo indisponivel: {usg['err']}", "yellow")
                 if usg["err"] else paint("  ao vivo: buscando...", "dim"))
    L.append("")

    # --- conta e modelo ---
    L.append(paint("CONTA E MODELO", "bold"))
    L.append(f"  conta   {acct['email']}" + paint(f"  {acct['org']} {acct['plan']}".rstrip(), "dim"))
    L.append(f"  modelo  {model}")
    L.append(f"  effort  {ctx['effort'] or '-'}")
    L.append("")

    # --- veredito ---
    L.append(paint("VEREDITO", "bold"))
    for line in verdict(cpct, worst, limit - used):
        L.append("  " + line)
    L.append("")
    L.append(paint(f"  atualiza a cada {int(REFRESH)}s   ctrl+c para sair", "grey"))
    return "\n".join(L)


def _ts_epoch(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def verdict(cpct, upct, free):
    out = []
    if cpct >= 80:
        out.append(paint("PARE. Renove o contexto (/compact ou chat novo).", "red"))
    elif cpct >= 60:
        out.append(paint("Contexto cheio pela metade. Planeje renovar em breve.", "yellow"))
    else:
        out.append(paint("Contexto OK. Pode seguir.", "green"))

    if upct >= 90:
        out.append(paint("Limite do plano quase no fim. Espere o reset.", "red"))
    elif upct >= 70:
        out.append(paint("Limite do plano subindo. Evite tarefa pesada.", "yellow"))
    else:
        out.append(paint("Limite do plano OK.", "green"))

    heavy = cpct < 65 and upct < 75 and free > 120_000
    out.append(paint("Tarefa pesada: PODE.", "green") if heavy
               else paint("Tarefa pesada: NAO agora.", "yellow"))
    return out


# ---------- loop ----------

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    live = "--no-live" not in sys.argv[1:]
    cfg = load_cfg()
    acct = account(cfg)
    print(paint(f"\nccwatch  ·  logado como {acct['email']}"
                + (f"  ({acct['org']})" if acct["org"] else ""), "bold"))

    chats = list_chats()
    if not chats:
        print(paint("Nenhum chat encontrado em ~/.claude/projects", "red"))
        return 1
    chat = pick_chat(chats, args[0] if args else None)

    if live:
        start_usage_worker()

    sys.stdout.write("\033[?25l")  # esconde cursor
    try:
        while True:
            cfg = load_cfg()
            frame = render(chat, account(cfg), scan_context(chat["path"]), scan_usage(cfg))
            sys.stdout.write("\033[H\033[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(REFRESH)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
