# pip install aiogram aiosqlite
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)

logging.basicConfig(level=logging.INFO)

# ================== SOZLAMALAR ==================
BOT_TOKEN = "8506218606:AAEP-Ps83bGSbDZ1GU_WyxM0eaI77nYfeOk"
ADMIN_ID = 6917400767              # admin telegram id
CHANNEL_ID = -1003717446145       # private kanal ID (-100... bilan)
DB_NAME = "bot.db"
# ================================================

TARIFFS = {
    1:  {"title": "1 kun",   "amount": 5000},
    7:  {"title": "1 hafta", "amount": 25000},
    30: {"title": "1 oy",    "amount": 49000},
    90: {"title": "3 oy",    "amount": 129000},
}

PHONE_RE = re.compile(r"^\+?\d{7,15}$")  # +998901234567 kabi

# -------------------- TIME UTILS --------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)

def days_left(end_at_iso: str | None) -> int | None:
    if not end_at_iso:
        return None
    try:
        end_dt = from_iso(end_at_iso)
    except Exception:
        return None
    diff = end_dt - now_utc()
    if diff.total_seconds() <= 0:
        return 0
    return int(diff.total_seconds() // 86400) + 1

def profile_link(user_id: int, name: str) -> str:
    safe = (name or "Foydalanuvchi").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'

# -------------------- KEYBOARDS --------------------
def ikb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Obuna olish", callback_data="buy_sub")],
        [InlineKeyboardButton(text="📞 Admin bilan bog'lanish", callback_data="support_start")],
    ])

def ikb_tariffs():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"1 kun - {TARIFFS[1]['amount']:,} so'm", callback_data="t:1")],
        [InlineKeyboardButton(text=f"1 hafta - {TARIFFS[7]['amount']:,} so'm", callback_data="t:7")],
        [InlineKeyboardButton(text=f"1 oy - {TARIFFS[30]['amount']:,} so'm", callback_data="t:30")],
        [InlineKeyboardButton(text=f"3 oy - {TARIFFS[90]['amount']:,} so'm", callback_data="t:90")],
        [InlineKeyboardButton(text="⬅️ Ortga", callback_data="back_main")],
    ])

def kb_request_phone():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def ikb_admin_panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="adm:stats")],
        [InlineKeyboardButton(text="✅ Aktiv obunachilar", callback_data="adm:users")],
        [InlineKeyboardButton(text="🧾 Pending to'lovlar", callback_data="adm:pending")],
        [InlineKeyboardButton(text="📩 Ochiq suhbatlar", callback_data="adm:tickets")],
        [InlineKeyboardButton(text="✉️ Userga xabar yozish", callback_data="adm:send")],
    ])

def ikb_users_pager(offset: int, limit: int, total: int):
    btns = []
    if offset > 0:
        btns.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"adm_users:{max(0, offset-limit)}"))
    if offset + limit < total:
        btns.append(InlineKeyboardButton(text="➡️ Keyingisi", callback_data=f"adm_users:{offset+limit}"))
    if not btns:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[btns])

def ikb_admin_payment(pay_id: int, user_id: int, days: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"pay_ok:{pay_id}:{user_id}:{days}"),
            InlineKeyboardButton(text="❌ Rad etish", callback_data=f"pay_no:{pay_id}:{user_id}:{days}"),
        ]
    ])

def ikb_user_chat_controls():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Javob yozish", callback_data="user_reply")],
        [InlineKeyboardButton(text="✅ Suhbatni yopish", callback_data="support_close")],
    ])

def ikb_admin_support(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="↩️ Javob yozish", callback_data=f"admin_reply:{user_id}"),
            InlineKeyboardButton(text="✅ Suhbatni yopish", callback_data=f"admin_close:{user_id}"),
        ]
    ])

# -------------------- FSM --------------------
class RegState(StatesGroup):
    waiting_name = State()
    waiting_phone = State()

class SubState(StatesGroup):
    waiting_check = State()

class SupportUserState(StatesGroup):
    waiting_message = State()

class SupportAdminState(StatesGroup):
    waiting_reply_text = State()

