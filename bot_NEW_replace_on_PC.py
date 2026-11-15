"""
Telegram order bot - Facebook post -> PDF service   (bKash AUTO-VERIFY version)

Customer flow:
  1. Customer pastes Facebook links (Share -> Copy link). 1 link or many. 5 taka per link.
  2. Checkout -> pays by bKash -> sends the TrxID.
  3. The phone app (bKash Bridge APK) forwards every bKash "You have received..." SMS.
     The bot matches TrxID + amount automatically and confirms the order by itself.
     A TrxID can be used ONCE only - after it confirms an order it is locked.
  4. AUTO_PDF=1: the bot opens every link in Chrome (fb2pdf_app.py), makes one PDF per link
     named Order_0012_01.pdf ... and sends each PDF to the customer.

If the SMS never arrives (phone off, wrong TrxID, number mismatch...) the order falls back
to YOUR manual Confirm / Reject buttons after PAY_WAIT_MIN minutes.

Run:  python bot.py
"""

import os
import re
import sys
import json
import hmac
import time
import hashlib
import sqlite3
import asyncio
import logging
import threading
import multiprocessing
from types import SimpleNamespace
from datetime import datetime

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

# ------------------------------------------------------------ config
# When packaged as an .exe, keep config.txt / orders.db next to the .exe (not in the temp folder)
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    # Settings file: config.txt (easy on Windows) or .env / config.env
    for name in ("config.txt", ".env", "config.env", "env.example.env"):
        p = os.path.join(BASE, name)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"'))
        break


load_env()
TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
BKASH = os.environ.get("BKASH_NUMBER", "01737250641")
PRICE = int(os.environ.get("PRICE_PER_LINK", "5"))
MIN_LINKS = int(os.environ.get("MIN_LINKS", "1"))
MAX_LINKS = int(os.environ.get("MAX_LINKS", "100"))
WORK_HOURS = os.environ.get("WORK_HOURS", "সকাল ৯টা – রাত ১১টা")
PROXY_URL = os.environ.get("PROXY_URL", "").strip()  # optional, e.g. socks5://127.0.0.1:1080
# Cloudflare Worker proxy for api.telegram.org (works when WiFi blocks Telegram).
# Override in config.txt with API_BASE_URL=..., or set API_BASE_URL= (empty) to use Telegram directly.
API_BASE_URL = os.environ.get(
    "API_BASE_URL", "https://tg-bot-proxy.snowai943.workers.dev/bot"
).strip().rstrip("/")
if API_BASE_URL and not API_BASE_URL.endswith("/bot"):
    API_BASE_URL += "/bot"
API_FILE_URL = API_BASE_URL[:-4] + "/file/bot" if API_BASE_URL else ""
DELIVERY_MINUTES = int(os.environ.get("DELIVERY_MINUTES", "30"))   # used for admin reminders
DELIVERY_TEXT = os.environ.get("DELIVERY_TEXT", "৫-১০ মিনিট")       # shown to customers

# ---- bKash SMS auto-verification (must match the phone app)
SMS_TOPIC = os.environ.get("SMS_TOPIC", "").strip()
SMS_SECRET = os.environ.get("SMS_SECRET", "").strip()
SMS_RELAY_URL = os.environ.get("SMS_RELAY_URL", "https://ntfy.sh").strip().rstrip("/")
SMS_AUTO = bool(SMS_TOPIC and SMS_SECRET)
PAY_WAIT_MIN = int(os.environ.get("PAY_WAIT_MIN", "10"))  # no SMS after this long -> ask admin

# ---- auto-PDF settings (config.txt)
AUTO_PDF = os.environ.get("AUTO_PDF", "1").strip().lower() not in ("0", "false", "no", "")
PDF_DIR = os.environ.get("PDF_DIR", "").strip() or os.path.join(BASE, "orders_pdf")
FB_DATA_DIR = os.environ.get("FB_DATA_DIR", "").strip() or os.path.join(BASE, "FB2PDF_data")
ASK_TIMEOUT_MIN = int(os.environ.get("ASK_TIMEOUT_MIN", "15"))
ALLOW_NO_PHOTO = os.environ.get("ALLOW_NO_PHOTO", "0").strip().lower() in ("1", "true", "yes")
DO_POST_PAGE = os.environ.get("DO_POST_PAGE", "1").strip().lower() not in ("0", "false", "no")
AUTO_RESUME_ON_START = os.environ.get("AUTO_RESUME_ON_START", "0").strip().lower() in ("1", "true", "yes")
MAX_TG_BYTES = 49 * 1024 * 1024  # Telegram bot upload limit is 50 MB

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

Engine = Cancelled = None
if AUTO_PDF:
    try:
        from fb2pdf_app import Engine, Cancelled  # the same engine as the FB2PDF app
    except Exception as e:  # missing module/package -> fall back to manual mode
        log.error("AUTO_PDF disabled: cannot import fb2pdf_app (%s). "
                  "Put fb2pdf_app.py next to bot.py and install its packages.", e)
        AUTO_PDF = False
WORKER = None

# ------------------------------------------------------------ texts (edit freely)
WELCOME = (
    "আসসালামু আলাইকুম! 👋\n\n"
    "এখানে Facebook পোস্ট (সব ছবি) Single PDF করে দেওয়া হয়।\n\n"
    "💰 প্রতিটি লিংক (১টি PDF) = {price} টাকা\n"
    "📦 ১টি লিংক দিলেও হবে, একসাথে অনেকগুলোও দিতে পারেন\n"
    "⏱ পেমেন্ট কনফার্ম হওয়ার পর সর্বোচ্চ {delivery} এর মধ্যে PDF পাবেন\n"
    "🕘 সার্ভিস সময়: {hours}\n\n"
    "লিংক কীভাবে দেবেন:\n"
    "১. Facebook পোস্টে Share বাটনে চাপ দিন\n"
    "২. Copy link সিলেক্ট করুন\n"
    "৩. এখানে পেস্ট করে পাঠান (একসাথে অনেকগুলোও দিতে পারেন)\n\n"
    "লিংক দেওয়া হলে ✅ Checkout চাপুন।\n\n"
    "ℹ️ শুধু যেসব পোস্ট দেখা যায় (post from public pages) সেগুলো করা সম্ভব।"
)

