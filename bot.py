#!/usr/bin/env python3
"""
TALAST.app | طلاست‌اپ
نسخه نهایی سازگار با Render Free (Web Service)
"""

import os
import sqlite3
import asyncio
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List

from dotenv import load_dotenv
load_dotenv()

from flask import Flask
from bale import Bot, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, MenuKeyboardMarkup, MenuKeyboardButton

# ==================== تنظیمات ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN تنظیم نشده")

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

COMMISSION_PERCENT = 1.0
MAX_BUY_WITHOUT_WALLET = 20_000_000
MIN_GRAMS = 0.1

BRAND_NAME = "TALAST.app | طلاست‌اپ"
SUPPORT_USERNAME = "@talast"
WEBSITE = "www.talastapp.ir"
INVOICE_PREFIX = "TALAST"

DB_PATH = Path("talast.db")

# ==================== Flask برای باز نگه داشتن پورت ====================
app = Flask(__name__)

@app.route("/")
def home():
    return "TALAST.app Bot is running ✅"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==================== دیتابیس و بقیه کد (همان قبلی) ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            wallet_balance REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_price REAL NOT NULL,
            final_price REAL NOT NULL,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            grams REAL NOT NULL,
            unit_price REAL NOT NULL,
            total_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            admin_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            approved_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL UNIQUE,
            invoice_number TEXT UNIQUE,
            content TEXT,
            sent_to_user INTEGER DEFAULT 0,
            issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            type TEXT,
            order_id INTEGER,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