class AdminSendState(StatesGroup):
    waiting_user_id = State()
    waiting_text = State()

# -------------------- DB --------------------
async def ensure_column(db, table: str, col: str, col_def: str):
    cur = await db.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in await cur.fetchall()]
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            created_at TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions(
            user_id INTEGER PRIMARY KEY,
            status TEXT,
            duration_days INTEGER,
            start_at TEXT,
            end_at TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            duration_days INTEGER,
            status TEXT,
            file_type TEXT,
            file_id TEXT,
            created_at TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS tickets(
            user_id INTEGER PRIMARY KEY,
            status TEXT,
            created_at TEXT,
            closed_at TEXT
        )
        """)
        await ensure_column(db, "subscriptions", "duration_days", "INTEGER")
        await ensure_column(db, "subscriptions", "end_at", "TEXT")
        await ensure_column(db, "payments", "duration_days", "INTEGER")
        await ensure_column(db, "tickets", "closed_at", "TEXT")
        await db.commit()

async def db_user_get(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT user_id, full_name, phone, created_at FROM users WHERE user_id=?",
            (user_id,)
        )
        return await cur.fetchone()

async def db_user_upsert(user_id: int, full_name: str | None = None, phone: str | None = None):
    existing = await db_user_get(user_id)
    async with aiosqlite.connect(DB_NAME) as db:
        if existing:
            if full_name is not None:
                await db.execute("UPDATE users SET full_name=? WHERE user_id=?", (full_name, user_id))
            if phone is not None:
                await db.execute("UPDATE users SET phone=? WHERE user_id=?", (phone, user_id))
        else:
            await db.execute(
                "INSERT INTO users(user_id, full_name, phone, created_at) VALUES(?,?,?,?)",
                (user_id, full_name or "", phone or "", iso(now_utc()))
            )
        await db.commit()

async def db_user_exists(user_id: int) -> bool:
    return (await db_user_get(user_id)) is not None

async def db_payment_create(user_id: int, days: int, file_type: str, file_id: str) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "INSERT INTO payments(user_id, duration_days, status, file_type, file_id, created_at) VALUES(?,?,?,?,?,?)",
            (user_id, days, "pending", file_type, file_id, iso(now_utc()))
        )
        await db.commit()
        return cur.lastrowid

async def db_payment_set_status(pay_id: int, status: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE payments SET status=? WHERE id=?", (status, pay_id))
        await db.commit()

async def db_sub_set_active(user_id: int, days: int):
    start = now_utc()
    end = start + timedelta(days=days)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO subscriptions(user_id, status, duration_days, start_at, end_at) VALUES(?,?,?,?,?)",
            (user_id, "active", days, iso(start), iso(end))
        )
        await db.commit()
    return start, end

async def db_sub_set_expired(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE subscriptions SET status='expired' WHERE user_id=?", (user_id,))
        await db.commit()

async def db_ticket_get(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT user_id, status FROM tickets WHERE user_id=?", (user_id,))
        return await cur.fetchone()

async def db_ticket_open(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO tickets(user_id, status, created_at, closed_at) VALUES(?,?,?,?)",
            (user_id, "open", iso(now_utc()), None)
        )
        await db.commit()

async def db_ticket_close(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tickets SET status='closed', closed_at=? WHERE user_id=?", (iso(now_utc()), user_id))
        await db.commit()

# ✅ Statistika: faqat users count kerak
async def db_users_count() -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        return (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]

async def db_pending_payments(limit=10):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
        SELECT p.id, p.user_id, p.duration_days, p.created_at, u.full_name, u.phone
        FROM payments p
        LEFT JOIN users u ON u.user_id = p.user_id
        WHERE p.status='pending'
        ORDER BY p.id DESC
        LIMIT ?
        """, (limit,))
        return await cur.fetchall()

