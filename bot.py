#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Tài Xỉu chuyên nghiệp - single file
Tính năng:
- Khi được add vào nhóm: admin phải cấp phép nhóm cho bot chạy
- /batdau trong nhóm -> bot bắt đầu chạy phiên 45s
- Người chơi cược: /T1000 (Tài) hoặc /X1000 (Xỉu)
- Hũ (pot) được cộng theo quy tắc ở phần "Lưu ý"
- Triple 1 hoặc triple 6 -> chia hũ cho người thắng ở phiên đó
- Admin commands (riêng tư chat với bot): /allowgroup, /denygroup, /KqTai, /KqXiu, /bettai, /betxiu, /tatbet, /addmoney, /top10, xử lý rút tiền
- Khi crash bot sẽ báo cho admin
"""

import os
import asyncio
import logging
import random
import aiosqlite
import signal
from datetime import datetime
from typing import Dict, List, Any, Optional
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

# -------------------------
# Configuration (ENV)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
DB_FILE = os.getenv("DB_FILE", "./data/bot.db")
ROUND_INTERVAL = int(os.getenv("ROUND_INTERVAL", "45"))  # seconds
PAYOUT_MULTIPLIER = 1.97
POT_FEE_RATIO = 0.3  # 0.3 * bet => chuyển vào hũ khi người thắng (theo yêu cầu)
# -------------------------

# Basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("taixiu-bot")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN chưa được đặt. Hãy set biến môi trường BOT_TOKEN.")
    raise SystemExit("BOT_TOKEN required")

# -------------------------
# DB helpers (sqlite async)
# Tables:
# - users: tg_id, balance, streak_win, total_wins
# - groups: chat_id, allowed (0/1), running (0/1), last_started
# - bets: session_id, chat_id, user_id, side, amount, result (nullable)
# - history: chat_id, session_id, result, dice, created_at
# - pot: total
# - withdraws: id, user_id, bank_name, account_number, amount, status
# -------------------------
async def init_db():
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            balance REAL DEFAULT 0,
            streak_win INTEGER DEFAULT 0,
            total_wins INTEGER DEFAULT 0,
            total_bets REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            allowed INTEGER DEFAULT 0,
            running INTEGER DEFAULT 0,
            last_started TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            started_at TEXT,
            forced_result TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            chat_id INTEGER,
            user_id INTEGER,
            side TEXT, -- 'T' or 'X'
            amount REAL,
            resolved INTEGER DEFAULT 0,
            won INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            session_id INTEGER,
            result TEXT,
            dice TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pot (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS withdraws (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bank_name TEXT,
            account_number TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        );
        INSERT OR IGNORE INTO pot (id, total) VALUES (1, 0);
        """)
        await db.commit()
    logger.info("DB initialized at %s", DB_FILE)