def db(query: str, params: tuple = (), fetch: str = None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(query, params)
    result = None
    if fetch == "one":
        result = c.fetchone()
    elif fetch == "all":
        result = c.fetchall()
    elif fetch == "lastrowid":
        result = c.lastrowid
    conn.commit()
    conn.close()
    return result

def get_or_create_user(user_id: int, full_name: str = None, username: str = None) -> dict:
    row = db("SELECT * FROM users WHERE id = ?", (user_id,), fetch="one")
    if row:
        return dict(row)
    db("INSERT INTO users (id, full_name, username) VALUES (?, ?, ?)", (user_id, full_name, username))
    row = db("SELECT * FROM users WHERE id = ?", (user_id,), fetch="one")
    return dict(row)

def get_wallet(user_id: int) -> float:
    row = db("SELECT wallet_balance FROM users WHERE id = ?", (user_id,), fetch="one")
    return row["wallet_balance"] if row else 0.0

def change_wallet(user_id: int, amount: float, type_: str, order_id: int = None, desc: str = ""):
    db("UPDATE users SET wallet_balance = wallet_balance + ? WHERE id = ?", (amount, user_id))
    db("INSERT INTO wallet_transactions (user_id, amount, type, order_id, description) VALUES (?, ?, ?, ?, ?)",
       (user_id, amount, type_, order_id, desc))

def save_price(source: float, final: float):
    db("INSERT INTO price_history (source_price, final_price) VALUES (?, ?)", (source, final))

def create_order(user_id: int, grams: float, unit_price: float, total: float) -> int:
    return db("INSERT INTO orders (user_id, grams, unit_price, total_amount) VALUES (?, ?, ?, ?)",
              (user_id, grams, unit_price, total), fetch="lastrowid")

def get_order(order_id: int) -> Optional[dict]:
    row = db("SELECT * FROM orders WHERE id = ?", (order_id,), fetch="one")
    return dict(row) if row else None

def update_order_status(order_id: int, status: str, note: str = None):
    if status == "approved":
        db("UPDATE orders SET status=?, approved_at=?, admin_note=? WHERE id=?",
           (status, datetime.now().isoformat(), note, order_id))
    else:
        db("UPDATE orders SET status=?, admin_note=? WHERE id=?", (status, note, order_id))

def get_pending_orders() -> List[dict]:
    rows = db("""SELECT o.*, u.full_name, u.username FROM orders o 
                 LEFT JOIN users u ON o.user_id = u.id 
                 WHERE o.status = 'pending' ORDER BY o.id DESC""", fetch="all")
    return [dict(r) for r in rows] if rows else []

def get_user_orders(user_id: int) -> List[dict]:
    rows = db("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 15", (user_id,), fetch="all")
    return [dict(r) for r in rows] if rows else []

def create_invoice(order_id: int, content: str) -> str:
    inv_number = f"{INVOICE_PREFIX}-{datetime.now().year}-{order_id:05d}"
    db("INSERT INTO invoices (order_id, invoice_number, content) VALUES (?, ?, ?)",
       (order_id, inv_number, content))
    return inv_number

current_price = {"source": 21351000, "final": None, "updated_at": None}

def calc_final(source: float) -> float:
    return round(source * (1 + COMMISSION_PERCENT / 100))

def get_price() -> dict:
    if current_price["final"] is None:
        current_price["final"] = calc_final(current_price["source"])
        current_price["updated_at"] = datetime.now().isoformat()
    return current_price

def set_manual_price(source: float) -> float:
    global current_price
    final = calc_final(source)
    current_price = {"source": source, "final": final, "updated_at": datetime.now().isoformat()}
    save_price(source, final)
    return final

async def can_buy(user_id: int, total: float):
    if total <= MAX_BUY_WITHOUT_WALLET:
        return True, "خرید بدون نیاز به کیف پول"
    balance = get_wallet(user_id)
    if balance >= total:
        return True, "موجودی کافی است"
    return False, f"نیاز به شارژ {total - balance:,.0f} تومان"

async def place_order(user_id: int, grams: float):
    if grams < MIN_GRAMS:
        return {"ok": False, "message": f"حداقل {MIN_GRAMS} گرم"}
    price = get_price()["final"]
    total = round(grams * price)
    ok, msg = await can_buy(user_id, total)
    if not ok:
        return {"ok": False, "message": msg}
    order_id = create_order(user_id, grams, price, total)
    if total > MAX_BUY_WITHOUT_WALLET:
        change_wallet(user_id, -total, "buy", order_id, f"خرید #{order_id}")
    return {"ok": True, "order_id": order_id, "grams": grams, "unit_price": price, "total": total}

def make_invoice_text(order: dict, user: dict, inv_number: str) -> str:
    now = datetime.now().strftime("%Y/%m/%d - %H:%M")
    delivery = (datetime.now() + timedelta(days=1)).strftime("%Y/%m/%d")
    return f"""
╔══════════════════════════════════════╗
║     فاکتور رسمی {BRAND_NAME}      ║
╚══════════════════════════════════════╝

شماره فاکتور: {inv_number}
تاریخ: {now}
وضعیت: تأیید شده ✅

خریدار: {user.get('full_name') or '—'}
آیدی: {order['user_id']}

مقدار: {order['grams']} گرم
قیمت واحد: {order['unit_price']:,} تومان
مبلغ کل: {order['total_amount']:,} تومان

تحویل: اولین روز کاری بعد از تأیید
تاریخ تقریبی: {delivery}
پشتیبانی: {SUPPORT_USERNAME}
""".strip()

def main_menu():
    kb = MenuKeyboardMarkup()
    kb.add(MenuKeyboardButton("💰 قیمت لحظه‌ای"), MenuKeyboardButton("🛒 خرید طلا"))
    kb.add(MenuKeyboardButton("👤 موجودی من"), MenuKeyboardButton("📜 سفارش‌های من"))
    kb.add(MenuKeyboardButton("📞 پشتیبانی"), MenuKeyboardButton("🌐 وب‌سایت"))
    return kb

def price_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="refresh_price"))
    kb.add(InlineKeyboardButton("🛒 خرید", callback_data="buy_start"), row=2)
    return kb

def confirm_kb(grams: float):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ تأیید", callback_data=f"confirm:{grams}"),
           InlineKeyboardButton("❌ انصراف", callback_data="cancel"))
    return kb

def admin_order_kb(order_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ تأیید", callback_data=f"approve:{order_id}"),
           InlineKeyboardButton("❌ رد", callback_data=f"reject:{order_id}"))
    return kb

