"""История — единый backend: FastAPI + aiogram 3 + Supabase."""

import os
import json
import hmac
import hashlib
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from io import BytesIO
from urllib.parse import parse_qsl
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, HTTPException, Header, Depends, Query
from fastapi.responses import FileResponse
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from supabase import create_client, Client

# ── Конфигурация ─────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5208615220"))
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
MINI_APP_URL = os.environ["MINI_APP_URL"]
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "history-media")

# ⚠️ ССЫЛКИ НА ТВОИ КАРТИНКИ С GITHUB ⚠️
START_IMG_URL = "https://raw.githubusercontent.com/morofhamoi-debug/HistoryBot/main/start.PNG"
STARTI_IMG_URL = "https://raw.githubusercontent.com/morofhamoi-debug/HistoryBot/main/starti.PNG"

# ── Инициализация ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("istoriya")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Московское время (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))

# ── Состояния (для ввода даты) ───────────────────────────────────────────────
class AdminState(StatesGroup):
    waiting_for_custom_date = State()

# ── Утилиты ──────────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def fmt_dt(v: Optional[datetime | str]) -> str:
    if not v:
        return "—"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    
    v = v.astimezone(MSK_TZ)
    return v.strftime("%d.%m.%Y %H:%M")

def fetch_one(query) -> Optional[dict]:
    try:
        res = query.execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"DB Fetch Error: {e}")
        return None

async def safe_answer(cq: CallbackQuery, text: str = None, show_alert: bool = False):
    try:
        await cq.answer(text, show_alert=show_alert)
    except Exception:
        pass

def validate_init_data(init_data: str) -> Optional[dict]:
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed:
            return None
        received_hash = parsed.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        return json.loads(parsed["user"]) if "user" in parsed else None
    except Exception:
        return None

async def get_verified_user(x_telegram_init_data: str = Header(None)) -> dict:
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="initData required")
    user = validate_init_data(x_telegram_init_data)
    if not user or not user.get("id"):
        raise HTTPException(status_code=401, detail="invalid initData")
    db_user = fetch_one(supabase.table("users").select("status, valid_until").eq("id", user["id"]))
    if not db_user or db_user.get("status") != "approved":
        raise HTTPException(status_code=403, detail="access denied")
        
    # Проверка срока действия
    v_until = db_user.get("valid_until")
    if v_until:
        try:
            dt = datetime.fromisoformat(v_until.replace("Z", "+00:00"))
            if dt < now_utc():
                supabase.table("users").update({"status": "denied"}).eq("id", user["id"]).execute()
                raise HTTPException(status_code=403, detail="term expired")
        except ValueError:
            pass
            
    return user

# ── База данных ──────────────────────────────────────────────────────────────

def ensure_user(user: types.User) -> dict:
    existing = fetch_one(supabase.table("users").select("*").eq("id", user.id))
    if existing:
        return existing
    status = "approved" if user.id == ADMIN_ID else "pending"
    new_user = {
        "id": user.id, "first_name": user.first_name,
        "last_name": user.last_name, "username": user.username,
        "status": status,
    }
    supabase.table("users").insert(new_user).execute()
    return new_user

def create_access_request(user_id: int) -> tuple[dict, bool]:
    existing = fetch_one(supabase.table("access_requests").select("*").eq("user_id", user_id).eq("status", "pending"))
    if existing:
        return existing, False
    res = supabase.table("access_requests").insert({"user_id": user_id, "status": "pending"}).execute()
    return res.data[0], True

def update_request_status(request_id: int, status: str, admin_id: int, valid_until: Optional[str] = None) -> Optional[dict]:
    req = fetch_one(supabase.table("access_requests").select("*").eq("id", request_id))
    if not req or req["status"] != "pending":
        return None
        
    if valid_until and status == "pending":
        status = "approved"

    supabase.table("access_requests").update({
        "status": status, "resolved_at": now_utc().isoformat(), "resolved_by": admin_id,
    }).eq("id", request_id).eq("status", "pending").execute()
    
    update_data = {"status": status, "updated_at": now_utc().isoformat()}
    if valid_until:
        update_data["valid_until"] = valid_until
        
    supabase.table("users").update(update_data).eq("id", req["user_id"]).execute()
    return fetch_one(supabase.table("access_requests").select("*").eq("id", request_id))

def get_owner_by_connection(connection_id: str) -> Optional[int]:
    res = fetch_one(supabase.table("business_connections").select("owner_id").eq("id", connection_id))
    return res["owner_id"] if res else None

# ДОБАВЛЕНА ФУНКЦИЯ ПРОВЕРКИ ДОСТУПА
def is_user_active(user_id: int) -> bool:
    u = fetch_one(supabase.table("users").select("status, valid_until").eq("id", user_id))
    if not u or u.get("status") != "approved":
        return False
    
    v_until = u.get("valid_until")
    if v_until:
        try:
            dt = datetime.fromisoformat(v_until.replace("Z", "+00:00"))
            if dt < now_utc():
                supabase.table("users").update({"status": "denied"}).eq("id", user_id).execute()
                return False
        except ValueError:
            pass
    return True

