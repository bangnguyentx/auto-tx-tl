# bot.py
# Telegram Tài Xỉu Bot - hoàn chỉnh (60s/phiên, 3 xúc xắc lần lượt, hũ, admin controls)
# WARNING: Token được chèn trực tiếp theo yêu cầu. Nếu repo public: RISK.

import os
import sys
import sqlite3
import random
import math
import traceback
import logging
import threading
import http.server
import socketserver
import asyncio
from datetime import datetime
from typing import List, Tuple, Optional, Dict, Any

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, Application
)

# -----------------------
# KEEP A PORT OPEN (for Render if using Web Service)
# -----------------------
def keep_port_open():
    PORT = int(os.environ.get("PORT_KEEP", 10000))
    Handler = http.server.SimpleHTTPRequestHandler
    try:
        with socketserver.TCPServer(("", PORT), Handler) as httpd:
            httpd.serve_forever()
    except Exception:
        # if fails, ignore (likely port in use)
        pass

threading.Thread(target=keep_port_open, daemon=True).start()

# -----------------------
# CONFIGURATION
# -----------------------

# NOTE: As requested, token is pasted directly here.
BOT_TOKEN = "7969189609:AAFG1-vmQEC_4nfgieG1fhUdWTWA8AsJt1I"

# Admin IDs
ADMIN_IDS = [7760459637, 6942793864]

# Constants
ROUND_SECONDS = 60  # seconds per round
MIN_BET = 1000
INITIAL_FREE = 10_000
WIN_MULTIPLIER = 1.97
HOUSE_RATE = 0.03  # 3% of winners goes to pot
DB_FILE = "tx_bot_data.db"
MAX_HISTORY = 20  # max rounds to show in history

# logging
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------
# DATABASE
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
        received_bonus INTEGER DEFAULT 0,  -- 1 if got initial 10k
        restricted_onek INTEGER DEFAULT 0  -- 1 if restricted to bet 1k only
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
        side TEXT,
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
# UTILITIES
# -----------------------

def now_iso():
    return datetime.utcnow().isoformat()

