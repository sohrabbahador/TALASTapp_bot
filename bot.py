#!/usr/bin/env python3
"""
TALAST.app | طلاست‌اپ
ربات فروش طلای آب‌شده - نسخه نهایی آماده دیپلوی
"""

import os
import sqlite3
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List

from dotenv import load_dotenv
load_dotenv()

from bale import (
    Bot, Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    MenuKeyboardMarkup, MenuKeyboardButton
)
from bale.handlers import CommandHandler, MessageHandler, CallbackQueryHandler
from bale.checks import Text, Data

# ==================== تنظیمات ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN تنظیم نشده است. در Render یا .env آن را اضافه کنید.")

ADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

COMMISSION_PERCENT = 1.0
MAX_BUY_WITHOUT_WALLET = 20_000_000
MIN_GRAMS = 0.1
PRICE_UPDATE_SECONDS = 30

BRAND_NAME = "TALAST.app | طلاست‌اپ"
SUPPORT_USERNAME = "@talast"
WEBSITE = "www.talastapp.ir"
INVOICE_PREFIX = "TALAST"

DB_PATH = Path("talast.db")

# ==================== دیتابیس ====================
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
            approved_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
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
    db("INSERT INTO users (id, full_name, username) VALUES (?, ?, ?)",
       (user_id, full_name, username))
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
    return db(
        "INSERT INTO orders (user_id, grams, unit_price, total_amount) VALUES (?, ?, ?, ?)",
        (user_id, grams, unit_price, total),
        fetch="lastrowid"
    )

def get_order(order_id: int) -> Optional[dict]:
    row = db("SELECT * FROM orders WHERE id = ?", (order_id,), fetch="one")
    return dict(row) if row else None

def update_order_status(order_id: int, status: str, note: str = None):
    if status == "approved":
        db("UPDATE orders SET status=?, approved_at=?, admin_note=? WHERE id=?",
           (status, datetime.now().isoformat(), note, order_id))
    else:
        db("UPDATE orders SET status=?, admin_note=? WHERE id=?",
           (status, note, order_id))

def get_pending_orders() -> List[dict]:
    rows = db("""
        SELECT o.*, u.full_name, u.username 
        FROM orders o LEFT JOIN users u ON o.user_id = u.id 
        WHERE o.status = 'pending' ORDER BY o.id DESC
    """, fetch="all")
    return [dict(r) for r in rows] if rows else []

def get_user_orders(user_id: int) -> List[dict]:
    rows = db("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 15", (user_id,), fetch="all")
    return [dict(r) for r in rows] if rows else []

def create_invoice(order_id: int, content: str) -> str:
    inv_number = f"{INVOICE_PREFIX}-{datetime.now().year}-{order_id:05d}"
    db("INSERT INTO invoices (order_id, invoice_number, content) VALUES (?, ?, ?)",
       (order_id, inv_number, content))
    return inv_number

# ==================== قیمت ====================
current_price = {
    "source": 21_351_000,
    "final": None,
    "updated_at": None
}

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
    current_price = {
        "source": source,
        "final": final,
        "updated_at": datetime.now().isoformat()
    }
    save_price(source, final)
    return final

async def price_loop():
    while True:
        try:
            # فعلاً فقط لاگ می‌گیریم (قیمت دستی است)
            p = get_price()
            print(f"[{datetime.now():%H:%M:%S}] قیمت فعلی: {p['final']:,} تومان")
        except Exception as e:
            print("خطا در حلقه قیمت:", e)
        await asyncio.sleep(PRICE_UPDATE_SECONDS)

# ==================== منطق کسب‌وکار ====================
async def can_buy(user_id: int, total: float) -> tuple[bool, str]:
    if total <= MAX_BUY_WITHOUT_WALLET:
        return True, "خرید بدون نیاز به کیف پول"
    balance = get_wallet(user_id)
    if balance >= total:
        return True, "موجودی کیف پول کافی است"
    return False, f"نیاز به شارژ {total - balance:,.0f} تومان"

