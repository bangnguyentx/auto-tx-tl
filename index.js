// index.js
require('dotenv').config();
const { Telegraf, Markup } = require('telegraf');
const { Pool } = require('pg');
const crypto = require('crypto');

const BOT_TOKEN = process.env.TELEGRAM_TOKEN;
const ADMIN_IDS = (process.env.ADMIN_IDS || '').split(',').map(s => s.trim()).filter(Boolean).map(Number);
const GROUP_ID = process.env.GROUP_ID ? Number(process.env.GROUP_ID) : null;
const ROUND_INTERVAL = Number(process.env.ROUND_INTERVAL || 60); // seconds
const WITHDRAW_MIN = 100000;
const HOUSE_FEE_PERCENT = 0.03; // 3% -> 0.03
const PAYOUT_MULTIPLIER = 1.97; // payout when win
const BONUS_AMOUNT = 10000; // 10k first join
const BONUS_MAX_BET = 1000; // can only bet up to 1k when using bonus
const PORT = process.env.PORT || 3000;

if (!BOT_TOKEN) {
  console.error("Missing TELEGRAM_TOKEN env var");
  process.exit(1);
}
if (!GROUP_ID) {
  console.error("Missing GROUP_ID env var");
  // don't exit; we can still run in private for admin flows, but warn
}

const bot = new Telegraf(BOT_TOKEN);
const pool = new Pool({ connectionString: process.env.DATABASE_URL });

// helper
function safeRandomDice() {
  // returns 1..6
  return crypto.randomInt(1, 7);
}

async function query(sql, params) {
  const client = await pool.connect();
  try {
    const res = await client.query(sql, params);
    return res;
  } finally {
    client.release();
  }
}

// init helpers
async function ensureUser(id, name) {
  await query(
    `INSERT INTO users (id, display_name) VALUES ($1, $2) 
     ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name`,
    [id, name || '']
  );
  await query(
    `INSERT INTO stats (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING`,
    [id]
  );
}

// create new round
async function createRound() {
  const res = await query(`INSERT INTO rounds (started_at) VALUES (now()) RETURNING id, started_at`, []);
  return res.rows[0];
}

// get current open round (not rolled)
async function getOpenRound() {
  const res = await query(`SELECT * FROM rounds WHERE rolled_at IS NULL ORDER BY id DESC LIMIT 1`);
  return res.rows[0];
}