def ensure_user(user_id: int, username: str = "", first_name: str = ""):
    rows = db_query("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if not rows:
        db_execute(
            "INSERT INTO users(user_id, username, first_name, balance, total_deposited, total_bet_volume, current_streak, best_streak, created_at, received_bonus, restricted_onek) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
# DICE / RESULT HELPERS
# -----------------------

# unicode dice chars U+2680..U+2685 => ⚀⚁⚂⚃⚄⚅
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

# -----------------------
# TELEGRAM BOT HANDLERS
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
    # If never received bonus, give initial free and restrict to 1k bets
    if u and u.get("received_bonus", 0) == 0:
        add_balance(user.id, INITIAL_FREE)
        db_execute("UPDATE users SET total_deposited=?, received_bonus=1, restricted_onek=1 WHERE user_id=?", (INITIAL_FREE, user.id))
        greeted = True

    text = f"Xin chào {user.first_name or 'bạn'}! 👋\n\n"
    text += "Chào mừng đến với bot Tài Xỉu tự động.\n"
    if greeted:
        text += f"Bạn đã được tặng {INITIAL_FREE:,}₫ miễn phí (một lần). Lưu ý: trong chế độ tặng, bạn chỉ được cược tối đa 1.000₫ mỗi lần. Nếu muốn chơi thoải mái, hãy liên hệ admin để cộng tiền.\n\n"
    text += "Menu:\n"
    text += "- Game: thông tin & link nhóm\n"
    text += "- Nạp tiền: hướng dẫn nạp\n"
    text += "- Rút tiền: /ruttien <Ngân hàng> <Số TK> <Số tiền>\n"
    text += "- Đặt cược trong nhóm: /T<amount> hoặc /X<amount>\n"
    text += "\nBạn có thể dùng phím menu hoặc lệnh trực tiếp."
    await update.message.reply_text(text, reply_markup=MAIN_MENU)

async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text == "game":
        return await game_info(update, context)
    if text in ("nạp tiền", "nap tien", "nạp"):
        return await nap_info(update, context)
    if text in ("rút tiền", "rut tien", "ruttien"):
        return await ruttien_help(update, context)
    if text in ("số dư", "so du"):
        u = get_user(update.effective_user.id)
        bal = int(u["balance"]) if u else 0
        await update.message.reply_text(f"Số dư hiện tại: {bal:,}₫")

async def game_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Game: Tài Xỉu (xúc xắc 3 con)\n\n"
    text += "Luật chính:\n- Tài: tổng 11-17 (Đen)\n- Xỉu: tổng 4-10 (Trắng)\n"
    text += f"- Phiên chạy mỗi {ROUND_SECONDS} giây khi nhóm được admin duyệt & bật /batdau.\n"
    text += f"- Thắng nhận x{WIN_MULTIPLIER} (house giữ {int(HOUSE_RATE*100)}% mỗi khoản thắng vào hũ).\n"
    text += "- Nếu ra 3 con 1 hoặc 3 con 6 → hũ được chia cho những người thắng phiên đó theo tỉ lệ cược.\n\n"
    text += "Link nhóm: @VET789cc\n"
    text += "Giới thiệu: Đặt cược bằng lệnh /T<amount> hoặc /X<amount> trong nhóm khi bot đang chạy."
    await update.message.reply_text(text)

async def nap_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Để nạp tiền, liên hệ: @HOANGDUNGG789")

async def ruttien_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Để rút tiền hãy nhập lệnh:\n"
        "/ruttien <Ngân hàng> <Số tài khoản> <Số tiền>\n\n"
        "Rút tối thiểu 100000 vnđ.\n"
        "Bạn phải cược tối thiểu 0.9 vòng cược (0.9x tổng đã nạp).\n"
    )
    await update.message.reply_text(text)

async def ruttien_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "", user.first_name or "")
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Sai cú pháp. Ví dụ: /ruttien Vietcombank 0123456789 100000")
        return
    bank = args[0]
    account = args[1]
    try:
        amount = int(args[2])
    except:
        await update.message.reply_text("Số tiền không hợp lệ.")
        return
    if amount < 100000:
        await update.message.reply_text("Rút tối thiểu 100000 vnđ.")
        return
    u = get_user(user.id)
    if not u:
        await update.message.reply_text("Không tìm thấy tài khoản.")
        return
    total_deposited = u["total_deposited"] or 0.0
    total_bet_volume = u["total_bet_volume"] or 0.0
    required = 0.9 * total_deposited
    if total_deposited > 0 and total_bet_volume < required:
        await update.message.reply_text(f"Bạn chưa cược đủ. Cần cược tối thiểu {required:,.0f} (đã cược {total_bet_volume:,.0f}).")
        return
    if amount > u["balance"]:
        await update.message.reply_text("Số dư không đủ.")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Thành công", callback_data=f"withdraw_ok|{user.id}|{amount}|{bank}|{account}"),
         InlineKeyboardButton("Từ chối", callback_data=f"withdraw_no|{user.id}|{amount}|{bank}|{account}")]
    ])
    await update.message.reply_text("Vui lòng chờ, nếu sau 1 tiếng chưa thấy thông báo Thành công/Từ chối thì nhắn admin nhé!")
    text = f"YÊU CẦU RÚT TIỀN\nUser: @{user.username or user.first_name} (id: {user.id})\nBank: {bank}\nAccount: {account}\nAmount: {amount:,}₫"
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=text, reply_markup=kb)
        except Exception:
            logger.exception(f"Cannot notify admin {aid} for withdraw request")

