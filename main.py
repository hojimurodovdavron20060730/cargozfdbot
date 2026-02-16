import os
import re
import sqlite3
import logging
import asyncio
import aiohttp
from datetime import datetime
from contextlib import contextmanager

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("7928409243:AAFKoLy7sA-Lu41KlV0CjS6NFzkpyCP9p30")
ADMINS = [5729543653]
PORT = int(os.environ.get("PORT", "5000"))

# ================= DB =================
DB_PATH = "cargo.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track TEXT UNIQUE,
            user_id INTEGER DEFAULT NULL,
            status TEXT DEFAULT 'В пути',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            lang TEXT DEFAULT 'ru',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.commit()

init_db()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        yield conn.cursor()
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        conn.close()

def db_upsert_user(user_id: int, username: str = None, lang: str = None):
    with get_db() as cur:
        cur.execute("INSERT OR IGNORE INTO users (user_id, username, lang) VALUES (?, ?, 'ru')", (user_id, username))
        if lang:
            cur.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id))
        if username:
            cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))

def db_get_lang(user_id: int) -> str:
    with get_db() as cur:
        cur.execute("SELECT lang FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else "ru"

# ================= MEMORY STATE =================
awaiting_track: dict[int, bool] = {}
calc_mode: dict[int, str] = {}
admin_state: dict[int, str] = {}

# ================= TRANSLATIONS =================
def t(lang, ru, tj, uz):
    return {"ru": ru, "tj": tj, "uz": uz}.get(lang, ru)

# ================= KEYBOARDS =================
def get_lang_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🇷🇺 RU"), KeyboardButton(text="🇹🇯 TJ"), KeyboardButton(text="🇺🇿 UZ")]],
        resize_keyboard=True
    )

def main_menu(lang):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "📦 Проверить трек", "📦 Санҷиши трек", "📦 Trek tekshirish"))],
            [KeyboardButton(text=t(lang, "📂 Мои треки", "📂 Трекҳои ман", "📂 Mening treklarim"))],
            [KeyboardButton(text=t(lang, "💰 Калькулятор", "💰 Ҳисобкунак", "💰 Kalkulyator"))],
            [KeyboardButton(text=t(lang, "⚙️ Настройки", "⚙️ Танзимот", "⚙️ Sozlamalar"))],
            [KeyboardButton(text=t(lang, "🚫 Запрещённые товары", "🚫 Молҳои манъшуда", "🚫 Taqiqlangan mahsulotlar"))],
            [KeyboardButton(text=t(lang, "📍 Информация", "📍 Маълумот", "📍 Ma’lumot"))],
        ],
        resize_keyboard=True
    )

def info_menu(lang):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "📦 Тарифы", "📦 Нархҳо", "📦 Tariflar"))],
            [KeyboardButton(text=t(lang, "🇨🇳 Адрес Китая", "🇨🇳 Суроғаи Чин", "🇨🇳 Xitoy manzili"))],
            [KeyboardButton(text=t(lang, "📍 Пункт выдачи", "📍 Ҷои супориш", "📍 Topshirish punkti"))],
            [KeyboardButton(text=t(lang, "☎️ Оператор", "☎️ Оператор", "☎️ Operator"))],
            [KeyboardButton(text=t(lang, "🔙 Назад", "🔙 Бозгашт", "🔙 Orqaga"))],
        ],
        resize_keyboard=True
    )

def calc_menu(lang):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "⚖️ По кг", "⚖️ Бо кг", "⚖️ Kg bo'yicha"))],
            [KeyboardButton(text=t(lang, "📦 По кубу", "📦 Бо куб", "📦 Kub bo'yicha"))],
            [KeyboardButton(text=t(lang, "🔙 Назад", "🔙 Бозгашт", "🔙 Orqaga"))]
        ],
        resize_keyboard=True
    )