// roll round: accept optional override {d1,d2,d3}
async function rollRound(override) {
  // get open round or create
  let round = await getOpenRound();
  if (!round) {
    round = await createRound();
  }
  const roundId = round.id;
  let d1,d2,d3;
  if (override && override.d1 && override.d2 && override.d3) {
    d1 = override.d1; d2 = override.d2; d3 = override.d3;
    await query(`UPDATE rounds SET overridden = true WHERE id=$1`, [roundId]);
  } else {
    d1 = safeRandomDice(); d2 = safeRandomDice(); d3 = safeRandomDice();
  }
  const total = d1 + d2 + d3;
  const result = (total >= 11 && total <= 17) ? 'TAI' : 'XIU'; // tài/xỉu rule
  // update round
  const potSnapshotRes = await query(`SELECT balance FROM house WHERE id=1`);
  const potSnapshot = (potSnapshotRes.rows[0] && potSnapshotRes.rows[0].balance) || 0;
  await query(
    `UPDATE rounds SET rolled_at=now(), d1=$1, d2=$2, d3=$3, result=$4, pot_snapshot=$5 WHERE id=$6`,
    [d1,d2,d3,result,potSnapshot,roundId]
  );

  // settle bets
  const betsRes = await query(`SELECT * FROM bets WHERE round_id=$1`, [roundId]);
  const bets = betsRes.rows;
  let winners = [];
  let totalHouseGain = 0;
  for (const b of bets) {
    if (b.choice === result) {
      // winner: payout = floor(amount * PAYOUT_MULTIPLIER)
      const payout = Math.floor(b.amount * PAYOUT_MULTIPLIER);
      const fee = Math.floor((payout - b.amount) * HOUSE_FEE_PERCENT / (PAYOUT_MULTIPLIER - 1)); // approximate 0.3 share into pot
      // simpler: take 30% of winning edge to house: edge = payout - amount; toHouse = floor(edge * 0.3)
      const edge = payout - b.amount;
      const toHouse = Math.floor(edge * 0.3);
      const finalPayout = payout - toHouse;
      await query(`UPDATE bets SET payout=$1, won=true WHERE id=$2`, [finalPayout, b.id]);
      await query(`UPDATE users SET balance = balance + $1 WHERE id=$2`, [finalPayout, b.user_id]);
      winners.push({ user_id: b.user_id, amount: b.amount, payout: finalPayout });
      // add to house pot
      await query(`UPDATE house SET balance = balance + $1 WHERE id=1`, [toHouse]);
      totalHouseGain += toHouse;
    } else {
      // loser: move amount to house
      await query(`UPDATE bets SET won=false WHERE id=$1`, [b.id]);
      await query(`UPDATE house SET balance = balance + $1 WHERE id=1`, [b.amount]);
      totalHouseGain += b.amount;
    }
  }

  // if triple 1 or triple 6 -> distribute house pot to winners
  if ((d1===1 && d2===1 && d3===1) || (d1===6 && d2===6 && d3===6)) {
    const potRes = await query(`SELECT balance FROM house WHERE id=1`);
    const pot = (potRes.rows[0] && potRes.rows[0].balance) || 0;
    if (pot > 0 && winners.length > 0) {
      const share = Math.floor(pot / winners.length);
      for (const w of winners) {
        await query(`UPDATE users SET balance = balance + $1 WHERE id=$2`, [share, w.user_id]);
      }
      // zero the pot
      await query(`UPDATE house SET balance = 0 WHERE id=1`);
    }
  }

  // update stats (win streaks)
  for (const b of bets) {
    if (b.choice === result) {
      await query(
        `UPDATE stats SET win_streak = win_streak + 1, max_win_streak = GREATEST(max_win_streak, win_streak + 1), total_wins = total_wins + 1 WHERE user_id = $1`,
        [b.user_id]
      );
    } else {
      await query(`UPDATE stats SET win_streak = 0, total_losses = total_losses + 1 WHERE user_id = $1`, [b.user_id]);
    }
  }

  return { roundId, d1, d2, d3, result, winners, totalHouseGain };
}

// create bet
async function placeBet(userId, displayName, choice, amount, roundId) {
  // validation
  if (amount <= 0) throw new Error('Invalid amount');
  // ensure user exists
  await ensureUser(userId, displayName);
  // check balance
  const res = await query(`SELECT balance, bonus_used FROM users WHERE id=$1`, [userId]);
  const row = res.rows[0];
  if (!row) throw new Error('User not found');
  if (row.balance < amount) throw new Error('Insufficient balance');
  // check bonus constraints: if user has bonus_unused? we track bonus_used flag
  // deduct immediately
  await query(`UPDATE users SET balance = balance - $1 WHERE id=$2`, [amount, userId]);
  // insert bet
  await query(`INSERT INTO bets (user_id, round_id, choice, amount) VALUES ($1,$2,$3,$4)`, [userId, roundId, choice, amount]);
  return true;
}

// admin check
function isAdmin(id) {
  return ADMIN_IDS.includes(Number(id));
}

// helper: obfuscate id for announcements e.g. show first 2 and last 3 digits
function obfuscateId(id) {
  const s = String(id);
  if (s.length <= 5) return s.replace(/.(?=...)/g, '*');
  const first2 = s.slice(0,2);
  const last3 = s.slice(-3);
  return `${first2}***${last3}`;
}