async def withdraw_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("|")
    action = parts[0]
    try:
        user_id = int(parts[1])
        amount = int(parts[2])
        bank = parts[3]
        account = parts[4]
    except Exception:
        await query.edit_message_text("Dữ liệu không hợp lệ.")
        return
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("Chỉ admin mới thao tác.")
        return
    if action == "withdraw_ok":
        u = get_user(user_id)
        if not u:
            await query.edit_message_text("User không tồn tại.")
            return
        if u["balance"] < amount:
            await query.edit_message_text("User không đủ tiền.")
            try:
                await context.bot.send_message(chat_id=user_id, text=f"Yêu cầu rút {amount:,}₫ bị từ chối: số dư không đủ.")
            except:
                pass
            return
        new_bal = u["balance"] - amount
        db_execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, user_id))
        await query.edit_message_text(f"Đã xác nhận rút {amount:,}₫ cho user {user_id}.")
        try:
            await context.bot.send_message(chat_id=user_id, text=f"Yêu cầu rút {amount:,}₫ đã được duyệt bởi admin. Vui lòng chờ chuyển khoản.")
        except:
            pass
    else:
        await query.edit_message_text(f"Yêu cầu rút {amount:,}₫ đã bị từ chối bởi admin {query.from_user.id}.")
        try:
            await context.bot.send_message(chat_id=user_id, text=f"Yêu cầu rút {amount:,}₫ đã bị từ chối bởi admin. Vui lòng liên hệ.")
        except:
            pass

# -----------------------
# BET HANDLING
# -----------------------

async def bet_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or msg.text is None:
        return
    text = msg.text.strip()
    user = update.effective_user
    chat = update.effective_chat
    if not text.startswith("/"):
        return
    cmd = text[1:]
    if len(cmd) < 2:
        return
    prefix = cmd[0].lower()
    if prefix not in ('t', 'x'):
        return
    side = 'tai' if prefix == 't' else 'xiu'
    try:
        amount = int(cmd[1:])
    except:
        await msg.reply_text("Cú pháp đặt cược sai. Ví dụ: /T1000 hoặc /X5000")
        return
    if amount < MIN_BET:
        await msg.reply_text(f"Đặt cược tối thiểu {MIN_BET:,}₫")
        return

    # group check
    if chat.type in ("group", "supergroup"):
        g = db_query("SELECT approved, running FROM groups WHERE chat_id=?", (chat.id,))
        if not g or g[0]["approved"] != 1 or g[0]["running"] != 1:
            await msg.reply_text("Nhóm này chưa được admin duyệt hoặc chưa bật /batdau.")
            return

    ensure_user(user.id, user.username or "", user.first_name or "")
    u = get_user(user.id)

    # restricted check: if restricted_onek ==1 then amount must equal 1000 (or <=1000)
    if u and u.get("restricted_onek", 0) == 1:
        if amount > 1000:
            await msg.reply_text("Bạn đang ở chế độ tặng thưởng (10k) và chỉ được cược tối đa 1.000₫. Liên hệ admin để mở giới hạn.")
            return

    if (u["balance"] or 0.0) < amount:
        await msg.reply_text("Số dư không đủ.")
        return

    # deduct immediately and update total_bet_volume
    new_balance = (u["balance"] or 0.0) - amount
    new_total_bet = (u["total_bet_volume"] or 0.0) + amount
    db_execute("UPDATE users SET balance=?, total_bet_volume=? WHERE user_id=?", (new_balance, new_total_bet, user.id))

    # round_id = chatid_epoch
    now_ts = int(datetime.utcnow().timestamp())
    round_epoch = now_ts // ROUND_SECONDS
    round_id = f"{chat.id}_{round_epoch}"
    db_execute("INSERT INTO bets(chat_id, round_id, user_id, side, amount, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
               (chat.id, round_id, user.id, side, amount, now_iso()))

    await msg.reply_text(f"Đã đặt {side.upper()} {amount:,}₫ cho phiên hiện tại. Số dư còn {int(new_balance):,}₫")

