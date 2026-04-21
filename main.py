<<<<<<< HEAD
import asyncio
import logging
import os
import re
import httpx
import json
import sqlite3
import csv
import io
import base64
import html
import secrets
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, LabeledPrice, PreCheckoutQuery
)
from aiogram.enums import ChatAction, ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Загрузка переменных ─────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YANDEX_KEY = os.getenv("YANDEX_API_KEY", "")
YANDEX_FOLDER = os.getenv("YANDEX_FOLDER_ID", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
BOT_USERNAME = os.getenv("BOT_USERNAME", "nutrimind_bot")
CHANNEL = os.getenv("CHANNEL", "https://t.me/NutriMindi")
WATER_GOAL = 2000

# Ссылка на твой Mini App
MINI_APP_URL = "https://maxi0154.github.io/nutrimind/"

# ─── Цены ───────────────────────────────────────────────────────────────────
PRICES = {
    "1m": {"old": "249₽", "new": "149₽", "days": 30, "per": "4₽/день", "stars": 120},
    "3m": {"old": "599₽", "new": "299₽", "days": 90, "per": "3₽/день", "stars": 299},
    "1y": {"old": "1990₽", "new": "990₽", "days": 365, "per": "2.7₽/день", "stars": 990},
}

# ─── Логгер ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ─── Глобальные ─────────────────────────────────────────────────────────────
_nutrition_cache: dict = {}
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()
DB_PATH = "data/nutrimind.db"

def e(text):
    return html.escape(str(text)) if text else ""

# ─── БД ─────────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    
    # Таблицы
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, username TEXT,
        is_pro INTEGER DEFAULT 0, pro_until TEXT,
        warned_3d INTEGER DEFAULT 0, warned_1d INTEGER DEFAULT 0,
        ref_code TEXT, referred_by INTEGER,
        onboarded INTEGER DEFAULT 0, trial_used INTEGER DEFAULT 0,
        streak_days INTEGER DEFAULT 0, streak_last TEXT,
        created_at TEXT DEFAULT(datetime('now')))""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS food_logs(
        id INTEGER PRIMARY KEY, user_id INTEGER, meal_type TEXT, food_name TEXT,
        grams REAL, kcal INTEGER, protein REAL, fat REAL, carbs REAL, recorded_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS user_profile(
        user_id INTEGER PRIMARY KEY, current_weight REAL, target_weight REAL,
        height INTEGER, age INTEGER, gender TEXT, updated_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS symptoms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, symptom TEXT, note TEXT, recorded_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS workouts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, workout_type TEXT, note TEXT, recorded_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS water_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, amount_ml INTEGER, recorded_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS weight_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, weight REAL, recorded_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT)""")
    c.execute("INSERT OR IGNORE INTO config(key,value) VALUES('founders_count','0')")
    
    # Индексы
    c.execute("CREATE INDEX IF NOT EXISTS idx_food ON food_logs(user_id,recorded_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sym ON symptoms(user_id,recorded_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_wrk ON workouts(user_id,recorded_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_wat ON water_logs(user_id,recorded_at)")
    
    # Миграции для старых БД
    migrations = [
        "ALTER TABLE users ADD COLUMN onboarded INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN trial_used INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN streak_days INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN streak_last TEXT",
        "ALTER TABLE users ADD COLUMN ref_code TEXT",
        "ALTER TABLE users ADD COLUMN referred_by INTEGER",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except:
            pass
    
    conn.commit()
    conn.close()
    logger.info("✅ БД инициализирована")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def ensure_user(uid, uname=None):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users(user_id,username,ref_code) VALUES(?,?,?)",
        (uid, uname or "", secrets.token_hex(4))
    )
    if uname:
        conn.execute("UPDATE users SET username=? WHERE user_id=?", (uname, uid))
    conn.commit()
    conn.close()

def get_user(uid):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row

def get_sub(uid):
    conn = db()
    row = conn.execute("SELECT is_pro,pro_until FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if not row or not row[0]:
        return {"active": False, "days_left": 0}
    if not row[1]:
        return {"active": False, "days_left": 0}
    try:
        end = datetime.strptime(row[1], "%Y-%m-%d %H:%M")
        diff = (end - datetime.now()).days
        if diff < 0:
            deactivate_pro(uid)
            return {"active": False, "days_left": 0}
        return {"active": True, "until": row[1], "days_left": max(0, diff)}
    except:
        return {"active": False, "days_left": 0}

def activate_pro(uid, days=30):
    conn = db()
    row = conn.execute("SELECT pro_until FROM users WHERE user_id=?", (uid,)).fetchone()
    base = datetime.now()
    if row and row[0]:
        try:
            ex = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
            if ex > base:
                base = ex
        except:
            pass
    until = (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "UPDATE users SET is_pro=1,pro_until=?,warned_3d=0,warned_1d=0 WHERE user_id=?",
        (until, uid)
    )
    conn.commit()
    conn.close()
    return until

def deactivate_pro(uid):
    conn = db()
    conn.execute("UPDATE users SET is_pro=0,pro_until=NULL WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def activate_trial(uid):
    conn = db()
    row = conn.execute("SELECT trial_used FROM users WHERE user_id=?", (uid,)).fetchone()
    if row and row[0]:
        conn.close()
        return None
    conn.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()
    return activate_pro(uid, 3)

def get_founder_status():
    conn = db()
    row = conn.execute("SELECT value FROM config WHERE key='founders_count'").fetchone()
    conn.close()
    count = int(row[0]) if row else 0
    return count < 100, count, 100

def increment_founder():
    conn = db()
    conn.execute("UPDATE config SET value=CAST(value AS INTEGER)+1 WHERE key='founders_count'")
    conn.commit()
    conn.close()

def delete_user_data(uid):
    conn = db()
    for table in ["food_logs", "user_profile", "symptoms", "workouts", "water_logs", "weight_logs", "users"]:
        conn.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def save_log(uid, meal, name, grams=0, kcal=0, p=0, f=0, c_=0, date=None):
    ensure_user(uid)
    conn = db()
    conn.execute(
        "INSERT INTO food_logs(user_id,meal_type,food_name,grams,kcal,protein,fat,carbs,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, meal, str(name).strip(), grams, kcal, p, f, c_, date or datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    _update_streak(uid)
    return lid

def _update_streak(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = db()
    row = conn.execute("SELECT streak_days,streak_last FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        conn.close()
        return
    streak, last = row
    streak = streak or 0
    if last == today:
        conn.close()
        return
    streak = (streak + 1) if last == yesterday else 1
    conn.execute("UPDATE users SET streak_days=?,streak_last=? WHERE user_id=?", (streak, today, uid))
    conn.commit()
    conn.close()

def get_streak(uid):
    conn = db()
    row = conn.execute("SELECT streak_days,streak_last FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if not row or not row[1]:
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return (row[0] or 0) if row[1] in (today, yesterday) else 0

def update_log(lid, name, grams, kcal, p, f, c_):
    conn = db()
    conn.execute(
        "UPDATE food_logs SET food_name=?,grams=?,kcal=?,protein=?,fat=?,carbs=? WHERE id=?",
        (name, grams, kcal, p, f, c_, lid)
    )
    conn.commit()
    conn.close()

def save_symptom(uid, symptom, note=""):
    conn = db()
    conn.execute(
        "INSERT INTO symptoms(user_id,symptom,note,recorded_at) VALUES(?,?,?,?)",
        (uid, symptom, note, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()

def save_workout(uid, wtype, note=""):
    conn = db()
    conn.execute(
        "INSERT INTO workouts(user_id,workout_type,note,recorded_at) VALUES(?,?,?,?)",
        (uid, wtype, note, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()

def log_water(uid, ml):
    conn = db()
    conn.execute(
        "INSERT INTO water_logs(user_id,amount_ml,recorded_at) VALUES(?,?,?)",
        (uid, ml, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()

def get_today_water(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = db()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_ml),0) FROM water_logs WHERE user_id=? AND recorded_at>=?",
        (uid, today)
    ).fetchone()
    conn.close()
    return row[0] if row else 0

def log_weight(uid, w):
    conn = db()
    conn.execute(
        "INSERT INTO weight_logs(user_id,weight,recorded_at) VALUES(?,?,?)",
        (uid, w, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.execute("UPDATE user_profile SET current_weight=? WHERE user_id=?", (w, uid))
    conn.commit()
    conn.close()

def get_weight_history(uid, limit=7):
    conn = db()
    rows = conn.execute(
        "SELECT weight,recorded_at FROM weight_logs WHERE user_id=? ORDER BY recorded_at DESC LIMIT ?",
        (uid, limit)
    ).fetchall()
    conn.close()
    return rows

def get_symptoms(uid, days=None):
    conn = db()
    if days:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT symptom,note,recorded_at FROM symptoms WHERE user_id=? AND recorded_at>=? ORDER BY recorded_at DESC",
            (uid, start)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT symptom,note,recorded_at FROM symptoms WHERE user_id=? ORDER BY recorded_at DESC",
            (uid,)
        ).fetchall()
    conn.close()
    return rows

def get_workouts(uid, days=None):
    conn = db()
    if days:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT workout_type,note,recorded_at FROM workouts WHERE user_id=? AND recorded_at>=? ORDER BY recorded_at DESC",
            (uid, start)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT workout_type,note,recorded_at FROM workouts WHERE user_id=? ORDER BY recorded_at DESC",
            (uid,)
        ).fetchall()
    conn.close()
    return rows

def save_profile(uid, weight=None, target=None, height=None, age=None, gender=None):
    ensure_user(uid)
    conn = db()
    exists = conn.execute("SELECT 1 FROM user_profile WHERE user_id=?", (uid,)).fetchone()
    if exists:
        conn.execute(
            """UPDATE user_profile SET
            current_weight=COALESCE(?,current_weight), target_weight=COALESCE(?,target_weight),
            height=COALESCE(?,height), age=COALESCE(?,age), gender=COALESCE(?,gender), updated_at=?
            WHERE user_id=?""",
            (weight, target, height, age, gender, datetime.now().strftime("%Y-%m-%d"), uid)
        )
    else:
        conn.execute(
            "INSERT INTO user_profile(user_id,current_weight,target_weight,height,age,gender,updated_at) VALUES(?,?,?,?,?,?,?)",
            (uid, weight, target, height, age, gender, datetime.now().strftime("%Y-%m-%d"))
        )
    conn.commit()
    conn.close()

def upd_profile_field(uid, field, val):
    field_map = {"weight": "current_weight", "target": "target_weight", "height": "height", "age": "age"}
    dbf = field_map.get(field)
    if not dbf:
        return
    conn = db()
    conn.execute(f"UPDATE user_profile SET {dbf}=? WHERE user_id=?", (val, uid))
    conn.commit()
    conn.close()

def get_profile(uid):
    conn = db()
    row = conn.execute("SELECT * FROM user_profile WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row

def get_logs(uid, days=None):
    conn = db()
    if days:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM food_logs WHERE user_id=? AND recorded_at>=? ORDER BY recorded_at DESC",
            (uid, start)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM food_logs WHERE user_id=? ORDER BY recorded_at DESC",
            (uid,)
        ).fetchall()
    conn.close()
    return rows

def get_today_logs(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = db()
    rows = conn.execute(
        "SELECT * FROM food_logs WHERE user_id=? AND recorded_at>=? ORDER BY recorded_at",
        (uid, today)
    ).fetchall()
    conn.close()
    return rows

def get_stats(rows):
    return {
        "kcal": sum(r[5] or 0 for r in rows),
        "p": sum(r[6] or 0 for r in rows),
        "f": sum(r[7] or 0 for r in rows),
        "c": sum(r[8] or 0 for r in rows),
        "count": len(rows)
    }

def get_all_logs_csv(uid):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM food_logs WHERE user_id=? ORDER BY recorded_at DESC",
        (uid,)
    ).fetchall()
    conn.close()
    return rows

def get_all_pro():
    conn = db()
    rows = conn.execute(
        "SELECT user_id,username,pro_until FROM users WHERE is_pro=1 ORDER BY pro_until"
    ).fetchall()
    conn.close()
    return rows

def get_counts():
    conn = db()
    total = conn.execute("SELECT COUNT() FROM users").fetchone()[0]
    pro = conn.execute("SELECT COUNT() FROM users WHERE is_pro=1").fetchone()[0]
    conn.close()
    return total, pro

def get_ref_code(uid):
    conn = db()
    row = conn.execute("SELECT ref_code FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row[0] if row else None

def find_by_ref(code):
    conn = db()
    row = conn.execute("SELECT user_id FROM users WHERE ref_code=?", (code,)).fetchone()
    conn.close()
    return row[0] if row else None

def set_referred(uid, ref_uid):
    conn = db()
    conn.execute(
        "UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL",
        (ref_uid, uid)
    )
    conn.commit()
    conn.close()

# ─── Расчёты ────────────────────────────────────────────────────────────────
def calc_tdee(profile):
    if not profile or not profile[1]:
        return None, None
    g = profile[5] or "male"
    age = profile[4] or 25
    h = profile[3] or 170
    w = profile[1]
    t = profile[2]
    bmr = 10 * w + 6.25 * h - 5 * age + (5 if g == "male" else -161)
    maint = int(bmr * 1.375)
    if t and t < w:
        goal = max(1200, maint - min(500, max(200, int((w - t) * 25))))
    elif t and t > w:
        goal = maint + 200
    else:
        goal = maint
    return maint, goal

def calc_macros(kcal):
    return int(kcal * 0.25 / 4), int(kcal * 0.30 / 9), int(kcal * 0.45 / 4)

def water_bar(total):
    pct = min(100, int(total / WATER_GOAL * 100))
    return "🟦" * (pct // 10) + "⬜" * (10 - pct // 10), pct

def streak_msg(days):
    if days >= 30:
        return f"🏆 {days} дней подряд — феноменально!"
    if days >= 14:
        return f"🥇 {days} дней подряд — отличная серия!"
    if days >= 7:
        return f"🔥 {days} дней подряд!"
    if days >= 3:
        return f"✨ {days} дня подряд"
    return ""

# ─── AI ─────────────────────────────────────────────────────────────────────
def safe_num(v, d=0):
    try:
        return float(str(v).replace(",", ".").strip()) if v not in (None, "", "null") else d
    except:
        return d

def extract_json(text):
    text = text.replace("```json", "").replace("```", "").strip()
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(text[start:i+1])
                except:
                    start = -1
                    depth = 0
    return None

async def yandex_post(prompt, model=None, temperature=0.1, timeout=35):
    if not YANDEX_KEY:
        return None
    headers = {
        "Authorization": f"Api-Key {YANDEX_KEY}",
        "Content-Type": "application/json",
        "x-folder-id": YANDEX_FOLDER
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers=headers,
                json={
                    "modelUri": model or f"gpt://{YANDEX_FOLDER}/yandexgpt/latest",
                    "completionOptions": {"stream": False, "temperature": temperature},
                    "messages": [{"role": "user", "text": prompt}]
                }
            )
            r.raise_for_status()
            return r.json()["result"]["alternatives"][0]["message"]["text"]
    except httpx.TimeoutException:
        logger.error("Yandex API: TIMEOUT")
        return None
    except Exception as ex:
        logger.error(f"Yandex API: {ex}")
        return None

async def ai_nutrition(text=None, photo_bytes=None):
    if not YANDEX_KEY or not YANDEX_FOLDER:
        logger.error("ai_nutrition: API key missing")
        return {"error": "no_api_key"}
    
    if text:
        ck = text.lower().strip()[:80]
        if ck in _nutrition_cache:
            logger.info(f"Cache hit: {ck[:30]}")
            return _nutrition_cache[ck]
    
    headers = {
        "Authorization": f"Api-Key {YANDEX_KEY}",
        "Content-Type": "application/json",
        "x-folder-id": YANDEX_FOLDER
    }
    
    if photo_bytes:
        if not isinstance(photo_bytes, bytes):
            logger.error(f"photo_bytes is {type(photo_bytes)}, not bytes!")
            return {"error": "failed"}
        b64 = base64.b64encode(photo_bytes).decode()
        model = f"gpt://{YANDEX_FOLDER}/yandexgpt/vision-latest"
        prompt = (
            "Ты нутрициолог. Что на фото?\n"
            "Определи блюдо, оцени вес (grams — только твёрдая еда, без жидкостей), посчитай КБЖУ.\n"
            "Если НЕ ЕДА — верни: {\"error\":\"not_food\"}\n"
            "Иначе верни ТОЛЬКО JSON:\n"
            "{\"food_desc\":\"краткое описание состава\",\"food\":\"название блюда\","
            "\"grams\":300,\"kcal\":450,\"p\":20,\"f\":15,\"c\":55}"
        )
        msgs = [{"role": "user", "content": [{"type": "image", "data": b64}, {"type": "text", "text": prompt}]}]
    else:
        model = f"gpt://{YANDEX_FOLDER}/yandexgpt/latest"
        prompt = (
            f"Еда: {text}\n\n"
            "Ты нутрициолог. Посчитай КБЖУ для всего перечисленного.\n"
            "Правила:\n"
            "- Если указан вес — используй его точно\n"
            "- Жидкости (чай, кофе, сок, пиво, вино) — считай их калории, но НЕ включай объём в grams\n"
            "- grams = суммарный вес только твёрдой еды\n"
            "- food = краткое название (что написал пользователь, можно уточнить)\n"
            "Верни ТОЛЬКО JSON без пояснений:\n"
            "{\"food_desc\":\"описание с весами каждой позиции\","
            "\"food\":\"название\",\"grams\":300,\"kcal\":450,\"p\":20,\"f\":15,\"c\":55}\n"
            "Если это не еда — {\"error\":\"not_food\"}"
        )
        msgs = [{"role": "user", "text": prompt}]
    
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            r = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers=headers,
                json={
                    "modelUri": model,
                    "completionOptions": {"stream": False, "temperature": 0.1},
                    "messages": msgs
                }
            )
            if r.status_code != 200:
                logger.error(f"ai_nutrition HTTP {r.status_code}: {r.text[:200]}")
                return {"error": "failed"}
            raw = r.json()["result"]["alternatives"][0]["message"]["text"]
            logger.info(f"AI raw response: {raw[:100]}")
            res = extract_json(raw)
            if not res:
                logger.error(f"ai_nutrition: no JSON in: {raw[:200]}")
                return {"error": "failed"}
            if "error" in res:
                return {"error": res["error"]}
            result = {
                "food_desc": str(res.get("food_desc", " "))[:300],
                "food": str(res.get("food", text or "Блюдо"))[:100],
                "grams": safe_num(res.get("grams"), 0),
                "kcal": int(safe_num(res.get("kcal"))),
                "p": safe_num(res.get("p")),
                "f": safe_num(res.get("f")),
                "c": safe_num(res.get("c")),
            }
            if text and len(_nutrition_cache) < 500:
                _nutrition_cache[text.lower().strip()[:80]] = result
            return result
    except httpx.TimeoutException:
        logger.error("ai_nutrition: TIMEOUT after 35s")
        return {"error": "timeout"}
    except Exception as ex:
        logger.error(f"ai_nutrition exception: {ex}", exc_info=True)
        return {"error": "failed"}

async def ai_recalc(food_name, grams):
    raw = await yandex_post(
        f'КБЖУ для "{food_name}", {grams}г. Только JSON:\n{{"kcal":400, "p":20, "f":15, "c":45}}'
    )
    if not raw:
        return {"error": "failed"}
    res = extract_json(raw)
    if not res:
        return {"error": "failed"}
    return {
        "kcal": int(safe_num(res.get("kcal"))),
        "p": safe_num(res.get("p")),
        "f": safe_num(res.get("f")),
        "c": safe_num(res.get("c"))
    }

async def ai_analysis(stats, actual_days, syms, wrks, profile=None, is_pro=False):
    if not YANDEX_KEY:
        return f"За период: {stats['kcal']} ккал, Б{stats['p']:.0f} Ж{stats['f']:.0f} У{stats['c']:.0f}."
    
    maint, goal = calc_tdee(profile)
    avg = stats['kcal'] // actual_days if actual_days > 0 else stats['kcal']
    
    prof_ctx = ""
    if profile and profile[1]:
        w = profile[1]
        t = profile[2]
        g = profile[5] or "male"
        age = profile[4] or 25
        h = profile[3] or 170
        prof_ctx = f"Человек: {w}кг → цель {t}кг, {h}см, {age}лет, {'мужчина' if g == 'male' else 'женщина'}. "
        if goal:
            deficit = maint - goal
            pn, fn, cn = calc_macros(goal)
            if t and t < w:
                prof_ctx += f"Для похудения норма {goal} ккал/день (дефицит {deficit} ккал). БЖУ: Б{pn}г Ж{fn}г У{cn}г. "
            else:
                prof_ctx += f"Норма {goal} ккал/день. БЖУ: Б{pn}г Ж{fn}г У{cn}г. "
    
    kcal_diff = avg - goal if goal else None
    sym_ctx = ""
    if syms:
        sym_ctx = "Симптомы за период: " + ", ".join(s[0] + (f"({s[1]})" if s[1] else "") for s in syms[-8:]) + ". "
    wrk_ctx = ""
    if wrks:
        wrk_ctx = "Тренировки: " + ", ".join(w[0] for w in wrks[-8:]) + f" (всего {len(wrks)} шт). "
    
    if is_pro:
        diff_str = ""
        if kcal_diff is not None:
            if kcal_diff > 0:
                diff_str = f"Переедание в среднем на {kcal_diff} ккал/день. "
            elif kcal_diff < 0:
                diff_str = f"Дефицит в среднем {abs(kcal_diff)} ккал/день. "
            else:
                diff_str = "В норме по калориям. "
        
        bju_ctx = ""
        if goal:
            pn, fn, cn = calc_macros(goal)
            p_diff = round(stats['p'] / actual_days - pn, 1)
            f_diff = round(stats['f'] / actual_days - fn, 1)
            c_diff = round(stats['c'] / actual_days - cn, 1)
            bju_ctx = (
                f"Среднее БЖУ в день: Б{stats['p']/actual_days:.0f}г (норма {pn}г, "
                f"{'↑'+str(p_diff) if p_diff > 0 else '↓'+str(abs(p_diff))}г), "
                f"Ж{stats['f']/actual_days:.0f}г (норма {fn}г, "
                f"{'↑'+str(f_diff) if f_diff > 0 else '↓'+str(abs(f_diff))}г), "
                f"У{stats['c']/actual_days:.0f}г (норма {cn}г, "
                f"{'↑'+str(c_diff) if c_diff > 0 else '↓'+str(abs(c_diff))}г). "
            )
        
        prompt = (
            f"Ты профессиональный нутрициолог. Сделай детальный разбор питания.\n"
            f"ДАННЫЕ:\n{prof_ctx}Период: {actual_days} дней. Среднее {avg} ккал/день. {diff_str}{bju_ctx}{sym_ctx}{wrk_ctx}\n"
            "ЗАДАЧА: 5-7 предложений живым текстом. Оцени калории vs норма, БЖУ, связь симптомов/тренировок с питанием. "
            "2 конкретные рекомендации. Без воды."
        )
    else:
        prompt = (
            f"Ты нутрициолог. {prof_ctx}Питание за {actual_days} дней: в среднем {avg} ккал/день, "
            f"Б{stats['p']/actual_days:.0f}г Ж{stats['f']/actual_days:.0f}г У{stats['c']/actual_days:.0f}г.{sym_ctx}\n"
            "Напиши 2-3 предложения: результат по калориям и одна практическая рекомендация. Без воды."
        )
    
    res = await yandex_post(prompt, temperature=0.35, timeout=35)
    return res or "Анализ временно недоступен."

async def ai_voice_to_text(ogg_bytes):
    stt_key = os.getenv("YANDEX_STT_KEY", YANDEX_KEY)
    if not stt_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
                headers={"Authorization": f"Api-Key {stt_key}"},
                params={"folderId": YANDEX_FOLDER, "lang": "ru-RU", "format": "oggopus"},
                content=ogg_bytes
            )
            if r.status_code == 200:
                result = r.json().get("result", "").strip()
                logger.info(f"STT result: {result}")
                return result if result else None
            else:
                logger.error(f"STT HTTP {r.status_code}: {r.text[:100]}")
    except Exception as ex:
        logger.error(f"STT: {ex}")
    return None

async def lookup_barcode(barcode):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json",
                headers={"User-Agent": "NutriMindBot/1.0"}
            )
            if r.status_code != 200 or r.json().get("status") != 1:
                return None
            p = r.json()["product"]
            n = p.get("nutriments", {})
            name = p.get("product_name_ru") or p.get("product_name") or "Продукт"
            kcal = safe_num(n.get("energy-kcal_100g") or n.get("energy_100g", 0) / 4.184)
            return {
                "name": name,
                "kcal_100g": kcal,
                "p_100g": safe_num(n.get("proteins_100g", 0)),
                "f_100g": safe_num(n.get("fat_100g", 0)),
                "c_100g": safe_num(n.get("carbohydrates_100g", 0))
            }
    except Exception as ex:
        logger.error(f"barcode: {ex}")
        return None

# ─── FSM ────────────────────────────────────────────────────────────────────
class WaitFood(StatesGroup):
    input = State()

class ProfileFS(StatesGroup):
    input = State()

class CorrectFS(StatesGroup):
    name = State()
    grams = State()

class SymNote(StatesGroup):
    input = State()

class WrkNote(StatesGroup):
    input = State()

class DelConfirm(StatesGroup):
    waiting = State()

class WaterIn(StatesGroup):
    input = State()

class WeightIn(StatesGroup):
    input = State()

class BarcodeWeight(StatesGroup):
    input = State()

# ─── Клавиатуры ─────────────────────────────────────────────────────────────
main_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🍳 Завтрак"), KeyboardButton(text="🥗 Обед")],
    [KeyboardButton(text="🍲 Ужин"), KeyboardButton(text="🍎 Перекус")],
    [KeyboardButton(text="💧 Вода"), KeyboardButton(text="⚖️ Мой вес")],
    [KeyboardButton(text="🤒 Симптом"), KeyboardButton(text="💪 Тренировка")],
    [KeyboardButton(text="📅 Вчера"), KeyboardButton(text="📊 Аналитика")],
    [KeyboardButton(text="📱 Приложение", web_app=WebAppInfo(url=MINI_APP_URL)), KeyboardButton(text="🆘 Помощь")]
], resize_keyboard=True)

settings_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💎 Купить PRO")],
    [KeyboardButton(text="📥 Экспорт"), KeyboardButton(text="📅 Статус подписки")],
    [KeyboardButton(text="🔗 Реферальная ссылка"), KeyboardButton(text="🗑 Удалить мои данные")],
    [KeyboardButton(text="⬅️ Назад")]
], resize_keyboard=True)

back_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="◀️ Назад")]], resize_keyboard=True)

symptom_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🤕 Голова", callback_data="sym_Головная боль"),
     InlineKeyboardButton(text="🤧 Насморк", callback_data="sym_Насморк")],
    [InlineKeyboardButton(text="😴 Усталость", callback_data="sym_Усталость"),
     InlineKeyboardButton(text="🤢 Тошнота", callback_data="sym_Тошнота")],
    [InlineKeyboardButton(text="😣 Живот", callback_data="sym_Боль в животе"),
     InlineKeyboardButton(text="🌸 Аллергия", callback_data="sym_Аллергия")],
    [InlineKeyboardButton(text="🌡 Температура", callback_data="sym_Температура"),
     InlineKeyboardButton(text="😰 Слабость", callback_data="sym_Слабость")],
    [InlineKeyboardButton(text="✏️ Другое", callback_data="sym_other")],
])

workout_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🫀 Кардио", callback_data="wrk_Кардио"),
     InlineKeyboardButton(text="💪 Грудь", callback_data="wrk_Грудь")],
    [InlineKeyboardButton(text="🦵 Ноги", callback_data="wrk_Ноги"),
     InlineKeyboardButton(text="🔙 Спина", callback_data="wrk_Спина")],
    [InlineKeyboardButton(text="💪 Руки", callback_data="wrk_Руки"),
     InlineKeyboardButton(text="🎯 Пресс", callback_data="wrk_Пресс")],
    [InlineKeyboardButton(text="🤸 Растяжка", callback_data="wrk_Растяжка"),
     InlineKeyboardButton(text="🏃 Бег", callback_data="wrk_Бег")],
    [InlineKeyboardButton(text="✏️ Другое", callback_data="wrk_other")],
])

water_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="💧 +250мл", callback_data="water_250"),
     InlineKeyboardButton(text="💧 +500мл", callback_data="water_500")],
    [InlineKeyboardButton(text="💧 +750мл", callback_data="water_750"),
     InlineKeyboardButton(text="💧 +1000мл", callback_data="water_1000")],
    [InlineKeyboardButton(text="✏️ Другое", callback_data="water_custom")],
])

def pricing_kb():
    labels = {"1m": "1 месяц", "3m": "3 месяца", "1y": "1 год"}
    rows = []
    for k, p in PRICES.items():
        rows.append([InlineKeyboardButton(
            text=f"💎 {labels[k]} — {p['stars']}⭐ ({p['per']})",
            callback_data=f"buy_{k}"
        )])
    rows.append([InlineKeyboardButton(text="❓ Что входит в PRO?", callback_data="pro_info")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

MEAL_BTNS = {"🍳 Завтрак", "🥗 Обед", "🍲 Ужин", "🍎 Перекус"}
ALL_MENU = MEAL_BTNS | {
    "📅 Вчера", "📊 Аналитика", "⚙️ Ещё", "🆘 Помощь", "⬅️ Назад", "◀️ Назад",
    "🤒 Симптом", "💪 Тренировка", "💧 Вода", "⚖️ Мой вес",
    "👤 Профиль", "💎 Купить PRO", "📥 Экспорт", "📅 Статус подписки",
    "🔗 Реферальная ссылка", "🗑 Удалить мои данные"
}
MEALS = {"🍳 Завтрак": "breakfast", "🥗 Обед": "lunch", "🍲 Ужин": "dinner", "🍎 Перекус": "snack"}

async def safe_send(message, text, **kwargs):
    try:
        return await message.answer(text, **kwargs)
    except Exception as ex1:
        logger.warning(f"safe_send HTML failed: {ex1}")
        try:
            plain = re.sub(r'<[^>]+>', '', text)
            return await message.answer(plain, parse_mode=None, **kwargs)
        except Exception as ex2:
            logger.error(f"safe_send plain also failed: {ex2}")
            try:
                return await message.answer("✅ Записано!", reply_markup=kwargs.get("reply_markup"), parse_mode=None)
            except:
                pass

# ─── Хендлеры ───────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    is_new = get_user(uid) is None
    ensure_user(uid, message.from_user.username)
    
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        ref = find_by_ref(args[1][4:])
        if ref and ref != uid:
            set_referred(uid, ref)
    
    user = get_user(uid)
    if is_new or (user and not user[8]):
        conn = db()
        conn.execute("UPDATE users SET onboarded=1 WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()
        trial = activate_trial(uid)
        trial_text = f"\n\n🎁 Активирован бесплатный PRO на 3 дня!\nПопробуй все функции до {trial[:10]}." if trial else ""
        await message.answer(
            f"👋 Привет! Я NutriMind — AI-нутрициолог.\n\n"
            f"Считаю КБЖУ, слежу за водой и весом, анализирую самочувствие.{trial_text}\n\n"
            f"📣 Советы по питанию: {CHANNEL}\n\n"
            f"Начнём — заполни профиль:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Заполнить профиль", callback_data="setup_profile")],
                [InlineKeyboardButton(text="Пропустить →", callback_data="skip_onboard")]
            ])
        )
    else:
        s = get_streak(uid)
        sm = f"\n{streak_msg(s)}" if s >= 3 else ""
        await message.answer(f"👋 С возвращением!{sm}", reply_markup=main_kb)

@dp.callback_query(F.data == "skip_onboard")
async def skip_onboard(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer("Профиль можно заполнить позже: ⚙️ Ещё → Профиль.\nПоехали! 👇", reply_markup=main_kb)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        f"📖 Как пользоваться:\n\n"
        f"• Еда: нажми приём пищи → напиши, сфотографируй или запиши голосом\n"
        f"  Можно несколько: «курица, рис 200г и чай»\n"
        f"• Штрихкод: отправь фото штрихкода упаковки\n"
        f"• Вода: 💧 Вода\n"
        f"• Вес: ⚖️ Мой вес\n"
        f"• Симптомы: 🤒 Симптом\n"
        f"• Тренировки: 💪 Тренировка\n"
        f"• Аналитика: 📊 Аналитика\n"
        f"• 📱 Приложение: открой удобный интерфейс для записи еды и аналитики!\n\n"
        f"📣 {CHANNEL}"
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    total, pro = get_counts()
    pro_users = get_all_pro()
    fc, ft = get_founder_status()
    text = f"📊 Админ-панель\n\n👥 Всего: {total}\n👑 PRO: {pro}\n🏅 Основатели: {fc}/{ft}\n\n"
    if pro_users:
        text += "PRO:\n"
        for uid_, un, until in pro_users:
            text += f"• {'@' + e(un) if un else 'ID:' + str(uid_)} — до {until}\n"
    else:
        text += "PRO пользователей нет."
    await message.answer(text)

@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Проверяю Yandex API...")
    result = await yandex_post("Скажи 'ok'", timeout=10)
    if result:
        await message.answer(f"✅ Yandex API работает.\nОтвет: {result[:80]}")
    else:
        await message.answer("❌ Yandex API не отвечает! Проверь ключ в Railway Variables.")

@dp.message(F.text.in_({"◀️ Назад", "❌ Отмена", "⬅️ Назад"}))
async def back_to_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👆 Главное меню:", reply_markup=main_kb)

@dp.message(F.text == "⚙️ Ещё")
async def open_settings(message: types.Message):
    await message.answer("⚙️ Настройки:", reply_markup=settings_kb)

@dp.message(F.text == "🆘 Помощь")
async def help_btn(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        return await message.answer("Ты админ. /admin — панель. /ping — проверка API.")
    await message.answer("🆘 Сообщение отправлено поддержке.")
    await bot.send_message(
        ADMIN_ID,
        f"🆨 @{e(message.from_user.username or 'нет')} (ID:{message.from_user.id})\n\n{e(message.text)}"
    )

# ─── ОБРАБОТКА ДАННЫХ ИЗ MINI APP ───────────────────────────────────────────
@dp.message(F.web_app_data)
async def handle_web_app_data(message: types.Message):
    """Получает данные из Mini App и обрабатывает их"""
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get("action")
        uid = message.from_user.id
        
        if action == "add_food":
            # Данные из Mini App: {"action":"add_food","meal":"breakfast","food":"Овсянка","grams":200,"kcal":350,"p":12,"f":6,"c":60}
            meal = data.get("meal", "snack")
            food = data.get("food", "Блюдо")
            grams = float(data.get("grams", 0))
            kcal = int(data.get("kcal", 0))
            p = float(data.get("p", 0))
            f = float(data.get("f", 0))
            c = float(data.get("c", 0))
            
            lid = save_log(uid, meal, food, grams, kcal, p, f, c)
            _update_streak(uid)
            
            await message.answer(f"✅ <b>{e(food)}</b> ({int(grams)}г) записано!\n🔥 {kcal} ккал | Б:{p:.1f} Ж:{f:.1f} У:{c:.1f}", parse_mode="HTML")
        
        elif action == "add_water":
            ml = int(data.get("ml", 0))
            log_water(uid, ml)
            total = get_today_water(uid)
            bar, pct = water_bar(total)
            await message.answer(f"✅ +{ml} мл воды!\n💧 Итого: {total} / {WATER_GOAL} мл\n{bar} {pct}%")
        
        elif action == "add_weight":
            w = float(data.get("weight", 0))
            log_weight(uid, w)
            await message.answer(f"✅ Вес {w} кг записан!")
        
        elif action == "add_symptom":
            symptom = data.get("symptom", "")
            note = data.get("note", "")
            save_symptom(uid, symptom, note)
            await message.answer(f"✅ Симптом «{e(symptom)}» записан.")
        
        elif action == "add_workout":
            workout = data.get("workout", "")
            note = data.get("note", "")
            save_workout(uid, workout, note)
            await message.answer(f"✅ Тренировка «{e(workout)}» записана.")
        
        else:
            await message.answer(f"📱 Получены данные из приложения: {data}")
            
    except json.JSONDecodeError:
        await message.answer("⚠️ Не удалось обработать данные из приложения.")
    except Exception as ex:
        logger.error(f"web_app_data error: {ex}")
        await message.answer("⚠️ Ошибка при обработке данных.")

# ─── ВОДА ───────────────────────────────────────────────────────────────────
@dp.message(F.text == "💧 Вода")
async def water_menu(message: types.Message):
    total = get_today_water(message.from_user.id)
    bar, pct = water_bar(total)
    await message.answer(
        f"💧 Вода сегодня: {total} / {WATER_GOAL} мл\n{bar} {pct}%\n\nДобавь:",
        reply_markup=water_kb
    )

@dp.callback_query(F.data.startswith("water_"))
async def water_add(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    amt = cb.data[6:]
    if amt == "custom":
        await state.set_state(WaterIn.input)
        await cb.message.answer("💧 Введи количество мл:", reply_markup=back_kb)
        return
    ml = int(amt)
    log_water(cb.from_user.id, ml)
    total = get_today_water(cb.from_user.id)
    bar, pct = water_bar(total)
    msg = f"✅ +{ml} мл\n💧 Итого: {total} / {WATER_GOAL} мл\n{bar} {pct}%"
    if total >= WATER_GOAL:
        msg += "\n\n🎉 Норма воды выполнена!"
    await cb.message.edit_text(msg)

@dp.message(WaterIn.input)
async def water_in(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return
    try:
        ml = int(message.text.strip().replace("мл", "").strip())
        if ml <= 0 or ml > 5000:
            raise ValueError
    except:
        return await message.answer("❌ Введи число от 1 до 5000")
    log_water(message.from_user.id, ml)
    await state.clear()
    await message.answer(
        f"✅ +{ml} мл\n💧 Итого: {get_today_water(message.from_user.id)} мл",
        reply_markup=main_kb
    )

# ─── ВЕС ────────────────────────────────────────────────────────────────────
@dp.message(F.text == "⚖️ Мой вес")
async def weight_menu(message: types.Message, state: FSMContext):
    history = get_weight_history(message.from_user.id)
    text = "⚖️ Журнал веса\n\n"
    if history:
        for w_, d in reversed(history[-5:]):
            text += f"• {d[:10]} — {w_} кг\n"
        if len(history) >= 2:
            diff = history[0][0] - history[-1][0]
            text += f"\n{'📉' if diff < 0 else '📈' if diff > 0 else '➡️'} За период: {diff:+.1f} кг"
    else:
        text += "Записей пока нет.\n"
    text += "\nВведи текущий вес (кг): "
    await state.set_state(WeightIn.input)
    await message.answer(text, reply_markup=back_kb)

@dp.message(WeightIn.input)
async def weight_in(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    try:
        w = float(message.text.strip().replace(",", ".").replace("кг", "").strip())
        if w < 20 or w > 500:
            raise ValueError
    except:
        return await message.answer("❌ Введи корректный вес, например: 75.5")
    log_weight(message.from_user.id, w)
    profile = get_profile(message.from_user.id)
    await state.clear()
    msg = f"✅ Вес {w} кг записан! "
    if profile and profile[2]:
        diff = w - profile[2]
        if diff > 0:
            msg += f"\n🎯 До цели {profile[2]} кг: {diff:.1f} кг"
        else:
            msg += f"\n🎉 Цель {profile[2]} кг достигнута!"
    await message.answer(msg, reply_markup=main_kb)

# ─── ЕДА ────────────────────────────────────────────────────────────────────
@dp.message(F.text.in_(MEAL_BTNS))
async def pick_meal(message: types.Message, state: FSMContext):
    await state.clear()
    await state.update_data(meal=MEALS[message.text], is_yesterday=False)
    await message.answer(
        f"✅ {e(message.text)}. Напиши что ел или:\n"
        f"• Отправь фото блюда\n"
        f"• Отправь голосовое\n"
        f"• Отправь фото штрихкода\n\n"
        f"Можно несколько: «скумбрия, 3 куска хлеба»",
        reply_markup=back_kb
    )
    await state.set_state(WaitFood.input)

@dp.message(F.text == "📅 Вчера")
async def yesterday_mode(message: types.Message, state: FSMContext):
    await state.clear()
    await state.update_data(is_yesterday=True)
    await message.answer(
        "📅 За ВЧЕРА. Выбери приём:",
        reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🍳 Завтрак"), KeyboardButton(text="🥗 Обед")],
            [KeyboardButton(text="🍲 Ужин"), KeyboardButton(text="🍎 Перекус")],
            [KeyboardButton(text="◀️ Назад")]
        ], resize_keyboard=True)
    )

async def _save_and_reply(message, state, ai_data):
    try:
        data = await state.get_data()
        meal = data.get("meal", "dinner")
        is_yest = data.get("is_yesterday", False)
        date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M") if is_yest else datetime.now().strftime("%Y-%m-%d %H:%M")
        
        lid = save_log(
            message.from_user.id, meal, ai_data["food"], ai_data["grams"],
            ai_data["kcal"], ai_data["p"], ai_data["f"], ai_data["c"], date
        )
        await state.update_data(last_log_id=lid, last_food=ai_data["food"], last_grams=ai_data["grams"])
        await state.set_state(None)
        logger.info(f"Saved log {lid} for uid={message.from_user.id}: {ai_data['food']} {ai_data['kcal']}kcal")
        
        profile = get_profile(message.from_user.id)
        _, goal = calc_tdee(profile)
        today_stats = get_stats(get_today_logs(message.from_user.id))
        
        desc_raw = (ai_data.get("food_desc") or "").strip()
        desc = f"📝 {e(desc_raw)}\n\n" if desc_raw else ""
        grams_str = f" ({int(ai_data['grams'])}г)" if ai_data.get("grams", 0) > 0 else ""
        
        progress = ""
        if goal and not is_yest:
            rem = goal - today_stats["kcal"]
            if rem > 0:
                progress = f"\n\n📊 Сегодня: {today_stats['kcal']} / {goal} ккал (осталось {rem})"
            else:
                progress = f"\n\n⚠️ Сегодня: {today_stats['kcal']} ккал (+{abs(rem)} сверх нормы)"
        
        s = get_streak(message.from_user.id)
        sk = f"\n{streak_msg(s)}" if s in (3, 7, 14, 30) else ""
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Исправить название", callback_data="fix_name"),
             InlineKeyboardButton(text="⚖️ Исправить вес", callback_data="fix_grams")]
        ])
        
        text = (
            f"{desc}✅ <b>{e(ai_data['food'])}</b>{e(grams_str)}\n"
            f"🔥 {ai_data['kcal']} ккал | Б:{ai_data['p']:.1f} Ж:{ai_data['f']:.1f} У:{ai_data['c']:.1f}"
            f"{progress}{sk}"
        )
        await safe_send(message, text, reply_markup=kb)
        
    except Exception as ex:
        logger.error(f"_save_and_reply CRASH: {ex}", exc_info=True)
        await state.clear()
        try:
            kcal = ai_data.get("kcal", 0)
            await message.answer(f"✅ Записано!\n🔥 {kcal} ккал", reply_markup=main_kb, parse_mode=None)
        except Exception as ex2:
            logger.error(f"Even fallback failed: {ex2}")

@dp.message(WaitFood.input)
async def process_food(message: types.Message, state: FSMContext):
    if message.text and message.text in ALL_MENU:
        await state.clear()
        if message.text in MEAL_BTNS:
            await state.update_data(meal=MEALS[message.text], is_yesterday=False)
            await message.answer(f"✅ {e(message.text)}. Напиши что ел:", reply_markup=back_kb)
            await state.set_state(WaitFood.input)
        else:
            await message.answer("👆 Главное меню:", reply_markup=main_kb)
        return
    
    data = await state.get_data()
    if not data.get("meal"):
        await state.clear()
        return await message.answer("👆 Сначала выбери приём пищи из меню.", reply_markup=main_kb)
    
    uid = message.from_user.id
    
    # Голосовое
    if message.voice:
        wait_msg = await message.answer("🎤 Распознаю голос...")
        ogg_bytes = None
        try:
            f_ = await bot.get_file(message.voice.file_id)
            bio = await bot.download_file(f_.file_path)
            ogg_bytes = bio.read()
        except Exception as ex:
            logger.error(f"voice download: {ex}")
        try:
            await wait_msg.delete()
        except:
            pass
        if not ogg_bytes:
            return await message.answer("⚠️ Не удалось загрузить голосовое. Напиши текстом.", reply_markup=main_kb)
        text_from_voice = await ai_voice_to_text(ogg_bytes)
        if not text_from_voice:
            return await message.answer("⚠️ Не удалось распознать голос. Напиши текстом.", reply_markup=main_kb)
        await message.answer(f"🎤 Распознано: <i>{e(text_from_voice)}</i>\n\n⏳ Анализирую...")
        ai = await ai_nutrition(text=text_from_voice)
        if ai.get("error"):
            await state.clear()
            return await message.answer("⚠️ Не удалось распознать еду из голоса.", reply_markup=main_kb)
        await _save_and_reply(message, state, ai)
        return
    
    # Фото
    if message.photo:
        wait_msg = await message.answer("⏳ Анализирую фото...")
        photo_bytes = None
        try:
            f_ = await bot.get_file(message.photo[-1].file_id)
            bio = await bot.download_file(f_.file_path)
            photo_bytes = bio.read()
            logger.info(f"Photo downloaded: {len(photo_bytes)} bytes")
        except Exception as ex:
            logger.error(f"photo download: {ex}")
            try:
                await wait_msg.delete()
            except:
                pass
            await state.clear()
            return await message.answer("⚠️ Не удалось загрузить фото. Напиши текстом.", reply_markup=main_kb)
        
        ai = await ai_nutrition(photo_bytes=photo_bytes)
        logger.info(f"Photo AI result: {ai}")
        try:
            await wait_msg.delete()
        except:
            pass
        
        if ai.get("error") == "not_food":
            await message.answer("🔍 Не вижу еду на фото. Если это штрихкод — напиши его цифры:")
            await state.update_data(waiting_barcode=True)
            return
        if ai.get("error") == "timeout":
            await state.clear()
            return await message.answer("⏱ Yandex API долго отвечает. Попробуй написать текстом.", reply_markup=main_kb)
        if ai.get("error"):
            await state.clear()
            return await message.answer("⚠️ Не удалось распознать фото. Попробуй написать текстом.", reply_markup=main_kb)
        await _save_and_reply(message, state, ai)
        return
    
    # Текст / штрихкод
    text = message.text.strip()
    if re.match(r'^\d{8,13}$', text) or data.get("waiting_barcode"):
        barcode = re.sub(r'\D', '', text)
        if len(barcode) >= 8:
            wait_msg = await message.answer(f"🔍 Ищу по штрихкоду {barcode}...")
            product = await lookup_barcode(barcode)
            try:
                await wait_msg.delete()
            except:
                pass
            if product:
                await state.update_data(barcode_product=product, waiting_barcode=False)
                await message.answer(
                    f"✅ Найден: <b>{e(product['name'])}</b>\n"
                    f"Калорийность: {product['kcal_100g']:.0f} ккал/100г\n\n"
                    f"Введи вес порции (г):",
                    reply_markup=back_kb
                )
                await state.set_state(BarcodeWeight.input)
                return
            else:
                await state.update_data(waiting_barcode=False)
                await message.answer("❌ Продукт не найден. Напиши название вручную:")
                return
    
    wait_msg = await message.answer("⏳ Анализирую...")
    ai = await ai_nutrition(text=text)
    try:
        await wait_msg.delete()
    except:
        pass
    
    if ai.get("error") == "not_food":
        await state.clear()
        return await message.answer("🚫 Не похоже на еду. Выбери приём пищи из меню.", reply_markup=main_kb)
    if ai.get("error") == "timeout":
        await state.clear()
        return await message.answer("⏱ Yandex API долго отвечает. Попробуй чуть позже.", reply_markup=main_kb)
    if ai.get("error"):
        await state.clear()
        return await message.answer(
            "⚠️ Не удалось распознать. Попробуй написать подробнее,\n"
            "например: <i>скумбрия копчёная 150г, хлеб 3 куска</i>",
            reply_markup=main_kb
        )
    await _save_and_reply(message, state, ai)

@dp.message(BarcodeWeight.input)
async def barcode_weight_input(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    product = data.get("barcode_product")
    if not product:
        await state.clear()
        return await message.answer("❌ Ошибка. Попробуй снова.", reply_markup=main_kb)
    try:
        grams = float(message.text.strip().replace("г", "").replace(",", ".").strip())
        if grams <= 0 or grams > 3000:
            raise ValueError
    except:
        return await message.answer("❌ Введи вес в граммах, например: 150")
    k = grams / 100
    ai_data = {
        "food": product["name"],
        "food_desc": f"{product['name']}, {grams:.0f}г",
        "grams": grams,
        "kcal": int(product["kcal_100g"] * k),
        "p": round(product["p_100g"] * k, 1),
        "f": round(product["f_100g"] * k, 1),
        "c": round(product["c_100g"] * k, 1),
    }
    await _save_and_reply(message, state, ai_data)

# ─── ИСПРАВЛЕНИЕ ────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "fix_name")
async def fix_name_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if not data.get("last_log_id"):
        return await cb.message.answer("❌ Запись не найдена.", reply_markup=main_kb)
    await state.set_state(CorrectFS.name)
    await cb.message.answer(
        f"✏️ Текущее: {e(data.get('last_food', '?'))}\n\nНапиши правильное название:",
        reply_markup=back_kb
    )

@dp.callback_query(F.data == "fix_grams")
async def fix_grams_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if not data.get("last_log_id"):
        return await cb.message.answer("❌ Запись не найдена.", reply_markup=main_kb)
    await state.set_state(CorrectFS.grams)
    await cb.message.answer(
        f"⚖️ Текущий вес: {int(data.get('last_grams', 0))}г\n\nНапиши правильный вес:",
        reply_markup=back_kb
    )

@dp.message(CorrectFS.name)
async def apply_fix_name(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    lid = data.get("last_log_id")
    grams = data.get("last_grams", 250)
    wait = await message.answer("⏳ Пересчитываю...")
    res = await ai_recalc(message.text.strip(), grams)
    try:
        await wait.delete()
    except:
        pass
    if res.get("error"):
        await state.set_state(None)
        return await message.answer("⚠️ Не удалось пересчитать.", reply_markup=main_kb)
    update_log(lid, message.text.strip(), grams, res["kcal"], res["p"], res["f"], res["c"])
    await state.update_data(last_food=message.text.strip())
    await state.set_state(None)
    await message.answer(
        f"✅ {e(message.text.strip())} ({int(grams)}г)\n"
        f"🔥 {res['kcal']} ккал | Б:{res['p']:.1f} Ж:{res['f']:.1f} У:{res['c']:.1f}",
        reply_markup=main_kb
    )

@dp.message(CorrectFS.grams)
async def apply_fix_grams(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    lid = data.get("last_log_id")
    fname = data.get("last_food", "Блюдо")
    try:
        ng = float(message.text.strip().replace(",", ".").replace("г", "").strip())
        if ng <= 0 or ng > 5000:
            raise ValueError
    except:
        return await message.answer("❌ Введи число от 1 до 5000")
    wait = await message.answer("⏳ Пересчитываю...")
    res = await ai_recalc(fname, ng)
    try:
        await wait.delete()
    except:
        pass
    if res.get("error"):
        await state.set_state(None)
        return await message.answer("⚠️ Не удалось пересчитать.", reply_markup=main_kb)
    update_log(lid, fname, ng, res["kcal"], res["p"], res["f"], res["c"])
    await state.update_data(last_grams=ng)
    await state.set_state(None)
    await message.answer(
        f"✅ {e(fname)} ({int(ng)}г)\n"
        f"🔥 {res['kcal']} ккал | Б:{res['p']:.1f} Ж:{res['f']:.1f} У:{res['c']:.1f}",
        reply_markup=main_kb
    )

# ─── СИМПТОМЫ ───────────────────────────────────────────────────────────────
@dp.message(F.text == "🤒 Симптом")
async def symptom_menu(message: types.Message):
    await message.answer("🤒 Выбери симптом:", reply_markup=symptom_kb)

@dp.callback_query(F.data == "sym_skip")
async def sym_skip(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    s = data.get("symptom_type", "")
    save_symptom(cb.from_user.id, s, "")
    await state.clear()
    await cb.message.answer(f"✅ Симптом «{e(s)}» записан.", reply_markup=main_kb)

@dp.callback_query(F.data.startswith("sym_"))
async def sym_selected(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    s = cb.data[4:]
    if s == "other":
        await state.set_state(SymNote.input)
        await state.update_data(symptom_type="custom")
        await cb.message.answer("✏️ Опиши симптом:", reply_markup=back_kb)
    else:
        await state.set_state(SymNote.input)
        await state.update_data(symptom_type=s)
        await cb.message.answer(
            f"📝 {e(s)} — добавь заметку или пропусти:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="sym_skip")]])
        )

@dp.message(SymNote.input)
async def sym_note(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    st = data.get("symptom_type", "custom")
    if st == "custom":
        save_symptom(message.from_user.id, message.text.strip(), "")
        await state.clear()
        await message.answer(f"✅ Симптом «{e(message.text.strip())}» записан.", reply_markup=main_kb)
    else:
        save_symptom(message.from_user.id, st, message.text.strip())
        await state.clear()
        await message.answer(f"✅ Симптом «{e(st)}» с заметкой записан.", reply_markup=main_kb)

# ─── ТРЕНИРОВКИ ─────────────────────────────────────────────────────────────
@dp.message(F.text == "💪 Тренировка")
async def workout_menu(message: types.Message):
    await message.answer("💪 Выбери тип тренировки:", reply_markup=workout_kb)

@dp.callback_query(F.data == "wrk_skip")
async def wrk_skip(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    w = data.get("workout_type", "")
    save_workout(cb.from_user.id, w, "")
    await state.clear()
    await cb.message.answer(f"✅ Тренировка «{e(w)}» записана.", reply_markup=main_kb)

@dp.callback_query(F.data.startswith("wrk_"))
async def wrk_selected(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    w = cb.data[4:]
    if w == "other":
        await state.set_state(WrkNote.input)
        await state.update_data(workout_type="custom")
        await cb.message.answer("✏️ Опиши тренировку:", reply_markup=back_kb)
    else:
        await state.set_state(WrkNote.input)
        await state.update_data(workout_type=w)
        await cb.message.answer(
            f"📝 {e(w)} — добавь заметку или пропусти:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="wrk_skip")]])
        )

@dp.message(WrkNote.input)
async def wrk_note(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    wt = data.get("workout_type", "custom")
    if wt == "custom":
        save_workout(message.from_user.id, message.text.strip(), "")
        await state.clear()
        await message.answer(f"✅ Тренировка «{e(message.text.strip())}» записана.", reply_markup=main_kb)
    else:
        save_workout(message.from_user.id, wt, message.text.strip())
        await state.clear()
        await message.answer(f"✅ Тренировка «{e(wt)}» с заметкой записана.", reply_markup=main_kb)

# ─── ПРОФИЛЬ ────────────────────────────────────────────────────────────────
@dp.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message):
    p = get_profile(message.from_user.id)
    if not p or not p[1]:
        return await message.answer(
            "👤 Профиль пуст.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Заполнить", callback_data="setup_profile")]])
        )
    maint, goal = calc_tdee(p)
    g_str = "♂️ Мужской" if p[5] == "male" else "♀️ Женский"
    text = (
        f"👤 Твой профиль\n"
        f"⚖️ Вес: {p[1]} кг → цель: {p[2]} кг\n"
        f"📏 Рост: {p[3]} см\n"
        f"🎂 Возраст: {p[4]} лет\n"
        f"👤 Пол: {g_str}\n"
    )
    if goal:
        pn, fn, cn = calc_macros(goal)
        diff_s = ""
        if p[2] and p[1]:
            if p[2] < p[1]:
                diff_s = f" (дефицит {maint - goal} ккал)"
            elif p[2] > p[1]:
                diff_s = f" (профицит {goal - maint} ккал)"
        text += f"\n🔥 Норма: ~{goal} ккал/день{diff_s}\n📊 БЖУ: Б~{pn}г / Ж~{fn}г / У~{cn}г"
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Изменить", callback_data="edit_profile_menu")]]))

@dp.callback_query(F.data.in_(["setup_profile", "edit_profile_menu"]))
async def profile_menu(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.data == "setup_profile":
        await state.update_data(step=1)
        await state.set_state(ProfileFS.input)
        await cb.message.edit_text("⚖️ Настройка профиля\n\n1/5. Текущий вес (кг):")
    else:
        await cb.message.edit_text(
            "✏️ Что изменить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Вес", callback_data="edit_weight"),
                 InlineKeyboardButton(text="🎯 Цель(кг)", callback_data="edit_target")],
                [InlineKeyboardButton(text="📏 Рост", callback_data="edit_height"),
                 InlineKeyboardButton(text="🎂 Возраст", callback_data="edit_age")]
            ])
        )

@dp.callback_query(F.data.startswith("edit_"))
async def start_edit(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    field = cb.data.split("_")[1]
    await state.update_data(edit_field=field, step=10)
    await state.set_state(ProfileFS.input)
    prompts = {
        "weight": "⚖️ Новый вес (кг):",
        "target": "🎯 Новая цель (кг):",
        "height": "📏 Новый рост (см):",
        "age": "🎂 Новый возраст (лет):"
    }
    await cb.message.edit_text(prompts.get(field, "Введи значение:"))

@dp.message(ProfileFS.input)
async def profile_input(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    step = data.get("step", 1)
    is_edit = step >= 10
    field = data.get("edit_field")
    try:
        val = float(message.text.replace(",", ".")) if not field or field in ["weight", "target"] else int(message.text)
    except:
        return await message.answer("❌ Введи число.")
    
    if is_edit and field:
        upd_profile_field(message.from_user.id, field, val)
        if field == "weight":
            log_weight(message.from_user.id, val)
        await state.clear()
        return await message.answer("✅ Обновлено!", reply_markup=main_kb)
    
    if step == 1:
        await state.update_data(weight=val, step=2)
        await message.answer(f"✅ {val} кг.\n\n2/5. Цель (кг):")
    elif step == 2:
        await state.update_data(target=val, step=3)
        await message.answer(f"✅ Цель: {val} кг.\n\n3/5. Рост (см):")
    elif step == 3:
        await state.update_data(height=int(val), step=4)
        await message.answer(f"✅ {int(val)} см.\n\n4/5. Возраст (лет):")
    elif step == 4:
        await state.update_data(age=int(val), step=5)
        await message.answer(
            f"✅ {int(val)} лет.\n\n5/5. Пол:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="♂️ Мужской", callback_data="gender_male"),
                 InlineKeyboardButton(text="♀️ Женский", callback_data="gender_female")]
            ])
        )

@dp.callback_query(F.data.in_(["gender_male", "gender_female"]))
async def profile_gender(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    g = "male" if cb.data == "gender_male" else "female"
    save_profile(cb.from_user.id, data.get("weight"), data.get("target"), data.get("height"), data.get("age"), g)
    if data.get("weight"):
        log_weight(cb.from_user.id, data["weight"])
    await state.clear()
    profile = get_profile(cb.from_user.id)
    maint, goal = calc_tdee(profile)
    msg = "✅ Профиль сохранён!\n"
    if goal:
        pn, fn, cn = calc_macros(goal)
        w = data.get("weight", 0)
        t = data.get("target", 0)
        if t and w and t < w:
            msg += f"\n🎯 Цель: похудеть {w}→{t} кг\n🔥 Норма: {goal} ккал/день (дефицит {maint - goal} ккал)\n"
        elif t and w and t > w:
            msg += f"\n🎯 Цель: набрать до {t} кг\n🔥 Норма: {goal} ккал/день (профицит {goal - maint} ккал)\n"
        else:
            msg += f"\n🔥 Норма: {goal} ккал/день\n"
        msg += f"📊 БЖУ: Б~{pn}г / Ж~{fn}г / У~{cn}г"
    await cb.message.edit_text(msg)
    await bot.send_message(cb.from_user.id, "👇 Главное меню:", reply_markup=main_kb)

# ─── АНАЛИТИКА ──────────────────────────────────────────────────────────────
@dp.message(F.text == "📊 Аналитика")
async def show_analytics(message: types.Message):
    is_pro = get_sub(message.from_user.id)["active"]
    btns = [
        [
            InlineKeyboardButton(text="Сегодня", callback_data="rep_1"),
            InlineKeyboardButton(text="3 дня", callback_data="rep_3"),
            InlineKeyboardButton(text="7 дней" if is_pro else "🔒 7 дней", callback_data="rep_7" if is_pro else "paywall")
        ],
        [
            InlineKeyboardButton(text="📅 Любой срок" if is_pro else "🔒 Любой срок", callback_data="rep_all" if is_pro else "paywall")
        ]
    ]
    await message.answer("📊 Выбери период:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("rep_"))
async def gen_report(cb: types.CallbackQuery):
    await cb.answer()
    period = cb.data.split("_")[1]
    days = None if period == "all" else int(period)
    rows = get_logs(cb.from_user.id, days)
    if not rows:
        return await cb.message.edit_text("Нет записей за этот период.")
    
    dates = [datetime.strptime(r[9].split()[0], "%Y-%m-%d") for r in rows]
    actual_days = max(1, (max(dates) - min(dates)).days + 1)
    stats = get_stats(rows)
    profile = get_profile(cb.from_user.id)
    is_pro = get_sub(cb.from_user.id)["active"]
    syms = get_symptoms(cb.from_user.id, days)
    wrks = get_workouts(cb.from_user.id, days)
    
    await cb.message.edit_text(f"⏳ Анализирую данные за {actual_days} дн...")
    ai_text = await ai_analysis(stats, actual_days, syms, wrks, profile, is_pro)
    
    maint, goal = calc_tdee(profile)
    avg_kcal = stats['kcal'] // actual_days if actual_days > 0 else stats['kcal']
    norm_line = f" / норма ~{goal}" if goal else ""
    
    header = (
        f"📊 <b>Отчёт за {actual_days} дн</b>\n"
        f"🔥 Среднее: {avg_kcal}{norm_line} ккал/день\n"
        f"📊 Б:{stats['p']/actual_days:.0f}г / Ж:{stats['f']/actual_days:.0f}г / У:{stats['c']/actual_days:.0f}г (в день)\n"
    )
    
    meal_breakdown = ""
    if is_pro:
        mn = {"breakfast": "🍳 Завтрак", "lunch": "🥗 Обед", "dinner": "🍲 Ужин", "snack": "🍎 Перекус"}
        bm = {}
        for r in rows:
            bm[r[2]] = bm.get(r[2], 0) + (r[5] or 0)
        if bm:
            meal_breakdown = "\n<b>Калории по приёмам (итого):</b>\n"
            for m in ["breakfast", "lunch", "dinner", "snack"]:
                if m in bm:
                    meal_breakdown += f"{mn.get(m, '•')}: {bm[m]} ккал\n"
    
    top_foods = ""
    if is_pro and rows:
        food_kcal = {}
        for r in rows:
            name = r[3]
            kcal = r[5] or 0
            food_kcal[name] = food_kcal.get(name, 0) + kcal
        top = sorted(food_kcal.items(), key=lambda x: -x[1])[:5]
        if top:
            top_foods = "\n<b>Топ-5 продуктов по калориям:</b>\n"
            for name, kcal in top:
                top_foods += f"• {e(name)}: {kcal} ккал\n"
    
    ext = ""
    if syms:
        ext += f"\n🤒 Симптомов за период: {len(syms)}"
    if wrks:
        ext += f"\n💪 Тренировок за период: {len(wrks)}"
    
    full_text = f"{header}{meal_breakdown}{top_foods}{ext}\n\n💡 {e(ai_text)}"
    
    if len(full_text) > 4000:
        full_text = full_text[:3990] + "..."
    
    await cb.message.edit_text(full_text)

@dp.callback_query(F.data == "paywall")
async def paywall(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "🔒 7+ дней аналитики — только PRO\n\n"
        "Также в PRO:\n"
        "• Разбивка по приёмам\n"
        "• Анализ симптомов\n"
        "• Экспорт CSV\n"
        "• Ежедневная сводка\n"
        "• Еженедельный AI-коуч",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Купить PRO", callback_data="open_pro")]])
    )

# ─── ЭКСПОРТ ────────────────────────────────────────────────────────────────
@dp.message(F.text == "📥 Экспорт")
async def export_data(message: types.Message):
    if not get_sub(message.from_user.id)["active"]:
        return await message.answer(
            "🔒 Экспорт CSV — только PRO.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Купить PRO", callback_data="open_pro")]])
        )
    rows = get_all_logs_csv(message.from_user.id)
    if not rows:
        return await message.answer("📭 Нет данных.")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Дата", "Приём", "Блюдо", "Вес(г)", "Ккал", "Б", "Ж", "У"])
    for r in rows:
        w.writerow([r[9], r[2], r[3], r[4], r[5], r[6], r[7], r[8]])
    out.seek(0)
    f = types.BufferedInputFile(out.getvalue().encode(), filename=f"nutrimind_{datetime.now().strftime('%Y%m%d')}.csv")
    await message.answer_document(f, caption=f"📥 Экспорт завершён! Записей: {len(rows)}")

# ─── РЕФЕРАЛЬНАЯ ────────────────────────────────────────────────────────────
@dp.message(F.text == "🔗 Реферальная ссылка")
async def ref_link(message: types.Message):
    code = get_ref_code(message.from_user.id)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{code}"
    await message.answer(
        f"🔗 Реферальная программа\n\n"
        f"Приглашай друзей — оба получите 7 дней PRO!\n\n"
        f"Твоя ссылка:\n`{link}`"
    )

# ─── УДАЛЕНИЕ ───────────────────────────────────────────────────────────────
@dp.message(F.text == "🗑 Удалить мои данные")
async def delete_start(message: types.Message, state: FSMContext):
    await state.set_state(DelConfirm.waiting)
    await message.answer(
        "⚠️ Удаление всех данных\n\n"
        "Питание, профиль, симптомы, вода, вес, PRO.\n"
        "Необратимо!\n\n"
        "Напиши УДАЛИТЬ:",
        reply_markup=back_kb
    )

@dp.message(DelConfirm.waiting)
async def delete_confirm(message: types.Message, state: FSMContext):
    if message.text.strip().upper() == "УДАЛИТЬ":
        delete_user_data(message.from_user.id)
        await state.clear()
        await message.answer(
            "✅ Все данные удалены. Напиши /start чтобы начать заново.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="/start")]], resize_keyboard=True)
        )
    else:
        await state.clear()
        await message.answer("↩️ Отменено.", reply_markup=main_kb)

# ─── PRO ────────────────────────────────────────────────────────────────────
@dp.message(F.text == "💎 Купить PRO")
async def buy_pro(message: types.Message):
    await message.answer("💎 Переход на PRO", reply_markup=pricing_kb())

@dp.message(F.text == "📅 Статус подписки")
async def sub_status(message: types.Message):
    s = get_sub(message.from_user.id)
    if s["active"]:
        await message.answer(f"👑 PRO активен\n📅 До: {s['until']}\n⏳ Осталось: {s['days_left']} дн.")
    else:
        await message.answer(
            "🆓 Бесплатный тариф.\n\n"
            "В PRO: 7+ дней, корреляции, экспорт, сводка, AI-коуч.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Купить PRO", callback_data="open_pro")]])
        )

@dp.callback_query(F.data == "open_pro")
async def open_pro(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer("💎 Переход на PRO", reply_markup=pricing_kb())

@dp.callback_query(F.data == "pro_info")
async def pro_info(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "✨ В PRO входит:\n"
        "• 📊 Аналитика за 7+ дней\n"
        "• 🍽 Разбивка по приёмам\n"
        "• 🎯 Норма с учётом цели\n"
        "• 🤒 Связь симптомов с питанием\n"
        "• 🌙 Ежедневная сводка в 22:00\n"
        "• 🤖 Еженедельный AI-коуч\n"
        "• 📥 Экспорт CSV"
    )

# ─── TELEGRAM STARS ОПЛАТА ──────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("buy_"))
async def buy_tariff_stars(cb: types.CallbackQuery):
    await cb.answer()
    tariff = cb.data.split("_")[1]
    
    if tariff not in PRICES:
        return
    
    plan = PRICES[tariff]
    labels = {"1m": "1 месяц", "3m": "3 месяца", "1y": "1 год"}
    
    # Отправляем инвойс в звёздах
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title="NutriMind PRO подписка",
        description=f"Доступ к полной аналитике, экспорту и AI-коучу на {labels[tariff]}",
        payload=f"pro_{tariff}_{cb.from_user.id}",
        provider_token="",  # Для XTR (звёзд) токен пустой!
        currency="XTR",     # Валюта: Telegram Stars
        prices=[LabeledPrice(label=labels[tariff], amount=plan["stars"])],
        start_parameter=f"pay_{tariff}",
    )
    await cb.message.delete()

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    """Подтверждаем предзаказ (обязательно для Stars)"""
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    """Обработка успешной оплаты"""
    payload = message.successful_payment.invoice_payload  # e.g. "pro_1m_123456789"
    try:
        _, tariff, uid_str = payload.split("_")
        uid = int(uid_str)
    except:
        await message.answer("❌ Ошибка распознавания платежа. Обратитесь в поддержку.")
        return

    # Выдаём PRO
    days = {"1m": 30, "3m": 90, "1y": 365}.get(tariff, 30)
    until = activate_pro(uid, days)
    
    # Если это первый платёж — считаем его «основателем»
    try:
        c = db()
        r = c.execute("SELECT trial_used FROM users WHERE user_id=?", (uid,)).fetchone()
        c.close()
        if r and not r[0]:
            increment_founder()
    except:
        pass
    
    # Реферальный бонус
    try:
        c = db()
        r = c.execute("SELECT referred_by FROM users WHERE user_id=?", (uid,)).fetchone()
        c.close()
        if r and r[0]:
            activate_pro(r[0], 7)
    except:
        pass

    # Сообщаем пользователю
    await message.answer(
        f"🎉 <b>Оплата прошла!</b>\n\n"
        f"PRO подписка активирована до {until}.\n"
        f"Спасибо за поддержку проекта! 🙏",
        parse_mode="HTML"
    )
    logger.info(f"🤑 PAYMENT SUCCESS: User {uid} paid for {tariff} with Stars")

# ─── НЕИЗВЕСТНЫЕ СООБЩЕНИЯ ──────────────────────────────────────────────────
@dp.message()
async def unknown(message: types.Message, state: FSMContext):
    cur = await state.get_state()
    if cur:
        return
    await message.answer(
        "Используй кнопки меню 👇\nЕсли меню пропало — напиши /start",
        reply_markup=main_kb
    )

# ─── ФОНОВЫЕ ЗАДАЧИ ─────────────────────────────────────────────────────────
async def job_check_subs():
    try:
        conn = db()
        rows = conn.execute(
            "SELECT user_id,pro_until,warned_3d,warned_1d FROM users WHERE is_pro=1 AND pro_until IS NOT NULL"
        ).fetchall()
        conn.close()
        now = datetime.now()
        for uid_, until, w3, w1 in rows:
            try:
                end = datetime.strptime(until, "%Y-%m-%d %H:%M")
                diff = (end - now).days
                if diff == 3 and not w3:
                    await bot.send_message(uid_, "⏳ PRO истекает через 3 дня. Продли в меню «Купить PRO».")
                    cc = db()
                    cc.execute("UPDATE users SET warned_3d=1 WHERE user_id=?", (uid_,))
                    cc.commit()
                    cc.close()
                elif diff == 1 and not w1:
                    await bot.send_message(uid_, "🚨 PRO истекает завтра! Продли сейчас.")
                    cc = db()
                    cc.execute("UPDATE users SET warned_1d=1 WHERE user_id=?", (uid_,))
                    cc.commit()
                    cc.close()
                elif diff < 0:
                    deactivate_pro(uid_)
                    try:
                        await bot.send_message(uid_, "😔 PRO истёк. Записи сохранены.\n💎 Продли в меню «Купить PRO».")
                    except:
                        pass
            except Exception as ex:
                logger.error(f"sub_exp {uid_}: {ex}")
    except Exception as ex:
        logger.error(f"job_check_subs: {ex}")

async def job_daily_summary():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = db()
        uids = conn.execute(
            "SELECT DISTINCT user_id FROM food_logs WHERE recorded_at >=?",
            (today,)
        ).fetchall()
        conn.close()
        for (uid_,) in uids:
            try:
                rows = get_today_logs(uid_)
                if not rows:
                    continue
                stats = get_stats(rows)
                p = get_profile(uid_)
                maint, goal = calc_tdee(p)
                s = get_streak(uid_)
                msg = f"🌙 Итог дня\n\n🔥 Калорий: {stats['kcal']} ккал"
                if goal:
                    diff = stats['kcal'] - goal
                    if diff > 100:
                        msg += f" (⚠️ +{diff} сверх нормы)"
                    elif diff < -100:
                        msg += f" (✅ дефицит {abs(diff)} ккал)"
                    else:
                        msg += " (🎯 в норме)"
                msg += f"\n📊 Б:{stats['p']:.0f}г / Ж:{stats['f']:.0f}г / У:{stats['c']:.0f}г"
                w = get_today_water(uid_)
                if w > 0:
                    msg += f"\n💧 Воды: {w} мл"
                    if w < WATER_GOAL:
                        msg += f" (осталось {WATER_GOAL - w} мл)"
                msg += f"\n🍽 Приёмов: {len(set(r[2] for r in rows))}"
                if s >= 3:
                    msg += f"\n{streak_msg(s)}"
                if not get_sub(uid_)["active"]:
                    msg += f"\n\n💎 PRO от {PRICES['1m']['new']}/мес — расширенная аналитика"
                msg += f"\n\n📣 {CHANNEL}"
                await bot.send_message(uid_, msg)
            except Exception as ex:
                logger.error(f"daily {uid_}: {ex}")
    except Exception as ex:
        logger.error(f"job_daily_summary: {ex}")

async def job_weekly_coach():
    try:
        conn = db()
        uids = conn.execute("SELECT user_id FROM users WHERE is_pro=1").fetchall()
        conn.close()
        for (uid_,) in uids:
            try:
                coach_text = await ai_weekly_coach(uid_)
                if coach_text:
                    await bot.send_message(
                        uid_,
                        f"🤖 Еженедельный отчёт\n\n{e(coach_text)}\n\n📊 Детальная аналитика: /start → 📊 Аналитика"
                    )
            except Exception as ex:
                logger.error(f"weekly_coach {uid_}: {ex}")
    except Exception as ex:
        logger.error(f"job_weekly_coach: {ex}")

async def job_water_reminder():
    try:
        conn = db()
        uids = conn.execute("SELECT user_id FROM users").fetchall()
        conn.close()
        for (uid_,) in uids:
            try:
                w = get_today_water(uid_)
                if w < WATER_GOAL // 2:
                    await bot.send_message(uid_, f"💧 Не забывай пить воду! Сегодня: {w} / {WATER_GOAL} мл.")
            except:
                pass
    except Exception as ex:
        logger.error(f"job_water: {ex}")

# ─── MAIN ───────────────────────────────────────────────────────────────────
async def main():
    init_db()
    if YANDEX_KEY:
        test = await yandex_post("Скажи 'ok'", timeout=10)
        if test:
            logger.info("✅ Yandex API: OK")
        else:
            logger.warning("⚠️ Yandex API не отвечает при старте!")
    else:
        logger.error("❌ YANDEX_API_KEY не задан!")
    
    scheduler.add_job(job_check_subs, 'cron', hour=9, minute=0)
    scheduler.add_job(job_daily_summary, 'cron', hour=22, minute=0)
    scheduler.add_job(job_weekly_coach, 'cron', day_of_week='sun', hour=20, minute=0)
    scheduler.add_job(job_water_reminder, 'cron', hour=12, minute=0)
    scheduler.add_job(job_water_reminder, 'cron', hour=17, minute=0)
    scheduler.start()
    logger.info("🚀 NutriMind запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
=======
import asyncio
import logging
import os
import re
import httpx
import json
import sqlite3
import csv
import io
import base64
import html
import secrets
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, LabeledPrice, PreCheckoutQuery
)
from aiogram.enums import ChatAction, ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Загрузка переменных ─────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YANDEX_KEY = os.getenv("YANDEX_API_KEY", "")
YANDEX_FOLDER = os.getenv("YANDEX_FOLDER_ID", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
BOT_USERNAME = os.getenv("BOT_USERNAME", "nutrimind_bot")
CHANNEL = os.getenv("CHANNEL", "https://t.me/NutriMindi")
WATER_GOAL = 2000

# Ссылка на твой Mini App
MINI_APP_URL = "https://maxi0154.github.io/nutrimind/"

# ─── Цены ───────────────────────────────────────────────────────────────────
PRICES = {
    "1m": {"old": "249₽", "new": "149₽", "days": 30, "per": "4₽/день", "stars": 120},
    "3m": {"old": "599₽", "new": "299₽", "days": 90, "per": "3₽/день", "stars": 299},
    "1y": {"old": "1990₽", "new": "990₽", "days": 365, "per": "2.7₽/день", "stars": 990},
}

# ─── Логгер ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ─── Глобальные ─────────────────────────────────────────────────────────────
_nutrition_cache: dict = {}
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()
DB_PATH = "data/nutrimind.db"

def e(text):
    return html.escape(str(text)) if text else ""

# ─── БД ─────────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    
    # Таблицы
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, username TEXT,
        is_pro INTEGER DEFAULT 0, pro_until TEXT,
        warned_3d INTEGER DEFAULT 0, warned_1d INTEGER DEFAULT 0,
        ref_code TEXT, referred_by INTEGER,
        onboarded INTEGER DEFAULT 0, trial_used INTEGER DEFAULT 0,
        streak_days INTEGER DEFAULT 0, streak_last TEXT,
        created_at TEXT DEFAULT(datetime('now')))""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS food_logs(
        id INTEGER PRIMARY KEY, user_id INTEGER, meal_type TEXT, food_name TEXT,
        grams REAL, kcal INTEGER, protein REAL, fat REAL, carbs REAL, recorded_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS user_profile(
        user_id INTEGER PRIMARY KEY, current_weight REAL, target_weight REAL,
        height INTEGER, age INTEGER, gender TEXT, updated_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS symptoms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, symptom TEXT, note TEXT, recorded_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS workouts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, workout_type TEXT, note TEXT, recorded_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS water_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, amount_ml INTEGER, recorded_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS weight_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, weight REAL, recorded_at TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT)""")
    c.execute("INSERT OR IGNORE INTO config(key,value) VALUES('founders_count','0')")
    
    # Индексы
    c.execute("CREATE INDEX IF NOT EXISTS idx_food ON food_logs(user_id,recorded_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sym ON symptoms(user_id,recorded_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_wrk ON workouts(user_id,recorded_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_wat ON water_logs(user_id,recorded_at)")
    
    # Миграции для старых БД
    migrations = [
        "ALTER TABLE users ADD COLUMN onboarded INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN trial_used INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN streak_days INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN streak_last TEXT",
        "ALTER TABLE users ADD COLUMN ref_code TEXT",
        "ALTER TABLE users ADD COLUMN referred_by INTEGER",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except:
            pass
    
    conn.commit()
    conn.close()
    logger.info("✅ БД инициализирована")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def ensure_user(uid, uname=None):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users(user_id,username,ref_code) VALUES(?,?,?)",
        (uid, uname or "", secrets.token_hex(4))
    )
    if uname:
        conn.execute("UPDATE users SET username=? WHERE user_id=?", (uname, uid))
    conn.commit()
    conn.close()