# ── ДОБАВЛЕНО: Фоновая задача для проверки сроков ──────────────────────────
async def check_expirations():
    while True:
        try:
            now_iso = now_utc().isoformat()
            # Ищем тех, у кого истек срок и еще не отправлено уведомление
            res = supabase.table("users").select("id").lt("valid_until", now_iso).or_("expiry_notified.eq.false,expiry_notified.is.null").execute()
            
            if res.data:
                for u in res.data:
                    uid = u["id"]
                    try:
                        await bot.send_message(uid, "⏳ <b>Срок действия вашего доступа истек.</b>\nДля продления обратитесь к администратору: @GaspardShop")
                        supabase.table("users").update({"expiry_notified": True, "status": "denied"}).eq("id", uid).execute()
                    except Exception as e:
                        logger.warning(f"Could not send expiry message to {uid}: {e}")
                        # Если заблокировали бота, все равно ставим флаг
                        supabase.table("users").update({"expiry_notified": True, "status": "denied"}).eq("id", uid).execute()
        except Exception as e:
            logger.error(f"Error in check_expirations: {e}")
            
        await asyncio.sleep(300) # Проверка каждые 5 минут
# ────────────────────────────────────────────────────────────────────────────

def upsert_chat(connection_id: str, chat: types.Chat, owner_id: int):
    supabase.table("chats").upsert({
        "connection_id": connection_id, "chat_id": chat.id, "chat_type": str(chat.type),
        "title": chat.title, "username": chat.username, "first_name": chat.first_name,
        "last_name": chat.last_name, "owner_id": owner_id,
    }, on_conflict="connection_id,chat_id").execute()

def upsert_contact(owner_id: int, chat: types.Chat):
    if str(chat.type) != "private":
        return
    supabase.table("contacts").upsert({
        "owner_id": owner_id, "telegram_user_id": chat.id, "first_name": chat.first_name,
        "last_name": chat.last_name, "username": chat.username,
    }, on_conflict="owner_id,telegram_user_id").execute()

# ── Aiogram: Пользователи ────────────────────────────────────────────────────

async def get_instruction_msg():
    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username
    except Exception:
        bot_username = "KhamoiHistory_Bot"

    # ИЗМЕНЕНО: Удалены строки, которые ты обвел белым на скриншоте
    text = (
        "✅ <b>Доступ открыт. Добро пожаловать в «Глазок»!</b>\n\n"
        "<b>Бот не подключён</b>\n"
        "После подключения покажет удаленные и отредактированные сообщения.\n"
        "Для сохранения сгорающих или однократных — ответьте на них любым сообщением."
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Скопировать @", copy_text=types.CopyTextButton(text=f"@{bot_username}")),
            InlineKeyboardButton(text="🔌 Подключить Глазок", url="tg://settings/edit")
        ],
        [InlineKeyboardButton(text="? Инструкция", callback_data="show_instructions_info")]
    ])
    return text, kb

# ── Aiogram: Админ-панель ────────────────────────────────────────────────────

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")]
    ])
    await message.answer("🛠 <b>Админ-панель</b>\nВыберите действие:", reply_markup=kb)

@dp.callback_query(F.data == "admin_users")
async def cb_admin_users(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    
    res = supabase.table("users").select("*").neq("id", ADMIN_ID).order("created_at", desc=True).limit(30).execute()
    users = res.data if res.data else []
    
    if not users:
        await cq.message.edit_text("В базе пока нет других пользователей.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]]))
        return
        
    kb_buttons = []
    for u in users:
        name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
        if not name: name = str(u['id'])
        status_emoji = "🟢" if u['status'] == 'approved' else ("⏳" if u['status'] == 'pending' else "🔴")
        kb_buttons.append([InlineKeyboardButton(text=f"{status_emoji} {name} (ID: {u['id']})", callback_data=f"admin_user:{u['id']}")])
        
    kb_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    text = "👥 <b>Список пользователей</b>\nВыберите пользователя:"
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cq.message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "admin_back")
async def cb_admin_back(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")]
    ])
    await cq.message.edit_text("🛠 <b>Админ-панель</b>\nВыберите действие:", reply_markup=kb)

@dp.callback_query(F.data.startswith("admin_user:"))
async def cb_admin_user_card(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    
    uid = int(cq.data.split(":")[1])
    u = fetch_one(supabase.table("users").select("*").eq("id", uid))
    if not u:
        await cq.message.edit_text("Пользователь не найден.")
        return
        
    name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
    uname = f"@{u['username']}" if u.get('username') else "—"
    status = u['status']
    valid = fmt_dt(u.get('valid_until')) if u.get('valid_until') else "Бессрочно"
    
    text = (
        f"👤 <b>Пользователь:</b> {name}\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"🔗 Username: {uname}\n"
        f"📌 Статус: {status}\n"
        f"📅 Срок до: {valid}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Запретить", callback_data=f"admin_deny:{uid}")],
        [InlineKeyboardButton(text="📅 Срок", callback_data=f"term_menu:{uid}:user")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_users")]
    ])
    
    await cq.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("admin_deny:"))
