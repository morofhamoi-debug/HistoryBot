"""История — единый backend: FastAPI + aiogram 3 + Supabase."""

import os
import json
import hmac
import hashlib
import logging
import asyncio
import time
import uuid
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

# ── Состояния ────────────────────────────────────────────────────────────────
class AdminState(StatesGroup):
    waiting_for_custom_date = State()
    waiting_for_broadcast = State()
    waiting_for_user_msg = State()
    waiting_for_block_id = State()
    waiting_for_unblock_id = State()
    waiting_for_ref_ban_id = State()
    waiting_for_ref_unban_id = State()

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

# ── ДОБАВЛЕНО: Реферальная программа ────────────────────────────────────────

def extend_validity(user_id: int, days: int) -> Optional[str]:
    """Продлевает valid_until на days. Возвращает новую дату (ISO) или None,
    если продлевать некуда (юзер не найден или у него бессрочный доступ)."""
    u = fetch_one(supabase.table("users").select("valid_until").eq("id", user_id))
    if not u:
        return None
    if not u.get("valid_until"):
        return None
    now = now_utc()
    try:
        curr = datetime.fromisoformat(u["valid_until"].replace("Z", "+00:00"))
    except ValueError:
        curr = now
    base = curr if curr > now else now
    new_valid = (base + timedelta(days=days)).isoformat()
    supabase.table("users").update({
        "valid_until": new_valid,
        "expiry_notified": False,
        "expiry_warning_notified": False,
    }).eq("id", user_id).execute()
    return new_valid


def try_register_referral(tg_user: types.User, referrer_id: int) -> tuple[bool, Optional[dict]]:
    """Антифрод + запись referrals. Юзер должен уже существовать в users."""
    if tg_user.id == referrer_id:
        return False, None
    ref = fetch_one(supabase.table("users").select("*").eq("id", referrer_id))
    if not ref or ref.get("status") != "approved" or ref.get("referral_banned"):
        return False, None
    existing = fetch_one(supabase.table("users").select("referred_by").eq("id", tg_user.id))
    if existing and existing.get("referred_by"):
        return False, None
    if ref.get("referred_by") == tg_user.id:
        return False, None

    supabase.table("users").update({"referred_by": referrer_id}).eq("id", tg_user.id).execute()
    try:
        supabase.table("referrals").insert({
            "referrer_id": referrer_id,
            "referred_id": tg_user.id,
            "status": "pending",
        }).execute()
    except Exception as e:
        logger.warning(f"referral insert failed: {e}")
        return False, None
    return True, ref


async def notify_admin_anomaly_if_needed(referrer_id: int):
    """Пинг админу при >10 приглашений за 24 часа. Без автобана."""
    try:
        day_ago = (now_utc() - timedelta(hours=24)).isoformat()
        count = supabase.table("referrals").select("id", count="exact").eq("referrer_id", referrer_id).gte("created_at", day_ago).execute().count or 0
        if count >= 11:
            referrer = fetch_one(supabase.table("users").select("first_name, username").eq("id", referrer_id))
            label = f"@{referrer['username']}" if referrer and referrer.get("username") else (referrer.get("first_name", str(referrer_id)) if referrer else str(referrer_id))
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ <b>Аномалия в рефералке</b>\n\n"
                    f"👤 {label} (id: <code>{referrer_id}</code>) пригласил {count} человек за 24 часа.\n"
                    f"Проверь на накрутку.",
                )
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"notify_admin_anomaly_if_needed failed: {e}")