def get_user(uid):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row

def get_sub(uid):
    conn = db()
    row = conn.execute("SELECT is_pro,pro_until FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if not row or not row[0]:
        return {"active": False, "days_left": 0}
    if not row[1]:
        return {"active": False, "days_left": 0}
    try:
        end = datetime.strptime(row[1], "%Y-%m-%d %H:%M")
        diff = (end - datetime.now()).days
        if diff < 0:
            deactivate_pro(uid)
            return {"active": False, "days_left": 0}
        return {"active": True, "until": row[1], "days_left": max(0, diff)}
    except:
        return {"active": False, "days_left": 0}

def activate_pro(uid, days=30):
    conn = db()
    row = conn.execute("SELECT pro_until FROM users WHERE user_id=?", (uid,)).fetchone()
    base = datetime.now()
    if row and row[0]:
        try:
            ex = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
            if ex > base:
                base = ex
        except:
            pass
    until = (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "UPDATE users SET is_pro=1,pro_until=?,warned_3d=0,warned_1d=0 WHERE user_id=?",
        (until, uid)
    )
    conn.commit()
    conn.close()
    return until

def deactivate_pro(uid):
    conn = db()
    conn.execute("UPDATE users SET is_pro=0,pro_until=NULL WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def activate_trial(uid):
    conn = db()
    row = conn.execute("SELECT trial_used FROM users WHERE user_id=?", (uid,)).fetchone()
    if row and row[0]:
        conn.close()
        return None
    conn.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()
    return activate_pro(uid, 3)

def get_founder_status():
    conn = db()
    row = conn.execute("SELECT value FROM config WHERE key='founders_count'").fetchone()
    conn.close()
    count = int(row[0]) if row else 0
    return count < 100, count, 100

def increment_founder():
    conn = db()
    conn.execute("UPDATE config SET value=CAST(value AS INTEGER)+1 WHERE key='founders_count'")
    conn.commit()
    conn.close()

def delete_user_data(uid):
    conn = db()
    for table in ["food_logs", "user_profile", "symptoms", "workouts", "water_logs", "weight_logs", "users"]:
        conn.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def save_log(uid, meal, name, grams=0, kcal=0, p=0, f=0, c_=0, date=None):
    ensure_user(uid)
    conn = db()
    conn.execute(
        "INSERT INTO food_logs(user_id,meal_type,food_name,grams,kcal,protein,fat,carbs,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, meal, str(name).strip(), grams, kcal, p, f, c_, date or datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    _update_streak(uid)
    return lid

def _update_streak(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = db()
    row = conn.execute("SELECT streak_days,streak_last FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        conn.close()
        return
    streak, last = row
    streak = streak or 0
    if last == today:
        conn.close()
        return
    streak = (streak + 1) if last == yesterday else 1
    conn.execute("UPDATE users SET streak_days=?,streak_last=? WHERE user_id=?", (streak, today, uid))
    conn.commit()
    conn.close()

def get_streak(uid):
    conn = db()
    row = conn.execute("SELECT streak_days,streak_last FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if not row or not row[1]:
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return (row[0] or 0) if row[1] in (today, yesterday) else 0

def update_log(lid, name, grams, kcal, p, f, c_):
    conn = db()
    conn.execute(
        "UPDATE food_logs SET food_name=?,grams=?,kcal=?,protein=?,fat=?,carbs=? WHERE id=?",
        (name, grams, kcal, p, f, c_, lid)
    )
    conn.commit()
    conn.close()

def save_symptom(uid, symptom, note=""):
    conn = db()
    conn.execute(
        "INSERT INTO symptoms(user_id,symptom,note,recorded_at) VALUES(?,?,?,?)",
        (uid, symptom, note, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()

def save_workout(uid, wtype, note=""):
    conn = db()
    conn.execute(
        "INSERT INTO workouts(user_id,workout_type,note,recorded_at) VALUES(?,?,?,?)",
        (uid, wtype, note, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()

def log_water(uid, ml):
    conn = db()
    conn.execute(
        "INSERT INTO water_logs(user_id,amount_ml,recorded_at) VALUES(?,?,?)",
        (uid, ml, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()

def get_today_water(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = db()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_ml),0) FROM water_logs WHERE user_id=? AND recorded_at>=?",
        (uid, today)
    ).fetchone()
    conn.close()
    return row[0] if row else 0

def log_weight(uid, w):
    conn = db()
    conn.execute(
        "INSERT INTO weight_logs(user_id,weight,recorded_at) VALUES(?,?,?)",
        (uid, w, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.execute("UPDATE user_profile SET current_weight=? WHERE user_id=?", (w, uid))
    conn.commit()
    conn.close()

def get_weight_history(uid, limit=7):
    conn = db()
    rows = conn.execute(
        "SELECT weight,recorded_at FROM weight_logs WHERE user_id=? ORDER BY recorded_at DESC LIMIT ?",
        (uid, limit)
    ).fetchall()
    conn.close()
    return rows

def get_symptoms(uid, days=None):
    conn = db()
    if days:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT symptom,note,recorded_at FROM symptoms WHERE user_id=? AND recorded_at>=? ORDER BY recorded_at DESC",
            (uid, start)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT symptom,note,recorded_at FROM symptoms WHERE user_id=? ORDER BY recorded_at DESC",
            (uid,)
        ).fetchall()
    conn.close()
    return rows

def get_workouts(uid, days=None):
    conn = db()
    if days:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT workout_type,note,recorded_at FROM workouts WHERE user_id=? AND recorded_at>=? ORDER BY recorded_at DESC",
            (uid, start)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT workout_type,note,recorded_at FROM workouts WHERE user_id=? ORDER BY recorded_at DESC",
            (uid,)
        ).fetchall()
    conn.close()
    return rows

def save_profile(uid, weight=None, target=None, height=None, age=None, gender=None):
    ensure_user(uid)
    conn = db()
    exists = conn.execute("SELECT 1 FROM user_profile WHERE user_id=?", (uid,)).fetchone()
    if exists:
        conn.execute(
            """UPDATE user_profile SET
            current_weight=COALESCE(?,current_weight), target_weight=COALESCE(?,target_weight),
            height=COALESCE(?,height), age=COALESCE(?,age), gender=COALESCE(?,gender), updated_at=?
            WHERE user_id=?""",
            (weight, target, height, age, gender, datetime.now().strftime("%Y-%m-%d"), uid)
        )
    else:
        conn.execute(
            "INSERT INTO user_profile(user_id,current_weight,target_weight,height,age,gender,updated_at) VALUES(?,?,?,?,?,?,?)",
            (uid, weight, target, height, age, gender, datetime.now().strftime("%Y-%m-%d"))
        )
    conn.commit()
    conn.close()

def upd_profile_field(uid, field, val):
    field_map = {"weight": "current_weight", "target": "target_weight", "height": "height", "age": "age"}
    dbf = field_map.get(field)
    if not dbf:
        return
    conn = db()
    conn.execute(f"UPDATE user_profile SET {dbf}=? WHERE user_id=?", (val, uid))
    conn.commit()
    conn.close()

def get_profile(uid):
    conn = db()
    row = conn.execute("SELECT * FROM user_profile WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row

def get_logs(uid, days=None):
    conn = db()
    if days:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM food_logs WHERE user_id=? AND recorded_at>=? ORDER BY recorded_at DESC",
            (uid, start)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM food_logs WHERE user_id=? ORDER BY recorded_at DESC",
            (uid,)
        ).fetchall()
    conn.close()
    return rows

def get_today_logs(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = db()
    rows = conn.execute(
        "SELECT * FROM food_logs WHERE user_id=? AND recorded_at>=? ORDER BY recorded_at",
        (uid, today)
    ).fetchall()
    conn.close()
    return rows

def get_stats(rows):
    return {
        "kcal": sum(r[5] or 0 for r in rows),
        "p": sum(r[6] or 0 for r in rows),
        "f": sum(r[7] or 0 for r in rows),
        "c": sum(r[8] or 0 for r in rows),
        "count": len(rows)
    }

def get_all_logs_csv(uid):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM food_logs WHERE user_id=? ORDER BY recorded_at DESC",
        (uid,)
    ).fetchall()
    conn.close()
    return rows

def get_all_pro():
    conn = db()
    rows = conn.execute(
        "SELECT user_id,username,pro_until FROM users WHERE is_pro=1 ORDER BY pro_until"
    ).fetchall()
    conn.close()
    return rows

def get_counts():
    conn = db()
    total = conn.execute("SELECT COUNT() FROM users").fetchone()[0]
    pro = conn.execute("SELECT COUNT() FROM users WHERE is_pro=1").fetchone()[0]
    conn.close()
    return total, pro

def get_ref_code(uid):
    conn = db()
    row = conn.execute("SELECT ref_code FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row[0] if row else None

def find_by_ref(code):
    conn = db()
    row = conn.execute("SELECT user_id FROM users WHERE ref_code=?", (code,)).fetchone()
    conn.close()
    return row[0] if row else None

def set_referred(uid, ref_uid):
    conn = db()
    conn.execute(
        "UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL",
        (ref_uid, uid)
    )
    conn.commit()
    conn.close()

# ─── Расчёты ────────────────────────────────────────────────────────────────
def calc_tdee(profile):
    if not profile or not profile[1]:
        return None, None
    g = profile[5] or "male"
    age = profile[4] or 25
    h = profile[3] or 170
    w = profile[1]
    t = profile[2]
    bmr = 10 * w + 6.25 * h - 5 * age + (5 if g == "male" else -161)
    maint = int(bmr * 1.375)
    if t and t < w:
        goal = max(1200, maint - min(500, max(200, int((w - t) * 25))))
    elif t and t > w:
        goal = maint + 200
    else:
        goal = maint
    return maint, goal

def calc_macros(kcal):
    return int(kcal * 0.25 / 4), int(kcal * 0.30 / 9), int(kcal * 0.45 / 4)

def water_bar(total):
    pct = min(100, int(total / WATER_GOAL * 100))
    return "🟦" * (pct // 10) + "⬜" * (10 - pct // 10), pct

def streak_msg(days):
    if days >= 30:
        return f"🏆 {days} дней подряд — феноменально!"
    if days >= 14:
        return f"🥇 {days} дней подряд — отличная серия!"
    if days >= 7:
        return f"🔥 {days} дней подряд!"
    if days >= 3:
        return f"✨ {days} дня подряд"
    return ""

# ─── AI ─────────────────────────────────────────────────────────────────────
def safe_num(v, d=0):
    try:
        return float(str(v).replace(",", ".").strip()) if v not in (None, "", "null") else d
    except:
        return d

def extract_json(text):
    text = text.replace("```json", "").replace("```", "").strip()
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(text[start:i+1])
                except:
                    start = -1
                    depth = 0
    return None

async def yandex_post(prompt, model=None, temperature=0.1, timeout=35):
    if not YANDEX_KEY:
        return None
    headers = {
        "Authorization": f"Api-Key {YANDEX_KEY}",
        "Content-Type": "application/json",
        "x-folder-id": YANDEX_FOLDER
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers=headers,
                json={
                    "modelUri": model or f"gpt://{YANDEX_FOLDER}/yandexgpt/latest",
                    "completionOptions": {"stream": False, "temperature": temperature},
                    "messages": [{"role": "user", "text": prompt}]
                }
            )
            r.raise_for_status()
            return r.json()["result"]["alternatives"][0]["message"]["text"]
    except httpx.TimeoutException:
        logger.error("Yandex API: TIMEOUT")
        return None
    except Exception as ex:
        logger.error(f"Yandex API: {ex}")
        return None

async def ai_nutrition(text=None, photo_bytes=None):
    if not YANDEX_KEY or not YANDEX_FOLDER:
        logger.error("ai_nutrition: API key missing")
        return {"error": "no_api_key"}
    
    if text:
        ck = text.lower().strip()[:80]
        if ck in _nutrition_cache:
            logger.info(f"Cache hit: {ck[:30]}")
            return _nutrition_cache[ck]
    
    headers = {
        "Authorization": f"Api-Key {YANDEX_KEY}",
        "Content-Type": "application/json",
        "x-folder-id": YANDEX_FOLDER
    }
    
    if photo_bytes:
        if not isinstance(photo_bytes, bytes):
            logger.error(f"photo_bytes is {type(photo_bytes)}, not bytes!")
            return {"error": "failed"}
        b64 = base64.b64encode(photo_bytes).decode()
        model = f"gpt://{YANDEX_FOLDER}/yandexgpt/vision-latest"
        prompt = (
            "Ты нутрициолог. Что на фото?\n"
            "Определи блюдо, оцени вес (grams — только твёрдая еда, без жидкостей), посчитай КБЖУ.\n"
            "Если НЕ ЕДА — верни: {\"error\":\"not_food\"}\n"
            "Иначе верни ТОЛЬКО JSON:\n"
            "{\"food_desc\":\"краткое описание состава\",\"food\":\"название блюда\","
            "\"grams\":300,\"kcal\":450,\"p\":20,\"f\":15,\"c\":55}"
        )
        msgs = [{"role": "user", "content": [{"type": "image", "data": b64}, {"type": "text", "text": prompt}]}]
    else:
        model = f"gpt://{YANDEX_FOLDER}/yandexgpt/latest"
        prompt = (
            f"Еда: {text}\n\n"
            "Ты нутрициолог. Посчитай КБЖУ для всего перечисленного.\n"
            "Правила:\n"
            "- Если указан вес — используй его точно\n"
            "- Жидкости (чай, кофе, сок, пиво, вино) — считай их калории, но НЕ включай объём в grams\n"
            "- grams = суммарный вес только твёрдой еды\n"
            "- food = краткое название (что написал пользователь, можно уточнить)\n"
            "Верни ТОЛЬКО JSON без пояснений:\n"
            "{\"food_desc\":\"описание с весами каждой позиции\","
            "\"food\":\"название\",\"grams\":300,\"kcal\":450,\"p\":20,\"f\":15,\"c\":55}\n"
            "Если это не еда — {\"error\":\"not_food\"}"
        )
        msgs = [{"role": "user", "text": prompt}]
    
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            r = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers=headers,
                json={
                    "modelUri": model,
                    "completionOptions": {"stream": False, "temperature": 0.1},
                    "messages": msgs
                }
            )
            if r.status_code != 200:
                logger.error(f"ai_nutrition HTTP {r.status_code}: {r.text[:200]}")
                return {"error": "failed"}
            raw = r.json()["result"]["alternatives"][0]["message"]["text"]
            logger.info(f"AI raw response: {raw[:100]}")
            res = extract_json(raw)
            if not res:
                logger.error(f"ai_nutrition: no JSON in: {raw[:200]}")
                return {"error": "failed"}
            if "error" in res:
                return {"error": res["error"]}
            result = {
                "food_desc": str(res.get("food_desc", " "))[:300],
                "food": str(res.get("food", text or "Блюдо"))[:100],
                "grams": safe_num(res.get("grams"), 0),
                "kcal": int(safe_num(res.get("kcal"))),
                "p": safe_num(res.get("p")),
                "f": safe_num(res.get("f")),
                "c": safe_num(res.get("c")),
            }
            if text and len(_nutrition_cache) < 500:
                _nutrition_cache[text.lower().strip()[:80]] = result
            return result
    except httpx.TimeoutException:
        logger.error("ai_nutrition: TIMEOUT after 35s")
        return {"error": "timeout"}
    except Exception as ex:
        logger.error(f"ai_nutrition exception: {ex}", exc_info=True)
        return {"error": "failed"}

async def ai_recalc(food_name, grams):
    raw = await yandex_post(
        f'КБЖУ для "{food_name}", {grams}г. Только JSON:\n{{"kcal":400, "p":20, "f":15, "c":45}}'
    )
    if not raw:
        return {"error": "failed"}
    res = extract_json(raw)
    if not res:
        return {"error": "failed"}
    return {
        "kcal": int(safe_num(res.get("kcal"))),
        "p": safe_num(res.get("p")),
        "f": safe_num(res.get("f")),
        "c": safe_num(res.get("c"))
    }

async def ai_analysis(stats, actual_days, syms, wrks, profile=None, is_pro=False):
    if not YANDEX_KEY:
        return f"За период: {stats['kcal']} ккал, Б{stats['p']:.0f} Ж{stats['f']:.0f} У{stats['c']:.0f}."
    
    maint, goal = calc_tdee(profile)
    avg = stats['kcal'] // actual_days if actual_days > 0 else stats['kcal']
    
    prof_ctx = ""
    if profile and profile[1]:
        w = profile[1]
        t = profile[2]
        g = profile[5] or "male"
        age = profile[4] or 25
        h = profile[3] or 170
        prof_ctx = f"Человек: {w}кг → цель {t}кг, {h}см, {age}лет, {'мужчина' if g == 'male' else 'женщина'}. "
        if goal:
            deficit = maint - goal
            pn, fn, cn = calc_macros(goal)
            if t and t < w:
                prof_ctx += f"Для похудения норма {goal} ккал/день (дефицит {deficit} ккал). БЖУ: Б{pn}г Ж{fn}г У{cn}г. "
            else:
                prof_ctx += f"Норма {goal} ккал/день. БЖУ: Б{pn}г Ж{fn}г У{cn}г. "
    
    kcal_diff = avg - goal if goal else None
    sym_ctx = ""
    if syms:
        sym_ctx = "Симптомы за период: " + ", ".join(s[0] + (f"({s[1]})" if s[1] else "") for s in syms[-8:]) + ". "
    wrk_ctx = ""
    if wrks:
        wrk_ctx = "Тренировки: " + ", ".join(w[0] for w in wrks[-8:]) + f" (всего {len(wrks)} шт). "
    
    if is_pro:
        diff_str = ""
        if kcal_diff is not None:
            if kcal_diff > 0:
                diff_str = f"Переедание в среднем на {kcal_diff} ккал/день. "
            elif kcal_diff < 0:
                diff_str = f"Дефицит в среднем {abs(kcal_diff)} ккал/день. "
            else:
                diff_str = "В норме по калориям. "
        
        bju_ctx = ""
        if goal:
            pn, fn, cn = calc_macros(goal)
            p_diff = round(stats['p'] / actual_days - pn, 1)
            f_diff = round(stats['f'] / actual_days - fn, 1)
            c_diff = round(stats['c'] / actual_days - cn, 1)
            bju_ctx = (
                f"Среднее БЖУ в день: Б{stats['p']/actual_days:.0f}г (норма {pn}г, "
                f"{'↑'+str(p_diff) if p_diff > 0 else '↓'+str(abs(p_diff))}г), "
                f"Ж{stats['f']/actual_days:.0f}г (норма {fn}г, "
                f"{'↑'+str(f_diff) if f_diff > 0 else '↓'+str(abs(f_diff))}г), "
                f"У{stats['c']/actual_days:.0f}г (норма {cn}г, "
                f"{'↑'+str(c_diff) if c_diff > 0 else '↓'+str(abs(c_diff))}г). "
            )
        
        prompt = (
            f"Ты профессиональный нутрициолог. Сделай детальный разбор питания.\n"
            f"ДАННЫЕ:\n{prof_ctx}Период: {actual_days} дней. Среднее {avg} ккал/день. {diff_str}{bju_ctx}{sym_ctx}{wrk_ctx}\n"
            "ЗАДАЧА: 5-7 предложений живым текстом. Оцени калории vs норма, БЖУ, связь симптомов/тренировок с питанием. "
            "2 конкретные рекомендации. Без воды."
        )
    else:
        prompt = (
            f"Ты нутрициолог. {prof_ctx}Питание за {actual_days} дней: в среднем {avg} ккал/день, "
            f"Б{stats['p']/actual_days:.0f}г Ж{stats['f']/actual_days:.0f}г У{stats['c']/actual_days:.0f}г.{sym_ctx}\n"
            "Напиши 2-3 предложения: результат по калориям и одна практическая рекомендация. Без воды."
        )
    
    res = await yandex_post(prompt, temperature=0.35, timeout=35)
    return res or "Анализ временно недоступен."

async def ai_voice_to_text(ogg_bytes):
    stt_key = os.getenv("YANDEX_STT_KEY", YANDEX_KEY)
    if not stt_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
                headers={"Authorization": f"Api-Key {stt_key}"},
                params={"folderId": YANDEX_FOLDER, "lang": "ru-RU", "format": "oggopus"},
                content=ogg_bytes
            )
            if r.status_code == 200:
                result = r.json().get("result", "").strip()
                logger.info(f"STT result: {result}")
                return result if result else None
            else:
                logger.error(f"STT HTTP {r.status_code}: {r.text[:100]}")
    except Exception as ex:
        logger.error(f"STT: {ex}")
    return None

async def lookup_barcode(barcode):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json",
                headers={"User-Agent": "NutriMindBot/1.0"}
            )
            if r.status_code != 200 or r.json().get("status") != 1:
                return None
            p = r.json()["product"]
            n = p.get("nutriments", {})
            name = p.get("product_name_ru") or p.get("product_name") or "Продукт"
            kcal = safe_num(n.get("energy-kcal_100g") or n.get("energy_100g", 0) / 4.184)
            return {
                "name": name,
                "kcal_100g": kcal,
                "p_100g": safe_num(n.get("proteins_100g", 0)),
                "f_100g": safe_num(n.get("fat_100g", 0)),
                "c_100g": safe_num(n.get("carbohydrates_100g", 0))
            }
    except Exception as ex:
        logger.error(f"barcode: {ex}")
        return None

# ─── FSM ────────────────────────────────────────────────────────────────────
class WaitFood(StatesGroup):
    input = State()

class ProfileFS(StatesGroup):
    input = State()

class CorrectFS(StatesGroup):
    name = State()
    grams = State()

class SymNote(StatesGroup):
    input = State()

class WrkNote(StatesGroup):
    input = State()

class DelConfirm(StatesGroup):
    waiting = State()

class WaterIn(StatesGroup):
    input = State()

class WeightIn(StatesGroup):
    input = State()

class BarcodeWeight(StatesGroup):
    input = State()

# ─── Клавиатуры ─────────────────────────────────────────────────────────────
main_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🍳 Завтрак"), KeyboardButton(text="🥗 Обед")],
    [KeyboardButton(text="🍲 Ужин"), KeyboardButton(text="🍎 Перекус")],
    [KeyboardButton(text="💧 Вода"), KeyboardButton(text="⚖️ Мой вес")],
    [KeyboardButton(text="🤒 Симптом"), KeyboardButton(text="💪 Тренировка")],
    [KeyboardButton(text="📅 Вчера"), KeyboardButton(text="📊 Аналитика")],
    [KeyboardButton(text="📱 Приложение", web_app=WebAppInfo(url=MINI_APP_URL)), KeyboardButton(text="🆘 Помощь")]
], resize_keyboard=True)

settings_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💎 Купить PRO")],
    [KeyboardButton(text="📥 Экспорт"), KeyboardButton(text="📅 Статус подписки")],
    [KeyboardButton(text="🔗 Реферальная ссылка"), KeyboardButton(text="🗑 Удалить мои данные")],
    [KeyboardButton(text="⬅️ Назад")]
], resize_keyboard=True)

back_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="◀️ Назад")]], resize_keyboard=True)

symptom_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🤕 Голова", callback_data="sym_Головная боль"),
     InlineKeyboardButton(text="🤧 Насморк", callback_data="sym_Насморк")],
    [InlineKeyboardButton(text="😴 Усталость", callback_data="sym_Усталость"),
     InlineKeyboardButton(text="🤢 Тошнота", callback_data="sym_Тошнота")],
    [InlineKeyboardButton(text="😣 Живот", callback_data="sym_Боль в животе"),
     InlineKeyboardButton(text="🌸 Аллергия", callback_data="sym_Аллергия")],
    [InlineKeyboardButton(text="🌡 Температура", callback_data="sym_Температура"),
     InlineKeyboardButton(text="😰 Слабость", callback_data="sym_Слабость")],
    [InlineKeyboardButton(text="✏️ Другое", callback_data="sym_other")],
])

