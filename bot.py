# bot.py
# QLottery / Tài Xỉu Bot - full implementation (auto 60s rounds)
# Features:
# - /start grants 80k once per account (requires 8 wager rounds to free-to-withdraw)
# - Admin approve groups; /batdau requests approval
# - Bets: /T<amount> for Tài, /X<amount> for Xỉu (in group when running & approved)
# - Auto cycle 60s; countdown 30s/10s/5s; lock chat at 5s; send GIF spin then 3 dice sequentially
# - Random rule: time (HHMM as number) + last4(round_epoch) parity -> odd = Tài, even = Xỉu
# - Promo code creation / redeem; promo requires N rounds wagering
# - Pot ("hũ") mechanics (house share goes to pot; triple1/6 distributes pot proportionally)
# - Admin commands: /addmoney, /top10, /balances, /code, /nhancode, /KqTai /KqXiu /bettai /betxiu /tatbet
# - Private menu (Game, Nạp, Rút, Số dư)
# - Database SQLite (tx_bot_data.db by default)
# - Uses python-telegram-bot v20+ style async Application

import os
import sys
import sqlite3
import random
import traceback
import logging
import threading
import http.server
import socketserver
import asyncio
from datetime import datetime
from typing import List, Tuple, Optional, Dict, Any
import secrets

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton, ChatPermissions
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, Application
)

# -----------------------
# Keep port open (for Render)
# -----------------------
def keep_port_open():
    PORT = int(os.getenv("PORT", "10000"))
    handler = http.server.SimpleHTTPRequestHandler
    try:
        with socketserver.TCPServer(("", PORT), handler) as httpd:
            print(f"[keep_port_open] serving on port {PORT}")
            httpd.serve_forever()
    except Exception as e:
        print(f"[keep_port_open] {e}")

threading.Thread(target=keep_port_open, daemon=True).start()

# -----------------------
# Configuration
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")  # set in Render env ideally
ADMIN_IDS = [7760459637, 6942793864]  # add admin ids here or via env var separated by comma
ROUND_SECONDS = int(os.getenv("ROUND_SECONDS", "60"))
MIN_BET = int(os.getenv("MIN_BET", "1000"))
START_BONUS = int(os.getenv("START_BONUS", "80000"))  # 80k as requested
START_BONUS_REQUIRED_ROUNDS = int(os.getenv("START_BONUS_REQUIRED_ROUNDS", "8"))
WIN_MULTIPLIER = float(os.getenv("WIN_MULTIPLIER", "1.97"))
HOUSE_RATE = float(os.getenv("HOUSE_RATE", "0.03"))
DB_FILE = os.getenv("DB_FILE", "tx_bot_data.db")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))
# GIF for 3D dice spin (your provided link)
DICE_SPIN_GIF_URL = os.getenv("DICE_SPIN_GIF_URL", "https://www.emojiall.com/images/60/telegram/1f3b2.gif")

# logging
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------
# Database helpers
# -----------------------
def get_db_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance REAL DEFAULT 0,
        total_deposited REAL DEFAULT 0,
        total_bet_volume REAL DEFAULT 0,
        current_streak INTEGER DEFAULT 0,
        best_streak INTEGER DEFAULT 0,
        created_at TEXT,
        start_bonus_given INTEGER DEFAULT 0,
        start_bonus_progress INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS groups (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        approved INTEGER DEFAULT 0,
        running INTEGER DEFAULT 0,
        bet_mode TEXT DEFAULT 'random',
        last_round INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        round_id TEXT,
        user_id INTEGER,
        side TEXT, -- 'tai' or 'xiu'
        amount REAL,
        timestamp TEXT
    );

    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        round_index INTEGER,
        round_id TEXT,
        result TEXT,
        dice TEXT,
        timestamp TEXT
    );

    CREATE TABLE IF NOT EXISTS pot (
        id INTEGER PRIMARY KEY CHECK (id=1),
        amount REAL DEFAULT 0
    );
    """)
    cur.execute("INSERT OR IGNORE INTO pot(id, amount) VALUES (1, 0)")
    # promo tables
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS promo_codes (
        code TEXT PRIMARY KEY,
        amount REAL,
        wager_required INTEGER,
        used INTEGER DEFAULT 0,
        created_by INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS promo_redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT,
        user_id INTEGER,
        amount REAL,
        wager_required INTEGER,
        wager_progress INTEGER DEFAULT 0,
        last_counted_round TEXT DEFAULT '',
        active INTEGER DEFAULT 1,
        redeemed_at TEXT
    );
    """)
    conn.commit()
    conn.close()

def db_execute(query: str, params: Tuple = ()):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    lastrowid = cur.lastrowid
    conn.close()
    return lastrowid

def db_query(query: str, params: Tuple = ()):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows

# -----------------------
# User helpers
# -----------------------
def now_iso():
    return datetime.utcnow().isoformat()

def ensure_user(user_id: int, username: str = "", first_name: str = ""):
    rows = db_query("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if not rows:
        db_execute(
            "INSERT INTO users(user_id, username, first_name, balance, total_deposited, total_bet_volume, current_streak, best_streak, created_at, start_bonus_given, start_bonus_progress) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username or "", first_name or "", 0.0, 0.0, 0.0, 0, 0, now_iso(), 0, 0)
        )

def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    rows = db_query("SELECT * FROM users WHERE user_id=?", (user_id,))
    return dict(rows[0]) if rows else None

def add_balance(user_id: int, amount: float):
    ensure_user(user_id, "", "")
    u = get_user(user_id)
    new_bal = (u["balance"] or 0.0) + amount
    db_execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, user_id))
    return new_bal

def set_balance(user_id: int, amount: float):
    ensure_user(user_id, "", "")
    db_execute("UPDATE users SET balance=? WHERE user_id=?", (amount, user_id))

def add_to_pot(amount: float):
    rows = db_query("SELECT amount FROM pot WHERE id=1")
    current = rows[0]["amount"] if rows else 0.0
    new = current + amount
    db_execute("UPDATE pot SET amount=? WHERE id=1", (new,))

def get_pot_amount() -> float:
    rows = db_query("SELECT amount FROM pot WHERE id=1")
    return rows[0]["amount"] if rows else 0.0

def reset_pot():
    db_execute("UPDATE pot SET amount=? WHERE id=1", (0.0,))

# -----------------------
# Dice logic
# -----------------------
DICE_CHARS = ["\u2680", "\u2681", "\u2682", "\u2683", "\u2684", "\u2685"]
WHITE = "⚪"  # Xỉu
BLACK = "⚫"  # Tài

def roll_one_die() -> int:
    return random.randint(1, 6)

def roll_three_dice_random() -> Tuple[List[int], int, Optional[str]]:
    a = roll_one_die()
    b = roll_one_die()
    c = roll_one_die()
    dice = [a, b, c]
    total = sum(dice)
    special = None
    if dice.count(1) == 3:
        special = "triple1"
    elif dice.count(6) == 3:
        special = "triple6"
    return dice, total, special