# ==================== ربات ====================
client = Bot(token=BOT_TOKEN)
user_states: Dict[int, str] = {}

@client.listen("on_ready")
async def on_ready():
    init_db()
    get_price()
    print(f"✅ ربات آماده است | {BRAND_NAME}")
    print(f"ادمین‌ها: {ADMIN_IDS}")

@client.listen("on_message")
async def on_message(message: Message):
    if not message.text:
        return

    user = message.author
    text = message.text.strip()
    user_id = user.user_id

    if text == "/start":
        get_or_create_user(user_id, user.first_name, user.username)
        await message.reply(
            f"سلام {user.first_name} 👋\n\nبه **{BRAND_NAME}** خوش آمدید.\n"
            f"پشتیبانی: {SUPPORT_USERNAME}\nوب‌سایت: {WEBSITE}",
            components=main_menu()
        )
        return

    if text == "/admin":
        if user_id not in ADMIN_IDS:
            await message.reply("دسترسی ندارید.")
            return
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("📋 سفارش‌های در انتظار", callback_data="admin_pending"))
        kb.add(InlineKeyboardButton("💰 تنظیم قیمت", callback_data="admin_set_price"), row=2)
        await message.reply("🛠 پنل مدیریت", components=kb)
        return

    if text == "💰 قیمت لحظه‌ای":
        p = get_price()
        await message.reply(
            f"📊 قیمت لحظه‌ای\n\nپایه: `{p['source']:,}`\nنهایی (+۱٪): `{p['final']:,}` تومان / گرم\n🕐 {p['updated_at'][11:19]}",
            components=price_kb()
        )
        return

    if text == "🛒 خرید طلا":
        user_states[user_id] = "waiting_grams"
        await message.reply(f"مقدار را به گرم وارد کنید (حداقل {MIN_GRAMS}):")
        return

    if text == "👤 موجودی من":
        get_or_create_user(user_id, user.first_name, user.username)
        balance = get_wallet(user_id)
        await message.reply(f"💰 موجودی شما: `{balance:,.0f}` تومان")
        return

    if text == "📜 سفارش‌های من":
        orders = get_user_orders(user_id)
        if not orders:
            await message.reply("سفارشی ندارید.")
            return
        lines = ["📜 سفارش‌های شما:\n"]
        for o in orders:
            status = {"pending": "⏳", "approved": "✅", "rejected": "❌"}.get(o["status"], o["status"])
            lines.append(f"#{o['id']} | {o['grams']}g | {o['total_amount']:,} | {status}")
        await message.reply("\n".join(lines))
        return

    if text in ["📞 پشتیبانی", "🌐 وب‌سایت"]:
        await message.reply(f"پشتیبانی: {SUPPORT_USERNAME}\nسایت: https://{WEBSITE}")
        return

    if user_states.get(user_id) == "waiting_grams":
        try:
            grams = float(text.replace(",", "."))
            if grams <= 0:
                raise ValueError
        except:
            await message.reply("عدد نامعتبر است.")
            return

        price = get_price()["final"]
        total = round(grams * price)
        ok, msg = await can_buy(user_id, total)
        await message.reply(
            f"📋 پیش‌فاکتور\n\nمقدار: {grams} گرم\nقیمت: {price:,}\nمبلغ: {total:,}\n\n{msg}",
            components=confirm_kb(grams)
        )
        user_states[user_id] = None
        return

    if user_states.get(user_id) == "admin_price" and user_id in ADMIN_IDS:
        try:
            source = float(text.replace(",", ""))
            final = set_manual_price(source)
            await message.reply(f"✅ قیمت تنظیم شد\nپایه: {source:,}\nنهایی: {final:,}")
            user_states[user_id] = None
        except:
            await message.reply("عدد نامعتبر")
        return