// commands and flows
bot.start(async (ctx) => {
  const id = ctx.from.id;
  await ensureUser(id, `${ctx.from.first_name || ''} ${ctx.from.last_name || ''}`);
  // give bonus if first time
  const u = await query(`SELECT bonus_used, balance FROM users WHERE id=$1`, [id]);
  if (u.rows.length) {
    if (!u.rows[0].bonus_used) {
      await query(`UPDATE users SET balance = balance + $1, bonus_used = true WHERE id=$2`, [BONUS_AMOUNT, id]);
      await ctx.reply(`Chúc mừng bạn được tặng ${BONUS_AMOUNT}₫ lần đầu tham gia (cược tối đa ${BONUS_MAX_BET}₫ khi dùng tiền thưởng).`);
    } else {
      await ctx.reply('Bạn đã đăng ký rồi.');
    }
  } else {
    await ctx.reply('Đã tạo tài khoản. Hãy sử dụng trong nhóm để đặt cược.');
  }
});

// handle group-only betting commands like /T1000 or /X1000
bot.hears(/^[\/\\]([TtXx])(\d+)$/, async (ctx) => {
  try {
    // must be in group
    if (!ctx.chat || ctx.chat.type === 'private') {
      return ctx.reply('Lệnh chỉ dùng trong nhóm nơi bot hoạt động.');
    }
    if (GROUP_ID && Number(ctx.chat.id) !== GROUP_ID) {
      return ctx.reply('Bot đang chỉ hoạt động ở nhóm được cấu hình.');
    }
    const choiceChar = ctx.match[1].toUpperCase();
    const amount = parseInt(ctx.match[2], 10);
    const choice = (choiceChar === 'T') ? 'TAI' : 'XIU';
    const userId = ctx.from.id;
    await ensureUser(userId, `${ctx.from.first_name || ''}`);
    // check if user on bonus and betting beyond max
    const userRow = await query(`SELECT bonus_used, balance FROM users WHERE id=$1`, [userId]);
    if (userRow.rows.length) {
      const row = userRow.rows[0];
      // if balance includes bonus we don't know original, but we track bonus_used boolean; if they have bonus_used=true means they used bonus; the restriction is only "when using bonus you can only bet up to 1k" - since implementing full trace is complex, we do a safer route:
      // if their balance <= BONUS_AMOUNT and bonus_used was just set earlier, restrict to BONUS_MAX_BET
      if (!row.bonus_used && amount > BONUS_MAX_BET) {
        return ctx.reply(`Bạn chỉ được cược tối đa ${BONUS_MAX_BET}₫ khi dùng tiền thưởng lần đầu.`);
      }
    }
    // get open round (create if none)
    let round = await getOpenRound();
    if (!round) round = await createRound();
    // place bet
    try {
      await placeBet(userId, ctx.from.username || ctx.from.first_name || '', choice, amount, round.id);
      await ctx.replyWithHTML(`${ctx.from.first_name || 'Bạn'} đã cược <b>${amount}₫</b> cho <b>${choice}</b> ở phiên #${round.id}`);
    } catch (err) {
      return ctx.reply(`Đặt cược thất bại: ${err.message}`);
    }
  } catch (err) {
    console.error('bet handler err', err);
    ctx.reply('Lỗi khi xử lý đặt cược.');
  }
});

// admin: setresult in group or private: /setresult 2 3 4 (d1 d2 d3)
bot.command('setresult', async (ctx) => {
  const from = ctx.from.id;
  if (!isAdmin(from)) return ctx.reply('Chỉ admin mới dùng lệnh này.');
  const parts = ctx.message.text.trim().split(/\s+/);
  if (parts.length !== 4) return ctx.reply('Cách dùng: /setresult d1 d2 d3 (ví dụ: /setresult 1 2 3)');
  const d1 = Number(parts[1]), d2 = Number(parts[2]), d3 = Number(parts[3]);
  if (![d1,d2,d3].every(n=>n>=1 && n<=6)) return ctx.reply('Giá trị xúc xắc phải từ 1 đến 6');
  // get open round
  let round = await getOpenRound();
  if (!round) round = await createRound();
  // roll with override
  const res = await rollRound({d1,d2,d3});
  // post result into group
  const seq = `🎲 Phiên #${res.roundId} kết quả: ${res.d1} ${res.d2} ${res.d3} → ${res.result}`;
  if (GROUP_ID) {
    await bot.telegram.sendMessage(GROUP_ID, seq);
  } else {
    ctx.reply(seq);
  }
});