async def place_order(user_id: int, grams: float) -> dict:
    if grams < MIN_GRAMS:
        return {"ok": False, "message": f"حداقل خرید {MIN_GRAMS} گرم است."}

    price = get_price()["final"]
    total = round(grams * price)

    ok, msg = await can_buy(user_id, total)
    if not ok:
        return {"ok": False, "message": msg}

    order_id = create_order(user_id, grams, price, total)

    if total > MAX_BUY_WITHOUT_WALLET:
        change_wallet(user_id, -total, "buy", order_id, f"خرید سفارش #{order_id}")

    return {
        "ok": True,
        "order_id": order_id,
        "grams": grams,
        "unit_price": price,
        "total": total
    }

def make_invoice_text(order: dict, user: dict, inv_number: str) -> str:
    now = datetime.now().strftime("%Y/%m/%d - %H:%M")
    delivery = (datetime.now() + timedelta(days=1)).strftime("%Y/%m/%d")
    return f"""
╔══════════════════════════════════════╗
║     فاکتور رسمی {BRAND_NAME}      ║
╚══════════════════════════════════════╝

شماره فاکتور: {inv_number}
تاریخ صدور: {now}
وضعیت: تأیید شده ✅

────────────────────────────
مشخصات خریدار:
نام: {user.get('full_name') or '—'}
آیدی: {order['user_id']}
یوزرنیم: @{user.get('username') or '—'}

────────────────────────────
جزئیات سفارش:
مقدار طلا: {order['grams']} گرم (۱۸ عیار آب‌شده)
قیمت هر گرم: {order['unit_price']:,} تومان
مبلغ کل: {order['total_amount']:,} تومان

────────────────────────────
شرایط تحویل:
• تحویل فیزیکی در اولین روز کاری بعد از تأیید
• تاریخ تقریبی: {delivery}
• پشتیبانی: {SUPPORT_USERNAME}
• وب‌سایت: {WEBSITE}

────────────────────────────
این فاکتور توسط سیستم {BRAND_NAME} صادر شده است.
""".strip()

# ==================== کیبوردها ====================
def main_menu():
    kb = MenuKeyboardMarkup()
    kb.add(MenuKeyboardButton("💰 قیمت لحظه‌ای"), MenuKeyboardButton("🛒 خرید طلا"))
    kb.add(MenuKeyboardButton("👤 موجودی من"), MenuKeyboardButton("📜 سفارش‌های من"))
    kb.add(MenuKeyboardButton("📞 پشتیبانی"), MenuKeyboardButton("🌐 وب‌سایت"))
    return kb

def price_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="refresh_price"))
    kb.add(InlineKeyboardButton("🛒 خرید همین حالا", callback_data="buy_start"), row=2)
    return kb

def confirm_kb(grams: float):
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ تأیید و ثبت", callback_data=f"confirm:{grams}"),
        InlineKeyboardButton("❌ انصراف", callback_data="cancel")
    )
    return kb

def admin_order_kb(order_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ تأیید + فاکتور", callback_data=f"approve:{order_id}"),
        InlineKeyboardButton("❌ رد", callback_data=f"reject:{order_id}")
    )
    return kb

def admin_panel_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📋 سفارش‌های در انتظار", callback_data="admin_pending"))
    kb.add(InlineKeyboardButton("💰 تنظیم قیمت دستی", callback_data="admin_set_price"), row=2)
    kb.add(InlineKeyboardButton("📊 آمار", callback_data="admin_stats"), row=3)
    return kb

# ==================== هندلرها ====================
user_states: Dict[int, str] = {}

async def cmd_start(message: Message):
    user = message.author
    get_or_create_user(user.user_id, user.first_name, user.username)
    text = (
        f"سلام {user.first_name} 👋\n\n"
        f"به **{BRAND_NAME}** خوش آمدید.\n"
        "مرکز فروش طلای آب‌شده با تحویل فیزیکی.\n\n"
        f"پشتیبانی: {SUPPORT_USERNAME}\n"
        f"وب‌سایت: {WEBSITE}"
    )
    await message.reply(text, components=main_menu())

