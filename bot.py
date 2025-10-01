# bot.py
# Telegram Tài Xỉu Bot — Hoàn chỉnh, nhiều chú giải, phù hợp deploy Render (worker)
# - Phiên: 60 giây
# - Lưu: SQLite
# - Admins: có quyền duyệt nhóm, ép kết quả, bật/tắt cầu bệt, add tiền, xử lý rút tiền
# - Người chơi: /start (tặng 10k), /T1000 /X500, /ruttien ...
# -------------------------------------------------------------
# IMPORTANT: Replace BOT_TOKEN below with your bot token before running.
# DO NOT upload your token to public repos. Use environment variables in production.
# -------------------------------------------------------------

import asyncio
import logging
import threading
import http.server
import socketserver

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# 📌 FAKE PORT ĐỂ RENDER KHÔNG KILL
def keep_port_open():
    PORT = 10000
    Handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        httpd.serve_forever()

threading.Thread(target=keep_port_open, daemon=True).start()   # Token thật của bạn

# Các hàm như init_db(), on_startup(), handler... nằm dưới đây
import os
import sys
import sqlite3
import random
import math
import traceback
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Dict, Any

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton, Chat, ChatPermissions
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, Application, PicklePersistence
)

# -----------------------
# CONFIGURATION
# -----------------------

# *** SECURITY: Put your token here BEFORE running, or better use environment variable.
# Replace the string below with your actual token, or set BOT_TOKEN env variable and leave this placeholder.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

# Admin IDs (list) - you can update these IDs. These are the accounts that receive approval requests, crash alerts, and can use admin commands.
ADMIN_IDS = [7760459637, 6942793864]  # <-- adjust if needed

# Round timing (seconds)
ROUND_SECONDS = 60  # user requested 60s per round

# Minimal bet
MIN_BET = 1000  # 1,000₫ minimal bet as you requested

# Initial free credit for new users on /start
INITIAL_FREE = 10_000

# Winning payout multiplier and house share
WIN_MULTIPLIER = 1.97
HOUSE_RATE = 0.03  # 3% of winning goes to pot

# DB filename
DB_FILE = "tx_bot_data.db"

# Logging config
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Safety: prevent running with placeholder token
if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
    logger.warning("BOT_TOKEN placeholder detected. Replace it with your actual token or set BOT_TOKEN env var before running.")
    # Not exiting — allow user to edit file locally. If running on server, will likely fail auth.

# -----------------------
# DATABASE SETUP
# -----------------------

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # users: track balance, streaks, totals
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
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS groups (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        approved INTEGER DEFAULT 0,
        running INTEGER DEFAULT 0,
        bet_mode TEXT DEFAULT 'random', -- 'random', 'bettai', 'betxiu', 'force_tai', 'force_xiu'
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
    # ensure pot row exists
    cur.execute("INSERT OR IGNORE INTO pot(id, amount) VALUES (1, 0)")
    conn.commit()
    conn.close()

# Helper DB functions
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
            "INSERT INTO users(user_id, username, first_name, balance, total_deposited, total_bet_volume, current_streak, best_streak, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username or "", first_name or "", 0.0, 0.0, 0.0, 0, 0, now_iso())
        )

def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    rows = db_query("SELECT * FROM users WHERE user_id=?", (user_id,))
    return dict(rows[0]) if rows else None

def add_balance(user_id: int, amount: float):
    u = get_user(user_id)
    if not u:
        ensure_user(user_id, "", "")
        u = get_user(user_id)
    new_bal = (u["balance"] or 0.0) + amount
    db_execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, user_id))

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

def send_admins(app: Application, text: str, reply_markup=None):
    for aid in ADMIN_IDS:
        try:
            app.bot.send_message(chat_id=aid, text=text, reply_markup=reply_markup)
        except Exception as e:
            logger.exception(f"Failed to notify admin {aid}: {e}")

# -----------------------
# DICE LOGIC
# -----------------------