def result_from_total(total: int) -> str:
    if 11 <= total <= 17:
        return "tai"
    elif 4 <= total <= 10:
        return "xiu"
    else:
        return "invalid"

def decide_result_by_time_rule(round_epoch: int) -> str:
    now = datetime.utcnow()
    hhmm = now.hour * 100 + now.minute  # e.g., 7:54 -> 754
    last4 = int(str(round_epoch)[-4:]) if round_epoch is not None else 0
    s = hhmm + last4
    return "tai" if (s % 2 == 1) else "xiu"

# -----------------------
# Chat lock/unlock and countdown
# -----------------------
async def lock_group_chat(bot, chat_id: int):
    try:
        perms = ChatPermissions(can_send_messages=False)
        await bot.set_chat_permissions(chat_id=chat_id, permissions=perms)
    except Exception:
        pass

async def unlock_group_chat(bot, chat_id: int):
    try:
        perms = ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
        await bot.set_chat_permissions(chat_id=chat_id, permissions=perms)
    except Exception:
        pass

async def send_countdown(bot, chat_id: int, seconds: int):
    try:
        if seconds == 30:
            await bot.send_message(chat_id=chat_id, text="⏰ Còn 30 giây trước khi quay kết quả — nhanh tay cược!")
        elif seconds == 10:
            await bot.send_message(chat_id=chat_id, text="⚠️ Còn 10 giây! Sắp khóa cược.")
        elif seconds == 5:
            await bot.send_message(chat_id=chat_id, text="🔒 Còn 5 giây — Chat bị khóa để chốt cược.")
            await lock_group_chat(bot, chat_id)
    except Exception:
        pass

# -----------------------
# UI / menu in private only
# -----------------------
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Game"), KeyboardButton("Nạp tiền")],
        [KeyboardButton("Rút tiền"), KeyboardButton("Số dư")]
    ],
    resize_keyboard=True
)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "", user.first_name or "")
    u = get_user(user.id)
    greeted = False
    if u and u.get("start_bonus_given", 0) == 0:
        add_balance(user.id, START_BONUS)
        db_execute("UPDATE users SET total_deposited=COALESCE(total_deposited,0)+?, start_bonus_given=1, start_bonus_progress=0 WHERE user_id=?", (START_BONUS, user.id))
        greeted = True

    text = f"Xin chào {user.first_name or 'bạn'}! 👋\nChào mừng đến phòng Tài Xỉu tự động.\n"
    if greeted:
        text += f"Bạn đã nhận {START_BONUS:,}₫ miễn phí (một lần). Để rút, hãy cược ít nhất {START_BONUS_REQUIRED_ROUNDS} vòng. Liên hệ admin để đổi quy chế.\n\n"
    text += "Menu:\n- Game\n- Nạp tiền\n- Rút tiền\n- Số dư\n\n(Lưu ý: Menu chỉ hiện trong tin nhắn riêng với bot.)"
    await update.message.reply_text(text, reply_markup=MAIN_MENU)

async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt == "game":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Room Tài Xỉu", callback_data="game_tx")],
            [InlineKeyboardButton("Chẵn lẻ (update)", callback_data="game_cl")],
            [InlineKeyboardButton("Sicbo (update)", callback_data="game_sb")]
        ])
        await update.message.reply_text("Chọn game:", reply_markup=kb)
    elif txt in ("nạp tiền", "nap tien", "nạp"):
        await update.message.reply_text("Liên hệ để nạp: @HOANGDUNGG789")
    elif txt in ("rút tiền", "rut tien", "ruttien"):
        await ruttien_help(update, context)
    elif txt in ("số dư", "so du"):
        u = get_user(update.effective_user.id)
        bal = int(u["balance"]) if u else 0
        await update.message.reply_text(f"Số dư hiện tại: {bal:,}₫")

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "game_tx":
        await q.message.reply_text("Room Tài Xỉu: Đặt cược trong nhóm bằng /T<amount> cho Tài hoặc /X<amount> cho Xỉu. Link: @VET789cc")
    elif q.data in ("game_cl","game_sb"):
        await q.message.reply_text("Sẽ cập nhật sau.")

# -----------------------
# Withdraw handlers
# -----------------------
async def withdraw_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) < 5:
        await query.edit_message_text("Dữ liệu không hợp lệ.")
        return

    action = parts[0]
    try:
        user_id = int(parts[1])
        amount = int(parts[2])
        bank = parts[3]
        account = parts[4]
    except:
        await query.edit_message_text("Dữ liệu không hợp lệ.")
        return

    # ✅ Chỉ admin mới có quyền duyệt
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("Chỉ admin mới thao tác.")
        return

    # ✅ Nếu admin duyệt rút tiền
    if action == "withdraw_ok":
        u = get_user(user_id)
        if not u:
            await query.edit_message_text("User không tồn tại.")
            return

        # 📌 1️⃣ Kiểm tra số dư
        if u["balance"] < amount:
            await query.edit_message_text("User không đủ tiền.")
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Yêu cầu rút {amount:,}₫ bị từ chối: số dư không đủ."
                )
            except:
                pass
            return

        # 📌 2️⃣ Giới hạn rút tối đa 1.000.000đ/ngày
        today = datetime.utcnow().date()
        total_today = db_query_one(
            "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE user_id=? AND DATE(created_at)=?",
            (user_id, today.isoformat())
        )[0]

        if total_today + amount > 1_000_000:
            await query.edit_message_text(f"Yêu cầu rút {amount:,}₫ bị từ chối (vượt giới hạn 1.000.000đ/ngày).")
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Bạn đã vượt giới hạn rút tối đa 1.000.000₫ trong ngày. Hãy thử lại vào ngày mai."
                )
            except:
                pass
            return

        # 📌 3️⃣ Cập nhật số dư
        new_bal = u["balance"] - amount
        db_execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, user_id))

        # 📌 4️⃣ Ghi lịch sử rút
        db_execute(
            "INSERT INTO withdrawals (user_id, amount, created_at) VALUES (?, ?, ?)",
            (user_id, amount, datetime.utcnow().isoformat())
        )

        # 📌 5️⃣ Gửi thông báo
        await query.edit_message_text(f"✅ Đã xác nhận rút {amount:,}₫ cho user {user_id}.")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ Yêu cầu rút {amount:,}₫ đã được duyệt bởi admin.\nNgân hàng: {bank}\nSố TK: {account}"
            )
        except:
            pass

    # ❌ Nếu admin từ chối
    else:
        await query.edit_message_text(
            f"Yêu cầu rút {amount:,}₫ đã bị từ chối bởi admin {query.from_user.id}."
        )
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ Yêu cầu rút {amount:,}₫ đã bị từ chối."
            )
        except:
            pass
        
