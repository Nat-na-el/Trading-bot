import sqlite3
import logging
import html
import os
import asyncio
from datetime import datetime
from telegram import Update, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

# ========================= CONFIG =========================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("No TOKEN environment variable set")

GROUP_CHAT_ID = -1003897036924
RULES_TOPIC_ID = 18
TRADE_TOPIC_ID = 25
VIOLATION_TOPIC_ID = 69
BALANCE_TOPIC_ID = 76
DB_NAME = "trading_bot.db"

# States
RULE_MAX_RISK, RULE_MAX_DAILY_LOSS, RULE_MIN_RR, RULE_REQUIRE_SL, RULE_ALLOWED_PAIRS, RULE_CONFIRM = range(6)
TRADE_PAIR, TRADE_TYPE, TRADE_ENTRY, TRADE_SL, TRADE_TP, TRADE_RISK, TRADE_POSITION_NUM, TRADE_LOT_SIZE, TRADE_TV_SCREENSHOT, TRADE_MT5_SCREENSHOT, TRADE_CONFIRM = range(11)
CLOSE_TRADE_ID, CLOSE_EXIT_PRICE, CLOSE_TV_RESULT, CLOSE_MT5_CLOSED, CLOSE_CONFIRM = range(5)
ACCOUNT_BALANCE = range(1)

# ========================= DATABASE =========================
def get_db():
    return sqlite3.connect(DB_NAME)

def migrate_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("PRAGMA table_info(Traders)")
    columns = [col[1] for col in c.fetchall()]
    if 'max_daily_loss' not in columns:
        c.execute("ALTER TABLE Traders ADD COLUMN max_daily_loss REAL")
    if 'account_balance' not in columns:
        c.execute("ALTER TABLE Traders ADD COLUMN account_balance REAL DEFAULT 10000")
    if 'current_daily_loss' not in columns:
        c.execute("ALTER TABLE Traders ADD COLUMN current_daily_loss REAL DEFAULT 0")
    if 'last_loss_reset_date' not in columns:
        c.execute("ALTER TABLE Traders ADD COLUMN last_loss_reset_date TEXT")
    if 'rules_message_id' not in columns:
        c.execute("ALTER TABLE Traders ADD COLUMN rules_message_id INTEGER")

    c.execute("PRAGMA table_info(Trades)")
    columns = [col[1] for col in c.fetchall()]
    if 'pl_monetary' not in columns:
        c.execute("ALTER TABLE Trades ADD COLUMN pl_monetary REAL")
    if 'balance_after' not in columns:
        c.execute("ALTER TABLE Trades ADD COLUMN balance_after REAL")
    if 'position_number' not in columns:
        c.execute("ALTER TABLE Trades ADD COLUMN position_number INTEGER")
    if 'lot_size' not in columns:
        c.execute("ALTER TABLE Trades ADD COLUMN lot_size REAL")

    c.execute('''CREATE TABLE IF NOT EXISTS Violations (
        violation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER,
        trader_id INTEGER,
        violation_type TEXT,
        message_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS Traders (
        trader_id INTEGER PRIMARY KEY,
        trader_name TEXT,
        max_risk REAL,
        max_daily_loss REAL,
        min_rr REAL,
        require_sl INTEGER,
        allowed_pairs TEXT,
        rules_message_id INTEGER,
        account_balance REAL DEFAULT 10000,
        current_daily_loss REAL DEFAULT 0,
        last_loss_reset_date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS Trades (
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
        trader_id INTEGER,
        pair TEXT,
        type TEXT,
        entry REAL,
        sl REAL,
        tp REAL,
        risk REAL,
        leverage INTEGER DEFAULT 1,
        position_number INTEGER,
        lot_size REAL,
        screenshot_tv TEXT,
        screenshot_mt5 TEXT,
        status TEXT DEFAULT 'Open',
        rule_violation TEXT,
        exit_price REAL,
        pl_percent REAL,
        pl_monetary REAL,
        rr_achieved REAL,
        balance_after REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS Violations (
        violation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER,
        trader_id INTEGER,
        violation_type TEXT,
        message_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()
    migrate_db()

# ========================= DATABASE HELPERS =========================
def save_trader(trader_id, trader_name, max_risk, max_daily_loss, min_rr, require_sl, allowed_pairs, rules_message_id=None):
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO Traders
        (trader_id, trader_name, max_risk, max_daily_loss, min_rr, require_sl, allowed_pairs, rules_message_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (trader_id, trader_name, max_risk, max_daily_loss, min_rr, require_sl, allowed_pairs, rules_message_id))
    conn.commit()
    conn.close()

def get_trader_rules(trader_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM Traders WHERE trader_id = ?", (trader_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    # Get column names
    col_names = [description[0] for description in c.description]
    row_dict = dict(zip(col_names, row))
    return {
        "trader_name": row_dict["trader_name"],
        "max_risk": row_dict["max_risk"],
        "max_daily_loss": row_dict["max_daily_loss"],
        "min_rr": row_dict["min_rr"],
        "require_sl": bool(row_dict["require_sl"]),
        "allowed_pairs": [p.strip().upper() for p in (row_dict["allowed_pairs"] or "").split(",") if p.strip()],
        "rules_message_id": row_dict["rules_message_id"],
        "account_balance": row_dict.get("account_balance", 10000),
        "current_daily_loss": row_dict.get("current_daily_loss", 0),
        "last_loss_reset_date": row_dict.get("last_loss_reset_date")
    }

def log_new_trade(trader_id, pair, trade_type, entry, sl, tp, risk, position_num, lot_size, tv_file_id, mt5_file_id, rule_violation):
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO Trades
        (trader_id, pair, type, entry, sl, tp, risk, position_number, lot_size, screenshot_tv, screenshot_mt5, rule_violation, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Open')
    ''', (trader_id, pair, trade_type, entry, sl, tp, risk, position_num, lot_size, tv_file_id, mt5_file_id, rule_violation))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    return trade_id

