import os
import asyncio
import sqlite3
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import LinkPreviewOptions
from aiohttp import web

# --- НАСТРОЙКИ ---
API_TOKEN = os.getenv('API_TOKEN')
PORT = int(os.getenv('PORT', 8080))

if not API_TOKEN:
    raise ValueError("ОШИБКА: Переменная окружения API_TOKEN не задана! Укажите её в настройках хостинга.")

ADMIN_IDS = [7541245548, 8470311411]
VIP_ADMIN_IDS = [7541245548, 8470311411]

CHANNEL_ID = -1004473411067
CHANNEL_URL = "https://t.me/Freakcrimea"
CHANNEL_NAME = "Фрики Крым"

logging.basicConfig(level=logging.INFO)

# --- БАЗА ДАННЫХ ---
conn = sqlite3.connect('suggestions.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''CREATE TABLE IF NOT EXISTS posts 
                  (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                   user_id INTEGER, 
                   status TEXT,
                   text TEXT,
                   file_id TEXT,
                   media_type TEXT)''')

cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                  (user_id INTEGER PRIMARY KEY, 
                   username TEXT)''')

cursor.execute('''CREATE TABLE IF NOT EXISTS banned_users 
                  (user_id INTEGER PRIMARY KEY, 
                   username TEXT)''')
conn.commit()

bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(
        parse_mode="HTML",
        link_preview=LinkPreviewOptions(is_disabled=True)
    )
)
dp = Dispatcher()


async def web_handler(request):
    """
    Простой HTTP обработчик, который Render будет пинговать.
    """
    return web.Response(text="Bot is running!")


# --- СОСТОЯНИЯ ДЛЯ РАССЫЛКИ ---
class VipStates(StatesGroup):
    waiting_for_broadcast = State()