# -------------------------
# Utilities
# -------------------------
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        uid = user.id if user else None
        if uid not in ADMIN_IDS:
            if update.effective_chat and update.effective_chat.type == "private":
                await update.message.reply_text("Bạn không có quyền admin.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

async def get_balance(db, tg_id: int) -> float:
    cur = await db.execute("SELECT balance FROM users WHERE tg_id = ?", (tg_id,))
    row = await cur.fetchone()
    return float(row[0]) if row else 0.0

async def set_balance(db, tg_id: int, amount: float):
    await db.execute("INSERT INTO users (tg_id, balance) VALUES (?, ?) ON CONFLICT(tg_id) DO UPDATE SET balance=excluded.balance", (tg_id, amount))
    await db.commit()

async def change_balance(db, tg_id: int, delta: float):
    cur = await db.execute("SELECT balance FROM users WHERE tg_id = ?", (tg_id,))
    row = await cur.fetchone()
    if row:
        new = float(row[0]) + delta
        await db.execute("UPDATE users SET balance = ? WHERE tg_id = ?", (new, tg_id))
    else:
        new = delta
        await db.execute("INSERT INTO users (tg_id, balance) VALUES (?, ?)", (tg_id, new))
    await db.commit()
    return new

async def add_to_pot(db, amount: float):
    await db.execute("UPDATE pot SET total = total + ? WHERE id = 1", (amount,))
    await db.commit()

async def get_pot(db) -> float:
    cur = await db.execute("SELECT total FROM pot WHERE id = 1")
    r = await cur.fetchone()
    return float(r[0]) if r else 0.0

async def reset_pot(db):
    await db.execute("UPDATE pot SET total = 0 WHERE id = 1")
    await db.commit()

# -------------------------
# In-memory control for sessions per chat
# -------------------------
chat_tasks: Dict[int, asyncio.Task] = {}
# forced results per chat: if set to 'T' or 'X' then next round result forced; cleared after used unless persistent forced_result in session.
chat_forced_mode: Dict[int, Optional[str]] = {}  # 'T', 'X', or None
# bet mode (bệt): None/'T'/'X' - if set, override random to repeated T or X until /tatbet
chat_bet_mode: Dict[int, Optional[str]] = {}

# -------------------------
# Core game logic
# -------------------------
def roll_three_dice():
    d = [random.randint(1,6) for _ in range(3)]
    s = sum(d)
    if d[0] == d[1] == d[2]:
        triple = True
    else:
        triple = False
    # Tài if sum 11-17, Xỉu if 4-10
    if 4 <= s <= 10:
        side = 'X'  # Xỉu (Trắng)
    elif 11 <= s <= 17:
        side = 'T'  # Tài (Đen)
    else:
        side = 'X'  # shouldn't happen but default
    return d, s, side, triple

async def resolve_session(db, application: Application, chat_id: int, session_id: int, forced_result: Optional[str]=None):
    """
    Resolve bets in DB for given session_id
    forced_result: 'T' or 'X' to override random (admin KqTai/KqXiu or bet mode)
    """
    # roll
    if forced_result:
        # if forced_result is 'T' -> make dice sum in 11-17 or triple logic
        if forced_result == 'T':
            # pick a random T sum (11-17) and craft dice if possible; simplest: roll until side==T
            while True:
                d, s, side, triple = roll_three_dice()
                if side == 'T':
                    break
        else:
            while True:
                d, s, side, triple = roll_three_dice()
                if side == 'X':
                    break
    else:
        d, s, side, triple = roll_three_dice()

    dice_str = ",".join(map(str,d))
    result_text = "T" if side == 'T' else "X"

    # Fetch bets for this session
    cur = await db.execute("SELECT id, user_id, side, amount FROM bets WHERE session_id = ? AND resolved = 0", (session_id,))
    bets = await cur.fetchall()

    winners = []
    losers = []
    total_pot_add = 0.0

    # compute payouts
    for bet in bets:
        bet_id, user_id, bside, amount = bet
        amount = float(amount)
        if bside == result_text:
            # winner
            # trích POT_FEE_RATIO * amount vào hũ, người thắng nhận PAYOUT_MULTIPLIER*amount - POT_FEE_RATIO*amount
            fee = POT_FEE_RATIO * amount
            payout = PAYOUT_MULTIPLIER * amount - fee
            # apply to user balance
            await change_balance(db, user_id, payout)
            # update winner stats
            await db.execute("UPDATE users SET streak_win = streak_win + 1, total_wins = total_wins + 1, total_bets = total_bets + ? WHERE tg_id = ?", (amount, user_id))
            # mark bet resolved, won
            await db.execute("UPDATE bets SET resolved = 1, won = 1 WHERE id = ?", (bet_id,))
            winners.append((user_id, payout, amount))
            total_pot_add += fee
        else:
            # loser: their bet added to pot
            await db.execute("UPDATE bets SET resolved = 1, won = 0 WHERE id = ?", (bet_id,))
            # subtract amount from user's balance (assuming already deducted at bet time)
            # add to pot
            total_pot_add += amount
            # reset streak
            await db.execute("UPDATE users SET streak_win = 0, total_bets = total_bets + ? WHERE tg_id = ?", (amount, user_id))
            losers.append((user_id, amount))

    await db.commit()
    if total_pot_add > 0:
        await add_to_pot(db, total_pot_add)

    # Add to history
    await db.execute("INSERT INTO history (chat_id, session_id, result, dice, created_at) VALUES (?, ?, ?, ?, ?)",
                     (chat_id, session_id, result_text, dice_str, datetime.utcnow().isoformat()))
    await db.commit()

    # If triple 1 or triple 6 -> share pot among winners of that session
    if d[0] == d[1] == d[2] and (d[0] == 1 or d[0] == 6):
        # special share
        pot_total = await get_pot(db)
        if pot_total > 0 and winners:
            share_each = pot_total / len(winners)
            for (uid, _, _) in winners:
                await change_balance(db, uid, share_each)
            await reset_pot(db)
            # notify admins about special distribution
            for adm in ADMIN_IDS:
                try:
                    await application.bot.send_message(adm, f"Chia hũ {pot_total:.2f} cho {len(winners)} người thắng ở phiên {session_id} (triple {d[0]}). Mỗi người: {share_each:.2f}")
                except Exception as e:
                    logger.exception("Cannot notify admin about pot share: %s", e)

    # Send result message to group
    # Build summary
    txt = f"🎲 Kết quả phiên #{session_id} — Dice: {dice_str} — Tổng: {s} — {'Tài (Đen)' if side=='T' else 'Xỉu (Trắng)'}\n"
    if winners:
        txt += f"Người thắng: {len(winners)}\n"
    else:
        txt += "Không có người thắng trong phiên này.\n"
    pot_now = await get_pot(db)
    txt += f"Hũ hiện tại: {pot_now:.2f}\n"

    # send to group
    try:
        await application.bot.send_message(chat_id=chat_id, text=txt)
    except Exception as e:
        logger.exception("Không gửi được kết quả nhóm %s: %s", chat_id, e)

    # After result, send history summary (last 10 results for the group)
    cur2 = await db.execute("SELECT result, dice, created_at FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT 10", (chat_id,))
    his = await cur2.fetchall()
    hist_txt = "Lịch sử (gần nhất -> cũ):\n"
    for r in his:
        rr, dd, ca = r
        hist_txt += f"{'Đen' if rr=='T' else 'Trắng'} [{dd}] - {ca}\n"
    try:
        await application.bot.send_message(chat_id=chat_id, text=hist_txt)
    except Exception:
        pass

# -------------------------
# Session scheduler per chat
# -------------------------
async def chat_runner(application: Application, chat_id: int):
    """
    Runs rounds every ROUND_INTERVAL seconds while group's running flag is 1 and allowed = 1
    Each round:
    - create session, accept bets during waiting period (we assume bets placed between rounds using /T /X)
    - after waiting, resolve bets
    """
    logger.info("Starting runner for chat %s", chat_id)
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            while True:
                # check group allowed & running
                cur = await db.execute("SELECT allowed, running FROM groups WHERE chat_id = ?", (chat_id,))
                row = await cur.fetchone()
                if not row or row[0] != 1 or row[1] != 1:
                    logger.info("Group %s no longer allowed/running, stopping runner", chat_id)
                    break
                # create session
                started_at = datetime.utcnow().isoformat()
                await db.execute("INSERT INTO sessions (chat_id, started_at) VALUES (?, ?)", (chat_id, started_at))
                await db.commit()
                cur2 = await db.execute("SELECT last_insert_rowid()")
                r = await cur2.fetchone()
                session_id = r[0]
                # inform group: phiên bắt đầu, mọi người đặt cược trong vòng ROUND_INTERVAL giây
                try:
                    await application.bot.send_message(chat_id=chat_id, text=f"🔔 Phiên #{session_id} bắt đầu! Bạn có {ROUND_INTERVAL} giây để cược. Gõ /T<amount> cho Tài hoặc /X<amount> cho Xỉu. Ví dụ: /T1000")
                except Exception as e:
                    logger.exception("Không gửi thông báo bắt đầu phiên: %s", e)

                # wait for the interval, but allow forced admin Kq to set forced_result for next resolution
                await asyncio.sleep(ROUND_INTERVAL)

                # Check forced result mode or bet mode for this chat
                forced = None
                if chat_forced_mode.get(chat_id):
                    forced = chat_forced_mode[chat_id]
                    # clear one-time forced after using
                    chat_forced_mode[chat_id] = None

                # bet mode (bệt) may set repeated side
                bet_mode = chat_bet_mode.get(chat_id)
                if bet_mode:
                    forced = bet_mode

                # resolve session
                await resolve_session(db, application, chat_id, session_id, forced_result=forced)

                # continue to next round
    except asyncio.CancelledError:
        logger.info("Runner for chat %s cancelled", chat_id)
    except Exception as e:
        logger.exception("Runner crashed for chat %s: %s", chat_id, e)
        # notify admins
        for adm in ADMIN_IDS:
            try:
                await application.bot.send_message(adm, f"Bot bị lỗi ở runner nhóm {chat_id}: {e}")
            except:
                pass
    finally:
        chat_tasks.pop(chat_id, None)
        logger.info("Runner stopped for chat %s", chat_id)

# -------------------------
# Command Handlers
# -------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != "private":
        # in group, simple greet
        await update.message.reply_text("Mình là bot Tài Xỉu. Dùng /batdau để bắt đầu phiên (yêu cầu nhóm đã được admin cấp phép).")
        return
    # private start: greet and give 10k free (10000 units)
    async with aiosqlite.connect(DB_FILE) as db:
        # create user if not exist
        await db.execute("INSERT OR IGNORE INTO users (tg_id, username, first_name, last_name, balance) VALUES (?, ?, ?, ?, ?)",
                         (user.id, user.username or "", user.first_name or "", user.last_name or "", 10000.0))
        # if existed but balance 0, we may choose not to topup. Simpler: only give 10k on first start
        # Check whether this is first time (streak as proxy)
        cur = await db.execute("SELECT balance FROM users WHERE tg_id = ?", (user.id,))
        row = await cur.fetchone()
        await db.commit()
        await update.message.reply_text("Xin chào! Mình tặng bạn 10,000₫ (số ảo) để thử chơi. Gõ /menu để xem các tùy chọn.")

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("Game & Hướng dẫn", callback_data="menu_game")],
        [InlineKeyboardButton("Nạp tiền", callback_data="menu_topup")],
        [InlineKeyboardButton("Rút tiền", callback_data="menu_withdraw")],
        [InlineKeyboardButton("Số dư", callback_data="menu_balance")]
    ]
    await update.message.reply_text("Chọn một mục:", reply_markup=InlineKeyboardMarkup(kb))