async def process_referral_reward(user_id: int):
    """Начисление награды приглашённому и рефереру. Вызывается при одобрении."""
    ref = fetch_one(supabase.table("referrals").select("*").eq("referred_id", user_id).eq("status", "pending"))
    if not ref:
        return

    referrer_id = ref["referrer_id"]
    reward_days = ref.get("reward_days") or 3
    referred_bonus = ref.get("referred_bonus_days") or 3

    referrer = fetch_one(supabase.table("users").select("first_name, valid_until, referral_banned").eq("id", referrer_id))

    target_status = "rewarded"
    cancel_reason = None

    if not referrer or referrer.get("referral_banned"):
        target_status = "cancelled"
        cancel_reason = "banned_referrer"
    elif not referrer.get("valid_until"):
        target_status = "cancelled"
        cancel_reason = "lifetime_referrer"
    else:
        rewarded_count = supabase.table("referrals").select("id", count="exact").eq("referrer_id", referrer_id).eq("status", "rewarded").execute().count or 0
        if rewarded_count >= 20:
            target_status = "cancelled"
            cancel_reason = "limit"

    # Атомарно "занимаем" запись — защита от гонки
    update_data = {"status": target_status}
    if target_status == "rewarded":
        update_data["rewarded_at"] = now_utc().isoformat()
    else:
        update_data["cancel_reason"] = cancel_reason

    claim = supabase.table("referrals").update(update_data).eq("id", ref["id"]).eq("status", "pending").execute()
    if not claim.data:
        return  # уже обработана параллельно

    # Приглашённый получает бонус во всех случаях (если у него срочный доступ)
    new_valid_refd = extend_validity(user_id, referred_bonus)

    # Реферер получает награду только если статус rewarded
    new_valid_ref = None
    if target_status == "rewarded":
        new_valid_ref = extend_validity(referrer_id, reward_days)

    referred = fetch_one(supabase.table("users").select("first_name, username").eq("id", user_id))
    referred_label = f"@{referred['username']}" if referred and referred.get("username") else (referred.get("first_name", "пользователь") if referred else "пользователь")

    if target_status == "rewarded":
        try:
            await bot.send_message(
                referrer_id,
                f"💎 <b>+{reward_days} дня!</b>\n\n{referred_label} получил доступ по вашей ссылке.\n"
                f"Ваш доступ продлён до <b>{fmt_dt(new_valid_ref)}</b>.",
            )
        except Exception as e:
            logger.warning(f"referrer notify failed: {e}")
    elif cancel_reason == "limit":
        try:
            await bot.send_message(
                referrer_id,
                f"⚠️ По вашей ссылке пришёл {referred_label}, но лимит рефералов (20) достигнут. Награда не начислена.",
            )
        except Exception:
            pass
    elif cancel_reason == "lifetime_referrer":
        try:
            await bot.send_message(
                referrer_id,
                f"ℹ️ По вашей ссылке пришёл {referred_label}, но у вас бессрочный доступ — начислять некуда.",
            )
        except Exception:
            pass

    if new_valid_refd is not None:
        try:
            await bot.send_message(
                user_id,
                f"💎 <b>+{referred_bonus} дня бонусом!</b>\n\nВы зарегистрировались по реферальной ссылке.\n"
                f"Ваш доступ продлён до <b>{fmt_dt(new_valid_refd)}</b>.",
            )
        except Exception as e:
            logger.warning(f"referred notify failed: {e}")


async def render_referral(user_id: int):
    u = fetch_one(supabase.table("users").select("status, referral_banned").eq("id", user_id))
    if not u or u.get("status") != "approved":
        return "❌ Реферальная программа доступна только после одобрения доступа.", None
    if u.get("referral_banned"):
        return "🚫 Доступ к реферальной программе заблокирован администратором.", None

    all_refs = supabase.table("referrals").select("status").eq("referrer_id", user_id).execute().data or []
    pending = sum(1 for r in all_refs if r["status"] == "pending")
    rewarded = sum(1 for r in all_refs if r["status"] == "rewarded")
    cancelled = sum(1 for r in all_refs if r["status"] == "cancelled")
    days_earned = rewarded * 3

    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username
    except Exception:
        bot_username = "KhamoiHistory_Bot"

    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    share_text = "Залетай в «Глазок» — бот сохраняет удалённые сообщения!"

    text = (
        "🎁 <b>Реферальная программа</b>\n\n"
        "Приглашайте друзей и получайте <b>+3 дня</b> доступа к «Глазку» за каждого, кому одобрят доступ!\n"
        "<i>Ваш друг тоже получит +3 дня бонусом.</i>\n\n"
        f"🔗 Ваша ссылка:\n<code>{link}</code>\n\n"
        f"📊 Приглашено: <b>{pending + rewarded + cancelled}</b>\n"
        f"✅ Начислено: <b>{rewarded}</b> (+{days_earned} дней)\n"
        + (f"⏳ Ожидают одобрения: <b>{pending}</b>\n" if pending else "")
        + (f"⚠️ Не зачтено: <b>{cancelled}</b>\n" if cancelled else "")
    )
   share_url = f"https://t.me/share/url?url={link}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📤 Поделиться", 
            url=share_url
        )],
        [InlineKeyboardButton(text="📋 Мои приглашения", callback_data="my_referrals_list")],
    ])
    return text, kb


@dp.message(Command("ref", "referral"))
async def cmd_ref(message: types.Message):
    text, kb = await render_referral(message.from_user.id)
    if kb:
        await message.answer(text, reply_markup=kb)
    else:
        await message.answer(text)


@dp.callback_query(F.data == "show_referral")
async def cb_show_referral(cq: CallbackQuery):
    await safe_answer(cq)
    text, kb = await render_referral(cq.from_user.id)
    if kb:
        try:
            await cq.message.answer(text, reply_markup=kb)
        except Exception:
            pass
    else:
        await safe_answer(cq, text, show_alert=True)


@dp.callback_query(F.data == "my_referrals_list")
async def cb_my_referrals_list(cq: CallbackQuery):
    await safe_answer(cq)
    res = supabase.table("referrals").select("referred_id, status, created_at").eq("referrer_id", cq.from_user.id).order("created_at", desc=True).limit(20).execute()
    refs = res.data or []

    if not refs:
        await cq.message.answer("У вас пока нет приглашений.")
        return

    status_map = {"pending": "⏳ ожидает", "rewarded": "✅ зачтено", "cancelled": "❌ не зачтено"}
    lines = ["📋 <b>Ваши последние приглашения</b>\n"]
    for r in refs:
        uid = r["referred_id"]
        ref_user = fetch_one(supabase.table("users").select("first_name, username").eq("id", uid))
        if ref_user:
            name = f"@{ref_user['username']}" if ref_user.get("username") else (ref_user.get("first_name") or str(uid))
        else:
            name = str(uid)
        lines.append(f"• {name} — {status_map.get(r['status'], r['status'])} ({fmt_dt(r.get('created_at'))})")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="show_referral")],
    ])
    await cq.message.answer("\n".join(lines), reply_markup=kb)