async def cb_admin_deny(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    uid = int(cq.data.split(":")[1])
    supabase.table("users").update({"status": "blocked"}).eq("id", uid).execute()
    await safe_answer(cq, "Пользователь заблокирован", show_alert=True)
    
    # --- ДОБАВЛЕНО: Уведомление пользователю при ручной блокировке ---
    try:
        await bot.send_message(uid, "🔒 <b>Доступ заблокирован.</b>\nСвяжитесь с администратором для восстановления: @GaspardShop")
    except Exception as e:
        logger.warning(f"Could not send block notify: {e}")
    # ------------------------------------------------------------------
    
    await cb_admin_user_card(cq)

# ── Установка срока ─────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("term_menu:"))
async def cb_term_menu(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    
    parts = cq.data.split(":")
    target_id = parts[1] 
    is_req = len(parts) > 2 and parts[2] == "req"
    
    text = "📅 <b>Выберите срок действия</b>\nНа сколько выдать доступ?"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день", callback_data=f"term_set:{target_id}:1:{'req' if is_req else 'user'}"),
         InlineKeyboardButton(text="7 дней", callback_data=f"term_set:{target_id}:7:{'req' if is_req else 'user'}")],
        [InlineKeyboardButton(text="30 дней", callback_data=f"term_set:{target_id}:30:{'req' if is_req else 'user'}"),
         InlineKeyboardButton(text="Навсегда", callback_data=f"term_set:{target_id}:0:{'req' if is_req else 'user'}")],
        [InlineKeyboardButton(text="✍️ Ввести вручную", callback_data=f"term_manual:{target_id}:{'req' if is_req else 'user'}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_user:{target_id}" if not is_req else f"cancel")]
    ])
    
    await cq.message.edit_text(text, reply_markup=kb)

# ИЗМЕНЕНО: Умное добавление дней и отправка уведомления с кнопкой подключения
@dp.callback_query(F.data.startswith("term_set:"))
async def cb_term_set(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    
    parts = cq.data.split(":")
    target_id = int(parts[1])
    days = int(parts[2])
    target_type = parts[3]
    
    # Определяем ID пользователя (из заявки или напрямую)
    user_id = target_id
    if target_type != "user":
        req = fetch_one(supabase.table("access_requests").select("user_id").eq("id", target_id))
        user_id = req["user_id"] if req else None
        
    if not user_id:
        await cq.message.edit_text("Ошибка: пользователь не найден.")
        return

    # Получаем текущую дату окончания
    u_data = fetch_one(supabase.table("users").select("valid_until").eq("id", user_id))
    current_valid = u_data.get("valid_until") if u_data else None
    
    valid_until = None
    is_extending = False
    
    if days > 0:
        now = now_utc()
        if current_valid:
            try:
                curr_dt = datetime.fromisoformat(current_valid.replace("Z", "+00:00"))
                if curr_dt > now:
                    # Срок еще активен — прибавляем дни к текущему сроку
                    valid_until = (curr_dt + timedelta(days=days)).isoformat()
                    is_extending = True
                else:
                    # Срок уже истек — отсчитываем от сегодня
                    valid_until = (now + timedelta(days=days)).isoformat()
            except ValueError:
                valid_until = (now + timedelta(days=days)).isoformat()
        else:
            # Срока не было — отсчитываем от сегодня
            valid_until = (now + timedelta(days=days)).isoformat()

    # Формируем текст уведомления
    if valid_until:
        if is_extending:
            # КУРСИВ для текста добавления, ЖИРНЫЙ для даты
            user_text = f"<i>Вам добавлено {days} дн. к текущему сроку.</i>\nНовая дата окончания: <b>{fmt_dt(valid_until)}</b>\n\nДля продления в будущем обращайтесь: @GaspardShop"
        else:
            user_text = f"✅ <b>Вам установлен срок доступа!</b>\n\nВаш доступ к боту «Глазок» активен до: <b>{fmt_dt(valid_until)}</b>.\n\nПо окончании срока вам потребуется продлить его у администратора (@GaspardShop)."
    else:
        user_text = "✅ <b>Вам выдан бессрочный доступ!</b>\n\nДобро пожаловать в «Глазок»."

    # Кнопка подключения
    user_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Подключить Глазок", callback_data="user_connect")]
    ])

    if target_type == "user":
        supabase.table("users").update({"status": "approved", "valid_until": valid_until, "expiry_notified": False}).eq("id", target_id).execute()
        try: await bot.send_message(target_id, user_text, reply_markup=user_kb)
        except Exception as e: logger.warning(f"Could not send term notify to {target_id}: {e}")
    else:
        req = fetch_one(supabase.table("access_requests").select("user_id").eq("id", target_id))
        if req:
            supabase.table("users").update({"status": "approved", "valid_until": valid_until, "expiry_notified": False}).eq("id", req["user_id"]).execute()
            supabase.table("access_requests").update({"status": "approved", "resolved_at": now_utc().isoformat()}).eq("id", target_id).execute()
            
            try: 
                await bot.send_message(req["user_id"], user_text, reply_markup=user_kb)
            except Exception as e: logger.warning(f"Could not send term notify to {req['user_id']}: {e}")

    await cq.message.edit_text("✅ Срок успешно установлен.")
    await asyncio.sleep(1.5)
    if target_type == "user":
        await cb_admin_user_card(cq)
    else:
        await cq.message.delete()