async def show_price(message: Message):
    p = get_price()
    text = (
        f"📊 **قیمت لحظه‌ای طلای آب‌شده ۱۸ عیار**\n\n"
        f"🟢 قیمت پایه: `{p['source']:,}` تومان\n"
        f"➕ حاشیه {COMMISSION_PERCENT}٪: `{p['final'] - p['source']:,}` تومان\n"
        f"────────────────────\n"
        f"💰 **قیمت نهایی:** `{p['final']:,}` تومان / گرم\n\n"
        f"🕐 {p['updated_at'][11:19] if p.get('updated_at') else '—'}"
    )
    await message.reply(text, components=price_kb())

async def buy_start(obj):
    if isinstance(obj, CallbackQuery):
        user_id = obj.author.user_id
        await obj.message.reply(
            f"🛒 مقدار طلا را به **گرم** وارد کنید:\n"
            f"(حداقل {MIN_GRAMS} گرم)\n"
            f"سقف بدون کیف پول: {MAX_BUY_WITHOUT_WALLET:,} تومان"
        )
        await obj.answer()
    else:
        user_id = obj.author.user_id
        await obj.reply(
            f"🛒 مقدار طلا را به **گرم** وارد کنید:\n"
            f"(حداقل {MIN_GRAMS} گرم)\n"
            f"سقف بدون کیف پول: {MAX_BUY_WITHOUT_WALLET:,} تومان"
        )
    user_states[user_id] = "waiting_grams"

async def process_grams(message: Message) -> bool:
    user_id = message.author.user_id
    if user_states.get(user_id) != "waiting_grams":
        return False
    try:
        grams = float(message.text.replace(",", ".").strip())
        if grams <= 0:
            raise ValueError
    except:
        await message.reply("❌ عدد نامعتبر. دوباره وارد کنید.")
        return True

    price = get_price()["final"]
    total = round(grams * price)
    ok, msg = await can_buy(user_id, total)

    text = (
        f"📋 **پیش‌فاکتور**\n\n"
        f"مقدار: `{grams}` گرم\n"
        f"قیمت واحد: `{price:,}` تومان\n"
        f"مبلغ کل: `{total:,}` تومان\n\n"
        f"{'✅' if ok else '⚠️'} {msg}\n\n"
        f"تأیید می‌کنید؟"
    )
    await message.reply(text, components=confirm_kb(grams))
    user_states[user_id] = None
    return True

async def confirm_order(callback: CallbackQuery):
    try:
        grams = float(callback.data.split(":")[1])
    except:
        await callback.answer("خطا")
        return

    user_id = callback.author.user_id
    get_or_create_user(user_id, callback.author.first_name, callback.author.username)

    result = await place_order(user_id, grams)
    if not result["ok"]:
        await callback.message.edit(f"❌ {result['message']}")
        await callback.answer()
        return

    # اطلاع به ادمین‌ها
    info = (
        f"🆕 سفارش #{result['order_id']}\n"
        f"کاربر: {callback.author.first_name} ({user_id})\n"
        f"مقدار: {result['grams']} گرم\n"
        f"مبلغ: {result['total']:,} تومان"
    )
    for admin in ADMIN_IDS:
        try:
            await callback.bot.send_message(admin, info, components=admin_order_kb(result["order_id"]))
        except:
            pass

    await callback.message.edit(
        f"✅ سفارش **#{result['order_id']}** ثبت شد.\n\n"
        f"پس از تأیید ادمین، فاکتور ارسال می‌شود.\n"
        f"تحویل: اولین روز کاری بعد از تأیید"
    )
    await callback.answer("ثبت شد")

async def show_wallet(message: Message):
    user_id = message.author.user_id
    get_or_create_user(user_id, message.author.first_name, message.author.username)
    balance = get_wallet(user_id)
    await message.reply(
        f"👤 **موجودی کیف پول**\n\n"
        f"💰 `{balance:,.0f}` تومان\n\n"
        f"سقف بدون شارژ: {MAX_BUY_WITHOUT_WALLET:,} تومان\n"
        f"برای شارژ با پشتیبانی تماس بگیرید: {SUPPORT_USERNAME}"
    )