# ── КОНЕЦ ДОБАВЛЕННОГО: Реферальная программа ───────────────────────────────

# ── ДОБАВЛЕНО ИЗМЕНЕНИЕ: Фоновая задача (добавлено предупреждение за 3 дня) ──
async def check_expirations():
    while True:
        try:
            now = now_utc()
            now_iso = now.isoformat()
            warning_date = (now + timedelta(days=3)).isoformat()

            # 1. Проверка тех, у кого срок УЖЕ истек
            res = supabase.table("users").select("id").lt("valid_until", now_iso).or_("expiry_notified.eq.false,expiry_notified.is.null").execute()
            if res.data:
                for u in res.data:
                    uid = u["id"]
                    try:
                        await bot.send_message(uid, "⏳ <b>Срок действия вашего доступа истек.</b>\nДля продления обратитесь к администратору: @GaspardShop")
                        supabase.table("users").update({"expiry_notified": True, "status": "denied"}).eq("id", uid).execute()
                    except Exception as e:
                        logger.warning(f"Could not send expiry message to {uid}: {e}")
                        supabase.table("users").update({"expiry_notified": True, "status": "denied"}).eq("id", uid).execute()

            # 2. Проверка тех, у кого осталось 3 дня или меньше
            res_warn = supabase.table("users").select("id").gte("valid_until", now_iso).lte("valid_until", warning_date).or_("expiry_warning_notified.eq.false,expiry_warning_notified.is.null").execute()
            if res_warn.data:
                for u in res_warn.data:
                    uid = u["id"]
                    try:
                        await bot.send_message(uid, "⚠️ <b>Внимание! Ваш доступ скоро закончится.</b>\n\nДоступ к боту «Глазок» будет приостановлен через 3 дня (или меньше).\nЧтобы не потерять историю, продлите доступ заранее.\n\nСвязаться с администратором: @GaspardShop")
                        supabase.table("users").update({"expiry_warning_notified": True}).eq("id", uid).execute()
                    except Exception as e:
                        logger.warning(f"Could not send warning message to {uid}: {e}")
                        supabase.table("users").update({"expiry_warning_notified": True}).eq("id", uid).execute()

        except Exception as e:
            logger.error(f"Error in check_expirations: {e}")
            
        await asyncio.sleep(300)
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
        [InlineKeyboardButton(text="? Инструкция", callback_data="show_instructions_info")],
        [InlineKeyboardButton(text="🎁 Реферальная программа", callback_data="show_referral")]
    ])
    return text, kb

# ── Aiogram: Админ-панель ────────────────────────────────────────────────────

def get_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"), 
         InlineKeyboardButton(text="💼 Подключения", callback_data="admin_conns")],
        [InlineKeyboardButton(text="✉️ Написать юзеру", callback_data="admin_msg_user")],
        [InlineKeyboardButton(text="📢 Написать всем (Рассылка)", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🎁 Рефералы", callback_data="admin_refs")],
        [InlineKeyboardButton(text="🔒 Заблокировать", callback_data="admin_block"), 
         InlineKeyboardButton(text="🔓 Разблокировать", callback_data="admin_unblock")]
    ])

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠 <b>Админ-панель</b>\nВыберите действие:", reply_markup=get_admin_kb())

# ── НОВЫЕ ОБРАБОТЧИКИ АДМИН-ПАНЕЛИ ──────────────────────────────────────────

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    
    users_all = supabase.table("users").select("id", count="exact").execute().count or 0
    users_approved = supabase.table("users").select("id", count="exact").eq("status", "approved").execute().count or 0
    users_pending = supabase.table("users").select("id", count="exact").eq("status", "pending").execute().count or 0
    users_blocked = supabase.table("users").select("id", count="exact").eq("status", "blocked").execute().count or 0
    
    media_count = supabase.table("media_files").select("id", count="exact").execute().count or 0
    deleted_count = supabase.table("deletion_events").select("id", count="exact").execute().count or 0
    connections = supabase.table("business_connections").select("id", count="exact").eq("is_enabled", True).execute().count or 0
    
    text = (
        f"📊 <b>Статистика «Глазок»</b>\n\n"
        f"👥 <b>Пользователи:</b> {users_all} всего\n"
        f"🟢 Одобрено: {users_approved}\n"
        f"⏳ В ожидании: {users_pending}\n"
        f"🔴 Заблокировано: {users_blocked}\n\n"
        f"💼 Активных Business: {connections}\n"
        f"💬 Удалений зафиксировано: {deleted_count}\n"
        f"📸 Медиа сохранено: {media_count}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]])
    await cq.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "admin_conns")
async def cb_admin_conns(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    
    res = supabase.table("business_connections").select("owner_id").eq("is_enabled", True).execute()
    if not res.data:
        text = "💼 <b>Активные подключения</b>\n\nСейчас нет активных Business-подключений."
    else:
        uids = [str(r["owner_id"]) for r in res.data]
        text = f"💼 <b>Активные подключения ({len(uids)})</b>\n\n" + "\n".join([f"🆔 <code>{uid}</code>" for uid in uids])
        
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]])
    await cq.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "admin_back")
