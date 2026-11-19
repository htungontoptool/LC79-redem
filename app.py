import os
import re
import json
import time
import random
import base64
import hashlib
import asyncio
import logging
import threading
import concurrent.futures
from datetime import datetime

import requests
from flask import Flask, jsonify, request, send_from_directory
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

# ========================= CẤU HÌNH =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")
SESSION_FILE = os.path.join(DATA_DIR, "tele_session.txt")

TELETHON_API_ID = int(os.environ.get("TELETHON_API_ID", "0") or 0)
TELETHON_API_HASH = os.environ.get("TELETHON_API_HASH", "").strip()
TELETHON_SESSION_ENV = os.environ.get("TELETHON_SESSION", "").strip()
PORT = int(os.environ.get("PORT", "10000"))

DEFAULT_CHANNEL_ID = -1002237214360
DEFAULT_CHANNEL_LINK = "https://t.me/codelaucuagame"
DEFAULT_CHANNEL_NAME = "Lẩu cua game - LC79"
BALANCE_INTERVAL = 600  # giây

API_GET_ACCESS = "https://apifo88daigia.tele68.com/api"
LOGIN_URL = "https://wlb.tele68.com/v1/lobby/auth/login"
REDEEM_URL = "https://gameapi.tele68.com/v1/giftcode/redemption?cp=R&cl=R&pf=web&at="
CONNECT_TIMEOUT, READ_TIMEOUT = 3, 10
DONE_KEYWORDS = ("hết lượt", "đã sử dụng", "đã nhận", "không tồn tại", "hết hạn")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lc79")
logging.getLogger("telethon").setLevel(logging.WARNING)

lock = threading.RLock()

# ---- Thống kê vận hành (reset khi khởi động lại server) ----
stats = {
    "boot_at": time.time(),
    "codes_sent": 0, "last_code": "", "last_code_time": "",
    "req_total": 0, "req_ok": 0, "req_err": 0, "lat_sum": 0.0,
    "proxy_current": "", "ping_ms": None, "ping_ok": None,
    "ping_ip": "", "ping_at": "", "ping_err": "",
}


def now_str():
    return datetime.now().strftime("%d/%m %H:%M:%S")


def record_request(status, ms):
    with lock:
        stats["req_total"] += 1
        stats["lat_sum"] += ms
        if status == "SUCCESS":
            stats["req_ok"] += 1
        if status in ("NET_ERR", "PROXY_ERR", "TIMEOUT"):
            stats["req_err"] += 1


def mask_proxy(uri):
    """Ẩn tài khoản/mật khẩu proxy, chỉ hiện host:port."""
    host = uri.split("@")[-1] if "@" in uri else uri.split("//")[-1]
    return host.rstrip("/")


def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.error("load %s: %s", path, e)
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        log.exception("save %s: %s", path, e)


state = load_json(STATE_FILE, {})
state.setdefault("accounts", [])
state.setdefault("enabled", False)
state.setdefault("proxy_url", "")
state.setdefault("proxy_list", [])
state.setdefault("burst", 10)
if state.get("channel_id") is None:
    state["channel_id"] = DEFAULT_CHANNEL_ID
    state["channel_name"] = DEFAULT_CHANNEL_NAME
    state["channel_link"] = DEFAULT_CHANNEL_LINK
state.setdefault("channel_name", DEFAULT_CHANNEL_NAME)
state.setdefault("channel_link", DEFAULT_CHANNEL_LINK)
logs = load_json(LOGS_FILE, [])
accounts = {}  # username lower -> Account


def save_state():
    with lock:
        save_json(STATE_FILE, state)


def save_accounts():
    with lock:
        state["accounts"] = [
            {"un": a.un, "pw": a.pw, "nick": a.nick, "balance": a.balance} for a in accounts.values()
        ]
        save_state()


def add_log(kind, **kw):
    entry = {"time": datetime.now().strftime("%d/%m/%Y %H:%M:%S"), "kind": kind, **kw}
    with lock:
        logs.append(entry)
        del logs[:-300]
        save_json(LOGS_FILE, logs)
    log.info("%s %s", kind, kw)


# ========================= HASH & PROXY =========================
def normalize_pw(pw):
    pw = pw.strip()
    if len(pw) == 32 and all(c in "0123456789abcdef" for c in pw.lower()):
        return pw.lower()
    return hashlib.md5(pw.encode()).hexdigest()