# -----------------------------
# ✅ BET HANDLER (T/X + /T/X)
# -----------------------------
async def bet_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    # ✅ Chấp nhận cả /T1000, /X1000 và T1000, X1000
    if text.startswith("/"):
        cmd = text[1:]
    else:
        cmd = text

    if len(cmd) < 2:
        return

    prefix = cmd[0].lower()
    if prefix not in ("t", "x"):
        return

    side = "tai" if prefix == "t" else "xiu"

    # ✅ Parse tiền cược
    try:
        amount = int(cmd[1:])
    except:
        await msg.reply_text("❌ Cú pháp đặt cược sai. Ví dụ: /T1000 hoặc X5000")
        return

    if amount < MIN_BET:
        await msg.reply_text(f"⚠️ Đặt cược tối thiểu {MIN_BET:,}₫")
        return

    user = update.effective_user
    chat = update.effective_chat

    # ✅ Chỉ cho phép cược trong group
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Lệnh cược chỉ dùng trong nhóm.")
        return

    # ✅ Kiểm tra nhóm đã duyệt & đang chạy
    g = db_query("SELECT approved, running FROM groups WHERE chat_id=?", (chat.id,))
    if not g or g[0]["approved"] != 1 or g[0]["running"] != 1:
        await msg.reply_text("Nhóm này chưa được admin duyệt hoặc chưa bật /batdau.")
        return

    ensure_user(user.id, user.username or "", user.first_name or "")
    u = get_user(user.id)
    if not u or (u["balance"] or 0.0) < amount:
        await msg.reply_text("❌ Số dư không đủ.")
        return

    # ✅ Trừ tiền ngay & cộng tổng cược
    new_balance = (u["balance"] or 0.0) - amount
    new_total_bet = (u["total_bet_volume"] or 0.0) + amount
    db_execute(
        "UPDATE users SET balance=?, total_bet_volume=? WHERE user_id=?",
        (new_balance, new_total_bet, user.id)
    )

    # ✅ Lưu cược vào DB
    now_ts = int(datetime.utcnow().timestamp())
    round_epoch = now_ts // ROUND_SECONDS
    round_id = f"{chat.id}_{round_epoch}"
    db_execute(
        "INSERT INTO bets(chat_id, round_id, user_id, side, amount, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (chat.id, round_id, user.id, side, amount, now_iso())
    )

    # ✅ Update bonus start progress nếu có
    try:
        rows = db_query("SELECT start_bonus_given, start_bonus_progress FROM users WHERE user_id=?", (user.id,))
        if rows and rows[0]["start_bonus_given"] == 1:
            new_prog = (rows[0]["start_bonus_progress"] or 0) + 1
            db_execute("UPDATE users SET start_bonus_progress=? WHERE user_id=?", (new_prog, user.id))
    except Exception:
        logger.exception("start bonus progress update failed")

    # ✅ Update promo wager progress nếu có
    try:
        await update_promo_wager_progress(context, user.id, round_id)
    except Exception:
        logger.exception("promo progress failed")

    # ✅ Phản hồi không kèm số dư
    await msg.reply_text(f"✅ Đã đặt {side.upper()} {amount:,}₫ cho phiên hiện tại.")
# -----------------------
# Admin handlers
# -----------------------
async def addmoney_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Chỉ admin mới dùng lệnh này.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Cú pháp: /addmoney <user_id> <amount>")
        return
    try:
        uid = int(args[0]); amt = float(args[1])
    except:
        await update.message.reply_text("Tham số không hợp lệ.")
        return
    ensure_user(uid, "", "")
    new_bal = add_balance(uid, amt)
    db_execute("UPDATE users SET total_deposited=COALESCE(total_deposited,0)+? WHERE user_id=?", (amt, uid))
    await update.message.reply_text(f"Đã cộng {int(amt):,}₫ cho user {uid}. Số dư hiện: {int(new_bal):,}₫")
    try:
        await context.bot.send_message(chat_id=uid, text=f"Bạn vừa được admin cộng {int(amt):,}₫. Số dư: {int(new_bal):,}₫")
    except:
        pass

async def top10_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Chỉ admin.")
        return
    rows = db_query("SELECT user_id, total_deposited FROM users ORDER BY total_deposited DESC LIMIT 10")
    text = "Top 10 nạp nhiều nhất:\n"
    for i, r in enumerate(rows, start=1):
        text += f"{i}. {r['user_id']} — {int(r['total_deposited'] or 0):,}₫\n"
    await update.message.reply_text(text)

async def balances_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Chỉ admin.")
        return
    rows = db_query("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 50")
    text = "Top balances:\n"
    for r in rows:
        text += f"- {r['user_id']}: {int(r['balance'] or 0):,}₫\n"
    await update.message.reply_text(text)

# admin force commands
async def admin_force_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Chỉ admin.")
        return
    text = update.message.text.strip()
    cmd = text.split()[0].lower()
    args = context.args
    if not args:
        await update.message.reply_text("Cú pháp: /KqTai <chat_id> hoặc /bettai <chat_id>")
        return
    try:
        chat_id = int(args[0])
    except:
        await update.message.reply_text("chat_id không hợp lệ.")
        return
    if cmd == "/kqtai":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("force_tai", chat_id))
        await update.message.reply_text(f"Đã đặt force TÀI cho nhóm {chat_id}.")
    elif cmd == "/kqxiu":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("force_xiu", chat_id))
        await update.message.reply_text(f"Đã đặt force XỈU cho nhóm {chat_id}.")
    elif cmd == "/bettai":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("bettai", chat_id))
        await update.message.reply_text(f"Đã bật cầu bệt TÀI cho nhóm {chat_id}.")
    elif cmd == "/betxiu":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("betxiu", chat_id))
        await update.message.reply_text(f"Đã bật cầu bệt XỈU cho nhóm {chat_id}.")
    elif cmd == "/tatbet":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("random", chat_id))
        await update.message.reply_text(f"Đã trả về chế độ random cho nhóm {chat_id}.")
    else:
        await update.message.reply_text("Lệnh admin không hợp lệ.")

# -----------------------
# Promo code handlers
# -----------------------
def ensure_promo_tables():
    # already created in init_db safely (idempotent)
    pass

async def admin_create_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Chỉ admin.")
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Cú pháp: /code <amount> <wager_rounds>")
        return
    try:
        amount = int(float(context.args[0])); wager_required = int(context.args[1])
    except:
        await update.message.reply_text("Tham số không hợp lệ.")
        return
    code = secrets.token_hex(4).upper()
    created_at = now_iso()
    db_execute("INSERT INTO promo_codes(code, amount, wager_required, used, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
               (code, amount, wager_required, 0, update.effective_user.id, created_at))
    await update.message.reply_text(f"Đã tạo code `{code}` — {int(amount):,}₫ — phải cược {wager_required} vòng. Người dùng nhập /nhancode {code}", parse_mode="Markdown")