@dp.callback_query(F.data.startswith("term_manual:"))
async def cb_term_manual(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    
    parts = cq.data.split(":")
    await state.update_data(target_id=parts[1], target_type=parts[2])
    await state.set_state(AdminState.waiting_for_custom_date)
    
    await cq.message.edit_text("✍️ Отправьте дату и время в формате:\n<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\nПример: <code>31.12.2026 23:59</code>")

# ИЗМЕНЕНО: Отправка уведомления при ручном вводе даты
@dp.message(AdminState.waiting_for_custom_date)
async def process_custom_date(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    
    try:
        dt = datetime.strptime(message.text, "%d.%m.%Y %H:%M").replace(tzinfo=MSK_TZ)
        valid_until = dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        await message.answer("❌ Неверный формат. Попробуйте снова: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>")
        return
        
    data = await state.get_data()
    target_id = int(data["target_id"])
    target_type = data["target_type"]
    await state.clear()
    
    # Текст для пользователя
    user_text = f"✅ <b>Вам установлен срок доступа!</b>\n\nВаш доступ к боту «Глазок» активен до: <b>{fmt_dt(valid_until)}</b>.\n\nПо окончании срока вам потребуется продлить его у администратора (@GaspardShop)."
    
    # Кнопка подключения
    user_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Подключить Глазок", callback_data="user_connect")]
    ])
    
    if target_type == "user":
        supabase.table("users").update({"status": "approved", "valid_until": valid_until, "expiry_notified": False}).eq("id", target_id).execute()
        try: await bot.send_message(target_id, user_text, reply_markup=user_kb)
        except Exception as e: logger.warning(f"Could not send term notify to {target_id}: {e}")
        
        await message.answer(f"✅ Срок установлен до: {fmt_dt(valid_until)}")
    else:
        req = fetch_one(supabase.table("access_requests").select("user_id").eq("id", target_id))
        if req:
            supabase.table("users").update({"status": "approved", "valid_until": valid_until, "expiry_notified": False}).eq("id", req["user_id"]).execute()
            supabase.table("access_requests").update({"status": "approved", "resolved_at": now_utc().isoformat()}).eq("id", target_id).execute()
            
            try: 
                await bot.send_message(req["user_id"], user_text, reply_markup=user_kb)
            except Exception as e: logger.warning(f"Could not send term notify to {req['user_id']}: {e}")
            
        await message.answer(f"✅ Пользователь одобрен, срок до: {fmt_dt(valid_until)}")

# ── Стандартные команды ──────────────────────────────────────────────────────

# ИЗМЕНЕНО: Отправляем starti.PNG при старте
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Начать пользоваться", callback_data="start_usage")]
    ])
    
    if message.from_user.id == ADMIN_ID:
        admin_kb = types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="👥 Пользователи")]],
            resize_keyboard=True,
            is_persistent=True
        )
        await message.answer("🛠 <b>Режим администратора активирован.</b>", reply_markup=admin_kb)

    caption = (
        "👋 Добро пожаловать в «Глазок»!\n"
        "👁️ Глазок — ваш помощник для сохранения доступной истории сообщений "
        "и отслеживания удалений.\n👨‍💻 Создатель: Khamoi\n\n"
        "Для начала нажмите кнопку ниже."
    )
    
    try:
        await message.answer_photo(photo=STARTI_IMG_URL, caption=caption, reply_markup=kb)
    except Exception as e:
        logger.error(f"Не удалось отправить фото /start: {e}")
        await message.answer(caption, reply_markup=kb)