def roll_three_dice() -> Tuple[List[int], int, Optional[str]]:
    # Returns (dice_list, total, special_flag)
    # special_flag: 'triple1' or 'triple6' or None
    a = random.randint(1, 6)
    b = random.randint(1, 6)
    c = random.randint(1, 6)
    dice = [a, b, c]
    total = sum(dice)
    if dice.count(1) == 3:
        special = 'triple1'
    elif dice.count(6) == 3:
        special = 'triple6'
    else:
        special = None
    return dice, total, special

def result_from_total(total: int) -> str:
    # Tài: 11-17 ; Xỉu: 4-10
    if 11 <= total <= 17:
        return 'tai'
    elif 4 <= total <= 10:
        return 'xiu'
    else:
        return 'invalid'

# -----------------------
# TELEGRAM HANDLERS
# -----------------------

# Keyboard menu for private chats
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
    if u and (u["total_deposited"] == 0):
        # Give initial free credit and mark as deposited to avoid re-gifting
        add_balance(user.id, INITIAL_FREE)
        db_execute("UPDATE users SET total_deposited=? WHERE user_id=?", (INITIAL_FREE, user.id))
        greeted = True

    # Friendly welcome message and menu
    text = f"Xin chào {user.first_name or 'bạn'}! 👋\n\n"
    text += "Chào mừng đến với bot Tài Xỉu tự động.\n"
    if greeted:
        text += f"Bạn đã được tặng {INITIAL_FREE:,}₫ miễn phí. Chúc chơi vui!\n\n"
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
    if text == "nạp tiền" or text == "nap tien" or text == "nạp":
        return await nap_info(update, context)
    if text == "rút tiền" or text == "rut tien":
        return await ruttien_help(update, context)
    if text == "số dư" or text == "so du":
        u = get_user(update.effective_user.id)
        bal = int(u["balance"]) if u else 0
        await update.message.reply_text(f"Số dư hiện tại: {bal:,}₫")
    # else ignore; catch other text elsewhere

async def game_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Game: Tài Xỉu (xúc xắc 3 con)\n\n"
    text += "Luật chính:\n"
    text += "- Tài: tổng 11-17 (Đen)\n- Xỉu: tổng 4-10 (Trắng)\n"
    text += f"- Phiên chạy mỗi {ROUND_SECONDS} giây khi nhóm được admin duyệt & bật /batdau.\n"
    text += f"- Thắng nhận x{WIN_MULTIPLIER} (house giữ {int(HOUSE_RATE*100)}% mỗi khoản thắng vào hũ).\n"
    text += "- Nếu ra 3 con 1 hoặc 3 con 6 → hũ được chia cho những người thắng phiên đó theo tỉ lệ cược.\n\n"
    text += "Link nhóm: @VET789cc\n"
    text += "Giới thiệu: Đặt cược bằng lệnh /T<amount> hoặc /X<amount> trong nhóm khi bot đang chạy."
    await update.message.reply_text(text)

async def nap_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Để nạp tiền, liên hệ: @HOANGDUNGG789\nAdmin sẽ kiểm tra và cộng tiền thủ công hoặc bạn có thể dùng hệ thống nạp (nếu có).")

async def ruttien_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Để rút tiền hãy nhập lệnh:\n"
        "/ruttien <Ngân hàng> <Số tài khoản> <Số tiền>\n\n"
        "Rút tối thiểu 100000 vnđ.\n"
        "Bạn phải cược tối thiểu 0.9 vòng cược (0.9x tổng đã nạp).\n"
    )
    await update.message.reply_text(text)

# Rút tiền command handler
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
    # check betting requirement
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

    # Send request to admin with approve/deny inline buttons
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Thành công", callback_data=f"withdraw_ok|{user.id}|{amount}|{bank}|{account}"),
         InlineKeyboardButton("Từ chối", callback_data=f"withdraw_no|{user.id}|{amount}|{bank}|{account}")]
    ])
    # Inform user
    await update.message.reply_text("Vui lòng chờ, nếu sau 1 tiếng chưa thấy thông báo Thành công/Từ chối thì nhắn admin nhé!")
    # Notify all admins
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
    # pattern: withdraw_ok|user_id|amount|bank|account  OR withdraw_no|...
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

    # Only admins can press these
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("Chỉ admin mới thao tác.")
        return

    if action == "withdraw_ok":
        # Deduct amount and notify user
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