async def redeem_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Cú pháp: /nhancode <CODE>")
        return
    code = context.args[0].strip().upper()
    rows = db_query("SELECT code, amount, wager_required, used FROM promo_codes WHERE code=?", (code,))
    if not rows:
        await update.message.reply_text("Code không tồn tại.")
        return
    row = rows[0]
    if row["used"] == 1:
        await update.message.reply_text("Code đã được sử dụng.")
        return
    # mark used
    db_execute("UPDATE promo_codes SET used=1 WHERE code=?", (code,))
    amount = row["amount"]; wager = int(row["wager_required"])
    ensure_user(update.effective_user.id, update.effective_user.username or "", update.effective_user.first_name or "")
    add_balance(update.effective_user.id, amount)
    db_execute("INSERT INTO promo_redemptions(code, user_id, amount, wager_required, wager_progress, last_counted_round, active, redeemed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (code, update.effective_user.id, amount, wager, 0, "", 1, now_iso()))
    await update.message.reply_text(f"Bạn nhận {int(amount):,}₫ từ code {code}. Phải cược {wager} vòng để hợp lệ.")

async def update_promo_wager_progress(context: ContextTypes.DEFAULT_TYPE, user_id: int, round_id: str):
    try:
        rows = db_query("SELECT id, code, wager_required, wager_progress, last_counted_round, active, amount FROM promo_redemptions WHERE user_id=? AND active=1", (user_id,))
        if not rows:
            return
        for r in rows:
            rid = r["id"]; last = r["last_counted_round"] or ""
            if str(last) == str(round_id):
                continue
            new_progress = (r["wager_progress"] or 0) + 1
            active = 1
            if new_progress >= (r["wager_required"] or 0):
                active = 0
            db_execute("UPDATE promo_redemptions SET wager_progress=?, last_counted_round=?, active=? WHERE id=?", (new_progress, str(round_id), active, rid))
            if active == 0:
                try:
                    await context.bot.send_message(chat_id=user_id, text=f"✅ Bạn đã hoàn thành yêu cầu cược cho code {r['code']}! Tiền {int(r['amount']):,}₫ hiện đã hợp lệ.")
                except Exception:
                    pass
    except Exception:
        logger.exception("update_promo_wager_progress failed")

# -----------------------
# Group approval command /batdau & approve callback
# -----------------------
async def batdau_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group","supergroup"):
        await update.message.reply_text("/batdau chỉ dùng trong nhóm.")
        return
    title = chat.title or ""
    rows = db_query("SELECT chat_id FROM groups WHERE chat_id=?", (chat.id,))
    if not rows:
        db_execute("INSERT INTO groups(chat_id, title, approved, running, bet_mode, last_round) VALUES (?, ?, 0, 0, 'random', ?)", (chat.id, title, 0))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Duyệt", callback_data=f"approve|{chat.id}"),
         InlineKeyboardButton("Từ chối", callback_data=f"deny|{chat.id}")]
    ])
    text = f"Yêu cầu bật bot cho nhóm:\n{title}\nchat_id: {chat.id}\nNgười yêu cầu: {update.effective_user.id}"
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=text, reply_markup=kb)
        except Exception:
            logger.exception("Cannot notify admin for group approval")
    await update.message.reply_text("Đã gửi yêu cầu tới admin để duyệt.")

async def approve_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 2:
        await query.edit_message_text("Dữ liệu không hợp lệ.")
        return
    action, chat_id_s = parts
    try:
        chat_id = int(chat_id_s)
    except:
        await query.edit_message_text("chat_id không hợp lệ.")
        return
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("Chỉ admin mới thao tác.")
        return
    if action == "approve":
        db_execute("UPDATE groups SET approved=1, running=1 WHERE chat_id=?", (chat_id,))
        await query.edit_message_text(f"Đã duyệt và bật chạy cho nhóm {chat_id}.")
        try:
            await context.bot.send_message(chat_id=chat_id, text="Bot đã được admin duyệt — bắt đầu chạy phiên mỗi 60s. Gõ /batdau để yêu cầu chạy lại.")
        except:
            pass
    else:
        db_execute("UPDATE groups SET approved=0, running=0 WHERE chat_id=?", (chat_id,))
        await query.edit_message_text(f"Đã từ chối cho nhóm {chat_id}.")

# -----------------------
# Rounds engine: orchestration
# -----------------------
def get_active_groups() -> List[Dict[str, Any]]:
    rows = db_query("SELECT chat_id, bet_mode, last_round FROM groups WHERE approved=1 AND running=1")
    return [dict(r) for r in rows]