@dp.message(F.text == "👥 Пользователи")
async def handle_admin_users_btn(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        res = supabase.table("users").select("*").neq("id", ADMIN_ID).order("created_at", desc=True).limit(30).execute()
        users = res.data if res.data else []
        if not users:
            await message.answer("В базе пока нет других пользователей.")
            return
        kb_buttons = []
        for u in users:
            name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
            if not name: name = str(u['id'])
            status_emoji = "🟢" if u['status'] == 'approved' else ("⏳" if u['status'] == 'pending' else "🔴")
            kb_buttons.append([InlineKeyboardButton(text=f"{status_emoji} {name} (ID: {u['id']})", callback_data=f"admin_user:{u['id']}")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
        await message.answer(f"👥 <b>Список пользователей</b> (найдено: {len(users)})", reply_markup=kb)
    except Exception as e:
        await message.answer(f"⚠️ <b>Ошибка при загрузке списка:</b>\n<code>{str(e)}</code>")

@dp.callback_query(F.data == "start_usage")
async def cb_start_usage(cq: CallbackQuery):
    await safe_answer(cq)
    try:
        user = ensure_user(cq.from_user)
    except Exception as e:
        await cq.message.answer(f"⚠️ <b>Ошибка базы данных Supabase:</b>\n<code>{str(e)}</code>")
        return
        
    if user["status"] == "approved":
        if user.get("valid_until"):
            try:
                v_until = datetime.fromisoformat(user["valid_until"].replace("Z", "+00:00"))
                if v_until < now_utc():
                    supabase.table("users").update({"status": "denied"}).eq("id", user["id"]).execute()
                    await cq.message.answer("❌ Срок действия вашего доступа истек.")
                    return
            except ValueError:
                pass
                
        text, kb = await get_instruction_msg()
        try:
            await cq.message.answer_photo(photo=START_IMG_URL, caption=text, reply_markup=kb)
        except Exception:
            await cq.message.answer(text, reply_markup=kb)
        return
    if user["status"] in ("denied", "blocked"):
        await cq.message.answer("❌ Доступ запрещён.")
        return
        
    try:
        req, is_new = create_access_request(user["id"])
    except Exception as e:
        await cq.message.answer(f"⚠️ <b>Ошибка при создании заявки:</b>\n<code>{str(e)}</code>")
        return

    if not is_new:
        await cq.message.answer("⏳ Ваша заявка уже ожидает решения администратора.")
        return

    await cq.message.answer("📨 Заявка отправлена администратору. Ожидайте решения.")
    
    uname = f"@{cq.from_user.username}" if cq.from_user.username else "отсутствует"
    text = (
        f"🔔 <b>НОВАЯ ЗАЯВКА</b>\n"
        f"👤 Имя: {cq.from_user.first_name or '—'}\n"
        f"👥 Фамилия: {cq.from_user.last_name or 'отсутствует'}\n"
        f"🔗 Username: {uname}\n"
        f"🆔 Telegram ID: <code>{cq.from_user.id}</code>\n"
        f"📅 Дата: {now_utc().astimezone(MSK_TZ).strftime('%d.%m.%Y')}\n"
        f"🕒 Время: {now_utc().astimezone(MSK_TZ).strftime('%H:%M')}\n"
        "📌 Статус: Ожидает рассмотрения"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Перейти в диалог", url=f"tg://user?id={cq.from_user.id}")],
        [InlineKeyboardButton(text="🟢 Разрешить", callback_data=f"approve:{req['id']}"),
         InlineKeyboardButton(text="🔴 Запретить", callback_data=f"deny:{req['id']}")]
    ])
    
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=kb)
    except Exception as e:
        logger.error("admin notify failed: %s", e)

@dp.callback_query(F.data == "show_instructions_info")
async def cb_show_instructions_info(cq: CallbackQuery):
    text = (
        "1. Нажмите кнопку \"Скопировать @\".\n"
        "2. Нажмите кнопку \"🔌 Подключить Глазок\"\n"
        "3. Выберите \"Автоматизация чатов\" и вставьте скопированный юзернейм."
    )
    await safe_answer(cq, text, show_alert=True)

# ИЗМЕНЕНО: При нажатии на кнопку подключения удаляем старое сообщение и присылаем фото с инструкцией
@dp.callback_query(F.data == "user_connect")
async def cb_user_connect(cq: CallbackQuery):
    await safe_answer(cq)
    text, kb = await get_instruction_msg()
    
    try: await cq.message.delete()
    except Exception: pass
    
    try:
        await cq.message.answer_photo(photo=START_IMG_URL, caption=text, reply_markup=kb)
    except Exception as e:
        logger.warning(f"Could not send instruction photo: {e}")
        await cq.message.answer(text, reply_markup=kb)

# ИЗМЕНЕНО: При нажатии "Разрешить" удаляем сообщение с заявкой и показываем меню сроков
@dp.callback_query(F.data.startswith("approve:"))
async def cb_approve(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await safe_answer(cq, "Недостаточно прав", show_alert=True)
        return
    req_id = int(cq.data.split(":")[1])
    
    try: await cq.message.delete()
    except Exception: pass
    
    text = "📅 <b>Выберите срок действия</b>\nНа сколько выдать доступ?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день", callback_data=f"term_set:{req_id}:1:req"),
         InlineKeyboardButton(text="7 дней", callback_data=f"term_set:{req_id}:7:req")],
        [InlineKeyboardButton(text="30 дней", callback_data=f"term_set:{req_id}:30:req"),
         InlineKeyboardButton(text="Навсегда", callback_data=f"term_set:{req_id}:0:req")],
        [InlineKeyboardButton(text="✍️ Ввести вручную", callback_data=f"term_manual:{req_id}:req")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    await cq.message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("deny:"))