async def menu_button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "menu_game":
        await q.edit_message_text("Game: Tài Xỉu. Link nhóm: @VET789cc\nCách chơi: Gõ /T<amount> cho Tài, /X<amount> cho Xỉu. Ví dụ /T1000\nPhiên chạy mỗi 45s.")
    elif data == "menu_topup":
        await q.edit_message_text("Nạp tiền: Liên hệ: @HOANGDUNGG789")
    elif data == "menu_withdraw":
        await q.edit_message_text("Rút tiền:\nĐể rút tiền hãy nhập lệnh:\n/ruttien <Ngân hàng của bạn> <Số tài khoản> <Số tiền>\nRút tối thiểu 100000 vnđ\nPhải cược 0.9 vòng cược")
    elif data == "menu_balance":
        async with aiosqlite.connect(DB_FILE) as db:
            bal = await get_balance(db, q.from_user.id)
        await q.edit_message_text(f"Số dư hiện tại: {bal:.2f}")

# Betting command: parse /T1000 or /X1000
async def bet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Lệnh cược chỉ dùng trong nhóm.")
        return
    # parse
    if msg[0].upper() == 'T':
        side = 'T'
        amt_str = msg[1:]
    elif msg[0].upper() == 'X':
        side = 'X'
        amt_str = msg[1:]
    else:
        return
    try:
        amt = float(amt_str)
        if amt <= 0:
            raise ValueError()
    except:
        await update.message.reply_text("Sai cú pháp cược. Ví dụ: /T1000")
        return

    # check group allowed & running
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT allowed, running FROM groups WHERE chat_id = ?", (chat.id,))
        row = await cur.fetchone()
        if not row or row[0] != 1 or row[1] != 1:
            await update.message.reply_text("Nhóm chưa bật game hoặc chưa được cấp phép.")
            return
        # get latest session for chat
        cur2 = await db.execute("SELECT session_id FROM sessions WHERE chat_id = ? ORDER BY session_id DESC LIMIT 1", (chat.id,))
        row2 = await cur2.fetchone()
        if not row2:
            await update.message.reply_text("Chưa có phiên để đặt cược, vui lòng chờ phiên tiếp theo.")
            return
        session_id = row2[0]
        # check user balance
        cur3 = await db.execute("SELECT balance FROM users WHERE tg_id = ?", (user.id,))
        r3 = await cur3.fetchone()
        bal = float(r3[0]) if r3 else 0.0
        if bal < amt:
            await update.message.reply_text(f"Số dư không đủ. Hiện có: {bal:.2f}")
            return
        # deduct immediately
        await change_balance(db, user.id, -amt)
        # create bet
        await db.execute("INSERT INTO bets (session_id, chat_id, user_id, side, amount) VALUES (?, ?, ?, ?, ?)",
                         (session_id, chat.id, user.id, side, amt))
        await db.commit()
        await update.message.reply_text(f"Đặt cược {amt:.2f} cho {'Tài' if side=='T' else 'Xỉu'}. Chúc may mắn!")