# ================= HELPERS =================
MENU_BUTTONS = {
    "📦 Проверить трек", "📦 Санҷиши трек", "📦 Trek tekshirish",
    "📂 Мои треки", "📂 Трекҳои ман", "📂 Mening treklarim",
    "💰 Калькулятор", "💰 Ҳисобкунак", "💰 Kalkulyator",
    "⚙️ Настройки", "⚙️ Танзимот", "⚙️ Sozlamalar",
    "🚫 Запрещённые товары", "🚫 Молҳои манъшуда", "🚫 Taqiqlangan mahsulotlar",
    "📍 Информация", "📍 Маълумот", "📍 Ma’lumot",
    "🔙 Назад", "🔙 Бозгашт", "🔙 Orqaga",
    "⚖️ По кг", "⚖️ Бо кг", "⚖️ Kg bo'yicha",
    "📦 По кубу", "📦 Бо куб", "📦 Kub bo'yicha",
    "📦 Тарифы", "📦 Нархҳо", "📦 Tariflar",
    "🇨🇳 Адрес Китая", "🇨🇳 Суроғаи Чин", "🇨🇳 Xitoy manzili",
    "📍 Пункт выдачи", "📍 Ҷои супориш", "📍 Topshirish punkti",
    "☎️ Оператор", "🇷🇺 RU", "🇹🇯 TJ", "🇺🇿 UZ", "📊 Статистика", "📢 Рассылка", "❌ Удалить трек", "➕ Добавить трек"
}

def looks_like_track(text: str) -> bool:
    text = text.strip()
    return bool(re.fullmatch(r"[A-Za-z0-9\-_]{5,}", text))

# ================= BOT =================
bot = None
dp = Dispatcher()

# ================= HANDLERS =================
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not msg.from_user: return
    db_upsert_user(msg.from_user.id, msg.from_user.username)
    await msg.answer("👋 Выберите язык / Забонро интихоб кунед / Tilni tanlang:", reply_markup=get_lang_kb())

@dp.message(F.text.in_(["🇷🇺 RU", "🇹🇯 TJ", "🇺🇿 UZ"]))
async def set_lang(msg: Message):
    if not msg.from_user: return
    lang = "ru"
    if "TJ" in msg.text: lang = "tj"
    elif "UZ" in msg.text: lang = "uz"
    db_upsert_user(msg.from_user.id, msg.from_user.username, lang)
    await msg.answer("✅ OK", reply_markup=main_menu(lang))

@dp.message(F.text.in_(["⚙️ Настройки", "⚙️ Танзимот", "⚙️ Sozlamalar"]))
async def settings_menu(msg: Message):
    if not msg.from_user: return
    await msg.answer("⚙️", reply_markup=get_lang_kb())

@dp.message(F.text.in_(["📦 Проверить трек", "📦 Санҷиши трек", "📦 Trek tekshirish"]))
async def ask_track(msg: Message):
    if not msg.from_user: return
    lang = db_get_lang(msg.from_user.id)
    awaiting_track[msg.from_user.id] = True
    await msg.answer(t(lang, "✍️ Введите трек-номера (каждый с новой строки):", "✍️ Рақамҳоро ворид кунед:", "✍️ Trek raqamlarini kiriting:"))

@dp.message(lambda m: awaiting_track.get(m.from_user.id) if m.from_user else False)
async def process_track_input(msg: Message):
    if not msg.from_user or not msg.text: return
    if msg.text in MENU_BUTTONS:
        awaiting_track[msg.from_user.id] = False
        return
    
    lang = db_get_lang(msg.from_user.id)
    lines = msg.text.strip().splitlines()
    results = []
    with get_db() as cur:
        for line in lines:
            track_code = line.strip().upper()
            if not track_code or not looks_like_track(track_code): continue
            cur.execute("SELECT status, user_id FROM tracks WHERE track=?", (track_code,))
            row = cur.fetchone()
            if row:
                status, existing_uid = row[0], row[1]
                if not existing_uid:
                    cur.execute("UPDATE tracks SET user_id=? WHERE track=?", (msg.from_user.id, track_code))
                results.append(f"📦 `{track_code}`: *{status}*")
            else:
                results.append(f"❌ `{track_code}`: {t(lang, 'не найден', 'ёфт нашуд', 'topilmadi')}")
    
    awaiting_track[msg.from_user.id] = False
    await msg.answer("\n".join(results) or "❌ Error", parse_mode="Markdown")