def format_history_line(chat_id: int) -> str:
    rows = db_query("SELECT result FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, MAX_HISTORY))
    results = [r["result"] for r in reversed(rows)]
    mapped = []
    for r in results:
        mapped.append(BLACK if r == "tai" else WHITE)
    return " ".join(mapped)

async def run_round_for_group(app: Application, chat_id: int):
    """
    Xử lý 1 phiên cho nhóm:
    - xác định round_id
    - gom cược
    - (nếu có) áp dụng chế độ ép kết quả / cầu bệt
    - hiển thị GIF quay (nếu có) và gửi từng xúc xắc cách đều
    - tính kết quả, lưu history
    - trả thưởng cho người thắng, đưa tiền thua + house_share vào pot
    - xử lý special triple -> chia pot
    - gửi thông báo kết quả tới group và summary cho admin
    """
    try:
        # --- chuẩn bị phiên ---
        now_ts = int(datetime.utcnow().timestamp())
        round_epoch = now_ts // ROUND_SECONDS
        round_index = round_epoch
        round_id = f"{chat_id}_{round_epoch}"

        # Lấy cược của phiên (nếu có)
        bets_rows = db_query("SELECT user_id, side, amount FROM bets WHERE chat_id=? AND round_id=?", (chat_id, round_id))
        bets = [dict(r) for r in bets_rows] if bets_rows else []

        # Lấy chế độ nhóm
        grows = db_query("SELECT bet_mode FROM groups WHERE chat_id=?", (chat_id,))
        bet_mode = grows[0]["bet_mode"] if grows else "random"

        # Quyết định forced/bettai/betxiu
        forced_value = None
        if bet_mode == "force_tai":
            forced_value = "tai"
            # revert one-shot
            db_execute("UPDATE groups SET bet_mode='random' WHERE chat_id=?", (chat_id,))
        elif bet_mode == "force_xiu":
            forced_value = "xiu"
            db_execute("UPDATE groups SET bet_mode='random' WHERE chat_id=?", (chat_id,))
        elif bet_mode == "bettai":
            forced_value = "tai"
        elif bet_mode == "betxiu":
            forced_value = "xiu"

        # Thông báo bắt đầu tung xúc xắc
        try:
            await app.bot.send_message(chat_id=chat_id, text=f"🎲 Phiên {round_index} — Đang tung xúc xắc...")
        except Exception:
            # không quan trọng nếu gửi thất bại
            pass

        # --- Quay xúc xắc ---
        dice = []
        special = None
        total = 0

        # Nếu có GIF spin, gửi GIF 3D quay (nếu định nghĩa DICE_SPIN_GIF_URL)
        if 'DICE_SPIN_GIF_URL' in globals() and DICE_SPIN_GIF_URL:
            try:
                await app.bot.send_animation(chat_id=chat_id, animation=DICE_SPIN_GIF_URL, caption="🔄 Quay xúc xắc...")
                # chờ GIF quay (điều chỉnh thời gian nếu cần)
                await asyncio.sleep(1.2)
            except Exception:
                pass

        if forced_value:
            # tìm 1 bộ xúc xắc phù hợp với forced_value (giới hạn số lần thử để tránh loop)
            attempts = 0
            dice, total, special = roll_three_dice_random()
            while result_from_total(total) != forced_value and attempts < 200:
                dice, total, special = roll_three_dice_random()
                attempts += 1
            # gửi từng viên ra cho đẹp
            for val in dice:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[val-1]}")
                except Exception:
                    pass
                await asyncio.sleep(1.0)
        else:
            # bình thường: tung từng viên và gửi từng viên 1s-1.2s
            a = roll_one_die()
            dice.append(a)
            try:
                await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[a-1]}")
            except Exception:
                pass
            await asyncio.sleep(1.2)

            b = roll_one_die()
            dice.append(b)
            try:
                await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[b-1]}")
            except Exception:
                pass
            await asyncio.sleep(1.2)

            c = roll_one_die()
            dice.append(c)
            try:
                await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[c-1]}")
            except Exception:
                pass

            total = sum(dice)
            if dice.count(1) == 3:
                special = "triple1"
            elif dice.count(6) == 3:
                special = "triple6"

        # đảm bảo total được set cho trường hợp forced
        if total == 0:
            total = sum(dice)

        # Kết quả cuối cùng
        result = result_from_total(total)

        # --- Lưu lịch sử ---
        dice_str = ",".join(map(str, dice))
        try:
            db_execute(
                "INSERT INTO history(chat_id, round_index, round_id, result, dice, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, round_index, round_id, result, dice_str, now_iso())
            )
        except Exception:
            logger.exception("Failed to insert history")

        # --- Tính winners / losers ---
        winners = []
        losers = []
        total_winner_bets = 0.0
        total_loser_bets = 0.0

        for b in bets:
            side = b.get("side")
            amt = float(b.get("amount") or 0.0)
            if side == result:
                winners.append((b["user_id"], amt))
                total_winner_bets += amt
            else:
                losers.append((b["user_id"], amt))
                total_loser_bets += amt

        # --- Chuyển tiền thua vào pot (atomic) ---
        try:
            if total_loser_bets > 0:
                db_execute("UPDATE pot SET amount = amount + ? WHERE id = 1", (total_loser_bets,))
        except Exception:
            logger.exception("Failed to add losers to pot")

        # --- Trả thưởng cho winners ---
        winners_paid = []
        for uid, amt in winners:
            try:
                # tính house share và payout
                house_share = int(round(amt * HOUSE_RATE))
                payout = int(round(amt * WIN_MULTIPLIER))

                ensure_user(uid, "", "")

                # cộng house share vào pot
                if house_share > 0:
                    try:
                        db_execute("UPDATE pot SET amount = amount + ? WHERE id = 1", (house_share,))
                    except Exception:
                        logger.exception("Failed to add house share to pot")

                # cập nhật balance và streak bằng 1 câu lệnh UPDATE (nếu DB SQLite hỗ trợ COALESCE)
                try:
                    db_execute(
                        """
                        UPDATE users SET
                            balance = COALESCE(balance, 0) + ?,
                            current_streak = COALESCE(current_streak, 0) + 1,
                            best_streak = CASE
                                WHEN COALESCE(current_streak, 0) + 1 > COALESCE(best_streak, 0)
                                THEN COALESCE(current_streak, 0) + 1
                                ELSE COALESCE(best_streak, 0)
                            END
                        WHERE user_id = ?
                        """,
                        (payout, uid)
                    )
                except Exception:
                    # fallback: đọc rồi cập nhật
                    logger.exception("Atomic update failed for user, falling back to read-then-write")
                    u = get_user(uid) or {"balance": 0, "current_streak": 0, "best_streak": 0}
                    new_balance = (u.get("balance") or 0) + payout
                    new_cur = (u.get("current_streak") or 0) + 1
                    new_best = max(u.get("best_streak") or 0, new_cur)
                    db_execute("UPDATE users SET balance=?, current_streak=?, best_streak=? WHERE user_id=?", (new_balance, new_cur, new_best, uid))

                winners_paid.append((uid, payout, int(amt)))
            except Exception:
                logger.exception(f"Error paying winner {uid}")
                # thông báo admin nếu muốn
                for aid in ADMIN_IDS:
                    try:
                        await app.bot.send_message(chat_id=aid, text=f"ERROR paying winner {uid} in group {chat_id}")
                    except Exception:
                        pass

        # --- Reset streak losers ---
        for uid, amt in losers:
            try:
                db_execute("UPDATE users SET current_streak=0 WHERE user_id=?", (uid,))
            except Exception:
                logger.exception(f"Failed to reset streak for user {uid}")

        # --- Special triple handling: chia pot cho winners tỉ lệ cược ---
        special_msg = ""
        try:
            if special in ("triple1", "triple6"):
                pot_amount = get_pot_amount()
                if pot_amount > 0 and winners:
                    total_bets_win = sum([amt for (_, amt) in winners]) or 0.0
                    if total_bets_win > 0:
                        for uid, amt in winners:
                            share = (amt / total_bets_win) * pot_amount
                            ensure_user(uid, "", "")
                            u = get_user(uid)
                            db_execute("UPDATE users SET balance = COALESCE(balance,0) + ? WHERE user_id=?", (share, uid))
                        special_msg = f"Hũ {int(pot_amount):,}₫ đã được chia cho người thắng theo tỷ lệ cược!"
                        reset_pot()
        except Exception:
            logger.exception("Error handling special triple")

        # --- Xóa cược cho phiên này (sau khi đã trả) ---
        try:
            db_execute("DELETE FROM bets WHERE chat_id=? AND round_id=?", (chat_id, round_id))
        except Exception:
            logger.exception("Failed to delete bets for round after settlement")

        # --- Chuẩn bị tin nhắn gửi nhóm ---
        display = "Tài" if result == "tai" else "Xỉu"
        symbol = BLACK if result == "tai" else WHITE
        history_line = format_history_line(chat_id)

        msg = f"▶️ Phiên {round_index} — Kết quả: {display} {symbol}\n"
        msg += f"Xúc xắc: {' '.join([DICE_CHARS[d-1] for d in dice])} — Tổng: {total}\n"
        if special_msg:
            msg += f"\n{special_msg}\n"
        if history_line:
            msg += f"\nLịch sử ({MAX_HISTORY} gần nhất):\n{history_line}\n"

        try:
            await app.bot.send_message(chat_id=chat_id, text=msg)
        except Exception:
            logger.exception("Cannot send round result to group")

        # --- Gửi tóm tắt cho admin (nếu cần) ---
        if winners_paid:
            admin_summary = f"Round {round_index} in group {chat_id} completed.\nResult: {result}\nWinners:\n"
            for uid, payout, amt in winners_paid:
                admin_summary += f"- {uid}: đặt {int(amt):,} -> nhận {int(payout):,}\n"
            for aid in ADMIN_IDS:
                try:
                    await app.bot.send_message(chat_id=aid, text=admin_summary)
                except Exception:
                    logger.exception(f"Không gửi được admin summary cho admin {aid}")

    except Exception as e:
        logger.exception("Exception in run_round_for_group")
        # notify admins about fatal exception for this group
        for aid in ADMIN_IDS:
            try:
                await app.bot.send_message(chat_id=aid, text=f"ERROR - run_round_for_group exception for group {chat_id}: {e}\n{traceback.format_exc()}")
            except Exception:
                pass
             