@client.listen("on_callback")
async def on_callback(callback: CallbackQuery):
    data = callback.data
    user_id = callback.author.user_id

    if data == "refresh_price":
        p = get_price()
        await callback.message.edit(f"📊 قیمت به‌روز شد\n💰 `{p['final']:,}` تومان / گرم", components=price_kb())
        await callback.answer("✅")
        return

    if data == "buy_start":
        user_states[user_id] = "waiting_grams"
        await callback.message.reply(f"مقدار را به گرم وارد کنید (حداقل {MIN_GRAMS}):")
        await callback.answer()
        return

    if data == "cancel":
        await callback.message.edit("لغو شد.")
        await callback.answer()
        return

    if data.startswith("confirm:"):
        grams = float(data.split(":")[1])
        get_or_create_user(user_id, callback.author.first_name, callback.author.username)
        result = await place_order(user_id, grams)
        if not result["ok"]:
            await callback.message.edit(f"❌ {result['message']}")
            await callback.answer()
            return

        for admin in ADMIN_IDS:
            try:
                await callback.bot.send_message(
                    admin,
                    f"🆕 سفارش #{result['order_id']}\nکاربر: {callback.author.first_name}\nمقدار: {result['grams']}g\nمبلغ: {result['total']:,}",
                    components=admin_order_kb(result["order_id"])
                )
            except:
                pass

        await callback.message.edit(f"✅ سفارش #{result['order_id']} ثبت شد.\nبعد از تأیید ادمین فاکتور ارسال می‌شود.")
        await callback.answer("ثبت شد")
        return

    if user_id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return

    if data == "admin_pending":
        orders = get_pending_orders()
        if not orders:
            await callback.message.edit("سفارشی در انتظار نیست.")
            await callback.answer()
            return
        text = "📋 سفارش‌های در انتظار:\n\n"
        for o in orders[:10]:
            text += f"#{o['id']} | {o.get('full_name') or o['user_id']} | {o['grams']}g | {o['total_amount']:,}\n"
        await callback.message.edit(text)
        for o in orders[:5]:
            await callback.bot.send_message(user_id, f"سفارش #{o['id']} – {o['grams']}g – {o['total_amount']:,}", components=admin_order_kb(o["id"]))
        await callback.answer()
        return

    if data == "admin_set_price":
        user_states[user_id] = "admin_price"
        await callback.message.reply("قیمت پایه را وارد کنید (مثال: 21351000)")
        await callback.answer()
        return

    if data.startswith("approve:"):
        order_id = int(data.split(":")[1])
        order = get_order(order_id)
        if not order or order["status"] != "pending":
            await callback.answer("قابل تأیید نیست", show_alert=True)
            return
        update_order_status(order_id, "approved", "تأیید ادمین")
        user = get_or_create_user(order["user_id"])
        inv_number = create_invoice(order_id, "")
        inv_text = make_invoice_text(order, user, inv_number)
        db("UPDATE invoices SET content=? WHERE order_id=?", (inv_text, order_id))
        try:
            await callback.bot.send_message(order["user_id"], f"✅ سفارش تأیید شد!\n\n{inv_text}")
        except:
            pass
        await callback.message.edit(f"✅ سفارش #{order_id} تأیید و فاکتور ارسال شد.")
        await callback.answer("انجام شد")
        return

    if data.startswith("reject:"):
        order_id = int(data.split(":")[1])
        order = get_order(order_id)
        if not order or order["status"] != "pending":
            await callback.answer("قابل رد نیست", show_alert=True)
            return
        update_order_status(order_id, "rejected", "رد ادمین")
        if order["total_amount"] > MAX_BUY_WITHOUT_WALLET:
            change_wallet(order["user_id"], order["total_amount"], "refund", order_id, f"بازگشت #{order_id}")
        try:
            await callback.bot.send_message(order["user_id"], f"❌ سفارش #{order_id} رد شد.")
        except:
            pass
        await callback.message.edit(f"❌ سفارش #{order_id} رد شد.")
        await callback.answer("رد شد")
        return

# ==================== اجرا ====================
if __name__ == "__main__":
    print(f"در حال راه‌اندازی {BRAND_NAME} ...")

    # Flask را در ترد جداگانه اجرا کن
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # ربات را اجرا کن
    client.run()