# -----------------------
# ADMIN HANDLERS
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
        uid = int(args[0])
        amt = float(args[1])
    except:
        await update.message.reply_text("Tham số không hợp lệ.")
        return
    ensure_user(uid, "", "")
    u = get_user(uid)
    new_bal = (u["balance"] or 0.0) + amt
    new_deposited = (u["total_deposited"] or 0.0) + amt
    # Update DB and remove restriction if present
    db_execute("UPDATE users SET balance=?, total_deposited=?, restricted_onek=0 WHERE user_id=?", (new_bal, new_deposited, uid))
    await update.message.reply_text(f"Đã cộng {int(amt):,}₫ cho user {uid}. Số dư hiện: {int(new_bal):,}₫")
    try:
        await context.bot.send_message(chat_id=uid, text=f"Bạn vừa được admin cộng {int(amt):,}₫. Số dư: {int(new_bal):,}₫")
    except Exception:
        pass

async def top10_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Chỉ admin.")
        return
    rows = db_query("SELECT user_id, best_streak FROM users ORDER BY best_streak DESC LIMIT 10")
    text = "Top 10 người có chuỗi thắng dài nhất:\n"
    for i, r in enumerate(rows, start=1):
        text += f"{i}. {r['user_id']} — {r['best_streak']} thắng liên tiếp\n"
    await update.message.reply_text(text)

async def balances_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Chỉ admin.")
        return
    rows = db_query("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 50")
    text = "Top balances:\n"
    for r in rows:
        text += f"- {r['user_id']}: {int(r['balance']):,}\n"
    await update.message.reply_text(text)

# admin force handlers: /KqTai /KqXiu /bettai /betxiu /tatbet
async def admin_force_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Chỉ admin.")
        return
    text = update.message.text.strip()
    cmd = text.split()[0].lower()
    args = context.args
    if not args:
        await update.message.reply_text("Cú pháp: /KqTai <chat_id> hoặc /bettai <chat_id> ...")
        return
    try:
        chat_id = int(args[0])
    except:
        await update.message.reply_text("chat_id không hợp lệ.")
        return
    if cmd == "/kqtai":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("force_tai", chat_id))
        await update.message.reply_text(f"Đã đặt force TÀI cho nhóm {chat_id}. (Không thông báo vào nhóm)")
    elif cmd == "/kqxiu":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("force_xiu", chat_id))
        await update.message.reply_text(f"Đã đặt force XỈU cho nhóm {chat_id}. (Không thông báo vào nhóm)")
    elif cmd == "/bettai":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("bettai", chat_id))
        await update.message.reply_text(f"Đã bật cầu bệt TÀI cho nhóm {chat_id}.")
    elif cmd == "/betxiu":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("betxiu", chat_id))
        await update.message.reply_text(f"Đã bật cầu bệt XỈU cho nhóm {chat_id}.")
    elif cmd == "/tatbet":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("random", chat_id))
        await update.message.reply_text(f"Đã tắt cầu bệt và trả về random cho nhóm {chat_id}.")
    else:
        await update.message.reply_text("Lệnh admin không hợp lệ.")

# -----------------------
# GROUP / BATDAU / APPROVAL
# -----------------------

async def batdau_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("/batdau chỉ dùng trong nhóm.")
        return
    title = chat.title or ""
    rows = db_query("SELECT chat_id FROM groups WHERE chat_id=?", (chat.id,))
    if not rows:
        db_execute("INSERT INTO groups(chat_id, title, approved, running, bet_mode, last_round) VALUES (?, ?, 0, 0, 'random', ?)",
                   (chat.id, title, 0))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Duyệt", callback_data=f"approve|{chat.id}"),
         InlineKeyboardButton("Từ chối", callback_data=f"deny|{chat.id}")]
    ])
    text = f"Yêu cầu bật bot cho nhóm:\n{title}\nchat_id: {chat.id}\nNgười yêu cầu: {update.effective_user.id}"
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=text, reply_markup=kb)
        except Exception:
            logger.exception(f"Không gửi được yêu cầu duyệt nhóm tới admin {aid}")
    await update.message.reply_text("Đã gửi yêu cầu tới admin để duyệt.")