async def run_round_for_group(app, chat_id, round_epoch):
    try:
        # ----- Xử lý xúc xắc + kết quả -----
        dice = roll_dice(3)
        total = sum(dice)
        result = "tai" if total >= 11 else "xiu"  # hoặc tùy luật bạn dùng
        round_id = round_epoch

        # ----- Lấy danh sách cược -----
        bets = db_query("SELECT * FROM bets WHERE chat_id=? AND round_id=?", (chat_id, round_id))

        winners = []
        losers = []

        for b in bets:
            uid = b["user_id"]
            amt = float(b["amount"] or 0.0)
            if b["side"] == result:
                winners.append((uid, amt))
            else:
                losers.append((uid, amt))

        # ----- Cộng tiền cho winners -----
        for uid, amt in winners:
            payout = amt * 2  # thắng ăn gấp đôi, bạn có thể chỉnh nếu cần
            try:
                db_execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (payout, uid))
            except Exception:
                logger.exception(f"Failed to pay winner {uid}")

        # ----- Reset streak cho losers -----
        for uid, amt in losers:
            try:
                db_execute("UPDATE users SET current_streak=0 WHERE user_id=?", (uid,))
            except Exception:
                logger.exception(f"Failed to reset streak for user {uid}")

        # ----- Gửi kết quả vòng -----
        display = "Tài" if result == "tai" else "Xỉu"
        symbol = BLACK if result == "tai" else WHITE
        msg = f"▶️ Phiên {round_id} — Kết quả: {display} {symbol}\n"
        msg += f"Xúc xắc: {' '.join([DICE_CHARS[d-1] for d in dice])} — Tổng: {total}\n"
        await app.bot.send_message(chat_id=chat_id, text=msg)

        # ----- Gửi admin summary -----
        if winners:
            admin_summary = f"Round {round_id} | Group {chat_id}\nResult: {result}\nWinners:\n"
            for uid, amt in winners:
                admin_summary += f"- {uid}: đặt {int(amt):,} → nhận {int(amt*2):,}\n"
            for aid in ADMIN_IDS:
                try:
                    await app.bot.send_message(chat_id=aid, text=admin_summary)
                except:
                    pass

    except Exception as e:
        logger.exception(f"Exception in run_round_for_group: {e}")
        # --- logic xử lý vòng chơi ---
        # (tính result, winners, losers, settle tiền, v.v...)

        # 🟡 Gửi kết quả cho group
        display = "Tài" if result == "tai" else "Xỉu"
        symbol = BLACK if result == "tai" else WHITE
        history_line = format_history_line(chat_id)
        msg = f"▶️ Phiên {round_index} — Kết quả: {display} {symbol}\n"
        msg += f"Xúc xắc: {' '.join([DICE_CHARS[d-1] for d in dice])} — Tổng: {total}\n"
        if special_msg:
            msg += f"\n{special_msg}\n"
        if history_line:
            msg += f"\nLịch sử ({MAX_HISTORY} gần nhất):\n{history_line}\n"
        try:
            await app.bot.send_message(chat_id=chat_id, text=msg)
        except:
            logger.exception("Cannot send round result to group")

        # 🟢 Gửi tóm tắt cho admin
        if winners_paid:
            admin_summary = f"Round {round_index} in group {chat_id} completed.\nResult: {result}\nWinners:\n"
            for uid, payout, amt in winners_paid:
                admin_summary += f"- {uid}: đặt {int(amt):,} -> nhận {int(payout):,}\n"
            for aid in ADMIN_IDS:
                try:
                    await app.bot.send_message(chat_id=aid, text=admin_summary)
                except:
                    pass

    except Exception as e:
        logger.exception(f"Exception in run_round_for_group: {e}")

        # unlock group
        try:
            await unlock_group_chat(app.bot, chat_id)
        except:
            pass

    except Exception as e:
        logger.exception("Exception in run_round_for_group")
        for aid in ADMIN_IDS:
            try:
                await app.bot.send_message(
                    chat_id=aid,
                    text=f"ERROR - run_round_for_group exception for group {chat_id}: {e}\n{traceback.format_exc()}"
                )
            except:
                pass