async def db_open_tickets(limit=20):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
        SELECT t.user_id, t.created_at, u.full_name, u.phone
        FROM tickets t
        LEFT JOIN users u ON u.user_id = t.user_id
        WHERE t.status='open'
        ORDER BY t.created_at DESC
        LIMIT ?
        """, (limit,))
        return await cur.fetchall()

# ✅ MUHIM: faqat ACTIVE obunachilar ro‘yxati
async def db_active_users_page(limit=20, offset=0):
    async with aiosqlite.connect(DB_NAME) as db:
        total = (await (await db.execute("""
            SELECT COUNT(*)
            FROM users u
            JOIN subscriptions s ON s.user_id=u.user_id
            WHERE s.status='active' AND s.end_at IS NOT NULL
        """)).fetchone())[0]

        cur = await db.execute("""
        SELECT
            u.user_id,
            u.full_name,
            u.phone,
            u.created_at,
            (SELECT COUNT(*) FROM users u2 WHERE u2.created_at <= u.created_at) AS global_no,
            s.duration_days,
            s.end_at
        FROM users u
        JOIN subscriptions s ON s.user_id = u.user_id
        WHERE s.status='active' AND s.end_at IS NOT NULL
        ORDER BY s.end_at ASC
        LIMIT ? OFFSET ?
        """, (limit, offset))
        rows = await cur.fetchall()
        return total, rows

# -------------------- EXPIRE JOB --------------------
async def expire_job(bot: Bot):
    while True:
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                cur = await db.execute("""
                SELECT user_id, end_at
                FROM subscriptions
                WHERE status='active' AND end_at IS NOT NULL
                """)
                rows = await cur.fetchall()

            now = now_utc()
            for user_id, end_at in rows:
                try:
                    end_dt = from_iso(end_at) if end_at else None
                except Exception:
                    end_dt = None
                if not end_dt:
                    continue

                if end_dt <= now:
                    try:
                        await bot.ban_chat_member(CHANNEL_ID, user_id)
                        await bot.unban_chat_member(CHANNEL_ID, user_id)
                    except Exception as e:
                        logging.warning(f"Kanaldan chiqarishda xato user={user_id}: {e}")

                    await db_sub_set_expired(user_id)
                    try:
                        await bot.send_message(user_id, "⏳ Obuna muddati tugadi. /start bosib qayta obuna oling.")
                    except Exception:
                        pass

        except Exception as e:
            logging.exception(f"expire_job xatosi: {e}")

        await asyncio.sleep(60 * 60)

# ========================= BOT =========================
async def main():
    await init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    def is_admin(uid: int) -> bool:
        return uid == ADMIN_ID

    @dp.startup()
    async def _startup():
        asyncio.create_task(expire_job(bot))
        logging.info("✅ expire_job ishga tushdi")

    # -------- /myid
    @dp.message(Command("myid"))
    async def myid(msg: Message):
        await msg.answer(f"Sizning ID: {msg.from_user.id}")

    # -------- /start
    @dp.message(CommandStart())
    async def start(msg: Message, state: FSMContext):
        user = await db_user_get(msg.from_user.id)
        if not user:
            await msg.answer("Assalomu alaykum! Ismingizni kiriting ✍️")
            await state.set_state(RegState.waiting_name)
            return

        # name yoki phone yo‘q bo‘lsa davom ettiramiz
        if not user[1]:
            await msg.answer("Ismingizni kiriting ✍️")
            await state.set_state(RegState.waiting_name)
            return

        if not user[2]:
            await msg.answer(
                "Telefon raqamingizni tugma orqali yuboring 👇\n",
                reply_markup=kb_request_phone()
            )
            await state.set_state(RegState.waiting_phone)
            return

        await state.clear()
        await msg.answer("Tanlang 👇", reply_markup=ikb_main())

    # -------- ism
    @dp.message(RegState.waiting_name)
    async def reg_name(msg: Message, state: FSMContext):
        name = (msg.text or "").strip()
        if len(name) <= 2:
            await msg.answer("Ism juda qisqa. Qayta kiriting ✍️")
            return
        await db_user_upsert(msg.from_user.id, full_name=name)
        await msg.answer(
            "Telefon raqamingizni tugma orqali yuboring 👇\n\n",
            reply_markup=kb_request_phone()
        )
        await state.set_state(RegState.waiting_phone)

    # ✅ contact bo‘lsa
    @dp.message(RegState.waiting_phone, F.contact)
    async def reg_phone_contact(msg: Message, state: FSMContext):
        await db_user_upsert(msg.from_user.id, phone=msg.contact.phone_number)
        await state.clear()
        await msg.answer("✅ Muvaffaqiyatli ro‘yxatdan o‘tdingiz!", reply_markup=ReplyKeyboardRemove())
        await msg.answer("Tanlang 👇", reply_markup=ikb_main())

    # ✅ matn bilan telefon (desktop/web uchun)
    @dp.message(RegState.waiting_phone)
    async def reg_phone_text(msg: Message, state: FSMContext):
        text = (msg.text or "").strip().replace(" ", "")
        if not PHONE_RE.match(text):
            await msg.answer(
                "❌ Noto‘g‘ri raqam.\nMasalan: +998901234567\n"
                "Yoki tugma orqali yuboring 👇",
                reply_markup=kb_request_phone()
            )
            return
        await db_user_upsert(msg.from_user.id, phone=text)
        await state.clear()
        await msg.answer("✅ Muvaffaqiyatli ro‘yxatdan o‘tdingiz!", reply_markup=ReplyKeyboardRemove())
        await msg.answer("Pulik kanaliga qoshilish uchun menyudan birini tanlang 👇", reply_markup=ikb_main())

    # -------- back main
    @dp.callback_query(F.data == "back_main")
    async def back_main(call: CallbackQuery, state: FSMContext):
        await state.clear()
        await call.message.answer("pulik kanaliga qoshilish uchun menyudan birini tanlang 👇", reply_markup=ikb_main())
        await call.answer()

    # ================= SUBSCRIPTION =================
    @dp.callback_query(F.data == "buy_sub")
    async def buy_sub(call: CallbackQuery, state: FSMContext):
        await state.clear()
        await call.message.answer("Tariflar birini tanlang 👇", reply_markup=ikb_tariffs())
        await call.answer()

    @dp.callback_query(F.data.startswith("t:"))
    async def choose_tariff(call: CallbackQuery, state: FSMContext):
        days = int(call.data.split(":")[1])

        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        
        await state.update_data(days=days)

        card_text = "5614 6835 1424 8970"

        await call.message.answer(
            f"✅ Siz {TARIFFS[days]['title']} tarifni tanladingiz.\n\n"
            f"💰 Miqdor: {TARIFFS[days]['amount']:,} so'm\n\n"
            "💳 Karta raqami (nusxa olish uchun):\n"
            f"<pre>{card_text}</pre>\n"
            "To'lovni qilib  chekni shu yerga yuboring 📸\n",
            parse_mode="HTML"
        )

        await state.set_state(SubState.waiting_check)
        await call.answer()


    @dp.message(SubState.waiting_check)
    async def get_check(msg: Message, state: FSMContext):
        data = await state.get_data()
        days = int(data.get("days", 30))

        if msg.photo:
            file_type = "photo"
            file_id = msg.photo[-1].file_id
        elif msg.document:
            file_type = "document"
            file_id = msg.document.file_id
        else:
            await msg.answer("Chekni rasm yoki fayl qilib yuboring 📸")
            return

        pay_id = await db_payment_create(msg.from_user.id, days, file_type, file_id)
        await msg.answer("✅ Chek adminga yuborildi. Tasdiqlanishini kuting...")

        user = await db_user_get(msg.from_user.id)
        name = user[1] if user else "(noma'lum)"
        phone = user[2] if user else "(noma'lum)"

        uname = msg.from_user.username
        uname_text = f"@{uname}" if uname else "username yo‘q"
        mention = profile_link(msg.from_user.id, name)

        cap = (
            f"🧾 TO'LOV CHEKI (Payment #{pay_id})\n"
            f"👤 {mention}\n"
            f"🔗 User: {uname_text}\n"
            f"🆔 ID: {msg.from_user.id}\n"
            f"📱 Tel: {phone}\n"
            f"📦 Tarif: {TARIFFS[days]['title']}\n"
            f"💰 {TARIFFS[days]['amount']:,} so'm"
        )

        try:
            if file_type == "photo":
                await msg.bot.send_photo(
                    ADMIN_ID, file_id, caption=cap,
                    reply_markup=ikb_admin_payment(pay_id, msg.from_user.id, days),
                    parse_mode="HTML"
                )
            else:
                await msg.bot.send_document(
                    ADMIN_ID, file_id, caption=cap,
                    reply_markup=ikb_admin_payment(pay_id, msg.from_user.id, days),
                    parse_mode="HTML"
                )
        except Exception as e:
            await msg.answer("⚠️ Adminga yuborilmadi. Admin botga /start qilgan bo‘lishi kerak.\n" + str(e))

        await state.clear()

    @dp.callback_query(F.data.startswith("pay_ok:"))
    async def pay_ok(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            await call.answer("Admin emas", show_alert=True)
            return

        _, pay_id, user_id, days = call.data.split(":")
        pay_id = int(pay_id)
        user_id = int(user_id)
        days = int(days)

        await db_payment_set_status(pay_id, "approved")
        _start, end = await db_sub_set_active(user_id, days)

        try:
            invite = await call.bot.create_chat_invite_link(chat_id=CHANNEL_ID, member_limit=1)
        except Exception:
            await call.answer("❌ Bot kanadal admin emas ", show_alert=True)
            return

        dl = days_left(iso(end))
        end_local = end.astimezone()

        try:
            await call.bot.send_message(
                user_id,
                "✅ To'lov tasdiqlandi!\n"
                f"📦 Tarif: {TARIFFS[days]['title']}\n"
                f"📅 Tugash: {end_local:%Y-%m-%d %H:%M}\n"
                f"⏳ Qolgan: {dl} kun\n\n"
                f"🔗 Kanal link: {invite.invite_link}"
            )
        except Exception:
            pass

        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await call.answer("Tasdiqlandi")

    @dp.callback_query(F.data.startswith("pay_no:"))
    async def pay_no(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            await call.answer("Admin emas", show_alert=True)
            return

        _, pay_id, user_id, _days = call.data.split(":")
        pay_id = int(pay_id)
        user_id = int(user_id)

        await db_payment_set_status(pay_id, "rejected")

        try:
            await call.bot.send_message(user_id, "❌ To'lov rad etildi. shkoyatingiz bolsa admin bilan bog‘laning.")
        except Exception:
            pass

        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await call.answer("Rad etildi")

    # ================= SUPPORT (ticket) =================
    @dp.callback_query(F.data == "support_start")
    async def support_start(call: CallbackQuery, state: FSMContext):
        await db_ticket_open(call.from_user.id)
        await state.set_state(SupportUserState.waiting_message)
        await call.message.answer("Xabaringizni yozing ✍️")
        await call.answer()

    @dp.callback_query(F.data == "user_reply")
    async def user_reply(call: CallbackQuery, state: FSMContext):
        t = await db_ticket_get(call.from_user.id)
        if not t or t[1] != "open":
            await state.clear()
            await call.message.answer("ℹ️ Suhbat yopilgan. /start bosib yangi suhbat boshlang.", reply_markup=ikb_main())
            await call.answer()
            return
        await state.set_state(SupportUserState.waiting_message)
        await call.message.answer("Javobingizni yozing ✍️")
        await call.answer()

    @dp.callback_query(F.data == "support_close")
    async def user_close_support(call: CallbackQuery, state: FSMContext):
        t = await db_ticket_get(call.from_user.id)
        if not t or t[1] != "open":
            await state.clear()
            await call.message.answer("ℹ️ Suhbat allaqachon yopilgan.", reply_markup=ikb_main())
            await call.answer()
            return

        await db_ticket_close(call.from_user.id)
        await state.clear()

        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await call.message.answer("✅ Suhbat yopildi.", reply_markup=ikb_main())
        await call.answer()

    @dp.message(SupportUserState.waiting_message)
    async def support_user_message(msg: Message, state: FSMContext):
        text = (msg.text or "").strip()
        if not text:
            await msg.answer("Iltimos, faqat matn yozing ✍️")
            return

        t = await db_ticket_get(msg.from_user.id)
        if not t or t[1] != "open":
            await state.clear()
            await msg.answer("ℹ️ Suhbat yopilgan. /start bosib yangi suhbat boshlang.", reply_markup=ikb_main())
            return

        user = await db_user_get(msg.from_user.id)
        name = user[1] if user else "(noma'lum)"
        phone = user[2] if user else "(noma'lum)"
        uname = msg.from_user.username
        uname_text = f"@{uname}" if uname else "username yo‘q"

        admin_text = (
            "📩 SUPPORT XABAR\n"
            f"👤 {profile_link(msg.from_user.id, name)}\n"
            f"🔗 User: {uname_text}\n"
            f"🆔 ID: {msg.from_user.id}\n"
            f"📱 Tel: {phone}\n\n"
            f"💬 Xabar:\n{text}"
        )

        await msg.bot.send_message(
            ADMIN_ID,
            admin_text,
            reply_markup=ikb_admin_support(msg.from_user.id),
            parse_mode="HTML"
        )

        await msg.answer("✅ Adminga yuborildi. Javobni kuting.", reply_markup=ikb_user_chat_controls())
        await state.clear()

    @dp.callback_query(F.data.startswith("admin_reply:"))
    async def admin_reply_click(call: CallbackQuery, state: FSMContext):
        if not is_admin(call.from_user.id):
            await call.answer("Admin emas", show_alert=True)
            return

        user_id = int(call.data.split(":")[1])
        await db_ticket_open(user_id)

        await state.update_data(reply_user_id=user_id)
        await state.set_state(SupportAdminState.waiting_reply_text)
        await call.message.answer(f"✍️ User ({user_id}) ga javob yozing:")
        await call.answer()

    @dp.message(SupportAdminState.waiting_reply_text)
    async def admin_send_reply(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return

        data = await state.get_data()
        user_id = int(data["reply_user_id"])
        text = (msg.text or "").strip()
        if not text:
            await msg.answer("Javob matnini yozing ✍️")
            return

        await db_ticket_open(user_id)
        await msg.bot.send_message(
            user_id,
            f"👑 Admin:\n{text}\n\n↩️ Javob qaytarish tugmasini bosing.",
            reply_markup=ikb_user_chat_controls()
        )
        await msg.answer("✅ Javob yuborildi.")
        await state.clear()

    @dp.callback_query(F.data.startswith("admin_close:"))
    async def admin_close_ticket(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            await call.answer("Admin emas", show_alert=True)
            return

        user_id = int(call.data.split(":")[1])
        await db_ticket_close(user_id)

        try:
            await call.bot.send_message(user_id, "✅ Admin suhbatni yopdi. Yangi suhbat uchun /start.", reply_markup=ikb_main())
        except Exception:
            pass

        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await call.answer("Suhbat yopildi")

    # ================= ADMIN PANEL =================
    @dp.message(Command("admin"))
    async def admin_cmd(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        await msg.answer("👑 Admin panel\nTanlang 👇", reply_markup=ikb_admin_panel())

    async def send_active_users_page(call: CallbackQuery, offset: int):
        limit = 20
        total, rows = await db_active_users_page(limit=limit, offset=offset)
        if not rows:
            await call.message.answer("✅ Aktiv obunachi yo‘q.")
            return

        header = f"✅ Aktiv obunachilar: {offset+1}-{min(offset+limit, total)} / {total}"
        lines = [header, ""]

        for idx, (user_id, name, phone, created_at, global_no, d_days, end_at) in enumerate(rows, start=offset+1):
            name = name or "Noma'lum"
            phone = phone or "—"
            dl = days_left(end_at)  # faqat active uchun bor
            left_text = f"⏳ {dl} kun qoldi" if dl is not None else "⏳ —"

            tariff_text = ""
            if d_days and d_days in TARIFFS:
                tariff_text = TARIFFS[d_days]["title"]
            elif d_days:
                tariff_text = f"{d_days} kun"
            else:
                tariff_text = "—"

            lines.append(
                f"#{idx} (Umumiy №{global_no})\n"
                f"👤 {profile_link(user_id, name)}\n"
                f"📱 {phone}\n"
                f"🆔 {user_id}\n"
                f"📦 {tariff_text}\n"
                f"{left_text}\n"
                f"————————————"
            )

        pager = ikb_users_pager(offset, limit, total)
        await call.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=pager, disable_web_page_preview=True)

    @dp.callback_query(F.data.startswith("adm:"))
    async def admin_panel_actions(call: CallbackQuery, state: FSMContext):
        if not is_admin(call.from_user.id):
            await call.answer("Admin emas", show_alert=True)
            return

        act = call.data.split(":")[1]

        # ✅ faqat users count
        if act == "stats":
            u = await db_users_count()
            await call.message.answer(f"📊 Botdagi foydalanuvchilar soni: {u} ta")

        # ✅ faqat active obunachilar
        elif act == "users":
            await send_active_users_page(call, offset=0)

        elif act == "pending":
            rows = await db_pending_payments(10)
            if not rows:
                await call.message.answer("Pending to'lov yo‘q.")
            else:
                lines = ["🧾 Pending to'lovlar (oxirgi 10):"]
                for pid, uid, d_days, created, name, phone in rows:
                    ttxt = TARIFFS[d_days]["title"] if d_days in TARIFFS else f"{d_days} kun"
                    lines.append(f"- Payment#{pid} | {name} | {phone} | {uid} | {ttxt} | {created}")
                await call.message.answer("\n".join(lines))

        elif act == "tickets":
            rows = await db_open_tickets(20)
            if not rows:
                await call.message.answer("Ochiq suhbat yo‘q.")
            else:
                lines = ["📩 Ochiq suhbatlar:"]
                for uid, created, name, phone in rows:
                    lines.append(f"- {name} | {phone} | {uid} | {created}")
                await call.message.answer("\n".join(lines))

        elif act == "send":
            await state.clear()
            await state.set_state(AdminSendState.waiting_user_id)
            await call.message.answer("User ID ni yuboring (faqat raqam).")

        await call.answer()

    @dp.callback_query(F.data.startswith("adm_users:"))
    async def admin_users_pagination(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            await call.answer("Admin emas", show_alert=True)
            return
        offset = int(call.data.split(":")[1])
        await send_active_users_page(call, offset=offset)
        await call.answer()

    @dp.message(AdminSendState.waiting_user_id)
    async def admin_send_user_id(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        txt = (msg.text or "").strip()
        if not txt.isdigit():
            await msg.answer("❌ Noto‘g‘ri. User ID faqat raqam bo‘lsin.")
            return
        user_id = int(txt)
        if not await db_user_exists(user_id):
            await msg.answer("❌ Bunday user topilmadi (u hali /start qilmagan bo‘lishi mumkin).")
            return

        await state.update_data(target_user_id=user_id)
        await state.set_state(AdminSendState.waiting_text)
        await msg.answer("Endi userga yuboriladigan xabarni yozing ✍️")

    @dp.message(AdminSendState.waiting_text)
    async def admin_send_text(msg: Message, state: FSMContext):
        if not is_admin(msg.from_user.id):
            return
        text = (msg.text or "").strip()
        if not text:
            await msg.answer("Xabar bo‘sh bo‘lmasin.")
            return

        data = await state.get_data()
        user_id = int(data["target_user_id"])

        # xabar yuborish ticketni ochadi (javob qaytarish uchun)
        await db_ticket_open(user_id)

        try:
            await msg.bot.send_message(
                user_id,
                f"👑 Admin:\n{text}\n\n↩️ Javob qaytarish tugmasini bosing.",
                reply_markup=ikb_user_chat_controls()
            )
            await msg.answer("✅ Userga yuborildi.")
        except Exception as e:
            await msg.answer(f"❌ Yuborilmadi: {e}")

        await state.clear()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