// admin: view top 10 win streak
bot.command('top10', async (ctx) => {
  const res = await query(`SELECT user_id, max_win_streak FROM stats ORDER BY max_win_streak DESC LIMIT 10`);
  let txt = '🏆 Top 10 chuỗi thắng:\n';
  for (const r of res.rows) {
    txt += `${obfuscateId(r.user_id)} — ${r.max_win_streak}\n`;
  }
  ctx.reply(txt);
});

// withdraw command in group or private: /ruttien 500000
bot.hears(/^[\/\\]ruttien\s+(\d+)$/i, async (ctx) => {
  try {
    const amount = Number(ctx.match[1]);
    if (amount < WITHDRAW_MIN) return ctx.reply(`Rút tối thiểu ${WITHDRAW_MIN}₫`);
    const userId = ctx.from.id;
    await ensureUser(userId, ctx.from.username || ctx.from.first_name || '');
    const balRes = await query(`SELECT balance FROM users WHERE id=$1`, [userId]);
    const bal = (balRes.rows[0] && balRes.rows[0].balance) || 0;
    if (bal < amount) return ctx.reply('Số dư không đủ để rút.');
    // create withdraw request
    await query(`INSERT INTO withdraws (user_id, amount) VALUES ($1,$2)`, [userId, amount]);
    // notify admins with approve/reject buttons
    for (const adminId of ADMIN_IDS) {
      try {
        await bot.telegram.sendMessage(adminId, `🔔 Yêu cầu rút tiền: ${obfuscateId(userId)} — ${amount}₫\nReply Approve hoặc Reject below.`, Markup.inlineKeyboard([
          Markup.button.callback('Approve', `approve_withdraw:${userId}:${amount}`),
          Markup.button.callback('Reject', `reject_withdraw:${userId}:${amount}`)
        ]));
      } catch (e) {
        console.warn('Notify admin fail', adminId, e.message);
      }
    }
    // obfuscate announcement in group
    if (GROUP_ID) {
      await bot.telegram.sendMessage(GROUP_ID, `ℹ️ Yêu cầu rút: ${obfuscateId(userId)} — ${String(amount).replace(/\B(?=(\d{3})+(?!\d))/g, ".")}₫ (chỉ hiển thị một phần ID)`);
    }
    ctx.reply('Yêu cầu rút đã gửi đến admin. Vui lòng chờ duyệt.');
  } catch (err) {
    console.error(err);
    ctx.reply('Lỗi gửi yêu cầu rút.');
  }
});

// handle callback for admin approve/reject
bot.on('callback_query', async (ctx) => {
  try {
    const data = ctx.callbackQuery.data;
    const from = ctx.from.id;
    if (!isAdmin(from)) return ctx.answerCbQuery('Chỉ admin được bấm.');
    if (data.startsWith('approve_withdraw:') || data.startsWith('reject_withdraw:')) {
      const parts = data.split(':'); // [action, userId, amount]
      const actionType = parts[0].split('_')[0]; // approve or reject
      const userId = Number(parts[1]);
      const amount = Number(parts[2]);
      // find pending withdraw
      const wres = await query(`SELECT * FROM withdraws WHERE user_id=$1 AND amount=$2 AND status='PENDING' ORDER BY created_at DESC LIMIT 1`, [userId, amount]);
      if (wres.rows.length === 0) {
        await ctx.editMessageText('Không tìm thấy yêu cầu hoặc đã xử lý rồi.');
        return ctx.answerCbQuery();
      }
      const wid = wres.rows[0].id;
      if (actionType === 'approve') {
        // mark approved and deduct user balance
        await query(`UPDATE withdraws SET status='APPROVED', handled_by=$1, handled_at=now() WHERE id=$2`, [from, wid]);
        await query(`UPDATE users SET balance = balance - $1 WHERE id=$2`, [amount, userId]);
        // notify user
        try {
          await bot.telegram.sendMessage(userId, `✅ Yêu cầu rút ${amount}₫ đã được admin chấp thuận. Vui lòng kiểm tra và admin sẽ chuyển tiền.`);
        } catch (e) {
          console.warn('notify user fail', e.message);
        }
        await ctx.editMessageText(`Đã phê duyệt rút ${amount}₫ của ${obfuscateId(userId)} bởi admin ${from}`);
      } else {
        await query(`UPDATE withdraws SET status='REJECTED', handled_by=$1, handled_at=now() WHERE id=$2`, [from, wid]);
        try {
          await bot.telegram.sendMessage(userId, `❌ Yêu cầu rút ${amount}₫ đã bị từ chối bởi admin.`);
        } catch (e) { /* ignore */ }
        await ctx.editMessageText(`Đã từ chối rút ${amount}₫ của ${obfuscateId(userId)} bởi admin ${from}`);
      }
      return ctx.answerCbQuery();
    }
    return ctx.answerCbQuery();
  } catch (err) {
    console.error('callback err', err);
    ctx.answerCbQuery('Lỗi xử lý.');
  }
});