# rounds orchestrator: waits for epoch boundaries and coordinates countdowns
async def rounds_loop(app: Application):
    logger.info("Rounds orchestrator started")
    await asyncio.sleep(2)
    while True:
        try:
            now_ts = int(datetime.utcnow().timestamp())
            next_epoch_ts = ((now_ts // ROUND_SECONDS) + 1) * ROUND_SECONDS
            rem = next_epoch_ts - now_ts

            if rem > 30:
                await asyncio.sleep(rem - 30)
                rows = db_query("SELECT chat_id FROM groups WHERE approved=1 AND running=1")
                for r in rows:
                    asyncio.create_task(send_countdown(app.bot, r["chat_id"], 30))
                await asyncio.sleep(20)
                rows = db_query("SELECT chat_id FROM groups WHERE approved=1 AND running=1")
                for r in rows:
                    asyncio.create_task(send_countdown(app.bot, r["chat_id"], 10))
                await asyncio.sleep(5)
                rows = db_query("SELECT chat_id FROM groups WHERE approved=1 AND running=1")
                for r in rows:
                    asyncio.create_task(send_countdown(app.bot, r["chat_id"], 5))
                await asyncio.sleep(5)
            else:
                # if less than 30s remain, send appropriate countdowns
                if rem > 10:
                    await asyncio.sleep(rem - 10)
                    rows = db_query("SELECT chat_id FROM groups WHERE approved=1 AND running=1")
                    for r in rows:
                        asyncio.create_task(send_countdown(app.bot, r["chat_id"], 10))
                    await asyncio.sleep(5)
                    rows = db_query("SELECT chat_id FROM groups WHERE approved=1 AND running=1")
                    for r in rows:
                        asyncio.create_task(send_countdown(app.bot, r["chat_id"], 5))
                    await asyncio.sleep(5)
                elif rem > 5:
                    await asyncio.sleep(rem - 5)
                    rows = db_query("SELECT chat_id FROM groups WHERE approved=1 AND running=1")
                    for r in rows:
                        asyncio.create_task(send_countdown(app.bot, r["chat_id"], 5))
                    await asyncio.sleep(5)
                else:
                    # rem <=5
                    rows = db_query("SELECT chat_id FROM groups WHERE approved=1 AND running=1")
                    for r in rows:
                        asyncio.create_task(send_countdown(app.bot, r["chat_id"], 5))
                    await asyncio.sleep(rem)

            # run rounds at boundary
            round_epoch = int(datetime.utcnow().timestamp()) // ROUND_SECONDS
            rows = db_query("SELECT chat_id FROM groups WHERE approved=1 AND running=1")
            tasks = []
            for r in rows:
                tasks.append(asyncio.create_task(run_round_for_group(app, r["chat_id"], round_epoch)))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception:
            logger.exception("Exception in rounds_loop")
            for aid in ADMIN_IDS:
                try:
                    await app.bot.send_message(chat_id=aid, text=f"ERROR - rounds_loop exception:\n{traceback.format_exc()}")
                except:
                    pass

# -----------------------
# Startup / Shutdown + Main entrypoint (PTB v20+ chuẩn)
# -----------------------
import traceback
import asyncio
import random

async def run_round_for_group(app, chat_id, round_epoch):
    """
    Xử lý 1 vòng chơi cho group chat_id.
    round_epoch được rounds_loop tính sẵn -> dùng để tạo round_id nhất quán.
    """
    try:
        round_index = int(round_epoch)
        round_id = f"{chat_id}_{round_epoch}"

        # lấy cược cho chính round này (chỉ round_id hiện tại)
        bets_rows = db_query("SELECT user_id, side, amount FROM bets WHERE chat_id=? AND round_id=?", (chat_id, round_id))
        bets = [dict(r) for r in bets_rows] if bets_rows else []

        # lấy chế độ nhóm (force/bettai...)
        grows = db_query("SELECT bet_mode FROM groups WHERE chat_id=?", (chat_id,))
        bet_mode = grows[0]["bet_mode"] if grows else "random"

        # quyết định forcedValue nếu admin đã set
        forced_value = None
        if bet_mode == "force_tai":
            forced_value = "tai"
            # revert one-shot
            db_execute("UPDATE groups SET bet_mode='random' WHERE chat_id=?", (chat_id,))
        elif bet_mode == "force_xiu":
            forced_value = "xiu"
            db_execute("UPDATE groups SET bet_mode='random' WHERE chat_id=?", (chat_id,))
        elif bet_mode == "bettai":
            forced_value = "tai"
        elif bet_mode == "betxiu":
            forced_value = "xiu"

        # Gửi thông báo bắt đầu/quay (GIF nếu có)
        try:
            # nếu bạn có biến DICE_SPIN_GIF_URL (đặt ở đầu file) dùng GIF 3D, nếu không có sẽ bỏ qua
            if 'DICE_SPIN_GIF_URL' in globals() and DICE_SPIN_GIF_URL:
                try:
                    await app.bot.send_animation(chat_id=chat_id, animation=DICE_SPIN_GIF_URL, caption="🔄 Quay xúc xắc...")
                    await asyncio.sleep(0.8)
                except Exception:
                    pass
            else:
                # fallback: 1 tin nhắn text thông báo
                await app.bot.send_message(chat_id=chat_id, text=f"🎲 Phiên {round_index} — Đang tung xúc xắc...")
        except Exception:
            pass

        # Tạo kết quả: nếu có forced_value thì tìm dice phù hợp, còn không thì random từng viên
        dice = []
        special = None
        total = 0

        if forced_value:
            # generate until match or timeout
            attempts = 0
            dice, total, special = roll_three_dice_random()
            while result_from_total(total) != forced_value and attempts < 200:
                dice, total, special = roll_three_dice_random()
                attempts += 1
            # send GIF already gửi, bây giờ gửi từng viên để hiển thị
            for v in dice:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[v-1]}")
                except:
                    pass
                await asyncio.sleep(1.0)
        else:
            # gửi lần lượt 3 viên, 1s mỗi viên
            a = roll_one_die(); dice.append(a)
            try: await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[a-1]}")
            except: pass
            await asyncio.sleep(1.0)

            b = roll_one_die(); dice.append(b)
            try: await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[b-1]}")
            except: pass
            await asyncio.sleep(1.0)

            c = roll_one_die(); dice.append(c)
            try: await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[c-1]}")
            except: pass

            total = sum(dice)
            if dice.count(1) == 3:
                special = "triple1"
            elif dice.count(6) == 3:
                special = "triple6"

            # ensure total assigned if not above
            if total == 0:
                total = sum(dice)

        # compute final result
        result = result_from_total(total)

        # persist history
        dice_str = ",".join(map(str, dice))
        try:
            db_execute(
                "INSERT INTO history(chat_id, round_index, round_id, result, dice, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, round_index, round_id, result, dice_str, now_iso())
            )
        except Exception:
            logger.exception("Failed to insert history")

        # ------- Tính winners/losers -------
winners = []
losers = []
total_winner_bets = 0.0
total_loser_bets = 0.0
for b in bets:
    amt_f = float(b.get("amount") or 0.0)
    if b.get("side") == result:
        winners.append((int(b["user_id"]), amt_f))
        total_winner_bets += amt_f
    else:
        losers.append((int(b["user_id"]), amt_f))
        total_loser_bets += amt_f

# Losers -> pot
try:
    if total_loser_bets > 0:
        db_execute("UPDATE pot SET amount = amount + ? WHERE id = 1", (total_loser_bets,))
except Exception:
    logger.exception("Failed to add losers to pot")