workout_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🫀 Кардио", callback_data="wrk_Кардио"),
     InlineKeyboardButton(text="💪 Грудь", callback_data="wrk_Грудь")],
    [InlineKeyboardButton(text="🦵 Ноги", callback_data="wrk_Ноги"),
     InlineKeyboardButton(text="🔙 Спина", callback_data="wrk_Спина")],
    [InlineKeyboardButton(text="💪 Руки", callback_data="wrk_Руки"),
     InlineKeyboardButton(text="🎯 Пресс", callback_data="wrk_Пресс")],
    [InlineKeyboardButton(text="🤸 Растяжка", callback_data="wrk_Растяжка"),
     InlineKeyboardButton(text="🏃 Бег", callback_data="wrk_Бег")],
    [InlineKeyboardButton(text="✏️ Другое", callback_data="wrk_other")],
])

water_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="💧 +250мл", callback_data="water_250"),
     InlineKeyboardButton(text="💧 +500мл", callback_data="water_500")],
    [InlineKeyboardButton(text="💧 +750мл", callback_data="water_750"),
     InlineKeyboardButton(text="💧 +1000мл", callback_data="water_1000")],
    [InlineKeyboardButton(text="✏️ Другое", callback_data="water_custom")],
])

def pricing_kb():
    labels = {"1m": "1 месяц", "3m": "3 месяца", "1y": "1 год"}
    rows = []
    for k, p in PRICES.items():
        rows.append([InlineKeyboardButton(
            text=f"💎 {labels[k]} — {p['stars']}⭐ ({p['per']})",
            callback_data=f"buy_{k}"
        )])
    rows.append([InlineKeyboardButton(text="❓ Что входит в PRO?", callback_data="pro_info")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

MEAL_BTNS = {"🍳 Завтрак", "🥗 Обед", "🍲 Ужин", "🍎 Перекус"}
ALL_MENU = MEAL_BTNS | {
    "📅 Вчера", "📊 Аналитика", "⚙️ Ещё", "🆘 Помощь", "⬅️ Назад", "◀️ Назад",
    "🤒 Симптом", "💪 Тренировка", "💧 Вода", "⚖️ Мой вес",
    "👤 Профиль", "💎 Купить PRO", "📥 Экспорт", "📅 Статус подписки",
    "🔗 Реферальная ссылка", "🗑 Удалить мои данные"
}
MEALS = {"🍳 Завтрак": "breakfast", "🥗 Обед": "lunch", "🍲 Ужин": "dinner", "🍎 Перекус": "snack"}

async def safe_send(message, text, **kwargs):
    try:
        return await message.answer(text, **kwargs)
    except Exception as ex1:
        logger.warning(f"safe_send HTML failed: {ex1}")
        try:
            plain = re.sub(r'<[^>]+>', '', text)
            return await message.answer(plain, parse_mode=None, **kwargs)
        except Exception as ex2:
            logger.error(f"safe_send plain also failed: {ex2}")
            try:
                return await message.answer("✅ Записано!", reply_markup=kwargs.get("reply_markup"), parse_mode=None)
            except:
                pass

# ─── Хендлеры ───────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    is_new = get_user(uid) is None
    ensure_user(uid, message.from_user.username)
    
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        ref = find_by_ref(args[1][4:])
        if ref and ref != uid:
            set_referred(uid, ref)
    
    user = get_user(uid)
    if is_new or (user and not user[8]):
        conn = db()
        conn.execute("UPDATE users SET onboarded=1 WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()
        trial = activate_trial(uid)
        trial_text = f"\n\n🎁 Активирован бесплатный PRO на 3 дня!\nПопробуй все функции до {trial[:10]}." if trial else ""
        await message.answer(
            f"👋 Привет! Я NutriMind — AI-нутрициолог.\n\n"
            f"Считаю КБЖУ, слежу за водой и весом, анализирую самочувствие.{trial_text}\n\n"
            f"📣 Советы по питанию: {CHANNEL}\n\n"
            f"Начнём — заполни профиль:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Заполнить профиль", callback_data="setup_profile")],
                [InlineKeyboardButton(text="Пропустить →", callback_data="skip_onboard")]
            ])
        )
    else:
        s = get_streak(uid)
        sm = f"\n{streak_msg(s)}" if s >= 3 else ""
        await message.answer(f"👋 С возвращением!{sm}", reply_markup=main_kb)