async def approve_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("|")
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
        except Exception:
            pass
    else:
        db_execute("UPDATE groups SET approved=0, running=0 WHERE chat_id=?", (chat_id,))
        await query.edit_message_text(f"Đã từ chối cho nhóm {chat_id}.")

def get_active_groups() -> List[Dict[str, Any]]:
    rows = db_query("SELECT chat_id, bet_mode, last_round FROM groups WHERE approved=1 AND running=1")
    return [dict(r) for r in rows]

# ===================== NGƯỜI DÙNG NHẬP CODE =====================
user_bonus_history = {}  # {user_id: set(code)}

async def redeem_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        code = context.args[0].upper()
    except IndexError:
        await update.message.reply_text("⚠️ Cú pháp: /nhancode <CODE>")
        return

    if code not in promo_codes:
        await update.message.reply_text("❌ Code không tồn tại.")
        return

    if promo_codes[code]["used"]:
        await update.message.reply_text("❌ Code này đã được sử dụng.")
        return

    # Kiểm tra nếu user đã nhập code này trước đó
    if user_id in user_bonus_history and code in user_bonus_history[user_id]:
        await update.message.reply_text("⚠️ Bạn đã nhập code này rồi.")
        return

    # Cộng tiền vào tài khoản user
    amount = promo_codes[code]["amount"]
    wager_required = promo_codes[code]["wager_required"]
    update_user_balance(user_id, amount)  # hàm bạn đã có sẵn để cộng tiền

    promo_codes[code]["used"] = True
    user_bonus_history.setdefault(user_id, set()).add(code)

    await update.message.reply_text(
        f"🎁 Bạn đã nhận {amount:,}đ thành công!\n"
        f"🔄 Vòng cược yêu cầu: {wager_required} vòng.\nChúc bạn may mắn 🍀"
    )

# -----------------------
# ROUND ENGINE
# -----------------------

async def rounds_loop(app: Application):
    logger.info("Rounds loop starting...")
    # wait a little for startup
    await asyncio.sleep(2)
    while True:
        try:
            groups = get_active_groups()
            if groups:
                logger.debug(f"Active groups: {[g['chat_id'] for g in groups]}")
            tasks = []
            for g in groups:
                chat_id = g['chat_id']
                tasks.append(asyncio.create_task(run_round_for_group(app, chat_id)))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.exception("Exception in rounds_loop")
            for aid in ADMIN_IDS:
                try:
                    await app.bot.send_message(chat_id=aid, text=f"ERROR - rounds_loop exception:\n{e}")
                except Exception:
                    pass
        await asyncio.sleep(ROUND_SECONDS)