@dp.message(F.text.in_(["📂 Мои треки", "📂 Трекҳои ман", "📂 Mening treklarim"]))
async def my_tracks(msg: Message):
    if not msg.from_user: return
    lang = db_get_lang(msg.from_user.id)
    with get_db() as cur:
        cur.execute("SELECT track, status, updated_at FROM tracks WHERE user_id=? ORDER BY updated_at DESC", (msg.from_user.id,))
        rows = cur.fetchall()
    if not rows:
        await msg.answer(t(lang, "📭 У вас пока нет сохраненных треков.", "📭 Шумо то ҳол трекҳои захирашуда надоред.", "📭 Sizda hali saqlangan treklar yo'q."))
        return
    text = "\n".join([f"📦 `{tr}` — *{st}*" for tr, st, _ in rows])
    await msg.answer(text, parse_mode="Markdown")

@dp.message(F.text.in_(["💰 Калькулятор", "💰 Ҳисобкунак", "💰 Kalkulyator"]))
async def open_calc(msg: Message):
    if not msg.from_user: return
    lang = db_get_lang(msg.from_user.id)
    await msg.answer(t(lang, "Выберите тип расчета:", "Навъи ҳисобро интихоб кунед:", "Hisoblash turini tanlang:"), reply_markup=calc_menu(lang))

@dp.message(F.text.in_(["⚖️ По кг", "⚖️ Бо кг", "⚖️ Kg bo'yicha"]))
async def calc_kg_start(msg: Message):
    if not msg.from_user: return
    calc_mode[msg.from_user.id] = "kg"
    await msg.answer(t(db_get_lang(msg.from_user.id), "Введите вес в кг (например: 1.5):", "Вазнро бо кг ворид кунед:", "Vaznni kgda kiriting:"))

@dp.message(F.text.in_(["📦 По кубу", "📦 Бо куб", "📦 Kub bo'yicha"]))
async def calc_cube_start(msg: Message):
    if not msg.from_user: return
    calc_mode[msg.from_user.id] = "cube"
    await msg.answer(t(db_get_lang(msg.from_user.id), "Введите объем в м³ (например: 0.5):", "Ҳаҷмро бо м³ ворид кунед:", "Hajmni m³da kiriting:"))

@dp.message(lambda m: calc_mode.get(m.from_user.id) if m.from_user else False)
async def process_calc(msg: Message):
    if not msg.from_user or not msg.text: return
    if msg.text in MENU_BUTTONS:
        calc_mode.pop(msg.from_user.id, None)
        return
    try:
        val = float(msg.text.replace(",", "."))
        mode = calc_mode.pop(msg.from_user.id)
        if mode == "kg":
            price = 30 if val <= 30 else 28
            res = val * price
            await msg.answer(f"⚖️ Вес: {val} кг\n💰 Цена: {price} смн/кг\n📊 Итого: *{res:.2f} смн*", parse_mode="Markdown")
        else:
            res = val * 280
            await msg.answer(f"📦 Объем: {val} м³\n💰 Цена: 280 $/м³\n📊 Итого: *{res:.2f} $*", parse_mode="Markdown")
    except:
        await msg.answer("❌ Пожалуйста, введите число.")

@dp.message(F.text.in_(["📍 Информация", "📍 Маълумот", "📍 Ma’lumot"]))
async def info_main(msg: Message):
    if not msg.from_user: return
    await msg.answer("ℹ️", reply_markup=info_menu(db_get_lang(msg.from_user.id)))

@dp.message(F.text.in_(["📦 Тарифы", "📦 Нархҳо", "📦 Tariflar"]))
async def tariffs(msg: Message):
    if not msg.from_user: return
    lang = db_get_lang(msg.from_user.id)
    text = t(
        lang,
        "📦 *ТАРИФЫ*\n\n⚖️ *По весу:*\n▪️ До 30 кг — 30 смн/кг\n▪️ От 31 кг — 28 смн/кг\n\n📦 *По объему:*\n▪️ 1 м³ — 280$",
        "📦 *НАРХҲО*\n\n⚖️ *Бо вазн:*\n▪️ То 30 кг — 30 смн/кг\n▪️ Аз 31 кг — 28 смн/кг\n\n📦 *Бо ҳаҷм:*\n▪️ 1 м³ — 280$",
        "📦 *TARIFLAR*\n\n⚖️ *Vazn bo'yicha:*\n▪️ 30 kg gacha — 30 smn/kg\n▪️ 31 kg dan — 28 smn/kg\n\n📦 *Hajm bo'yicha:*\n▪️ 1 m³ — 280$"
    )
    await msg.answer(text, parse_mode="Markdown")