# Betting message in groups or private: /T1000 or /X500
async def bet_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text.strip()
    user = update.effective_user
    chat = update.effective_chat
    # accept patterns like /T1000 or /t1000 or /X500
    if not text.startswith("/"):
        return
    cmd = text[1:]
    if len(cmd) < 2:
        return
    prefix = cmd[0].lower()
    if prefix not in ('t', 'x'):
        return
    side = 'tai' if prefix == 't' else 'xiu'
    # parse amount
    try:
        amount = int(cmd[1:])
    except:
        await msg.reply_text("Cú pháp đặt cược sai. Ví dụ: /T1000 hoặc /X5000")
        return
    if amount < MIN_BET:
        await msg.reply_text(f"Đặt cược tối thiểu {MIN_BET:,}₫")
        return

    # If in group, check group approved and running
    if chat.type in ("group", "supergroup"):
        g = db_query("SELECT approved, running FROM groups WHERE chat_id=?", (chat.id,))
        if not g or g[0]["approved"] != 1 or g[0]["running"] != 1:
            await msg.reply_text("Nhóm này chưa được admin duyệt hoặc chưa bật /batdau.")
            return

    # ensure user and check balance
    ensure_user(user.id, user.username or "", user.first_name or "")
    u = get_user(user.id)
    if u["balance"] < amount:
        await msg.reply_text("Số dư không đủ.")
        return

    # Deduct immediately, store bet
    new_balance = u["balance"] - amount
    new_total_bet = (u["total_bet_volume"] or 0.0) + amount
    db_execute("UPDATE users SET balance=?, total_bet_volume=? WHERE user_id=?", (new_balance, new_total_bet, user.id))

    # round_id strategy: use integer floor of timestamp / ROUND_SECONDS as epoch round
    now_ts = int(datetime.utcnow().timestamp())
    round_epoch = now_ts // ROUND_SECONDS
    round_id = f"{chat.id}_{round_epoch}"
    db_execute("INSERT INTO bets(chat_id, round_id, user_id, side, amount, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
               (chat.id, round_id, user.id, side, amount, now_iso()))

    await msg.reply_text(f"Đã đặt {side.upper()} {amount:,}₫ cho phiên hiện tại.")

# Admin /addmoney
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
    db_execute("UPDATE users SET balance=?, total_deposited=? WHERE user_id=?", (new_bal, new_deposited, uid))
    await update.message.reply_text(f"Đã cộng {amt:,.0f}₫ cho user {uid}.")

# Admin top10 by best streak
async def top10_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Chỉ admin.")
        return
    rows = db_query("SELECT user_id, best_streak FROM users ORDER BY best_streak DESC LIMIT 10")
    text = "Top 10 người có chuỗi thắng dài nhất:\n"
    for i, r in enumerate(rows, start=1):
        text += f"{i}. {r['user_id']} — {r['best_streak']} thắng liên tiếp\n"
    await update.message.reply_text(text)

# Admin balance dump (debug)
async def balances_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Chỉ admin.")
        return
    rows = db_query("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 50")
    text = "Top balances:\n"
    for r in rows:
        text += f"- {r['user_id']}: {r['balance']:,.0f}\n"
    await update.message.reply_text(text)

# Admin commands to set result or bet mode:
# /KqTai <chat_id>  => one-shot force to tai (the DB stores 'force_tai' and reverts after one round)
# /KqXiu <chat_id>
# /bettai <chat_id> => continuous bet bệt (always result TAI)
# /betxiu <chat_id>
# /tatbet <chat_id> => revert to random
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
        await update.message.reply_text(f"Đã bật cầu bệt TÀI cho nhóm {chat_id}. (Không thông báo vào nhóm)")
    elif cmd == "/betxiu":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("betxiu", chat_id))
        await update.message.reply_text(f"Đã bật cầu bệt XỈU cho nhóm {chat_id}. (Không thông báo vào nhóm)")
    elif cmd == "/tatbet":
        db_execute("UPDATE groups SET bet_mode=? WHERE chat_id=?", ("random", chat_id))
        await update.message.reply_text(f"Đã tắt cầu bệt và trả về random cho nhóm {chat_id}.")
    else:
        await update.message.reply_text("Lệnh admin không hợp lệ.")