# Helper: format history row up to MAX_HISTORY
def format_history_line(chat_id: int) -> str:
    rows = db_query("SELECT result FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, MAX_HISTORY))
    # rows are recent first; we want left-to-right oldest -> newest, so reverse
    results = [r["result"] for r in reversed(rows)]
    mapped = []
    for r in results:
        if r == "tai":
            mapped.append(BLACK)
        elif r == "xiu":
            mapped.append(WHITE)
    return " ".join(mapped)

# main per-group round runner
async def run_round_for_group(app: Application, chat_id: int):
    """
    For each active group, do:
    - identify current round_id (epoch)
    - gather bets placed for this round
    - determine bet_mode and possibly force result
    - send 3 dice one-by-one (1s apart), show emoji for each
    - compute payouts, update balances & pot
    - persist history and send summary + history line
    """
    try:
        now_ts = int(datetime.utcnow().timestamp())
        round_epoch = now_ts // ROUND_SECONDS
        round_index = round_epoch
        round_id = f"{chat_id}_{round_epoch}"

        # fetch bets for this round
        bets_rows = db_query("SELECT user_id, side, amount FROM bets WHERE chat_id=? AND round_id=?", (chat_id, round_id))
        bets = [dict(r) for r in bets_rows]

        # get group bet_mode
        grows = db_query("SELECT bet_mode FROM groups WHERE chat_id=?", (chat_id,))
        bet_mode = grows[0]["bet_mode"] if grows else "random"

        # decide forced/bettai/betxiu
        forced_value = None
        if bet_mode == "force_tai":
            forced_value = "tai"
            # revert after applying once
            db_execute("UPDATE groups SET bet_mode='random' WHERE chat_id=?", (chat_id,))
        elif bet_mode == "force_xiu":
            forced_value = "xiu"
            db_execute("UPDATE groups SET bet_mode='random' WHERE chat_id=?", (chat_id,))
        elif bet_mode == "bettai":
            forced_value = "tai"
        elif bet_mode == "betxiu":
            forced_value = "xiu"

        # send initial rolling message
        try:
            await app.bot.send_message(chat_id=chat_id, text=f"🎲 Phiên {round_index} — Đang tung xúc xắc...")
        except Exception:
            pass

        # roll dice one-by-one with small delay
        dice = []
        special = None
        if forced_value:
            # generate until meet forced_value (bounded attempts)
            attempts = 0
            dice, total, special = roll_three_dice_random()
            while result_from_total(total) != forced_value and attempts < 50:
                dice, total, special = roll_three_dice_random()
                attempts += 1
        else:
            # normal roll: generate sequentially
            a = roll_one_die()
            dice.append(a)
            try:
                await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[a-1]}")  # send first die
            except Exception:
                pass
            await asyncio.sleep(1)

            b = roll_one_die()
            dice.append(b)
            try:
                await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[b-1]}")
            except Exception:
                pass
            await asyncio.sleep(1)

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

        # If forced_value case: we didn't send step-by-step above; send step-by-step for forced as well
        if forced_value:
            # send each die individually with 1s gap
            # reconstruct dice variable already set
            for val in dice:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=f"{DICE_CHARS[val-1]}")
                except Exception:
                    pass
                await asyncio.sleep(1)
            total = sum(dice)

        result = result_from_total(total)

        # persist history
        dice_str = ",".join(map(str, dice))
        db_execute("INSERT INTO history(chat_id, round_index, round_id, result, dice, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                   (chat_id, round_index, round_id, result, dice_str, now_iso()))

        # compute winners/losers
        winners = []
        losers = []
        total_winner_bets = 0.0
        total_loser_bets = 0.0
        for b in bets:
            if b["side"] == result:
                winners.append((b["user_id"], b["amount"]))
                total_winner_bets += b["amount"]
            else:
                losers.append((b["user_id"], b["amount"]))
                total_loser_bets += b["amount"]

        # losers go to pot
        if total_loser_bets > 0:
            add_to_pot(total_loser_bets)

        winners_paid = []
        for uid, amt in winners:
            # house share to pot (3% of amt)
            house_share = amt * HOUSE_RATE
            add_to_pot(house_share)
            payout = amt * WIN_MULTIPLIER
            ensure_user(uid, "", "")
            u = get_user(uid)
            new_balance = (u["balance"] or 0.0) + payout
            cur_streak = (u["current_streak"] or 0) + 1
            best_streak = max(u["best_streak"] or 0, cur_streak)
            db_execute("UPDATE users SET balance=?, current_streak=?, best_streak=? WHERE user_id=?", (new_balance, cur_streak, best_streak, uid))
            winners_paid.append((uid, payout, amt))

        for uid, amt in losers:
            rows = db_query("SELECT current_streak FROM users WHERE user_id=?", (uid,))
            if rows:
                db_execute("UPDATE users SET current_streak=0 WHERE user_id=?", (uid,))

        # special triple -> distribute entire pot to winners proportionally
        special_msg = ""
        if special in ("triple1", "triple6"):
            pot_amount = get_pot_amount()
            if pot_amount > 0 and winners:
                total_bets_win = sum([amt for (_, amt) in winners])
                if total_bets_win > 0:
                    distributed = []
                    for uid, amt in winners:
                        share = (amt / total_bets_win) * pot_amount
                        ensure_user(uid, "", "")
                        u = get_user(uid)
                        db_execute("UPDATE users SET balance=? WHERE user_id=?", ((u["balance"] or 0.0) + share, uid))
                        distributed.append((uid, share))
                    special_msg = f"Hũ {int(pot_amount):,}₫ đã được chia cho người thắng theo tỷ lệ cược!"
                    reset_pot()

        # clear bets for this round
        db_execute("DELETE FROM bets WHERE chat_id=? AND round_id=?", (chat_id, round_id))

        # prepare display message
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

        # send admin summary optionally
        if winners_paid:
            admin_summary = f"Round {round_index} in group {chat_id} completed.\nResult: {result}\nWinners:\n"
            for uid, payout, amt in winners_paid:
                admin_summary += f"- {uid}: đặt {int(amt):,} -> nhận {int(payout):,}\n"
            for aid in ADMIN_IDS:
                try:
                    await app.bot.send_message(chat_id=aid, text=admin_summary)
                except Exception:
                    pass

    except Exception as e:
        logger.exception("Exception in run_round_for_group")
        for aid in ADMIN_IDS:
            try:
                await app.bot.send_message(chat_id=aid, text=f"ERROR - run_round_for_group exception for group {chat_id}: {e}\n{traceback.format_exc()}")
            except Exception:
                pass

# -----------------------
# STARTUP / SHUTDOWN / EXCEPTIONS
# -----------------------

async def on_startup(app: Application):
    logger.info("Bot starting up...")
    init_db()
    # small delay so loop ready
    await asyncio.sleep(1)
    # schedule rounds loop
    loop = asyncio.get_running_loop()
    loop.create_task(rounds_loop(app))
    # notify admins
    for aid in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=aid, text="✅ Bot đã khởi động và sẵn sàng.")
        except Exception:
            pass

