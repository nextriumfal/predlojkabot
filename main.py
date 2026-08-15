import os
import time
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import LinkPreviewOptions, InputMediaPhoto, InputMediaVideo
from aiohttp import web

# --- НАСТРОЙКИ ---
API_TOKEN = os.getenv('API_TOKEN')
PORT = int(os.getenv('PORT', 8080))

if not API_TOKEN:
    raise ValueError("ОШИБКА: Переменная окружения API_TOKEN не задана! Укажите её в настройках хостинга.")

ADMIN_IDS = [7541245548, 8470311411]
VIP_ADMIN_IDS = [7541245548, 8470311411]

CHANNEL_ID = -1003916335483
CHANNEL_URL = "https://t.me/Freakcrimea"
CHANNEL_NAME = "Фрики Крым"

SUGGEST_BOT = "@Freakcrimeabot"
DELETE_CONTACT = "@Triumfal"

logging.basicConfig(level=logging.INFO)

# --- БАЗА ДАННЫХ ---
conn = sqlite3.connect('suggestions.db', check_same_thread=False)
cursor = conn.cursor()

def init_db():
    tables = [
        '''CREATE TABLE IF NOT EXISTS posts 
           (id INTEGER PRIMARY KEY AUTOINCREMENT, 
            user_id INTEGER, 
            status TEXT, 
            text TEXT, 
            file_id TEXT, 
            media_type TEXT, 
            media_group_id TEXT, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        
        '''CREATE TABLE IF NOT EXISTS post_media 
           (id INTEGER PRIMARY KEY AUTOINCREMENT, 
            post_id INTEGER, 
            file_id TEXT, 
            media_type TEXT)''',
        
        '''CREATE TABLE IF NOT EXISTS users 
           (user_id INTEGER PRIMARY KEY, 
            username TEXT)''',
        
        '''CREATE TABLE IF NOT EXISTS banned_users 
           (user_id INTEGER PRIMARY KEY, 
            username TEXT)''',
        
        '''CREATE TABLE IF NOT EXISTS admin_messages 
           (admin_id INTEGER, 
            admin_msg_id INTEGER, 
            user_id INTEGER, 
            PRIMARY KEY (admin_id, admin_msg_id))''',
        
        '''CREATE TABLE IF NOT EXISTS tickets 
           (id INTEGER PRIMARY KEY AUTOINCREMENT, 
            user_id INTEGER, 
            post_id INTEGER, 
            status TEXT DEFAULT 'open', 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        
        '''CREATE TABLE IF NOT EXISTS ticket_messages 
           (id INTEGER PRIMARY KEY AUTOINCREMENT, 
            ticket_id INTEGER, 
            sender_type TEXT, 
            sender_id INTEGER, 
            sender_name TEXT, 
            text TEXT, 
            media_type TEXT, 
            file_id TEXT, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        
        '''CREATE TABLE IF NOT EXISTS scheduled_posts 
           (id INTEGER PRIMARY KEY AUTOINCREMENT, 
            post_id INTEGER, 
            publish_time INTEGER)'''
    ]
    for table in tables:
        cursor.execute(table)
    conn.commit()

init_db()

bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(
        parse_mode="HTML",
        link_preview=LinkPreviewOptions(is_disabled=True)
    )
)
dp = Dispatcher()

# Буфер для сборки альбомов (MediaGroup)
media_group_buffers = {}

# Хранилище активных прямодиалоговых чатов админов: admin_id -> ticket_id
active_admin_chats = {}

async def web_handler(request):
    return web.Response(text="Bot is running!")

# --- СОСТОЯНИЯ ---
class VipStates(StatesGroup):
    waiting_for_broadcast = State()

class AdminStates(StatesGroup):
    waiting_for_custom_time = State()

# --- КНОПКИ КЛАВИАТУР ---
def get_admin_kb(post_id, user_id, ticket_id=None):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Опубликовать", callback_data=f"pub_{post_id}_{user_id}")
    builder.button(text="⏱ Отложить", callback_data=f"schedmenu_{post_id}_{user_id}")
    builder.button(text="❌ Отклонить", callback_data=f"rej_{post_id}_{user_id}")
    if ticket_id:
        builder.button(text="💬 Чат / История", callback_data=f"open_ticket_{ticket_id}")
    else:
        builder.button(text="💬 Чат с автором", callback_data=f"chat_{post_id}_{user_id}")
    builder.button(text="🚫 Забанить автора", callback_data=f"ban_{post_id}_{user_id}")
    builder.adjust(2, 2, 1)
    return builder.as_markup()

def get_user_more_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Отправить еще один пост", callback_data="send_more")
    return builder.as_markup()

def get_admin_panel_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 Активные тикеты (Чаты)", callback_data="list_tickets")
    builder.button(text="📅 Отложенные посты", callback_data="list_scheduled")
    builder.button(text="📊 Детальная аналитика", callback_data="view_analytics")
    builder.button(text="📋 Список банов", callback_data="view_banlist")
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()

def get_active_chat_kb(ticket_id: int, post_id: int, user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="🚪 Выйти из чата", callback_data=f"exitchat_{ticket_id}")
    builder.button(text="✅ Опубликовать пост", callback_data=f"pub_{post_id}_{user_id}")
    builder.button(text="❌ Отклонить пост", callback_data=f"rej_{post_id}_{user_id}")
    builder.button(text="🏁 Завершить ТИКЕТ", callback_data=f"close_ticket_{ticket_id}")
    builder.adjust(1, 2, 1)
    return builder.as_markup()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БД ---
def is_banned(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

def register_user(user: types.User):
    cursor.execute("INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)",
                   (user.id, user.username or "None"))
    conn.commit()

def save_admin_msg_mapping(admin_id: int, admin_msg_id: int, user_id: int):
    cursor.execute("INSERT OR REPLACE INTO admin_messages (admin_id, admin_msg_id, user_id) VALUES (?, ?, ?)",
                   (admin_id, admin_msg_id, user_id))
    conn.commit()

def log_ticket_message(ticket_id: int, sender_type: str, sender_id: int, sender_name: str, text: str, media_type: str, file_id: str):
    cursor.execute(
        "INSERT INTO ticket_messages (ticket_id, sender_type, sender_id, sender_name, text, media_type, file_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, sender_type, sender_id, sender_name, text, media_type, file_id)
    )
    conn.commit()

# --- ОТОБРАЖЕНИЕ АДМИН-ПАНЕЛИ ---
async def show_admin_panel(user_id: int, message: types.Message = None, callback: types.CallbackQuery = None):
    if user_id not in ADMIN_IDS and user_id not in VIP_ADMIN_IDS:
        if callback:
            return await callback.answer("🔒 У вас нет доступа к этой команде.", show_alert=True)
        elif message:
            return await message.answer("🔒 У вас нет доступа к этой команде.")

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE status = 'pending'")
    pending_posts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE status = 'scheduled'")
    scheduled_posts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE status = 'published'")
    pub_posts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE status = 'rejected'")
    rej_posts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM banned_users")
    banned_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM tickets WHERE status = 'open'")
    open_tickets = cursor.fetchone()[0]

    stats_text = (
        f"📊 <b>Панель администратора предложки</b>\n\n"
        f"👥 Всего пользователей в БД: {total_users}\n"
        f"📩 Открытых тикетов (чатов): <b>{open_tickets}</b>\n"
        f"⏳ На модерации: {pending_posts}\n"
        f"📅 На таймере (отложено): <b>{scheduled_posts}</b>\n"
        f"✅ Опубликовано всего: {pub_posts}\n"
        f"❌ Отклонено всего: {rej_posts}\n"
        f"🚫 В черном списке: {banned_count}\n\n"
        f"📖 <b>Мини-гайды и быстрые команды:</b>\n"
        f"• <code>/chat 12</code> — войти в чат с Тикетом #12\n"
        f"• <code>/pub</code> — опубликовать пост активного тикета\n"
        f"• <code>/rej</code> — отклонить пост активного тикета\n"
        f"• <code>/close</code> — завершить текущий тикет\n"
        f"• <code>/exit</code> — выйти из режима прямого чата\n\n"
        f"💡 <i>В режиме чата всё, что вы пишите боту, отправляется пользователю анонимно!</i>"
    )

    if callback:
        await callback.message.edit_text(stats_text, reply_markup=get_admin_panel_kb())
        await callback.answer()
    elif message:
        await message.answer(stats_text, reply_markup=get_admin_panel_kb())

# --- КОМАНДА /start ---
@dp.message(Command("start"))
async def start(message: types.Message):
    if is_banned(message.from_user.id):
        return await message.answer("⚠️ Вы заблокированы в этой предложке.")

    register_user(message.from_user)
    await message.answer(
        "👋 Привет! Присылай сюда свой пост (текст, фото или видео).\n\n"
        "⚠️ <b>Важно:</b> По умолчанию все посты анонимные. "
        "Если хочешь не анонимный пост — укажи свой юзернейм в тексте.\n\n"
        "Ты получишь уведомление, когда твой пост пройдет проверку.\n"
        "Если вдруг вы получили уведомление, но не увидели пост, напишите людям из отдела связи Фрики Крыма."
    )

# --- КОМАНДЫ АДМИНА ---
@dp.message(Command("admin"))
@dp.message(Command("stats"))
async def admin_panel(message: types.Message):
    await show_admin_panel(message.from_user.id, message=message)

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin_callback(callback: types.CallbackQuery):
    await show_admin_panel(callback.from_user.id, callback=callback)

# --- РЕЖИМ ЧАТА И КОМАНДЫ УПРАВЛЕНИЯ ---
@dp.message(Command("chat"))
async def cmd_chat(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id not in VIP_ADMIN_IDS:
        return

    args = message.text.split()
    if len(args) < 2:
        return await message.answer("⚠️ Использование: <code>/chat 12</code> (где 12 — номер тикета)")

    try:
        ticket_id = int(args[1])
        cursor.execute("SELECT id, user_id, post_id, status FROM tickets WHERE id = ?", (ticket_id,))
        row = cursor.fetchone()

        if not row:
            return await message.answer("❌ Тикет не найден.")

        tid, uid, pid, t_status = row
        if t_status != 'open':
            return await message.answer(f"⚠️ Тикет #{tid} уже закрыт!")

        active_admin_chats[message.from_user.id] = tid

        await message.answer(
            f"💬 <b>Вы вошли в режим прямого чата с Тикетом #{tid}!</b>\n\n"
            f"Все ваши обычные сообщения (текст, фото) теперь напрямую уходят автору анонимно.\n"
            f"Быстрые команды: <code>/pub</code>, <code>/rej</code>, <code>/close</code>, <code>/exit</code>",
            reply_markup=get_active_chat_kb(tid, pid, uid)
        )
    except ValueError:
        await message.answer("⚠️ Номер тикета должен состоять из цифр.")

@dp.message(Command("exit"))
async def cmd_exit_chat(message: types.Message):
    admin_id = message.from_user.id
    if admin_id in active_admin_chats:
        tid = active_admin_chats.pop(admin_id)
        await message.answer(f"🚪 Вы вышли из чата Тикета #{tid}.")
    else:
        await message.answer("ℹ️ Вы не находитесь в режиме прямого чата.")

@dp.callback_query(F.data.startswith("exitchat_"))
async def callback_exit_chat(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    if admin_id in active_admin_chats:
        tid = active_admin_chats.pop(admin_id)
        await callback.message.answer(f"🚪 Вы вышли из чата Тикета #{tid}.")

    await show_admin_panel(admin_id, callback=callback)

@dp.message(Command("pub"))
@dp.message(Command("publish"))
async def cmd_pub_active_ticket(message: types.Message):
    admin_id = message.from_user.id
    if admin_id not in ADMIN_IDS and admin_id not in VIP_ADMIN_IDS:
        return

    if admin_id not in active_admin_chats:
        return await message.answer("⚠️ Вы не в режиме чата! Войдите в тикет или используйте кнопки под постом.")

    ticket_id = active_admin_chats[admin_id]
    cursor.execute("SELECT post_id, user_id FROM tickets WHERE id = ?", (ticket_id,))
    row = cursor.fetchone()

    if not row:
        return await message.answer("❌ Тикет не найден.")

    post_id, user_id = row
    await publish_post_by_id(post_id, user_id, message=message)

@dp.message(Command("rej"))
@dp.message(Command("reject"))
async def cmd_rej_active_ticket(message: types.Message):
    admin_id = message.from_user.id
    if admin_id not in ADMIN_IDS and admin_id not in VIP_ADMIN_IDS:
        return

    if admin_id not in active_admin_chats:
        return await message.answer("⚠️ Вы не в режиме чата!")

    ticket_id = active_admin_chats[admin_id]
    cursor.execute("SELECT post_id, user_id FROM tickets WHERE id = ?", (ticket_id,))
    row = cursor.fetchone()

    if not row:
        return await message.answer("❌ Тикет не найден.")

    post_id, user_id = row
    await reject_post_by_id(post_id, user_id, message=message)

@dp.message(Command("close"))
async def cmd_close_active_ticket(message: types.Message):
    admin_id = message.from_user.id
    if admin_id not in ADMIN_IDS and admin_id not in VIP_ADMIN_IDS:
        return

    if admin_id not in active_admin_chats:
        return await message.answer("⚠️ Вы не в режиме чата!")

    ticket_id = active_admin_chats[admin_id]
    await close_ticket_by_id(ticket_id, message=message)

# --- СПИСОК ОТЛОЖЕННЫХ ПОСТОВ ---
@dp.callback_query(F.data == "list_scheduled")
async def list_scheduled_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    cursor.execute('''SELECT s.id, s.post_id, s.publish_time, p.text, p.media_type 
                      FROM scheduled_posts s 
                      JOIN posts p ON s.post_id = p.id 
                      WHERE p.status = 'scheduled' 
                      ORDER BY s.publish_time ASC''')
    sched_list = cursor.fetchall()

    builder = InlineKeyboardBuilder()

    if not sched_list:
        builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
        await callback.message.edit_text("📅 <b>Запланированных постов нет!</b>", reply_markup=builder.as_markup())
        return await callback.answer()

    text = "📅 <b>Список отложенных постов:</b>\n\n"

    for sid, pid, ptime, ptext, mtype in sched_list:
        time_str = datetime.fromtimestamp(ptime).strftime("%d.%m %H:%M")
        preview = (ptext[:25] + "...") if ptext else f"[{mtype.upper()}]"
        text += f"• ⏱ <b>{time_str}</b> | Пост #{pid}\n   └ <i>{preview}</i>\n\n"
        builder.button(text=f"⏱ {time_str} (Пост #{pid})", callback_data=f"viewsched_{sid}")

    builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("viewsched_"))
async def view_scheduled_item_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    sched_id = int(callback.data.split("_")[1])

    cursor.execute('''SELECT s.id, s.post_id, s.publish_time, p.user_id, p.text, p.media_type 
                      FROM scheduled_posts s 
                      JOIN posts p ON s.post_id = p.id 
                      WHERE s.id = ?''', (sched_id,))
    item = cursor.fetchone()

    if not item:
        return await callback.answer("⚠️ Запись не найдена.", show_alert=True)

    sid, pid, ptime, uid, ptext, mtype = item
    time_str = datetime.fromtimestamp(ptime).strftime("%d.%m.%Y в %H:%M")

    text = (
        f"📅 <b>Управление отложенным постом #{pid}</b>\n\n"
        f"⏱ <b>Время публикации:</b> {time_str}\n"
        f"👤 <b>ID автора:</b> <code>{uid}</code>\n"
        f"📦 <b>Тип:</b> {mtype}\n\n"
        f"📄 <b>Текст поста:</b>\n<i>{ptext or 'Без текста'}</i>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Опубликовать сейчас", callback_data=f"pubnow_{sid}")
    builder.button(text="❌ Отменить публикацию", callback_data=f"cancelsched_{sid}")
    builder.button(text="🔙 К списку отложенных", callback_data="list_scheduled")
    builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("pubnow_"))
async def pub_now_scheduled_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    sched_id = int(callback.data.split("_")[1])

    cursor.execute('''SELECT s.post_id, p.user_id, p.text, p.file_id, p.media_type 
                      FROM scheduled_posts s 
                      JOIN posts p ON s.post_id = p.id 
                      WHERE s.id = ?''', (sched_id,))
    item = cursor.fetchone()

    if not item:
        return await callback.answer("⚠️ Запись не найдена.", show_alert=True)

    post_id, user_id, original_text, file_id, media_type = item

    footer = (
        f"предложить пост : {SUGGEST_BOT}\n\n"
        f"удалить / узнать пост {DELETE_CONTACT}\n\n"
        f"📢 <a href='{CHANNEL_URL}'>{CHANNEL_NAME}</a>"
    )

    final_text = f"{original_text}\n\n{footer}" if original_text else footer

    cursor.execute("SELECT file_id, media_type FROM post_media WHERE post_id = ?", (post_id,))
    all_media = cursor.fetchall()

    try:
        if len(all_media) > 1:
            media_group = []
            for idx, (m_file_id, m_type) in enumerate(all_media):
                cap = final_text if idx == 0 else ""
                if m_type == "photo":
                    media_group.append(InputMediaPhoto(media=m_file_id, caption=cap))
                elif m_type == "video":
                    media_group.append(InputMediaVideo(media=m_file_id, caption=cap))
            await bot.send_media_group(CHANNEL_ID, media=media_group)
        else:
            if media_type == "photo":
                await bot.send_photo(CHANNEL_ID, file_id, caption=final_text)
            elif media_type == "video":
                await bot.send_video(CHANNEL_ID, file_id, caption=final_text)
            else:
                await bot.send_message(CHANNEL_ID, final_text)

        cursor.execute("UPDATE posts SET status = 'published' WHERE id = ?", (post_id,))
        cursor.execute("DELETE FROM scheduled_posts WHERE id = ?", (sched_id,))
        conn.commit()

        try:
            await bot.send_message(
                user_id,
                "✅ Твой пост опубликован в канале!",
                reply_markup=get_user_more_kb()
            )
        except Exception:
            pass

        await callback.message.edit_text(f"🚀 Пост #{post_id} опубликован в канале!")
        await callback.answer("🚀 Опубликовано!")

    except Exception as e:
        await callback.answer(f"Ошибка при публикации: {e}", show_alert=True)

@dp.callback_query(F.data.startswith("cancelsched_"))
async def cancel_scheduled_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    sched_id = int(callback.data.split("_")[1])

    cursor.execute("SELECT post_id FROM scheduled_posts WHERE id = ?", (sched_id,))
    row = cursor.fetchone()
    if row:
        post_id = row[0]
        cursor.execute("UPDATE posts SET status = 'rejected' WHERE id = ?", (post_id,))
        cursor.execute("DELETE FROM scheduled_posts WHERE id = ?", (sched_id,))
        conn.commit()

    await callback.message.edit_text("❌ Запланированная публикация отменена.")
    await callback.answer("❌ Публикация отменена!")

# --- ДЕТАЛЬНАЯ АНАЛИТИКА ---
@dp.callback_query(F.data == "view_analytics")
async def view_analytics_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    cursor.execute("SELECT COUNT(*) FROM posts WHERE date(created_at) = date('now')")
    today_total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE date(created_at) = date('now') AND status = 'published'")
    today_pub = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE date(created_at) = date('now') AND status = 'rejected'")
    today_rej = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE created_at >= datetime('now', '-7 days')")
    week_total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE created_at >= datetime('now', '-7 days') AND status = 'published'")
    week_pub = cursor.fetchone()[0]

    cursor.execute("SELECT strftime('%H', created_at) as hr, COUNT(*) as c FROM posts GROUP BY hr ORDER BY c DESC LIMIT 1")
    peak_row = cursor.fetchone()
    peak_hour = f"{peak_row[0]}:00 - {int(peak_row[0])+1:02d}:00" if peak_row and peak_row[0] else "Нет данных"

    cursor.execute("SELECT COUNT(*) FROM tickets WHERE status = 'closed'")
    closed_tickets = cursor.fetchone()[0]

    analytics_text = (
        f"📈 <b>Расширенная аналитика модерации</b>\n\n"
        f"📅 <b>За сегодня:</b>\n"
        f"  • Получено постов: <b>{today_total}</b>\n"
        f"  • Опубликовано: <b>{today_pub}</b>\n"
        f"  • Отклонено: <b>{today_rej}</b>\n\n"
        f"🗓 <b>За последние 7 дней:</b>\n"
        f"  • Всего заявок: <b>{week_total}</b>\n"
        f"  • Выложено в канал: <b>{week_pub}</b>\n\n"
        f"🔥 <b>Пик активности авторов:</b> {peak_hour}\n"
        f"🏁 <b>Всего обработано тикетов:</b> {closed_tickets}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    await callback.message.edit_text(analytics_text, reply_markup=builder.as_markup())
    await callback.answer()

# --- СПИСОК ТИКЕТОВ ---
@dp.callback_query(F.data == "list_tickets")
async def list_tickets_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    cursor.execute('''SELECT t.id, t.user_id, t.post_id, u.username, p.text 
                      FROM tickets t 
                      LEFT JOIN users u ON t.user_id = u.user_id 
                      LEFT JOIN posts p ON t.post_id = p.id 
                      WHERE t.status = 'open' 
                      ORDER BY t.id DESC LIMIT 20''')
    tickets = cursor.fetchall()

    builder = InlineKeyboardBuilder()

    if not tickets:
        builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
        await callback.message.edit_text("📥 <b>Открытых тикетов нет!</b>", reply_markup=builder.as_markup())
        return await callback.answer()

    text = "💬 <b>Список активных диалогов (Тикетов):</b>\n\n"

    for tid, uid, pid, uname, ptext in tickets:
        user_disp = f"@{uname}" if uname and uname != "None" else f"ID {uid}"
        preview = (ptext[:25] + "...") if ptext else "Медиафайл / Альбом"
        text += f"• <b>Тикет #{tid}</b> | {user_disp} (Заявка #{pid})\n   └ <i>{preview}</i>\n\n"
        builder.button(text=f"💬 {user_disp} (Тикет #{tid})", callback_data=f"open_ticket_{tid}")

    builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

# --- ПРОСМОТР И УПРАВЛЕНИЕ ТИКЕТОМ ---
@dp.callback_query(F.data.startswith("open_ticket_"))
async def open_ticket_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    ticket_id = int(callback.data.split("_")[2])
    await open_ticket_callback_by_id(callback, ticket_id)

async def open_ticket_callback_by_id(callback: types.CallbackQuery, ticket_id: int):
    cursor.execute('''SELECT t.id, t.user_id, t.post_id, t.status, u.username, p.text, p.status 
                      FROM tickets t 
                      LEFT JOIN users u ON t.user_id = u.user_id 
                      LEFT JOIN posts p ON t.post_id = p.id 
                      WHERE t.id = ?''', (ticket_id,))
    ticket = cursor.fetchone()

    if not ticket:
        return await callback.answer("⚠️ Тикет не найден.", show_alert=True)

    tid, uid, pid, t_status, uname, ptext, p_status = ticket
    user_mention = f"@{uname}" if uname and uname != "None" else f"ID <code>{uid}</code>"

    cursor.execute('''SELECT sender_type, text, media_type, created_at 
                      FROM ticket_messages 
                      WHERE ticket_id = ? 
                      ORDER BY id ASC LIMIT 15''', (tid,))
    history = cursor.fetchall()

    history_str = ""
    if history:
        history_str = "\n\n📜 <b>История переписки:</b>\n"
        for stype, mtext, mtype, ctime in history:
            sender_label = "👤 [Юзер]" if stype == "user" else "🛡 [Модератор]"
            content = mtext if mtext else f"<i>[{mtype.upper()}]</i>"
            history_str += f"• {sender_label}: {content}\n"
    else:
        history_str = "\n\n📜 <i>История сообщений пуста.</i>"

    text = (
        f"💬 <b>Управление Тикетом #{tid}</b>\n\n"
        f"👤 <b>Пользователь:</b> {user_mention}\n"
        f"🆔 <b>ID пользователя:</b> <code>{uid}</code>\n"
        f"📝 <b>Заявка:</b> #{pid} (Статус поста: {p_status})\n"
        f"📌 <b>Статус тикета:</b> {'🟢 Открыт' if t_status == 'open' else '🔴 Завершен'}"
        f"{history_str}"
    )

    builder = InlineKeyboardBuilder()
    if t_status == 'open':
        builder.button(text="💬 Войти в режим чата", callback_data=f"enter_chat_{tid}")
        builder.button(text="🏁 Завершить ТИКЕТ", callback_data=f"close_ticket_{tid}")

    builder.button(text="🔙 К списку тикетов", callback_data="list_tickets")
    builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("enter_chat_"))
async def enter_chat_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    ticket_id = int(callback.data.split("_")[2])

    cursor.execute("SELECT user_id, post_id FROM tickets WHERE id = ?", (ticket_id,))
    row = cursor.fetchone()

    if not row:
        return await callback.answer("⚠️ Тикет не найден.", show_alert=True)

    user_id, post_id = row
    active_admin_chats[callback.from_user.id] = ticket_id

    await callback.message.edit_text(
        f"💬 <b>Вы вошли в режим прямого чата с Тикетом #{ticket_id}!</b>\n\n"
        f"Все ваши обычные сообщения теперь уходят пользователю анонимно.\n"
        f"Команды: <code>/pub</code>, <code>/rej</code>, <code>/close</code>, <code>/exit</code>",
        reply_markup=get_active_chat_kb(ticket_id, post_id, user_id)
    )
    await callback.answer("💬 Вход в чат выполнен!")

# --- ОБРАБОТКА ВСЕХ СООБЩЕНИЙ МОДЕРАТОРА В РЕЖИМЕ ЧАТА ---
@dp.message(
    F.chat.type == "private",
    lambda m: (m.from_user.id in ADMIN_IDS or m.from_user.id in VIP_ADMIN_IDS) and m.from_user.id in active_admin_chats
)
async def handle_admin_chat_messages(message: types.Message):
    admin_id = message.from_user.id

    if message.text and message.text.startswith("/"):
        return

    ticket_id = active_admin_chats[admin_id]

    cursor.execute("SELECT user_id, status FROM tickets WHERE id = ?", (ticket_id,))
    t_row = cursor.fetchone()

    if not t_row or t_row[1] != 'open':
        active_admin_chats.pop(admin_id, None)
        return await message.reply("⚠️ Этот тикет уже закрыт. Вы вышли из режима чата.")

    target_user_id = t_row[0]
    admin_mention = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name

    text_content = message.text or message.caption or ""
    media_type = "text"
    file_id = None

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id

    log_ticket_message(ticket_id, "admin", admin_id, admin_mention, text_content, media_type, file_id)

    try:
        await bot.send_message(target_user_id, "💬 <b>Сообщение от администрации:</b>")
        await bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
        await message.reply(
            f"✅ Сообщение доставлено пользователю! (Тикет #{ticket_id})\n"
            f"<i>Вы все еще в чате. Продолжайте писать или нажмите «🚪 Выйти из чата».</i>"
        )
    except Exception as e:
        await message.reply(f"❌ Ошибка отправки пользователю: {e}")

    admin_info = f"💬 <b>[Модератор в Тикет #{ticket_id} (Юзер <code>{target_user_id}</code>)]:</b>"
    for aid in list(set(ADMIN_IDS + VIP_ADMIN_IDS)):
        if aid != admin_id:
            try:
                head_msg = await bot.send_message(aid, admin_info)
                copy_msg = await bot.copy_message(
                    chat_id=aid,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                save_admin_msg_mapping(aid, head_msg.message_id, target_user_id)
                save_admin_msg_mapping(aid, copy_msg.message_id, target_user_id)
            except Exception:
                pass

# --- ЗАКРЫТИЕ ТИКЕТА ---
async def close_ticket_by_id(ticket_id: int, callback: types.CallbackQuery = None, message: types.Message = None):
    cursor.execute("SELECT user_id, post_id FROM tickets WHERE id = ?", (ticket_id,))
    ticket = cursor.fetchone()

    if not ticket:
        if callback:
            await callback.answer("⚠️ Тикет не найден.", show_alert=True)
        elif message:
            await message.reply("⚠️ Тикет не найден.")
        return

    user_id, post_id = ticket

    cursor.execute("UPDATE tickets SET status = 'closed' WHERE id = ?", (ticket_id,))
    conn.commit()

    for aid, tid in list(active_admin_chats.items()):
        if tid == ticket_id:
            active_admin_chats.pop(aid, None)

    try:
        await bot.send_message(
            user_id,
            f"🏁 <b>Ваш Тикет #{ticket_id} был завершен модератором.</b>\n\n"
            f"Если у вас возникнут новые вопросы или вы захотите прислать еще один пост, просто отправьте сообщение боту!",
            reply_markup=get_user_more_kb()
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить пользователя о закрытии тикета: {e}")

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 К списку тикетов", callback_data="list_tickets")
    builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    builder.adjust(1)

    text_msg = (
        f"🏁 <b>Тикет #{ticket_id} успешно завершен!</b>\n\n"
        f"Пользователь может продолжать пользоваться предложкой и отправлять новые посты."
    )

    if callback:
        await callback.message.edit_text(text_msg, reply_markup=builder.as_markup())
        await callback.answer("🏁 Тикет завершен!")
    elif message:
        await message.reply(text_msg, reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("close_ticket_"))
async def close_ticket_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    ticket_id = int(callback.data.split("_")[2])
    await close_ticket_by_id(ticket_id, callback=callback)

# --- МЕНЮ ОТЛОЖЕННОЙ ПУБЛИКАЦИИ ---
@dp.callback_query(F.data.startswith("schedmenu_"))
async def sched_menu_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    data = callback.data.split("_")
    post_id, user_id = data[1], data[2]

    builder = InlineKeyboardBuilder()
    builder.button(text="⌨️ Указать свое время (ЧЧ:ММ)", callback_data=f"customsched_{post_id}_{user_id}")
    builder.button(text="⏱ Через 1 час", callback_data=f"dosched_{post_id}_{user_id}_3600")
    builder.button(text="⏱ Через 3 часа", callback_data=f"dosched_{post_id}_{user_id}_10800")
    builder.button(text="⏱ Через 6 часов", callback_data=f"dosched_{post_id}_{user_id}_21600")
    builder.button(text="⏱ Через 12 часов", callback_data=f"dosched_{post_id}_{user_id}_43200")
    builder.button(text="⏱ Через 24 часа", callback_data=f"dosched_{post_id}_{user_id}_86400")
    builder.button(text="🔙 Назад", callback_data=f"backpost_{post_id}_{user_id}")
    builder.adjust(1, 2, 2, 1, 1)

    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("backpost_"))
async def back_post_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    data = callback.data.split("_")
    post_id, user_id = data[1], data[2]

    cursor.execute("SELECT id FROM tickets WHERE user_id = ? AND post_id = ? AND status = 'open'", (user_id, post_id))
    t_row = cursor.fetchone()
    ticket_id = t_row[0] if t_row else None

    await callback.message.edit_reply_markup(reply_markup=get_admin_kb(post_id, user_id, ticket_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("customsched_"))
async def custom_sched_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    data = callback.data.split("_")
    post_id, user_id = int(data[1]), int(data[2])

    await state.set_state(AdminStates.waiting_for_custom_time)
    await state.update_data(post_id=post_id, user_id=user_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"backpost_{post_id}_{user_id}")

    await callback.message.edit_text(
        "⌨️ <b>Введите точное время публикации в формате ЧЧ:ММ</b>\n"
        "Например: <code>06:44</code> или <code>18:30</code>",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.message(StateFilter(AdminStates.waiting_for_custom_time))
async def process_custom_time_input(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id not in VIP_ADMIN_IDS:
        return

    data = await state.get_data()
    post_id = data.get("post_id")

    time_raw = message.text.strip() if message.text else ""
    try:
        parsed_time = datetime.strptime(time_raw, "%H:%M")
        now = datetime.now()
        target_dt = now.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)

        if target_dt <= now:
            target_dt += timedelta(days=1)

        publish_time = int(target_dt.timestamp())
        formatted_time = target_dt.strftime("%H:%M")

        cursor.execute("SELECT status FROM posts WHERE id = ?", (post_id,))
        res = cursor.fetchone()
        if not res or res[0] != "pending":
            await state.clear()
            return await message.reply("⚠️ Пост уже обработан или не найден.")

        cursor.execute("UPDATE posts SET status = 'scheduled' WHERE id = ?", (post_id,))
        cursor.execute("INSERT INTO scheduled_posts (post_id, publish_time) VALUES (?, ?)", (post_id, publish_time))
        conn.commit()

        await state.clear()
        await message.reply(f"⏰ Пост будет опубликован в {formatted_time}")

    except ValueError:
        await message.reply("⚠️ Неверный формат времени. Пожалуйста, введите время в формате <b>ЧЧ:ММ</b> (например, <code>06:44</code>):")

@dp.callback_query(F.data.startswith("dosched_"))
async def do_sched_callback(callback: types.CallbackQuery):
    data = callback.data.split("_")
    post_id, user_id, seconds = int(data[1]), int(data[2]), int(data[3])

    cursor.execute("SELECT status FROM posts WHERE id = ?", (post_id,))
    res = cursor.fetchone()
    if not res or res[0] != "pending":
        return await callback.answer("⚠️ Пост уже обработан или не найден.", show_alert=True)

    publish_time = int(time.time()) + seconds
    cursor.execute("UPDATE posts SET status = 'scheduled' WHERE id = ?", (post_id,))
    cursor.execute("INSERT INTO scheduled_posts (post_id, publish_time) VALUES (?, ?)", (post_id, publish_time))
    conn.commit()

    time_str = time.strftime("%H:%M", time.localtime(publish_time))

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.reply(f"⏰ Пост будет опубликован в {time_str}")
    await callback.answer(f"⏰ Пост будет опубликован в {time_str}")

# --- ФОНОВЫЙ ПЛАНИРОВЩИК ---
async def scheduled_publisher_loop():
    while True:
        try:
            now = int(time.time())
            cursor.execute('''SELECT s.id, s.post_id, p.user_id, p.text, p.file_id, p.media_type 
                              FROM scheduled_posts s 
                              JOIN posts p ON s.post_id = p.id 
                              WHERE s.publish_time <= ? AND p.status = 'scheduled' ''', (now,))
            due_posts = cursor.fetchall()

            for sched_id, post_id, user_id, original_text, file_id, media_type in due_posts:
                footer = (
                    f"предложить пост : {SUGGEST_BOT}\n\n"
                    f"удалить / узнать пост {DELETE_CONTACT}\n\n"
                    f"📢 <a href='{CHANNEL_URL}'>{CHANNEL_NAME}</a>"
                )
                final_text = f"{original_text}\n\n{footer}" if original_text else footer

                cursor.execute("SELECT file_id, media_type FROM post_media WHERE post_id = ?", (post_id,))
                all_media = cursor.fetchall()

                try:
                    if len(all_media) > 1:
                        media_group = []
                        for idx, (m_file_id, m_type) in enumerate(all_media):
                            cap = final_text if idx == 0 else ""
                            if m_type == "photo":
                                media_group.append(InputMediaPhoto(media=m_file_id, caption=cap))
                            elif m_type == "video":
                                media_group.append(InputMediaVideo(media=m_file_id, caption=cap))
                        await bot.send_media_group(CHANNEL_ID, media=media_group)
                    else:
                        if media_type == "photo":
                            await bot.send_photo(CHANNEL_ID, file_id, caption=final_text)
                        elif media_type == "video":
                            await bot.send_video(CHANNEL_ID, file_id, caption=final_text)
                        else:
                            await bot.send_message(CHANNEL_ID, final_text)

                    cursor.execute("UPDATE posts SET status = 'published' WHERE id = ?", (post_id,))
                    cursor.execute("DELETE FROM scheduled_posts WHERE id = ?", (sched_id,))
                    conn.commit()

                    try:
                        await bot.send_message(
                            user_id,
                            "✅ Твой запланированный пост опубликован в канале!",
                            reply_markup=get_user_more_kb()
                        )
                    except Exception:
                        pass

                except Exception as e:
                    logging.error(f"Ошибка отложенной публикации поста #{post_id}: {e}")

        except Exception as e:
            logging.error(f"Ошибка в scheduled_publisher_loop: {e}")

        await asyncio.sleep(30)

# --- ОБРАБОТКА АЛЬБОМОВ ---
async def process_media_group_delayed(mg_id: str):
    await asyncio.sleep(1.5)
    if mg_id not in media_group_buffers:
        return

    data = media_group_buffers.pop(mg_id)
    messages = data['messages']
    first_msg = messages[0]
    user = first_msg.from_user

    text_content = ""
    for m in messages:
        if m.caption:
            text_content = m.caption
            break

    cursor.execute("SELECT id FROM tickets WHERE user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (user.id,))
    active_ticket = cursor.fetchone()

    if active_ticket:
        ticket_id = active_ticket[0]
        log_ticket_message(ticket_id, "user", user.id, user.full_name, text_content or "[Альбом фотографий]", "album", None)

        all_admins = list(set(ADMIN_IDS + VIP_ADMIN_IDS))
        for admin_id in all_admins:
            try:
                media_group = []
                for idx, m in enumerate(messages):
                    cap = f"💬 <b>[Тикет #{ticket_id} | Пользователь {user.full_name}]:</b>\n{text_content}" if idx == 0 else ""
                    if m.photo:
                        media_group.append(InputMediaPhoto(media=m.photo[-1].file_id, caption=cap))
                    elif m.video:
                        media_group.append(InputMediaVideo(media=m.video.file_id, caption=cap))

                sent_msgs = await bot.send_media_group(chat_id=admin_id, media=media_group)
                for sm in sent_msgs:
                    save_admin_msg_mapping(admin_id, sm.message_id, user.id)
            except Exception as e:
                logging.error(f"Не удалось переслать альбом админу {admin_id}: {e}")

        await first_msg.answer("💬 Ваше сообщение с альбомом добавлено в текущий диалог!")
        return

    cursor.execute(
        "INSERT INTO posts (user_id, status, text, file_id, media_type, media_group_id) VALUES (?, ?, ?, ?, ?, ?)",
        (user.id, "pending", text_content, messages[0].photo[-1].file_id if messages[0].photo else messages[0].video.file_id, "album", mg_id)
    )
    post_id = cursor.lastrowid

    for m in messages:
        m_type = "photo" if m.photo else "video"
        f_id = m.photo[-1].file_id if m.photo else m.video.file_id
        cursor.execute("INSERT INTO post_media (post_id, file_id, media_type) VALUES (?, ?, ?)", (post_id, f_id, m_type))

    cursor.execute("INSERT INTO tickets (user_id, post_id, status) VALUES (?, ?, 'open')", (user.id, post_id))
    ticket_id = cursor.lastrowid
    conn.commit()

    log_ticket_message(ticket_id, "user", user.id, user.full_name, text_content or "[Альбом фотографий]", "album", None)

    await first_msg.answer(f"🚀 Альбом отправлен на модерацию! (Тикет #{ticket_id})")

    user_link = f"<a href='tg://user?id={user.id}'>{user.full_name}</a>"
    username = f" (@{user.username})" if user.username else " (нет юзернейма)"

    admin_caption = (
        f"{text_content}\n\n"
        f"🖼 <b>Альбом из {len(messages)} медиафайлов</b>\n"
        f"👤 <b>Автор:</b> {user_link}{username}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"💬 <b>Тикет #{ticket_id}</b> | 📝 <b>Заявка #{post_id}</b>"
    )

    all_admins = list(set(ADMIN_IDS + VIP_ADMIN_IDS))
    for admin_id in all_admins:
        try:
            media_group = []
            for idx, m in enumerate(messages):
                cap = admin_caption if idx == 0 else ""
                if m.photo:
                    media_group.append(InputMediaPhoto(media=m.photo[-1].file_id, caption=cap))
                elif m.video:
                    media_group.append(InputMediaVideo(media=m.video.file_id, caption=cap))

            sent_msgs = await bot.send_media_group(chat_id=admin_id, media=media_group)
            ctrl_msg = await bot.send_message(
                chat_id=admin_id,
                text=f"⚙️ <b>Управление заявкой #{post_id} (Альбом):</b>",
                reply_markup=get_admin_kb(post_id, user.id, ticket_id)
            )
            save_admin_msg_mapping(admin_id, ctrl_msg.message_id, user.id)
            for sm in sent_msgs:
                save_admin_msg_mapping(admin_id, sm.message_id, user.id)

        except Exception as e:
            logging.error(f"Не удалось отправить альбом админу {admin_id}: {e}")

# --- ПРИЕМ ПРЕДЛОЖЕНИЙ И СООБЩЕНИЙ ОТ ПОЛЬЗОВАТЕЛЕЙ ---
@dp.message(F.chat.type == "private")
async def handle_suggestion(message: types.Message):
    if is_banned(message.from_user.id):
        return await message.answer("⚠️ Вы заблокированы в этой предложке.")

    register_user(message.from_user)

    if message.text and message.text.startswith("/"):
        return

    cursor.execute("SELECT id FROM tickets WHERE user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                   (message.from_user.id,))
    active_ticket = cursor.fetchone()

    if active_ticket:
        ticket_id = active_ticket[0]

        if message.media_group_id:
            mg_id = message.media_group_id
            if mg_id not in media_group_buffers:
                media_group_buffers[mg_id] = {'messages': [], 'task': None}

            media_group_buffers[mg_id]['messages'].append(message)
            if media_group_buffers[mg_id]['task']:
                media_group_buffers[mg_id]['task'].cancel()

            media_group_buffers[mg_id]['task'] = asyncio.create_task(process_media_group_delayed(mg_id))
            return

        text_content = message.text or message.caption or ""
        media_type = "text"
        file_id = None

        if message.photo:
            media_type = "photo"
            file_id = message.photo[-1].file_id
        elif message.video:
            media_type = "video"
            file_id = message.video.file_id

        log_ticket_message(ticket_id, "user", message.from_user.id, message.from_user.full_name, text_content, media_type, file_id)

        user_info = f"💬 <b>[Тикет #{ticket_id} | Пользователь {message.from_user.full_name}]:</b>"
        all_admins = list(set(ADMIN_IDS + VIP_ADMIN_IDS))

        for admin_id in all_admins:
            try:
                head_msg = await bot.send_message(admin_id, user_info)
                copy_msg = await bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                save_admin_msg_mapping(admin_id, head_msg.message_id, message.from_user.id)
                save_admin_msg_mapping(admin_id, copy_msg.message_id, message.from_user.id)
            except Exception as e:
                logging.error(f"Не удалось переслать сообщение админу {admin_id}: {e}")

        await message.answer("💬 Ваше сообщение добавлено в текущий диалог с администрацией!")
        return

    if message.media_group_id:
        mg_id = message.media_group_id
        if mg_id not in media_group_buffers:
            media_group_buffers[mg_id] = {'messages': [], 'task': None}

        media_group_buffers[mg_id]['messages'].append(message)

        if media_group_buffers[mg_id]['task']:
            media_group_buffers[mg_id]['task'].cancel()

        media_group_buffers[mg_id]['task'] = asyncio.create_task(process_media_group_delayed(mg_id))
        return

    media_type = "text"
    file_id = None
    text_content = message.text or ""

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
        text_content = message.caption or ""
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
        text_content = message.caption or ""

    cursor.execute(
        "INSERT INTO posts (user_id, status, text, file_id, media_type) VALUES (?, ?, ?, ?, ?)",
        (message.from_user.id, "pending", text_content, file_id, media_type)
    )
    post_id = cursor.lastrowid

    if file_id:
        cursor.execute("INSERT INTO post_media (post_id, file_id, media_type) VALUES (?, ?, ?)", (post_id, file_id, media_type))

    cursor.execute("INSERT INTO tickets (user_id, post_id, status) VALUES (?, ?, 'open')",
                   (message.from_user.id, post_id))
    ticket_id = cursor.lastrowid
    conn.commit()

    log_ticket_message(ticket_id, "user", message.from_user.id, message.from_user.full_name, text_content, media_type, file_id)

    await message.answer(f"🚀 Пост отправлен на модерацию! (Тикет #{ticket_id})")

    user_link = f"<a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a>"
    username = f" (@{message.from_user.username})" if message.from_user.username else " (нет юзернейма)"

    admin_caption = (
        f"{text_content}\n\n"
        f"👤 <b>Автор:</b> {user_link}{username}\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"💬 <b>Тикет #{ticket_id}</b> | 📝 <b>Заявка #{post_id}</b>"
    )

    all_admins = list(set(ADMIN_IDS + VIP_ADMIN_IDS))
    for admin_id in all_admins:
        try:
            sent_msg = None
            if media_type == "photo":
                sent_msg = await bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=admin_caption,
                    reply_markup=get_admin_kb(post_id, message.from_user.id, ticket_id)
                )
            elif media_type == "video":
                sent_msg = await bot.send_video(
                    chat_id=admin_id,
                    video=file_id,
                    caption=admin_caption,
                    reply_markup=get_admin_kb(post_id, message.from_user.id, ticket_id)
                )
            else:
                sent_msg = await bot.send_message(
                    chat_id=admin_id,
                    text=admin_caption,
                    reply_markup=get_admin_kb(post_id, message.from_user.id, ticket_id)
                )

            if sent_msg:
                save_admin_msg_mapping(admin_id, sent_msg.message_id, message.from_user.id)

        except Exception as e:
            logging.error(f"Не удалось отправить админу {admin_id}: {e}")

# --- ФУНКЦИЯ ПУБЛИКАЦИИ ПОСТА И ЗАКРЫТИЯ ТИКЕТА ---
async def publish_post_by_id(post_id: int, user_id: int, message: types.Message = None, callback: types.CallbackQuery = None):
    cursor.execute("SELECT status, text, file_id, media_type FROM posts WHERE id = ?", (post_id,))
    res = cursor.fetchone()

    if not res:
        if callback:
            await callback.answer("⚠️ Пост не найден в базе данных.", show_alert=True)
        elif message:
            await message.reply("⚠️ Пост не найден.")
        return

    status, original_text, file_id, media_type = res

    if status != "pending":
        if callback:
            await callback.answer(f"⚠️ Этот пост уже обработан! Статус: {status}", show_alert=True)
        elif message:
            await message.reply(f"⚠️ Этот пост уже обработан! Статус: {status}")
        return

    if is_banned(int(user_id)):
        if callback:
            await callback.answer("⚠️ Автор этого поста находится в черном списке!", show_alert=True)
        elif message:
            await message.reply("⚠️ Автор этого поста находится в черном списке!")
        return

    footer = (
        f"предложить пост : {SUGGEST_BOT}\n\n"
        f"удалить / узнать пост {DELETE_CONTACT}\n\n"
        f"📢 <a href='{CHANNEL_URL}'>{CHANNEL_NAME}</a>"
    )

    final_text = f"{original_text}\n\n{footer}" if original_text else footer

    cursor.execute("SELECT file_id, media_type FROM post_media WHERE post_id = ?", (post_id,))
    all_media = cursor.fetchall()

    try:
        if len(all_media) > 1:
            media_group = []
            for idx, (m_file_id, m_type) in enumerate(all_media):
                cap = final_text if idx == 0 else ""
                if m_type == "photo":
                    media_group.append(InputMediaPhoto(media=m_file_id, caption=cap))
                elif m_type == "video":
                    media_group.append(InputMediaVideo(media=m_file_id, caption=cap))

            await bot.send_media_group(CHANNEL_ID, media=media_group)
        else:
            if media_type == "photo":
                await bot.send_photo(CHANNEL_ID, file_id, caption=final_text)
            elif media_type == "video":
                await bot.send_video(CHANNEL_ID, file_id, caption=final_text)
            else:
                await bot.send_message(CHANNEL_ID, final_text)

        cursor.execute("UPDATE posts SET status = 'published' WHERE id = ?", (post_id,))
        cursor.execute("UPDATE tickets SET status = 'closed' WHERE post_id = ?", (post_id,))
        conn.commit()

        for aid, tid in list(active_admin_chats.items()):
            cursor.execute("SELECT post_id FROM tickets WHERE id = ?", (tid,))
            t_post = cursor.fetchone()
            if t_post and t_post[0] == post_id:
                active_admin_chats.pop(aid, None)

        try:
            await bot.send_message(
                user_id,
                "✅ Твой пост опубликован в канале!",
                reply_markup=get_user_more_kb()
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить пользователя {user_id}: {e}")

        if callback:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.message.reply("🚀 Пост опубликован модератором! Тикет закрыт.")
        elif message:
            await message.reply("🚀 Пост опубликован модератором! Тикет закрыт.")

    except Exception as e:
        if callback:
            await callback.answer(f"Ошибка при публикации: {e}", show_alert=True)
        elif message:
            await message.reply(f"Ошибка при публикации: {e}")

@dp.callback_query(F.data.startswith("pub_"))
async def approve_post(callback: types.CallbackQuery):
    data = callback.data.split("_")
    post_id, user_id = int(data[1]), int(data[2])
    await publish_post_by_id(post_id, user_id, callback=callback)

# --- ФУНКЦИЯ И КНОПКА ОТКЛОНЕНИЯ ПОСТА ---
async def reject_post_by_id(post_id: int, user_id: int, message: types.Message = None, callback: types.CallbackQuery = None):
    cursor.execute("SELECT status FROM posts WHERE id = ?", (post_id,))
    res = cursor.fetchone()

    if not res:
        if callback:
            await callback.answer("⚠️ Пост не найден.", show_alert=True)
        elif message:
            await message.reply("⚠️ Пост не найден.")
        return

    status = res[0]

    if status != "pending":
        if callback:
            await callback.answer(f"⚠️ Этот пост уже обработан! Статус: {status}", show_alert=True)
        elif message:
            await message.reply(f"⚠️ Этот пост уже обработан! Статус: {status}")
        return

    cursor.execute("UPDATE posts SET status = 'rejected' WHERE id = ?", (post_id,))
    cursor.execute("UPDATE tickets SET status = 'closed' WHERE post_id = ?", (post_id,))
    conn.commit()

    for aid, tid in list(active_admin_chats.items()):
        cursor.execute("SELECT post_id FROM tickets WHERE id = ?", (tid,))
        t_post = cursor.fetchone()
        if t_post and t_post[0] == post_id:
            active_admin_chats.pop(aid, None)

    try:
        await bot.send_message(
            user_id,
            "❌ К сожалению, твой пост был отклонен модератором.",
            reply_markup=get_user_more_kb()
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить пользователя {user_id}: {e}")

    if callback:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.reply("❌ Пост отклонен модератором! Тикет закрыт.")
    elif message:
        await message.reply("❌ Пост отклонен модератором! Тикет закрыт.")

@dp.callback_query(F.data.startswith("rej_"))
async def reject_post(callback: types.CallbackQuery):
    data = callback.data.split("_")
    post_id, user_id = int(data[1]), int(data[2])
    await reject_post_by_id(post_id, user_id, callback=callback)

# --- ОБРАБОТКА БЛОКИРОВКИ ПОЛЬЗОВАТЕЛЯ ---
@dp.callback_query(F.data.startswith("ban_"))
async def ban_user_callback(callback: types.CallbackQuery):
    data = callback.data.split("_")
    post_id, user_id = int(data[1]), int(data[2])

    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    if is_banned(user_id):
        await callback.answer("⚠️ Этот пользователь уже заблокирован!", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    cursor.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
    res_user = cursor.fetchone()
    username = res_user[0] if res_user else "None"

    cursor.execute("INSERT OR REPLACE INTO banned_users (user_id, username) VALUES (?, ?)", (user_id, username))
    cursor.execute("UPDATE posts SET status = 'rejected' WHERE user_id = ? AND status = 'pending'", (user_id,))
    cursor.execute("UPDATE tickets SET status = 'closed' WHERE user_id = ?", (user_id,))
    conn.commit()

    try:
        await bot.send_message(user_id, "❌ Вы были заблокированы администратором и больше не можете присылать посты.")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление о бане {user_id}: {e}")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.reply("🚫 Автор поста заблокирован модератором!")
    await callback.answer("🚫 Пользователь успешно забанен!")

# --- ОБРАБОТКА КНОПКИ «ОТПРАВИТЬ ЕЩЕ ОДИН ПОСТ» ---
@dp.callback_query(F.data == "send_more")
async def send_more_handler(callback: types.CallbackQuery):
    if is_banned(callback.from_user.id):
        return await callback.answer("⚠️ Вы заблокированы и не можете присылать посты.", show_alert=True)

    await callback.message.answer("📝 Жду твой новый пост! Просто отправь его мне (текст, фото или видео).")
    await callback.answer()

# --- КОМАНДЫ РАЗБЛОКИРОВКИ И УПРАВЛЕНИЯ БАНАМИ ---
@dp.message(Command("unban"))
async def unban_user_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id not in VIP_ADMIN_IDS:
        return await message.answer("🔒 У вас нет прав для выполнения этой команды.")

    args = message.text.split()
    if len(args) < 2:
        return await message.answer("⚠️ Пример использования команды: <code>/unban 123456789</code>")

    try:
        target_id = int(args[1])
        cursor.execute("SELECT username FROM banned_users WHERE user_id = ?", (target_id,))
        banned_user = cursor.fetchone()

        if not banned_user:
            return await message.answer("❌ Данный пользователь не найден в бан-листе.")

        cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (target_id,))
        conn.commit()

        try:
            await bot.send_message(target_id, "🔓 Администратор разблокировал вас в предложке.")
        except Exception:
            pass

        await message.answer(f"✅ Пользователь с ID <code>{target_id}</code> успешно разблокирован.")
    except ValueError:
        await message.answer("⚠️ ID должен состоять только из цифр.")

@dp.callback_query(F.data.startswith("unb_"))
async def unban_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    target_id = int(callback.data.split("_")[1])

    cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (target_id,))
    conn.commit()

    try:
        await bot.send_message(target_id, "🔓 Администратор разблокировал вас в предложке.")
    except Exception:
        pass

    await callback.answer("🔓 Пользователь успешно разблокирован!")
    await view_banlist(callback)

@dp.callback_query(F.data == "view_banlist")
async def view_banlist(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    cursor.execute("SELECT user_id, username FROM banned_users LIMIT 30")
    banned = cursor.fetchall()

    builder = InlineKeyboardBuilder()

    if not banned:
        builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
        await callback.message.edit_text("📝 <b>Список блокировок пуст!</b>", reply_markup=builder.as_markup())
        return await callback.answer()

    text = "📋 <b>Список заблокированных пользователей:</b>\n\nНажмите на кнопку ниже, чтобы удобно разбанить:\n\n"

    for uid, uname in banned:
        mention = f"@{uname}" if uname and uname != "None" else f"ID: {uid}"
        text += f"• {mention} (<code>{uid}</code>)\n"
        builder.button(text=f"🔓 Разбанить {uname if uname and uname != 'None' else uid}", callback_data=f"unb_{uid}")

    builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

# --- ПАНЕЛЬ /vipadmin И РАССЫЛКА ---
@dp.message(Command("vipadmin"))
async def vip_admin_panel(message: types.Message):
    if message.from_user.id not in VIP_ADMIN_IDS:
        return await message.answer("🔒 У вас нет прав VIP-администратора.")

    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Запустить рассылку", callback_data="vip_broadcast_start")
    await message.answer("👑 <b>VIP Панель Управления</b>", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "vip_broadcast_start")
async def start_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.")

    await state.set_state(VipStates.waiting_for_broadcast)

    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(text="❌ Отмена", callback_data="vip_broadcast_cancel")

    await callback.message.edit_text(
        "📝 <b>Режим создания рекламной рассылки.</b>\n\n"
        "Отправьте мне любое сообщение (текст, фото или видео) и я перешлю его абсолютно всем пользователям бота.",
        reply_markup=cancel_kb.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "vip_broadcast_cancel", StateFilter(VipStates.waiting_for_broadcast))
async def cancel_broadcast(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена.")
    await callback.answer()

@dp.message(StateFilter(VipStates.waiting_for_broadcast))
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()

    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()

    await message.answer(f"🚀 Запускаю рассылку для {len(users)} пользователей...")

    success_count = 0
    for user in users:
        try:
            await bot.copy_message(
                chat_id=user[0],
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await message.answer(f"📢 Рассылка завершена!\n✅ Успешно доставлено: {success_count}/{len(users)}")

# --- ИНЛАЙН-КНОПКА "ЧАТ С АВТОРОМ" ---
@dp.callback_query(F.data.startswith("chat_"))
async def start_chat_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    data = callback.data.split("_")
    post_id, user_id = int(data[1]), int(data[2])

    cursor.execute("SELECT id FROM tickets WHERE user_id = ? AND post_id = ? AND status = 'open'", (user_id, post_id))
    t_row = cursor.fetchone()

    if t_row:
        ticket_id = t_row[0]
    else:
        cursor.execute("INSERT INTO tickets (user_id, post_id, status) VALUES (?, ?, 'open')", (user_id, post_id))
        ticket_id = cursor.lastrowid
        conn.commit()

    active_admin_chats[callback.from_user.id] = ticket_id
    await callback.message.edit_text(
        f"💬 <b>Вы вошли в режим прямого чата с Тикетом #{ticket_id}!</b>\n\n"
        f"Все ваши обычные сообщения теперь уходят пользователю анонимно.\n"
        f"Команды: <code>/pub</code>, <code>/rej</code>, <code>/close</code>, <code>/exit</code>",
        reply_markup=get_active_chat_kb(ticket_id, post_id, user_id)
    )
    await callback.answer("💬 Вход в чат выполнен!")

# --- ЗАПУСК ВЕБ-СЕРВЕРА И ПОЛЛИНГА ---
async def main():
    app = web.Application()
    app.router.add_get('/', web_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    asyncio.create_task(scheduled_publisher_loop())

    logging.info(f"Bot polling started on port {PORT}...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