async def cb_deny(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await safe_answer(cq, "Недостаточно прав", show_alert=True)
        return
    req_id = int(cq.data.split(":")[1])
    upd = update_request_status(req_id, "denied", cq.from_user.id)
    if not upd:
        await safe_answer(cq, "Заявка уже обработана", show_alert=True)
        return
    await safe_answer(cq, "Отклонено")
    try:
        await bot.send_message(upd["user_id"], "❌ Ваша заявка отклонена администратором.")
    except Exception:
        pass
    await cq.message.delete()

# ── Aiogram: Business-события ────────────────────────────────────────────────

@dp.business_connection()
async def on_business_connection(conn: types.BusinessConnection):
    supabase.table("business_connections").upsert({
        "id": conn.id, "owner_id": conn.user.id,
        "is_enabled": conn.is_enabled, "updated_at": now_utc().isoformat(),
    }, on_conflict="id").execute()

    if conn.is_enabled:
        # УБРАНА ИНЛАЙН КНОПКА ОТКРЫТИЯ МИНИ ЭП
        text = (
            "✅ <b>Бот успешно подключён к автоматизации чатов!</b>\n\n"
            "Теперь я буду сохранять историю сообщений и присылать уведомления, "
            "если собеседник удалит или изменит сообщение.\n\n"
            "Все сохранённые данные доступны в приложении «Глазок»."
        )
        try:
            await bot.send_message(conn.user.id, text)
        except Exception as e:
            logger.error(f"Failed to send business connection notify: {e}")
    else:
        try:
            await bot.send_message(conn.user.id, "⚠️ <b>Бот отключён от автоматизации чатов.</b>\nУведомления об удалённых сообщениях больше не будут приходить.")
        except Exception as e:
            logger.error(f"Failed to send business disconnect notify: {e}")

@dp.business_message()
async def on_business_message(msg: types.Message):
    if not msg.business_connection_id:
        return
    owner_id = get_owner_by_connection(msg.business_connection_id)
    if not owner_id:
        return
        
    if not is_user_active(owner_id):
        return
        
    upsert_chat(msg.business_connection_id, msg.chat, owner_id)
    upsert_contact(owner_id, msg.chat)

    messages_to_process = [msg]
    if msg.reply_to_message:
        messages_to_process.append(msg.reply_to_message)

    for target_msg in messages_to_process:
        sender = target_msg.from_user
        sender_id = sender.id if sender else None
        is_outgoing = (sender_id == owner_id)
        
        media_type = media_file_id = None
        if target_msg.photo:
            media_type, media_file_id = "photo", target_msg.photo[-1].file_id
        elif target_msg.video:
            media_type, media_file_id = "video", target_msg.video.file_id
        elif target_msg.animation:
            media_type, media_file_id = "animation", target_msg.animation.file_id
        elif target_msg.video_note:
            media_type, media_file_id = "video_note", target_msg.video_note.file_id
            
        is_spoiler = getattr(target_msg, 'has_media_spoiler', False)
        is_ttl = getattr(target_msg, 'ttl_seconds', None) is not None
        is_ephemeral = is_spoiler or is_ttl
        
        if is_outgoing and media_type:
            continue
            
        supabase.table("messages").upsert({
            "connection_id": msg.business_connection_id, "chat_id": target_msg.chat.id,
            "message_id": target_msg.message_id, "owner_id": owner_id,
            "sender_id": sender_id,
            "sender_name": sender.full_name if sender else None,
            "sender_username": sender.username if sender else None,
            "text": target_msg.text or target_msg.caption,
            "media_type": media_type, "media_file_id": media_file_id,
            "sent_at": target_msg.date.isoformat() if target_msg.date else None,
        }, on_conflict="connection_id,chat_id,message_id").execute()
        
        if media_file_id and media_type in ("photo", "video", "animation", "video_note"):
            try:
                file = await bot.get_file(media_file_id)
                if (file.file_size or 0) <= 20 * 1024 * 1024:
                    buf = BytesIO()
                    await bot.download_file(file.file_path, buf)
                    file_bytes = buf.getvalue()
                    
                    path = f"{owner_id}/{msg.business_connection_id}/{target_msg.chat.id}/{target_msg.message_id}.{media_type}"
                    
                    supabase.storage.from_(STORAGE_BUCKET).upload(
                        path, file_bytes,
                        file_options={
                            "content-type": "image/jpeg" if media_type == "photo" else "video/mp4",
                            "upsert": "true"
                        }
                    )
                    mid_row = fetch_one(supabase.table("messages").select("id").eq("connection_id", msg.business_connection_id).eq("chat_id", target_msg.chat.id).eq("message_id", target_msg.message_id))
                    if mid_row:
                        supabase.table("media_files").upsert({
                            "message_id": mid_row["id"], "owner_id": owner_id,
                            "media_type": media_type, "storage_path": path, "file_size": file.file_size,
                        }, on_conflict="message_id").execute()

                    if target_msg.message_id != msg.message_id and not is_outgoing:
                        try:
                            if media_type == "photo":
                                prefix = "📸 Сохранено фото"
                                if is_spoiler: prefix = "📸 Сохранено однократное фото"
                                elif is_ttl: prefix = "📸 Сохранено исчезающее фото"
                                input_file = types.BufferedInputFile(file_bytes, filename="photo.jpg")
                                await bot.send_photo(owner_id, input_file, caption=prefix)
                            elif media_type == "video":
                                prefix = "🎬 Сохранено видео"
                                if is_spoiler: prefix = "🎬 Сохранено однократное видео"
                                elif is_ttl: prefix = "🎬 Сохранено исчезающее видео"
                                input_file = types.BufferedInputFile(file_bytes, filename="video.mp4")
                                await bot.send_video(owner_id, input_file, caption=prefix)
                            elif media_type == "animation":
                                input_file = types.BufferedInputFile(file_bytes, filename="animation.mp4")
                                await bot.send_animation(owner_id, input_file, caption="🎬 Сохранена GIF-анимация")
                            elif media_type == "video_note":
                                input_file = types.BufferedInputFile(file_bytes, filename="video_note.mp4")
                                await bot.send_video_note(owner_id, input_file)
                                await bot.send_message(owner_id, "⭕️ Сохранен кружок")
                        except Exception as send_err:
                            logger.warning(f"Could not send TTL/Spoiler media back to owner: {send_err}")
            except Exception as e:
                logger.warning(f"media save failed: {e}")

@dp.edited_business_message()
async def on_edited_business_message(msg: types.Message):
    if not msg.business_connection_id:
        return
    owner_id = get_owner_by_connection(msg.business_connection_id)
    if not owner_id:
        return
        
    if not is_user_active(owner_id):
        return
        
    row = fetch_one(supabase.table("messages").select("id,text,sender_id,sender_name,sender_username").eq("connection_id", msg.business_connection_id).eq("chat_id", msg.chat.id).eq("message_id", msg.message_id))
    if not row:
        return
        
    old_text, new_text = row.get("text"), (msg.text or msg.caption)
    if old_text == new_text:
        return
        
    supabase.table("message_versions").insert({
        "message_id": row["id"], "old_text": old_text,
        "new_text": new_text, "edited_at": now_utc().isoformat(),
    }).execute()
    
    supabase.table("messages").update({"text": new_text}).eq("id", row["id"]).execute()
    
    sender_id = row.get("sender_id")
    if sender_id != owner_id:
        sender_name = row.get("sender_name") or "Неизвестно"
        username = row.get("sender_username")
        uname_text = f"@{username}" if username else "Не указан"
        
        text = (
            f"✏️ <b>СООБЩЕНИЕ ИЗМЕНЕНО</b>\n\n"
            f"👤 Отправитель: {sender_name}\n"
            f"🔗 Username: {uname_text}\n"
            f"🆔 Telegram ID: <code>{sender_id}</code>\n\n"
            f"Было:\n"
            f"<blockquote>«{old_text or 'Содержимое недоступно'}»</blockquote>\n"
            f"Стало:\n"
            f"<blockquote>«{new_text or 'Содержимое недоступно'}»</blockquote>"
        )
        try:
            await bot.send_message(owner_id, text)
        except Exception as e:
            logger.warning(f"Could not send edit notify: {e}")

@dp.deleted_business_messages()
async def on_deleted_business_messages(event: types.BusinessMessagesDeleted):
    owner_id = get_owner_by_connection(event.business_connection_id)
    if not owner_id:
        return
        
    if not is_user_active(owner_id):
        return
        
    parts = []
    media_to_send = [] 
    valid_message_ids = []
    
    for mid in event.message_ids:
        row = fetch_one(supabase.table("messages").select("*").eq("connection_id", event.business_connection_id).eq("chat_id", event.chat.id).eq("message_id", mid))
        
        if row:
            m = row
            if m.get("sender_id") == owner_id:
                continue
                
            valid_message_ids.append(mid)
            
            sent = fmt_dt(m.get("sent_at"))
            username = m.get('sender_username')
            uname_text = f"@{username}" if username else "Не указан"
            sender_id = m.get('sender_id') or '—'
            sender_name = m.get('sender_name') or 'Неизвестно'
            
            parts.append(
                f"👤 Отправитель: {sender_name}\n"
                f"🔗 Username: {uname_text}\n"
                f"🆔 Telegram ID: <code>{sender_id}</code>\n"
                f"💬 Сообщение:\n"
                f"<blockquote>«{m.get('text') or 'Содержимое недоступно'}»</blockquote>\n"
                f"🕒 Отправлено: {sent}"
            )
            
            if m.get("media_type"):
                mf_row = fetch_one(supabase.table("media_files").select("*").eq("message_id", m["id"]))
                if mf_row:
                    media_to_send.append({
                        "type": mf_row["media_type"],
                        "path": mf_row["storage_path"]
                    })
                    
    if not valid_message_ids:
        return
        
    event_hash = hashlib.sha256(
        f"{event.business_connection_id}:{event.chat.id}:{','.join(map(str, sorted(valid_message_ids)))}".encode()
    ).hexdigest()
    existing = fetch_one(supabase.table("deletion_events").select("id").eq("event_hash", event_hash))
    if existing:
        return
        
    supabase.table("deletion_events").insert({
        "connection_id": event.business_connection_id, "chat_id": event.chat.id,
        "message_ids": valid_message_ids, "owner_id": owner_id,
        "detected_at": now_utc().isoformat(), "event_hash": event_hash,
    }).execute()
            
    if parts:
        text = (
            "🗑 <b>УДАЛЕНО СООБЩЕНИЕ</b>\n\n" + "\n\n".join(parts) +
            f"\n\n🗑 Удаление обнаружено: {fmt_dt(now_utc())}"
        )
        
        try:
            await bot.send_message(owner_id, text)
            
            for media in media_to_send:
                try:
                    res = supabase.storage.from_(STORAGE_BUCKET).download(media["path"])
                    ext = "jpg" if media["type"] == "photo" else "mp4"
                    input_file = types.BufferedInputFile(res, filename=f"deleted_{media['type']}.{ext}")
                    
                    if media["type"] == "photo":
                        await bot.send_photo(owner_id, input_file, caption="📸 Прикрепленное фото из удаленного сообщения")
                    elif media["type"] == "video":
                        await bot.send_video(owner_id, input_file, caption="🎬 Прикрепленное видео из удаленного сообщения")
                    elif media["type"] == "animation":
                        await bot.send_animation(owner_id, input_file, caption="🎬 Прикрепленная GIF из удаленного сообщения")
                    elif media["type"] == "video_note":
                        await bot.send_video_note(owner_id, input_file)
                        await bot.send_message(owner_id, "⭕️ Прикреплен кружок из удаленного сообщения")
                except Exception as e:
                    logger.warning(f"Could not send deleted media: {e}")
                    
        except Exception:
            pass

# ── FastAPI ──────────────────────────────────────────────────────────────────

app = FastAPI(title="История API")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(check_expirations())

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="invalid secret")
    try:
        data = await request.json()
        update = types.Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"WEBHOOK ERROR: {e}", exc_info=True)
        err_str = str(e)
        if "query is too old" not in err_str and "response timeout expired" not in err_str:
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ <b>Ошибка в webhook:</b>\n<code>{err_str}</code>")
            except Exception:
                pass
    return {"ok": True}