# /batdau in group - request start, but requires group allowed by admin
async def batdau_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Lệnh /batdau chỉ dùng trong nhóm.")
        return
    async with aiosqlite.connect(DB_FILE) as db:
        # check allowed
        cur = await db.execute("SELECT allowed, running FROM groups WHERE chat_id = ?", (chat.id,))
        row = await cur.fetchone()
        if not row or row[0] != 1:
            # notify admins that this group wants permission
            for adm in ADMIN_IDS:
                try:
                    await context.bot.send_message(adm, f"Yêu cầu cấp phép cho nhóm {chat.title or chat.id} ({chat.id}). Để cho phép, trả lời lệnh /allowgroup {chat.id} trong riêng tư với bot.")
                except Exception:
                    pass
            await update.message.reply_text("Nhóm chưa được cấp phép. Mình đã gửi yêu cầu cho admin để cấp quyền.")
            # ensure group record exists
            await db.execute("INSERT OR IGNORE INTO groups (chat_id, allowed, running) VALUES (?, ?, ?)", (chat.id, 0, 0))
            await db.commit()
            return
        # if allowed, start runner if not already running
        if row[1] == 1:
            await update.message.reply_text("Phiên đã đang chạy trong nhóm.")
            return
        # set running = 1
        await db.execute("UPDATE groups SET running = 1, last_started = ? WHERE chat_id = ?", (datetime.utcnow().isoformat(), chat.id))
        await db.commit()
    # spawn runner task
    app = context.application
    if chat.id in chat_tasks:
        await update.message.reply_text("Runner đã chạy.")
        return
    t = app.create_task(chat_runner(app, chat.id))
    chat_tasks[chat.id] = t
    await update.message.reply_text(f"Đã bật chế độ chơi tự động - mỗi {ROUND_INTERVAL}s sẽ có 1 phiên.")