// deposit request: /naptien 500000
bot.hears(/^[\/\\]naptien\s+(\d+)$/i, async (ctx) => {
  try {
    const amount = Number(ctx.match[1]);
    const userId = ctx.from.id;
    await ensureUser(userId, ctx.from.username || ctx.from.first_name || '');
    // create deposit pending
    await query(`INSERT INTO deposits (user_id, amount) VALUES ($1,$2)`, [userId, amount]);
    // notify admin to confirm and send qr or tin nhan
    for (const adminId of ADMIN_IDS) {
      try {
        await bot.telegram.sendMessage(adminId, `🔔 Yêu cầu nạp: ${obfuscateId(userId)} — ${amount}₫`, Markup.inlineKeyboard([
          Markup.button.callback('Confirm', `confirm_deposit:${userId}:${amount}`),
          Markup.button.callback('RejectDep', `reject_deposit:${userId}:${amount}`)
        ]));
      } catch (e) { console.warn(e.message); }
    }
    if (GROUP_ID) {
      await bot.telegram.sendMessage(GROUP_ID, `ℹ️ Yêu cầu nạp: ${obfuscateId(userId)} — ${String(amount).replace(/\B(?=(\d{3})+(?!\d))/g, ".")}₫`);
    }
    ctx.reply('Yêu cầu nạp đã gửi admin. Admin sẽ gửi mã QR hoặc hướng dẫn, sau đó xác nhận.');
  } catch (err) {
    console.error(err);
    ctx.reply('Lỗi gửi yêu cầu nạp.');
  }
});

// handle deposit confirm/reject
bot.on('callback_query', async (ctx) => {
  // handled above for withdraws; add handling for deposits
  try {
    const data = ctx.callbackQuery.data;
    const from = ctx.from.id;
    if (!isAdmin(from)) return ctx.answerCbQuery('Chỉ admin được bấm.');
    if (data.startsWith('confirm_deposit:') || data.startsWith('reject_deposit:')) {
      const parts = data.split(':');
      const action = parts[0].startsWith('confirm') ? 'CONFIRMED' : 'REJECTED';
      const userId = Number(parts[1]);
      const amount = Number(parts[2]);
      const dres = await query(`SELECT * FROM deposits WHERE user_id=$1 AND amount=$2 AND status='PENDING' ORDER BY created_at DESC LIMIT 1`, [userId, amount]);
      if (dres.rows.length === 0) {
        await ctx.editMessageText('Không tìm thấy yêu cầu nạp còn chờ.');
        return ctx.answerCbQuery();
      }
      const did = dres.rows[0].id;
      if (action === 'CONFIRMED') {
        await query(`UPDATE deposits SET status='CONFIRMED', handled_by=$1, handled_at=now() WHERE id=$2`, [from, did]);
        // credit user's balance
        await query(`UPDATE users SET balance = balance + $1 WHERE id=$2`, [amount, userId]);
        try {
          await bot.telegram.sendMessage(userId, `✅ Nạp ${amount}₫ đã được admin xác nhận.`);
        } catch (e) {}
        await ctx.editMessageText(`Xác nhận nạp ${amount}₫ cho ${obfuscateId(userId)} bởi admin ${from}`);
      } else {
        await query(`UPDATE deposits SET status='REJECTED', handled_by=$1, handled_at=now() WHERE id=$2`, [from, did]);
        try {
          await bot.telegram.sendMessage(userId, `❌ Yêu cầu nạp ${amount}₫ đã bị từ chối.`);
        } catch (e) {}
        await ctx.editMessageText(`Từ chối nạp ${amount}₫ cho ${obfuscateId(userId)} bởi admin ${from}`);
      }
      return ctx.answerCbQuery();
    }
  } catch (err) {
    console.error(err);
    ctx.answerCbQuery('Lỗi xử lý deposit');
  }
});

