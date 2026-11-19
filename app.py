import telebot
import requests
import hashlib
import base64
import json
import logging
import threading
import random
import time
import os
import re
import io
import html
import asyncio
from flask import Flask
from datetime import datetime
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession

# ========================= FLASK KEEPALIVE =========================
app = Flask(__name__)


@app.route("/")
def home():
    return "✨ LỤM CODE LC79 - ONLINE ✨"


@app.route("/health")
def health():
    return {"status": "online", "bot": "Lum code LC79"}


def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ========================= CONFIG & LOGGING =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("telethon").setLevel(logging.WARNING)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
SUPPORT_HANDLE = "@htungdzvipmax"
TELETHON_API_ID = int(os.environ.get("TELETHON_API_ID", "0"))
TELETHON_API_HASH = os.environ.get("TELETHON_API_HASH", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

bot = None
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    logger.error("❌ LỖI: BOT_TOKEN chưa có hoặc sai định dạng.")
else:
    try:
        bot = telebot.TeleBot(BOT_TOKEN)
        bot.set_my_commands([
            telebot.types.BotCommand("start", "✨ Mở menu chính"),
            telebot.types.BotCommand("menu", "🏠 Về màn hình chính"),
        ])
        logger.info("✅ Kết nối Telegram Bot OK!")
    except Exception as e:
        logger.error(f"❌ LỖI TOKEN TELEGRAM: {e}")
        bot = None

if not bot:
    class DummyBot:
        def __getattr__(self, name):
            return lambda *a, **kw: None
    bot = DummyBot()

REDEEM_STATE_FILE = os.path.join(DATA_DIR, "redeem_state.json")
REDEEM_LOGS_FILE = os.path.join(DATA_DIR, "redeem_logs.json")
GAME_ACCOUNTS_FILE = os.path.join(DATA_DIR, "game_accounts.json")
TELE_SESSION_FILE = os.path.join(DATA_DIR, "tele_session.txt")


def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Load {path}: {e}")
    return default


def save_json(path, data):
    """Ghi JSON an toàn qua file tạm."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        logger.exception(f"Save {path} failed: {e}")
        try:
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")
        except Exception:
            pass
        return False


redeem_state = load_json(REDEEM_STATE_FILE, {
    "accounts": [],
    "channel_id": None,
    "channel_name": "",
    "enabled": False,
    "proxy_url": "",
    "proxy_list": [],
    "burst_count": 10
})
redeem_state.setdefault("proxy_url", "")
redeem_state.setdefault("proxy_list", [])
redeem_state.setdefault("burst_count", 10)

redeem_logs = load_json(REDEEM_LOGS_FILE, [])
game_accounts = load_json(GAME_ACCOUNTS_FILE, {})

# Quản lý trạng thái dừng khẩn cấp khi code đã có người nhập
active_redeem_stop_event = threading.Event()
active_redeem_code = None
active_redeem_lock = threading.Lock()


def md5_hash(t):
    return hashlib.md5(t.encode()).hexdigest()


def normalize_pw(pw, force_mode=None):
    pw = pw.strip()
    if force_mode == "md5":
        return pw.lower()
    if force_mode == "plain":
        return md5_hash(pw)
    if len(pw) == 32 and all(c in "0123456789abcdef" for c in pw.lower()):
        return pw.lower()
    return md5_hash(pw)


# ========================= HỆ THỐNG PROXY WEBSHARE.IO =========================
def parse_proxy_line(line):
    """Chuyển đổi các định dạng proxy (http://user:pass@ip:port/, ip:port:user:pass, user:pass@ip:port) sang dict cho requests."""
    line = line.strip().rstrip("/")
    if not line or line.startswith("#"):
        return None
    if line.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return {"http": line, "https": line}
    if "@" in line:
        proxy_uri = f"http://{line}"
        return {"http": proxy_uri, "https": proxy_uri}
    parts = line.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        proxy_uri = f"http://{user}:{pwd}@{ip}:{port}"
        return {"http": proxy_uri, "https": proxy_uri}
    if len(parts) == 2:
        ip, port = parts
        proxy_uri = f"http://{ip}:{port}"
        return {"http": proxy_uri, "https": proxy_uri}
    return None


def fetch_webshare_proxies(source_input):
    """Tải danh sách proxy từ link Webshare.io hoặc phân tích trực tiếp proxy được dán vào."""
    source_input = source_input.strip()
    if parse_proxy_line(source_input) and "/api/" not in source_input and not source_input.endswith(".txt"):
        return [source_input]
    resp = requests.get(source_input, timeout=15)
    resp.raise_for_status()
    proxies = []
    for raw_line in resp.text.splitlines():
        parsed = parse_proxy_line(raw_line)
        if parsed:
            proxies.append(raw_line.strip())
    return proxies


def get_random_proxy_dict():
    plist = redeem_state.get("proxy_list") or []
    if not plist:
        return None
    raw = random.choice(plist)
    return parse_proxy_line(raw)


# ========================= REDEEM & BALANCE SYNC SYSTEM =========================
def append_redeem_log(code, account, bal_before, bal_after, amount, status, detail=""):
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": code,
        "account": account,
        "balance_before": int(bal_before or 0),
        "balance_after": int(bal_after or 0),
        "amount": int(amount or 0),
        "status": status,
        "detail": detail,
    }
    redeem_logs.append(entry)
    save_json(REDEEM_LOGS_FILE, redeem_logs)
    logger.info(f"[REDEEM] {code} | {account} | {status} | {detail} | amount={amount}")


def sync_balance_to_storage(username, new_balance):
    """Đồng bộ số dư mới nhất vào redeem_state.json và game_accounts.json."""
    updated_state = False
    for item in redeem_state.get("accounts", []):
        if str(item.get("un", "")).lower() == str(username).lower():
            item["balance"] = int(new_balance)
            updated_state = True
    if updated_state:
        save_json(REDEEM_STATE_FILE, redeem_state)

    updated_game = False
    for k, info in game_accounts.items():
        if str(info.get("username", "")).lower() == str(username).lower():
            info["money"] = int(new_balance)
            updated_game = True
    if updated_game:
        save_json(GAME_ACCOUNTS_FILE, game_accounts)


class RedeemAccount:
    CONNECT_TIMEOUT = 3
    READ_TIMEOUT = 10
    API_GET_ACCESS = "https://apifo88daigia.tele68.com/api"
    LOGIN_URL = "https://wlb.tele68.com/v1/lobby/auth/login"
    BASE_URL = "https://gameapi.tele68.com"
    PARAMS = "/v1/giftcode/redemption?cp=R&cl=R&pf=web&at="

    def __init__(self, un, pw, nick, owner=None, balance=0):
        self.un = un
        self.pw = pw
        self.nick = nick
        self.owner = owner
        self.jwt = None
        self.jwt_exp = 0
        self.balance = int(balance or 0)
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=30, pool_maxsize=30)
        self.session.mount("https://", adapter)

    def _headers(self, auth=None):
        h = {
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0",
            "origin": "https://lc79.to",
            "referer": "https://lc79.to/",
            "accept": "application/json, text/plain, */*",
            "connection": "keep-alive",
        }
        if auth:
            h["authorization"] = f"Bearer {auth}"
        return h

    @staticmethod
    def parse_jwt_exp(token):
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
        except Exception:
            return 0

    def is_token_valid(self):
        return self.jwt is not None and time.time() < (self.jwt_exp - 60)

    def get_access_token(self):
        try:
            proxy = get_random_proxy_dict()
            res = self.session.get(
                self.API_GET_ACCESS,
                params={"c": "3", "un": self.un, "pw": self.pw, "cp": "R", "cl": "R", "pf": "web", "at": ""},
                headers=self._headers(),
                proxies=proxy,
                timeout=(self.CONNECT_TIMEOUT, self.READ_TIMEOUT),
            )
            data = res.json()
            return data.get("accessToken") if data.get("success") else None
        except Exception as e:
            logger.error(f"[{self.nick}] get_access_token: {e}")
            return None

    def refresh_token(self):
        at = self.get_access_token()
        if not at:
            return False
        try:
            proxy = get_random_proxy_dict()
            res = self.session.post(
                self.LOGIN_URL,
                headers={
                    "content-type": "application/json",
                    "origin": "https://lc79b.bet",
                    "referer": "https://lc79b.bet/",
                    "user-agent": "Mozilla/5.0",
                    "connection": "keep-alive",
                },
                json={"nickName": self.nick, "accessToken": at},
                proxies=proxy,
                timeout=(self.CONNECT_TIMEOUT, self.READ_TIMEOUT),
            )
            data = res.json()
            new_tok = data.get("token")
            if new_tok:
                self.jwt = new_tok
                self.jwt_exp = self.parse_jwt_exp(new_tok)
                new_bal = int(data.get("remoteLoginResp", {}).get("money", self.balance) or self.balance)
                self.balance = new_bal
                sync_balance_to_storage(self.un, new_bal)
                return True
            return False
        except Exception as e:
            logger.error(f"[{self.nick}] refresh: {e}")
            return False

    def _single_redeem_request(self, code):
        try:
            proxy = get_random_proxy_dict()
            res = self.session.post(
                self.BASE_URL + self.PARAMS,
                headers=self._headers(self.jwt),
                json={"code": code.strip(), "captcha": ""},
                proxies=proxy,
                timeout=(self.CONNECT_TIMEOUT, self.READ_TIMEOUT),
            )
            raw_text = res.text[:200] if res.text else ""
            try:
                data = res.json()
            except Exception:
                data = {}

            if res.status_code in (200, 201):
                amount = (
                    data.get("money")
                    or data.get("amount")
                    or data.get("reward")
                    or (data.get("data") or {}).get("money")
                    or 0
                )
                try:
                    amount = int(amount)
                except Exception:
                    amount = 0
                msg = data.get("message") or data.get("msg") or "Đổi code thành công"
                return "SUCCESS", amount, f"HTTP {res.status_code}: {msg}"
            elif res.status_code == 401:
                return "TOKEN_EXPIRED", 0, "HTTP 401: Token hết hạn"
            else:
                err_msg = (
                    data.get("message")
                    or data.get("msg")
                    or data.get("error")
                    or raw_text
                    or f"HTTP {res.status_code}"
                )
                return "FAIL", 0, f"HTTP {res.status_code}: {err_msg}"
        except requests.exceptions.ProxyError:
            return "PROXY_ERR", 0, "Lỗi kết nối Proxy"
        except requests.exceptions.Timeout:
            return "TIMEOUT", 0, "Quá thời gian phản hồi (Timeout)"
        except Exception as e:
            return "NET_ERR", 0, f"Lỗi mạng: {str(e)[:80]}"

    def redeem_continuous_until_stop(self, code, stop_event, burst_count=10):
        """
        Bắn liên tục đồng thời với xoay vòng IP proxy cho đến khi:
        - Tài khoản này nhận thành công (SUCCESS) -> kích hoạt stop_event dừng toàn hệ thống
        - Hoặc stop_event được bật (do acc khác hoặc người trên kênh đã nhập được)
        """
        if not self.is_token_valid():
            if not self.refresh_token():
                append_redeem_log(code, self.nick, self.balance, self.balance, 0, "LOGIN_FAIL", "Không lấy được token đăng nhập")
                return {
                    "nick": self.nick,
                    "un": self.un,
                    "owner": self.owner,
                    "success": False,
                    "bal_before": self.balance,
                    "bal_after": self.balance,
                    "amount": 0,
                    "attempts": 0,
                    "last_response": "❌ Đăng nhập thất bại (Sai pass hoặc kẹt phiên)"
                }

        bal_before = self.balance
        attempts = 0
        last_response = "Chưa có phản hồi"
        win_info = None
        state_lock = threading.Lock()
        token_refreshing = threading.Lock()

        def worker():
            nonlocal attempts, last_response, win_info
            while not stop_event.is_set():
                st, amt, resp_detail = self._single_redeem_request(code)
                with state_lock:
                    attempts += 1
                    last_response = resp_detail

                if st == "SUCCESS":
                    with state_lock:
                        if win_info is None:
                            win_info = (amt, resp_detail)
                    stop_event.set()
                    return

                if st == "TOKEN_EXPIRED":
                    if token_refreshing.acquire(blocking=False):
                        try:
                            self.refresh_token()
                        finally:
                            token_refreshing.release()

                low_resp = resp_detail.lower()
                if any(kw in low_resp for kw in ("hết lượt", "đã sử dụng", "đã nhận", "không tồn tại", "hết hạn")):
                    with state_lock:
                        last_response = resp_detail
                    return

                time.sleep(0.05)

        workers = []
        n_threads = max(1, min(int(burst_count or 10), 30))
        for _ in range(n_threads):
            t = threading.Thread(target=worker, daemon=True)
            workers.append(t)
            t.start()

        for t in workers:
            t.join(timeout=45)

        if win_info is not None:
            amt_raw, resp_detail = win_info
            self.refresh_token()
            bal_after = self.balance
            diff = max(0, bal_after - bal_before)
            total_amt = amt_raw or diff
            sync_balance_to_storage(self.un, bal_after)
            append_redeem_log(code, self.nick, bal_before, bal_after, total_amt, "SUCCESS", resp_detail)
            return {
                "nick": self.nick,
                "un": self.un,
                "owner": self.owner,
                "success": True,
                "bal_before": bal_before,
                "bal_after": bal_after,
                "amount": total_amt,
                "attempts": attempts,
                "last_response": f"✅ {resp_detail}",
            }
        else:
            status_tag = "STOPPED" if stop_event.is_set() else "FAIL"
            append_redeem_log(code, self.nick, bal_before, self.balance, 0, status_tag, last_response)
            return {
                "nick": self.nick,
                "un": self.un,
                "owner": self.owner,
                "success": False,
                "bal_before": bal_before,
                "bal_after": self.balance,
                "amount": 0,
                "attempts": attempts,
                "last_response": f"⏹️ Dừng (Đã có người nhập) | Cuối: {last_response}" if stop_event.is_set() else f"❌ {last_response}",
            }