# Admin commands (private chat)
@admin_only
async def allowgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # usage: /allowgroup <chat_id>
    args = context.args
    if not args:
        await update.message.reply_text("Cú pháp: /allowgroup <chat_id>")
        return
    try:
        cid = int(args[0])
    except:
        await update.message.reply_text("chat_id phải là số nguyên.")
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR REPLACE INTO groups (chat_id, allowed, running) VALUES (?, ?, ?)", (cid, 1, 0))
        await db.commit()
    await update.message.reply_text(f"Đã cấp phép cho nhóm {cid}.")

@admin_only
async def denygroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Cú pháp: /denygroup <chat_id>")
        return
    cid = int(args[0])
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE groups SET allowed = 0, running = 0 WHERE chat_id = ?", (cid,))
        await db.commit()
    # cancel runner if any
    if cid in chat_tasks:
        chat_tasks[cid].cancel()
    await update.message.reply_text(f"Đã thu hồi phép nhóm {cid}.")

@admin_only
async def stopgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Cú pháp: /stopgroup <chat_id>")
        return
    cid = int(args[0])
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE groups SET running = 0 WHERE chat_id = ?", (cid,))
        await db.commit()
    if cid in chat_tasks:
        chat_tasks[cid].cancel()
    await update.message.reply_text(f"Đã tắt chạy tự động nhóm {cid}.")

@admin_only
async def kqtai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Force next resolution in last session for a group to T
    args = context.args
    if not args:
        await update.message.reply_text("Cú pháp: /KqTai <chat_id>")
        return
    cid = int(args[0])
    # set one-time forced mode
    chat_forced_mode[cid] = 'T'
    await update.message.reply_text(f"Đã set kết quả tiếp theo cho nhóm {cid} là Tài (lệnh im lặng, không báo trong nhóm).")

@admin_only
async def kqxiu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Cú pháp: /KqXiu <chat_id>")
        return
    cid = int(args[0])
    chat_forced_mode[cid] = 'X'
    await update.message.reply_text(f"Đã set kết quả tiếp theo cho nhóm {cid} là Xỉu (lệnh im lặng).")

@admin_only
async def bettai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /bettai <chat_id> => turn on repeated T
    args = context.args
    if not args:
        await update.message.reply_text("Cú pháp: /bettai <chat_id>")
        return
    cid = int(args[0])
    chat_bet_mode[cid] = 'T'
    await update.message.reply_text(f"Bật chế độ bệt Tài cho nhóm {cid} (im lặng ở nhóm).")