# Group command /batdau: request admin approve, after approve group can run rounds.
async def batdau_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("/batdau chỉ dùng trong nhóm.")
        return
    title = chat.title or ""
    # ensure group stored
    rows = db_query("SELECT chat_id FROM groups WHERE chat_id=?", (chat.id,))
    if not rows:
        db_execute("INSERT INTO groups(chat_id, title, approved, running, bet_mode, last_round) VALUES (?, ?, 0, 0, 'random', ?)",
                   (chat.id, title, 0))
    # send approval request to admins
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Duyệt", callback_data=f"approve|{chat.id}"),
         InlineKeyboardButton("Từ chối", callback_data=f"deny|{chat.id}")]
    ])
    text = f"Yêu cầu bật bot cho nhóm:\n{title}\nchat_id: {chat.id}\nNgười yêu cầu: {update.effective_user.id}"
    # send to each admin
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=text, reply_markup=kb)
        except Exception:
            logger.exception(f"Không gửi được yêu cầu duyệt nhóm tới admin {aid}")
    await update.message.reply_text("Đã gửi yêu cầu tới admin để duyệt.")

# Callback for approve/deny
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
        # notify group
        try:
            await context.bot.send_message(chat_id=chat_id, text="Bot đã được admin duyệt — bắt đầu chạy phiên mỗi 60s. Gõ /batdau để khởi động lại nếu cần.")
        except Exception:
            pass
    else:
        db_execute("UPDATE groups SET approved=0, running=0 WHERE chat_id=?", (chat_id,))
        await query.edit_message_text(f"Đã từ chối cho nhóm {chat_id}.")

# Helper: get groups that are approved and running
def get_active_groups() -> List[Dict[str, Any]]:
    rows = db_query("SELECT chat_id, bet_mode, last_round FROM groups WHERE approved=1 AND running=1")
    return [dict(r) for r in rows]

# -----------------------
# ROUND ENGINE
# -----------------------

# rounds_loop: background coroutine that launches run_round_for_group for each active group every ROUND_SECONDS
async def rounds_loop(app: Application):
    logger.info("Rounds loop starting...")
    # minimal initial delay to let bot boot
    await asyncio.sleep(2)
    while True:
        try:
            groups = get_active_groups()
            if groups:
                logger.debug(f"Active groups: {[g['chat_id'] for g in groups]}")
            tasks = []
            for g in groups:
                chat_id = g['chat_id']
                # Launch a task to run this group's current round.
                tasks.append(asyncio.create_task(run_round_for_group(app, chat_id)))
            # wait for all group tasks to finish (they should be quick) or time out
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            # Notify admins about the exception
            logger.exception("Exception in rounds_loop")
            for aid in ADMIN_IDS:
                try:
                    await app.bot.send_message(chat_id=aid, text=f"ERROR - rounds_loop exception:\n{e}")
                except Exception:
                    pass
        # Sleep until next tick; note that run_round_for_group uses round_id calculation by epoch of ROUND_SECONDS
        await asyncio.sleep(ROUND_SECONDS)