@dp.message(F.text.in_(["🇨🇳 Адрес Китая", "🇨🇳 Суроғаи Чин", "🇨🇳 Xitoy manzili"]))
async def china_address(msg: Message):
    await msg.answer(
        "🇨🇳 *АДРЕС В КИТАЕ*\n\n"
        "收货人: LLC\n"
        "手机号: 18144746943\n"
        "地址: 广州市荔湾区站前路19号A21档868仓库2房间\n"
        "ZFD",
        parse_mode="Markdown"
    )

@dp.message(F.text.in_(["📍 Пункт выдачи", "📍 Ҷои супориш", "📍 Topshirish punkti"]))
async def pickup(msg: Message):
    if not msg.from_user: return
    await msg.answer(
        t(
            db_get_lang(msg.from_user.id),
            "📍 *ПУНКТ ВЫДАЧИ*\nУл. Рудаки около автовокзала",
            "📍 *ҶОИ СУПОРИШ*\nк. Рудаки назди автовакзал",
            "📍 *TOPSHIRISH PUNKTI*\nRudaki ko‘chasi, avtovokzal yonida"
        ),
        parse_mode="Markdown"
    )

@dp.message(F.text.in_(["☎️ Оператор", "☎️ Operator"]))
async def operator(msg: Message):
    await msg.answer("☎️ *СВЯЗЬ С ОПЕРАТОРОМ*\n\n📞 +992406374444\n👤 @Zfdcargoadmin", parse_mode="Markdown")

@dp.message(F.text.in_(["🚫 Запрещённые товары", "🚫 Молҳои манъшуда", "🚫 Taqiqlangan mahsulotlar"]))
async def forbidden(msg: Message):
    if not msg.from_user: return
    lang = db_get_lang(msg.from_user.id)
    text = t(
        lang,
        "🚫 *ЗАПРЕЩЁННЫЕ ТОВАРЫ!*\n\n▪️ Лекарства\n▪️ Жидкости\n▪️ Оружие\n▪️ Кальяны\n▪️ Электроника (по договору)\n▪️ Хрупкие товары",
        "🚫 *МОЛҲОИ МАНЪШУДА!*\n\n▪️ Доруҳо\n▪️ Моеъҳо\n▪️ Силоҳ\n▪️ Калянҳо\n▪️ Электроника (бо шартнома)",
        "🚫 *TAQIQLANGAN MAHSULOTLAR!*\n\n▪️ Dorilar\n▪️ Suyuqliklar\n▪️ Qurollar\n▪️ Kalyandlar\n▪️ Elektronika (shartnoma bo'yicha)"
    )
    await msg.answer(text, parse_mode="Markdown")

# ================= ADMIN =================
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if not msg.from_user or msg.from_user.id not in ADMINS: return
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="➕ Добавить трек"), KeyboardButton(text="❌ Удалить трек")],
            [KeyboardButton(text="🔙 Назад")]
        ],
        resize_keyboard=True
    )
    await msg.answer("🛠 *Панель администратора*", reply_markup=kb, parse_mode="Markdown")

@dp.message(F.text == "📊 Статистика")
async def admin_stats(msg: Message):
    if not msg.from_user or msg.from_user.id not in ADMINS: return
    with get_db() as cur:
        cur.execute("SELECT COUNT(*) FROM tracks"); t_tr = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users"); t_us = cur.fetchone()[0]
    await msg.answer(f"📊 *Статистика*\n\n📦 Треков: {t_tr}\n👤 Пользователей: {t_us}", parse_mode="Markdown")

@dp.message(F.text == "📢 Рассылка")
async def broadcast_start(msg: Message):
    if not msg.from_user or msg.from_user.id not in ADMINS: return
    admin_state[msg.from_user.id] = "broadcast"
    await msg.answer("📝 Введите текст для рассылки всем пользователям:")