async def show_orders(message: Message):
    orders = get_user_orders(message.author.user_id)
    if not orders:
        await message.reply("هنوز سفارشی ندارید.")
        return
    status_map = {
        "pending": "⏳ در انتظار",
        "approved": "✅ تأیید شده",
        "rejected": "❌ رد شده"
    }
    lines = ["📜 **سفارش‌های شما:**\n"]
    for o in orders:
        lines.append(f"#{o['id']} | {o['grams']}g | {o['total_amount']:,} | {status_map.get(o['status'], o['status'])}")
    await message.reply("\n".join(lines))

# ---------- ادمین ----------
async def admin_panel(message: Message):
    if message.author.user_id not in ADMIN_IDS:
        await message.reply("دسترسی ندارید.")
        return
    await message.reply(f"🛠 پنل مدیریت {BRAND_NAME}", components=admin_panel_kb())

async def admin_pending(callback: CallbackQuery):
    if callback.author.user_id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    orders = get_pending_orders()
    if not orders:
        await callback.message.edit("سفارش در انتظاری نیست.")
        await callback.answer()
        return

    text = "📋 **سفارش‌های در انتظار:**\n\n"
    for o in orders[:10]:
        text += f"#{o['id']} | {o.get('full_name') or o['user_id']} | {o['grams']}g | {o['total_amount']:,}\n"
    await callback.message.edit(text)

    for o in orders[:5]:
        await callback.bot.send_message(
            callback.author.user_id,
            f"سفارش #{o['id']} – {o['grams']} گرم – {o['total_amount']:,} تومان",
            components=admin_order_kb(o["id"])
        )
    await callback.answer()

async def admin_approve(callback: CallbackQuery):
    if callback.author.user_id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])
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
        await callback.bot.send_message(order["user_id"], f"✅ سفارش شما تأیید شد!\n\n{inv_text}")
        db("UPDATE invoices SET sent_to_user=1 WHERE order_id=?", (order_id,))
    except Exception as e:
        print("خطا در ارسال فاکتور:", e)

    await callback.message.edit(f"✅ سفارش #{order_id} تأیید و فاکتور ارسال شد.")
    await callback.answer("انجام شد")

async def admin_reject(callback: CallbackQuery):
    if callback.author.user_id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])
    order = get_order(order_id)
    if not order or order["status"] != "pending":
        await callback.answer("قابل رد نیست", show_alert=True)
        return

    update_order_status(order_id, "rejected", "رد ادمین")

    if order["total_amount"] > MAX_BUY_WITHOUT_WALLET:
        change_wallet(order["user_id"], order["total_amount"], "refund", order_id, f"بازگشت سفارش #{order_id}")

    try:
        await callback.bot.send_message(
            order["user_id"],
            f"❌ سفارش #{order_id} رد شد.\nدر صورت کسر وجه، بازگردانده شد.\nپشتیبانی: {SUPPORT_USERNAME}"
        )
    except:
        pass

    await callback.message.edit(f"❌ سفارش #{order_id} رد شد.")
    await callback.answer("رد شد")

async def admin_set_price_start(callback: CallbackQuery):
    if callback.author.user_id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    user_states[callback.author.user_id] = "admin_price"
    await callback.message.reply("قیمت پایه (سبز مطیع‌ها) را وارد کنید:\nمثال: 21351000")
    await callback.answer()

async def process_admin_price(message: Message) -> bool:
    if user_states.get(message.author.user_id) != "admin_price":
        return False
    if message.author.user_id not in ADMIN_IDS:
        return False
    try:
        source = float(message.text.replace(",", "").strip())
        if source < 1_000_000:
            raise ValueError
    except:
        await message.reply("❌ عدد نامعتبر")
        return True

    final = set_manual_price(source)
    user_states[message.author.user_id] = None
    await message.reply(f"✅ قیمت تنظیم شد\nپایه: {source:,}\nنهایی (+۱٪): {final:,}")
    return True