# --- КНОПКИ ---
def get_admin_kb(post_id, user_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Опубликовать", callback_data=f"pub_{post_id}_{user_id}")
    builder.button(text="❌ Отклонить", callback_data=f"rej_{post_id}_{user_id}")
    builder.button(text="🚫 Забанить автора", callback_data=f"ban_{post_id}_{user_id}")
    builder.adjust(2, 1)  # 2 кнопки в первом ряду, 1 во втором
    return builder.as_markup()


def get_user_more_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Отправить еще один пост", callback_data="send_more")
    return builder.as_markup()


def get_admin_panel_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Список банов", callback_data="view_banlist")
    return builder.as_markup()


# --- ПРОВЕРКА НА БАН ---
def is_banned(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None


# --- РЕГИСТРАЦИЯ ПОЛЬЗОВАТЕЛЯ В БД ---
def register_user(user: types.User):
    cursor.execute("INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)",
                   (user.id, user.username or "None"))
    conn.commit()


# --- ОБЩИЕ КОМАНДЫ ---
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
        "Если вдруг вы получили уведомление, но не увидели пост, напишите людям из отдела связи SS."
    )


# --- ПАНЕЛЬ /admin ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id not in VIP_ADMIN_IDS:
        return await message.answer("🔒 У вас нет доступа к этой команде.")

    # Сбор статистики
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE status = 'pending'")
    pending_posts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE status = 'published'")
    pub_posts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM posts WHERE status = 'rejected'")
    rej_posts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM banned_users")
    banned_count = cursor.fetchone()[0]

    stats_text = (
        f"📊 <b>Панель администратора предложки</b>\n\n"
        f"👥 Всего пользователей в БД: {total_users}\n"
        f"⏳ На модерации: {pending_posts}\n"
        f"✅ Опубликовано: {pub_posts}\n"
        f"❌ Отклонено: {rej_posts}\n"
        f"🚫 В черном списке: {banned_count}"
    )
    await message.answer(stats_text, reply_markup=get_admin_panel_kb())


# --- КОМАНДА РАЗБЛОКИРОВКИ ЧЕРЕЗ ТЕКСТ /unban ---
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

        # Пробуем уведомить пользователя
        try:
            await bot.send_message(target_id, "🔓 Администратор разблокировал вас в предложке.")
        except Exception:
            pass

        await message.answer(f"✅ Пользователь с ID <code>{target_id}</code> успешно разблокирован.")
    except ValueError:
        await message.answer("⚠️ ID должен состоять только из цифр.")


# --- СНЯТИЕ БАНА ЧЕРЕЗ ИНЛАЙН КНОПКУ ---
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
    # Обновляем список банов на экране
    await view_banlist(callback)


# --- ПОКАЗ СПИСКА БАНОВ ---
@dp.callback_query(F.data == "view_banlist")
async def view_banlist(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    cursor.execute("SELECT user_id, username FROM banned_users LIMIT 30")
    banned = cursor.fetchall()

    if not banned:
        await callback.message.edit_text("📝 Список блокировок пуст!")  # Изменил, чтобы редактировать сообщение
        return await callback.answer("📝 Список блокировок пуст!", show_alert=True)

    builder = InlineKeyboardBuilder()
    text = "📋 <b>Список заблокированных пользователей:</b>\n\n"

    for uid, uname in banned:
        mention = f"@{uname}" if uname != "None" else f"ID: {uid}"
        text += f"• {mention} (ID: <code>{uid}</code>)\n"
        builder.button(text=f"🔓 Разбан {uname if uname != 'None' else uid}", callback_data=f"unb_{uid}")

    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


# --- ПАНЕЛЬ /vipadmin ---
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
            await asyncio.sleep(0.05)  # Защита от лимитов Telegram API
        except Exception:
            pass

    await message.answer(f"📢 Рассылка завершена!\n✅ Успешно доставлено: {success_count}/{len(users)}")


# --- ПРИЕМ ПРЕДЛОЖЕНИЙ ---
@dp.message(F.chat.type == "private")
async def handle_suggestion(message: types.Message):
    # Если пользователь в бане — игнорируем
    if is_banned(message.from_user.id):
        return await message.answer("⚠️ Вы заблокированы в этой предложке.")

    register_user(message.from_user)

    # Команды отсекаем
    if message.text and message.text.startswith("/"):
        return

    # Определяем тип медиафайла и сохраняем информацию
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

    # Запись в БД
    cursor.execute(
        "INSERT INTO posts (user_id, status, text, file_id, media_type) VALUES (?, ?, ?, ?, ?)",
        (message.from_user.id, "pending", text_content, file_id, media_type)
    )
    post_id = cursor.lastrowid
    conn.commit()

    await message.answer("🚀 Пост отправлен на модерацию!")

    # Формируем сообщение для админов (с данными автора)
    user_link = f"<a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a>"
    username = f" (@{message.from_user.username})" if message.from_user.username else " (нет юзернейма)"

    admin_caption = (
        f"{text_content}\n\n"
        f"👤 <b>Автор:</b> {user_link}{username}\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"📝 <b>Заявка #{post_id}</b>"
    )

    # Рассылаем пост админам
    for admin_id in ADMIN_IDS:
        try:
            if media_type == "photo":
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=admin_caption,
                    reply_markup=get_admin_kb(post_id, message.from_user.id)
                )
            elif media_type == "video":
                await bot.send_video(
                    chat_id=admin_id,
                    video=file_id,
                    caption=admin_caption,
                    reply_markup=get_admin_kb(post_id, message.from_user.id)
                )
            else:
                await bot.send_message(
                    chat_id=admin_id,
                    text=admin_caption,
                    reply_markup=get_admin_kb(post_id, message.from_user.id)
                )
        except Exception as e:
            logging.error(f"Не удалось отправить админу {admin_id}: {e}")


# --- ОБРАБОТКА ОДОБРЕНИЯ ПОСТА ---
@dp.callback_query(F.data.startswith("pub_"))
async def approve_post(callback: types.CallbackQuery):
    data = callback.data.split("_")
    post_id, user_id = data[1], data[2]

    cursor.execute("SELECT status, text, file_id, media_type FROM posts WHERE id = ?", (post_id,))
    res = cursor.fetchone()

    if not res:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await callback.answer("⚠️ Пост не найден в базе данных.", show_alert=True)

    status, original_text, file_id, media_type = res

    if status != "pending":
        await callback.answer(f"⚠️ Этот пост уже обработан! Статус: {status}", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # Проверяем, не забанен ли автор на момент публикации
    if is_banned(int(user_id)):
        await callback.answer("⚠️ Автор этого поста находится в черном списке!", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    final_text = f"{original_text}\n\n📢 <a href='{CHANNEL_URL}'>{CHANNEL_NAME}</a>"

    try:
        if media_type == "photo":
            await bot.send_photo(CHANNEL_ID, file_id, caption=final_text)
        elif media_type == "video":
            await bot.send_video(CHANNEL_ID, file_id, caption=final_text)
        else:
            await bot.send_message(CHANNEL_ID, final_text)

        cursor.execute("UPDATE posts SET status = 'published' WHERE id = ?", (post_id,))
        conn.commit()

        try:
            await bot.send_message(
                user_id,
                "✅ Твой пост опубликован в канале!",
                reply_markup=get_user_more_kb()
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить пользователя {user_id}: {e}")

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        admin = callback.from_user
        admin_mention = f"@{admin.username}" if admin.username else admin.full_name
        admin_link = f"<a href='tg://user?id={admin.id}'>{admin_mention}</a>"

        await callback.message.reply(f"🚀 Пост выложил администратор: {admin_link}")

    except Exception as e:
        await callback.answer(f"Ошибка при публикации: {e}", show_alert=True)


# --- ОБРАБОТКА ОТКЛОНЕНИЯ ПОСТА ---
@dp.callback_query(F.data.startswith("rej_"))
async def reject_post(callback: types.CallbackQuery):
    data = callback.data.split("_")
    post_id, user_id = data[1], data[2]

    cursor.execute("SELECT status FROM posts WHERE id = ?", (post_id,))
    res = cursor.fetchone()

    if not res:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await callback.answer("⚠️ Пост не найден.", show_alert=True)

    status = res[0]  # Исправлено: убрано лишнее смещение отступа

    if status != "pending":
        await callback.answer(f"⚠️ Этот пост уже обработан! Статус: {status}", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    cursor.execute("UPDATE posts SET status = 'rejected' WHERE id = ?", (post_id,))
    conn.commit()

    try:
        await bot.send_message(
            user_id,
            "❌ К сожалению, твой пост был отклонен модератором.",
            reply_markup=get_user_more_kb()
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить пользователя {user_id}: {e}")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    admin = callback.from_user
    admin_mention = f"@{admin.username}" if admin.username else admin.full_name
    admin_link = f"<a href='tg://user?id={admin.id}'>{admin_mention}</a>"

    await callback.message.reply(f"❌ Пост отклонил администратор: {admin_link}")


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
    conn.commit()

    try:
        await bot.send_message(user_id, "❌ Вы были заблокированы администратором и больше не можете присылать посты.")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление о бане {user_id}: {e}")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    admin = callback.from_user
    admin_mention = f"@{admin.username}" if admin.username else admin.full_name
    admin_link = f"<a href='tg://user?id={admin.id}'>{admin_mention}</a>"

    await callback.message.reply(f"🚫 Автор поста заблокирован администратором: {admin_link}")
    await callback.answer("🚫 Пользователь успешно забанен!")


# --- ОБРАБОТКА КНОПКИ «ОТПРАВИТЬ ЕЩЕ ОДИН ПОСТ» ---
@dp.callback_query(F.data == "send_more")
async def send_more_handler(callback: types.CallbackQuery):
    if is_banned(callback.from_user.id):
        return await callback.answer("⚠️ Вы заблокированы и не можете присылать посты.", show_alert=True)

    await callback.message.answer("📝 Жду твой новый пост! Просто отправь его мне (текст, фото или видео).")
    await callback.answer()


# --- ГЛАВНАЯ ФУНКЦИЯ ЗАПУСКА БОТА И ВЕБ-СЕРВЕРА ---
async def main():
    # Инициализация веб-приложения aiohttp
    app = web.Application()
    app.router.add_get('/', web_handler)


    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()


    logging.info(f"Bot polling started on port {PORT}...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())