async def on_shutdown(app: Application):
    logger.info("Bot shutting down...")
    for aid in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=aid, text="⚠️ Bot đang tắt (shutdown).")
        except Exception:
            pass

def handle_loop_exception(loop, context):
    msg = context.get("exception", context.get("message"))
    logger.error(f"Caught exception in event loop: {msg}")

# -----------------------
# MAIN
# -----------------------

def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        print("ERROR: BOT_TOKEN not set.")
        return

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("game", game_info))
    app.add_handler(CommandHandler("nap", nap_info))
    app.add_handler(CommandHandler("ruttien", ruttien_handler))
    app.add_handler(CallbackQueryHandler(withdraw_callback_handler, pattern=r"^withdraw_.*|^withdraw.*"))

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
    
    app.add_handler(CommandHandler("batdau", batdau_handler))
    app.add_handler(CallbackQueryHandler(approve_callback_handler, pattern=r"^(approve|deny)\|"))

    app.add_handler(MessageHandler(filters.Regex(r"^/[TtXx]\d+"), bet_message_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_handler))

    # lifecycle hooks
    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    # event loop exception
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(handle_loop_exception)

    try:
        logger.info("Running bot (polling)...")
        app.run_polling(poll_interval=1.0)
    except Exception as e:
        logger.exception(f"Fatal error running the bot: {e}")
        for aid in ADMIN_IDS:
            try:
                app.bot.send_message(chat_id=aid, text=f"Bot crashed on startup: {e}")
            except Exception:
                pass

if __name__ == "__main__":
    main() 