def build_redeem_accounts():
    accounts = redeem_state.get("accounts", []) if isinstance(redeem_state, dict) else []
    result = []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        un = str(item.get("un") or item.get("username") or "").strip()
        pw = str(item.get("pw") or item.get("password") or "").strip()
        nick = str(item.get("nick") or item.get("nickname") or un).strip()
        owner = item.get("owner")
        bal = item.get("balance", 0)
        if un and pw:
            result.append(RedeemAccount(un, pw, nick or un, owner=owner, balance=bal))
    return result


def sync_redeem_accounts():
    global redeem_state, REDEEM_ACCOUNTS
    disk_state = load_json(REDEEM_STATE_FILE, None)
    if isinstance(disk_state, dict):
        disk_state.setdefault("accounts", [])
        disk_state.setdefault("channel_id", None)
        disk_state.setdefault("channel_name", "")
        disk_state.setdefault("enabled", False)
        disk_state.setdefault("proxy_url", "")
        disk_state.setdefault("proxy_list", [])
        disk_state.setdefault("burst_count", 10)
        redeem_state = disk_state
    REDEEM_ACCOUNTS = build_redeem_accounts()
    return REDEEM_ACCOUNTS


REDEEM_ACCOUNTS = build_redeem_accounts()


def redeem_all_simultaneous(code):
    """Chạy đồng loạt mọi tài khoản liên tục đến khi có người trúng hoặc stop_event bật."""
    global active_redeem_code
    if not REDEEM_ACCOUNTS:
        logger.warning("Chưa có acc redeem")
        return

    with active_redeem_lock:
        active_redeem_stop_event.clear()
        active_redeem_code = code

    def worker():
        global active_redeem_code
        prep_threads = []
        for acc in REDEEM_ACCOUNTS:
            if not acc.is_token_valid():
                pt = threading.Thread(target=acc.refresh_token, daemon=True)
                pt.start()
                prep_threads.append(pt)
        for pt in prep_threads:
            pt.join(timeout=6)

        burst = int(redeem_state.get("burst_count") or 10)
        threads = []
        all_reports = []
        rep_lock = threading.Lock()

        def run_acc(account):
            rep = account.redeem_continuous_until_stop(code, active_redeem_stop_event, burst_count=burst)
            with rep_lock:
                all_reports.append(rep)

        for acc in REDEEM_ACCOUNTS:
            t = threading.Thread(target=run_acc, args=(acc,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=50)

        with active_redeem_lock:
            active_redeem_code = None

        win_accounts = [r for r in all_reports if r["success"]]
        ok = len(win_accounts)
        total = sum(w["amount"] for w in win_accounts)

        log_lines = []
        for r in all_reports:
            if r["success"]:
                log_lines.append(
                    f"🟢 <b>{html.escape(r['nick'])}</b> (<code>{html.escape(r['un'])}</code>) [{r['attempts']} lần]\n"
                    f"   💰 Số dư: {r['bal_before']:,} ➔ <b>{r['bal_after']:,}</b> (+{r['amount']:,})\n"
                    f"   📩 Phản hồi: <code>{html.escape(r['last_response'])}</code>"
                )
            else:
                log_lines.append(
                    f"⚪ <b>{html.escape(r['nick'])}</b> (<code>{html.escape(r['un'])}</code>) [{r['attempts']} lần]\n"
                    f"   📩 Phản hồi: <code>{html.escape(r['last_response'])}</code>"
                )

        report_body = "\n".join(log_lines)
        if len(report_body) > 3200:
            report_body = report_body[:3200] + "\n... (Xem thêm trong Xuất lịch sử)"

        try:
            bot.send_message(
                ADMIN_ID,
                f"🎁 <b>BÁO CÁO REDEEM CODE:</b> <code>{html.escape(code)}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Thành công: <b>{ok}/{len(REDEEM_ACCOUNTS)}</b> acc\n"
                f"💰 Tổng cộng thêm: <b>+{total:,}</b>\n"
                f"🌐 Proxy đang dùng: <b>{len(redeem_state.get('proxy_list') or [])}</b> IP\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 <b>LOG PHẢN HỒI CHI TIẾT:</b>\n{report_body}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Lỗi gửi báo cáo admin: {e}")

        for w in win_accounts:
            if w.get("owner") and w["owner"] != ADMIN_ID:
                try:
                    bot.send_message(
                        w["owner"],
                        f"🎉 <b>Acc của bạn vừa lụm được code!</b>\n"
                        f"📝 Mã code: <code>{html.escape(code)}</code>\n"
                        f"👤 Tài khoản: <b>{html.escape(w['nick'])}</b> (<code>{html.escape(w['un'])}</code>)\n"
                        f"💰 Số dư: {w['bal_before']:,} ➔ <b>{w['bal_after']:,}</b> (+{w['amount']:,})",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    threading.Thread(target=worker, daemon=True).start()


# ========================= LOGIN & LẤY NICKNAME TỰ ĐỘNG =========================
def login_and_get_info(u, p):
    try:
        pw_hashed = normalize_pw(p)
        proxy = get_random_proxy_dict()
        r = requests.get(
            f"https://apifo88daigia.tele68.com/api?c=3&un={u}&pw={pw_hashed}&cp=R&cl=R&pf=web&at=",
            proxies=proxy,
            timeout=10,
        ).json()
        if not r.get("success"):
            return {"_error": "Sai tài khoản hoặc mật khẩu"}
        sk = r["sessionKey"] + "=" * ((4 - len(r["sessionKey"]) % 4) % 4)
        nn = json.loads(base64.b64decode(sk)).get("nickname")
        r2 = requests.post(
            "https://wlb.tele68.com/v1/lobby/auth/login?cp=R&cl=R&pf=web&at=",
            json={"nickName": nn, "accessToken": r["accessToken"]},
            proxies=proxy,
            timeout=10,
        ).json()
        if not r2.get("token"):
            return {"_error": "Không nhận được token, có thể tài khoản đang kẹt phiên"}
        return {
            "token": r2["token"],
            "nickname": nn,
            "money": int(r2.get("remoteLoginResp", {}).get("money", 0) or 0),
            "pw_hashed": pw_hashed,
        }
    except Exception as e:
        return {"_error": str(e)}


def save_game_account(owner_id, username, nickname, token, money, source="user"):
    key = f"{owner_id}_{username.lower()}"
    game_accounts[key] = {
        "owner": owner_id,
        "username": username,
        "nickname": nickname,
        "token": token,
        "money": int(money or 0),
        "login_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
    }
    save_json(GAME_ACCOUNTS_FILE, game_accounts)


def upsert_redeem_account(un, pw_hashed, nick, owner_id, balance=0):
    global REDEEM_ACCOUNTS
    redeem_state.setdefault("accounts", [])
    updated = False
    for acc in redeem_state["accounts"]:
        if acc.get("un", "").lower() == un.lower():
            acc["pw"] = pw_hashed
            acc["nick"] = nick
            acc["owner"] = owner_id
            acc["balance"] = int(balance or 0)
            updated = True
            break
    if not updated:
        redeem_state["accounts"].append({
            "un": un, "pw": pw_hashed, "nick": nick, "owner": owner_id, "balance": int(balance or 0),
        })
    save_json(REDEEM_STATE_FILE, redeem_state)
    REDEEM_ACCOUNTS = build_redeem_accounts()


# ========================= TELETHON, BẮT CODE & BẮT THÔNG BÁO TRÚNG =========================
tele_client = None
tele_loop = None
tele_ready = False
tele_login_pending = {}


def _load_tele_session():
    if os.path.exists(TELE_SESSION_FILE):
        try:
            with open(TELE_SESSION_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return os.environ.get("TELETHON_SESSION", "").strip()


def _save_tele_session(s):
    with open(TELE_SESSION_FILE, "w", encoding="utf-8") as f:
        f.write(s)


def extract_clean_code(raw_text):
    """Cắt tiền tố LC79 ở đầu mã code, ví dụ LC79JAXLKDLGT thành JAXLKDLGT."""
    if not raw_text:
        return None
    match = re.search(r"Mã\s*code\s*là\s*:\s*([A-Z0-9]+)", raw_text, re.IGNORECASE)
    if not match:
        return None
    code = match.group(1).strip().upper()
    if code.startswith("LC79") and len(code) > 4:
        code = code[4:]
    return code


def extract_winner_announcement(raw_text):
    """
    Nhận diện thông báo trên kênh khi có người đổi thành công Giftcode:
    Ví dụ: '💰 💰 💰 Chúc mừng thaomee đã đổi thành công Giftcode trị giá 6.500!'
    """
    if not raw_text:
        return None
    match = re.search(
        r"Chúc\s*mừng\s+([^\s]+)\s+đã\s+đổi\s+thành\s+công\s+Giftcode(?:\s+trị\s+giá\s+([0-9.,]+))?",
        raw_text,
        re.IGNORECASE,
    )
    if not match:
        return None
    winner_nick = match.group(1).strip()
    reward_str = (match.group(2) or "0").strip()
    return winner_nick, reward_str


def _handle_winner_on_channel(winner_nick, reward_str):
    """Dừng quá trình bắn hiện tại và kiểm tra xem nickname trúng có trùng với acc trong bot không."""
    global active_redeem_code
    was_running = not active_redeem_stop_event.is_set() and active_redeem_code is not None
    stopped_code = active_redeem_code
    active_redeem_stop_event.set()

    sync_redeem_accounts()
    matched_acc = None
    for acc in REDEEM_ACCOUNTS:
        if acc.nick.strip().lower() == winner_nick.strip().lower():
            matched_acc = acc
            break

    if matched_acc:
        def sync_and_notify():
            bal_before = matched_acc.balance
            matched_acc.refresh_token()
            bal_after = matched_acc.balance
            diff = max(0, bal_after - bal_before)
            try:
                reward_num = int(re.sub(r"[^\d]", "", reward_str) or 0)
            except Exception:
                reward_num = 0
            plus_amount = diff if diff > 0 else reward_num

            sync_balance_to_storage(matched_acc.un, bal_after)
            msg_text = (
                f"🎊 <b>PHÁT HIỆN ACC CỦA BOT ĐÃ TRÚNG CODE TRÊN KÊNH!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏹️ <b>Trạng thái:</b> Đã dừng bắn code <code>{html.escape(stopped_code or 'N/A')}</code>\n"
                f"👤 <b>Nickname trùng khớp:</b> <b>{html.escape(matched_acc.nick)}</b> (<code>{html.escape(matched_acc.un)}</code>)\n"
                f"🎁 <b>Giá trị thông báo:</b> <b>+{reward_str}</b>\n"
                f"💰 <b>Cập nhật số dư:</b> {bal_before:,} ➔ <b>{bal_after:,}</b> (<b>+{plus_amount:,}</b>)"
            )
            try:
                bot.send_message(ADMIN_ID, msg_text, parse_mode="HTML")
            except Exception:
                pass
            if matched_acc.owner and matched_acc.owner != ADMIN_ID:
                try:
                    bot.send_message(matched_acc.owner, msg_text, parse_mode="HTML")
                except Exception:
                    pass

        threading.Thread(target=sync_and_notify, daemon=True).start()
    elif was_running:
        try:
            bot.send_message(
                ADMIN_ID,
                f"⏹️ <b>ĐÃ DỪNG BẮN CODE</b> <code>{html.escape(stopped_code or '')}</code>\n"
                f"📢 Kênh báo người chơi <b>{html.escape(winner_nick)}</b> đã nhập thành công (+{html.escape(reward_str)}).",
                parse_mode="HTML",
            )
        except Exception:
            pass


def _activate_telethon_client(client, loop):
    global tele_client, tele_loop, tele_ready
    tele_client, tele_loop = client, loop

    @client.on(events.NewMessage())
    async def _on_msg(event):
        _handle_tele_message(event)

    async def _keep_running():
        global tele_ready
        try:
            if not await client.is_user_authorized():
                tele_ready = False
                return
            me = await client.get_me()
            tele_ready = True
            session_str = client.session.save()
            full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "N/A"
            uname = f"@{me.username}" if me.username else "Không có"
            phone = f"+{me.phone}" if getattr(me, "phone", None) else "Ẩn"
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"📡 <b>Tele nguồn đã kết nối</b>\n"
                    f"👤 Tên: {html.escape(full_name)}\n"
                    f"🆔 ID: <code>{me.id}</code>\n"
                    f"🔗 Username: {html.escape(uname)}\n"
                    f"📱 SĐT: <code>{html.escape(phone)}</code>\n"
                    f"🔑 Session:\n<code>{html.escape(session_str)}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            await client.run_until_disconnected()
        except Exception as exc:
            tele_ready = False
            logger.exception("Tele listener lỗi: %s", exc)

    loop.run_until_complete(_keep_running())


def _handle_tele_message(event):
    if not redeem_state.get("enabled") or redeem_state.get("channel_id") is None:
        return
    if event.chat_id != redeem_state["channel_id"]:
        return

    raw = event.raw_text or ""

    # 1. Kiểm tra nếu kênh báo có người đã đổi thành công code
    winner_data = extract_winner_announcement(raw)
    if winner_data:
        winner_nick, reward_str = winner_data
        logger.info(f"[TELE] Phát hiện người trúng: {winner_nick} (+{reward_str})")
        _handle_winner_on_channel(winner_nick, reward_str)
        return

    # 2. Kiểm tra tin nhắn phát code mới
    clean_code = extract_clean_code(raw)
    if not clean_code:
        return
    logger.info(f"[TELE] Bắt được code sạch: {clean_code}")
    try:
        bot.send_message(
            ADMIN_ID,
            f"⚡ <b>Phát hiện code mới:</b> <code>{clean_code}</code>\n"
            f"🚀 Bắn đồng loạt <b>{len(REDEEM_ACCOUNTS)}</b> acc ({redeem_state.get('burst_count', 10)} luồng/acc) • Xoay vòng <b>{len(redeem_state.get('proxy_list') or [])}</b> Proxy...",
            parse_mode="HTML",
        )
    except Exception:
        pass
    redeem_all_simultaneous(clean_code)


def _start_telethon_listener():
    global tele_client, tele_loop, tele_ready
    if TELETHON_API_ID == 0 or not TELETHON_API_HASH:
        return
    session_str = _load_tele_session()
    if not session_str:
        return
    tele_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(tele_loop)
    try:
        tele_client = TelegramClient(StringSession(session_str), TELETHON_API_ID, TELETHON_API_HASH, loop=tele_loop)
    except Exception as e:
        logger.error(f"Tele init: {e}")
        return

    @tele_client.on(events.NewMessage())
    async def _on_msg(event):
        _handle_tele_message(event)

    async def _run():
        global tele_ready
        try:
            await tele_client.connect()
            if not await tele_client.is_user_authorized():
                return
            tele_ready = True
            await tele_client.run_until_disconnected()
        except Exception as e:
            logger.error(f"Tele run: {e}")

    tele_loop.run_until_complete(_run())


def tele_resolve_entity(target, timeout=15):
    if tele_client is None or not tele_ready:
        raise RuntimeError("Tele nguồn chưa đăng nhập hoặc chưa online")
    f = asyncio.run_coroutine_threadsafe(tele_client.get_entity(target), tele_loop)
    return f.result(timeout=timeout)


def get_tele_account_details():
    session_str = _load_tele_session()
    if not session_str:
        return None, None
    if tele_client and tele_ready and tele_loop:
        try:
            fut = asyncio.run_coroutine_threadsafe(tele_client.get_me(), tele_loop)
            me = fut.result(timeout=8)
            return me, session_str
        except Exception:
            pass
    return None, session_str


def tele_list_dialogs_and_cleanup(keep_channel_id=None, do_delete=False):
    """Liệt kê tất cả nhóm/kênh của tài khoản Tele nguồn và xoá toàn bộ cuộc trò chuyện khác nếu do_delete=True."""
    if tele_client is None or not tele_ready or tele_loop is None:
        raise RuntimeError("Tele nguồn chưa đăng nhập hoặc chưa online")

    async def _task():
        dialogs = await tele_client.get_dialogs()
        groups_info = []
        deleted_count = 0
        kept_count = 0

        for d in dialogs:
            peer_id = utils.get_peer_id(d.entity)
            title = d.title or getattr(d.entity, "first_name", None) or str(peer_id)
            is_channel = bool(getattr(d, "is_channel", False))
            is_group = bool(getattr(d, "is_group", False))

            if is_channel or is_group:
                groups_info.append({
                    "id": peer_id,
                    "title": title,
                    "type": "Kênh" if is_channel and not getattr(d.entity, "megagroup", False) else "Nhóm",
                    "is_target": (keep_channel_id is not None and peer_id == keep_channel_id),
                })

            if do_delete:
                if keep_channel_id is not None and peer_id == keep_channel_id:
                    kept_count += 1
                    continue
                try:
                    await tele_client.delete_dialog(d.entity)
                    deleted_count += 1
                except Exception as ex:
                    logger.warning(f"Không xoá được dialog {title} ({peer_id}): {ex}")

        return groups_info, deleted_count, kept_count

    fut = asyncio.run_coroutine_threadsafe(_task(), tele_loop)
    return fut.result(timeout=60)


# ========================= GIAO DIỆN =========================
BOT_TITLE = "✨ LỤM CODE LC79 ✨"
DIV = "━━━━━━━━━━━━━━━━━━━━"
user_flows = {}
SCREEN_MESSAGES = {}


def _btn(text, data):
    return telebot.types.InlineKeyboardButton(text, callback_data=data)


def build_home_header(user_obj, chat_id):
    first = getattr(user_obj, "first_name", "") or ""
    last = getattr(user_obj, "last_name", "") or ""
    full_name = f"{first} {last}".strip() or "Người dùng"
    uname = f"@{user_obj.username}" if getattr(user_obj, "username", None) else "Không có"
    role = "👑 Quản trị viên" if chat_id == ADMIN_ID else "👤 Thành viên"

    lines = [
        f"<b>{BOT_TITLE}</b>",
        DIV,
        f"👋 Xin chào, <b>{html.escape(full_name)}</b>",
        f"🔗 Username: {html.escape(uname)}",
        f"🆔 ID User: <code>{chat_id}</code>",
        f"🎖️ Vai trò: {role}",
        DIV,
        f"💬 Hỗ trợ: {SUPPORT_HANDLE}",
        "👇 Chọn chức năng bên dưới",
    ]
    return "\n".join(lines)


def replace_screen(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    """Thay màn hình hiện tại bằng màn hình mới: xoá tin cũ và gửi tin mới."""
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        try:
            msg = bot.edit_message_text(
                text, chat_id, message_id,
                parse_mode=parse_mode, reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            SCREEN_MESSAGES[chat_id] = message_id
            return msg
        except Exception:
            pass
    msg = bot.send_message(
        chat_id, text, parse_mode=parse_mode,
        reply_markup=reply_markup, disable_web_page_preview=True,
    )
    SCREEN_MESSAGES[chat_id] = msg.message_id
    return msg


def flow_prompt(chat_id, message_id, flow, title, detail):
    user_flows[chat_id] = {"step": flow}
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(_btn("❌ Huỷ", "flow_cancel"))
    return replace_screen(
        chat_id, message_id,
        f"<b>{title}</b>\n{DIV}\n{detail}\n\n💬 <i>Nhập nội dung rồi gửi vào khung chat.</i>",
        kb, "HTML",
    )


def main_menu(cid):
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    if cid == ADMIN_ID:
        kb.add(_btn("⚡ Auto Redeem", "rd_panel"), _btn("🔐 Login TK game", "rd_login_acc"))
        kb.add(_btn("📋 Danh sách acc", "rd_list"), _btn("➖ Xoá tài khoản", "rd_delacc"))
        kb.add(_btn("🌐 Proxy Webshare", "proxy_panel"), _btn("📡 Tele nguồn", "tele_panel"))
        kb.add(_btn("📄 Xuất token", "export_tokens"), _btn("📊 Xuất lịch sử", "export_history"))
        kb.add(_btn("🛟 Hỗ trợ", "support_info"), _btn("🔄 Làm mới", "home"))
    else:
        kb.add(_btn("🔐 Login TK game", "user_login_acc"), _btn("📋 Acc của tôi", "user_my_accs"))
        kb.add(_btn("➖ Xoá acc của tôi", "user_del_acc"), _btn("🛟 Hỗ trợ", "support_info"))
        kb.add(_btn("🔄 Làm mới", "home"))
    return kb


def redeem_menu():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    toggle = "🟢 Đang bật · tắt" if redeem_state.get("enabled") else "🔴 Đang tắt · bật"
    kb.add(_btn(toggle, "rd_toggle"), _btn("📡 Đặt kênh", "rd_setchannel"))
    kb.add(_btn("🔐 Login TK", "rd_login_acc"), _btn("📋 Danh sách", "rd_list"))
    kb.add(_btn("➖ Xoá TK", "rd_delacc"), _btn("🔄 Cập nhật số dư", "rd_refresh"))
    kb.add(_btn("🌐 Cấu hình Proxy", "proxy_panel"), _btn("⬅️ Quay lại", "home"))
    return kb


def proxy_menu():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(_btn("➕ Thêm link / Proxy", "proxy_add"), _btn("🧪 Test IP Proxy", "proxy_test"))
    kb.add(_btn("🔄 Tải lại từ link", "proxy_reload"), _btn("🗑️ Xoá toàn bộ Proxy", "proxy_clear"))
    kb.add(_btn("⬅️ Quay lại", "home"))
    return kb


def tele_menu():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(_btn("🔐 Đăng nhập Tele", "tele_login"), _btn("🔑 Xuất session", "tele_export_session"))
    kb.add(_btn("👥 Danh sách nhóm", "tele_groups"), _btn("🧹 Xoá tất cả chat", "tele_clean_confirm"))
    kb.add(_btn("📊 Trạng thái", "tele_status_btn"), _btn("🗑️ Xoá session", "tele_logout_confirm"))
    kb.add(_btn("⬅️ Quay lại", "home"))
    return kb


# ========================= XUẤT FILE TXT =========================
def send_tokens_txt(chat_id):
    if chat_id != ADMIN_ID:
        return
    sync_redeem_accounts()
    if not REDEEM_ACCOUNTS and not game_accounts:
        return bot.send_message(chat_id, "📭 Chưa có tài khoản hoặc token nào để xuất.")

    lines = [
        "========================================================================",
        "              LỤM CODE LC79 - DANH SÁCH TOKEN TÀI KHOẢN",
        "========================================================================",
        f"Thời gian xuất : {datetime.now():%d/%m/%Y %H:%M:%S}",
        f"Tổng tài khoản : {len(REDEEM_ACCOUNTS)}",
        "========================================================================\n",
    ]
    token_map = {str(v.get("username", "")).lower(): v.get("token", "") for v in game_accounts.values()}

    for i, acc in enumerate(REDEEM_ACCOUNTS, 1):
        if not acc.is_token_valid():
            acc.refresh_token()
        tok = acc.jwt or token_map.get(acc.un.lower(), "CHƯA CÓ TOKEN")
        lines.append(
            f"[{i}] Nickname: {acc.nick} | Username: {acc.un} | Số dư: {acc.balance:,}\n"
            f"Token: {tok}\n" + "-" * 72
        )

    bio = io.BytesIO("\n".join(lines).encode("utf-8"))
    bio.name = f"LumCodeLC79_Tokens_{datetime.now():%Y%m%d_%H%M%S}.txt"
    return bot.send_document(
        chat_id, bio,
        caption=f"📄 <b>Xuất token</b>\n👥 Tổng: <b>{len(REDEEM_ACCOUNTS)}</b> tài khoản",
        parse_mode="HTML",
    )


def send_full_history_txt(chat_id):
    if chat_id != ADMIN_ID:
        return
    if not redeem_logs:
        return bot.send_message(chat_id, "📭 Chưa có dữ liệu lịch sử redeem nào.")

    ok_count = sum(1 for x in redeem_logs if x.get("status") == "SUCCESS")
    fail_count = len(redeem_logs) - ok_count
    total_money = sum(int(x.get("amount", 0) or 0) for x in redeem_logs if x.get("status") == "SUCCESS")

    lines = [
        "========================================================================",
        "             LỤM CODE LC79 - LỊCH SỬ REDEEM FULL THÔNG TIN",
        "========================================================================",
        f"Thời gian xuất  : {datetime.now():%d/%m/%Y %H:%M:%S}",
        f"Tổng lượt chạy  : {len(redeem_logs)}",
        f"Thành công      : {ok_count}",
        f"Thất bại        : {fail_count}",
        f"Tổng tiền nhận  : {total_money:,} VNĐ",
        "========================================================================\n",
    ]
    for i, item in enumerate(redeem_logs, 1):
        lines.append(
            f"[{i}] Thời gian   : {item.get('time', '')}\n"
            f"    Mã Code     : {item.get('code', '')}\n"
            f"    Tài khoản   : {item.get('account', '')}\n"
            f"    Số dư trước : {int(item.get('balance_before', 0) or 0):,}\n"
            f"    Số dư sau   : {int(item.get('balance_after', 0) or 0):,}\n"
            f"    Tiền nhận   : +{int(item.get('amount', 0) or 0):,}\n"
            f"    Trạng thái  : {item.get('status', '')}\n"
            f"    Phản hồi    : {item.get('detail', '')}\n" + "-" * 72
        )

    bio = io.BytesIO("\n".join(lines).encode("utf-8"))
    bio.name = f"LumCodeLC79_LichSuFull_{datetime.now():%Y%m%d_%H%M%S}.txt"
    return bot.send_document(
        chat_id, bio,
        caption=(
            f"📊 <b>Lịch sử redeem</b>\n"
            f"📝 Tổng lượt: <b>{len(redeem_logs)}</b> · ✅ OK: <b>{ok_count}</b>\n"
            f"💰 Tổng tiền nhận: <b>{total_money:,}</b>"
        ),
        parse_mode="HTML",
    )


# ========================= HANDLERS CHÍNH =========================
@bot.message_handler(commands=["start", "menu"])
def cmd_start(m):
    cid = m.chat.id
    user_flows.pop(cid, None)
    try:
        bot.delete_message(cid, m.message_id)
    except Exception:
        pass
    old_id = SCREEN_MESSAGES.get(cid)
    if old_id:
        try:
            bot.delete_message(cid, old_id)
        except Exception:
            pass
    msg = bot.send_message(cid, build_home_header(m.from_user, cid), parse_mode="HTML",
                           reply_markup=main_menu(cid), disable_web_page_preview=True)
    SCREEN_MESSAGES[cid] = msg.message_id


@bot.callback_query_handler(func=lambda c: True)
def cb(c):
    global REDEEM_ACCOUNTS
    cid = c.message.chat.id
    data = c.data
    try:
        bot.answer_callback_query(c.id)
    except Exception:
        pass

    def go(text, kb=None, mode="HTML"):
        return replace_screen(cid, c.message.message_id, text, kb, mode)

    if data in ("home", "flow_cancel"):
        user_flows.pop(cid, None)
        return go(build_home_header(c.from_user, cid), main_menu(cid))

    if data == "support_info":
        txt = (
            f"<b>🛟 HỖ TRỢ</b>\n{DIV}\n"
            f"👨‍💻 Admin: {SUPPORT_HANDLE}\n"
            f"📌 Hỗ trợ kỹ thuật và vận hành hệ thống."
        )
        return go(txt, main_menu(cid))

    # ---- Người dùng ----
    if data == "user_login_acc":
        return flow_prompt(cid, c.message.message_id, "user_login_game", "🔐 LOGIN TÀI KHOẢN GAME",
                           "Gửi theo mẫu: <code>TÀI_KHOẢN MẬT_KHẨU</code>\n"
                           "Hệ thống tự lấy nickname và số dư.")

    if data == "user_my_accs":
        sync_redeem_accounts()
        my_accs = [a for a in REDEEM_ACCOUNTS if a.owner == cid]
        if not my_accs:
            return go("📭 Bạn chưa có tài khoản nào. Bấm <b>Login TK game</b> để thêm.", main_menu(cid))
        lines = [f"{i}. <b>{html.escape(a.nick)}</b> (<code>{html.escape(a.un)}</code>)\n    💰 {a.balance:,}"
                 for i, a in enumerate(my_accs, 1)]
        return go(f"<b>📋 TÀI KHOẢN CỦA BẠN</b>\n{DIV}\n" + "\n".join(lines), main_menu(cid))

    if data == "user_del_acc":
        sync_redeem_accounts()
        my_accs = [a for a in REDEEM_ACCOUNTS if a.owner == cid]
        if not my_accs:
            return go("📭 Bạn không có tài khoản nào để xoá.", main_menu(cid))
        preview = "\n".join([f"• <code>{html.escape(a.un)}</code> ({html.escape(a.nick)})" for a in my_accs])
        return flow_prompt(cid, c.message.message_id, "user_del_redeem", "➖ XOÁ TÀI KHOẢN",
                           f"{preview}\n\nGửi đúng <b>username</b> hoặc <b>nickname</b> cần xoá.")

    # ---- Chỉ admin ----
    if cid != ADMIN_ID:
        return go("⛔ Chức năng này chỉ dành cho quản trị viên.", main_menu(cid))

    if data == "rd_panel":
        sync_redeem_accounts()
        status_str = "🟢 Đang bật" if redeem_state.get("enabled") else "🔴 Đang tắt"
        ch_name = html.escape(redeem_state.get("channel_name") or "Chưa đặt")
        proxy_cnt = len(redeem_state.get("proxy_list") or [])
        burst_cnt = redeem_state.get("burst_count") or 10
        txt = (
            f"<b>⚡ AUTO REDEEM</b>\n{DIV}\n"
            f"🔔 Trạng thái: {status_str}\n"
            f"📡 Kênh nguồn: {ch_name}\n"
            f"👥 Tài khoản sẵn sàng: <b>{len(REDEEM_ACCOUNTS)}</b>\n"
            f"🌐 Proxy xoay vòng: <b>{proxy_cnt}</b> IP\n"
            f"⚡ Luồng bắn/acc: <b>{burst_cnt}</b> (liên tục đến khi trúng)"
        )
        return go(txt, redeem_menu())

    if data == "rd_toggle":
        sync_redeem_accounts()
        if redeem_state.get("enabled"):
            redeem_state["enabled"] = False
        elif not REDEEM_ACCOUNTS:
            return go("⚠️ Chưa có tài khoản nào. Hãy login TK trước.", redeem_menu())
        elif redeem_state.get("channel_id") is None:
            return go("⚠️ Chưa đặt kênh nhận code. Hãy bấm <b>Đặt kênh</b> trước.", redeem_menu())
        else:
            redeem_state["enabled"] = True
        save_json(REDEEM_STATE_FILE, redeem_state)
        status_str = "🟢 Đang bật" if redeem_state.get("enabled") else "🔴 Đang tắt"
        ch_name = html.escape(redeem_state.get("channel_name") or "Chưa đặt")
        txt = (
            f"<b>⚡ AUTO REDEEM</b>\n{DIV}\n"
            f"🔔 Trạng thái: {status_str}\n"
            f"📡 Kênh nguồn: {ch_name}\n"
            f"👥 Tài khoản sẵn sàng: <b>{len(REDEEM_ACCOUNTS)}</b>"
        )
        return go(txt, redeem_menu())

    if data == "rd_setchannel":
        return flow_prompt(cid, c.message.message_id, "set_channel", "📡 ĐẶT KÊNH NHẬN CODE",
                           "Gửi <code>@username</code>, link <code>t.me/...</code> hoặc ID kênh.")

    if data == "rd_login_acc":
        return flow_prompt(cid, c.message.message_id, "admin_login_game", "🔐 LOGIN TK GAME",
                           "Gửi theo mẫu: <code>TÀI_KHOẢN MẬT_KHẨU</code>\n"
                           "Hệ thống tự lấy nickname và số dư.")

    if data == "rd_delacc":
        sync_redeem_accounts()
        if not REDEEM_ACCOUNTS:
            return go("📭 Danh sách tài khoản đang trống.", redeem_menu())
        return flow_prompt(cid, c.message.message_id, "admin_del_redeem", "➖ XOÁ TÀI KHOẢN",
                           "Gửi đúng <b>nickname</b> hoặc <b>username</b> cần xoá.")

    if data == "rd_list":
        sync_redeem_accounts()
        if not REDEEM_ACCOUNTS:
            return go("📭 Chưa có tài khoản nào.", redeem_menu())
        lines = [f"{i}. {'🟢' if a.is_token_valid() else '🟡'} <b>{html.escape(a.nick)}</b> "
                 f"(<code>{html.escape(a.un)}</code>) · 💰 {a.balance:,}"
                 for i, a in enumerate(REDEEM_ACCOUNTS, 1)]
        return go(f"<b>📋 DANH SÁCH TÀI KHOẢN</b>\n{DIV}\n" + "\n".join(lines), redeem_menu())

    if data == "rd_refresh":
        sync_redeem_accounts()
        if not REDEEM_ACCOUNTS:
            return go("📭 Không có tài khoản nào để cập nhật.", redeem_menu())
        go("🔄 <b>Đang làm mới token và số dư...</b>", redeem_menu())

        def refresh_all_worker():
            ok = 0
            for acc in REDEEM_ACCOUNTS:
                try:
                    if acc.refresh_token():
                        ok += 1
                except Exception:
                    pass
            lines = [f"• <b>{html.escape(a.nick)}</b>: 💰 {a.balance:,}" for a in REDEEM_ACCOUNTS]
            try:
                bot.send_message(
                    cid,
                    f"✅ <b>Đã cập nhật {ok}/{len(REDEEM_ACCOUNTS)} tài khoản</b>\n{DIV}\n" + "\n".join(lines),
                    parse_mode="HTML", reply_markup=redeem_menu(),
                )
            except Exception:
                pass

        threading.Thread(target=refresh_all_worker, daemon=True).start()
        return

    # ---- Proxy Webshare.io ----
    if data == "proxy_panel":
        plist = redeem_state.get("proxy_list") or []
        purl = redeem_state.get("proxy_url") or "Chưa cài đặt"
        burst = redeem_state.get("burst_count") or 10
        txt = (
            f"<b>🌐 QUẢN LÝ PROXY WEBSHARE.IO</b>\n{DIV}\n"
            f"📦 Số lượng IP đang lưu: <b>{len(plist)}</b> proxy\n"
            f"⚡ Số luồng bắn đồng thời/acc: <b>{burst}</b>\n"
            f"🔗 Link nguồn: <code>{html.escape(purl[:60] + ('...' if len(purl) > 60 else ''))}</code>\n{DIV}\n"
            f"Bot sẽ xoay vòng IP ngẫu nhiên trên mỗi request khi bắn code."
        )
        return go(txt, proxy_menu())

    if data == "proxy_add":
        return flow_prompt(
            cid, c.message.message_id, "proxy_add_link", "🌐 THÊM PROXY WEBSHARE.IO",
            "Gửi <b>Link download Webshare.io</b> hoặc <b>Proxy trực tiếp</b> kèm <b>Số luồng bắn/acc</b> (cách nhau bởi dấu cách).\n\n"
            "Ví dụ 1 (Link list):\n<code>https://proxy.webshare.io/api/v2/proxy/list/download/xxx/-/any/username/direct/-/ 15</code>\n\n"
            "Ví dụ 2 (1 Proxy):\n<code>http://yfgowccy:t6d7a65yvis3@45.38.107.97:6014/ 10</code>"
        )

    if data == "proxy_test":
        plist = redeem_state.get("proxy_list") or []
        if not plist:
            return go("⚠️ Chưa có proxy nào để kiểm tra.", proxy_menu())
        go("🧪 <b>Đang kiểm tra kết nối tới https://ipv4.webshare.io/...</b>", proxy_menu())

        def test_px():
            sample_raw = random.choice(plist)
            px_dict = parse_proxy_line(sample_raw)
            try:
                t0 = time.time()
                r = requests.get("https://ipv4.webshare.io/", proxies=px_dict, timeout=10)
                ms = int((time.time() - t0) * 1000)
                ip_out = r.text.strip()
                bot.send_message(
                    cid,
                    f"✅ <b>PROXY HOẠT ĐỘNG TỐT</b>\n{DIV}\n"
                    f"🌍 IP đầu ra: <code>{html.escape(ip_out)}</code>\n"
                    f"⚡ Tốc độ phản hồi: <b>{ms} ms</b>\n"
                    f"🔌 Proxy: <code>{html.escape(sample_raw)}</code>",
                    parse_mode="HTML", reply_markup=proxy_menu(),
                )
            except Exception as ex:
                bot.send_message(
                    cid,
                    f"❌ <b>PROXY LỖI KẾT NỐI</b>\n{DIV}\n"
                    f"🔌 Proxy: <code>{html.escape(sample_raw)}</code>\n"
                    f"⚠️ Chi tiết: <code>{html.escape(str(ex)[:150])}</code>",
                    parse_mode="HTML", reply_markup=proxy_menu(),
                )

        threading.Thread(target=test_px, daemon=True).start()
        return

    if data == "proxy_reload":
        purl = (redeem_state.get("proxy_url") or "").strip()
        if not purl or purl == "Nhập trực tiếp":
            return go("⚠️ Chưa có link Webshare.io nào được lưu để tải lại.", proxy_menu())
        go("🔄 <b>Đang tải lại danh sách Proxy từ Webshare.io...</b>", proxy_menu())

        def reload_px():
            try:
                plist = fetch_webshare_proxies(purl)
                redeem_state["proxy_list"] = plist
                save_json(REDEEM_STATE_FILE, redeem_state)
                bot.send_message(cid, f"✅ Đã cập nhật <b>{len(plist)}</b> proxy từ Webshare.io!",
                                 parse_mode="HTML", reply_markup=proxy_menu())
            except Exception as ex:
                bot.send_message(cid, f"❌ Lỗi tải proxy: {html.escape(str(ex))}",
                                 parse_mode="HTML", reply_markup=proxy_menu())

        threading.Thread(target=reload_px, daemon=True).start()
        return

    if data == "proxy_clear":
        redeem_state["proxy_url"] = ""
        redeem_state["proxy_list"] = []
        save_json(REDEEM_STATE_FILE, redeem_state)
        return go("🗑️ Đã xoá toàn bộ danh sách Proxy.", proxy_menu())

    # ---- Xuất file ----
    if data == "export_tokens":
        send_tokens_txt(cid)
        return

    if data == "export_history":
        send_full_history_txt(cid)
        return

    # ---- Tele nguồn & Quản lý nhóm/chat ----
    if data == "tele_panel":
        me, session_str = get_tele_account_details()
        if me:
            full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "N/A"
            uname = f"@{me.username}" if me.username else "Không có"
            phone = f"+{me.phone}" if getattr(me, "phone", None) else "Ẩn"
            info_block = (
                f"🟢 Online, đang theo dõi\n"
                f"👤 {html.escape(full_name)}\n"
                f"🆔 <code>{me.id}</code>\n"
                f"🔗 {html.escape(uname)}\n"
                f"📱 <code>{html.escape(phone)}</code>"
            )
        elif session_str:
            info_block = "🟡 Đã lưu session, đang chờ kết nối"
        else:
            info_block = "🔴 Chưa đăng nhập Tele nguồn"
        ch_name = html.escape(redeem_state.get("channel_name") or "Chưa đặt")
        return go(f"<b>📡 TELE NGUỒN</b>\n{DIV}\n{info_block}\n\n📢 Kênh theo dõi: {ch_name}", tele_menu())

    if data == "tele_groups":
        if not tele_ready:
            return go("❌ Tele nguồn chưa kết nối hoặc chưa đăng nhập.", tele_menu())
        go("🔄 <b>Đang tải danh sách nhóm/kênh của tài khoản Tele...</b>", tele_menu())

        def load_groups_worker():
            try:
                keep_id = redeem_state.get("channel_id")
                groups, _, _ = tele_list_dialogs_and_cleanup(keep_channel_id=keep_id, do_delete=False)
                if not groups:
                    bot.send_message(cid, "📭 Tài khoản Tele này chưa tham gia nhóm hoặc kênh nào.", reply_markup=tele_menu())
                    return
                lines = []
                for idx, g in enumerate(groups[:60], 1):
                    mark = "⭐ <b>[KÊNH NHẬN CODE]</b> " if g["is_target"] else ""
                    lines.append(f"{idx}. {mark}[{g['type']}] <b>{html.escape(g['title'])}</b>\n    🆔 <code>{g['id']}</code>")
                extra = f"\n\n<i>(Hiển thị 60/{len(groups)} nhóm/kênh)</i>" if len(groups) > 60 else ""
                bot.send_message(
                    cid,
                    f"<b>👥 DANH SÁCH NHÓM/KÊNH TELE ({len(groups)})</b>\n{DIV}\n" + "\n".join(lines) + extra,
                    parse_mode="HTML", reply_markup=tele_menu(),
                )
            except Exception as ex:
                bot.send_message(cid, f"❌ Lỗi lấy danh sách nhóm: {html.escape(str(ex))}",
                                 parse_mode="HTML", reply_markup=tele_menu())

        threading.Thread(target=load_groups_worker, daemon=True).start()
        return

    if data == "tele_clean_confirm":
        if not tele_ready:
            return go("❌ Tele nguồn chưa kết nối.", tele_menu())
        keep_id = redeem_state.get("channel_id")
        if keep_id is None:
            return go("⚠️ Bạn chưa đặt <b>Kênh nhận code</b>! Hãy đặt kênh trước để tránh bị xoá nhầm.", tele_menu())
        ch_name = html.escape(redeem_state.get("channel_name") or str(keep_id))
        kb = telebot.types.InlineKeyboardMarkup(row_width=2)
        kb.add(_btn("🧹 Xác nhận xoá tất cả", "tele_clean_yes"), _btn("↩️ Quay lại", "tele_panel"))
        return go(
            f"⚠️ <b>XÁC NHẬN DỌN DẸP CUỘC TRÒ CHUYỆN</b>\n{DIV}\n"
            f"Hệ thống sẽ xoá <b>toàn bộ</b> tin nhắn riêng, nhóm và kênh khác trên tài khoản Tele nguồn, "
            f"<b>CHỈ GIỮ LẠI DUY NHẤT</b> kênh nhận code:\n"
            f"📡 <b>{ch_name}</b> (<code>{keep_id}</code>)",
            kb,
        )

    if data == "tele_clean_yes":
        keep_id = redeem_state.get("channel_id")
        if keep_id is None:
            return go("⚠️ Chưa đặt kênh nhận code.", tele_menu())
        go("🧹 <b>Đang dọn dẹp toàn bộ cuộc trò chuyện khác...</b>", tele_menu())

        def clean_worker():
            try:
                _, deleted_cnt, kept_cnt = tele_list_dialogs_and_cleanup(keep_channel_id=keep_id, do_delete=True)
                bot.send_message(
                    cid,
                    f"✅ <b>ĐÃ DỌN DẸP XONG CUỘC TRÒ CHUYỆN</b>\n{DIV}\n"
                    f"🗑️ Đã xoá: <b>{deleted_cnt}</b> cuộc trò chuyện/nhóm\n"
                    f"⭐ Đã giữ lại: <b>{kept_cnt}</b> kênh nhận code",
                    parse_mode="HTML", reply_markup=tele_menu(),
                )
            except Exception as ex:
                bot.send_message(cid, f"❌ Lỗi khi dọn dẹp: {html.escape(str(ex))}",
                                 parse_mode="HTML", reply_markup=tele_menu())

        threading.Thread(target=clean_worker, daemon=True).start()
        return

    if data == "tele_login":
        return flow_prompt(cid, c.message.message_id, "tele_phone", "🔐 ĐĂNG NHẬP TELE NGUỒN",
                           "Gửi số điện thoại kèm mã vùng, ví dụ <code>+849xxxxxxxx</code>.")

    if data == "tele_export_session":
        me, session_str = get_tele_account_details()
        if not session_str:
            return go("❌ Chưa có session nào. Hãy đăng nhập Tele trước.", tele_menu())
        return go(f"<b>🔑 SESSION TELE NGUỒN</b>\n{DIV}\n<code>{html.escape(session_str)}</code>", tele_menu())

    if data == "tele_status_btn":
        me, session_str = get_tele_account_details()
        return go(
            f"<b>📊 TRẠNG THÁI TELE</b>\n{DIV}\n"
            f"🔑 API ID/HASH: {'✅ Đã cấu hình' if TELETHON_API_ID and TELETHON_API_HASH else '❌ Thiếu'}\n"
            f"💾 Session: {'✅ Đã lưu' if session_str else '❌ Chưa có'}\n"
            f"🟢 Client: {'✅ Đang hoạt động' if tele_ready else '🔴 Ngoại tuyến'}\n"
            f"📡 Kênh: {html.escape(redeem_state.get('channel_name') or 'Chưa đặt')}",
            tele_menu(),
        )

    if data == "tele_logout_confirm":
        kb = telebot.types.InlineKeyboardMarkup(row_width=2)
        kb.add(_btn("🗑️ Xác nhận xoá", "tele_logout_yes"), _btn("↩️ Quay lại", "tele_panel"))
        return go("⚠️ Bạn chắc chắn muốn xoá session Tele nguồn?", kb)

    if data == "tele_logout_yes":
        if os.path.exists(TELE_SESSION_FILE):
            os.remove(TELE_SESSION_FILE)
        return go("✅ Đã xoá session Tele nguồn.", tele_menu())


# ========================= NHẬP LIỆU TỪ CHAT =========================
@bot.message_handler(func=lambda m: bool(m and m.chat and m.chat.id in user_flows), content_types=["text"])
def handle_menu_input(m):
    global REDEEM_ACCOUNTS
    cid = m.chat.id
    flow_state = user_flows.get(cid, {})
    flow = flow_state.get("step")
    value = (m.text or "").strip()

    try:
        bot.delete_message(cid, m.message_id)
    except Exception:
        pass

    if value.lower() in ("huy", "huỷ", "cancel", "/cancel"):
        user_flows.pop(cid, None)
        return bot.send_message(cid, "↩️ Đã huỷ.", reply_markup=main_menu(cid))

    if flow in ("user_login_game", "admin_login_game"):
        is_admin_flow = (flow == "admin_login_game")
        if is_admin_flow and cid != ADMIN_ID:
            user_flows.pop(cid, None)
            return

        parts = value.split(maxsplit=1)
        if len(parts) != 2:
            return bot.send_message(cid, "⚠️ Sai định dạng. Gửi: <code>TÀI_KHOẢN MẬT_KHẨU</code>", parse_mode="HTML")

        un, pw = parts[0].strip(), parts[1].strip()
        status_msg = bot.send_message(cid, f"🔄 Đang đăng nhập <code>{html.escape(un)}</code>...", parse_mode="HTML")

        def do_login_task():
            res = login_and_get_info(un, pw)
            back_kb = redeem_menu() if is_admin_flow else main_menu(cid)
            if "_error" in res:
                try:
                    bot.edit_message_text(f"❌ Đăng nhập thất bại: {html.escape(res['_error'])}",
                                          cid, status_msg.message_id, parse_mode="HTML", reply_markup=back_kb)
                except Exception:
                    pass
                return

            nick = res["nickname"]
            money = res["money"]
            save_game_account(cid, un, nick, res["token"], money, source="admin" if is_admin_flow else "user")
            upsert_redeem_account(un, res["pw_hashed"], nick, owner_id=cid, balance=money)
            user_flows.pop(cid, None)
            try:
                bot.edit_message_text(
                    f"✅ <b>Đăng nhập thành công</b>\n{DIV}\n"
                    f"👤 Nickname: <b>{html.escape(nick)}</b>\n"
                    f"🔑 Tài khoản: <code>{html.escape(un)}</code>\n"
                    f"💰 Số dư: <b>{money:,}</b>\n"
                    f"⚡ Đã sẵn sàng lụm code.",
                    cid, status_msg.message_id, parse_mode="HTML", reply_markup=back_kb,
                )
            except Exception:
                pass

        threading.Thread(target=do_login_task, daemon=True).start()
        return

    if flow in ("user_del_redeem", "admin_del_redeem"):
        is_admin_flow = (flow == "admin_del_redeem")
        if is_admin_flow and cid != ADMIN_ID:
            user_flows.pop(cid, None)
            return

        target = value.lower()
        new_list = []
        removed = []
        for acc in redeem_state.get("accounts", []):
            match = acc.get("un", "").lower() == target or acc.get("nick", "").lower() == target
            can_remove = is_admin_flow or (acc.get("owner") == cid)
            if match and can_remove:
                removed.append(acc.get("nick") or acc.get("un"))
            else:
                new_list.append(acc)

        back_kb = redeem_menu() if is_admin_flow else main_menu(cid)
        if not removed:
            return bot.send_message(cid, "❌ Không tìm thấy tài khoản phù hợp. Gửi lại tên khác.",
                                    reply_markup=back_kb)

        redeem_state["accounts"] = new_list
        save_json(REDEEM_STATE_FILE, redeem_state)
        REDEEM_ACCOUNTS = build_redeem_accounts()
        user_flows.pop(cid, None)
        return bot.send_message(cid, f"🗑️ Đã xoá: {', '.join(removed)}", reply_markup=back_kb)

    if flow == "proxy_add_link" and cid == ADMIN_ID:
        parts = value.split()
        link = parts[0].strip()
        burst = 10
        if len(parts) >= 2:
            try:
                burst = max(1, min(int(parts[1]), 30))
            except ValueError:
                return bot.send_message(cid, "⚠️ Số luồng bắn phải là số nguyên (từ 1 đến 30).")
        if not link.startswith("http://") and not link.startswith("https://"):
            return bot.send_message(cid, "⚠️ Link Webshare.io phải bắt đầu bằng <code>http://</code> hoặc <code>https://</code>", parse_mode="HTML")

        status_msg = bot.send_message(cid, "🔄 Đang kết nối Webshare.io để tải danh sách Proxy...")

        def load_proxy_task():
            try:
                plist = fetch_webshare_proxies(link)
                if not plist:
                    bot.edit_message_text("❌ Không tìm thấy proxy hợp lệ nào.", cid, status_msg.message_id, reply_markup=proxy_menu())
                    return
                is_direct_proxy = bool(parse_proxy_line(link) and "/api/" not in link and not link.endswith(".txt"))
                redeem_state["proxy_url"] = "Nhập trực tiếp" if is_direct_proxy else link
                redeem_state["proxy_list"] = plist
                redeem_state["burst_count"] = burst
                save_json(REDEEM_STATE_FILE, redeem_state)
                user_flows.pop(cid, None)
                bot.edit_message_text(
                    f"✅ <b>ĐÃ CẬP NHẬT PROXY WEBSHARE.IO</b>\n{DIV}\n"
                    f"🌐 Tổng số IP đang dùng: <b>{len(plist)}</b> proxy\n"
                    f"⚡ Số luồng bắn đồng thời/acc: <b>{burst}</b>\n"
                    f"🔄 Tự động xoay vòng IP liên tục khi có code!",
                    cid, status_msg.message_id, parse_mode="HTML", reply_markup=proxy_menu(),
                )
            except Exception as exc:
                bot.edit_message_text(
                    f"❌ Lỗi tải Proxy: {html.escape(str(exc))}",
                    cid, status_msg.message_id, parse_mode="HTML", reply_markup=proxy_menu(),
                )

        threading.Thread(target=load_proxy_task, daemon=True).start()
        return

    if flow == "set_channel" and cid == ADMIN_ID:
        target = value
        if "t.me/" in target:
            target = target.split("t.me/")[-1].strip("/").split("?")[0]
        if not target.startswith("@") and not target.lstrip("-").isdigit():
            target = "@" + target
        status_msg = bot.send_message(cid, f"🔄 Đang kết nối kênh <code>{html.escape(target)}</code>...",
                                      parse_mode="HTML")

        def resolve_ch():
            try:
                entity = tele_resolve_entity(target)
                peer_id = utils.get_peer_id(entity)
                redeem_state["channel_id"] = peer_id
                redeem_state["channel_name"] = getattr(entity, "title", None) or target
                save_json(REDEEM_STATE_FILE, redeem_state)
                user_flows.pop(cid, None)
                bot.edit_message_text(
                    f"✅ <b>Đã đặt kênh nhận code</b>\n📡 {html.escape(redeem_state['channel_name'])}\n🆔 <code>{peer_id}</code>",
                    cid, status_msg.message_id, parse_mode="HTML", reply_markup=redeem_menu(),
                )
            except Exception as exc:
                bot.edit_message_text(
                    f"❌ Lỗi đặt kênh: {html.escape(str(exc))}\n💡 Hãy chắc chắn đã đăng nhập Tele nguồn.",
                    cid, status_msg.message_id, parse_mode="HTML", reply_markup=redeem_menu(),
                )

        threading.Thread(target=resolve_ch, daemon=True).start()
        return

    if flow == "tele_phone" and cid == ADMIN_ID:
        if not TELETHON_API_ID or not TELETHON_API_HASH:
            user_flows.pop(cid, None)
            return bot.send_message(cid, "❌ Thiếu TELETHON_API_ID hoặc TELETHON_API_HASH.", reply_markup=tele_menu())
        status_msg = bot.send_message(cid, "📲 Đang gửi mã OTP...")

        def send_otp_task():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                client = TelegramClient(StringSession(), TELETHON_API_ID, TELETHON_API_HASH, loop=loop)
                loop.run_until_complete(client.connect())
                sent = loop.run_until_complete(client.send_code_request(value))
                tele_login_pending.update({"phone": value, "hash": sent.phone_code_hash, "client": client, "loop": loop})
                user_flows[cid] = {"step": "tele_otp"}
                bot.edit_message_text("📲 Đã gửi OTP. Nhập mã vào khung chat:", cid, status_msg.message_id)
            except Exception as exc:
                user_flows.pop(cid, None)
                bot.edit_message_text(f"❌ Lỗi gửi OTP: {html.escape(str(exc))}", cid, status_msg.message_id,
                                      parse_mode="HTML", reply_markup=tele_menu())

        threading.Thread(target=send_otp_task, daemon=True).start()
        return

    if flow == "tele_otp" and cid == ADMIN_ID:
        pending = tele_login_pending
        if not pending.get("client"):
            user_flows.pop(cid, None)
            return bot.send_message(cid, "❌ Phiên OTP đã hết hạn.", reply_markup=tele_menu())
        status_msg = bot.send_message(cid, "🔐 Đang xác thực OTP...")

        def verify_otp_task():
            client, loop = pending["client"], pending["loop"]
            try:
                loop.run_until_complete(client.sign_in(phone=pending["phone"], code=value,
                                                       phone_code_hash=pending["hash"]))
                _save_tele_session(client.session.save())
                tele_login_pending.clear()
                user_flows.pop(cid, None)
                bot.edit_message_text("✅ Đăng nhập Tele thành công. Đang bật canh kênh...",
                                      cid, status_msg.message_id, reply_markup=tele_menu())
                _activate_telethon_client(client, loop)
            except Exception as exc:
                if "SessionPasswordNeeded" in type(exc).__name__ or "password" in str(exc).lower():
                    user_flows[cid] = {"step": "tele_password"}
                    bot.edit_message_text("🔐 Tài khoản có bật 2FA. Nhập mật khẩu 2FA:",
                                          cid, status_msg.message_id)
                else:
                    bot.edit_message_text(f"❌ Lỗi OTP: {html.escape(str(exc))}", cid, status_msg.message_id,
                                          parse_mode="HTML", reply_markup=tele_menu())

        threading.Thread(target=verify_otp_task, daemon=True).start()
        return

    if flow == "tele_password" and cid == ADMIN_ID:
        pending = tele_login_pending
        status_msg = bot.send_message(cid, "🔐 Đang xác thực 2FA...")

        def verify_2fa_task():
            try:
                client, loop = pending["client"], pending["loop"]
                loop.run_until_complete(client.sign_in(password=value))
                _save_tele_session(client.session.save())
                tele_login_pending.clear()
                user_flows.pop(cid, None)
                bot.edit_message_text("✅ Đăng nhập Tele thành công. Đang bật canh kênh...",
                                      cid, status_msg.message_id, reply_markup=tele_menu())
                _activate_telethon_client(client, loop)
            except Exception as exc:
                bot.edit_message_text(f"❌ Sai mật khẩu 2FA: {html.escape(str(exc))}",
                                      cid, status_msg.message_id, parse_mode="HTML", reply_markup=tele_menu())

        threading.Thread(target=verify_2fa_task, daemon=True).start()
        return

    user_flows.pop(cid, None)
    return bot.send_message(cid, "⚠️ Phiên thao tác đã kết thúc.", reply_markup=main_menu(cid))


def init_redeem_tokens():
    if not REDEEM_ACCOUNTS:
        return
    for acc in REDEEM_ACCOUNTS:
        try:
            acc.refresh_token()
        except Exception:
            pass


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=init_redeem_tokens, daemon=True).start()
    if TELETHON_API_ID and TELETHON_API_HASH:
        threading.Thread(target=_start_telethon_listener, daemon=True).start()
    logger.info("✨ LỤM CODE LC79 - HỆ THỐNG SẴN SÀNG ✨")

    if bot:
        while True:
            try:
                bot.infinity_polling(timeout=20, long_polling_timeout=30)
            except Exception as e:
                logger.error(f"Bot crashed: {e}")
                time.sleep(5)
    else:
        while True:
            time.sleep(60)