@app.get("/api/me")
async def api_me(user: dict = Depends(get_verified_user)):
    return {"user": user}

@app.get("/api/stats")
async def api_stats(user: dict = Depends(get_verified_user)):
    uid = user["id"]
    deleted = supabase.table("deletion_events").select("id", count="exact").eq("owner_id", uid).execute().count or 0
    media = supabase.table("media_files").select("id", count="exact").eq("owner_id", uid).execute().count or 0
    chats = supabase.table("chats").select("id", count="exact").eq("owner_id", uid).execute().count or 0
    return {"deleted": deleted, "media": media, "chats": chats}

@app.get("/api/contacts")
async def api_contacts(q: str = Query(""), user: dict = Depends(get_verified_user)):
    uid = user["id"]
    
    user_msgs = supabase.table("messages").select("id, chat_id").eq("owner_id", uid).execute()
    user_msg_ids = [m["id"] for m in user_msgs.data] if user_msgs.data else []
    msg_to_chat = {m["id"]: m["chat_id"] for m in user_msgs.data} if user_msgs.data else {}
    
    active_chat_ids = set()
    
    dels = supabase.table("deletion_events").select("chat_id").eq("owner_id", uid).execute()
    if dels.data:
        for d in dels.data:
            active_chat_ids.add(d["chat_id"])
            
    media = supabase.table("media_files").select("message_id").eq("owner_id", uid).execute()
    if media.data:
        for m in media.data:
            chat_id = msg_to_chat.get(m["message_id"])
            if chat_id:
                active_chat_ids.add(chat_id)
                
    if user_msg_ids:
        vers_res = supabase.table("message_versions").select("message_id").in_("message_id", user_msg_ids).execute()
        if vers_res.data:
            for v in vers_res.data:
                chat_id = msg_to_chat.get(v["message_id"])
                if chat_id:
                    active_chat_ids.add(chat_id)
                    
    if not active_chat_ids:
        return {"contacts": []}
        
    active_ids_list = list(active_chat_ids)
    
    query = supabase.table("contacts").select("*").eq("owner_id", uid).in_("telegram_user_id", active_ids_list)
    if q:
        filters = f"first_name.ilike.%{q}%,last_name.ilike.%{q}%,username.ilike.%{q}%"
        if q.isdigit():
            filters += f",telegram_user_id.eq.{q}"
        query = query.or_(filters)
        
    return {"contacts": query.order("created_at", desc=True).limit(100).execute().data}