@dp.callback_query(F.data == "skip_onboard")
async def skip_onboard(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer("Профиль можно заполнить позже: ⚙️ Ещё → Профиль.\nПоехали! 👇", reply_markup=main_kb)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        f"📖 Как пользоваться:\n\n"
        f"• Еда: нажми приём пищи → напиши, сфотографируй или запиши голосом\n"
        f"  Можно несколько: «курица, рис 200г и чай»\n"
        f"• Штрихкод: отправь фото штрихкода упаковки\n"
        f"• Вода: 💧 Вода\n"
        f"• Вес: ⚖️ Мой вес\n"
        f"• Симптомы: 🤒 Симптом\n"
        f"• Тренировки: 💪 Тренировка\n"
        f"• Аналитика: 📊 Аналитика\n"
        f"• 📱 Приложение: открой удобный интерфейс для записи еды и аналитики!\n\n"
        f"📣 {CHANNEL}"
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    total, pro = get_counts()
    pro_users = get_all_pro()
    fc, ft = get_founder_status()
    text = f"📊 Админ-панель\n\n👥 Всего: {total}\n👑 PRO: {pro}\n🏅 Основатели: {fc}/{ft}\n\n"
    if pro_users:
        text += "PRO:\n"
        for uid_, un, until in pro_users:
            text += f"• {'@' + e(un) if un else 'ID:' + str(uid_)} — до {until}\n"
    else:
        text += "PRO пользователей нет."
    await message.answer(text)

@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Проверяю Yandex API...")
    result = await yandex_post("Скажи 'ok'", timeout=10)
    if result:
        await message.answer(f"✅ Yandex API работает.\nОтвет: {result[:80]}")
    else:
        await message.answer("❌ Yandex API не отвечает! Проверь ключ в Railway Variables.")

@dp.message(F.text.in_({"◀️ Назад", "❌ Отмена", "⬅️ Назад"}))
async def back_to_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👆 Главное меню:", reply_markup=main_kb)

@dp.message(F.text == "⚙️ Ещё")
async def open_settings(message: types.Message):
    await message.answer("⚙️ Настройки:", reply_markup=settings_kb)

@dp.message(F.text == "🆘 Помощь")
async def help_btn(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        return await message.answer("Ты админ. /admin — панель. /ping — проверка API.")
    await message.answer("🆘 Сообщение отправлено поддержке.")
    await bot.send_message(
        ADMIN_ID,
        f"🆨 @{e(message.from_user.username or 'нет')} (ID:{message.from_user.id})\n\n{e(message.text)}"
    )

# ─── ОБРАБОТКА ДАННЫХ ИЗ MINI APP ───────────────────────────────────────────
@dp.message(F.web_app_data)
async def handle_web_app_data(message: types.Message):
    """Получает данные из Mini App и обрабатывает их"""
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get("action")
        uid = message.from_user.id
        
        if action == "add_food":
            # Данные из Mini App: {"action":"add_food","meal":"breakfast","food":"Овсянка","grams":200,"kcal":350,"p":12,"f":6,"c":60}
            meal = data.get("meal", "snack")
            food = data.get("food", "Блюдо")
            grams = float(data.get("grams", 0))
            kcal = int(data.get("kcal", 0))
            p = float(data.get("p", 0))
            f = float(data.get("f", 0))
            c = float(data.get("c", 0))
            
            lid = save_log(uid, meal, food, grams, kcal, p, f, c)
            _update_streak(uid)
            
            await message.answer(f"✅ <b>{e(food)}</b> ({int(grams)}г) записано!\n🔥 {kcal} ккал | Б:{p:.1f} Ж:{f:.1f} У:{c:.1f}", parse_mode="HTML")
        
        elif action == "add_water":
            ml = int(data.get("ml", 0))
            log_water(uid, ml)
            total = get_today_water(uid)
            bar, pct = water_bar(total)
            await message.answer(f"✅ +{ml} мл воды!\n💧 Итого: {total} / {WATER_GOAL} мл\n{bar} {pct}%")
        
        elif action == "add_weight":
            w = float(data.get("weight", 0))
            log_weight(uid, w)
            await message.answer(f"✅ Вес {w} кг записан!")
        
        elif action == "add_symptom":
            symptom = data.get("symptom", "")
            note = data.get("note", "")
            save_symptom(uid, symptom, note)
            await message.answer(f"✅ Симптом «{e(symptom)}» записан.")
        
        elif action == "add_workout":
            workout = data.get("workout", "")
            note = data.get("note", "")
            save_workout(uid, workout, note)
            await message.answer(f"✅ Тренировка «{e(workout)}» записана.")
        
        else:
            await message.answer(f"📱 Получены данные из приложения: {data}")
            
    except json.JSONDecodeError:
        await message.answer("⚠️ Не удалось обработать данные из приложения.")
    except Exception as ex:
        logger.error(f"web_app_data error: {ex}")
        await message.answer("⚠️ Ошибка при обработке данных.")

# ─── ВОДА ───────────────────────────────────────────────────────────────────
@dp.message(F.text == "💧 Вода")
async def water_menu(message: types.Message):
    total = get_today_water(message.from_user.id)
    bar, pct = water_bar(total)
    await message.answer(
        f"💧 Вода сегодня: {total} / {WATER_GOAL} мл\n{bar} {pct}%\n\nДобавь:",
        reply_markup=water_kb
    )

@dp.callback_query(F.data.startswith("water_"))
async def water_add(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    amt = cb.data[6:]
    if amt == "custom":
        await state.set_state(WaterIn.input)
        await cb.message.answer("💧 Введи количество мл:", reply_markup=back_kb)
        return
    ml = int(amt)
    log_water(cb.from_user.id, ml)
    total = get_today_water(cb.from_user.id)
    bar, pct = water_bar(total)
    msg = f"✅ +{ml} мл\n💧 Итого: {total} / {WATER_GOAL} мл\n{bar} {pct}%"
    if total >= WATER_GOAL:
        msg += "\n\n🎉 Норма воды выполнена!"
    await cb.message.edit_text(msg)

@dp.message(WaterIn.input)
async def water_in(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return
    try:
        ml = int(message.text.strip().replace("мл", "").strip())
        if ml <= 0 or ml > 5000:
            raise ValueError
    except:
        return await message.answer("❌ Введи число от 1 до 5000")
    log_water(message.from_user.id, ml)
    await state.clear()
    await message.answer(
        f"✅ +{ml} мл\n💧 Итого: {get_today_water(message.from_user.id)} мл",
        reply_markup=main_kb
    )

# ─── ВЕС ────────────────────────────────────────────────────────────────────
@dp.message(F.text == "⚖️ Мой вес")
async def weight_menu(message: types.Message, state: FSMContext):
    history = get_weight_history(message.from_user.id)
    text = "⚖️ Журнал веса\n\n"
    if history:
        for w_, d in reversed(history[-5:]):
            text += f"• {d[:10]} — {w_} кг\n"
        if len(history) >= 2:
            diff = history[0][0] - history[-1][0]
            text += f"\n{'📉' if diff < 0 else '📈' if diff > 0 else '➡️'} За период: {diff:+.1f} кг"
    else:
        text += "Записей пока нет.\n"
    text += "\nВведи текущий вес (кг): "
    await state.set_state(WeightIn.input)
    await message.answer(text, reply_markup=back_kb)

@dp.message(WeightIn.input)
async def weight_in(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    try:
        w = float(message.text.strip().replace(",", ".").replace("кг", "").strip())
        if w < 20 or w > 500:
            raise ValueError
    except:
        return await message.answer("❌ Введи корректный вес, например: 75.5")
    log_weight(message.from_user.id, w)
    profile = get_profile(message.from_user.id)
    await state.clear()
    msg = f"✅ Вес {w} кг записан! "
    if profile and profile[2]:
        diff = w - profile[2]
        if diff > 0:
            msg += f"\n🎯 До цели {profile[2]} кг: {diff:.1f} кг"
        else:
            msg += f"\n🎉 Цель {profile[2]} кг достигнута!"
    await message.answer(msg, reply_markup=main_kb)

# ─── ЕДА ────────────────────────────────────────────────────────────────────
@dp.message(F.text.in_(MEAL_BTNS))
async def pick_meal(message: types.Message, state: FSMContext):
    await state.clear()
    await state.update_data(meal=MEALS[message.text], is_yesterday=False)
    await message.answer(
        f"✅ {e(message.text)}. Напиши что ел или:\n"
        f"• Отправь фото блюда\n"
        f"• Отправь голосовое\n"
        f"• Отправь фото штрихкода\n\n"
        f"Можно несколько: «скумбрия, 3 куска хлеба»",
        reply_markup=back_kb
    )
    await state.set_state(WaitFood.input)

@dp.message(F.text == "📅 Вчера")
async def yesterday_mode(message: types.Message, state: FSMContext):
    await state.clear()
    await state.update_data(is_yesterday=True)
    await message.answer(
        "📅 За ВЧЕРА. Выбери приём:",
        reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🍳 Завтрак"), KeyboardButton(text="🥗 Обед")],
            [KeyboardButton(text="🍲 Ужин"), KeyboardButton(text="🍎 Перекус")],
            [KeyboardButton(text="◀️ Назад")]
        ], resize_keyboard=True)
    )

async def _save_and_reply(message, state, ai_data):
    try:
        data = await state.get_data()
        meal = data.get("meal", "dinner")
        is_yest = data.get("is_yesterday", False)
        date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M") if is_yest else datetime.now().strftime("%Y-%m-%d %H:%M")
        
        lid = save_log(
            message.from_user.id, meal, ai_data["food"], ai_data["grams"],
            ai_data["kcal"], ai_data["p"], ai_data["f"], ai_data["c"], date
        )
        await state.update_data(last_log_id=lid, last_food=ai_data["food"], last_grams=ai_data["grams"])
        await state.set_state(None)
        logger.info(f"Saved log {lid} for uid={message.from_user.id}: {ai_data['food']} {ai_data['kcal']}kcal")
        
        profile = get_profile(message.from_user.id)
        _, goal = calc_tdee(profile)
        today_stats = get_stats(get_today_logs(message.from_user.id))
        
        desc_raw = (ai_data.get("food_desc") or "").strip()
        desc = f"📝 {e(desc_raw)}\n\n" if desc_raw else ""
        grams_str = f" ({int(ai_data['grams'])}г)" if ai_data.get("grams", 0) > 0 else ""
        
        progress = ""
        if goal and not is_yest:
            rem = goal - today_stats["kcal"]
            if rem > 0:
                progress = f"\n\n📊 Сегодня: {today_stats['kcal']} / {goal} ккал (осталось {rem})"
            else:
                progress = f"\n\n⚠️ Сегодня: {today_stats['kcal']} ккал (+{abs(rem)} сверх нормы)"
        
        s = get_streak(message.from_user.id)
        sk = f"\n{streak_msg(s)}" if s in (3, 7, 14, 30) else ""
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Исправить название", callback_data="fix_name"),
             InlineKeyboardButton(text="⚖️ Исправить вес", callback_data="fix_grams")]
        ])
        
        text = (
            f"{desc}✅ <b>{e(ai_data['food'])}</b>{e(grams_str)}\n"
            f"🔥 {ai_data['kcal']} ккал | Б:{ai_data['p']:.1f} Ж:{ai_data['f']:.1f} У:{ai_data['c']:.1f}"
            f"{progress}{sk}"
        )
        await safe_send(message, text, reply_markup=kb)
        
    except Exception as ex:
        logger.error(f"_save_and_reply CRASH: {ex}", exc_info=True)
        await state.clear()
        try:
            kcal = ai_data.get("kcal", 0)
            await message.answer(f"✅ Записано!\n🔥 {kcal} ккал", reply_markup=main_kb, parse_mode=None)
        except Exception as ex2:
            logger.error(f"Even fallback failed: {ex2}")