if SMS_AUTO:
    PAYMENT = (
        "🧾 অর্ডার #{id}\n"
        "লিংক: {n} টি\n"
        "মোট: {amount} টাকা\n\n"
        "bKash (Send Money): {bkash}\n\n"
        "টাকা পাঠানোর পর bKash এর মেসেজ থেকে TrxID টি এখানে লিখে পাঠান।\n"
        "উদাহরণ: 8N7A6D5E4F\n\n"
        "⚠️ ঠিক {amount} টাকা (বা বেশি) পাঠাতে হবে। একই TrxID দ্বিতীয়বার ব্যবহার করা যাবে না।"
    )
else:
    PAYMENT = (
        "🧾 অর্ডার #{id}\n"
        "লিংক: {n} টি\n"
        "মোট: {amount} টাকা\n\n"
        "bKash (Send Money): {bkash}\n\n"
        "টাকা পাঠিয়ে এখানে লিখে জানান: TrxID এবং যে নম্বর থেকে পাঠিয়েছেন।\n"
        "উদাহরণ: 8N7A6D5E4F 01XXXXXXXXX"
    )

BTN_CART = "🛒 আমার লিংক"
BTN_CHECKOUT = "✅ Checkout"
BTN_CLEAR = "🗑 মুছুন"
BTN_PRICE = "💰 রেট"
MENU = ReplyKeyboardMarkup(
    [[BTN_CART, BTN_CHECKOUT], [BTN_CLEAR, BTN_PRICE]], resize_keyboard=True
)

LINK_RE = re.compile(
    r"https?://(?:[\w-]+\.)*(?:facebook\.com|fb\.com|fb\.watch|fb\.me)/[^\s]+", re.I
)
TRX_RE = re.compile(r"\b[A-Z0-9]{8,12}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?88)?(01\d{9})(?!\d)")