async def cb_admin_back(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    await cq.message.edit_text("🛠 <b>Админ-панель</b>\nВыберите действие:", reply_markup=get_admin_kb())

# ── БЛОКИРОВКА / РАЗБЛОКИРОВКА ──────────────────────────────────────────────

@dp.callback_query(F.data == "admin_block")
async def cb_admin_block(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    await state.set_state(AdminState.waiting_for_block_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back")]])
    await cq.message.edit_text("🔒 Отправьте ID пользователя, которого нужно <b>заблокировать</b>:", reply_markup=kb)

@dp.callback_query(F.data == "admin_unblock")
async def cb_admin_unblock(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    await state.set_state(AdminState.waiting_for_unblock_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back")]])
    await cq.message.edit_text("🔓 Отправьте ID пользователя, которого нужно <b>разблокировать</b>:", reply_markup=kb)

@dp.message(AdminState.waiting_for_block_id)
async def process_block(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if not message.text.isdigit():
        await message.answer("⚠️ ID должен состоять только из цифр. Попробуйте снова.")
        return
    uid = int(message.text)
    await state.clear()
    supabase.table("users").update({"status": "blocked"}).eq("id", uid).execute()
    try: await bot.send_message(uid, "🔒 <b>Доступ заблокирован.</b>\nСвяжитесь с администратором: @GaspardShop")
    except Exception: pass
    await message.answer(f"✅ Пользователь <code>{uid}</code> заблокирован.", reply_markup=get_admin_kb())

@dp.message(AdminState.waiting_for_unblock_id)
async def process_unblock(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if not message.text.isdigit():
        await message.answer("⚠️ ID должен состоять только из цифр. Попробуйте снова.")
        return
    uid = int(message.text)
    await state.clear()
    supabase.table("users").update({"status": "approved"}).eq("id", uid).execute()
    try: await bot.send_message(uid, "✅ <b>Ваш доступ восстановлен!</b>\nДобро пожаловать обратно в «Глазок».")
    except Exception: pass
    await message.answer(f"✅ Пользователь <code>{uid}</code> разблокирован.", reply_markup=get_admin_kb())

# ── ДОБАВЛЕНО: Админ-секция рефералов ───────────────────────────────────────

@dp.callback_query(F.data == "admin_refs")
async def cb_admin_refs(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)

    all_refs = supabase.table("referrals").select("status, cancel_reason").execute().data or []
    total = len(all_refs)
    rewarded = sum(1 for r in all_refs if r["status"] == "rewarded")
    pending = sum(1 for r in all_refs if r["status"] == "pending")
    cancelled = sum(1 for r in all_refs if r["status"] == "cancelled")
    cancelled_limit = sum(1 for r in all_refs if r["status"] == "cancelled" and r.get("cancel_reason") == "limit")
    cancelled_banned = sum(1 for r in all_refs if r["status"] == "cancelled" and r.get("cancel_reason") == "banned_referrer")
    cancelled_lifetime = sum(1 for r in all_refs if r["status"] == "cancelled" and r.get("cancel_reason") == "lifetime_referrer")
    conversion = int((rewarded / total * 100)) if total else 0

    top_res = supabase.table("referrals").select("referrer_id").eq("status", "rewarded").execute()
    top_counts = {}
    for r in (top_res.data or []):
        top_counts[r["referrer_id"]] = top_counts.get(r["referrer_id"], 0) + 1
    top_sorted = sorted(top_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    top_lines = []
    for rid, cnt in top_sorted:
        u = fetch_one(supabase.table("users").select("first_name, username").eq("id", rid))
        label = f"@{u['username']}" if u and u.get("username") else (u.get("first_name") if u else str(rid))
        top_lines.append(f"• {label} (id: <code>{rid}</code>) — {cnt}")

    cancelled_detail = ""
    if cancelled:
        parts = []
        if cancelled_limit: parts.append(f"лимит: {cancelled_limit}")
        if cancelled_banned: parts.append(f"бан: {cancelled_banned}")
        if cancelled_lifetime: parts.append(f"бессрочник: {cancelled_lifetime}")
        if parts:
            cancelled_detail = f" ({', '.join(parts)})"

    text = (
        f"🎁 <b>Реферальная программа</b>\n\n"
        f"📊 Всего приглашений: <b>{total}</b>\n"
        f"✅ Начислено: <b>{rewarded}</b> (+{rewarded * 3} дней)\n"
        f"⏳ Ожидают: <b>{pending}</b>\n"
        f"❌ Отменено: <b>{cancelled}</b>{cancelled_detail}\n"
        f"📈 Конверсия: <b>{conversion}%</b>\n"
    )
    if top_lines:
        text += "\n🏆 <b>Топ-5 рефереров:</b>\n" + "\n".join(top_lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Последние 20", callback_data="admin_refs_list")],
        [InlineKeyboardButton(text="🚫 Забанить реферера", callback_data="admin_ref_ban"),
         InlineKeyboardButton(text="🔓 Разбанить", callback_data="admin_ref_unban")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    await cq.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data == "admin_refs_list")
async def cb_admin_refs_list(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)

    res = supabase.table("referrals").select("*").order("created_at", desc=True).limit(20).execute()
    refs = res.data or []

    if not refs:
        text = "Приглашений нет."
    else:
        status_emoji = {"pending": "⏳", "rewarded": "✅", "cancelled": "❌"}
        lines = ["📋 <b>Последние 20 приглашений</b>\n"]
        for r in refs:
            emoji = status_emoji.get(r["status"], "•")
            reason = f" ({r['cancel_reason']})" if r.get("cancel_reason") else ""
            lines.append(f"{emoji} <code>{r['referrer_id']}</code> → <code>{r['referred_id']}</code>{reason} · {fmt_dt(r.get('created_at'))}")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_refs")]
    ])
    await cq.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data == "admin_ref_ban")
async def cb_admin_ref_ban(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    await state.set_state(AdminState.waiting_for_ref_ban_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_refs")]])
    await cq.message.edit_text("🚫 Отправьте ID пользователя, которому нужно <b>заблокировать реферальную программу</b>:", reply_markup=kb)


@dp.message(AdminState.waiting_for_ref_ban_id)
async def process_ref_ban(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if not message.text.isdigit():
        await message.answer("⚠️ ID должен состоять только из цифр. Попробуйте снова.")
        return
    uid = int(message.text)
    await state.clear()
    supabase.table("users").update({"referral_banned": True}).eq("id", uid).execute()
    try: await bot.send_message(uid, "🚫 Доступ к реферальной программе заблокирован администратором.")
    except Exception: pass
    await message.answer(f"✅ Пользователь <code>{uid}</code> заблокирован в реф-программе.", reply_markup=get_admin_kb())


@dp.callback_query(F.data == "admin_ref_unban")
async def cb_admin_ref_unban(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    await state.set_state(AdminState.waiting_for_ref_unban_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_refs")]])
    await cq.message.edit_text("🔓 Отправьте ID пользователя, которому нужно <b>разблокировать реферальную программу</b>:", reply_markup=kb)


@dp.message(AdminState.waiting_for_ref_unban_id)
async def process_ref_unban(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if not message.text.isdigit():
        await message.answer("⚠️ ID должен состоять только из цифр. Попробуйте снова.")
        return
    uid = int(message.text)
    await state.clear()
    supabase.table("users").update({"referral_banned": False}).eq("id", uid).execute()
    try: await bot.send_message(uid, "🔓 Доступ к реферальной программе восстановлен администратором.")
    except Exception: pass
    await message.answer(f"✅ Пользователь <code>{uid}</code> разблокирован в реф-программе.", reply_markup=get_admin_kb())

# ── КОНЕЦ ДОБАВЛЕННОГО: Админ-секция рефералов ──────────────────────────────

# ── ЛИЧНЫЕ СООБЩЕНИЯ ────────────────────────────────────────────────────────

@dp.callback_query(F.data == "admin_msg_user")
async def cb_admin_msg_user(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    
    res = supabase.table("users").select("id, first_name, last_name").neq("id", ADMIN_ID).eq("status", "approved").limit(30).execute()
    users = res.data if res.data else []
    if not users:
        await cq.message.edit_text("Нет одобренных пользователей.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]]))
        return
        
    kb_buttons = []
    for u in users:
        name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
        if not name: name = str(u['id'])
        kb_buttons.append([InlineKeyboardButton(text=f"👤 {name}", callback_data=f"admin_msg_sel:{u['id']}")])
        
    kb_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    await cq.message.edit_text("✉️ <b>Кому отправить сообщение?</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

@dp.callback_query(F.data.startswith("admin_msg_sel:"))
async def cb_admin_msg_sel(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    target_id = int(cq.data.split(":")[1])
    await state.update_data(target_id=target_id)
    await state.set_state(AdminState.waiting_for_user_msg)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back")]])
    await cq.message.edit_text(f"✍️ Отправьте текст или фото (с подписью) для пользователя <code>{target_id}</code>:", reply_markup=kb)

@dp.message(AdminState.waiting_for_user_msg)
async def process_user_msg(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    data = await state.get_data()
    target_id = data["target_id"]
    await state.clear()
    
    try:
        if message.photo:
            await bot.send_photo(target_id, message.photo[-1].file_id, caption=message.caption or "")
        elif message.text:
            await bot.send_message(target_id, message.text)
        else:
            await message.answer("⚠️ Поддерживается только текст или фото.")
            return
        await message.answer(f"✅ Сообщение доставлено пользователю <code>{target_id}</code>.", reply_markup=get_admin_kb())
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}", reply_markup=get_admin_kb())

# ── РАССЫЛКА (С ФОТО И УДАЛЕНИЕМ) ───────────────────────────────────────────

@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    await state.set_state(AdminState.waiting_for_broadcast)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back")]])
    await cq.message.edit_text("📢 <b>Режим рассылки</b>\n\nОтправьте текст или фото (с подписью), которое получат ВСЕ пользователи:", reply_markup=kb)

@dp.message(AdminState.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    
    res = supabase.table("users").select("id").eq("status", "approved").execute()
    users = res.data if res.data else []
    if not users:
        await message.answer("В базе нет одобренных пользователей.", reply_markup=get_admin_kb())
        return
        
    status_msg = await message.answer("⏳ Начинаю рассылку...")
    batch_id = str(uuid.uuid4())
    success, failed = 0, 0
    
    for u in users:
        try:
            if message.photo:
                sent_msg = await bot.send_photo(u["id"], message.photo[-1].file_id, caption=message.caption or "")
            else:
                sent_msg = await bot.send_message(u["id"], message.text)
                
            # Сохраняем в историю для возможности удаления
            supabase.table("broadcast_history").insert({
                "batch_id": batch_id, "user_id": u["id"], "message_id": sent_msg.message_id
            }).execute()
            success += 1
        except Exception:
            failed += 1
            
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить эту рассылку у всех", callback_data=f"del_broadcast:{batch_id}")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="admin_back")]
    ])
    await status_msg.edit_text(f"✅ <b>Рассылка завершена!</b>\n\nУспешно: {success}\nЗаблокировали бота: {failed}", reply_markup=kb)

@dp.callback_query(F.data.startswith("del_broadcast:"))
async def cb_del_broadcast(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    batch_id = cq.data.split(":")[1]
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить везде", callback_data=f"confirm_del:{batch_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back")]
    ])
    await cq.message.edit_text("⚠️ Вы уверены, что хотите удалить эту рассылку у всех пользователей?", reply_markup=kb)

@dp.callback_query(F.data.startswith("confirm_del:"))
async def cb_confirm_del(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    batch_id = cq.data.split(":")[1]
    
    await cq.message.edit_text("⏳ Удаляю сообщения...")
    res = supabase.table("broadcast_history").select("user_id, message_id").eq("batch_id", batch_id).execute()
    records = res.data if res.data else []
    
    success, failed = 0, 0
    for r in records:
        try:
            await bot.delete_message(r["user_id"], r["message_id"])
            success += 1
        except Exception:
            failed += 1
            
    supabase.table("broadcast_history").delete().eq("batch_id", batch_id).execute()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="admin_back")]])
    await cq.message.edit_text(f"✅ <b>Удаление завершено!</b>\n\nУспешно удалено: {success}\nНе удалось (прошло >48ч): {failed}", reply_markup=kb)

# ── СТАРЫЕ ФУНКЦИИ ПОЛЬЗОВАТЕЛЕЙ (БЕЗ ИЗМЕНЕНИЙ) ────────────────────────────

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
    
    try:
        await bot.send_message(uid, "🔒 <b>Доступ заблокирован.</b>\nСвяжитесь с администратором для восстановления: @GaspardShop")
    except Exception as e:
        logger.warning(f"Could not send block notify: {e}")
    
    await cb_admin_user_card(cq)

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

@dp.callback_query(F.data.startswith("term_set:"))
async def cb_term_set(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await safe_answer(cq)
    
    parts = cq.data.split(":")
    target_id = int(parts[1])
    days = int(parts[2])
    target_type = parts[3]
    
    user_id = target_id
    if target_type != "user":
        req = fetch_one(supabase.table("access_requests").select("user_id").eq("id", target_id))
        user_id = req["user_id"] if req else None
        
    if not user_id:
        await cq.message.edit_text("Ошибка: пользователь не найден.")
        return

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
                    valid_until = (curr_dt + timedelta(days=days)).isoformat()
                    is_extending = True
                else:
                    valid_until = (now + timedelta(days=days)).isoformat()
            except ValueError:
                valid_until = (now + timedelta(days=days)).isoformat()
        else:
            valid_until = (now + timedelta(days=days)).isoformat()

    if valid_until:
        if is_extending:
            user_text = f"<i>Вам добавлено {days} дн. к текущему сроку.</i>\nНовая дата окончания: <b>{fmt_dt(valid_until)}</b>\n\nДля продления в будущем обращайтесь: @GaspardShop"
        else:
            user_text = f"✅ <b>Вам установлен срок доступа!</b>\n\nВаш доступ к боту «Глазок» активен до: <b>{fmt_dt(valid_until)}</b>.\n\nПо окончании срока вам потребуется продлить его у администратора (@GaspardShop)."
    else:
        user_text = "✅ <b>Вам выдан бессрочный доступ!</b>\n\nДобро пожаловать в «Глазок»."

    user_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Подключить Глазок", callback_data="user_connect")]
    ])

    if target_type == "user":
        # Сбрасываем флаги уведомлений!
        supabase.table("users").update({"status": "approved", "valid_until": valid_until, "expiry_notified": False, "expiry_warning_notified": False}).eq("id", target_id).execute()
        try: await bot.send_message(target_id, user_text, reply_markup=user_kb)
        except Exception as e: logger.warning(f"Could not send term notify to {target_id}: {e}")
    else:
        req = fetch_one(supabase.table("access_requests").select("user_id").eq("id", target_id))
        if req:
            supabase.table("users").update({"status": "approved", "valid_until": valid_until, "expiry_notified": False, "expiry_warning_notified": False}).eq("id", req["user_id"]).execute()
            supabase.table("access_requests").update({"status": "approved", "resolved_at": now_utc().isoformat()}).eq("id", target_id).execute()
            
            try: 
                await bot.send_message(req["user_id"], user_text, reply_markup=user_kb)
            except Exception as e: logger.warning(f"Could not send term notify to {req['user_id']}: {e}")

    # ДОБАВЛЕНО: начисление реферальной награды (только при одобрении/выдаче срока)
    if target_type == "user":
        _actual_uid = target_id
    else:
        _req = fetch_one(supabase.table("access_requests").select("user_id").eq("id", target_id))
        _actual_uid = _req["user_id"] if _req else None
    if _actual_uid:
        try:
            await process_referral_reward(_actual_uid)
        except Exception as e:
            logger.warning(f"process_referral_reward failed: {e}")

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
    
    user_text = f"✅ <b>Вам установлен срок доступа!</b>\n\nВаш доступ к боту «Глазок» активен до: <b>{fmt_dt(valid_until)}</b>.\n\nПо окончании срока вам потребуется продлить его у администратора (@GaspardShop)."
    
    user_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Подключить Глазок", callback_data="user_connect")]
    ])
    
    if target_type == "user":
        # Сбрасываем флаги уведомлений!
        supabase.table("users").update({"status": "approved", "valid_until": valid_until, "expiry_notified": False, "expiry_warning_notified": False}).eq("id", target_id).execute()
        try: await bot.send_message(target_id, user_text, reply_markup=user_kb)
        except Exception as e: logger.warning(f"Could not send term notify to {target_id}: {e}")
        
        await message.answer(f"✅ Срок установлен до: {fmt_dt(valid_until)}")
    else:
        req = fetch_one(supabase.table("access_requests").select("user_id").eq("id", target_id))
        if req:
            supabase.table("users").update({"status": "approved", "valid_until": valid_until, "expiry_notified": False, "expiry_warning_notified": False}).eq("id", req["user_id"]).execute()
            supabase.table("access_requests").update({"status": "approved", "resolved_at": now_utc().isoformat()}).eq("id", target_id).execute()
            
            try: 
                await bot.send_message(req["user_id"], user_text, reply_markup=user_kb)
            except Exception as e: logger.warning(f"Could not send term notify to {req['user_id']}: {e}")
            
        await message.answer(f"✅ Пользователь одобрен, срок до: {fmt_dt(valid_until)}")

    # ДОБАВЛЕНО: начисление реферальной награды (только при одобрении/выдаче срока)
    if target_type == "user":
        _actual_uid = target_id
    else:
        _req = fetch_one(supabase.table("access_requests").select("user_id").eq("id", target_id))
        _actual_uid = _req["user_id"] if _req else None
    if _actual_uid:
        try:
            await process_referral_reward(_actual_uid)
        except Exception as e:
            logger.warning(f"process_referral_reward failed: {e}")

# ── Стандартные команды ──────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Начать пользоваться", callback_data="start_usage")]
    ])

    caption = (
        "👋 Добро пожаловать в «Глазок»!\n"
        "👁️ Глазок — ваш помощник для сохранения доступной истории сообщений "
        "и отслеживания удалений.\n👨‍💻 Создатель: Khamoi\n\n"
        "Для начала нажмите кнопку ниже."
    )

    # ДОБАВЛЕНО: парсинг реф-диплинка + баннер
    banner = None
    if message.text and len(message.text.split()) > 1:
        payload = message.text.split(maxsplit=1)[1]
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload[4:])
                if referrer_id != message.from_user.id:
                    ref = fetch_one(supabase.table("users").select("first_name, status, referral_banned").eq("id", referrer_id))
                    if ref and ref.get("status") == "approved" and not ref.get("referral_banned"):
                        await state.update_data(ref_pending=referrer_id)
                        referrer_name = ref.get("first_name") or "пользователь"
                        banner = f"🎁 Вас пригласил {referrer_name}. После одобрения вы получите +3 дня."
            except ValueError:
                pass

    if banner:
        caption = f"{banner}\n\n{caption}"

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
async def cb_start_usage(cq: CallbackQuery, state: FSMContext):
    await safe_answer(cq)
    try:
        user = ensure_user(cq.from_user)
    except Exception as e:
        await cq.message.answer(f"⚠️ <b>Ошибка базы данных Supabase:</b>\n<code>{str(e)}</code>")
        return

    # ДОБАВЛЕНО: обработка реф-диплинка (отложенно, после ensure_user)
    fsm_data = await state.get_data()
    ref_pending = fsm_data.get("ref_pending")
    if ref_pending:
        try:
            ok, _ = try_register_referral(cq.from_user, ref_pending)
            if ok:
                await notify_admin_anomaly_if_needed(ref_pending)
        except Exception as e:
            logger.warning(f"referral processing failed: {e}")
        await state.update_data(ref_pending=None)

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

# ── ДОБАВЛЕНО: Новые команды ────────────────────────────────────────────────

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("⚠️ Использование: <code>/broadcast Текст сообщения</code>\nИли воспользуйтесь /admin для рассылки с фото.")
        return
    
    await message.answer("⏳ Начинаю рассылку...")
    res = supabase.table("users").select("id").eq("status", "approved").execute()
    users = res.data if res.data else []
    
    success, failed = 0, 0
    for u in users:
        try:
            await bot.send_message(u["id"], text)
            success += 1
        except Exception:
            failed += 1
    
    await message.answer(f"✅ <b>Рассылка завершена!</b>\n\nУспешно: {success}\nНе доставлено (заблокировали): {failed}")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    users_all = supabase.table("users").select("id", count="exact").execute().count or 0
    users_approved = supabase.table("users").select("id", count="exact").eq("status", "approved").execute().count or 0
    users_pending = supabase.table("users").select("id", count="exact").eq("status", "pending").execute().count or 0
    users_blocked = supabase.table("users").select("id", count="exact").eq("status", "blocked").execute().count or 0
    
    media_count = supabase.table("media_files").select("id", count="exact").execute().count or 0
    deleted_count = supabase.table("deletion_events").select("id", count="exact").execute().count or 0
    connections = supabase.table("business_connections").select("id", count="exact").eq("is_enabled", True).execute().count or 0
    
    text = (
        f"📊 <b>Статистика «Глазок»</b>\n\n"
        f"👥 <b>Пользователи:</b> {users_all} всего\n"
        f"🟢 Одобрено: {users_approved}\n"
        f"⏳ В ожидании: {users_pending}\n"
        f"🔴 Заблокировано: {users_blocked}\n\n"
        f"💼 Активных Business: {connections}\n"
        f"💬 Удалений зафиксировано: {deleted_count}\n"
        f"📸 Медиа сохранено: {media_count}"
    )
    await message.answer(text)

@dp.message(Command("find"))
async def cmd_find(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Использование: <code>/find 123456789</code> или <code>/find @username</code>")
        return
        
    query = args[1].strip()
    if query.isdigit():
        u = fetch_one(supabase.table("users").select("*").eq("id", int(query)))
    else:
        clean_uname = query.replace("@", "")
        u = fetch_one(supabase.table("users").select("*").eq("username", clean_uname))
        
    if not u:
        await message.answer("❌ Пользователь не найден в базе.")
        return
        
    name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
    uname = f"@{u['username']}" if u.get('username') else "—"
    valid = fmt_dt(u.get('valid_until')) if u.get('valid_until') else "Бессрочно"
    
    text = (
        f"👤 <b>Найден:</b> {name}\n"
        f"🆔 ID: <code>{u['id']}</code>\n"
        f"🔗 Username: {uname}\n"
        f"📌 Статус: {u['status']}\n"
        f"📅 Срок до: {valid}"
    )
    await message.answer(text)

@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    start = time.perf_counter()
    try:
        supabase.table("users").select("id").limit(1).execute()
        db_status = "🟢 Онлайн"
    except Exception:
        db_status = "🔴 Ошибка"
        
    end = time.perf_counter()
    ping_ms = round((end - start) * 1000)
    
    await message.answer(f"🏓 <b>Понг!</b>\nСервер: 🟢 Работает\nБаза данных: {db_status}\nЗадержка: <b>{ping_ms} мс</b>")

@dp.message(Command("status"))
async def cmd_user_status(message: types.Message):
    u = fetch_one(supabase.table("users").select("*").eq("id", message.from_user.id))
    if not u:
        await message.answer("❌ Вы еще не зарегистрированы. Нажмите /start")
        return
        
    status = u.get("status")
    if status == "approved":
        valid = fmt_dt(u.get("valid_until")) if u.get("valid_until") else "Бессрочно"
        await message.answer(f"✅ <b>Ваш доступ активен!</b>\n📅 Действует до: <b>{valid}</b>")
    elif status == "pending":
        await message.answer("⏳ <b>Ваша заявка на рассмотрении.</b>\nПожалуйста, ожидайте решения администратора.")
    elif status in ("blocked", "denied"):
        await message.answer("🔒 <b>Ваш доступ заблокирован или отклонен.</b>\nДля выяснения причин напишите в поддержку: /support")
    else:
        await message.answer("ℹ️ Статус вашего аккаунта неизвестен. Попробуйте /start")

@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(
        f"🆔 <b>Ваш Telegram ID:</b>\n<code>{message.from_user.id}</code>\n\n"
        "Скопируйте его и отправьте администратору, если у вас возникли вопросы."
    )

@dp.message(Command("support"))
async def cmd_support(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать @GaspardShop", url="https://t.me/GaspardShop")]
    ])
    await message.answer(
        "💬 <b>Служба поддержки</b>\n\n"
        "По всем вопросам (продление доступа, ошибки, предложения) "
        "обращайтесь к администратору по кнопке ниже:",
        reply_markup=kb
    )

# ── Aiogram: Business-события ────────────────────────────────────────────────

@dp.business_connection()
async def on_business_connection(conn: types.BusinessConnection):
    supabase.table("business_connections").upsert({
        "id": conn.id, "owner_id": conn.user.id,
        "is_enabled": conn.is_enabled, "updated_at": now_utc().isoformat(),
    }, on_conflict="id").execute()

    if conn.is_enabled:
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