def close_trade_in_db(trade_id, trader_id, exit_price, rr_achieved, pl_percent, pl_monetary, balance_after):
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        UPDATE Trades
        SET status = 'Closed', exit_price = ?, rr_achieved = ?, pl_percent = ?, pl_monetary = ?, balance_after = ?
        WHERE trade_id = ? AND trader_id = ?
    ''', (exit_price, rr_achieved, pl_percent, pl_monetary, balance_after, trade_id, trader_id))
    conn.commit()
    conn.close()

def get_open_trade_count(trader_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM Trades WHERE trader_id = ? AND status = 'Open'", (trader_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def get_trade(trade_id, trader_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM Trades WHERE trade_id = ? AND trader_id = ?", (trade_id, trader_id))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    # Get column names
    col_names = [description[0] for description in c.description]
    d = dict(zip(col_names, row))
    rules = get_trader_rules(trader_id)
    d["trader_name"] = rules["trader_name"] if rules else "Unknown"
    return d

def get_user_trades(trader_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM Trades WHERE trader_id = ? ORDER BY trade_id DESC LIMIT 20", (trader_id,))
    rows = c.fetchall()
    col_names = [description[0] for description in c.description]
    trades = []
    for row in rows:
        trades.append(dict(zip(col_names, row)))
    conn.close()
    return trades

def get_user_violations(trader_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT trade_id, pair, rule_violation FROM Trades WHERE trader_id = ? AND rule_violation IS NOT NULL ORDER BY trade_id DESC", (trader_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def update_account_balance(trader_id, new_balance):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE Traders SET account_balance = ? WHERE trader_id = ?", (new_balance, trader_id))
    conn.commit()
    conn.close()

def get_account_balance(trader_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT account_balance FROM Traders WHERE trader_id = ?", (trader_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 10000

def update_daily_loss(trader_id, loss_amount):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE Traders SET current_daily_loss = current_daily_loss + ? WHERE trader_id = ?", (loss_amount, trader_id))
    conn.commit()
    conn.close()

def reset_daily_loss_if_needed(trader_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT current_daily_loss, last_loss_reset_date FROM Traders WHERE trader_id = ?", (trader_id,))
    row = c.fetchone()
    if row:
        daily_loss, last_reset = row
        today = datetime.now().strftime("%Y-%m-%d")
        if last_reset != today:
            c.execute("UPDATE Traders SET current_daily_loss = 0, last_loss_reset_date = ? WHERE trader_id = ?", (today, trader_id))
            daily_loss = 0
    conn.commit()
    conn.close()
    return daily_loss if row else 0

# ========================= HELPERS =========================
def calculate_rr(trade_type, entry, stop, target):
    trade_type = trade_type.upper()
    if trade_type == "BUY":
        risk_dist = entry - stop
        reward = target - entry
    else:
        risk_dist = stop - entry
        reward = entry - target
    return reward / risk_dist if risk_dist > 0 else 0.0

def calculate_achieved_rr(trade_type, entry, sl, exit_price):
    trade_type = trade_type.upper()
    if trade_type == "BUY":
        risk_dist = entry - sl
        reward = exit_price - entry
    else:
        risk_dist = sl - entry
        reward = entry - exit_price
    return reward / risk_dist if risk_dist > 0 else 0.0

def normalize_allowed_pairs(raw):
    cleaned = raw.replace(",", " ")
    pairs = [p.strip().upper() for p in cleaned.split() if p.strip()]
    return ",".join(pairs)

async def send_trade_post(context, caption, tv_file_id, mt5_file_id, thread_id):
    try:
        media = [
            InputMediaPhoto(media=tv_file_id, caption=caption, parse_mode=ParseMode.HTML),
            InputMediaPhoto(media=mt5_file_id, parse_mode=ParseMode.HTML)
        ]
        await context.bot.send_media_group(
            chat_id=GROUP_CHAT_ID,
            media=media,
            message_thread_id=thread_id
        )
    except Exception as e:
        logging.error(f"Media post failed: {e}")

async def send_balance_update(context, trader_name, old_balance, new_balance, change_percent, change_amount, trade_id=None):
    if change_amount >= 0:
        emoji = "📈"
        change_str = f"+{change_amount:+,.2f} (+{change_percent:+.2f}%)"
    else:
        emoji = "📉"
        change_str = f"{change_amount:+,.2f} ({change_percent:+.2f}%)"

    text = f"""
{emoji} <b>BALANCE UPDATE</b>
Trader: {html.escape(trader_name)}
Old Balance: <b>${old_balance:,.2f}</b>
New Balance: <b>${new_balance:,.2f}</b>
Change: <b>{change_str}</b>
{f"Trade #{trade_id}" if trade_id else ""}
    """.strip()

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            message_thread_id=BALANCE_TOPIC_ID,
            text=text,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logging.error(f"Failed to send balance update: {e}")

async def send_rules_post(context, trader_name, rules, message_thread_id=RULES_TOPIC_ID):
    text = f"""
📋 <b>TRADING RULES – {html.escape(trader_name)}</b>
• Max risk per trade: <b>{rules['max_risk']}%</b>
• Max daily loss: <b>{rules['max_daily_loss']}%</b>
• Minimum risk-reward: <b>1:{rules['min_rr']}</b>
• Stop loss required: <b>{'YES' if rules['require_sl'] else 'NO'}</b>
• Allowed pairs: <b>{', '.join(rules['allowed_pairs'])}</b>
• Leverage: <b>1x (fixed)</b>
<i>Rules set on {datetime.now().strftime('%Y-%m-%d %H:%M')}</i>
    """.strip()
    try:
        msg = await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            message_thread_id=message_thread_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        return msg.message_id
    except Exception as e:
        logging.error(f"Failed to post rules: {e}")
        return None

async def delete_previous_rules_post(context, message_id):
    if message_id:
        try:
            await context.bot.delete_message(chat_id=GROUP_CHAT_ID, message_id=message_id)
        except BadRequest as e:
            logging.warning(f"Could not delete rules message {message_id}: {e}")

async def post_violation(context, trade_id, trader_id, trader_name, violation_text, screenshot_file_id=None):
    caption = f"""
⚠️ <b>RULE VIOLATION DETECTED</b>
Trade #{trade_id}
Trader: {html.escape(trader_name)}
Violation: {violation_text}
Please review and take necessary action.
    """.strip()
    try:
        if screenshot_file_id:
            msg = await context.bot.send_photo(
                chat_id=GROUP_CHAT_ID,
                photo=screenshot_file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                message_thread_id=VIOLATION_TOPIC_ID
            )
        else:
            msg = await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
                message_thread_id=VIOLATION_TOPIC_ID
            )
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO Violations (trade_id, trader_id, violation_type, message_id)
            VALUES (?, ?, ?, ?)
        ''', (trade_id, trader_id, violation_text, msg.message_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Failed to post violation: {e}")

def is_private(update):
    return update.effective_chat.type == "private"

async def show_menu(update: Update):
    menu = """
✅ <b>Done!</b>
What next?
• /trade → Open new trade
• /close → Close trade
• /mytrades → Your history
• /balance → View current balance
• /violations → Violations
• /setrules → Update rules
• /setbalance → Update account balance
Type any command 👇
    """.strip()
    await update.message.reply_text(menu, parse_mode=ParseMode.HTML)

# ========================= COMMANDS =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
Hey trader 👋 Welcome to the 10k accountability bot!
Commands:
• /setrules → Set or update your rules
• /setbalance → Set your account balance
• /trade → Log new trade (2 screenshots)
• /close → Close a trade (2 screenshots)
• /mytrades → Your trade history
• /balance → View your current balance
• /violations → Your rule violations
Start with /setrules or /trade
Let's grow that 10k together 🚀
    """.strip()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ── SET RULES ─────────────────────────────────────────────
async def start_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        await update.message.reply_text("Use /setrules in private.")
        return ConversationHandler.END
    trader_id = update.effective_user.id
    existing = get_trader_rules(trader_id)
    if existing:
        context.user_data['is_update'] = True
        msg = f"<b>Update Rules</b>\n\n1/5 Max risk per trade (%)? (current: {existing['max_risk']}%)"
    else:
        context.user_data['is_update'] = False
        msg = "<b>Set Rules</b>\n\n1/5 Max risk per trade (%)?"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    return RULE_MAX_RISK

async def get_max_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['max_risk'] = float(update.message.text.strip())
        await update.message.reply_text(f"<b>2/5</b> Max daily loss (%)?", parse_mode=ParseMode.HTML)
        return RULE_MAX_DAILY_LOSS
    except ValueError:
        await update.message.reply_text("Enter a number.")
        return RULE_MAX_RISK

async def get_max_daily_loss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['max_daily_loss'] = float(update.message.text.strip())
        await update.message.reply_text(f"<b>3/5</b> Minimum R:R ratio? (e.g., 1.5 for 1:1.5)", parse_mode=ParseMode.HTML)
        return RULE_MIN_RR
    except ValueError:
        await update.message.reply_text("Enter a number.")
        return RULE_MAX_DAILY_LOSS

async def get_min_rr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['min_rr'] = float(update.message.text.strip())
        await update.message.reply_text(f"<b>4/5</b> Stop loss required? (1 = Yes, 0 = No)", parse_mode=ParseMode.HTML)
        return RULE_REQUIRE_SL
    except ValueError:
        await update.message.reply_text("Enter a number.")
        return RULE_MIN_RR

async def get_require_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = int(update.message.text.strip())
        context.user_data['require_sl'] = 1 if val == 1 else 0
        await update.message.reply_text(f"<b>5/5</b> Allowed pairs? (space/comma separated)", parse_mode=ParseMode.HTML)
        return RULE_ALLOWED_PAIRS
    except ValueError:
        await update.message.reply_text("Reply 1 or 0.")
        return RULE_REQUIRE_SL

async def get_allowed_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    normalized = normalize_allowed_pairs(update.message.text)
    context.user_data['allowed_pairs'] = normalized
    ud = context.user_data
    is_update = ud.get('is_update', False)
    summary = f"""
<b>{'Update' if is_update else 'New'} Rules Summary</b>
• Max risk per trade: <b>{ud['max_risk']}%</b>
• Max daily loss: <b>{ud['max_daily_loss']}%</b>
• Minimum R:R: <b>1:{ud['min_rr']}</b>
• Stop loss required: <b>{'YES' if ud['require_sl'] else 'NO'}</b>
• Allowed pairs: <b>{normalized.replace(',', ', ')}</b>
• Leverage: <b>1x (fixed)</b>
Reply <b>YES</b> to save or <b>NO</b> to cancel
    """.strip()
    await update.message.reply_text(summary, parse_mode=ParseMode.HTML)
    return RULE_CONFIRM

async def confirm_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().upper() not in ["YES", "Y"]:
        await update.message.reply_text("Cancelled.")
        context.user_data.clear()
        return ConversationHandler.END
    user = update.effective_user
    ud = context.user_data
    old_rules = get_trader_rules(user.id)
    old_message_id = old_rules['rules_message_id'] if old_rules else None
    save_trader(
        user.id, user.full_name,
        ud['max_risk'], ud['max_daily_loss'], ud['min_rr'],
        ud['require_sl'], ud['allowed_pairs'], None
    )
    new_rules = get_trader_rules(user.id)
    new_message_id = await send_rules_post(context, user.full_name, new_rules)
    if new_message_id:
        save_trader(
            user.id, user.full_name,
            ud['max_risk'], ud['max_daily_loss'], ud['min_rr'],
            ud['require_sl'], ud['allowed_pairs'], new_message_id
        )
        if old_message_id:
            await delete_previous_rules_post(context, old_message_id)
    status = "updated" if ud.get('is_update') else "added"
    await update.message.reply_text(f"Rules {status} successfully!", parse_mode=ParseMode.HTML)
    await show_menu(update)
    context.user_data.clear()
    return ConversationHandler.END

# ── SET BALANCE ───────────────────────────────────────────
async def start_setbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        await update.message.reply_text("Use in private chat.")
        return ConversationHandler.END
    await update.message.reply_text("Enter your current account balance (e.g., 10000):")
    return ACCOUNT_BALANCE

async def get_account_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = float(update.message.text.strip())
        trader_id = update.effective_user.id
        update_account_balance(trader_id, balance)
        await update.message.reply_text(f"Account balance set to ${balance:,.2f}")
        await show_menu(update)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return ACCOUNT_BALANCE

# ── VIEW BALANCE ──────────────────────────────────────────
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        await update.message.reply_text("Use in private chat.")
        return
    trader_id = update.effective_user.id
    balance = get_account_balance(trader_id)
    rules = get_trader_rules(trader_id)
    name = rules['trader_name'] if rules else "Trader"
    await update.message.reply_text(
        f"💰 <b>Your Balance</b>\n\n{html.escape(name)}: <b>${balance:,.2f}</b>",
        parse_mode=ParseMode.HTML
    )

# ── TRADE OPEN ────────────────────────────────────────────
async def start_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        await update.message.reply_text("Use /trade in private.")
        return ConversationHandler.END
    if not get_trader_rules(update.effective_user.id):
        await update.message.reply_text("Set rules first: /setrules")
        return ConversationHandler.END
    await update.message.reply_text("<b>🟢 New Trade</b>\n\n1/10 Pair? (e.g., EURUSD, XAUUSD)", parse_mode=ParseMode.HTML)
    return TRADE_PAIR

async def get_trade_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['pair'] = update.message.text.upper().strip()
    await update.message.reply_text("2/10 BUY or SELL?")
    return TRADE_TYPE

async def get_trade_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.upper().strip()
    if t not in ["BUY", "SELL"]:
        await update.message.reply_text("Only BUY or SELL")
        return TRADE_TYPE
    context.user_data['type'] = t
    await update.message.reply_text("3/10 Entry price?")
    return TRADE_ENTRY

async def get_trade_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['entry'] = float(update.message.text)
        await update.message.reply_text("4/10 SL price?")
        return TRADE_SL
    except ValueError:
        await update.message.reply_text("Number please.")
        return TRADE_ENTRY

async def get_trade_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['sl'] = float(update.message.text)
        await update.message.reply_text("5/10 TP price?")
        return TRADE_TP
    except ValueError:
        await update.message.reply_text("Number please.")
        return TRADE_SL

async def get_trade_tp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['tp'] = float(update.message.text)
        await update.message.reply_text("6/10 Risk % of account?")
        return TRADE_RISK
    except ValueError:
        await update.message.reply_text("Number please.")
        return TRADE_TP

async def get_trade_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['risk'] = float(update.message.text)
        await update.message.reply_text("7/10 Position number (e.g., 1,2,3)?")
        return TRADE_POSITION_NUM
    except ValueError:
        await update.message.reply_text("Number please.")
        return TRADE_RISK

async def get_trade_position_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['position_num'] = int(update.message.text)
        await update.message.reply_text("8/10 Lot size?")
        return TRADE_LOT_SIZE
    except ValueError:
        await update.message.reply_text("Enter a whole number.")
        return TRADE_POSITION_NUM

async def get_trade_lot_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['lot_size'] = float(update.message.text)
        await update.message.reply_text("<b>9/10</b> TradingView screenshot (photo)", parse_mode=ParseMode.HTML)
        return TRADE_TV_SCREENSHOT
    except ValueError:
        await update.message.reply_text("Number please.")
        return TRADE_LOT_SIZE

async def get_tv_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Send photo")
        return TRADE_TV_SCREENSHOT
    context.user_data['tv_file_id'] = update.message.photo[-1].file_id
    await update.message.reply_text("<b>10/10</b> MT5 opened positions screenshot (photo)", parse_mode=ParseMode.HTML)
    return TRADE_MT5_SCREENSHOT

async def finish_trade_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Send MT5 screenshot")
        return TRADE_MT5_SCREENSHOT
    context.user_data['mt5_file_id'] = update.message.photo[-1].file_id

    ud = context.user_data
    trader_id = update.effective_user.id
    rules = get_trader_rules(trader_id)

    reset_daily_loss_if_needed(trader_id)

    violations = []
    if ud['pair'] not in rules['allowed_pairs']:
        violations.append("Pair not allowed")
    if rules['require_sl'] and (ud.get('sl') is None or ud['sl'] <= 0):
        violations.append("Missing SL")
    if ud['risk'] > rules['max_risk']:
        violations.append("Risk too high")
    planned_rr = calculate_rr(ud['type'], ud['entry'], ud['sl'], ud['tp'])
    if planned_rr < rules['min_rr']:
        violations.append("RR too low")
    if rules['current_daily_loss'] + ud['risk'] > rules['max_daily_loss']:
        violations.append("Max daily loss exceeded")

    violation_str = "⚠️ " + ", ".join(violations) if violations else "✅ No violations"

    preview = f"""
<b>Trade Preview – Confirm?</b>
Pair: <b>{ud['pair']}</b>
Type: <b>{ud['type']}</b>
Entry: <b>{ud['entry']}</b>
SL: <b>{ud['sl']}</b>
TP: <b>{ud['tp']}</b>
Risk: <b>{ud['risk']}%</b>
Position #: <b>{ud['position_num']}</b>
Lot size: <b>{ud['lot_size']}</b>
Lev: <b>1x (fixed)</b>
RR: <b>{planned_rr:.2f}</b>
Violations: {violation_str}
Reply <b>YES</b> to post or <b>NO</b> to cancel
    """.strip()
    await update.message.reply_text(preview, parse_mode=ParseMode.HTML)
    return TRADE_CONFIRM

async def confirm_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if text in ["CANCEL", "NO"]:
        await update.message.reply_text("Cancelled.")
        context.user_data.clear()
        return ConversationHandler.END
    if text == "EDIT":
        await update.message.reply_text("Starting over...\n\n1/10 Pair?")
        context.user_data.clear()
        return TRADE_PAIR
    if text not in ["YES", "Y"]:
        await update.message.reply_text("Reply YES, EDIT or CANCEL.")
        return TRADE_CONFIRM

    ud = context.user_data
    trader_id = update.effective_user.id
    rules = get_trader_rules(trader_id)

    violations = []
    if ud['pair'] not in rules['allowed_pairs']:
        violations.append("Pair not allowed")
    if rules['require_sl'] and (ud.get('sl') is None or ud['sl'] <= 0):
        violations.append("Missing SL")
    if ud['risk'] > rules['max_risk']:
        violations.append("Risk too high")
    planned_rr = calculate_rr(ud['type'], ud['entry'], ud['sl'], ud['tp'])
    if planned_rr < rules['min_rr']:
        violations.append("RR too low")
    reset_daily_loss_if_needed(trader_id)
    rules = get_trader_rules(trader_id)
    if rules['current_daily_loss'] + ud['risk'] > rules['max_daily_loss']:
        violations.append("Max daily loss exceeded")

    rule_violation = ", ".join(violations) if violations else None

    trade_id = log_new_trade(
        trader_id, ud['pair'], ud['type'], ud['entry'], ud['sl'], ud['tp'],
        ud['risk'], ud['position_num'], ud['lot_size'],
        ud['tv_file_id'], ud['mt5_file_id'], rule_violation
    )

    if rule_violation:
        await post_violation(context, trade_id, trader_id, rules['trader_name'], rule_violation, ud['tv_file_id'])

    violation_text = f"\n⚠️ {rule_violation}" if rule_violation else ""

    caption = f"""
🟢 <b>OPEN TRADE #{trade_id}</b>
Trader: {html.escape(rules['trader_name'])}
Pair: <b>{ud['pair']}</b> • {ud['type']}
Entry: <b>{ud['entry']}</b>
SL: <b>{ud['sl']}</b>
TP: <b>{ud['tp']}</b>
Risk: <b>{ud['risk']}%</b>
Pos #: <b>{ud['position_num']}</b>
Lot: <b>{ud['lot_size']}</b>
Lev: <b>1x</b>
RR: <b>{planned_rr:.2f}</b>{violation_text}
    """.strip()

    await send_trade_post(context, caption, ud['tv_file_id'], ud['mt5_file_id'], TRADE_TOPIC_ID)
    await update.message.reply_text(f"Trade #{trade_id} posted!")
    await show_menu(update)
    context.user_data.clear()
    return ConversationHandler.END

# ── CLOSE TRADE ───────────────────────────────────────────
async def start_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        await update.message.reply_text("Use /close in private.")
        return ConversationHandler.END
    trader_id = update.effective_user.id
    open_count = get_open_trade_count(trader_id)
    if open_count == 0:
        await update.message.reply_text("No open trades to close.", parse_mode=ParseMode.HTML)
        await show_menu(update)
        return ConversationHandler.END
    await update.message.reply_text(
        f"<b>🔴 Close Trade</b>\n\nYou have {open_count} open trade(s).\nEnter trade ID to close (use /mytrades):",
        parse_mode=ParseMode.HTML
    )
    return CLOSE_TRADE_ID

async def get_close_trade_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        trade_id = int(update.message.text.strip())
        trade = get_trade(trade_id, update.effective_user.id)
        if not trade:
            await update.message.reply_text("Trade not found.")
            return CLOSE_TRADE_ID
        if trade['status'] != 'Open':
            await update.message.reply_text("This trade is already closed.")
            return CLOSE_TRADE_ID
        context.user_data['trade_id'] = trade_id
        context.user_data['trade'] = trade
        await update.message.reply_text(f"Trade #{trade_id} selected.\nEnter exit price:")
        return CLOSE_EXIT_PRICE
    except ValueError:
        await update.message.reply_text("Enter a valid trade ID (number).")
        return CLOSE_TRADE_ID

async def get_close_exit_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['exit_price'] = float(update.message.text)
        await update.message.reply_text("<b>Upload TradingView result screenshot</b> (photo)", parse_mode=ParseMode.HTML)
        return CLOSE_TV_RESULT
    except ValueError:
        await update.message.reply_text("Enter a valid number.")
        return CLOSE_EXIT_PRICE

async def get_close_tv_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Send photo")
        return CLOSE_TV_RESULT
    context.user_data['close_tv_id'] = update.message.photo[-1].file_id
    await update.message.reply_text("<b>Upload MT5 closed trades screenshot</b> (photo)", parse_mode=ParseMode.HTML)
    return CLOSE_MT5_CLOSED

async def get_close_mt5(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Send photo")
        return CLOSE_MT5_CLOSED
    context.user_data['close_mt5_id'] = update.message.photo[-1].file_id

    trade = context.user_data['trade']
    exit_p = context.user_data['exit_price']
    rr = calculate_achieved_rr(trade['type'], trade['entry'], trade['sl'], exit_p)
    pl_percent = trade['risk'] * rr if trade['risk'] else 0

    balance = get_account_balance(trade['trader_id'])
    pl_monetary = balance * (pl_percent / 100)
    new_balance = balance + pl_monetary

    preview = f"""
<b>Close Preview – Confirm?</b>
Trade #{context.user_data['trade_id']}
Pair: <b>{trade['pair']}</b>
Exit: <b>{exit_p}</b>
RR: <b>{rr:.2f}</b>
P/L: <b>{pl_percent:+.2f}%</b> (${pl_monetary:+,.2f})
New Balance: <b>${new_balance:,.2f}</b>
Reply <b>YES</b> to post or <b>NO</b> to cancel
    """.strip()
    await update.message.reply_text(preview, parse_mode=ParseMode.HTML)
    return CLOSE_CONFIRM

async def confirm_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().upper() not in ["YES", "Y"]:
        await update.message.reply_text("Cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    ud = context.user_data
    trade = ud['trade']
    exit_p = ud['exit_price']
    rr = calculate_achieved_rr(trade['type'], trade['entry'], trade['sl'], exit_p)
    pl_percent = trade['risk'] * rr if trade['risk'] else 0

    trader_id = trade['trader_id']
    old_balance = get_account_balance(trader_id)
    pl_monetary = old_balance * (pl_percent / 100)
    new_balance = old_balance + pl_monetary

    try:
        close_trade_in_db(
            ud['trade_id'], trader_id, exit_p, rr, pl_percent, pl_monetary, new_balance
        )
        update_account_balance(trader_id, new_balance)

        if pl_monetary < 0:
            loss_percent = -pl_percent
            update_daily_loss(trader_id, loss_percent)

        await send_balance_update(
            context, trade['trader_name'], old_balance, new_balance,
            pl_percent, pl_monetary, ud['trade_id']
        )

        caption = f"""
🔴 <b>CLOSED TRADE #{ud['trade_id']}</b>
Trader: {html.escape(trade['trader_name'])}
Pair: <b>{trade['pair']}</b> • {trade['type']}
Entry: <b>{trade['entry']}</b>
SL: <b>{trade['sl']}</b>
Exit: <b>{exit_p}</b>
RR: <b>{rr:.2f}</b>
P/L: <b>{pl_percent:+.2f}%</b> (${pl_monetary:+,.2f})
Pos #: <b>{trade['position_number']}</b>
Lot: <b>{trade['lot_size']}</b>
💰 <b>New Balance: ${new_balance:,.2f}</b>
        """.strip()

        await send_trade_post(context, caption, ud['close_tv_id'], ud['close_mt5_id'], TRADE_TOPIC_ID)
        await update.message.reply_text(f"Trade #{ud['trade_id']} closed and posted!")
        await show_menu(update)
    except Exception as e:
        logging.error(f"Error closing trade: {e}")
        await update.message.reply_text("An error occurred while closing the trade. Please try again.")
    finally:
        context.user_data.clear()
    return ConversationHandler.END

# ── MYTRADES ──────────────────────────────────────────────
async def cmd_mytrades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        await update.message.reply_text("Use in private chat.")
        return
    trader_id = update.effective_user.id
    trades = get_user_trades(trader_id)
    if not trades:
        await update.message.reply_text("No trades recorded yet.")
        return

    text = "<b>Your Trades (latest first):</b>\n\n"
    for t in trades:
        status = t['status']
        if status == 'Open':
            status_str = "🟢 OPEN"
            pl_str = ""
        else:
            pl = t.get('pl_percent')
            pl_mon = t.get('pl_monetary')
            pl_str = f"{pl:+.2f}%" if pl is not None else "—"
            mon_str = f" (${pl_mon:+,.2f})" if pl_mon is not None else ""
            status_str = f"🔴 CLOSED {pl_str}{mon_str}"

        text += f"#{t['trade_id']} {t['pair']} {t['type']}  Pos:{t['position_number']} Lot:{t['lot_size']}  •  {status_str}  •  Risk {t['risk']}%\n"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ── VIOLATIONS ────────────────────────────────────────────
async def cmd_violations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        await update.message.reply_text("Use in private chat.")
        return
    trader_id = update.effective_user.id
    rows = get_user_violations(trader_id)
    if not rows:
        await update.message.reply_text("No violations yet. Good job! 👍", parse_mode=ParseMode.HTML)
        return
    text = "<b>Your Violations:</b>\n\n"
    for r in rows:
        text += f"#{r[0]} {r[1]} → {r[2]}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Action cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ========================= MAIN =========================
def main():
    init_db()
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    app = Application.builder().token(TOKEN).build()

    rules_conv = ConversationHandler(
        entry_points=[CommandHandler("setrules", start_setrules)],
        states={
            RULE_MAX_RISK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_max_risk)],
            RULE_MAX_DAILY_LOSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_max_daily_loss)],
            RULE_MIN_RR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_min_rr)],
            RULE_REQUIRE_SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_require_sl)],
            RULE_ALLOWED_PAIRS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_allowed_pairs)],
            RULE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_rules)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    trade_conv = ConversationHandler(
        entry_points=[CommandHandler("trade", start_trade)],
        states={
            TRADE_PAIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trade_pair)],
            TRADE_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trade_type)],
            TRADE_ENTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trade_entry)],
            TRADE_SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trade_sl)],
            TRADE_TP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trade_tp)],
            TRADE_RISK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trade_risk)],
            TRADE_POSITION_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trade_position_num)],
            TRADE_LOT_SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trade_lot_size)],
            TRADE_TV_SCREENSHOT: [MessageHandler(filters.PHOTO, get_tv_screenshot)],
            TRADE_MT5_SCREENSHOT: [MessageHandler(filters.PHOTO, finish_trade_open)],
            TRADE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_trade)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    close_conv = ConversationHandler(
        entry_points=[CommandHandler("close", start_close)],
        states={
            CLOSE_TRADE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_close_trade_id)],
            CLOSE_EXIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_close_exit_price)],
            CLOSE_TV_RESULT: [MessageHandler(filters.PHOTO, get_close_tv_result)],
            CLOSE_MT5_CLOSED: [MessageHandler(filters.PHOTO, get_close_mt5)],
            CLOSE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_close)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    balance_conv = ConversationHandler(
        entry_points=[CommandHandler("setbalance", start_setbalance)],
        states={
            ACCOUNT_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_account_balance)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(rules_conv)
    app.add_handler(balance_conv)
    app.add_handler(trade_conv)
    app.add_handler(close_conv)
    app.add_handler(CommandHandler("mytrades", cmd_mytrades))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("violations", cmd_violations))

    print("Bot started with full features: balance, daily loss, violation topic, position/lot, balance updates.")

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