# ------------------------------------------------------------ database
DB_PATH = os.path.join(BASE, "orders.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
conn.executescript(
    """
CREATE TABLE IF NOT EXISTS cart(
    user_id INTEGER, link TEXT, PRIMARY KEY(user_id, link));
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, chat_id INTEGER, username TEXT, name TEXT,
    links TEXT, amount INTEGER, proof TEXT, status TEXT,
    created REAL, confirmed REAL,
    delivered_count INTEGER DEFAULT 0, reminded INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS items(
    order_id INTEGER, idx INTEGER, link TEXT, status TEXT, file TEXT, err TEXT,
    PRIMARY KEY(order_id, idx));
CREATE TABLE IF NOT EXISTS payments(
    trx TEXT PRIMARY KEY, amount REAL, sender TEXT, received REAL, used_order INTEGER);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""
)
_order_cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
for _col, _decl in (("trx", "TEXT"), ("admin_notified", "INTEGER DEFAULT 0"), ("proof_at", "REAL")):
    if _col not in _order_cols:
        conn.execute(f"ALTER TABLE orders ADD COLUMN {_col} {_decl}")
conn.commit()


def kv_get(k, default=None):
    r = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(k, v):
    conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES (?,?)", (k, v))
    conn.commit()


def enqueue_items(row):
    """Create one queued job per link of a confirmed order (safe to call twice)."""
    if not AUTO_PDF:
        return
    links = json.loads(row["links"])
    done = row["delivered_count"] or 0
    for i, l in enumerate(links, 1):
        conn.execute(
            "INSERT OR IGNORE INTO items(order_id,idx,link,status) VALUES (?,?,?,?)",
            (row["id"], i, l, "done" if i <= done else "queued"))
    conn.commit()


def migrate_items():
    """Confirmed orders that existed before auto mode get their jobs too."""
    for r in conn.execute("SELECT * FROM orders WHERE status='confirmed'").fetchall():
        have = conn.execute("SELECT COUNT(*) c FROM items WHERE order_id=?", (r["id"],)).fetchone()["c"]
        if have == 0:
            enqueue_items(r)


def cart_links(uid):
    return [r["link"] for r in conn.execute("SELECT link FROM cart WHERE user_id=?", (uid,))]


def get_order(oid):
    return conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()


def is_admin(update: Update):
    return update.effective_user and update.effective_user.id == ADMIN_ID


async def send_long(bot, chat_id, text):
    for i in range(0, len(text), 3800):
        await bot.send_message(chat_id, text[i:i + 3800], disable_web_page_preview=True)


async def tell_admin(bot, text, markup=None):
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        log.error("Could not message admin: %s", e)


def cart_text(n):
    total = n * PRICE
    if n == 0:
        return "আপনার কোনো লিংক জমা নেই। Facebook পোস্টের লিংক পেস্ট করুন।"
    t = f"🔗 জমা আছে: {n} টি লিংক\n💰 মোট: {total} টাকা\n"
    if n < MIN_LINKS:
        t += f"\nআরও {MIN_LINKS - n} টি লিংক দিন (সর্বনিম্ন {MIN_LINKS} টি)।"
    else:
        t += "\nআরও লিংক দিতে পারেন, অথবা Checkout চাপুন।"
    return t


def checkout_markup(n):
    if n >= MIN_LINKS:
        return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Checkout", callback_data="checkout")]])
    return None


# ------------------------------------------------------------ bKash payment matching
def extract_trx(text):
    """First token that looks like a bKash TrxID (8-12 chars, letters AND digits)."""
    for tok in TRX_RE.findall((text or "").upper()):
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            return tok
    return None


def extract_phone(text):
    m = PHONE_RE.search(text or "")
    return m.group(1) if m else None


def phone_key(p):
    return re.sub(r"\D", "", p or "")[-10:]


def sign(kind, trx, amt, frm, ts):
    msg = f"{kind}|{trx}|{amt}|{frm}|{ts}"
    return hmac.new(SMS_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def trx_taken(trx, except_order=0):
    """True if this TrxID already paid for (or is being claimed by) another order."""
    o = conn.execute(
        "SELECT id FROM orders WHERE trx=? AND id<>? AND status IN ('confirmed','delivered','pending_review') LIMIT 1",
        (trx, except_order)).fetchone()
    p = conn.execute("SELECT used_order FROM payments WHERE trx=?", (trx,)).fetchone()
    return bool(o) or bool(p and p["used_order"] and p["used_order"] != except_order)


async def notify_admin_review(context, row, note=""):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"ok:{row['id']}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"no:{row['id']}"),
    ]])
    uname = f"@{row['username']}" if row["username"] else "(no username)"
    await tell_admin(
        context.bot,
        f"💳 Payment to verify{(' - ' + note) if note else ''}\nOrder #{row['id']}\n"
        f"User: {row['name']} {uname}\nLinks: {len(json.loads(row['links']))}\n"
        f"Amount: {row['amount']} taka\nProof: {row['proof']}", kb)
    conn.execute("UPDATE orders SET admin_notified=1 WHERE id=?", (row["id"],))
    conn.commit()


async def try_match(context, oid):
    """Match an order (that has a TrxID) against received bKash SMS payments.
    Returns: waiting | confirmed | short | review | used | none"""
    row = get_order(oid)
    if not row or row["status"] not in ("awaiting_payment", "pending_review") or not row["trx"]:
        return "none"
    pay = conn.execute("SELECT * FROM payments WHERE trx=?", (row["trx"],)).fetchone()
    if not pay:
        return "waiting"
    if pay["used_order"] and pay["used_order"] != row["id"]:
        conn.execute("UPDATE orders SET trx=NULL, status='awaiting_payment' WHERE id=?", (row["id"],))
        conn.commit()
        await context.bot.send_message(
            row["chat_id"], "❌ এই TrxID আগেই ব্যবহার করা হয়েছে। সঠিক TrxID পাঠান।")
        return "used"
    if pay["amount"] + 0.001 < row["amount"]:
        await context.bot.send_message(
            row["chat_id"],
            f"⚠️ আপনার পেমেন্ট {pay['amount']:g} টাকা, কিন্তু অর্ডার #{row['id']} এর মোট {row['amount']} টাকা।\n"
            "অ্যাডমিন দেখে জানাবেন।")
        if not row["admin_notified"]:
            await notify_admin_review(context, row, f"SHORT: paid {pay['amount']:g}, need {row['amount']}")
        return "short"
    given = extract_phone(row["proof"])
    if given and phone_key(given) != phone_key(pay["sender"]):
        await context.bot.send_message(
            row["chat_id"], "⏳ নম্বর মিলছে না, অ্যাডমিন যাচাই করে কনফার্ম করবেন।")
        if not row["admin_notified"]:
            await notify_admin_review(
                context, row, f"NUMBER MISMATCH: customer wrote {given}, SMS sender {pay['sender']}")
        return "review"
    # claim the payment (atomic) so it can never be used twice
    cur = conn.execute(
        "UPDATE payments SET used_order=? WHERE trx=? AND (used_order IS NULL OR used_order=?)",
        (row["id"], row["trx"], row["id"]))
    conn.commit()
    if cur.rowcount == 0:
        return "used"
    extra = f" (extra {pay['amount'] - row['amount']:g} tk)" if pay["amount"] - row["amount"] > 0.001 else ""
    await tell_admin(
        context.bot,
        f"💳 Auto-verified: order #{row['id']} | TrxID {row['trx']} | {pay['amount']:g} tk from {pay['sender']}{extra}")
    await do_confirm(context, row)
    return "confirmed"


async def handle_payment(context, trx, amt_str, sender):
    """A bKash 'received' SMS arrived from the phone app."""
    trx = (trx or "").upper().strip()
    try:
        amt = float(str(amt_str).replace(",", ""))
    except ValueError:
        return
    if not trx:
        return
    cur = conn.execute(
        "INSERT OR IGNORE INTO payments(trx,amount,sender,received) VALUES (?,?,?,?)",
        (trx, amt, sender, time.time()))
    conn.commit()
    if cur.rowcount == 0:
        return  # already known
    log.info("bKash SMS: TrxID %s, %s tk from %s", trx, amt, sender)
    orders = conn.execute("SELECT id, status FROM orders WHERE trx=? ORDER BY id DESC", (trx,)).fetchall()
    for o in orders:  # already paid for something (e.g. admin confirmed by hand)
        if o["status"] in ("confirmed", "delivered"):
            conn.execute("UPDATE payments SET used_order=? WHERE trx=? AND used_order IS NULL", (o["id"], trx))
            conn.commit()
            return
    for o in orders:
        if o["status"] in ("awaiting_payment", "pending_review"):
            await try_match(context, o["id"])
            return


def fetch_relay(since):
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    r = requests.get(f"{SMS_RELAY_URL}/{SMS_TOPIC}/json",
                     params={"poll": "1", "since": since}, timeout=20, proxies=proxies)
    r.raise_for_status()
    out = []
    for line in r.text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


_poll_fail = {"n": 0, "told": False}


async def poll_sms(context: ContextTypes.DEFAULT_TYPE):
    if not SMS_AUTO:
        return
    since = kv_get("sms_since") or "all"
    try:
        msgs = await asyncio.to_thread(fetch_relay, since)
    except Exception as e:
        _poll_fail["n"] += 1
        if _poll_fail["n"] in (1, 12) or _poll_fail["n"] % 120 == 0:
            log.warning("SMS relay not reachable (%s)", e)
        if isinstance(e, requests.HTTPError) and e.response is not None \
                and 400 <= e.response.status_code < 500 and since != "all":
            kv_set("sms_since", "all")  # old message id expired: re-read everything (safe, de-duplicated)
        if _poll_fail["n"] >= 60 and not _poll_fail["told"]:
            _poll_fail["told"] = True
            await tell_admin(context.bot, f"⚠️ Cannot reach the SMS relay ({SMS_RELAY_URL}) for 5+ minutes: {e}")
        return
    _poll_fail["n"] = 0
    _poll_fail["told"] = False
    for m in msgs:
        if m.get("event") != "message":
            continue
        try:
            d = json.loads(m.get("message", ""))
            kind = str(d.get("type", ""))
            trx, amt, frm, ts = (str(d.get(k, "")) for k in ("trx", "amt", "from", "ts"))
            ok = hmac.compare_digest(sign(kind, trx, amt, frm, ts), str(d.get("sig", "")))
        except Exception:
            ok = False
            kind = ""
        if ok and kind == "pay":
            try:
                await handle_payment(context, trx, amt, frm)
            except Exception as e:
                log.exception("handle_payment failed: %s", e)
        elif ok and kind == "test":
            await tell_admin(context.bot, "✅ Phone app is connected. Test message received.")
        else:
            log.warning("Ignored an SMS relay message with a bad signature / format")
        if m.get("id"):
            kv_set("sms_since", m["id"])


# ------------------------------------------------------------ auto PDF worker
class LogQ:
    """Stands in for the GUI queue: Engine.log()/status() just write to the bot log."""
    def put(self, item):
        try:
            kind, val = item
            if kind == "log":
                log.info("[pdf] %s", val)
        except Exception:
            pass


NEXT_SQL = (
    "SELECT i.order_id, i.idx, i.link FROM items i JOIN orders o ON o.id=i.order_id "
    "WHERE i.status='queued' AND o.status='confirmed' ORDER BY i.order_id, i.idx LIMIT 1")


class AutoWorker:
    """Background thread: takes queued links one by one, makes the PDF, sends it."""

    def __init__(self, loop, bot):
        self.loop, self.bot = loop, bot
        self.stop = threading.Event()
        self.paused = threading.Event()
        self.ask_ev = None
        self.eng = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="auto-pdf")

    # ---- bridge to the Telegram event loop
    def tg(self, coro, timeout=180):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def notify_admin(self, text):
        try:
            self.tg(self.bot.send_message(ADMIN_ID, text, disable_web_page_preview=True))
        except Exception as e:
            log.error("Could not message admin: %s", e)

    def ask(self, message):
        """Engine asks for help (login, post did not open...). Admin presses Continue in Telegram."""
        ev = threading.Event()
        self.ask_ev = ev
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶ Continue", callback_data="cont")]])
        try:
            self.tg(self.bot.send_message(
                ADMIN_ID, "🖥 Action needed on your PC:\n\n" + message +
                f"\n\n(waiting up to {ASK_TIMEOUT_MIN} min)", reply_markup=kb))
        except Exception as e:
            log.error("Could not send the prompt to admin: %s", e)
        end = time.time() + ASK_TIMEOUT_MIN * 60
        while not ev.wait(0.5):
            if self.stop.is_set() or time.time() > end:
                break
        self.ask_ev = None

    # ---- browser
    def close_browser(self):
        if self.eng:
            try:
                self.eng.quit()
            except Exception:
                pass
        self.eng = None

    def ensure_browser(self):
        if self.eng is not None:
            try:
                _ = self.eng.driver.current_url
                return
            except Exception:
                self.close_browser()
        eng = Engine(FB_DATA_DIR, LogQ(), self.ask, self.stop.is_set)
        eng.start_browser()
        try:
            eng.ensure_login()
        except Exception:
            eng.quit()
            raise
        self.eng = eng

    # ---- main loop
    def run(self):
        wconn = sqlite3.connect(DB_PATH, timeout=30)
        wconn.row_factory = sqlite3.Row
        wconn.execute("UPDATE items SET status='queued' WHERE status='running'")
        wconn.commit()
        log.info("Auto-PDF worker started. PDFs folder: %s", PDF_DIR)
        while not self.stop.is_set():
            try:
                if self.paused.is_set():
                    time.sleep(2)
                    continue
                it = wconn.execute(NEXT_SQL).fetchone()
                if not it:
                    self.close_browser()  # nothing to do -> close Chrome
                    time.sleep(3)
                    continue
                self.process(wconn, it)
            except Exception as e:
                if Cancelled and isinstance(e, Cancelled):
                    break
                log.exception("Worker error: %s", e)
                time.sleep(5)
        self.close_browser()
        log.info("Auto-PDF worker stopped.")

    def process(self, wconn, it):
        oid, idx, link = it["order_id"], it["idx"], it["link"]
        order = wconn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        total = wconn.execute("SELECT COUNT(*) c FROM items WHERE order_id=?", (oid,)).fetchone()["c"]
        wconn.execute("UPDATE items SET status='running' WHERE order_id=? AND idx=?", (oid, idx))
        wconn.commit()
        log.info("Order #%s link %s/%s: %s", oid, idx, total, link)

        out_dir = os.path.join(PDF_DIR, f"Order_{oid:04d}")
        os.makedirs(out_dir, exist_ok=True)
        name = f"Order_{oid:04d}_{idx:02d}.pdf"
        path = os.path.join(out_dir, name)

        err = None
        for attempt in (1, 2):
            err = None
            try:
                self.ensure_browser()
                n = self.eng.save_post(link, path, DO_POST_PAGE, True)
                if n == 0 and not ALLOW_NO_PHOTO:
                    err = "no photos found in this post (private post or wrong link?)"
            except Exception as e:
                if Cancelled and isinstance(e, Cancelled):
                    wconn.execute("UPDATE items SET status='queued' WHERE order_id=? AND idx=?", (oid, idx))
                    wconn.commit()
                    raise
                err = str(e) or type(e).__name__
            if not err:
                break
            log.warning("Order #%s link %s attempt %s failed: %s", oid, idx, attempt, err)
            if err.startswith("Not logged in"):
                break  # retrying will not help

        if err and err.startswith("Not logged in"):
            # not the link's fault: put it back, pause, and tell the admin
            wconn.execute("UPDATE items SET status='queued' WHERE order_id=? AND idx=?", (oid, idx))
            wconn.commit()
            self.paused.set()
            self.notify_admin(
                "🔒 Facebook login needed. Auto-PDF is PAUSED.\n"
                "Run the bot again or log in to Facebook in Chrome (FB2PDF_data profile), "
                "then send /resume. Nothing was lost - the links stay queued.")
            return

        if not err:
            size = os.path.getsize(path)
            if size > MAX_TG_BYTES:
                err = f"PDF is {size / 1048576:.0f} MB - over Telegram's 50 MB limit (file kept in {out_dir})"
        if not err:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                done_now = wconn.execute(
                    "SELECT COUNT(*) c FROM items WHERE order_id=? AND status='done'", (oid,)).fetchone()["c"] + 1
                self.tg(self.bot.send_document(
                    order["chat_id"], document=data, filename=name,
                    caption=f"📄 অর্ডার #{oid} - PDF {done_now}/{total}",
                    write_timeout=180, read_timeout=180, connect_timeout=60), timeout=300)
            except Exception as e:
                err = f"could not send to customer: {e}"

        if err:
            if os.path.exists(path) and "Telegram" not in err and "customer" not in err:
                try:
                    os.remove(path)
                except Exception:
                    pass
            wconn.execute("UPDATE items SET status='failed', err=? WHERE order_id=? AND idx=?", (err[:300], oid, idx))
        else:
            wconn.execute("UPDATE items SET status='done', file=?, err=NULL WHERE order_id=? AND idx=?", (path, oid, idx))
            wconn.execute("UPDATE orders SET delivered_count=(SELECT COUNT(*) FROM items WHERE order_id=? AND status='done') WHERE id=?", (oid, oid))
        wconn.commit()
        self.after_item(wconn, oid)

    def after_item(self, wconn, oid):
        """When the last queued link of an order has been handled, finish or report."""
        cnt = {r["status"]: r["c"] for r in wconn.execute(
            "SELECT status, COUNT(*) c FROM items WHERE order_id=? GROUP BY status", (oid,))}
        if cnt.get("queued") or cnt.get("running"):
            return
        order = wconn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        failed = wconn.execute(
            "SELECT idx, link, err FROM items WHERE order_id=? AND status='failed' ORDER BY idx", (oid,)).fetchall()
        if not failed and cnt.get("cancelled"):
            self.notify_admin(
                f"ℹ️ Order #{oid}: {cnt.get('done', 0)} delivered, {cnt['cancelled']} cancelled. "
                f"Send the rest by hand (caption {oid}) or close it with /finish {oid} <note>.")
        elif not failed:
            try:
                self.tg(finish_order(SimpleNamespace(bot=self.bot), order))
            except Exception as e:
                log.error("finish_order failed: %s", e)
            self.notify_admin(f"✅ Order #{oid} fully delivered automatically ({cnt.get('done', 0)} PDFs).")
        else:
            lines = [f"⚠️ Order #{oid}: {cnt.get('done', 0)} done, {len(failed)} FAILED"]
            for r in failed:
                lines.append(f"{r['idx']}. {r['link']}\n   → {r['err']}")
            lines.append(
                f"\nOptions:\n/retry {oid}  → try the failed links again\n"
                f"or make those PDFs yourself and send them to this bot with caption {oid}\n"
                f"/finish {oid} <note>  → close the order")
            self.notify_admin("\n".join(lines))


# ------------------------------------------------------------ customer handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        WELCOME.format(price=PRICE, min=MIN_LINKS, min_total=PRICE * MIN_LINKS, hours=WORK_HOURS,
                       delivery=DELIVERY_TEXT),
        reply_markup=MENU,
    )


async def do_checkout(context, user, chat_id):
    links = cart_links(user.id)
    n = len(links)
    if n < MIN_LINKS:
        await context.bot.send_message(
            chat_id, f"Checkout করতে কমপক্ষে {MIN_LINKS} টি লিংক লাগবে। এখন আছে {n} টি।"
        )
        return
    amount = n * PRICE
    cur = conn.execute(
        "INSERT INTO orders(user_id,chat_id,username,name,links,amount,status,created) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (user.id, chat_id, user.username or "", user.full_name, json.dumps(links),
         amount, "awaiting_payment", time.time()),
    )
    oid = cur.lastrowid
    conn.execute("DELETE FROM cart WHERE user_id=?", (user.id,))
    conn.commit()
    await context.bot.send_message(
        chat_id, PAYMENT.format(id=oid, n=n, amount=amount, bkash=BKASH)
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    text = (msg.text or "").strip()

    if text == BTN_CART:
        n = len(cart_links(user.id))
        await msg.reply_text(cart_text(n), reply_markup=checkout_markup(n))
        return
    if text == BTN_CHECKOUT:
        await do_checkout(context, user, msg.chat_id)
        return
    if text == BTN_CLEAR:
        conn.execute("DELETE FROM cart WHERE user_id=?", (user.id,))
        conn.commit()
        await msg.reply_text("🗑 আপনার জমা থাকা সব লিংক মুছে ফেলা হয়েছে।")
        return
    if text == BTN_PRICE:
        await msg.reply_text(
            f"প্রতি লিংক {PRICE} টাকা। ১টি লিংক = {PRICE} টাকা, {MIN_LINKS + 4} টি লিংক = {PRICE * (MIN_LINKS + 4)} টাকা।"
        )
        return

    # --- links
    found = [l.rstrip(".,)>]") for l in LINK_RE.findall(text)]
    if found:
        have = set(cart_links(user.id))
        added = 0
        for l in found:
            if l not in have and len(have) < MAX_LINKS:
                conn.execute("INSERT OR IGNORE INTO cart(user_id,link) VALUES (?,?)", (user.id, l))
                have.add(l)
                added += 1
        conn.commit()
        n = len(have)
        prefix = f"✅ {added} টি নতুন লিংক যোগ হয়েছে।\n\n" if added else "এই লিংক আগেই জমা আছে।\n\n"
        await msg.reply_text(prefix + cart_text(n), reply_markup=checkout_markup(n))
        return

    # --- payment proof (TrxID)
    row = conn.execute(
        "SELECT * FROM orders WHERE user_id=? AND status IN ('awaiting_payment','pending_review') "
        "ORDER BY id DESC LIMIT 1",
        (user.id,),
    ).fetchone()
    if row and len(text) >= 6:
        if not SMS_AUTO:
            # ---- old manual mode: admin presses Confirm / Reject
            conn.execute("UPDATE orders SET proof=?, status='pending_review', proof_at=? WHERE id=?",
                         (text, time.time(), row["id"]))
            conn.commit()
            await msg.reply_text("ধন্যবাদ! আপনার পেমেন্ট যাচাই করা হচ্ছে। কনফার্ম হলেই জানিয়ে দেব। ✅")
            await notify_admin_review(context, get_order(row["id"]))
            return

        trx = extract_trx(text)
        if not trx:
            await msg.reply_text(
                "TrxID বুঝতে পারিনি। bKash এর মেসেজে থাকা TrxID (যেমন 8N7A6D5E4F) লিখে পাঠান।")
            return
        if trx_taken(trx, row["id"]):
            await msg.reply_text("❌ এই TrxID আগেই ব্যবহার করা হয়েছে। সঠিক TrxID পাঠান।")
            return
        conn.execute(
            "UPDATE orders SET proof=?, trx=?, status='pending_review', proof_at=?, admin_notified=0 WHERE id=?",
            (text, trx, time.time(), row["id"]))
        conn.commit()
        res = await try_match(context, row["id"])
        if res == "waiting":
            await msg.reply_text(
                f"ধন্যবাদ! TrxID {trx} পেয়েছি। ⏳ পেমেন্ট যাচাই হচ্ছে — কনফার্ম হলেই নিজে থেকে জানিয়ে দেব "
                f"(১-২ মিনিট, দেরি হলে {PAY_WAIT_MIN} মিনিট পর্যন্ত লাগতে পারে)।")
        return

    await msg.reply_text("Facebook পোস্টের লিংক পেস্ট করুন, অথবা নিচের মেনু ব্যবহার করুন।", reply_markup=MENU)


# ------------------------------------------------------------ buttons
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "checkout":
        await do_checkout(context, q.from_user, q.message.chat_id)
        return

    if data == "cont":
        if q.from_user.id == ADMIN_ID and WORKER and WORKER.ask_ev:
            WORKER.ask_ev.set()
            await q.edit_message_text((q.message.text or "") + "\n\n▶ Continued")
        return

    if not data.startswith(("ok:", "no:")) or q.from_user.id != ADMIN_ID:
        return
    action, oid = data.split(":")
    row = get_order(int(oid))
    if not row:
        await q.edit_message_text("Order not found.")
        return
    if row["status"] not in ("pending_review", "awaiting_payment"):
        await q.answer(f"Already {row['status']}", show_alert=True)
        return

    if action == "ok":
        await q.edit_message_text(q.message.text + f"\n\n✅ CONFIRMED - deliver within {DELIVERY_MINUTES} min")
        await do_confirm(context, row)
    else:
        conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (row["id"],))
        conn.commit()
        await q.edit_message_text(q.message.text + "\n\n❌ REJECTED")
        await context.bot.send_message(
            row["chat_id"],
            f"❌ অর্ডার #{row['id']} এর পেমেন্ট খুঁজে পাওয়া যায়নি। "
            f"সঠিক TrxID আবার পাঠান, অথবা অ্যাডমিনকে জানান।",
        )


# ------------------------------------------------------------ admin: deliver PDFs
async def finish_order(context, row, note=""):
    conn.execute("UPDATE orders SET status='delivered' WHERE id=?", (row["id"],))
    conn.commit()
    await context.bot.send_message(
        row["chat_id"],
        f"✅ অর্ডার #{row['id']} সম্পন্ন। সব PDF পাঠানো হয়েছে।{note}\n"
        f"আবার দরকার হলে নতুন লিংক পাঠান। ধন্যবাদ! 🙏",
    )


async def on_admin_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    cap = (msg.caption or "").strip()
    m = re.search(r"\d+", cap)
    if m:
        context.bot_data["current"] = int(m.group())
    oid = context.bot_data.get("current")
    if not oid:
        await msg.reply_text("Caption e order number likhun (e.g. 12), or use /order 12 first.")
        return
    row = get_order(oid)
    if not row or row["status"] != "confirmed":
        await msg.reply_text(f"Order #{oid} is not in 'confirmed' state.")
        return
    total = len(json.loads(row["links"]))
    done = row["delivered_count"] + 1
    await context.bot.send_document(
        row["chat_id"], msg.document.file_id, caption=f"📄 অর্ডার #{oid} - PDF {done}/{total}"
    )
    conn.execute("UPDATE orders SET delivered_count=? WHERE id=?", (done, oid))
    it = conn.execute(
        "SELECT idx FROM items WHERE order_id=? AND status IN ('failed','queued') "
        "ORDER BY (status='failed') DESC, idx LIMIT 1", (oid,)).fetchone()
    if it:
        conn.execute("UPDATE items SET status='done', err=NULL WHERE order_id=? AND idx=?", (oid, it["idx"]))
    conn.commit()
    await msg.reply_text(f"Sent {done}/{total} for order #{oid}")
    if done >= total:
        await finish_order(context, get_order(oid))
        await msg.reply_text(f"Order #{oid} complete ✅")


# ------------------------------------------------------------ admin commands
async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    rows = conn.execute(
        "SELECT * FROM orders WHERE status IN ('pending_review','confirmed') ORDER BY id"
    ).fetchall()
    if not rows:
        await update.message.reply_text("No open orders. 🎉")
        return
    lines = []
    for r in rows:
        total = len(json.loads(r["links"]))
        age = ""
        if r["status"] == "confirmed" and r["confirmed"]:
            age = f" | {int((time.time() - r['confirmed']) // 60)} min ago"
        proof = f" | proof: {r['proof']}" if r["status"] == "pending_review" and r["proof"] else ""
        lines.append(f"#{r['id']} {r['status']} | {r['delivered_count']}/{total} done | {r['amount']} tk{age}{proof}")
    await update.message.reply_text("\n".join(lines))


async def cmd_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/payments - last 15 bKash SMS payments the phone app has reported."""
    if not is_admin(update):
        return
    rows = conn.execute("SELECT * FROM payments ORDER BY received DESC LIMIT 15").fetchall()
    if not rows:
        await update.message.reply_text("No bKash SMS received yet.")
        return
    lines = []
    for r in rows:
        when = datetime.fromtimestamp(r["received"]).strftime("%d/%m %H:%M")
        used = f"used by #{r['used_order']}" if r["used_order"] else "unused"
        lines.append(f"{when} | {r['trx']} | {r['amount']:g} tk | {r['sender']} | {used}")
    await update.message.reply_text("\n".join(lines))


async def do_confirm(context, row):
    conn.execute("UPDATE orders SET status='confirmed', confirmed=? WHERE id=?", (time.time(), row["id"]))
    if row["trx"]:  # lock the TrxID so it can never pay for another order
        conn.execute("UPDATE payments SET used_order=? WHERE trx=? AND used_order IS NULL", (row["id"], row["trx"]))
    conn.commit()
    await context.bot.send_message(
        row["chat_id"],
        f"✅ অর্ডার #{row['id']} এর পেমেন্ট কনফার্ম হয়েছে।\n"
        f"আগামী {DELIVERY_TEXT} এর মধ্যে আপনার PDF গুলো এখানে পাঠানো হবে।",
    )
    links = json.loads(row["links"])
    body = f"Order #{row['id']} links ({len(links)}):\n" + "\n".join(
        f"{i+1}. {l}" for i, l in enumerate(links))
    await send_long(context.bot, ADMIN_ID, body)
    context.bot_data["current"] = row["id"]
    if AUTO_PDF:
        enqueue_items(get_order(row["id"]))
        await context.bot.send_message(
            ADMIN_ID, f"🤖 Auto-PDF started for order #{row['id']} - PDFs will go to the customer by themselves.")


async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/confirm 1  - same as pressing the Confirm button."""
    if not is_admin(update) or not context.args:
        return
    row = get_order(int(context.args[0]))
    if not row or row["status"] not in ("pending_review", "awaiting_payment"):
        await update.message.reply_text("Order not found or not waiting for payment check.")
        return
    await do_confirm(context, row)
    await update.message.reply_text(f"Order #{row['id']} confirmed. Customer notified.")


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reject 1  - same as pressing the Reject button."""
    if not is_admin(update) or not context.args:
        return
    row = get_order(int(context.args[0]))
    if not row or row["status"] not in ("pending_review", "awaiting_payment"):
        await update.message.reply_text("Order not found or not waiting for payment check.")
        return
    conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (row["id"],))
    conn.commit()
    await context.bot.send_message(
        row["chat_id"],
        f"❌ অর্ডার #{row['id']} এর পেমেন্ট খুঁজে পাওয়া যায়নি। "
        f"সঠিক TrxID আবার পাঠান, অথবা অ্যাডমিনকে জানান।",
    )
    await update.message.reply_text(f"Order #{row['id']} rejected.")


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not context.args:
        return
    row = get_order(int(context.args[0]))
    if not row:
        await update.message.reply_text("Not found.")
        return
    links = json.loads(row["links"])
    await send_long(context.bot, ADMIN_ID, "\n".join(f"{i+1}. {l}" for i, l in enumerate(links)))


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not context.args:
        return
    context.bot_data["current"] = int(context.args[0])
    await update.message.reply_text(f"Current order set to #{context.args[0]}. Now send PDFs.")


async def cmd_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/finish 12   - close an order early (e.g. a few links failed)."""
    if not is_admin(update) or not context.args:
        return
    row = get_order(int(context.args[0]))
    if not row:
        return
    note = ""
    if len(context.args) > 1:
        note = "\n" + " ".join(context.args[1:])
    await finish_order(context, row, note)
    await update.message.reply_text("Closed.")


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/retry 12 - run the failed links of order 12 again."""
    if not is_admin(update) or not context.args:
        return
    oid = int(context.args[0])
    n = conn.execute(
        "UPDATE items SET status='queued', err=NULL WHERE order_id=? AND status='failed'", (oid,)).rowcount
    conn.commit()
    await update.message.reply_text(f"{n} failed link(s) of order #{oid} queued again.")


async def cmd_clearqueue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/clearqueue  or  /clearqueue 12 - cancel waiting/failed links (all orders, or one)."""
    if not is_admin(update):
        return
    if context.args:
        oid = int(context.args[0])
        n = conn.execute(
            "UPDATE items SET status='cancelled' WHERE status IN ('queued','running','failed') AND order_id=?",
            (oid,)).rowcount
    else:
        n = conn.execute(
            "UPDATE items SET status='cancelled' WHERE status IN ('queued','running','failed')").rowcount
    conn.commit()
    await update.message.reply_text(
        f"🗑 {n} link(s) cancelled. (A link already being processed may still finish.)")


async def cmd_autorun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/autorun 12 - put a confirmed order into the auto-PDF queue."""
    if not is_admin(update) or not context.args or not AUTO_PDF:
        return
    row = get_order(int(context.args[0]))
    if not row or row["status"] != "confirmed":
        await update.message.reply_text("Order not found or not in 'confirmed' state.")
        return
    enqueue_items(row)
    conn.execute("UPDATE items SET status='queued', err=NULL WHERE order_id=? AND status='cancelled'", (row["id"],))
    conn.commit()
    await update.message.reply_text(f"Order #{row['id']} queued for auto-PDF.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if WORKER:
        WORKER.paused.set()
    await update.message.reply_text("⏸ Auto-PDF paused (the current link finishes first). /resume to continue.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if WORKER:
        WORKER.paused.clear()
    await update.message.reply_text("▶ Auto-PDF running.")


async def cmd_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/continue - same as the Continue button."""
    if not is_admin(update):
        return
    if WORKER and WORKER.ask_ev:
        WORKER.ask_ev.set()
        await update.message.reply_text("▶ Continued.")
    else:
        await update.message.reply_text("Nothing is waiting for you.")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    rows = conn.execute(
        "SELECT order_id, status, COUNT(*) c FROM items WHERE order_id IN "
        "(SELECT order_id FROM items WHERE status IN ('queued','running','failed')) "
        "GROUP BY order_id, status ORDER BY order_id").fetchall()
    if not rows:
        await update.message.reply_text("Queue is empty. 🎉")
        return
    by = {}
    for r in rows:
        by.setdefault(r["order_id"], []).append(f"{r['status']} {r['c']}")
    state = "⏸ PAUSED" if (WORKER and WORKER.paused.is_set()) else "running"
    text = f"Auto-PDF: {state}\n" + "\n".join(f"#{o}: " + ", ".join(v) for o, v in by.items())
    nxt = conn.execute(
        "SELECT order_id, idx, link, status FROM items WHERE status IN ('running','queued') "
        "ORDER BY (status='running') DESC, order_id, idx LIMIT 6").fetchall()
    if nxt:
        text += "\n\nNext:\n" + "\n".join(f"#{r['order_id']}.{r['idx']} [{r['status']}] {r['link']}" for r in nxt)
    await send_long(context.bot, update.effective_chat.id, text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    start_today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    start_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()

    def q(since):
        r = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders "
            "WHERE status IN ('confirmed','delivered') AND confirmed>=?", (since,)).fetchone()
        return r["c"], r["s"]

    tc, ts = q(start_today)
    mc, ms = q(start_month)
    await update.message.reply_text(f"Today: {tc} orders, {ts} taka\nThis month: {mc} orders, {ms} taka")


# ------------------------------------------------------------ reminders
async def reminder(context: ContextTypes.DEFAULT_TYPE):
    warn_after = max(1, int(DELIVERY_MINUTES * 0.75))
    cutoff = time.time() - warn_after * 60
    rows = conn.execute(
        "SELECT * FROM orders WHERE status='confirmed' AND reminded=0 AND confirmed<?", (cutoff,)
    ).fetchall()
    for r in rows:
        await context.bot.send_message(
            ADMIN_ID,
            f"⏰ Order #{r['id']} confirmed {warn_after}+ min ago - "
            f"deadline in ~{DELIVERY_MINUTES - warn_after} min!"
        )
        conn.execute("UPDATE orders SET reminded=1 WHERE id=?", (r["id"],))
    conn.commit()

    # payment proof sent but no matching SMS for PAY_WAIT_MIN minutes -> ask the admin
    stale = conn.execute(
        "SELECT * FROM orders WHERE status='pending_review' AND admin_notified=0 AND proof_at<?",
        (time.time() - PAY_WAIT_MIN * 60,)).fetchall()
    for r in stale:
        await notify_admin_review(context, r, f"no bKash SMS found after {PAY_WAIT_MIN} min")
        try:
            await context.bot.send_message(
                r["chat_id"], "⏳ পেমেন্ট এখনো যাচাই হয়নি, অ্যাডমিন নিজে দেখে কনফার্ম করবেন।")
        except Exception:
            pass


async def on_start(app):
    """Runs once when the bot starts: tells the admin it is online."""
    try:
        me = await app.bot.get_me()
        await app.bot.send_message(
            ADMIN_ID,
            f"✅ Bot @{me.username} is online. You are the ADMIN.\n"
            f"Payment checks will arrive here. Try /orders /payments",
        )
        log.info("Startup message sent to admin %s", ADMIN_ID)
        if AUTO_PDF:
            await app.bot.send_message(ADMIN_ID, "🤖 Auto-PDF mode is ON. Commands: /queue /pause /resume /retry N /continue")
        else:
            await app.bot.send_message(ADMIN_ID, "ℹ️ Auto-PDF mode is OFF (manual mode).")
        if SMS_AUTO:
            await app.bot.send_message(
                ADMIN_ID, "📲 bKash SMS auto-verify is ON. Press 'Send test' in the phone app to check the link.")
        else:
            await app.bot.send_message(
                ADMIN_ID, "ℹ️ bKash SMS auto-verify is OFF (SMS_TOPIC / SMS_SECRET not set in config.txt). "
                          "Every payment needs your manual Confirm.")
    except Exception as e:
        log.error(
            "COULD NOT MESSAGE ADMIN (ADMIN_ID=%s): %s\n"
            "Fix: open your bot from the admin Telegram account, press START, "
            "and make sure ADMIN_ID in config.txt is that account's number.",
            ADMIN_ID, e,
        )

    global WORKER
    if AUTO_PDF:
        WORKER = AutoWorker(asyncio.get_running_loop(), app.bot)
        waiting = conn.execute(
            "SELECT COUNT(*) c FROM items i JOIN orders o ON o.id=i.order_id "
            "WHERE i.status IN ('queued','running') AND o.status='confirmed'").fetchone()["c"]
        if waiting and not AUTO_RESUME_ON_START:
            WORKER.paused.set()
            try:
                await app.bot.send_message(
                    ADMIN_ID,
                    f"⏸ {waiting} link(s) were still waiting from before. Auto-PDF is PAUSED.\n"
                    "/queue → see them\n/resume → start working on them\n"
                    "/clearqueue → cancel them (or /clearqueue 12 for one order)")
            except Exception:
                pass
        WORKER.thread.start()


async def on_stop(app):
    if WORKER:
        WORKER.stop.set()
        WORKER.thread.join(timeout=15)


# ------------------------------------------------------------ main
def main():
    if not TOKEN or not ADMIN_ID:
        raise SystemExit("Fill BOT_TOKEN and ADMIN_ID in config.txt (same folder as bot.py), save, then run again.")
    builder = (
        Application.builder()
        .token(TOKEN)
        .post_init(on_start)
        .post_shutdown(on_stop)
        .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
        .get_updates_connect_timeout(30).get_updates_read_timeout(40)
        .get_updates_write_timeout(30).get_updates_pool_timeout(30)
    )
    if API_BASE_URL:
        # PTB appends the token itself: <base_url><TOKEN>/<method>
        builder = builder.base_url(API_BASE_URL).base_file_url(API_FILE_URL)
        log.info("Using Telegram API proxy: %s", API_BASE_URL)
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
        log.info("Using proxy: %s", PROXY_URL)
    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("payments", cmd_payments))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("finish", cmd_finish))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("clearqueue", cmd_clearqueue))
    app.add_handler(CommandHandler("autorun", cmd_autorun))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("continue", cmd_continue))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.User(user_id=ADMIN_ID), on_admin_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue:
        app.job_queue.run_repeating(reminder, interval=60, first=30)
        if SMS_AUTO:
            app.job_queue.run_repeating(poll_sms, interval=5, first=5)
            log.info("bKash SMS auto-verify ON (relay %s)", SMS_RELAY_URL)
    else:
        log.warning("Install python-telegram-bot[job-queue] to enable reminders and SMS auto-verify.")

    log.info("Bot is running...")
    # bootstrap_retries=-1: keep retrying until the connection to Telegram works
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    multiprocessing.freeze_support()  # needed when packaged as .exe (Chrome driver helper)
    main()