@dp.message(WaitFood.input)
async def process_food(message: types.Message, state: FSMContext):
    if message.text and message.text in ALL_MENU:
        await state.clear()
        if message.text in MEAL_BTNS:
            await state.update_data(meal=MEALS[message.text], is_yesterday=False)
            await message.answer(f"✅ {e(message.text)}. Напиши что ел:", reply_markup=back_kb)
            await state.set_state(WaitFood.input)
        else:
            await message.answer("👆 Главное меню:", reply_markup=main_kb)
        return
    
    data = await state.get_data()
    if not data.get("meal"):
        await state.clear()
        return await message.answer("👆 Сначала выбери приём пищи из меню.", reply_markup=main_kb)
    
    uid = message.from_user.id
    
    # Голосовое
    if message.voice:
        wait_msg = await message.answer("🎤 Распознаю голос...")
        ogg_bytes = None
        try:
            f_ = await bot.get_file(message.voice.file_id)
            bio = await bot.download_file(f_.file_path)
            ogg_bytes = bio.read()
        except Exception as ex:
            logger.error(f"voice download: {ex}")
        try:
            await wait_msg.delete()
        except:
            pass
        if not ogg_bytes:
            return await message.answer("⚠️ Не удалось загрузить голосовое. Напиши текстом.", reply_markup=main_kb)
        text_from_voice = await ai_voice_to_text(ogg_bytes)
        if not text_from_voice:
            return await message.answer("⚠️ Не удалось распознать голос. Напиши текстом.", reply_markup=main_kb)
        await message.answer(f"🎤 Распознано: <i>{e(text_from_voice)}</i>\n\n⏳ Анализирую...")
        ai = await ai_nutrition(text=text_from_voice)
        if ai.get("error"):
            await state.clear()
            return await message.answer("⚠️ Не удалось распознать еду из голоса.", reply_markup=main_kb)
        await _save_and_reply(message, state, ai)
        return
    
    # Фото
    if message.photo:
        wait_msg = await message.answer("⏳ Анализирую фото...")
        photo_bytes = None
        try:
            f_ = await bot.get_file(message.photo[-1].file_id)
            bio = await bot.download_file(f_.file_path)
            photo_bytes = bio.read()
            logger.info(f"Photo downloaded: {len(photo_bytes)} bytes")
        except Exception as ex:
            logger.error(f"photo download: {ex}")
            try:
                await wait_msg.delete()
            except:
                pass
            await state.clear()
            return await message.answer("⚠️ Не удалось загрузить фото. Напиши текстом.", reply_markup=main_kb)
        
        ai = await ai_nutrition(photo_bytes=photo_bytes)
        logger.info(f"Photo AI result: {ai}")
        try:
            await wait_msg.delete()
        except:
            pass
        
        if ai.get("error") == "not_food":
            await message.answer("🔍 Не вижу еду на фото. Если это штрихкод — напиши его цифры:")
            await state.update_data(waiting_barcode=True)
            return
        if ai.get("error") == "timeout":
            await state.clear()
            return await message.answer("⏱ Yandex API долго отвечает. Попробуй написать текстом.", reply_markup=main_kb)
        if ai.get("error"):
            await state.clear()
            return await message.answer("⚠️ Не удалось распознать фото. Попробуй написать текстом.", reply_markup=main_kb)
        await _save_and_reply(message, state, ai)
        return
    
    # Текст / штрихкод
    text = message.text.strip()
    if re.match(r'^\d{8,13}$', text) or data.get("waiting_barcode"):
        barcode = re.sub(r'\D', '', text)
        if len(barcode) >= 8:
            wait_msg = await message.answer(f"🔍 Ищу по штрихкоду {barcode}...")
            product = await lookup_barcode(barcode)
            try:
                await wait_msg.delete()
            except:
                pass
            if product:
                await state.update_data(barcode_product=product, waiting_barcode=False)
                await message.answer(
                    f"✅ Найден: <b>{e(product['name'])}</b>\n"
                    f"Калорийность: {product['kcal_100g']:.0f} ккал/100г\n\n"
                    f"Введи вес порции (г):",
                    reply_markup=back_kb
                )
                await state.set_state(BarcodeWeight.input)
                return
            else:
                await state.update_data(waiting_barcode=False)
                await message.answer("❌ Продукт не найден. Напиши название вручную:")
                return
    
    wait_msg = await message.answer("⏳ Анализирую...")
    ai = await ai_nutrition(text=text)
    try:
        await wait_msg.delete()
    except:
        pass
    
    if ai.get("error") == "not_food":
        await state.clear()
        return await message.answer("🚫 Не похоже на еду. Выбери приём пищи из меню.", reply_markup=main_kb)
    if ai.get("error") == "timeout":
        await state.clear()
        return await message.answer("⏱ Yandex API долго отвечает. Попробуй чуть позже.", reply_markup=main_kb)
    if ai.get("error"):
        await state.clear()
        return await message.answer(
            "⚠️ Не удалось распознать. Попробуй написать подробнее,\n"
            "например: <i>скумбрия копчёная 150г, хлеб 3 куска</i>",
            reply_markup=main_kb
        )
    await _save_and_reply(message, state, ai)