# run_round_for_group: does a single round's processing for the given group
async def run_round_for_group(app: Application, chat_id: int):
    """
    1) Determine current round_id (based on epoch)
    2) Read bets for this round
    3) Decide result (consider group.bet_mode for forced or bet bệt)
    4) Compute winners/losers, update balances & pot accordingly
    5) Handle special triple 1/6 -> distribute pot proportionally to winners
    6) Save history and send messages to group & admin
    """
    try:
        now_ts = int(datetime.utcnow().timestamp())
        round_epoch = now_ts // ROUND_SECONDS
        round_index = round_epoch  # used as incremental epoch
        round_id = f"{chat_id}_{round_epoch}"

        # gather bets
        bets_rows = db_query("SELECT user_id, side, amount FROM bets WHERE chat_id=? AND round_id=?", (chat_id, round_id))
        bets = [dict(r) for r in bets_rows]

        # Get group's bet_mode to determine forced/bettai/betxiu/random
        grows = db_query("SELECT bet_mode FROM groups WHERE chat_id=?", (chat_id,))
        bet_mode = grows[0]["bet_mode"] if grows else "random"

        # Decide result (apply forced once semantics, bet bệt semantics)
        # If bet_mode == force_tai or force_xiu: apply and revert to random
        forced_applied = False
        forced_value = None
        if bet_mode == "force_tai":
            forced_value = "tai"
            forced_applied = True
            # revert to random after applying
            db_execute("UPDATE groups SET bet_mode='random' WHERE chat_id=?", (chat_id,))
        elif bet_mode == "force_xiu":
            forced_value = "xiu"
            forced_applied = True
            db_execute("UPDATE groups SET bet_mode='random' WHERE chat_id=?", (chat_id,))
        elif bet_mode == "bettai":
            forced_value = "tai"
        elif bet_mode == "betxiu":
            forced_value = "xiu"
        # If forced_value is None, roll normally
        if forced_value:
            # to add unpredictability while honoring admin, we still generate dice that match the forced outcome
            # find a random dice triple that leads to desired result, but keep some randomness
            # Simpler: attempt until we get a dice total in target range
            attempts = 0
            dice, total, special = roll_three_dice()
            while result_from_total(total) != forced_value and attempts < 50:
                dice, total, special = roll_three_dice()
                attempts += 1
            result = result_from_total(total)
        else:
            dice, total, special = roll_three_dice()
            result = result_from_total(total)

        # Persist history
        dice_str = ",".join(map(str, dice))
        db_execute("INSERT INTO history(chat_id, round_index, round_id, result, dice, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                   (chat_id, round_index, round_id, result, dice_str, now_iso()))

        # Compute winners & losers
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

        # Losers' amounts go to pot
        if total_loser_bets > 0:
            add_to_pot(total_loser_bets)

        # For each winner: credit payout = amount * WIN_MULTIPLIER; house share = amount * HOUSE_RATE -> add to pot
        winners_paid = []
        for uid, amt in winners:
            # house share to pot
            house_share = amt * HOUSE_RATE
            add_to_pot(house_share)
            payout = amt * WIN_MULTIPLIER
            ensure_user(uid, "", "")
            u = get_user(uid)
            new_balance = (u["balance"] or 0.0) + payout
            # update streaks
            cur_streak = (u["current_streak"] or 0) + 1
            best_streak = max(u["best_streak"] or 0, cur_streak)
            db_execute("UPDATE users SET balance=?, current_streak=?, best_streak=? WHERE user_id=?", (new_balance, cur_streak, best_streak, uid))
            winners_paid.append((uid, payout, amt))

        # For losers: reset streak to 0
        for uid, amt in losers:
            rows = db_query("SELECT current_streak FROM users WHERE user_id=?", (uid,))
            if rows:
                db_execute("UPDATE users SET current_streak=0 WHERE user_id=?", (uid,))

        # Special triple handling: 3x1 or 3x6 -> distribute entire pot proportionally to winners in this round
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
                    special_msg = f"Hũ {pot_amount:,.0f}₫ đã được chia cho người thắng theo tỷ lệ cược!"
                    reset_pot()
            else:
                # if no winners, keep pot as is
                pass

        # Remove bets from DB for this round
        db_execute("DELETE FROM bets WHERE chat_id=? AND round_id=?", (chat_id, round_id))

        # Prepare and send group message: show result and short history
        display = "ĐEN (Tài)" if result == "tai" else "TRẮNG (Xỉu)"
        msg = f"▶️ Phiên {round_index} — Kết quả: {display}\n"
        msg += f"Xúc xắc: {dice_str} — Tổng: {total}\n"
        if special_msg:
            msg += f"\n{special_msg}\n"
        # provide short history (last 10)
        hist_rows = db_query("SELECT result, dice, timestamp FROM history WHERE chat_id=? ORDER BY id DESC LIMIT 10", (chat_id,))
        if hist_rows:
            msg += "\nLịch sử (gần nhất):\n"
            for hr in hist_rows:
                r = hr["result"]
                d = hr["dice"]
                rdisp = "ĐEN" if r == "tai" else "TRẮNG"
                msg += f"- {rdisp} | {d}\n"

        # Send message to group (do not reveal admin forced actions — we only post final)
        try:
            await app.bot.send_message(chat_id=chat_id, text=msg)
        except Exception:
            logger.exception("Cannot send round result to group")

        # Also send short summary to admins (optional)
        if winners_paid:
            admin_summary = f"Round {round_index} in group {chat_id} completed.\nResult: {result}\nWinners:\n"
            for uid, payout, amt in winners_paid:
                admin_summary += f"- {uid}: đặt {amt:,.0f} -> nhận {payout:,.0f}\n"
            try:
                for aid in ADMIN_IDS:
                    await app.bot.send_message(chat_id=aid, text=admin_summary)
            except Exception:
                pass

    except Exception as e:
        logger.exception("Exception in run_round_for_group")
        # notify admins
        for aid in ADMIN_IDS:
            try:
                await app.bot.send_message(chat_id=aid, text=f"ERROR - run_round_for_group exception for group {chat_id}: {e}\n{traceback.format_exc()}")
            except Exception:
                pass

# -----------------------
# STARTUP / SHUTDOWN HANDLERS
# -----------------------

import asyncio
from telegram.ext import Application

async def on_startup(app: Application):
    logger.info("Bot starting up...")
    # Đảm bảo DB đã được khởi tạo
    init_db()

    # ✅ Chờ 1 chút để bot thực sự vào vòng lặp event
    await asyncio.sleep(1)

    # ✅ Tạo task đúng cách sau khi vòng lặp event đã sẵn sàng
    loop = asyncio.get_running_loop()
    loop.create_task(rounds_loop(app))

    # Gửi thông báo tới admin khi bot khởi động
    for aid in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=aid, text="✅ Bot đã khởi động và sẵn sàng.")
        except Exception:
            pass