// animate dice in group and then show result
async function animateAndRoll(roundId, override) {
  try {
    const message = await bot.telegram.sendMessage(GROUP_ID, `🎲 Phiên #${roundId} — Đang tung xúc xắc...`);
    // animation: three emoji updates (quick)
    const frames = [
      '🎲 🎲 🎲',
      '⬛ 🎲 🎲',
      '⬛ ⬛ 🎲',
      '🎲 🎲 🎲'
    ];
    for (let i=0;i<3;i++){
      await bot.telegram.editMessageText(GROUP_ID, message.message_id, null, frames[i%frames.length] + `  (lần ${i+1})`);
      await new Promise(r=>setTimeout(r, 300)); // 300ms
    }
    // now roll
    const res = await rollRound(override);
    const txt = `🎲 Phiên #${res.roundId} kết quả: ${res.d1} - ${res.d2} - ${res.d3}\nKết luận: ${res.result}\nNgười thắng: ${res.winners.length} người.`;
    await bot.telegram.editMessageText(GROUP_ID, message.message_id, null, txt);
  } catch (err) {
    console.error('animateAndRoll err', err);
    notifyAdmins(`Bot gặp lỗi khi tung xúc xắc: ${err.message}`);
  }
}

// notify admins
async function notifyAdmins(msg) {
  for (const a of ADMIN_IDS) {
    try {
      await bot.telegram.sendMessage(a, `⚠️ ${msg}`);
    } catch (e) {
      console.warn('notify admin fail', a, e.message);
    }
  }
}

// main loop: ensure we always roll every ROUND_INTERVAL seconds
let lastRollTs = 0;
async function mainLoop() {
  try {
    const open = await getOpenRound();
    if (!open) {
      const r = await createRound();
      // animate + roll it
      await animateAndRoll(r.id);
      lastRollTs = Date.now();
    } else {
      // if open round started more than ROUND_INTERVAL seconds ago and not rolled -> roll it
      const started = new Date(open.started_at).getTime();
      const now = Date.now();
      if ((now - started) / 1000 >= ROUND_INTERVAL) {
        await animateAndRoll(open.id);
        lastRollTs = Date.now();
        // create next round immediately
        await createRound();
      }
    }
  } catch (err) {
    console.error('mainLoop err', err);
    await notifyAdmins(`Lỗi chính trong vòng: ${err.message}`);
  }
}

// recovery watch: if last roll older than 90s beyond interval -> notify admin
async function crashWatcher() {
  const now = Date.now();
  if (lastRollTs === 0) return;
  const allowed = (ROUND_INTERVAL + 30) * 1000; // buffer
  if (now - lastRollTs > allowed) {
    await notifyAdmins('Phát hiện bot có thể đã crash hoặc chậm: chưa gửi kết quả đúng giờ.');
  }
}

// start bot and schedule
(async () => {
  try {
    await bot.launch();
    console.log('Bot started');
    // create initial round if none
    const open = await getOpenRound();
    if (!open) await createRound();
    // schedule main loop every 5s check (actual roll controlled by timestamps)
    setInterval(mainLoop, 5000);
    setInterval(crashWatcher, 30000);
    // graceful
    process.once('SIGINT', () => bot.stop('SIGINT'));
    process.once('SIGTERM', () => bot.stop('SIGTERM'));
  } catch (err) {
    console.error('Failed to launch bot', err);
    await notifyAdmins(`Bot không thể khởi động: ${err.message}`);
    process.exit(1);
  }
})();