@dp.message(BarcodeWeight.input)
async def barcode_weight_input(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    product = data.get("barcode_product")
    if not product:
        await state.clear()
        return await message.answer("❌ Ошибка. Попробуй снова.", reply_markup=main_kb)
    try:
        grams = float(message.text.strip().replace("г", "").replace(",", ".").strip())
        if grams <= 0 or grams > 3000:
            raise ValueError
    except:
        return await message.answer("❌ Введи вес в граммах, например: 150")
    k = grams / 100
    ai_data = {
        "food": product["name"],
        "food_desc": f"{product['name']}, {grams:.0f}г",
        "grams": grams,
        "kcal": int(product["kcal_100g"] * k),
        "p": round(product["p_100g"] * k, 1),
        "f": round(product["f_100g"] * k, 1),
        "c": round(product["c_100g"] * k, 1),
    }
    await _save_and_reply(message, state, ai_data)

# ─── ИСПРАВЛЕНИЕ ────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "fix_name")
async def fix_name_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if not data.get("last_log_id"):
        return await cb.message.answer("❌ Запись не найдена.", reply_markup=main_kb)
    await state.set_state(CorrectFS.name)
    await cb.message.answer(
        f"✏️ Текущее: {e(data.get('last_food', '?'))}\n\nНапиши правильное название:",
        reply_markup=back_kb
    )

@dp.callback_query(F.data == "fix_grams")
async def fix_grams_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if not data.get("last_log_id"):
        return await cb.message.answer("❌ Запись не найдена.", reply_markup=main_kb)
    await state.set_state(CorrectFS.grams)
    await cb.message.answer(
        f"⚖️ Текущий вес: {int(data.get('last_grams', 0))}г\n\nНапиши правильный вес:",
        reply_markup=back_kb
    )

@dp.message(CorrectFS.name)
async def apply_fix_name(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    lid = data.get("last_log_id")
    grams = data.get("last_grams", 250)
    wait = await message.answer("⏳ Пересчитываю...")
    res = await ai_recalc(message.text.strip(), grams)
    try:
        await wait.delete()
    except:
        pass
    if res.get("error"):
        await state.set_state(None)
        return await message.answer("⚠️ Не удалось пересчитать.", reply_markup=main_kb)
    update_log(lid, message.text.strip(), grams, res["kcal"], res["p"], res["f"], res["c"])
    await state.update_data(last_food=message.text.strip())
    await state.set_state(None)
    await message.answer(
        f"✅ {e(message.text.strip())} ({int(grams)}г)\n"
        f"🔥 {res['kcal']} ккал | Б:{res['p']:.1f} Ж:{res['f']:.1f} У:{res['c']:.1f}",
        reply_markup=main_kb
    )

@dp.message(CorrectFS.grams)
async def apply_fix_grams(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    lid = data.get("last_log_id")
    fname = data.get("last_food", "Блюдо")
    try:
        ng = float(message.text.strip().replace(",", ".").replace("г", "").strip())
        if ng <= 0 or ng > 5000:
            raise ValueError
    except:
        return await message.answer("❌ Введи число от 1 до 5000")
    wait = await message.answer("⏳ Пересчитываю...")
    res = await ai_recalc(fname, ng)
    try:
        await wait.delete()
    except:
        pass
    if res.get("error"):
        await state.set_state(None)
        return await message.answer("⚠️ Не удалось пересчитать.", reply_markup=main_kb)
    update_log(lid, fname, ng, res["kcal"], res["p"], res["f"], res["c"])
    await state.update_data(last_grams=ng)
    await state.set_state(None)
    await message.answer(
        f"✅ {e(fname)} ({int(ng)}г)\n"
        f"🔥 {res['kcal']} ккал | Б:{res['p']:.1f} Ж:{res['f']:.1f} У:{res['c']:.1f}",
        reply_markup=main_kb
    )

# ─── СИМПТОМЫ ───────────────────────────────────────────────────────────────
@dp.message(F.text == "🤒 Симптом")
async def symptom_menu(message: types.Message):
    await message.answer("🤒 Выбери симптом:", reply_markup=symptom_kb)

@dp.callback_query(F.data == "sym_skip")
async def sym_skip(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    s = data.get("symptom_type", "")
    save_symptom(cb.from_user.id, s, "")
    await state.clear()
    await cb.message.answer(f"✅ Симптом «{e(s)}» записан.", reply_markup=main_kb)

@dp.callback_query(F.data.startswith("sym_"))
async def sym_selected(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    s = cb.data[4:]
    if s == "other":
        await state.set_state(SymNote.input)
        await state.update_data(symptom_type="custom")
        await cb.message.answer("✏️ Опиши симптом:", reply_markup=back_kb)
    else:
        await state.set_state(SymNote.input)
        await state.update_data(symptom_type=s)
        await cb.message.answer(
            f"📝 {e(s)} — добавь заметку или пропусти:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="sym_skip")]])
        )

@dp.message(SymNote.input)
async def sym_note(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    st = data.get("symptom_type", "custom")
    if st == "custom":
        save_symptom(message.from_user.id, message.text.strip(), "")
        await state.clear()
        await message.answer(f"✅ Симптом «{e(message.text.strip())}» записан.", reply_markup=main_kb)
    else:
        save_symptom(message.from_user.id, st, message.text.strip())
        await state.clear()
        await message.answer(f"✅ Симптом «{e(st)}» с заметкой записан.", reply_markup=main_kb)

# ─── ТРЕНИРОВКИ ─────────────────────────────────────────────────────────────
@dp.message(F.text == "💪 Тренировка")
async def workout_menu(message: types.Message):
    await message.answer("💪 Выбери тип тренировки:", reply_markup=workout_kb)

@dp.callback_query(F.data == "wrk_skip")
async def wrk_skip(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    w = data.get("workout_type", "")
    save_workout(cb.from_user.id, w, "")
    await state.clear()
    await cb.message.answer(f"✅ Тренировка «{e(w)}» записана.", reply_markup=main_kb)

@dp.callback_query(F.data.startswith("wrk_"))
async def wrk_selected(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    w = cb.data[4:]
    if w == "other":
        await state.set_state(WrkNote.input)
        await state.update_data(workout_type="custom")
        await cb.message.answer("✏️ Опиши тренировку:", reply_markup=back_kb)
    else:
        await state.set_state(WrkNote.input)
        await state.update_data(workout_type=w)
        await cb.message.answer(
            f"📝 {e(w)} — добавь заметку или пропусти:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="wrk_skip")]])
        )

@dp.message(WrkNote.input)
async def wrk_note(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    wt = data.get("workout_type", "custom")
    if wt == "custom":
        save_workout(message.from_user.id, message.text.strip(), "")
        await state.clear()
        await message.answer(f"✅ Тренировка «{e(message.text.strip())}» записана.", reply_markup=main_kb)
    else:
        save_workout(message.from_user.id, wt, message.text.strip())
        await state.clear()
        await message.answer(f"✅ Тренировка «{e(wt)}» с заметкой записана.", reply_markup=main_kb)

# ─── ПРОФИЛЬ ────────────────────────────────────────────────────────────────
@dp.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message):
    p = get_profile(message.from_user.id)
    if not p or not p[1]:
        return await message.answer(
            "👤 Профиль пуст.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Заполнить", callback_data="setup_profile")]])
        )
    maint, goal = calc_tdee(p)
    g_str = "♂️ Мужской" if p[5] == "male" else "♀️ Женский"
    text = (
        f"👤 Твой профиль\n"
        f"⚖️ Вес: {p[1]} кг → цель: {p[2]} кг\n"
        f"📏 Рост: {p[3]} см\n"
        f"🎂 Возраст: {p[4]} лет\n"
        f"👤 Пол: {g_str}\n"
    )
    if goal:
        pn, fn, cn = calc_macros(goal)
        diff_s = ""
        if p[2] and p[1]:
            if p[2] < p[1]:
                diff_s = f" (дефицит {maint - goal} ккал)"
            elif p[2] > p[1]:
                diff_s = f" (профицит {goal - maint} ккал)"
        text += f"\n🔥 Норма: ~{goal} ккал/день{diff_s}\n📊 БЖУ: Б~{pn}г / Ж~{fn}г / У~{cn}г"
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Изменить", callback_data="edit_profile_menu")]]))

@dp.callback_query(F.data.in_(["setup_profile", "edit_profile_menu"]))
async def profile_menu(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.data == "setup_profile":
        await state.update_data(step=1)
        await state.set_state(ProfileFS.input)
        await cb.message.edit_text("⚖️ Настройка профиля\n\n1/5. Текущий вес (кг):")
    else:
        await cb.message.edit_text(
            "✏️ Что изменить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Вес", callback_data="edit_weight"),
                 InlineKeyboardButton(text="🎯 Цель(кг)", callback_data="edit_target")],
                [InlineKeyboardButton(text="📏 Рост", callback_data="edit_height"),
                 InlineKeyboardButton(text="🎂 Возраст", callback_data="edit_age")]
            ])
        )

@dp.callback_query(F.data.startswith("edit_"))
async def start_edit(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    field = cb.data.split("_")[1]
    await state.update_data(edit_field=field, step=10)
    await state.set_state(ProfileFS.input)
    prompts = {
        "weight": "⚖️ Новый вес (кг):",
        "target": "🎯 Новая цель (кг):",
        "height": "📏 Новый рост (см):",
        "age": "🎂 Новый возраст (лет):"
    }
    await cb.message.edit_text(prompts.get(field, "Введи значение:"))

@dp.message(ProfileFS.input)
async def profile_input(message: types.Message, state: FSMContext):
    if message.text in ALL_MENU:
        await state.clear()
        return await message.answer("👆 Главное меню:", reply_markup=main_kb)
    data = await state.get_data()
    step = data.get("step", 1)
    is_edit = step >= 10
    field = data.get("edit_field")
    try:
        val = float(message.text.replace(",", ".")) if not field or field in ["weight", "target"] else int(message.text)
    except:
        return await message.answer("❌ Введи число.")
    
    if is_edit and field:
        upd_profile_field(message.from_user.id, field, val)
        if field == "weight":
            log_weight(message.from_user.id, val)
        await state.clear()
        return await message.answer("✅ Обновлено!", reply_markup=main_kb)
    
    if step == 1:
        await state.update_data(weight=val, step=2)
        await message.answer(f"✅ {val} кг.\n\n2/5. Цель (кг):")
    elif step == 2:
        await state.update_data(target=val, step=3)
        await message.answer(f"✅ Цель: {val} кг.\n\n3/5. Рост (см):")
    elif step == 3:
        await state.update_data(height=int(val), step=4)
        await message.answer(f"✅ {int(val)} см.\n\n4/5. Возраст (лет):")
    elif step == 4:
        await state.update_data(age=int(val), step=5)
        await message.answer(
            f"✅ {int(val)} лет.\n\n5/5. Пол:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="♂️ Мужской", callback_data="gender_male"),
                 InlineKeyboardButton(text="♀️ Женский", callback_data="gender_female")]
            ])
        )