@app.get("/api/chat/{chat_id}/messages")
async def api_chat_messages(chat_id: int, user: dict = Depends(get_verified_user)):
    uid = user["id"]
    
    msgs_res = supabase.table("messages").select(
        "id,message_id,text,media_type,sent_at,sender_name,sender_username,sender_id"
    ).eq("owner_id", uid).eq("chat_id", chat_id).order("sent_at", desc=True).limit(200).execute()
    all_msgs = msgs_res.data if msgs_res.data else []
    
    versions = []
    if all_msgs:
        ids = [m["id"] for m in all_msgs]
        versions = supabase.table("message_versions").select("*").in_("message_id", ids).execute().data
        
    dels_res = supabase.table("deletion_events").select(
        "id,message_ids,detected_at"
    ).eq("owner_id", uid).eq("chat_id", chat_id).order("detected_at", desc=True).limit(100).execute()
    dels = dels_res.data if dels_res.data else []
    
    deleted_telegram_ids = set()
    for d in dels:
        deleted_telegram_ids.update(d["message_ids"])
        
    edited_db_ids = {v["message_id"] for v in versions}
    
    filtered_msgs = []
    for m in all_msgs:
        if m.get("sender_id") == uid:
            continue
            
        is_deleted = m["message_id"] in deleted_telegram_ids
        is_edited = m["id"] in edited_db_ids
        
        if is_deleted or is_edited:
            filtered_msgs.append(m)
    
    return {"messages": filtered_msgs, "versions": versions, "deletions": dels}

@app.get("/api/chat/{chat_id}/media")
async def api_chat_media(chat_id: int, user: dict = Depends(get_verified_user)):
    uid = user["id"]
    mids = supabase.table("messages").select("id").eq("owner_id", uid).eq("chat_id", chat_id).execute().data
    if not mids:
        return {"media": []}
    ids = [m["id"] for m in mids]
    files = supabase.table("media_files").select("*").eq("owner_id", uid).in_("message_id", ids).order("created_at", desc=True).limit(100).execute().data
    result = []
    for f in files:
        try:
            signed = supabase.storage.from_(STORAGE_BUCKET).create_signed_url(f["storage_path"], 3600)
            result.append({**f, "url": signed.get("signedURL") or signed.get("signedUrl")})
        except Exception:
            result.append({**f, "url": None})
    return {"media": result}

@app.delete("/api/history")
async def api_delete_history(user: dict = Depends(get_verified_user)):
    uid = user["id"]
    supabase.table("messages").delete().eq("owner_id", uid).execute()
    supabase.table("deletion_events").delete().eq("owner_id", uid).execute()
    supabase.table("chats").delete().eq("owner_id", uid).execute()
    supabase.table("contacts").delete().eq("owner_id", uid).execute()
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok", "time": now_utc().isoformat()}

@app.get("/")
async def serve_index():
    return FileResponse("index.html", media_type="text/html")

@app.get("/static/app.js")
async def serve_js():
    return FileResponse("app.js", media_type="application/javascript")

@app.get("/static/style.css")
async def serve_css():
    return FileResponse("style.css", media_type="text/css")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