# -------- TRẢ THƯỞNG --------
winners_paid = []
for uid, amt in winners:
    try:
        house_share = amt * HOUSE_RATE
        payout = amt * WIN_MULTIPLIER

        # cộng house share vào pot
        if house_share > 0:
            try:
                db_execute("UPDATE pot SET amount = amount + ? WHERE id = 1", (house_share,))
            except Exception:
                logger.exception("Failed to add house share to pot")

        # đảm bảo user tồn tại
        ensure_user(uid, "", "")

        # cộng tiền thắng cho user
        db_execute(
            """
            UPDATE users SET
                balance = COALESCE(balance, 0) + ?,
                current_streak = COALESCE(current_streak, 0) + 1,
                best_streak = CASE
                    THEN COALESCE(current_streak, 0) + 1 > COALESCE(best_streak, 0)
                    THEN COALESCE(current_streak, 0) + 1
                    ELSE COALESCE(best_streak, 0)
                END
            WHERE user_id = ?
            """,
            (payout, uid)
        )

        winners_paid.append((uid, payout, amt))
    except Exception:
        logger.exception(f"Error paying winner {uid}")

        # Special triple distributions (3x1 or 3x6) -> share pot proportionally
        special_msg = ""
        if special in ("triple1", "triple6"):
            try:
                pot_amount = get_pot_amount()
                if pot_amount > 0 and winners:
                    total_bets_win = sum([amt for (_, amt) in winners]) or 0.0
                    if total_bets_win > 0:
                        for uid, amt in winners:
                            share = (amt / total_bets_win) * pot_amount
                            ensure_user(uid, "", "")
                            u = get_user(uid) or {"balance": 0}
                            db_execute("UPDATE users SET balance=? WHERE user_id=?", ((u["balance"] or 0.0) + share, uid))
                        special_msg = f"Hũ {int(pot_amount):,}₫ đã được chia cho người thắng theo tỷ lệ cược!"
                        reset_pot()
            except Exception:
                logger.exception("Error distributing special pot")

        # Xóa bets chỉ của round này (không xóa tất cả)
        try:
            db_execute("DELETE FROM bets WHERE chat_id=? AND round_id=?", (chat_id, round_id))
        except Exception:
            logger.exception("Failed to delete bets for round")

        # Chuẩn bị và gửi tin nhắn kết quả
        display = "Tài" if result == "tai" else "Xỉu"
        symbol = BLACK if result == "tai" else WHITE
        history_line = format_history_line(chat_id)
        msg = f"▶️ Phiên {round_index} — Kết quả: {display} {symbol}\n"
        msg += f"Xúc xắc: {' '.join([DICE_CHARS[d-1] for d in dice])} — Tổng: {total}\n"
        if special_msg:
            msg += f"\n{special_msg}\n"
        if history_line:
            msg += f"\nLịch sử ({MAX_HISTORY} gần nhất):\n{history_line}\n"

        try:
            await app.bot.send_message(chat_id=chat_id, text=msg)
        except Exception:
            logger.exception("Cannot send round result to group")

        # Gửi báo cáo cho admin (nếu có người trúng)
        if winners_paid:
            admin_summary = f"Round {round_index} in group {chat_id} completed.\nResult: {result}\nWinners:\n"
            for uid, payout, amt in winners_paid:
                admin_summary += f"- {uid}: đặt {int(amt):,} -> nhận {int(payout):,}\n"
            for aid in ADMIN_IDS:
                try:
                    await app.bot.send_message(chat_id=aid, text=admin_summary)
                except Exception:
                    pass

        # Mở lại chat (nếu trước đó bị khoá)
        try:
            await unlock_group_chat(app.bot, chat_id)
        except Exception:
            pass

    except Exception as e:
        logger.exception("Exception in run_round_for_group")
        for aid in ADMIN_IDS:
            try:
                await app.bot.send_message(chat_id=aid, text=f"ERROR - run_round_for_group exception for group {chat_id}: {e}\n{traceback.format_exc()}")
            except Exception:
                pass
        
async def on_startup(app: Application):
    """Hàm chạy khi bot khởi động."""
    logger.info("Bot starting up...")
    init_db()

    # notify admins
    for aid in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=aid, text="✅ Bot đã khởi động và sẵn sàng.")
        except Exception as e:
            logger.warning(f"Không gửi được tin nhắn startup cho admin {aid}: {e}")

    # chạy vòng quay tài xỉu nền
    loop = asyncio.get_running_loop()
    loop.create_task(rounds_loop(app))


async def on_shutdown(app: Application):
    """Hàm chạy khi bot shutdown."""
    logger.info("Bot shutting down...")
    for aid in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=aid, text="⚠️ Bot đang tắt (shutdown).")
        except Exception as e:
            logger.warning(f"Không gửi được tin nhắn shutdown cho admin {aid}: {e}")

# ==============================
# Handler rút tiền (dán trước hàm main)
# ==============================
from telegram import Update
from telegram.ext import ContextTypes

async def ruttien_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý lệnh rút tiền từ người chơi."""
    try:
        args = context.args
        if len(args) < 3:
            await update.message.reply_text(
                "⚠️ Cú pháp không đúng!\nDùng: /ruttien <Ngân hàng> <Số TK> <Số tiền>"
            )
            return

        bank = args[0]
        account = args[1]
        try:
            amount = int(args[2])
        except ValueError:
            await update.message.reply_text("⚠️ Số tiền không hợp lệ.")
            return

        if amount < 100000:
            await update.message.reply_text("⚠️ Số tiền rút tối thiểu là 100.000đ.")
            return

        # (giả lập kiểm tra số dư)
        balance = 9999999  
        if amount > balance:
            await update.message.reply_text("⚠️ Số dư không đủ.")
            return

        await update.message.reply_text(
            f"✅ Đã nhận yêu cầu rút {amount:,}đ về {bank} ({account}).\nĐang xử lý..."
        )

    except Exception as e:
        await update.message.reply_text("❌ Lỗi hệ thống khi xử lý yêu cầu rút tiền.")
        print("ruttien_handler error:", e)

# ==============================
# Hàm main — để nguyên bên dưới
# ==============================
def main():
    """Main entrypoint — dùng run_polling() thay cho updater.start_polling()"""
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: BOT_TOKEN not set. Please set BOT_TOKEN env variable.")
        return

    # Khởi tạo database
    init_db()

    # Tạo app
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ----- Đăng ký HANDLERS -----
    # user
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("game", game_info))
    app.add_handler(CommandHandler("nap", nap_info))
    app.add_handler(CommandHandler("ruttien", ruttien_handler))
    app.add_handler(CallbackQueryHandler(withdraw_callback_handler, pattern=r"^withdraw_.*|^withdraw.*"))
    app.add_handler(CallbackQueryHandler(callback_query_handler, pattern=r"^game_.*"))

    # admin
    app.add_handler(CommandHandler("addmoney", addmoney_handler))
    app.add_handler(CommandHandler("top10", top10_handler))
    app.add_handler(CommandHandler("balances", balances_handler))
    app.add_handler(CommandHandler("KqTai", admin_force_handler))
    app.add_handler(CommandHandler("KqXiu", admin_force_handler))
    app.add_handler(CommandHandler("bettai", admin_force_handler))
    app.add_handler(CommandHandler("betxiu", admin_force_handler))
    app.add_handler(CommandHandler("tatbet", admin_force_handler))
    app.add_handler(CommandHandler("code", admin_create_code_handler))
    app.add_handler(CommandHandler("nhancode", redeem_code_handler))

    # group control
    app.add_handler(CommandHandler("batdau", batdau_handler))
    app.add_handler(CallbackQueryHandler(approve_callback_handler, pattern=r"^(approve|deny)\|"))

    # bets & private menu
    app.add_handler(MessageHandler(filters.Regex(r"^/[TtXx]\d+"), bet_message_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_handler))

    # lifecycle hooks
    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    # ----- CHẠY BOT -----
    try:
        logger.info("🚀 Bot starting... using run_polling()")
        app.run_polling(poll_interval=1.0, timeout=20)
    except Exception as e:
        logger.exception(f"❌ Fatal error in main(): {e}")
        # Notify admins nếu bot crash
        for aid in ADMIN_IDS:
            try:
                app.bot.send_message(chat_id=aid, text=f"❌ Bot crashed: {e}")
            except Exception:
                pass


# -----------------------
# Helper command wrappers
# -----------------------

async def game_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎲 *Game: Tài Xỉu (xúc xắc 3 con)*\n"
        "- Tài: tổng 11–17\n"
        "- Xỉu: tổng 4–10\n"
        "- Mỗi phiên 60s\n"
        "- Đặt cược bằng: /T<tiền> hoặc /X<tiền>\n"
        "👉 Tham gia nhóm chơi: @VET789cc",
        parse_mode="Markdown",
    )


async def nap_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Để nạp tiền, liên hệ: @HOANGDUNGG789")


# -----------------------
# Run as script
# -----------------------
if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Fatal error in main()")