@admin_only
async def betxiu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Cú pháp: /betxiu <chat_id>")
        return
    cid = int(args[0])
    chat_bet_mode[cid] = 'X'
    await update.message.reply_text(f"Bật chế độ bệt Xỉu cho nhóm {cid} (im lặng ở nhóm).")

@admin_only
async def tatbet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Cú pháp: /tatbet <chat_id>")
        return
    cid = int(args[0])
    chat_bet_mode[cid] = None
    await update.message.reply_text(f"Tắt chế độ bệt cho nhóm {cid} — trở về random.")

@admin_only
async def addmoney_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /addmoney <tg_id> <amount>
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Cú pháp: /addmoney <tg_id> <amount>")
        return
    uid = int(args[0])
    amt = float(args[1])
    async with aiosqlite.connect(DB_FILE) as db:
        new = await change_balance(db, uid, amt)
    await update.message.reply_text(f"Đã cộng {amt:.2f} cho {uid}. Số dư mới: {new:.2f}")

@admin_only
async def top10_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # top 10 users by streak_win
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT tg_id, username, streak_win FROM users ORDER BY streak_win DESC LIMIT 10")
        rows = await cur.fetchall()
    txt = "Top 10 chuỗi thắng:\n"
    for r in rows:
        txt += f"{r[0]} @{r[1] or 'no'} - Chuỗi: {r[2]}\n"
    await update.message.reply_text(txt)

# Withdraw command by user: /ruttien <bank> <account> <amount>
async def ruttien_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user = update.effective_user
    if len(args) < 3:
        await update.message.reply_text("Cú pháp: /ruttien <Ngân hàng> <Số tài khoản> <Số tiền>")
        return
    bank = args[0]
    acc = args[1]
    try:
        amt = float(args[2])
    except:
        await update.message.reply_text("Số tiền không hợp lệ.")
        return
    if amt < 100000:
        await update.message.reply_text("Rút tối thiểu 100000 vnđ.")
        return
    async with aiosqlite.connect(DB_FILE) as db:
        # check 0.9 vòng cược: approximate by total_bets >= 0.9 * (???)
        # We'll interpret: user must have placed bets totaling >= 0.9 * requested amount
        cur = await db.execute("SELECT total_bets, balance FROM users WHERE tg_id = ?", (user.id,))
        r = await cur.fetchone()
        total_bets = float(r[0]) if r else 0.0
        bal = float(r[1]) if r else 0.0
        if total_bets < 0.9 * amt:
            await update.message.reply_text("Bạn chưa cược đủ 0.9 vòng (tổng cược chưa đạt yêu cầu).")
            return
        if bal < amt:
            await update.message.reply_text("Số dư không đủ để rút.")
            return
        # create withdraw request
        await db.execute("INSERT INTO withdraws (user_id, bank_name, account_number, amount, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                         (user.id, bank, acc, amt, datetime.utcnow().isoformat()))
        # deduct temporarily to avoid double requests
        await change_balance(db, user.id, -amt)
        await db.commit()
        # notify admins with inline buttons to approve/deny
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Thành công", callback_data=f"wd_ok:{user.id}:{amt}"),
             InlineKeyboardButton("Từ chối", callback_data=f"wd_reject:{user.id}:{amt}")]
        ])
        for adm in ADMIN_IDS:
            try:
                await context.bot.send_message(adm, f"Yêu cầu rút tiền từ {user.id} - {user.full_name} - {bank} - {acc} - {amt:.2f}", reply_markup=kb)
            except:
                pass
        await update.message.reply_text("Vui lòng chờ, nếu sau 1 tiếng chưa thấy thông báo Thành công/Từ chối thì nhắn admin nhé!")