async def on_shutdown(app: Application):
    logger.info("Bot shutting down...")
    # Gửi thông báo tới admin khi bot tắt
    for aid in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=aid, text="⚠️ Bot đang tắt (shutdown).")
        except Exception:
            pass


# Exception handler cho lỗi cấp vòng lặp
def handle_loop_exception(loop, context):
    msg = context.get("exception", context.get("message"))
    logger.error(f"Caught exception in event loop: {msg}")
    # Không gửi tin nhắn ở đây vì không có context của bot


# -----------------------
# MAIN: Build Application & Handlers
# -----------------------
import asyncio
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters

def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: BOT_TOKEN not set. Please edit bot.py and set BOT_TOKEN.")
        return

    # init db sớm
    init_db()

    # Build application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers - commands
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("game", game_info))
    app.add_handler(CommandHandler("nap", nap_info))
    app.add_handler(CommandHandler("ruttien", ruttien_handler))
    app.add_handler(CallbackQueryHandler(withdraw_callback_handler, pattern=r"^withdraw_.*|^withdraw.*"))

    # Admin commands
    app.add_handler(CommandHandler("addmoney", addmoney_handler))
    app.add_handler(CommandHandler("top10", top10_handler))
    app.add_handler(CommandHandler("balances", balances_handler))
    app.add_handler(CommandHandler("KqTai", admin_force_handler))
    app.add_handler(CommandHandler("KqXiu", admin_force_handler))
    app.add_handler(CommandHandler("bettai", admin_force_handler))
    app.add_handler(CommandHandler("betxiu", admin_force_handler))
    app.add_handler(CommandHandler("tatbet", admin_force_handler))

    # Group control
    app.add_handler(CommandHandler("batdau", batdau_handler))
    app.add_handler(CallbackQueryHandler(approve_callback_handler, pattern=r"^(approve|deny)\|"))

    # Betting messages (pattern /T123 /X500)
    app.add_handler(MessageHandler(filters.Regex(r"^/[TtXx]\d+"), bet_message_handler))

    # Menu text in private
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_handler))

    # ✅ Hook startup & shutdown đúng cú pháp
    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    # ✅ Exception handler cho loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(handle_loop_exception)

    # ✅ Run polling
    try:
        logger.info("🚀 Bot đang chạy polling...")
        app.run_polling(poll_interval=1.0)
    except Exception as e:
        logger.exception(f"Lỗi khi chạy bot: {e}")

if __name__ == "__main__":
    main()