async def admin_stats(callback: CallbackQuery):
    if callback.author.user_id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    total = db("SELECT COUNT(*) as c FROM orders", fetch="one")["c"]
    pending = db("SELECT COUNT(*) as c FROM orders WHERE status='pending'", fetch="one")["c"]
    approved = db("SELECT COUNT(*) as c FROM orders WHERE status='approved'", fetch="one")["c"]
    sales = db("SELECT COALESCE(SUM(total_amount),0) as s FROM orders WHERE status='approved'", fetch="one")["s"]
    text = (
        f"📊 آمار {BRAND_NAME}\n\n"
        f"کل سفارش‌ها: {total}\n"
        f"در انتظار: {pending}\n"
        f"تأیید شده: {approved}\n"
        f"فروش تأییدشده: {sales:,.0f} تومان\n"
        f"قیمت فعلی: {get_price()['final']:,}"
    )
    await callback.message.edit(text)
    await callback.answer()

# ==================== راه‌اندازی ====================
client = Bot(token=BOT_TOKEN)

@client.listen("on_ready")
async def on_ready():
    init_db()
    get_price()
    asyncio.create_task(price_loop())
    print(f"✅ ربات آماده است | {BRAND_NAME}")
    print(f"ادمین‌ها: {ADMIN_IDS}")

@client.handle(CommandHandler("start"))
async def _(m: Message):
    await cmd_start(m)

@client.handle(CommandHandler("admin"))
async def _(m: Message):
    await admin_panel(m)

@client.handle(MessageHandler(Text(["💰 قیمت لحظه‌ای"])))
async def _(m: Message):
    await show_price(m)

@client.handle(MessageHandler(Text(["🛒 خرید طلا"])))
async def _(m: Message):
    await buy_start(m)

@client.handle(MessageHandler(Text(["👤 موجودی من"])))
async def _(m: Message):
    await show_wallet(m)

@client.handle(MessageHandler(Text(["📜 سفارش‌های من"])))
async def _(m: Message):
    await show_orders(m)

@client.handle(MessageHandler(Text(["📞 پشتیبانی"])))
async def _(m: Message):
    await m.reply(f"پشتیبانی: {SUPPORT_USERNAME}\nوب‌سایت: https://{WEBSITE}")

@client.handle(MessageHandler(Text(["🌐 وب‌سایت"])))
async def _(m: Message):
    await m.reply(f"🌐 https://{WEBSITE}")

@client.handle(CallbackQueryHandler(Data("refresh_price")))
async def _(c: CallbackQuery):
    p = get_price()
    text = f"📊 قیمت به‌روز شد\n💰 `{p['final']:,}` تومان / گرم\n🕐 {p['updated_at'][11:19]}"
    await c.message.edit(text, components=price_kb())
    await c.answer("✅")

@client.handle(CallbackQueryHandler(Data("buy_start")))
async def _(c: CallbackQuery):
    await buy_start(c)

@client.handle(CallbackQueryHandler(Data(startswith="confirm:")))
async def _(c: CallbackQuery):
    await confirm_order(c)

@client.handle(CallbackQueryHandler(Data("cancel")))
async def _(c: CallbackQuery):
    await c.message.edit("لغو شد.")
    await c.answer()

@client.handle(CallbackQueryHandler(Data("admin_pending")))
async def _(c: CallbackQuery):
    await admin_pending(c)

@client.handle(CallbackQueryHandler(Data(startswith="approve:")))
async def _(c: CallbackQuery):
    await admin_approve(c)

@client.handle(CallbackQueryHandler(Data(startswith="reject:")))
async def _(c: CallbackQuery):
    await admin_reject(c)

@client.handle(CallbackQueryHandler(Data("admin_set_price")))
async def _(c: CallbackQuery):
    await admin_set_price_start(c)

@client.handle(CallbackQueryHandler(Data("admin_stats")))
async def _(c: CallbackQuery):
    await admin_stats(c)

@client.handle(MessageHandler(Text()))
async def text_handler(m: Message):
    if await process_admin_price(m):
        return
    if await process_grams(m):
        return
    await m.reply("از منوی پایین استفاده کنید.", components=main_menu())

# ==================== اجرا ====================
if __name__ == "__main__":
    print(f"در حال راه‌اندازی {BRAND_NAME} ...")
    client.run()