@dp.message(F.text == "➕ Добавить трек")
async def admin_add_track_start(msg: Message):
    if not msg.from_user or msg.from_user.id not in ADMINS: return
    await msg.answer("Используйте команду:\n`/addtrack ТРЕК СТАТУС`", parse_mode="Markdown")

@dp.message(F.text == "❌ Удалить трек")
async def delete_start(msg: Message):
    if not msg.from_user or msg.from_user.id not in ADMINS: return
    admin_state[msg.from_user.id] = "delete"
    await msg.answer("🗑 Введите трек-номер для удаления:")

@dp.message(lambda m: admin_state.get(m.from_user.id) if m.from_user else False)
async def process_admin(msg: Message):
    if not msg.from_user or not msg.text or msg.from_user.id not in ADMINS: return
    if msg.text in MENU_BUTTONS:
        admin_state.pop(msg.from_user.id, None)
        return
    
    action = admin_state.pop(msg.from_user.id)
    if action == "broadcast":
        with get_db() as cur:
            cur.execute("SELECT user_id FROM users")
            uids = [r[0] for r in cur.fetchall()]
        sent = 0
        for uid in uids:
            try: await bot.send_message(uid, msg.text); sent += 1; await asyncio.sleep(0.05)
            except: pass
        await msg.answer(f"✅ Рассылка завершена. Получили: {sent}")
    elif action == "delete":
        track = msg.text.upper().strip()
        with get_db() as cur: 
            cur.execute("DELETE FROM tracks WHERE track=?", (track,))
        await msg.answer(f"🗑 Трек `{track}` удален.", parse_mode="Markdown")

@dp.message(Command("addtrack"))
async def add_track(msg: Message):
    if not msg.from_user or msg.from_user.id not in ADMINS: return
    p = msg.text.split(maxsplit=2)
    if len(p) < 3:
        await msg.answer("Формат: `/addtrack ТРЕК СТАТУС`", parse_mode="Markdown")
        return
    tr, st = p[1].upper(), p[2]
    with get_db() as cur:
        cur.execute("SELECT user_id FROM tracks WHERE track=?", (tr,))
        row = cur.fetchone(); uid = row[0] if row else None
        cur.execute("INSERT OR REPLACE INTO tracks (track, status, user_id, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)", (tr, st, uid))
    if uid:
        try: await bot.send_message(uid, f"🔔 *Обновление статуса*\n\n📦 Трек: `{tr}`\n✅ Статус: *{st}*", parse_mode="Markdown")
        except: pass
    await msg.answer(f"✅ Трек `{tr}` обновлен.", parse_mode="Markdown")

@dp.message(F.document)
async def upload_txt(msg: Message):
    if not msg.from_user or msg.from_user.id not in ADMINS: return
    try:
        info = await bot.get_file(msg.document.file_id)
        f = await bot.download_file(info.file_path)
        content = f.read().decode("utf-8", errors="ignore")
        count = 0
        with get_db() as cur:
            for line in content.splitlines():
                tr = line.strip().upper()
                if tr and looks_like_track(tr):
                    cur.execute("INSERT OR IGNORE INTO tracks (track, status) VALUES (?, 'В пути')", (tr,))
                    count += 1
        await msg.answer(f"✅ Загружено {count} новых треков.")
    except Exception as e: await msg.answer(f"❌ Ошибка: {e}")

@dp.message(F.text.in_(["🔙 Назад", "🔙 Бозгашт", "🔙 Orqaga"]))
async def go_back(msg: Message):
    if not msg.from_user: return
    await msg.answer("🏠", reply_markup=main_menu(db_get_lang(msg.from_user.id)))

# ================= SERVER =================
async def run_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        logger.info(f"Server started on port {PORT}")
    except OSError:
        logger.warning(f"Port {PORT} is already in use, skipping server start.")

async def main():
    global bot
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing")
        await run_server()
        await asyncio.Event().wait()
        return
    
    bot = Bot(BOT_TOKEN)
    await run_server()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Starting polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