@dp.callback_query(F.data.in_(["gender_male", "gender_female"]))
async def profile_gender(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    g = "male" if cb.data == "gender_male" else "female"
    save_profile(cb.from_user.id, data.get("weight"), data.get("target"), data.get("height"), data.get("age"), g)
    if data.get("weight"):
        log_weight(cb.from_user.id, data["weight"])
    await state.clear()
    profile = get_profile(cb.from_user.id)
    maint, goal = calc_tdee(profile)
    msg = "✅ Профиль сохранён!\n"
    if goal:
        pn, fn, cn = calc_macros(goal)
        w = data.get("weight", 0)
        t = data.get("target", 0)
        if t and w and t < w:
            msg += f"\n🎯 Цель: похудеть {w}→{t} кг\n🔥 Норма: {goal} ккал/день (дефицит {maint - goal} ккал)\n"
        elif t and w and t > w:
            msg += f"\n🎯 Цель: набрать до {t} кг\n🔥 Норма: {goal} ккал/день (профицит {goal - maint} ккал)\n"
        else:
            msg += f"\n🔥 Норма: {goal} ккал/день\n"
        msg += f"📊 БЖУ: Б~{pn}г / Ж~{fn}г / У~{cn}г"
    await cb.message.edit_text(msg)
    await bot.send_message(cb.from_user.id, "👇 Главное меню:", reply_markup=main_kb)

# ─── АНАЛИТИКА ──────────────────────────────────────────────────────────────
@dp.message(F.text == "📊 Аналитика")
async def show_analytics(message: types.Message):
    is_pro = get_sub(message.from_user.id)["active"]
    btns = [
        [
            InlineKeyboardButton(text="Сегодня", callback_data="rep_1"),
            InlineKeyboardButton(text="3 дня", callback_data="rep_3"),
            InlineKeyboardButton(text="7 дней" if is_pro else "🔒 7 дней", callback_data="rep_7" if is_pro else "paywall")
        ],
        [
            InlineKeyboardButton(text="📅 Любой срок" if is_pro else "🔒 Любой срок", callback_data="rep_all" if is_pro else "paywall")
        ]
    ]
    await message.answer("📊 Выбери период:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("rep_"))
async def gen_report(cb: types.CallbackQuery):
    await cb.answer()
    period = cb.data.split("_")[1]
    days = None if period == "all" else int(period)
    rows = get_logs(cb.from_user.id, days)
    if not rows:
        return await cb.message.edit_text("Нет записей за этот период.")
    
    dates = [datetime.strptime(r[9].split()[0], "%Y-%m-%d") for r in rows]
    actual_days = max(1, (max(dates) - min(dates)).days + 1)
    stats = get_stats(rows)
    profile = get_profile(cb.from_user.id)
    is_pro = get_sub(cb.from_user.id)["active"]
    syms = get_symptoms(cb.from_user.id, days)
    wrks = get_workouts(cb.from_user.id, days)
    
    await cb.message.edit_text(f"⏳ Анализирую данные за {actual_days} дн...")
    ai_text = await ai_analysis(stats, actual_days, syms, wrks, profile, is_pro)
    
    maint, goal = calc_tdee(profile)
    avg_kcal = stats['kcal'] // actual_days if actual_days > 0 else stats['kcal']
    norm_line = f" / норма ~{goal}" if goal else ""
    
    header = (
        f"📊 <b>Отчёт за {actual_days} дн</b>\n"
        f"🔥 Среднее: {avg_kcal}{norm_line} ккал/день\n"
        f"📊 Б:{stats['p']/actual_days:.0f}г / Ж:{stats['f']/actual_days:.0f}г / У:{stats['c']/actual_days:.0f}г (в день)\n"
    )
    
    meal_breakdown = ""
    if is_pro:
        mn = {"breakfast": "🍳 Завтрак", "lunch": "🥗 Обед", "dinner": "🍲 Ужин", "snack": "🍎 Перекус"}
        bm = {}
        for r in rows:
            bm[r[2]] = bm.get(r[2], 0) + (r[5] or 0)
        if bm:
            meal_breakdown = "\n<b>Калории по приёмам (итого):</b>\n"
            for m in ["breakfast", "lunch", "dinner", "snack"]:
                if m in bm:
                    meal_breakdown += f"{mn.get(m, '•')}: {bm[m]} ккал\n"
    
    top_foods = ""
    if is_pro and rows:
        food_kcal = {}
        for r in rows:
            name = r[3]
            kcal = r[5] or 0
            food_kcal[name] = food_kcal.get(name, 0) + kcal
        top = sorted(food_kcal.items(), key=lambda x: -x[1])[:5]
        if top:
            top_foods = "\n<b>Топ-5 продуктов по калориям:</b>\n"
            for name, kcal in top:
                top_foods += f"• {e(name)}: {kcal} ккал\n"
    
    ext = ""
    if syms:
        ext += f"\n🤒 Симптомов за период: {len(syms)}"
    if wrks:
        ext += f"\n💪 Тренировок за период: {len(wrks)}"
    
    full_text = f"{header}{meal_breakdown}{top_foods}{ext}\n\n💡 {e(ai_text)}"
    
    if len(full_text) > 4000:
        full_text = full_text[:3990] + "..."
    
    await cb.message.edit_text(full_text)

@dp.callback_query(F.data == "paywall")
async def paywall(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "🔒 7+ дней аналитики — только PRO\n\n"
        "Также в PRO:\n"
        "• Разбивка по приёмам\n"
        "• Анализ симптомов\n"
        "• Экспорт CSV\n"
        "• Ежедневная сводка\n"
        "• Еженедельный AI-коуч",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Купить PRO", callback_data="open_pro")]])
    )

# ─── ЭКСПОРТ ────────────────────────────────────────────────────────────────
@dp.message(F.text == "📥 Экспорт")
async def export_data(message: types.Message):
    if not get_sub(message.from_user.id)["active"]:
        return await message.answer(
            "🔒 Экспорт CSV — только PRO.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Купить PRO", callback_data="open_pro")]])
        )
    rows = get_all_logs_csv(message.from_user.id)
    if not rows:
        return await message.answer("📭 Нет данных.")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Дата", "Приём", "Блюдо", "Вес(г)", "Ккал", "Б", "Ж", "У"])
    for r in rows:
        w.writerow([r[9], r[2], r[3], r[4], r[5], r[6], r[7], r[8]])
    out.seek(0)
    f = types.BufferedInputFile(out.getvalue().encode(), filename=f"nutrimind_{datetime.now().strftime('%Y%m%d')}.csv")
    await message.answer_document(f, caption=f"📥 Экспорт завершён! Записей: {len(rows)}")