# Callback handler for withdraw admin buttons
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    parts = data.split(":")
    if len(parts) < 3:
        return
    cmd, uid_s, amt_s = parts[0], parts[1], parts[2]
    uid = int(uid_s)
    amt = float(amt_s)
    # Only admin can press; we check sender
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("Bạn không có quyền xử lý yêu cầu này.")
        return
    async with aiosqlite.connect(DB_FILE) as db:
        if cmd == "wd_ok":
            # mark withdraw as success
            await db.execute("UPDATE withdraws SET status = 'done' WHERE user_id = ? AND amount = ? AND status = 'pending'", (uid, amt))
            await db.commit()
            # notify user
            try:
                await context.bot.send_message(uid, f"Yêu cầu rút tiền {amt:.2f} đã được duyệt. Vui lòng kiểm tra tài khoản ngân hàng.")
            except:
                pass
            await q.edit_message_text("Đã thực hiện: Thành công.")
        elif cmd == "wd_reject":
            # mark reject and refund
            await db.execute("UPDATE withdraws SET status = 'rejected' WHERE user_id = ? AND amount = ? AND status = 'pending'", (uid, amt))
            # refund balance
            await change_balance(db, uid, amt)
            await db.commit()
            try:
                await context.bot.send_message(uid, f"Yêu cầu rút tiền {amt:.2f} đã bị từ chối. Tiền đã được hoàn về tài khoản của bạn.")
            except:
                pass
            await q.edit_message_text("Đã thực hiện: Từ chối và hoàn tiền.")

# Basic commands
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with aiosqlite.connect(DB_FILE) as db:
        bal = await get_balance(db, user.id)
    await update.message.reply_text(f"Số dư hiện tại: {bal:.2f}")

# Admin: list crash /set up exception handler (we'll notify in main exception)
@admin_only
async def crash_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raise RuntimeError("Crash test by admin")

# echo / help
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("/menu - Menu\n/start - bắt đầu\n/balance - Xem số dư\n/ruttien - rút tiền\n")
    else:
        await update.message.reply_text("/batdau - bật phiên tự động (yêu cầu nhóm được cấp phép)\n/T<amount> hoặc /X<amount> để cược.")

# -------------------------
# Startup and shutdown
# -------------------------
async def on_startup(app: Application):
    await init_db()
    # Restart tasks for groups that were running before possible crash
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT chat_id FROM groups WHERE allowed = 1 AND running = 1")
        rows = await cur.fetchall()
    for r in rows:
        cid = r[0]
        if cid not in chat_tasks:
            t = app.create_task(chat_runner(app, cid))
            chat_tasks[cid] = t
    logger.info("Bot started. Active runners: %s", list(chat_tasks.keys()))

async def on_shutdown(app: Application):
    # cancel tasks
    for t in list(chat_tasks.values()):
        t.cancel()
    logger.info("Shutting down...")

# Global error handler to notify admin
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Exception in update handler: %s", context.error)
    # Notify admins
    for adm in ADMIN_IDS:
        try:
            await context.bot.send_message(adm, f"Bot gặp lỗi: {context.error}")
        except:
            pass

# -------------------------
# Main
# -------------------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # user commands
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("menu", menu_cmd))
    application.add_handler(CallbackQueryHandler(menu_button_cb, pattern="menu_"))
    application.add_handler(CommandHandler("balance", balance_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("batdau", batdau_cmd))
    application.add_handler(CommandHandler("ruttien", ruttien_cmd))

    # betting: treat messages starting with /T or /X followed by number
    application.add_handler(MessageHandler(filters.Regex(r"^/[Tt]\d+$") | filters.Regex(r"^/[Xx]\d+$"), bet_handler))

    # admin commands (private only)
    application.add_handler(CommandHandler("allowgroup", allowgroup_cmd))
    application.add_handler(CommandHandler("denygroup", denygroup_cmd))
    application.add_handler(CommandHandler("stopgroup", stopgroup_cmd))
    application.add_handler(CommandHandler("KqTai", kqtai_cmd))
    application.add_handler(CommandHandler("KqXiu", kqxiu_cmd))
    application.add_handler(CommandHandler("bettai", bettai_cmd))
    application.add_handler(CommandHandler("betxiu", betxiu_cmd))
    application.add_handler(CommandHandler("tatbet", tatbet_cmd))
    application.add_handler(CommandHandler("addmoney", addmoney_cmd))
    application.add_handler(CommandHandler("top10", top10_cmd))
    application.add_handler(CommandHandler("crashtest", crash_test))

    # callback handler for withdraw approval
    application.add_handler(CallbackQueryHandler(callback_query_handler, pattern="^wd_"))

    application.add_error_handler(error_handler)

    application.post_init = on_startup
    application.stop = on_shutdown

    # run
    logger.info("Starting application...")
    application.run_polling(stop_signals=None)  # Render will keep process alive

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # notify admins if crash on startup
        logger.exception("Bot crashed at startup: %s", e)
        for adm in ADMIN_IDS:
            # best-effort: we cannot use bot API before Application built, but we can log
            logger.info("Would notify admin %s about crash: %s", adm, e)
        raise