def parse_jwt_exp(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
    except Exception:
        return 0


def parse_proxy_line(line):
    line = line.strip().rstrip("/")
    if not line or line.startswith("#"):
        return None
    if line.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return {"http": line, "https": line}
    if "@" in line:
        uri = f"http://{line}"
        return {"http": uri, "https": uri}
    parts = line.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        uri = f"http://{user}:{pwd}@{ip}:{port}"
        return {"http": uri, "https": uri}
    if len(parts) == 2:
        uri = f"http://{parts[0]}:{parts[1]}"
        return {"http": uri, "https": uri}
    return None


def fetch_proxies(src):
    src = src.strip()
    if src.startswith(("http://", "https://")) and ("/api/" in src or src.endswith(".txt")):
        r = requests.get(src, timeout=15)
        r.raise_for_status()
        lines = r.text.splitlines()
    else:
        lines = src.splitlines()
    return [l.strip() for l in lines if parse_proxy_line(l)]


def has_proxy():
    return bool(state.get("proxy_list"))


def random_proxy():
    """Bắt buộc proxy: không có proxy thì không gửi request nào ra ngoài."""
    plist = state.get("proxy_list") or []
    if not plist:
        return None  # không có proxy: đi trực tiếp
    proxy = parse_proxy_line(random.choice(plist))
    if not proxy:
        return None
    stats["proxy_current"] = mask_proxy(proxy["http"])
    return proxy


# ========================= TÀI KHOẢN =========================
class Account:
    def __init__(self, un, pw, nick, balance=0):
        self.un = un
        self.pw = pw
        self.nick = nick
        self.balance = int(balance or 0)
        self.jwt = None
        self.jwt_exp = 0
        self.last_check = 0
        self.session = requests.Session()
        self.session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=30, pool_maxsize=30))

    def is_token_valid(self):
        return self.jwt is not None and time.time() < (self.jwt_exp - 60)

    @staticmethod
    def _headers(auth=None):
        h = {
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0",
            "origin": "https://lc79.to",
            "referer": "https://lc79.to/",
            "accept": "application/json, text/plain, */*",
        }
        if auth:
            h["authorization"] = f"Bearer {auth}"
        return h

    def get_access_token(self):
        try:
            r = self.session.get(
                API_GET_ACCESS,
                params={"c": "3", "un": self.un, "pw": self.pw, "cp": "R", "cl": "R", "pf": "web", "at": ""},
                headers=self._headers(),
                proxies=random_proxy(),
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            data = r.json()
            return data.get("accessToken") if data.get("success") else None
        except Exception as e:
            log.error("[%s] access token: %s", self.nick, e)
            return None

    def refresh_token(self):
        at = self.get_access_token()
        self.last_check = time.time()
        if not at:
            return False
        try:
            r = self.session.post(
                LOGIN_URL,
                headers={"content-type": "application/json", "origin": "https://lc79b.bet",
                         "referer": "https://lc79b.bet/", "user-agent": "Mozilla/5.0"},
                json={"nickName": self.nick, "accessToken": at},
                proxies=random_proxy(),
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            data = r.json()
            tok = data.get("token")
            if not tok:
                return False
            self.jwt = tok
            self.jwt_exp = parse_jwt_exp(tok)
            self.balance = int(data.get("remoteLoginResp", {}).get("money", self.balance) or self.balance)
            save_accounts()
            return True
        except Exception as e:
            log.error("[%s] refresh: %s", self.nick, e)
            return False

    def single_request(self, code):
        t0 = time.time()
        res = self._single_request_raw(code)
        record_request(res[0], (time.time() - t0) * 1000)
        return res

    def _single_request_raw(self, code):
        try:
            r = self.session.post(
                REDEEM_URL,
                headers=self._headers(self.jwt),
                json={"code": code.strip(), "captcha": ""},
                proxies=random_proxy(),
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            raw = r.text[:200] if r.text else ""
            try:
                data = r.json()
            except Exception:
                data = {}
            if r.status_code in (200, 201):
                amt = (data.get("money") or data.get("amount") or data.get("reward")
                       or (data.get("data") or {}).get("money") or 0)
                try:
                    amt = int(amt)
                except Exception:
                    amt = 0
                msg = data.get("message") or data.get("msg") or "Đổi code thành công"
                return "SUCCESS", amt, f"HTTP {r.status_code}: {msg}"
            if r.status_code == 401:
                return "TOKEN_EXPIRED", 0, "HTTP 401: Token hết hạn"
            err = data.get("message") or data.get("msg") or data.get("error") or raw or f"HTTP {r.status_code}"
            return "FAIL", 0, f"HTTP {r.status_code}: {err}"
        except requests.exceptions.ProxyError:
            return "PROXY_ERR", 0, "Lỗi kết nối Proxy"
        except requests.exceptions.Timeout:
            return "TIMEOUT", 0, "Quá thời gian phản hồi"
        except Exception as e:
            return "NET_ERR", 0, f"Lỗi mạng: {str(e)[:80]}"


def login_and_get_info(u, p):
    try:
        pw_hashed = normalize_pw(p)
        proxy = random_proxy()
        r = requests.get(
            f"{API_GET_ACCESS}?c=3&un={u}&pw={pw_hashed}&cp=R&cl=R&pf=web&at=",
            proxies=proxy, timeout=10,
        ).json()
        if not r.get("success"):
            return {"_error": "Sai tài khoản hoặc mật khẩu"}
        sk = r["sessionKey"] + "=" * ((4 - len(r["sessionKey"]) % 4) % 4)
        nn = json.loads(base64.b64decode(sk)).get("nickname")
        r2 = requests.post(
            "https://wlb.tele68.com/v1/lobby/auth/login?cp=R&cl=R&pf=web&at=",
            json={"nickName": nn, "accessToken": r["accessToken"]},
            proxies=proxy, timeout=10,
        ).json()
        if not r2.get("token"):
            return {"_error": "Không nhận được token, tài khoản có thể đang kẹt phiên"}
        return {
            "token": r2["token"],
            "nickname": nn,
            "money": int(r2.get("remoteLoginResp", {}).get("money", 0) or 0),
            "pw_hashed": pw_hashed,
        }
    except json.JSONDecodeError:
        return {"_error": "Máy chủ LC79 không trả dữ liệu qua proxy này (proxy có thể bị chặn hoặc chết). Hãy đo ping proxy, thử proxy khác rồi thêm lại tài khoản."}
    except Exception as e:
        return {"_error": str(e)}


# ========================= BẮN CODE =========================
class Run:
    def __init__(self, code, accs):
        self.code = code
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.attempts = 0
        self.started = time.time()
        self.baseline = {a.un: a.balance for a in accs}  # số dư trước khi bắn
        self.wins = set()  # acc đã được ghi nhận trúng trong lượt này


current_run = None
run_lock = threading.Lock()


def redeem_account(acc, run, burst):
    if not acc.is_token_valid() and not acc.refresh_token():
        add_log("LOGIN_FAIL", code=run.code, username=acc.un, nick=acc.nick, detail="Không lấy được token")
        return

    before = acc.balance
    result = {"win": None, "last": "Chưa có phản hồi"}
    mu = threading.Lock()
    refreshing = threading.Lock()

    def worker():
        while not run.stop.is_set():
            st, amt, detail = acc.single_request(run.code)
            with run.lock:
                run.attempts += 1
            with mu:
                result["last"] = detail
            if st == "SUCCESS":
                with mu:
                    if result["win"] is None:
                        result["win"] = (amt, detail)
                run.stop.set()
                return
            if st == "TOKEN_EXPIRED" and refreshing.acquire(blocking=False):
                try:
                    acc.refresh_token()
                finally:
                    refreshing.release()
            if any(k in detail.lower() for k in DONE_KEYWORDS):
                return
            time.sleep(0.05)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(int(burst), 30)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    if result["win"]:
        amt_raw, detail = result["win"]
        acc.refresh_token()
        after = acc.balance
        total = amt_raw or max(0, after - before)
        with run.lock:
            run.wins.add(acc.un)
        add_log("WIN", code=run.code, username=acc.un, nick=acc.nick, amount=total,
                before=before, after=after, detail=detail)
    else:
        kind = "STOPPED" if run.stop.is_set() else "FAIL"
        add_log(kind, code=run.code, username=acc.un, nick=acc.nick, amount=0,
                before=before, after=acc.balance, detail=result["last"])


def _run_worker(run, accs):
    burst = int(state.get("burst") or 10)
    prep = [threading.Thread(target=a.refresh_token, daemon=True) for a in accs if not a.is_token_valid()]
    for t in prep:
        t.start()
    for t in prep:
        t.join(timeout=8)
    threads = [threading.Thread(target=redeem_account, args=(a, run, burst), daemon=True) for a in accs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    # Kiểm tra số dư sau lượt: acc nhập được nhưng kênh không báo thì lấy sd mới - sd trước lượt
    for a in accs:
        with run.lock:
            if a.un in run.wins:
                continue
        base = run.baseline.get(a.un, a.balance)
        a.refresh_token()
        diff = a.balance - base
        if diff > 0:
            with run.lock:
                run.wins.add(a.un)
            add_log("WIN", code=run.code, username=a.un, nick=a.nick, amount=diff,
                    before=base, after=a.balance, detail="Kênh không báo, tính theo số dư")


def start_run(code):
    global current_run
    accs = list(accounts.values())
    if not accs:
        add_log("NO_ACC", code=code, detail="Chưa có tài khoản nào")
        return
    with run_lock:
        if current_run and not current_run.stop.is_set():
            current_run.stop.set()
        current_run = Run(code, accs)
        run = current_run
    with lock:
        stats["codes_sent"] += 1
        stats["last_code"] = code
        stats["last_code_time"] = now_str()
    add_log("CODE", code=code, detail=f"Bắn {len(accs)} acc, {state.get('burst', 10)} luồng/acc")
    threading.Thread(target=_run_worker, args=(run, accs), daemon=True).start()


def stop_current_run():
    with run_lock:
        if current_run and not current_run.stop.is_set():
            current_run.stop.set()
            return current_run.code
    return None


def on_channel_winner(nick, reward_str):
    run = current_run
    stopped = stop_current_run()
    reward = int(re.sub(r"\D", "", reward_str or "") or 0)
    matched = next((a for a in list(accounts.values())
                    if a.nick.strip().lower() == nick.strip().lower()), None)
    if matched:
        if run:
            with run.lock:
                run.wins.add(matched.un)

        def task():
            before = matched.balance
            matched.refresh_token()
            after = matched.balance
            amount = max(0, after - before) or reward
            add_log("CHANNEL_WIN", code=stopped or "", username=matched.un, nick=matched.nick,
                    amount=amount, before=before, after=after, detail="Acc trong web trúng, kênh báo")
        threading.Thread(target=task, daemon=True).start()
    else:
        add_log("CHANNEL_WIN", code=stopped or "", username="", nick=nick, amount=reward,
                before=0, after=0, detail="Người khác trên kênh đã lụm, đã dừng bắn")


def balance_loop():
    """Mỗi 10 phút kiểm tra số dư; ghi nhận nếu tăng mà chưa được ghi nhận."""
    while True:
        time.sleep(BALANCE_INTERVAL)
        with run_lock:
            busy = current_run is not None and not current_run.stop.is_set()
        if busy:
            continue
        for acc in list(accounts.values()):
            before = acc.balance
            acc.refresh_token()
            diff = acc.balance - before
            if diff > 0:
                add_log("BALANCE", username=acc.un, nick=acc.nick, amount=diff,
                        before=before, after=acc.balance, detail="Tự kiểm tra 10 phút")


# ========================= TELETHON (chạy trong 1 event loop riêng) =========================
tele_loop = asyncio.new_event_loop()
threading.Thread(target=tele_loop.run_forever, daemon=True).start()

tele = {"client": None, "ready": False, "name": "", "phone": ""}
login = {"step": None, "client": None, "phone": None, "hash": None}


def tcall(coro, timeout=120):
    return asyncio.run_coroutine_threadsafe(coro, tele_loop).result(timeout=timeout)


def extract_clean_code(raw):
    if not raw:
        return None
    m = re.search(r"Mã\s*code\s*là\s*:\s*([A-Z0-9]+)", raw, re.IGNORECASE)
    if not m:
        return None
    code = m.group(1).strip().upper()
    if code.startswith("LC79") and len(code) > 4:
        code = code[4:]
    return code


def extract_winner(raw):
    if not raw:
        return None
    m = re.search(
        r"Chúc\s*mừng\s+([^\s]+)\s+đã\s+đổi\s+thành\s+công\s+Giftcode(?:\s+trị\s+giá\s+([0-9.,]+))?",
        raw, re.IGNORECASE,
    )
    if not m:
        return None
    return m.group(1).strip(), (m.group(2) or "0").strip()


def handle_message(event):
    if not state.get("enabled") or state.get("channel_id") is None:
        return
    if event.chat_id != state["channel_id"]:
        return
    raw = event.raw_text or ""
    winner = extract_winner(raw)
    if winner:
        on_channel_winner(*winner)
        return
    code = extract_clean_code(raw)
    if code:
        start_run(code)


async def _on_msg(event):
    try:
        handle_message(event)
    except Exception:
        log.exception("handle_message lỗi")


async def _attach(client):
    old = tele.get("client")
    if old and old is not client:
        try:
            await old.disconnect()
        except Exception:
            pass
    tele["client"] = client
    client.add_event_handler(_on_msg, events.NewMessage())
    me = await client.get_me()
    tele.update(ready=True, name=f"{me.first_name or ''} {me.last_name or ''}".strip() or "Tele",
                phone=f"+{me.phone}" if getattr(me, "phone", None) else "")
    return me


async def _connect_saved(sess):
    client = TelegramClient(StringSession(sess), TELETHON_API_ID, TELETHON_API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return None
    return client


async def _send_code(phone):
    client = TelegramClient(StringSession(), TELETHON_API_ID, TELETHON_API_HASH)
    await client.connect()
    sent = await client.send_code_request(phone)
    return client, sent.phone_code_hash


async def _sign_in_code(client, phone, code, h):
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=h)
        return "ok"
    except SessionPasswordNeededError:
        return "2fa"


async def _sign_in_pw(client, pw):
    await client.sign_in(password=pw)


async def _cleanup():
    """Xoá mọi chat/nhóm/kênh khác, chỉ giữ kênh nhận code. Không chạy nếu không thấy kênh nhận code."""
    client = tele["client"]
    ch = state["channel_id"]
    dialogs = await client.get_dialogs(limit=None)
    if not any(utils.get_peer_id(d.entity) == ch for d in dialogs):
        return {"skipped": True}
    deleted = kept = errors = 0
    for d in dialogs:
        e = d.entity
        if utils.get_peer_id(e) == ch:
            kept += 1
            continue
        if getattr(e, "is_self", False):
            continue
        try:
            await client.delete_dialog(e)
            deleted += 1
        except Exception:
            errors += 1
    return {"skipped": False, "deleted": deleted, "kept": kept, "errors": errors}


def _finish_login(client):
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        f.write(client.session.save())
    me = tcall(_attach(client))
    login.update(step=None, client=None, phone=None, hash=None)
    add_log("TELE", detail=f"Đã kết nối Tele nguồn: {tele['name']}")
    threading.Thread(target=_cleanup_and_log, daemon=True).start()
    return me


def _cleanup_and_log():
    try:
        res = tcall(_cleanup(), 1200)
        if res.get("skipped"):
            add_log("TELE", detail="Chưa thấy kênh nhận code trong tài khoản này, chưa dọn chat nào")
        else:
            add_log("TELE", detail=f"Đã dọn chat: xoá {res['deleted']}, giữ kênh nhận code, lỗi {res['errors']}")
    except Exception as e:
        add_log("TELE", detail=f"Dọn chat lỗi: {e}")


def load_session_str():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                s = f.read().strip()
                if s:
                    return s
        except Exception:
            pass
    return TELETHON_SESSION_ENV


def boot_tele():
    sess = load_session_str()
    if not sess or not TELETHON_API_ID or not TELETHON_API_HASH:
        log.warning("Chưa có Tele nguồn: đăng nhập trên web")
        return
    try:
        client = tcall(_connect_saved(sess), 60)
        if client:
            tcall(_attach(client), 60)
            log.info("Tele nguồn đã kết nối: %s", tele["name"])
        else:
            log.error("Session Tele không còn hiệu lực, cần đăng nhập lại trên web")
    except Exception as e:
        log.exception("Boot Tele lỗi: %s", e)


# ========================= BOT TELEGRAM: ĐĂNG NHẬP TELE NGUỒN =========================
import telebot

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
tgbot = telebot.TeleBot(BOT_TOKEN) if (BOT_TOKEN and ":" in BOT_TOKEN) else None
tg_flow = {"step": None, "phone": None, "client": None, "hash": None}


def _tg_reset():
    tg_flow.update(step=None, phone=None, client=None, hash=None)


def _tg_is_admin(m):
    return bool(ADMIN_ID) and m.chat.id == ADMIN_ID


if tgbot:
    @tgbot.message_handler(commands=["start", "login"], func=_tg_is_admin)
    def tg_login(m):
        _tg_reset()
        tg_flow["step"] = "phone"
        tgbot.send_message(m.chat.id,
            "Đăng nhập Tele nguồn.\nGửi số điện thoại kèm mã vùng, ví dụ +84912345678.\nGõ /cancel để huỷ.")

    @tgbot.message_handler(commands=["cancel"], func=_tg_is_admin)
    def tg_cancel(m):
        if tg_flow.get("client"):
            try:
                tcall(tg_flow["client"].disconnect(), 10)
            except Exception:
                pass
        _tg_reset()
        tgbot.send_message(m.chat.id, "Đã huỷ đăng nhập.")

    @tgbot.message_handler(commands=["status"], func=_tg_is_admin)
    def tg_status(m):
        tgbot.send_message(m.chat.id, f"Tele nguồn: {'đã kết nối ' + tele['name'] if tele['ready'] else 'chưa kết nối'}")

    @tgbot.message_handler(func=lambda m: _tg_is_admin(m) and tg_flow["step"] in ("phone", "code", "2fa"))
    def tg_flow_step(m):
        text = (m.text or "").strip()
        step = tg_flow["step"]
        try:
            if step == "phone":
                phone = re.sub(r"\s+", "", text)
                client, h = tcall(_send_code(phone), 60)
                tg_flow.update(step="code", phone=phone, client=client, hash=h)
                tgbot.send_message(m.chat.id, "Đã gửi mã OTP vào Telegram. Nhập mã vào đây.")
            elif step == "code":
                code = re.sub(r"\D", "", text)
                res = tcall(_sign_in_code(tg_flow["client"], tg_flow["phone"], code, tg_flow["hash"]), 60)
                if res == "2fa":
                    tg_flow["step"] = "2fa"
                    tgbot.send_message(m.chat.id, "Tài khoản có 2FA. Nhập mật khẩu 2FA.")
                    return
                _finish_login(tg_flow["client"])
                _tg_reset()
                tgbot.send_message(m.chat.id, f"Đăng nhập thành công: {tele['name']}. Đang dọn chat khác, giữ kênh nhận code.")
            elif step == "2fa":
                tcall(_sign_in_pw(tg_flow["client"], text), 60)
                _finish_login(tg_flow["client"])
                _tg_reset()
                tgbot.send_message(m.chat.id, f"Đăng nhập thành công: {tele['name']}. Đang dọn chat khác, giữ kênh nhận code.")
        except concurrent.futures.TimeoutError:
            tgbot.send_message(m.chat.id, "Telegram không phản hồi sau 60 giây. Thử lại sau vài phút bằng /login.")
            _tg_reset()
        except Exception as e:
            log.exception("Bot đăng nhập Tele lỗi")
            tgbot.send_message(m.chat.id, f"Lỗi: {type(e).__name__}: {e or 'không có chi tiết'}. Gõ /login để thử lại.")
            _tg_reset()


def _bot_poll():
    while True:
        try:
            tgbot.polling(none_stop=True, interval=1, timeout=20)
        except Exception as e:
            log.error("Bot Telegram lỗi: %s", e)
            time.sleep(5)


# ========================= FLASK API =========================
app = Flask(__name__, static_folder=None)


def ok(**kw):
    return jsonify(ok=True, **kw)


def err(msg, code=400):
    return jsonify(ok=False, error=msg), code


@app.errorhandler(Exception)
def handle_any_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify(ok=False, error=e.description), e.code
    log.exception("Lỗi server")
    return jsonify(ok=False, error=f"Lỗi server: {type(e).__name__}: {str(e)[:200]}"), 500


@app.get("/__version")
def version():
    return "lc79 v5 · routes: " + ", ".join(sorted(r.rule for r in app.url_map.iter_rules()))


@app.get("/")
def index():
    if not os.path.exists(os.path.join(BASE_DIR, "index.html")):
        files = ", ".join(sorted(os.listdir(BASE_DIR)))
        return (f"<h3>Thiếu index.html</h3><p>Server tìm ở: {BASE_DIR}</p>"
                f"<p>Các file hiện có: {files}</p>"), 500
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/logo.jpg")
def logo():
    return send_from_directory(BASE_DIR, "logo.jpg")


@app.get("/api/state")
def api_state():
    with lock:
        acc_list = [{"un": a.un, "nick": a.nick, "balance": a.balance, "valid": a.is_token_valid()}
                    for a in accounts.values()]
        total = sum(int(l.get("amount") or 0) for l in logs
                    if l.get("kind") in ("WIN", "BALANCE") or (l.get("kind") == "CHANNEL_WIN" and l.get("username")))
        wins = sum(1 for l in logs if l.get("kind") in ("WIN", "CHANNEL_WIN") and l.get("amount"))
        history = list(reversed(logs[-200:]))
    run = current_run
    return jsonify(
        enabled=bool(state.get("enabled")),
        channel={"id": state.get("channel_id"), "name": state.get("channel_name"),
                 "link": state.get("channel_link")},
        tele={"ready": tele["ready"], "name": tele["name"], "phone": tele["phone"],
              "step": login["step"], "pending_phone": login["phone"] or ""},
        accounts=acc_list,
        proxy_count=len(state.get("proxy_list") or []),
        has_proxy=has_proxy(),
        stats=stats_view(),
        proxy_url=state.get("proxy_url") or "",
        burst=state.get("burst") or 10,
        run={"code": run.code, "attempts": run.attempts, "active": not run.stop.is_set()} if run else None,
        history=history,
        totals={"total": total, "wins": wins},
    )


# ---- Tài khoản game ----
@app.post("/api/accounts")
def api_add_account():
    d = request.get_json(force=True, silent=True) or {}
    un = (d.get("username") or "").strip()
    pw = (d.get("password") or "").strip()
    if not un or not pw:
        return err("Nhập đủ tài khoản và mật khẩu")
    res = login_and_get_info(un, pw)
    if "_error" in res:
        return err(res["_error"])
    with lock:
        acc = accounts.get(un.lower())
        if acc is None:
            acc = Account(un, res["pw_hashed"], res["nickname"], res["money"])
            accounts[un.lower()] = acc
        acc.pw = res["pw_hashed"]
        acc.nick = res["nickname"]
        acc.balance = res["money"]
        acc.jwt = res["token"]
        acc.jwt_exp = parse_jwt_exp(res["token"])
        save_accounts()
    add_log("TELE", detail=f"Đã thêm acc {res['nickname']} ({un}), số dư {res['money']:,}")
    return ok(nick=res["nickname"], balance=res["money"])


@app.delete("/api/accounts/<un>")
def api_del_account(un):
    with lock:
        acc = accounts.pop(un.lower(), None)
        if acc is None:
            return err("Không tìm thấy tài khoản", 404)
        save_accounts()
    return ok()


@app.post("/api/accounts/refresh")
def api_refresh_accounts():
    def task():
        for a in list(accounts.values()):
            before = a.balance
            a.refresh_token()
            if a.balance - before > 0:
                add_log("BALANCE", username=a.un, nick=a.nick, amount=a.balance - before,
                        before=before, after=a.balance, detail="Làm mới thủ công")
    threading.Thread(target=task, daemon=True).start()
    return ok(message="Đang làm mới số dư")


# ---- Tele nguồn: đăng nhập ngay trên web ----
@app.post("/api/tele/send_code")
def api_tele_send_code():
    phone = re.sub(r"\s+", "", (request.get_json(force=True, silent=True) or {}).get("phone") or "")
    if not phone:
        return err("Nhập số điện thoại kèm mã vùng, ví dụ +849xxxxxxxx")
    if not TELETHON_API_ID or not TELETHON_API_HASH:
        return err("Server chưa có TELETHON_API_ID / TELETHON_API_HASH")
    try:
        log.info("Gửi OTP tới %s ...", phone[:4] + "***")
        client, h = tcall(_send_code(phone), 60)
    except concurrent.futures.TimeoutError:
        log.error("Gửi OTP: Telegram không phản hồi sau 60 giây")
        return err("Telegram không phản hồi sau 60 giây. Server không kết nối được tới Telegram, thử lại sau vài phút.")
    except Exception as e:
        log.exception("Gửi OTP lỗi")
        return err(f"Gửi OTP lỗi: {type(e).__name__}: {e or 'không có chi tiết'}")
    if login.get("client"):
        try:
            tcall(login["client"].disconnect(), 10)
        except Exception:
            pass
    login.update(step="code", client=client, phone=phone, hash=h)
    return ok(step="code")


@app.post("/api/tele/verify")
def api_tele_verify():
    if login["step"] != "code":
        return err("Chưa gửi OTP")
    code = re.sub(r"\D", "", (request.get_json(force=True, silent=True) or {}).get("code") or "")
    if not code:
        return err("Nhập mã OTP")
    try:
        res = tcall(_sign_in_code(login["client"], login["phone"], code, login["hash"]), 60)
    except Exception as e:
        return err(f"OTP sai hoặc hết hạn: {e}")
    if res == "2fa":
        login["step"] = "2fa"
        return ok(step="2fa")
    try:
        _finish_login(login["client"])
    except Exception as e:
        return err(f"Lưu phiên lỗi: {e}")
    return ok(step=None)


@app.post("/api/tele/2fa")
def api_tele_2fa():
    if login["step"] != "2fa":
        return err("Chưa đến bước 2FA")
    pw = (request.get_json(force=True, silent=True) or {}).get("password") or ""
    if not pw:
        return err("Nhập mật khẩu 2FA")
    try:
        tcall(_sign_in_pw(login["client"], pw), 60)
    except Exception as e:
        return err(f"Sai mật khẩu 2FA: {e}")
    try:
        _finish_login(login["client"])
    except Exception as e:
        return err(f"Lưu phiên lỗi: {e}")
    return ok(step=None)


@app.post("/api/tele/cancel")
def api_tele_cancel():
    if login.get("client"):
        try:
            tcall(login["client"].disconnect(), 10)
        except Exception:
            pass
    login.update(step=None, client=None, phone=None, hash=None)
    return ok()


@app.post("/api/tele/logout")
def api_tele_logout():
    client = tele.get("client")
    if client:
        try:
            tcall(client.disconnect(), 15)
        except Exception:
            pass
    tele.update(client=None, ready=False, name="", phone="")
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    state["enabled"] = False
    save_state()
    add_log("TELE", detail="Đã đăng xuất Tele nguồn, tắt bot")
    return ok()


# ---- Bot, kênh, proxy, dừng ----
@app.post("/api/toggle")
def api_toggle():
    if state.get("enabled"):
        state["enabled"] = False
    else:
        if not tele["ready"]:
            return err("Chưa đăng nhập Tele nguồn")
        if not accounts:
            return err("Chưa có tài khoản nào")
        if state.get("channel_id") is None:
            return err("Chưa đặt kênh nhận code")
        state["enabled"] = True
    save_state()
    add_log("TELE", detail="Bật bot" if state["enabled"] else "Tắt bot")
    return ok(enabled=state["enabled"])


@app.post("/api/stop")
def api_stop():
    code = stop_current_run()
    if code:
        add_log("STOPPED", code=code, username="", nick="", amount=0, detail="Dừng thủ công")
        return ok(message=f"Đã dừng bắn {code}")
    return ok(message="Không có lượt bắn nào đang chạy")


@app.post("/api/channel")
def api_channel():
    target = ((request.get_json(force=True, silent=True) or {}).get("target") or "").strip()
    if not target:
        return err("Nhập kênh")
    if "t.me/" in target:
        target = target.split("t.me/")[-1].strip("/").split("?")[0]
    target = int(target) if target.lstrip("-").isdigit() else (target if target.startswith("@") else "@" + target)
    if not tele["ready"]:
        return err("Cần đăng nhập Tele nguồn trước")
    try:
        ent = tcall(tele["client"].get_entity(target), 20)
    except Exception as e:
        return err(f"Không tìm thấy kênh: {e}")
    state.update(channel_id=utils.get_peer_id(ent),
                 channel_name=getattr(ent, "title", None) or str(target),
                 channel_link=f"https://t.me/{getattr(ent, 'username', '')}" if getattr(ent, "username", None) else "")
    save_state()
    return ok(name=state["channel_name"])


@app.post("/api/proxy")
def api_proxy():
    d = request.get_json(force=True, silent=True) or {}
    src = (d.get("source") or "").strip()
    try:
        burst = max(1, min(int(d.get("burst") or 10), 30))
    except ValueError:
        return err("Số luồng phải là số nguyên 1–30")
    if not src:
        return err("Nhập link hoặc proxy")
    try:
        plist = fetch_proxies(src)
    except Exception as e:
        return err(f"Lỗi tải proxy: {e}")
    if not plist:
        return err("Không tìm thấy proxy hợp lệ")
    with lock:
        state.update(proxy_url=src, proxy_list=plist, burst=burst)
        save_state()
    return ok(count=len(plist))


@app.post("/api/proxy/clear")
def api_proxy_clear():
    with lock:
        state.update(proxy_url="", proxy_list=[])
        save_state()
    stop_current_run()
    add_log("TELE", detail="Đã xoá proxy")
    return ok()


def ping_proxy():
    """Đo độ trễ qua proxy hiện tại tới một endpoint công khai."""
    if not has_proxy():
        with lock:
            stats.update(ping_ms=None, ping_ok=False, ping_err="Chưa có proxy", ping_at=now_str())
        return
    t0 = time.time()
    try:
        proxy = random_proxy()
        r = requests.get("https://ipv4.webshare.io/", proxies=proxy, timeout=10)
        ms = round((time.time() - t0) * 1000)
        with lock:
            stats.update(ping_ms=ms, ping_ok=True, ping_ip=r.text.strip()[:45],
                         ping_at=now_str(), ping_err="")
    except Exception as e:
        with lock:
            stats.update(ping_ms=None, ping_ok=False, ping_err=str(e)[:90], ping_at=now_str())


def ping_loop():
    while True:
        try:
            ping_proxy()
        except Exception:
            pass
        time.sleep(60)


def stats_view():
    with lock:
        s = dict(stats)
    total = s["req_total"]
    return {
        "uptime_s": int(time.time() - s["boot_at"]),
        "codes_sent": s["codes_sent"],
        "last_code": s["last_code"],
        "last_code_time": s["last_code_time"],
        "req_total": total,
        "req_ok": s["req_ok"],
        "req_err": s["req_err"],
        "success_rate": round(s["req_ok"] * 100 / total, 1) if total else 0,
        "avg_ms": round(s["lat_sum"] / total) if total else 0,
        "proxy_current": s["proxy_current"],
        "ping_ms": s["ping_ms"],
        "ping_ok": s["ping_ok"],
        "ping_ip": s["ping_ip"],
        "ping_at": s["ping_at"],
        "ping_err": s["ping_err"],
    }


@app.post("/api/proxy/ping")
def api_proxy_ping():
    if not has_proxy():
        return err("Chưa có proxy")
    threading.Thread(target=ping_proxy, daemon=True).start()
    return ok(message="Đang đo ping proxy")


def boot():
    for item in state.get("accounts", []):
        un = str(item.get("un") or "").strip()
        pw = str(item.get("pw") or "").strip()
        if un and pw:
            accounts[un.lower()] = Account(un, pw, str(item.get("nick") or un), item.get("balance", 0))
    boot_tele()
    threading.Thread(target=balance_loop, daemon=True).start()
    threading.Thread(target=ping_loop, daemon=True).start()
    if tgbot:
        threading.Thread(target=_bot_poll, daemon=True).start()
        log.info("Bot Telegram đã chạy: nhắn /login để đăng nhập Tele nguồn")
    else:
        log.warning("Chưa có BOT_TOKEN: không bật được đăng nhập Tele qua bot")
    threading.Thread(target=lambda: [a.refresh_token() for a in list(accounts.values())], daemon=True).start()


if __name__ == "__main__":
    boot()
    log.info("LC79 web chạy tại cổng %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True, use_reloader=False)