# ─── РЕФЕРАЛЬНАЯ ────────────────────────────────────────────────────────────
@dp.message(F.text == "🔗 Реферальная ссылка")
async def ref_link(message: types.Message):
    code = get_ref_code(message.from_user.id)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{code}"
    await message.answer(
        f"🔗 Реферальная программа\n\n"
        f"Приглашай друзей — оба получите 7 дней PRO!\n\n"
        f"Твоя ссылка:\n`{link}`"
    )

# ─── УДАЛЕНИЕ ───────────────────────────────────────────────────────────────
@dp.message(F.text == "🗑 Удалить мои данные")
async def delete_start(message: types.Message, state: FSMContext):
    await state.set_state(DelConfirm.waiting)
    await message.answer(
        "⚠️ Удаление всех данных\n\n"
        "Питание, профиль, симптомы, вода, вес, PRO.\n"
        "Необратимо!\n\n"
        "Напиши УДАЛИТЬ:",
        reply_markup=back_kb
    )

@dp.message(DelConfirm.waiting)
async def delete_confirm(message: types.Message, state: FSMContext):
    if message.text.strip().upper() == "УДАЛИТЬ":
        delete_user_data(message.from_user.id)
        await state.clear()
        await message.answer(
            "✅ Все данные удалены. Напиши /start чтобы начать заново.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="/start")]], resize_keyboard=True)
        )
    else:
        await state.clear()
        await message.answer("↩️ Отменено.", reply_markup=main_kb)

# ─── PRO ────────────────────────────────────────────────────────────────────
@dp.message(F.text == "💎 Купить PRO")
async def buy_pro(message: types.Message):
    await message.answer("💎 Переход на PRO", reply_markup=pricing_kb())

@dp.message(F.text == "📅 Статус подписки")
async def sub_status(message: types.Message):
    s = get_sub(message.from_user.id)
    if s["active"]:
        await message.answer(f"👑 PRO активен\n📅 До: {s['until']}\n⏳ Осталось: {s['days_left']} дн.")
    else:
        await message.answer(
            "🆓 Бесплатный тариф.\n\n"
            "В PRO: 7+ дней, корреляции, экспорт, сводка, AI-коуч.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Купить PRO", callback_data="open_pro")]])
        )

@dp.callback_query(F.data == "open_pro")
async def open_pro(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer("💎 Переход на PRO", reply_markup=pricing_kb())

@dp.callback_query(F.data == "pro_info")
async def pro_info(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "✨ В PRO входит:\n"
        "• 📊 Аналитика за 7+ дней\n"
        "• 🍽 Разбивка по приёмам\n"
        "• 🎯 Норма с учётом цели\n"
        "• 🤒 Связь симптомов с питанием\n"
        "• 🌙 Ежедневная сводка в 22:00\n"
        "• 🤖 Еженедельный AI-коуч\n"
        "• 📥 Экспорт CSV"
    )

# ─── TELEGRAM STARS ОПЛАТА ──────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("buy_"))
async def buy_tariff_stars(cb: types.CallbackQuery):
    await cb.answer()
    tariff = cb.data.split("_")[1]
    
    if tariff not in PRICES:
        return
    
    plan = PRICES[tariff]
    labels = {"1m": "1 месяц", "3m": "3 месяца", "1y": "1 год"}
    
    # Отправляем инвойс в звёздах
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title="NutriMind PRO подписка",
        description=f"Доступ к полной аналитике, экспорту и AI-коучу на {labels[tariff]}",
        payload=f"pro_{tariff}_{cb.from_user.id}",
        provider_token="",  # Для XTR (звёзд) токен пустой!
        currency="XTR",     # Валюта: Telegram Stars
        prices=[LabeledPrice(label=labels[tariff], amount=plan["stars"])],
        start_parameter=f"pay_{tariff}",
    )
    await cb.message.delete()

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    """Подтверждаем предзаказ (обязательно для Stars)"""
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    """Обработка успешной оплаты"""
    payload = message.successful_payment.invoice_payload  # e.g. "pro_1m_123456789"
    try:
        _, tariff, uid_str = payload.split("_")
        uid = int(uid_str)
    except:
        await message.answer("❌ Ошибка распознавания платежа. Обратитесь в поддержку.")
        return

    # Выдаём PRO
    days = {"1m": 30, "3m": 90, "1y": 365}.get(tariff, 30)
    until = activate_pro(uid, days)
    
    # Если это первый платёж — считаем его «основателем»
    try:
        c = db()
        r = c.execute("SELECT trial_used FROM users WHERE user_id=?", (uid,)).fetchone()
        c.close()
        if r and not r[0]:
            increment_founder()
    except:
        pass
    
    # Реферальный бонус
    try:
        c = db()
        r = c.execute("SELECT referred_by FROM users WHERE user_id=?", (uid,)).fetchone()
        c.close()
        if r and r[0]:
            activate_pro(r[0], 7)
    except:
        pass

    # Сообщаем пользователю
    await message.answer(
        f"🎉 <b>Оплата прошла!</b>\n\n"
        f"PRO подписка активирована до {until}.\n"
        f"Спасибо за поддержку проекта! 🙏",
        parse_mode="HTML"
    )
    logger.info(f"🤑 PAYMENT SUCCESS: User {uid} paid for {tariff} with Stars")

# ─── НЕИЗВЕСТНЫЕ СООБЩЕНИЯ ──────────────────────────────────────────────────
@dp.message()
async def unknown(message: types.Message, state: FSMContext):
    cur = await state.get_state()
    if cur:
        return
    await message.answer(
        "Используй кнопки меню 👇\nЕсли меню пропало — напиши /start",
        reply_markup=main_kb
    )

# ─── ФОНОВЫЕ ЗАДАЧИ ─────────────────────────────────────────────────────────
async def job_check_subs():
    try:
        conn = db()
        rows = conn.execute(
            "SELECT user_id,pro_until,warned_3d,warned_1d FROM users WHERE is_pro=1 AND pro_until IS NOT NULL"
        ).fetchall()
        conn.close()
        now = datetime.now()
        for uid_, until, w3, w1 in rows:
            try:
                end = datetime.strptime(until, "%Y-%m-%d %H:%M")
                diff = (end - now).days
                if diff == 3 and not w3:
                    await bot.send_message(uid_, "⏳ PRO истекает через 3 дня. Продли в меню «Купить PRO».")
                    cc = db()
                    cc.execute("UPDATE users SET warned_3d=1 WHERE user_id=?", (uid_,))
                    cc.commit()
                    cc.close()
                elif diff == 1 and not w1:
                    await bot.send_message(uid_, "🚨 PRO истекает завтра! Продли сейчас.")
                    cc = db()
                    cc.execute("UPDATE users SET warned_1d=1 WHERE user_id=?", (uid_,))
                    cc.commit()
                    cc.close()
                elif diff < 0:
                    deactivate_pro(uid_)
                    try:
                        await bot.send_message(uid_, "😔 PRO истёк. Записи сохранены.\n💎 Продли в меню «Купить PRO».")
                    except:
                        pass
            except Exception as ex:
                logger.error(f"sub_exp {uid_}: {ex}")
    except Exception as ex:
        logger.error(f"job_check_subs: {ex}")

async def job_daily_summary():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = db()
        uids = conn.execute(
            "SELECT DISTINCT user_id FROM food_logs WHERE recorded_at >=?",
            (today,)
        ).fetchall()
        conn.close()
        for (uid_,) in uids:
            try:
                rows = get_today_logs(uid_)
                if not rows:
                    continue
                stats = get_stats(rows)
                p = get_profile(uid_)
                maint, goal = calc_tdee(p)
                s = get_streak(uid_)
                msg = f"🌙 Итог дня\n\n🔥 Калорий: {stats['kcal']} ккал"
                if goal:
                    diff = stats['kcal'] - goal
                    if diff > 100:
                        msg += f" (⚠️ +{diff} сверх нормы)"
                    elif diff < -100:
                        msg += f" (✅ дефицит {abs(diff)} ккал)"
                    else:
                        msg += " (🎯 в норме)"
                msg += f"\n📊 Б:{stats['p']:.0f}г / Ж:{stats['f']:.0f}г / У:{stats['c']:.0f}г"
                w = get_today_water(uid_)
                if w > 0:
                    msg += f"\n💧 Воды: {w} мл"
                    if w < WATER_GOAL:
                        msg += f" (осталось {WATER_GOAL - w} мл)"
                msg += f"\n🍽 Приёмов: {len(set(r[2] for r in rows))}"
                if s >= 3:
                    msg += f"\n{streak_msg(s)}"
                if not get_sub(uid_)["active"]:
                    msg += f"\n\n💎 PRO от {PRICES['1m']['new']}/мес — расширенная аналитика"
                msg += f"\n\n📣 {CHANNEL}"
                await bot.send_message(uid_, msg)
            except Exception as ex:
                logger.error(f"daily {uid_}: {ex}")
    except Exception as ex:
        logger.error(f"job_daily_summary: {ex}")

async def job_weekly_coach():
    try:
        conn = db()
        uids = conn.execute("SELECT user_id FROM users WHERE is_pro=1").fetchall()
        conn.close()
        for (uid_,) in uids:
            try:
                coach_text = await ai_weekly_coach(uid_)
                if coach_text:
                    await bot.send_message(
                        uid_,
                        f"🤖 Еженедельный отчёт\n\n{e(coach_text)}\n\n📊 Детальная аналитика: /start → 📊 Аналитика"
                    )
            except Exception as ex:
                logger.error(f"weekly_coach {uid_}: {ex}")
    except Exception as ex:
        logger.error(f"job_weekly_coach: {ex}")

async def job_water_reminder():
    try:
        conn = db()
        uids = conn.execute("SELECT user_id FROM users").fetchall()
        conn.close()
        for (uid_,) in uids:
            try:
                w = get_today_water(uid_)
                if w < WATER_GOAL // 2:
                    await bot.send_message(uid_, f"💧 Не забывай пить воду! Сегодня: {w} / {WATER_GOAL} мл.")
            except:
                pass
    except Exception as ex:
        logger.error(f"job_water: {ex}")

# ─── MAIN ───────────────────────────────────────────────────────────────────
async def main():
    init_db()
    if YANDEX_KEY:
        test = await yandex_post("Скажи 'ok'", timeout=10)
        if test:
            logger.info("✅ Yandex API: OK")
        else:
            logger.warning("⚠️ Yandex API не отвечает при старте!")
    else:
        logger.error("❌ YANDEX_API_KEY не задан!")
    
    scheduler.add_job(job_check_subs, 'cron', hour=9, minute=0)
    scheduler.add_job(job_daily_summary, 'cron', hour=22, minute=0)
    scheduler.add_job(job_weekly_coach, 'cron', day_of_week='sun', hour=20, minute=0)
    scheduler.add_job(job_water_reminder, 'cron', hour=12, minute=0)
    scheduler.add_job(job_water_reminder, 'cron', hour=17, minute=0)
    scheduler.start()
    logger.info("🚀 NutriMind запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
>>>>>>> a7634565dcdb67a62d8326c01dddd7a3e4127f46
        logger.info("🛑")