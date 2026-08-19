import os
import time
import asyncio
import sqlite3
import logging
import csv
import html
import shutil
import zipfile
import io
from pathlib import Path as FilePath
from openpyxl import Workbook
from PIL import Image, ImageOps
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import LinkPreviewOptions, InputMediaPhoto, InputMediaVideo, FSInputFile
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
DB_PATH = os.getenv('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'suggestions.db'))
logging.info('SQLite database: %s', DB_PATH)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
            is_anonymous INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        
        '''CREATE TABLE IF NOT EXISTS post_media 
           (id INTEGER PRIMARY KEY AUTOINCREMENT, 
            post_id INTEGER, 
            file_id TEXT, 
            media_type TEXT)''',
        '''CREATE TABLE IF NOT EXISTS media_hashes
           (file_id TEXT PRIMARY KEY,
            media_type TEXT,
            hash_value TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        
        '''CREATE TABLE IF NOT EXISTS users
           (user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            language_code TEXT,
            is_premium INTEGER DEFAULT 0,
            avatar_file_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        
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
            publish_time INTEGER)''',
        '''CREATE TABLE IF NOT EXISTS channel_messages
           (id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER,
            message_id INTEGER,
            post_id INTEGER,
            file_id TEXT,
            file_unique_id TEXT,
            media_type TEXT,
            media_group_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel_id, message_id))''',
        '''CREATE TABLE IF NOT EXISTS audit_log
           (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT,
            post_id INTEGER, ticket_id INTEGER, user_id INTEGER, details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS search_history
           (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, post_id INTEGER,
            method TEXT, query_text TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'''
    ]
    for table in tables:
        cursor.execute(table)
    
    # Безопасная миграция старых БД: существующие данные не удаляются.
    for table, column in [
        ("posts", "is_anonymous INTEGER DEFAULT 1"),
        ("users", "first_name TEXT"),
        ("users", "last_name TEXT"),
        ("users", "language_code TEXT"),
        ("users", "is_premium INTEGER DEFAULT 0"),
        ("users", "avatar_file_id TEXT"),
        ("users", "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("users", "last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("channel_messages", "file_unique_id TEXT"),
        ("channel_messages", "media_group_id TEXT"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass

    # Восстанавливаем пользователей из старых данных, если users пустая.
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) SELECT DISTINCT user_id, 'None' FROM posts WHERE user_id IS NOT NULL")
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) SELECT DISTINCT user_id, 'None' FROM tickets WHERE user_id IS NOT NULL")
    conn.commit()

init_db()

# Миграция/индексация изображений: хеш хранится отдельно, чтобы поиск по фото
# работал быстро и не зависел от локальных копий файлов.
def _ensure_media_hash_schema():
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_hashes_hash ON media_hashes(hash_value)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_post_media_file ON post_media(file_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_channel_messages_post ON channel_messages(post_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_channel_messages_file ON channel_messages(file_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_channel_messages_unique ON channel_messages(file_unique_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_post ON audit_log(post_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_search_admin ON search_history(admin_id, id DESC)")
        conn.commit()
    except Exception:
        logging.exception('Не удалось создать индексы media_hashes')

_ensure_media_hash_schema()

bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(
        parse_mode="HTML",
        link_preview=LinkPreviewOptions(is_disabled=True)
    )
)
dp = Dispatcher()

media_group_buffers = {}
active_admin_chats = {}
# Админ может временно работать как обычный пользователь.
admin_in_user_mode = set()

async def web_handler(request):
    return web.Response(text="Bot is running!")

# --- СОСТОЯНИЯ (FSM) ---
class VipStates(StatesGroup):
    waiting_for_broadcast = State()

class AdminStates(StatesGroup):
    waiting_for_custom_time = State()
    waiting_for_edited_text = State()
    waiting_for_forwarded_post = State()
    waiting_for_post_link = State()
    waiting_for_post_id = State()
    waiting_for_channel_message_id = State()
    waiting_for_post_author = State()
    waiting_for_photo_search = State()
    waiting_for_post_text = State()

class UserStates(StatesGroup):
    waiting_for_edit_post = State()

# --- МЕНЮ НИЖНИХ КНОПОК (REPLY KEYBOARDS) ---
def get_user_reply_kb(has_active_ticket=False):
    builder = ReplyKeyboardBuilder()
    if has_active_ticket:
        builder.button(text="💬 Мой активный тикет")
        builder.button(text="👤 Настройка анонимности")
        builder.button(text="✏️ Изменить текст/медиа поста")
        builder.button(text="ℹ️ Помощь")
        builder.adjust(2, 2)
    else:
        builder.button(text="✍️ Отправить пост")
        builder.button(text="ℹ️ Помощь")
        builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def get_admin_reply_kb(in_chat=False):
    builder = ReplyKeyboardBuilder()
    if in_chat:
        builder.button(text="🚀 Опубликовать пост")
        builder.button(text="❌ Отклонить пост")
        builder.button(text="🚪 Выйти из чата")
        builder.adjust(2, 1)
    else:
        builder.button(text="📊 Админ панель")
        builder.button(text="📥 Активные тикеты")
        builder.button(text="📅 Отложенные посты")
        builder.button(text="📋 Список банов")
        builder.adjust(2, 2)
    return builder.as_markup(resize_keyboard=True)

# --- ИНЛАЙН-КНОПКИ ---
def get_admin_kb(post_id, user_id, ticket_id=None, is_anon=1):
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Опубликовать (Предпросмотр)", callback_data=f"prepub_{post_id}_{user_id}")
    builder.button(text="⏱ Отложить", callback_data=f"schedmenu_{post_id}_{user_id}")
    builder.button(text="❌ Отклонить", callback_data=f"rej_{post_id}_{user_id}")
    
    anon_str = "👤 Автор: Анонимно" if is_anon == 1 else "📛 Автор: Публично"
    builder.button(text=f"🔄 {anon_str}", callback_data=f"toggleanon_{post_id}_{user_id}")
    
    if ticket_id:
        builder.button(text="💬 Чат / История", callback_data=f"open_ticket_{ticket_id}")
    else:
        builder.button(text="💬 Чат с автором", callback_data=f"chat_{post_id}_{user_id}")
    
    builder.button(text="🚫 Забанить автора", callback_data=f"ban_{post_id}_{user_id}")
    builder.adjust(1, 2, 1, 1, 1)
    return builder.as_markup()

def get_user_anon_kb(post_id, is_anon):
    builder = InlineKeyboardBuilder()
    if is_anon == 1:
        builder.button(text="🔒 Сейчас: АНОНИМНО (Нажми, чтобы показать @username)", callback_data=f"user_anon_off_{post_id}")
    else:
        builder.button(text="📛 Сейчас: ПУБЛИЧНО (Нажми, чтобы скрыть юзернейм)", callback_data=f"user_anon_on_{post_id}")
    builder.adjust(1)
    return builder.as_markup()

def get_publish_confirm_kb(post_id, user_id, is_anon):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ ПОДТВЕРДИТЬ И ОПУБЛИКОВАТЬ", callback_data=f"confirmpub_{post_id}_{user_id}")
    
    anon_text = "👤 Анонимно (Скрыт)" if is_anon == 1 else "📛 Публично (Показать @username)"
    builder.button(text=f"🔄 {anon_text}", callback_data=f"toggleanon_confirm_{post_id}_{user_id}")
    
    builder.button(text="✏️ Изменить текст перед выкладкой", callback_data=f"adminedittext_{post_id}_{user_id}")
    builder.button(text="❌ Отмена", callback_data=f"cancelprepub_{post_id}_{user_id}")
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()

def get_admin_panel_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 Активные тикеты (Чаты)", callback_data="list_tickets")
    builder.button(text="📅 Отложенные посты", callback_data="list_scheduled")
    builder.button(text="📊 Детальная аналитика", callback_data="view_analytics")
    builder.button(text="📋 Список банов", callback_data="view_banlist")
    builder.button(text="📁 Архив закрытых тикетов", callback_data="closed_0")
    builder.button(text="📊 Excel пользователей", callback_data="export_excel")
    builder.button(text="💾 Скачать БД", callback_data="export_db")
    builder.button(text="🔎 Поиск поста", callback_data="post_search_menu")
    builder.button(text="🕘 Последние поиски", callback_data="recent_searches_0")
    builder.button(text="📜 Журнал действий", callback_data="audit_0")
    builder.adjust(1)
    return builder.as_markup()

async def calculate_photo_hash(file_id: str):
    """Возвращает 128-битный комбинированный dHash+aHash для фото Telegram."""
    if not file_id:
        return None
    try:
        tgfile = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tgfile.file_path, destination=buf)
        buf.seek(0)
        with Image.open(buf) as im:
            im = ImageOps.exif_transpose(im).convert('L')
            # dHash: 8x8 сравнений соседних пикселей
            d = im.resize((9, 8), Image.Resampling.LANCZOS)
            dbits = 0
            for y in range(8):
                for x in range(8):
                    dbits = (dbits << 1) | int(d.getpixel((x, y)) > d.getpixel((x + 1, y)))
            # aHash: средняя яркость 8x8
            a = im.resize((8, 8), Image.Resampling.LANCZOS)
            avg = sum(a.getdata()) / 64
            abits = 0
            for px in a.getdata():
                abits = (abits << 1) | int(px >= avg)
            return f'{dbits:016x}{abits:016x}'
    except Exception:
        logging.exception('Не удалось вычислить хеш фото %s', file_id)
        return None

async def index_photo_file(file_id: str, media_type: str = 'photo'):
    if not file_id or media_type != 'photo':
        return None
    cursor.execute('SELECT hash_value FROM media_hashes WHERE file_id=?', (file_id,))
    row = cursor.fetchone()
    if row and row[0]:
        return row[0]
    hv = await calculate_photo_hash(file_id)
    if hv:
        cursor.execute('INSERT OR REPLACE INTO media_hashes(file_id,media_type,hash_value) VALUES(?,?,?)', (file_id, media_type, hv))
        conn.commit()
    return hv

def hash_distance(a: str, b: str) -> int:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except Exception:
        return 999

async def index_all_photos(limit: int = 5000):
    cursor.execute('''SELECT DISTINCT pm.file_id FROM post_media pm
                      LEFT JOIN media_hashes mh ON mh.file_id=pm.file_id
                      WHERE pm.media_type='photo' AND mh.file_id IS NULL
                      LIMIT ?''', (limit,))
    files = [r[0] for r in cursor.fetchall() if r[0]]
    done = 0
    for fid in files:
        if await index_photo_file(fid, 'photo'):
            done += 1
    return done, len(files)

def cleanup_ticket_statuses():
    """Убирает зависшие open-текеты, если их пост уже опубликован/отклонён."""
    cursor.execute('''UPDATE tickets SET status='closed'
                      WHERE status='open' AND post_id IN
                      (SELECT id FROM posts WHERE status IN ('published','rejected'))''')
    changed = cursor.rowcount
    if changed:
        conn.commit()
        logging.info('Закрыто зависших тикетов: %s', changed)
    return changed

def get_active_chat_kb(ticket_id: int, post_id: int, user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Опубликовать пост", callback_data=f"prepub_{post_id}_{user_id}")
    builder.button(text="❌ Отклонить пост", callback_data=f"rej_{post_id}_{user_id}")
    builder.button(text="🚪 Выйти из чата", callback_data=f"exitchat_{ticket_id}")
    builder.adjust(1, 2, 1)
    return builder.as_markup()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_banned(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

def is_real_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in VIP_ADMIN_IDS


def is_admin_active(user_id: int) -> bool:
    return is_real_admin(user_id) and user_id not in admin_in_user_mode


def get_ticket_label(username, first_name, media_type, text, ticket_id):
    name = f"@{username}" if username and username != "None" else (first_name or "Пользователь")
    icons = {"photo": "📸", "video": "🎥", "album": "🖼", "text": "📝"}
    icon = icons.get(media_type, "📩")
    preview = (text or "").replace("\n", " ").strip()
    preview = f" «{preview[:18]}…»" if preview else ""
    return f"{name} • {icon}{preview} [#{ticket_id}]"


async def update_avatar(user_id: int):
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count:
            file_id = photos.photos[0][-1].file_id
            cursor.execute("UPDATE users SET avatar_file_id=? WHERE user_id=?", (file_id, user_id))
            conn.commit()
    except Exception as e:
        logging.debug("avatar %s: %s", user_id, e)


def register_user(user: types.User):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,))
    if cursor.fetchone():
        cursor.execute(
            '''UPDATE users SET username=?, first_name=?, last_name=?,
               language_code=?, is_premium=?, last_active=?
               WHERE user_id=?''',
            (user.username or "None", user.first_name or "",
             user.last_name or "", user.language_code or "unknown",
             int(bool(user.is_premium)), now, user.id)
        )
    else:
        cursor.execute(
            '''INSERT INTO users
               (user_id, username, first_name, last_name, language_code,
                is_premium, created_at, last_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (user.id, user.username or "None", user.first_name or "",
             user.last_name or "", user.language_code or "unknown",
             int(bool(user.is_premium)), now, now)
        )
    conn.commit()
    asyncio.create_task(update_avatar(user.id))


def sync_users_from_legacy_tables():
    cursor.execute("INSERT OR IGNORE INTO users(user_id,username,first_name,last_name,created_at,last_active) SELECT DISTINCT user_id, COALESCE(NULLIF(username,''), 'None'), '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM banned_users WHERE user_id IS NOT NULL")
    cursor.execute("INSERT OR IGNORE INTO users(user_id,username,first_name,last_name,created_at,last_active) SELECT DISTINCT user_id, 'None', '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM posts WHERE user_id IS NOT NULL")
    cursor.execute("INSERT OR IGNORE INTO users(user_id,username,first_name,last_name,created_at,last_active) SELECT DISTINCT user_id, 'None', '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM tickets WHERE user_id IS NOT NULL")
    conn.commit()

async def export_excel(admin_id: int):
    if not is_real_admin(admin_id): return
    sync_users_from_legacy_tables()
    filename=f"database_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb=Workbook()
    tables=[
        ("Пользователи","SELECT user_id,username,first_name,last_name,language_code,is_premium,avatar_file_id,created_at,last_active FROM users ORDER BY user_id"),
        ("Посты","SELECT * FROM posts ORDER BY id"),("Тикеты","SELECT * FROM tickets ORDER BY id"),
        ("История тикетов","SELECT * FROM ticket_messages ORDER BY id"),("Медиа","SELECT * FROM post_media ORDER BY id"),
        ("Баны","SELECT * FROM banned_users ORDER BY user_id"),("Расписание","SELECT * FROM scheduled_posts ORDER BY id")]
    for i,(title,q) in enumerate(tables):
        sh=wb.active if i==0 else wb.create_sheet()
        sh.title=title; cursor.execute(q); rows=cursor.fetchall(); sh.append([d[0] for d in cursor.description])
        for r in rows: sh.append(list(r))
        sh.freeze_panes='A2'; sh.auto_filter.ref=sh.dimensions
        for col in sh.columns:
            sh.column_dimensions[col[0].column_letter].width=min(max(len(str(c.value or '')) for c in col)+2,45)
    wb.save(filename)
    try: await bot.send_document(admin_id,FSInputFile(filename),caption='📊 Полный Excel-экспорт базы')
    finally:
        if os.path.exists(filename): os.remove(filename)

def create_sqlite_backup():
    backup_dir=FilePath(os.path.dirname(DB_PATH))/"backups"; backup_dir.mkdir(parents=True,exist_ok=True)
    path=backup_dir/f"suggestions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    dst=sqlite3.connect(str(path))
    try:
        conn.backup(dst)
    finally: dst.close()
    backups=sorted(backup_dir.glob('suggestions_*.db'),key=lambda x:x.stat().st_mtime,reverse=True)
    for old in backups[14:]:
        try: old.unlink()
        except OSError: pass
    return str(path)

async def export_db(admin_id:int):
    if not is_real_admin(admin_id): return
    path=create_sqlite_backup()
    await bot.send_document(admin_id,FSInputFile(path),caption='💾 Согласованная резервная копия SQLite-базы')

async def automatic_backup_loop():
    interval=max(1,int(os.getenv('BACKUP_INTERVAL_HOURS','24')))*3600
    while True:
        try:
            await asyncio.sleep(interval); create_sqlite_backup()
            logging.info('Automatic SQLite backup created')
        except asyncio.CancelledError: raise
        except Exception: logging.exception('Automatic backup failed')

def safe_filename(value):
    return ''.join(c if c.isalnum() or c in '-_' else '_' for c in str(value))[:80] or 'file'

async def build_ticket_zip(ticket_id:int):
    work=FilePath(os.path.dirname(DB_PATH))/"ticket_exports"/f"ticket_{ticket_id}_{int(time.time())}"; media_dir=work/'media'; media_dir.mkdir(parents=True,exist_ok=True)
    cursor.execute("SELECT sender_type,sender_id,sender_name,text,media_type,file_id,created_at FROM ticket_messages WHERE ticket_id=? ORDER BY id",(ticket_id,)); msgs=cursor.fetchall(); media_map={}
    for i,row in enumerate(msgs,1):
        mtype,file_id=row[4],row[5]
        if not file_id or mtype in (None,'text','album'): continue
        try:
            tgfile=await bot.get_file(file_id); ext={'photo':'.jpg','video':'.mp4','audio':'.mp3','document':'.bin'}.get(mtype,'.bin'); target=media_dir/f'{i:04d}_{safe_filename(mtype)}{ext}'
            await bot.download_file(tgfile.file_path,destination=str(target)); media_map[file_id]=f'media/{target.name}'
        except Exception: logging.exception('Media export failed for ticket %s',ticket_id)
    (work/'history.html').write_text(build_ticket_html(ticket_id,media_map),encoding='utf-8')
    (work/'README.txt').write_text(f'Ticket #{ticket_id}\nOpen history.html in a browser.\n',encoding='utf-8')
    zip_path=FilePath(os.path.dirname(DB_PATH))/f'ticket_{ticket_id}_full.zip'
    with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED) as z: 
        for f in work.rglob('*'):
            if f.is_file(): z.write(f,f.relative_to(work))
    shutil.rmtree(work,ignore_errors=True); return str(zip_path)

def build_ticket_html(ticket_id:int, media_map=None)->str:
    media_map=media_map or {}
    cursor.execute("SELECT t.id,t.user_id,t.status,t.created_at,u.username,u.first_name,u.last_name,t.post_id,p.status,p.text,p.media_type,p.created_at FROM tickets t LEFT JOIN users u ON u.user_id=t.user_id LEFT JOIN posts p ON p.id=t.post_id WHERE t.id=?",(ticket_id,)); t=cursor.fetchone()
    if not t: raise ValueError('Тикет не найден')
    tid,uid,status,created,username,first_name,last_name,pid,pstatus,ptext,pmtype,pcreated=t
    cursor.execute("SELECT sender_type,sender_id,sender_name,text,media_type,file_id,created_at FROM ticket_messages WHERE ticket_id=? ORDER BY id",(ticket_id,)); msgs=cursor.fetchall()
    esc=lambda v:html.escape(str(v or '')); name=(f'@{username}' if username and username!='None' else (first_name or 'Пользователь'))+(f' {last_name}' if last_name else '')
    blocks=[]
    for i,(stype,sid,sname,text,mtype,file_id,ctime) in enumerate(msgs,1):
        role='Пользователь' if stype=='user' else 'Администратор'; cls='user' if stype=='user' else 'admin'; media=''
        if mtype and mtype!='text':
            rel=media_map.get(file_id)
            if rel and mtype=='photo': media=f"<div class='media'><img src='{esc(rel)}'><br><a href='{esc(rel)}'>Открыть фото</a></div>"
            elif rel and mtype=='video': media=f"<div class='media'><video controls src='{esc(rel)}'></video><br><a href='{esc(rel)}'>Скачать видео</a></div>"
            elif rel: media=f"<div class='media'><a href='{esc(rel)}'>📎 Скачать {esc(mtype)}</a></div>"
            else: media=f"<div class='media'>📎 {esc(mtype)} <small>{esc(file_id)}</small></div>"
        blocks.append(f"<div class='message {cls}'><div class='meta'><b>{esc(role)}</b> · {esc(sname)} · {esc(ctime)}</div><div>{esc(text).replace(chr(10),'<br>')}</div>{media}</div>")
    return f"<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Тикет #{tid}</title><style>body{{font-family:Arial;background:#f4f5f7;padding:24px}}.container{{max-width:950px;margin:auto}}.card,.message{{background:#fff;border-radius:14px;padding:18px;margin:12px 0;box-shadow:0 2px 8px #0001}}.user{{background:#eef6ff}}.admin{{background:#f6fef9}}.meta{{color:#667085;font-size:13px;margin-bottom:8px}}img,video{{max-width:100%;border-radius:10px}}</style><div class='container'><div class='card'><h1>💬 Тикет #{tid}</h1><p><b>Пользователь:</b> {esc(name)}<br><b>ID:</b> {uid}<br><b>Статус:</b> {esc(status)}<br><b>Пост:</b> #{pid} ({esc(pstatus)})<br><b>Создан:</b> {esc(created)}</p></div><div class='card'><h2>📝 Исходная заявка</h2>{esc(ptext).replace(chr(10),'<br>')}<br><small>{esc(pmtype)} · {esc(pcreated)}</small></div><div class='card'><h2>📜 История чата ({len(msgs)})</h2>{''.join(blocks) or '<i>Сообщений нет.</i>'}</div></div></html>"

async def build_ticket_html_self_contained(ticket_id: int):
    """Создаёт HTML, в котором фото встроены base64, поэтому они открываются без Telegram/интернета."""
    cursor.execute("SELECT sender_type,sender_id,sender_name,text,media_type,file_id,created_at FROM ticket_messages WHERE ticket_id=? ORDER BY id", (ticket_id,))
    msgs = cursor.fetchall()
    media_map = {}
    for idx, row in enumerate(msgs, 1):
        mtype, file_id = row[4], row[5]
        if not file_id or mtype != 'photo':
            continue
        try:
            tgfile = await bot.get_file(file_id)
            buf = io.BytesIO()
            await bot.download_file(tgfile.file_path, destination=buf)
            import base64
            media_map[file_id] = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')
        except Exception:
            logging.exception('HTML media export failed for ticket %s message %s', ticket_id, idx)
    return build_ticket_html(ticket_id, media_map)

@dp.callback_query(F.data.startswith('ticket_html_'))
async def ticket_html_callback(callback:types.CallbackQuery):
    if not is_real_admin(callback.from_user.id): return await callback.answer('🔒 Доступ запрещен.',show_alert=True)
    filename=None
    try:
        tid=int(callback.data.split('_')[-1]); filename=f'ticket_{tid}_history.html'; html_data=await build_ticket_html_self_contained(tid); open(filename,'w',encoding='utf-8').write(html_data); await bot.send_document(callback.from_user.id,FSInputFile(filename),caption=f'📄 Полная HTML-история тикета #{tid} — фото встроены в файл'); await callback.answer('📄 HTML готов')
    except Exception: logging.exception('HTML export error'); await callback.answer('❌ Не удалось сформировать HTML.',show_alert=True)
    finally:
        if filename and os.path.exists(filename): os.remove(filename)

@dp.callback_query(F.data.startswith('ticket_zip_'))
async def ticket_zip_callback(callback:types.CallbackQuery):
    if not is_real_admin(callback.from_user.id): return await callback.answer('🔒 Доступ запрещен.',show_alert=True)
    filename=None
    try:
        tid=int(callback.data.split('_')[-1]); filename=await build_ticket_zip(tid); await bot.send_document(callback.from_user.id,FSInputFile(filename),caption=f'📦 Полный архив тикета #{tid}: HTML + медиа'); await callback.answer('📦 Архив готов')
    except Exception: logging.exception('ZIP export error'); await callback.answer('❌ Не удалось собрать архив.',show_alert=True)
    finally:
        if filename and os.path.exists(filename): os.remove(filename)

async def show_user_profile(user_id:int,admin_id:int,callback=None,message=None):
    if not is_real_admin(admin_id): return await (callback.answer('🔒 Доступ запрещен.',show_alert=True) if callback else None)
    sync_users_from_legacy_tables(); cursor.execute("SELECT user_id,username,first_name,last_name,language_code,is_premium,avatar_file_id,created_at,last_active FROM users WHERE user_id=?",(user_id,)); u=cursor.fetchone()
    if not u: return await (callback.answer('Пользователь не найден.',show_alert=True) if callback else message.answer('Пользователь не найден.'))
    uid,un,fn,ln,lang,prem,av,created,last=u; name=' '.join(x for x in [fn,ln] if x) or 'Без имени'
    def cnt(q): cursor.execute(q,(uid,)); return cursor.fetchone()[0]
    pc=cnt('SELECT COUNT(*) FROM posts WHERE user_id=?'); pub=cnt("SELECT COUNT(*) FROM posts WHERE user_id=? AND status='published'"); rej=cnt("SELECT COUNT(*) FROM posts WHERE user_id=? AND status='rejected'"); tc=cnt('SELECT COUNT(*) FROM tickets WHERE user_id=?'); oc=cnt("SELECT COUNT(*) FROM tickets WHERE user_id=? AND status='open'")
    text=(f"👤 <b>ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ</b>\n\n<b>{html.escape(name)}</b>\nUsername: {('@'+html.escape(un)) if un and un!='None' else 'нет'}\n🆔 ID: <code>{uid}</code>\n🌐 Язык: {html.escape(lang or '—')}\n⭐ Premium: {'да' if prem else 'нет'}\n🚫 Бан: {'да' if is_banned(uid) else 'нет'}\n\n📊 <b>Статистика</b>\n• Постов: {pc}\n• Опубликовано: {pub}\n• Отклонено: {rej}\n• Тикетов: {tc}\n• Открытых: {oc}\n\n📅 Регистрация: {html.escape(str(created or '—'))}\n🕐 Активность: {html.escape(str(last or '—'))}")
    kb=InlineKeyboardBuilder(); kb.button(text='📋 Посты пользователя',callback_data=f'user_posts_{uid}_0'); kb.button(text='💬 Тикеты пользователя',callback_data=f'user_tickets_{uid}_0'); kb.button(text='🚫 Забанить' if not is_banned(uid) else '✅ Разбанить',callback_data=f'profile_ban_{uid}'); kb.button(text='🔙 Назад',callback_data='back_to_admin'); kb.adjust(1)
    if callback: await callback.message.edit_text(text,reply_markup=kb.as_markup()); await callback.answer()
    else: await message.answer(text,reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith('user_profile_'))
async def user_profile_callback(callback:types.CallbackQuery):
    if not is_real_admin(callback.from_user.id): return await callback.answer('🔒 Доступ запрещен.',show_alert=True)
    uid=int(callback.data.split('_')[-1]); audit(callback.from_user.id,'open_user_profile',None,None,uid); await show_user_profile(uid,callback.from_user.id,callback=callback)

@dp.message(Command('user'))
async def user_command(message:types.Message):
    if not is_real_admin(message.from_user.id): return
    parts=message.text.split(maxsplit=1)
    if len(parts)<2 or not parts[1].strip().isdigit(): return await message.answer('Использование: <code>/user 123456789</code>')
    await show_user_profile(int(parts[1].strip()),message.from_user.id,message=message)

@dp.message(Command('finduser'))
async def find_user_command(message:types.Message):
    if not is_real_admin(message.from_user.id): return
    parts=message.text.split(maxsplit=1); q=parts[1].strip().lstrip('@') if len(parts)>1 else ''
    if not q: return await message.answer('Использование: <code>/finduser username</code> или имя')
    cursor.execute("SELECT user_id,username,first_name,last_name FROM users WHERE username LIKE ? OR first_name LIKE ? OR last_name LIKE ? ORDER BY last_active DESC LIMIT 20",(f'%{q}%',f'%{q}%',f'%{q}%')); rows=cursor.fetchall(); kb=InlineKeyboardBuilder()
    for uid,un,fn,ln in rows: kb.button(text=f"👤 {('@'+un) if un and un!='None' else (' '.join(x for x in [fn,ln] if x) or str(uid))} [{uid}]",callback_data=f'user_profile_{uid}')
    kb.button(text='🔙 Админка',callback_data='back_to_admin'); kb.adjust(1); await message.answer(f'🔎 Найдено: {len(rows)}',reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith('profile_ban_'))
async def profile_ban_callback(callback:types.CallbackQuery):
    if not is_real_admin(callback.from_user.id): return await callback.answer('🔒 Доступ запрещен.',show_alert=True)
    uid=int(callback.data.split('_')[-1]); cursor.execute('SELECT username FROM users WHERE user_id=?',(uid,)); row=cursor.fetchone();
    if is_banned(uid):
        cursor.execute('DELETE FROM banned_users WHERE user_id=?',(uid,)); conn.commit(); await callback.answer('✅ Пользователь разбанен')
    else:
        username=row[0] if row else 'None'; cursor.execute('INSERT OR REPLACE INTO banned_users(user_id,username) VALUES(?,?)',(uid,username)); conn.commit(); await callback.answer('🚫 Пользователь заблокирован')
    await show_user_profile(uid,callback.from_user.id,callback=callback)

@dp.message(Command('findticket'))
async def find_ticket_command(message:types.Message):
    if not is_real_admin(message.from_user.id): return
    parts=message.text.split(maxsplit=1); q=parts[1].strip() if len(parts)>1 else ''
    if not q: return await message.answer('Использование: <code>/findticket 123</code> или <code>/findticket username</code>')
    if q.isdigit():
        cursor.execute('SELECT id FROM tickets WHERE id=?',(int(q),)); ids=[r[0] for r in cursor.fetchall()]
    else:
        q=q.lstrip('@'); cursor.execute("SELECT t.id FROM tickets t LEFT JOIN users u ON u.user_id=t.user_id WHERE u.username LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ? ORDER BY t.id DESC LIMIT 30",(f'%{q}%',f'%{q}%',f'%{q}%')); ids=[r[0] for r in cursor.fetchall()]
    kb=InlineKeyboardBuilder()
    for tid in ids: kb.button(text=f'💬 Тикет #{tid}',callback_data=f'open_ticket_{tid}')
    kb.button(text='🔙 Админка',callback_data='back_to_admin'); kb.adjust(1)
    await message.answer(f'🔎 Найдено тикетов: {len(ids)}',reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith('user_posts_'))
async def user_posts_callback(callback:types.CallbackQuery):
    if not is_real_admin(callback.from_user.id): return await callback.answer('🔒 Доступ запрещен.',show_alert=True)
    parts=callback.data.split('_'); uid=int(parts[2]); page=int(parts[3]); limit=10; offset=page*limit
    cursor.execute("SELECT id,status,media_type,text,created_at FROM posts WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",(uid,limit,offset)); rows=cursor.fetchall(); kb=InlineKeyboardBuilder(); text=f'📋 <b>Посты пользователя {uid}</b>\n\n'
    for pid,st,mt,tx,dt in rows: text+=f'#{pid} · {st} · {mt} · {html.escape((tx or "")[:60])} · {dt}\n'; kb.button(text=f'📌 Пост #{pid}',callback_data=f'post_card_{pid}')
    if page: kb.button(text='⬅️',callback_data=f'user_posts_{uid}_{page-1}')
    if len(rows)==limit: kb.button(text='➡️',callback_data=f'user_posts_{uid}_{page+1}')
    kb.button(text='👤 Профиль',callback_data=f'user_profile_{uid}'); kb.adjust(2,1)
    await callback.message.edit_text(text if rows else '📋 Постов нет.',reply_markup=kb.as_markup()); await callback.answer()

@dp.callback_query(F.data.startswith('user_tickets_'))
async def user_tickets_callback(callback:types.CallbackQuery):
    if not is_real_admin(callback.from_user.id): return await callback.answer('🔒 Доступ запрещен.',show_alert=True)
    parts=callback.data.split('_'); uid=int(parts[2]); page=int(parts[3]); limit=8; offset=page*limit
    cursor.execute("SELECT id,status,created_at FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",(uid,limit,offset)); rows=cursor.fetchall(); kb=InlineKeyboardBuilder(); text=f'💬 <b>Тикеты пользователя {uid}</b>\n\n'
    for tid,st,dt in rows: text+=f'#{tid} · {st} · {dt}\n'; kb.button(text=f'💬 Тикет #{tid}',callback_data=f'open_ticket_{tid}')
    if page: kb.button(text='⬅️',callback_data=f'user_tickets_{uid}_{page-1}')
    if len(rows)==limit: kb.button(text='➡️',callback_data=f'user_tickets_{uid}_{page+1}')
    kb.button(text='👤 Профиль',callback_data=f'user_profile_{uid}'); kb.adjust(1)
    await callback.message.edit_text(text if rows else '💬 Тикетов нет.',reply_markup=kb.as_markup()); await callback.answer()

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

def get_user_open_ticket(user_id: int):
    cursor.execute("SELECT t.id, t.post_id FROM tickets t JOIN posts p ON p.id=t.post_id WHERE t.user_id = ? AND t.status = 'open' AND p.status = 'pending' ORDER BY t.id DESC LIMIT 1", (user_id,))
    return cursor.fetchone()

def build_final_post_text(original_text: str, user_id: int, is_anon: int):
    cursor.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
    u_row = cursor.fetchone()
    username = u_row[0] if u_row and u_row[0] != "None" else None

    text_body = original_text or ""

    author_tag = ""
    if is_anon == 0:
        if username:
            author_tag = f"\n\n👤 <b>Автор:</b> @{username}"
        else:
            author_tag = f"\n\n👤 <b>Автор:</b> <a href='tg://user?id={user_id}'>Пользователь</a>"

    footer = (
        f"\n\nпредложить пост : {SUGGEST_BOT}\n\n"
        f"удалить / узнать пост {DELETE_CONTACT}\n\n"
        f"📢 <a href='{CHANNEL_URL}'>{CHANNEL_NAME}</a>"
    )

    return f"{text_body}{author_tag}{footer}"


async def register_channel_messages(post_id: int, sent_messages):
    """Сохраняет связь поста с реальными сообщениями канала."""
    if not sent_messages:
        return
    if not isinstance(sent_messages, (list, tuple)):
        sent_messages = [sent_messages]
    for msg in sent_messages:
        try:
            mid = getattr(msg, 'message_id', None)
            if not mid:
                continue
            fid = None; unique_id = None; mtype = 'text'; mgid = getattr(msg, 'media_group_id', None)
            if getattr(msg, 'photo', None):
                obj = msg.photo[-1]; fid = obj.file_id; unique_id = getattr(obj, 'file_unique_id', None); mtype = 'photo'
            elif getattr(msg, 'video', None):
                obj = msg.video; fid = obj.file_id; unique_id = getattr(obj, 'file_unique_id', None); mtype = 'video'
            cursor.execute("""INSERT OR REPLACE INTO channel_messages
                              (channel_id,message_id,post_id,file_id,file_unique_id,media_type,media_group_id)
                              VALUES(?,?,?,?,?,?,?)""",
                           (CHANNEL_ID, mid, post_id, fid, unique_id, mtype, mgid))
        except Exception:
            logging.exception('Не удалось сохранить связь channel message -> post')
    conn.commit()

def find_ticket_for_post(post_id: int):
    cursor.execute("SELECT id FROM tickets WHERE post_id=? ORDER BY id DESC LIMIT 1", (post_id,))
    row = cursor.fetchone()
    return row[0] if row else None

async def find_post_by_forwarded_message(message: types.Message):
    """Ищет пост по сообщению канала, file_id или визуальному хешу фото."""
    origin = getattr(message, 'forward_origin', None)
    channel_id = None; original_message_id = None
    if origin is not None and getattr(origin, 'type', None) == 'channel':
        chat = getattr(origin, 'chat', None)
        channel_id = getattr(chat, 'id', None)
        original_message_id = getattr(origin, 'message_id', None)
    if channel_id is None:
        fchat = getattr(message, 'forward_from_chat', None)
        channel_id = getattr(fchat, 'id', None)
        original_message_id = getattr(message, 'forward_from_message_id', None)
    if channel_id and original_message_id:
        cursor.execute("SELECT post_id FROM channel_messages WHERE channel_id=? AND message_id=? LIMIT 1", (channel_id, original_message_id))
        row = cursor.fetchone()
        if row: return row[0], 'channel_message', 0
    file_id = None
    if getattr(message, 'photo', None): file_id = message.photo[-1].file_id
    elif getattr(message, 'video', None): file_id = message.video.file_id
    if file_id:
        cursor.execute("""SELECT DISTINCT post_id FROM post_media WHERE file_id=?
                          UNION SELECT id FROM posts WHERE file_id=? LIMIT 10""", (file_id, file_id))
        row = cursor.fetchone()
        if row: return row[0], 'file_id', 0
    unique_id = None
    if getattr(message, 'photo', None): unique_id = getattr(message.photo[-1], 'file_unique_id', None)
    elif getattr(message, 'video', None): unique_id = getattr(message.video, 'file_unique_id', None)
    if unique_id:
        cursor.execute('SELECT post_id FROM channel_messages WHERE file_unique_id=? ORDER BY id DESC LIMIT 1',(unique_id,))
        row=cursor.fetchone()
        if row: return row[0], 'file_unique_id', 0
    if getattr(message, 'photo', None) and file_id:
        hv = await calculate_photo_hash(file_id)
        if hv:
            cursor.execute("SELECT file_id, hash_value FROM media_hashes WHERE media_type='photo'")
            best=[]
            for fid, dbhv in cursor.fetchall():
                d=hash_distance(hv, dbhv)
                if d <= 18:
                    cursor.execute("SELECT DISTINCT post_id FROM post_media WHERE file_id=?", (fid,))
                    best.extend((d,pid) for (pid,) in cursor.fetchall())
            if best:
                best.sort(key=lambda x:x[0]); d,pid=best[0]
                return pid, 'photo_similarity', d
    return None, None, None

def build_lookup_keyboard(post_id: int, ticket_id, user_id: int):
    kb=InlineKeyboardBuilder()
    if ticket_id: kb.button(text=f"🎫 Открыть тикет #{ticket_id}", callback_data=f"open_ticket_{ticket_id}")
    kb.button(text="👤 Панель пользователя", callback_data=f"user_profile_{user_id}")
    kb.button(text="📋 Все посты пользователя", callback_data=f"user_posts_{user_id}_0")
    if ticket_id:
        kb.button(text="📄 HTML тикета", callback_data=f"ticket_html_{ticket_id}")
        kb.button(text="📦 ZIP тикета", callback_data=f"ticket_zip_{ticket_id}")
    kb.button(text="🔎 Искать другой пост", callback_data="post_search_menu")
    kb.button(text="🔙 Админ панель", callback_data="back_to_admin")
    kb.adjust(1)
    return kb.as_markup()

def audit(admin_id:int, action:str, post_id=None, ticket_id=None, user_id=None, details=""):
    try:
        cursor.execute("INSERT INTO audit_log(admin_id,action,post_id,ticket_id,user_id,details) VALUES(?,?,?,?,?,?)", (admin_id,action,post_id,ticket_id,user_id,details))
        conn.commit()
    except Exception: logging.exception("audit log failed")

def save_search(admin_id:int, post_id:int, method:str, query_text=""):
    try:
        cursor.execute("INSERT INTO search_history(admin_id,post_id,method,query_text) VALUES(?,?,?,?)", (admin_id,post_id,method,query_text[:500]))
        conn.commit()
    except Exception: logging.exception("search history failed")

def post_fingerprint(post_id:int):
    cursor.execute("SELECT p.id,p.media_group_id,p.file_id,cm.message_id,cm.channel_id FROM posts p LEFT JOIN channel_messages cm ON cm.post_id=p.id WHERE p.id=? ORDER BY cm.message_id", (post_id,))
    return cursor.fetchall()

def get_post_card(post_id:int):
    cursor.execute("""SELECT p.id,p.user_id,p.status,p.text,p.media_type,p.file_id,p.media_group_id,p.created_at,
                             u.username,u.first_name,u.last_name,t.id,t.status,p.is_anonymous
                      FROM posts p LEFT JOIN users u ON u.user_id=p.user_id LEFT JOIN tickets t ON t.post_id=p.id
                      WHERE p.id=? ORDER BY t.id DESC LIMIT 1""", (post_id,))
    return cursor.fetchone()

def post_card_text(row):
    pid,uid,status,text_body,media_type,file_id,mgid,created,un,fn,ln,tid,tstatus,is_anon=row
    name=f"@{un}" if un and un!='None' else ' '.join(x for x in [fn,ln] if x) or f'ID {uid}'
    icons={'photo':'📸','video':'🎥','album':'🖼','text':'📝'}
    channels=post_fingerprint(pid); mids=[str(x[3]) for x in channels if x[3]]
    return (f"📌 <b>ПОСТ #{pid}</b>\n\n👤 <b>Автор:</b> {html.escape(name)}\n🆔 <b>ID:</b> <code>{uid}</code>\n"
            f"🎫 <b>Тикет:</b> #{tid or '—'}\n📊 <b>Статус:</b> {html.escape(str(status))}\n"
            f"📦 <b>Тип:</b> {icons.get(media_type,'📩')} {html.escape(str(media_type or '—'))}\n📅 <b>Создан:</b> {html.escape(str(created or '—'))}\n"
            f"🔗 <b>Сообщения канала:</b> {', '.join(mids) if mids else '—'}\n" + (f"🌐 <b>Ссылка:</b> {CHANNEL_URL}/{mids[0]}\n" if mids and CHANNEL_URL else '') + f"\n📝 <b>Текст:</b>\n{html.escape(text_body or '—')[:3000]}")

def build_post_card_kb(post_id:int, ticket_id, user_id:int):
    kb=InlineKeyboardBuilder()
    if ticket_id: kb.button(text=f"🎫 Открыть тикет #{ticket_id}",callback_data=f"open_ticket_{ticket_id}")
    kb.button(text="👤 Панель пользователя",callback_data=f"user_profile_{user_id}")
    kb.button(text="📋 Все посты пользователя",callback_data=f"user_posts_{user_id}_0")
    if ticket_id:
        kb.button(text="📄 HTML тикета",callback_data=f"ticket_html_{ticket_id}")
        kb.button(text="📦 ZIP тикета",callback_data=f"ticket_zip_{ticket_id}")
    kb.button(text="🔎 Искать другой пост",callback_data="post_search_menu")
    kb.button(text="🔙 Админ панель",callback_data="back_to_admin")
    kb.adjust(1)
    return kb.as_markup()

async def show_lookup_result(message: types.Message, post_id: int, method: str, distance, query_text=""):
    row=get_post_card(post_id)
    if not row: return await message.answer('⚠️ Пост найден по индексу, но записи в БД нет.')
    tid=row[11]; uid=row[1]
    save_search(message.from_user.id,post_id,method,query_text); audit(message.from_user.id,'search_post',post_id,tid,uid,f'method={method};distance={distance}')
    text=post_card_text(row)
    if method=='photo_similarity': text += f"\n\n📐 <b>Совпадение по фото:</b> {max(0,round(100-(distance or 0)*100/128))}%"
    elif method=='channel_message': text += "\n\n🎯 <b>Найдено по ID сообщения канала.</b>"
    elif method=='file_id': text += "\n\n🎯 <b>Найдено по Telegram media ID.</b>"
    elif method=='channel_link': text += "\n\n🎯 <b>Найдено по ссылке на сообщение канала.</b>"
    elif method=='file_unique_id': text += "\n\n🎯 <b>Найдено по Telegram file_unique_id.</b>"
    elif method=='post_id': text += "\n\n🎯 <b>Найдено по внутреннему ID поста.</b>"
    await message.answer(text,reply_markup=build_post_card_kb(post_id,tid,uid))
    cursor.execute("SELECT file_id,media_type FROM post_media WHERE post_id=? ORDER BY id",(post_id,))
    for fid,mt in cursor.fetchall()[:10]:
        try:
            if mt=='photo': await bot.send_photo(message.chat.id,fid,caption=f'📸 Пост #{post_id}')
            elif mt=='video': await bot.send_video(message.chat.id,fid,caption=f'🎥 Пост #{post_id}')
        except Exception: pass

def build_post_search_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Пересланный пост", callback_data="search_mode_forward")
    kb.button(text="🔗 Ссылка на пост", callback_data="search_mode_link")
    kb.button(text="🆔 ID поста в боте", callback_data="search_mode_post_id")
    kb.button(text="📢 ID сообщения канала", callback_data="search_mode_channel_id")
    kb.button(text="🖼 По фотографии", callback_data="search_mode_photo")
    kb.button(text="👤 По автору", callback_data="search_mode_author")
    kb.button(text="📝 По тексту поста", callback_data="search_mode_text")
    kb.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    kb.adjust(1)
    return kb.as_markup()

@dp.callback_query(F.data == 'post_search_menu')
async def post_search_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    if not is_real_admin(callback.from_user.id):
        return await callback.answer('🔒 Доступ запрещен.', show_alert=True)
    await state.clear()
    await callback.message.edit_text(
        "🔎 <b>ПОИСК ПОСТА</b>\n\nВыберите параметр, по которому искать:\n\n"
        "📤 Пересланный пост — самый точный способ для опубликованных сообщений.\n"
        "🔗 Ссылка — если у вас есть ссылка на пост.\n"
        "🆔 ID поста — внутренний номер поста в боте.\n"
        "📢 ID сообщения канала — Telegram message ID.\n"
        "🖼 Фото — резервный поиск по изображению.\n"
        "👤 Автор — username или Telegram ID.\n"
        "📝 Текст — поиск по тексту/подписи.",
        reply_markup=build_post_search_menu()
    )
    await callback.answer()

async def start_search_mode(callback: types.CallbackQuery, state: FSMContext, mode: str, prompt: str):
    if not is_real_admin(callback.from_user.id):
        return await callback.answer('🔒 Доступ запрещен.', show_alert=True)
    await state.clear()
    await state.set_state(getattr(AdminStates, mode))
    kb = InlineKeyboardBuilder()
    kb.button(text='🔙 Отмена', callback_data='post_search_menu')
    await callback.message.edit_text(prompt, reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data == 'search_mode_forward')
async def search_mode_forward(callback: types.CallbackQuery, state: FSMContext):
    await start_search_mode(callback, state, 'waiting_for_forwarded_post', '📤 <b>Пересланный пост</b>\n\nПерешлите сюда пост из Telegram-канала.')

@dp.callback_query(F.data == 'search_mode_link')
async def search_mode_link(callback: types.CallbackQuery, state: FSMContext):
    await start_search_mode(callback, state, 'waiting_for_post_link', '🔗 <b>Ссылка на пост</b>\n\nОтправьте ссылку вида <code>https://t.me/channel/1234</code>.')

@dp.callback_query(F.data == 'search_mode_post_id')
async def search_mode_post_id(callback: types.CallbackQuery, state: FSMContext):
    await start_search_mode(callback, state, 'waiting_for_post_id', '🆔 <b>ID поста в боте</b>\n\nОтправьте числовой ID поста, например <code>1842</code>.')

@dp.callback_query(F.data == 'search_mode_channel_id')
async def search_mode_channel_id(callback: types.CallbackQuery, state: FSMContext):
    await start_search_mode(callback, state, 'waiting_for_channel_message_id', '📢 <b>ID сообщения канала</b>\n\nОтправьте message ID опубликованного сообщения, например <code>5831</code>.')

@dp.callback_query(F.data == 'search_mode_photo')
async def search_mode_photo(callback: types.CallbackQuery, state: FSMContext):
    await start_search_mode(callback, state, 'waiting_for_photo_search', '🖼 <b>Поиск по фотографии</b>\n\nОтправьте фотографию. Это резервный поиск по визуальному сходству.')

@dp.callback_query(F.data == 'search_mode_author')
async def search_mode_author(callback: types.CallbackQuery, state: FSMContext):
    await start_search_mode(callback, state, 'waiting_for_post_author', '👤 <b>Поиск по автору</b>\n\nОтправьте @username, username без @ или Telegram ID.')

@dp.callback_query(F.data == 'search_mode_text')
async def search_mode_text(callback: types.CallbackQuery, state: FSMContext):
    await start_search_mode(callback, state, 'waiting_for_post_text', '📝 <b>Поиск по тексту</b>\n\nОтправьте слово или фразу из текста/подписи поста.')

@dp.message(StateFilter(AdminStates.waiting_for_post_id))
async def process_post_id_search(message: types.Message, state: FSMContext):
    if not is_real_admin(message.from_user.id): return
    value=(message.text or '').strip(); await state.clear()
    if not value.isdigit(): return await message.answer('❌ ID должен быть числом.')
    row=get_post_card(int(value))
    if not row: return await message.answer('❌ Пост с таким ID не найден.', reply_markup=build_post_search_menu())
    await show_lookup_result(message, int(value), 'post_id', 0, value)

@dp.message(StateFilter(AdminStates.waiting_for_channel_message_id))
async def process_channel_message_id_search(message: types.Message, state: FSMContext):
    if not is_real_admin(message.from_user.id): return
    value=(message.text or '').strip(); await state.clear()
    if not value.isdigit(): return await message.answer('❌ Message ID должен быть числом.')
    cursor.execute('SELECT post_id FROM channel_messages WHERE channel_id=? AND message_id=? LIMIT 1',(CHANNEL_ID,int(value)))
    row=cursor.fetchone()
    if not row: return await message.answer('❌ Сообщение канала не найдено в базе.', reply_markup=build_post_search_menu())
    await show_lookup_result(message,row[0],'channel_message',0,value)

@dp.message(StateFilter(AdminStates.waiting_for_post_author))
async def process_post_author_search(message: types.Message, state: FSMContext):
    if not is_real_admin(message.from_user.id): return
    q=(message.text or '').strip().lstrip('@'); await state.clear()
    if not q: return await message.answer('❌ Укажите username или ID.')
    if q.isdigit():
        cursor.execute('SELECT id FROM posts WHERE user_id=? ORDER BY id DESC LIMIT 30',(int(q),))
    else:
        cursor.execute("SELECT p.id FROM posts p JOIN users u ON u.user_id=p.user_id WHERE u.username LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ? ORDER BY p.id DESC LIMIT 30",(f'%{q}%',f'%{q}%',f'%{q}%'))
    rows=cursor.fetchall()
    if not rows: return await message.answer('❌ Посты такого автора не найдены.', reply_markup=build_post_search_menu())
    kb=InlineKeyboardBuilder()
    for (pid,) in rows:
        row=get_post_card(pid); kb.button(text=f'📌 Пост #{pid} · {row[2] if row else "?"}',callback_data=f'post_card_{pid}')
    kb.button(text='🔎 Новый поиск',callback_data='post_search_menu'); kb.adjust(1)
    await message.answer(f'👤 Найдено постов: <b>{len(rows)}</b>',reply_markup=kb.as_markup())

@dp.message(StateFilter(AdminStates.waiting_for_post_text))
async def process_post_text_search(message: types.Message, state: FSMContext):
    if not is_real_admin(message.from_user.id): return
    q=(message.text or '').strip(); await state.clear()
    if len(q)<2: return await message.answer('❌ Слишком короткий запрос.')
    cursor.execute('SELECT id FROM posts WHERE text LIKE ? ORDER BY id DESC LIMIT 30',(f'%{q}%',))
    rows=cursor.fetchall()
    if not rows: return await message.answer('❌ Посты с таким текстом не найдены.', reply_markup=build_post_search_menu())
    kb=InlineKeyboardBuilder()
    for (pid,) in rows:
        row=get_post_card(pid); preview=html.escape((row[3] or '')[:45]) if row else ''
        kb.button(text=f'📌 #{pid} · {preview}',callback_data=f'post_card_{pid}')
    kb.button(text='🔎 Новый поиск',callback_data='post_search_menu'); kb.adjust(1)
    await message.answer(f'📝 Найдено постов: <b>{len(rows)}</b>',reply_markup=kb.as_markup())

@dp.message(StateFilter(AdminStates.waiting_for_photo_search))
async def process_photo_search(message: types.Message, state: FSMContext):
    if not is_real_admin(message.from_user.id): return
    await state.clear()
    if not message.photo: return await message.answer('❌ Отправьте именно фотографию.', reply_markup=build_post_search_menu())
    fid=message.photo[-1].file_id; hv=await calculate_photo_hash(fid)
    if not hv: return await message.answer('❌ Не удалось обработать фотографию.')
    cursor.execute("SELECT file_id,hash_value FROM media_hashes WHERE media_type='photo'")
    best=[]
    for dbfid,dbhv in cursor.fetchall():
        d=hash_distance(hv,dbhv)
        if d<=18:
            cursor.execute('SELECT DISTINCT post_id FROM post_media WHERE file_id=?',(dbfid,))
            best.extend((d,pid) for (pid,) in cursor.fetchall())
    if not best: return await message.answer('❌ Похожие фотографии не найдены.', reply_markup=build_post_search_menu())
    best.sort(key=lambda x:x[0]); seen=set(); kb=InlineKeyboardBuilder(); lines=[]
    for d,pid in best[:15]:
        if pid in seen: continue
        seen.add(pid); pct=max(0,round(100-d*100/128)); lines.append(f'📌 #{pid} · совпадение {pct}%')
        kb.button(text=f'📌 Пост #{pid} · {pct}%',callback_data=f'post_card_{pid}')
    kb.button(text='🔎 Новый поиск',callback_data='post_search_menu'); kb.adjust(1)
    await message.answer('🖼 <b>Кандидаты по фотографии</b>\n\n'+'\n'.join(lines),reply_markup=kb.as_markup())

@dp.callback_query(F.data == 'find_post_link')
async def find_post_link_callback(callback: types.CallbackQuery, state: FSMContext):
    if not is_real_admin(callback.from_user.id): return await callback.answer('🔒 Доступ запрещен.',show_alert=True)
    await state.set_state(AdminStates.waiting_for_post_link)
    await callback.message.answer('🔗 <b>Поиск по ссылке</b>\n\nОтправьте ссылку вида <code>https://t.me/channel/1234</code>.')
    await callback.answer()

@dp.message(StateFilter(AdminStates.waiting_for_post_link))
async def process_post_link(message: types.Message, state: FSMContext):
    if not is_real_admin(message.from_user.id): return
    await state.clear(); import re
    m=re.search(r'(?:https?://)?t\.me/(?:c/(\d+)/(\d+)|([A-Za-z0-9_]+)/([0-9]+))', message.text or '')
    if not m: return await message.answer('❌ Не распознал ссылку. Пример: <code>https://t.me/channel/1234</code>')
    if m.group(1): channel_id=-1000000000000-int(m.group(1)); mid=int(m.group(2))
    else: channel_id=CHANNEL_ID; mid=int(m.group(4))
    cursor.execute('SELECT post_id FROM channel_messages WHERE channel_id=? AND message_id=? LIMIT 1',(channel_id,mid)); row=cursor.fetchone()
    if not row: return await message.answer('❌ Пост по этой ссылке не найден в базе.')
    await show_lookup_result(message,row[0],'channel_link',0,message.text)

@dp.callback_query(F.data.startswith('audit_'))
async def audit_callback(callback:types.CallbackQuery):
    if not is_real_admin(callback.from_user.id): return await callback.answer('🔒 Доступ запрещен.',show_alert=True)
    page=int(callback.data.split('_')[-1]); limit=15; offset=page*limit
    cursor.execute("SELECT admin_id,action,post_id,ticket_id,user_id,details,created_at FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?",(limit,offset))
    rows=cursor.fetchall(); text='📜 <b>Журнал действий</b>\n\n'; kb=InlineKeyboardBuilder()
    for aid,action,pid,tid,uid,details,dt in rows:
        text += f'• {dt} · 👮 <code>{aid}</code> · <b>{html.escape(action)}</b> · пост #{pid or "—"} · тикет #{tid or "—"} · user {uid or "—"}\n'
        if details: text += f'  <i>{html.escape(details[:160])}</i>\n'
    if page: kb.button(text='⬅️',callback_data=f'audit_{page-1}')
    if len(rows)==limit: kb.button(text='➡️',callback_data=f'audit_{page+1}')
    kb.button(text='🔙 Админка',callback_data='back_to_admin'); kb.adjust(2,1)
    await callback.message.edit_text(text if rows else '📜 Журнал пока пуст.',reply_markup=kb.as_markup()); await callback.answer()

@dp.callback_query(F.data.startswith('recent_searches_'))
async def recent_searches_callback(callback:types.CallbackQuery):
    if not is_real_admin(callback.from_user.id): return await callback.answer('🔒 Доступ запрещен.',show_alert=True)
    page=int(callback.data.split('_')[-1]); limit=10; offset=page*limit
    cursor.execute('''SELECT s.post_id,s.method,s.created_at,p.status,u.username,u.first_name FROM search_history s
                      LEFT JOIN posts p ON p.id=s.post_id LEFT JOIN users u ON u.user_id=p.user_id
                      WHERE s.admin_id=? ORDER BY s.id DESC LIMIT ? OFFSET ?''',(callback.from_user.id,limit,offset))
    rows=cursor.fetchall(); kb=InlineKeyboardBuilder(); text='🕘 <b>Последние поиски</b>\n\n'
    for pid,method,dt,st,un,fn in rows:
        name=f'@{un}' if un and un!='None' else fn or 'без username'; text+=f'• <b>#{pid}</b> · {html.escape(name)} · {html.escape(method)} · {dt}\n'; kb.button(text=f'📌 Пост #{pid}',callback_data=f'post_card_{pid}')
    if page: kb.button(text='⬅️',callback_data=f'recent_searches_{page-1}')
    if len(rows)==limit: kb.button(text='➡️',callback_data=f'recent_searches_{page+1}')
    kb.button(text='🔙 Админка',callback_data='back_to_admin'); kb.adjust(1)
    await callback.message.edit_text(text if rows else '🕘 История поисков пуста.',reply_markup=kb.as_markup()); await callback.answer()

@dp.callback_query(F.data.startswith('post_card_'))
async def post_card_callback(callback:types.CallbackQuery):
    if not is_real_admin(callback.from_user.id): return await callback.answer('🔒 Доступ запрещен.',show_alert=True)
    pid=int(callback.data.split('_')[-1]); row=get_post_card(pid)
    if not row: return await callback.answer('Пост не найден.',show_alert=True)
    await callback.message.edit_text(post_card_text(row),reply_markup=build_post_card_kb(pid,row[11],row[1])); audit(callback.from_user.id,'open_post_card',pid,row[11],row[1]); await callback.answer()

@dp.callback_query(F.data == 'find_forwarded_post')
async def find_forwarded_post_callback(callback: types.CallbackQuery, state: FSMContext):
    if not is_real_admin(callback.from_user.id):
        return await callback.answer('🔒 Отказано в доступе.', show_alert=True)
    await state.set_state(AdminStates.waiting_for_forwarded_post)
    await callback.message.answer('🔎 <b>Поиск автора по пересланному посту</b>\n\nПерешлите сюда пост из Telegram-канала. Я сначала найду точное сообщение канала, затем при необходимости попробую поиск по фотографии.\n\nПосле поиска покажу автора, тикет и панель пользователя.')
    await callback.answer()

@dp.message(StateFilter(AdminStates.waiting_for_forwarded_post))
async def process_forwarded_post_lookup(message: types.Message, state: FSMContext):
    if not is_real_admin(message.from_user.id): return
    await state.clear()
    post_id,method,distance=await find_post_by_forwarded_message(message)
    if not post_id:
        return await message.answer('❌ <b>Пост не найден.</b>\n\nПерешлите именно опубликованное сообщение из канала или отправьте его фотографию.')
    await show_lookup_result(message,post_id,method,distance,'forwarded_message')

# --- START & HELP ---
@dp.message(Command("start"))
async def start(message: types.Message):
    if is_banned(message.from_user.id):
        return await message.answer("⚠️ Вы заблокированы в этой предложке.")

    register_user(message.from_user)
    
    is_admin = is_real_admin(message.from_user.id)
    if is_admin and is_admin_active(message.from_user.id):
        await show_admin_panel(message.from_user.id, message=message)
        return

    open_ticket = get_user_open_ticket(message.from_user.id)
    has_active = open_ticket is not None

    await message.answer(
        "👋 Привет! Присылай сюда свой пост (текст, фото или видео).\n\n"
        "⚙️ <b>По умолчанию все посты анонимные.</b>\n"
        "Ты в любой момент можешь выбрать — выложить анонимно или с указанием твоего @username!",
        reply_markup=get_user_reply_kb(has_active)
    )

@dp.message(F.text == "ℹ️ Помощь")
async def help_handler(message: types.Message):
    await message.answer(
        "ℹ️ <b>Как пользоваться предложкой:</b>\n\n"
        "1. Отправьте текст, фото или видео бота.\n"
        "2. Нажмите кнопку <b>«👤 Настройка анонимности»</b>, если хотите указать или скрыть свой юзернейм.\n"
        "3. Вы можете в любой момент изменить пост кнопкой <b>«✏️ Изменить текст/медиа поста»</b>.\n"
        "4. Все сообщения отправляются модераторам в едином тикете.",
        reply_markup=get_user_reply_kb(get_user_open_ticket(message.from_user.id) is not None)
    )

# --- ПЕРЕКЛЮЧЕНИЕ АНОНИМНОСТИ ПОЛЬЗОВАТЕЛЕМ ---
@dp.message(F.text == "👤 Настройка анонимности")
async def btn_user_anon_setting(message: types.Message):
    open_ticket = get_user_open_ticket(message.from_user.id)
    if not open_ticket:
        return await message.answer("ℹ️ У вас нет открытых постов на модерации.", reply_markup=get_user_reply_kb(False))

    tid, pid = open_ticket
    cursor.execute("SELECT is_anonymous FROM posts WHERE id = ?", (pid,))
    p_row = cursor.fetchone()
    is_anon = p_row[0] if p_row else 1

    anon_status_text = "🔒 <b>Анонимный пост</b> (Ваш юзернейм будет скрыт)" if is_anon == 1 else "📛 <b>Публичный пост</b> (Под постом будет указан ваш @username)"

    await message.answer(
        f"⚙️ <b>Настройка анонимности для поста #{pid} (Тикет #{tid}):</b>\n\n"
        f"Текущий режим: {anon_status_text}\n\n"
        f"Нажмите на кнопку ниже, чтобы переключить свой выбор:",
        reply_markup=get_user_anon_kb(pid, is_anon)
    )

@dp.callback_query(F.data.startswith("user_anon_"))
async def user_anon_toggle_callback(callback: types.CallbackQuery):
    data = callback.data.split("_")
    action = data[2] # "on" or "off"
    post_id = int(data[3])

    cursor.execute("SELECT user_id FROM posts WHERE id = ?", (post_id,))
    owner = cursor.fetchone()
    if not owner or owner[0] != callback.from_user.id:
        return await callback.answer("🔒 Это не ваш пост.", show_alert=True)

    new_anon = 1 if action == "on" else 0
    cursor.execute("UPDATE posts SET is_anonymous = ? WHERE id = ?", (new_anon, post_id))
    conn.commit()

    anon_status_text = "🔒 <b>АНОНИМНО</b> (Юзернейм скрыт)" if new_anon == 1 else "📛 <b>ПУБЛИЧНО</b> (Ваш @username будет указан под постом)"

    await callback.message.edit_text(
        f"⚙️ <b>Настройка анонимности обновлена!</b>\n\n"
        f"Ваш выбор: {anon_status_text}\n\n"
        f"<i>Примечание: Модератор видит ваш выбор при публикации.</i>",
        reply_markup=get_user_anon_kb(post_id, new_anon)
    )
    await callback.answer("⚙️ Выбор анонимности сохранен!")

# --- НИЖНИЕ КНОПКИ ПОЛЬЗОВАТЕЛЯ И ИЗМЕНЕНИЕ ПОСТА ---
@dp.message(F.text == "💬 Мой активный тикет")
async def btn_my_ticket(message: types.Message):
    open_ticket = get_user_open_ticket(message.from_user.id)
    if not open_ticket:
        return await message.answer("ℹ️ У вас нет открытых тикетов.", reply_markup=get_user_reply_kb(False))

    tid, pid = open_ticket
    cursor.execute("SELECT text, is_anonymous, status FROM posts WHERE id = ?", (pid,))
    p_row = cursor.fetchone()
    p_text = p_row[0] if p_row else ""
    is_anon = p_row[1] if p_row else 1
    p_status = p_row[2] if p_row else "в обработке"

    anon_str = "🔒 Анонимно" if is_anon == 1 else "📛 Публично"

    await message.answer(
        f"💬 <b>Ваш активный Тикет #{tid}</b>\n"
        f"📝 <b>Статус заявки #{pid}:</b> {p_status}\n"
        f"👤 <b>Ваш выбор анонимности:</b> {anon_str}\n\n"
        f"📄 <b>Текст поста:</b>\n<i>{p_text or 'Медиафайл'}</i>",
        reply_markup=get_user_reply_kb(True)
    )

@dp.message(F.text == "✏️ Изменить текст/медиа поста")
@dp.message(F.text == "✍️ Отправить пост")
async def btn_edit_or_send_post(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id):
        return await message.answer("⚠️ Вы заблокированы.")

    open_ticket = get_user_open_ticket(message.from_user.id)
    if open_ticket:
        tid, pid = open_ticket
        await state.set_state(UserStates.waiting_for_edit_post)
        await state.update_data(ticket_id=tid, post_id=pid)
        await message.answer(
            f"✏️ <b>Отправьте новый пост (текст, фото или видео) для Тикета #{tid}:</b>\n\n"
            f"Текущее содержимое поста будет заменено на новое!",
            reply_markup=get_user_reply_kb(True)
        )
    else:
        await message.answer("📝 Жду ваш новый пост! Отправьте текст, фото или видео следующим сообщением.")

@dp.message(StateFilter(UserStates.waiting_for_edit_post))
async def process_post_edit(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    post_id = data.get("post_id")
    await state.clear()

    text_content = message.text or message.caption or ""
    media_type = "text"
    file_id = None

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id

    cursor.execute("UPDATE posts SET text = ?, file_id = ?, media_type = ?, status = 'pending' WHERE id = ?",
                   (text_content, file_id, media_type, post_id))
    cursor.execute("DELETE FROM post_media WHERE post_id = ?", (post_id,))
    if file_id:
        cursor.execute("INSERT INTO post_media (post_id, file_id, media_type) VALUES (?, ?, ?)", (post_id, file_id, media_type))
    conn.commit()

    log_ticket_message(ticket_id, "user", message.from_user.id, message.from_user.full_name, f"[ИЗМЕНЕН ПОСТ]: {text_content}", media_type, file_id)

    await message.answer(
        f"✅ <b>Пост в Тикете #{ticket_id} успешно обновлен!</b>",
        reply_markup=get_user_reply_kb(True)
    )

    user_link = f"<a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a>"
    username = f" (@{message.from_user.username})" if message.from_user.username else ""
    ticket_label = get_ticket_label(
        message.from_user.username, message.from_user.first_name,
        media_type, text_content, ticket_id
    )

    admin_caption = (
        f"✏️ <b>АВТОР ИЗМЕНИЛ ПОСТ В ТИКЕТЕ</b>\n"
        f"🏷 <b>{ticket_label}</b>\n\n"
        f"{text_content}\n\n"
        f"👤 <b>Автор:</b> {user_link}{username}\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"🏷 <b>Тикет:</b> {ticket_label} | 📝 <b>Заявка #{post_id}</b>"
    )

    all_admins = list(set(ADMIN_IDS + VIP_ADMIN_IDS))
    for admin_id in all_admins:
        try:
            if media_type == "photo":
                sent_msg = await bot.send_photo(admin_id, photo=file_id, caption=admin_caption, reply_markup=get_admin_kb(post_id, message.from_user.id, ticket_id, 1))
            elif media_type == "video":
                sent_msg = await bot.send_video(admin_id, video=file_id, caption=admin_caption, reply_markup=get_admin_kb(post_id, message.from_user.id, ticket_id, 1))
            else:
                sent_msg = await bot.send_message(admin_id, text=admin_caption, reply_markup=get_admin_kb(post_id, message.from_user.id, ticket_id, 1))

            if sent_msg:
                save_admin_msg_mapping(admin_id, sent_msg.message_id, message.from_user.id)
        except Exception as e:
            logging.error(f"Ошибка уведомления админа {admin_id}: {e}")

# --- ЗАЩИЩЕННЫЕ АДМИН-КОМАНДЫ И РЕЖИМЫ ---
@dp.message(Command("admin", "stats", "usermode", "adminmode", "getdb", "excel"))
async def protected_commands(message: types.Message, state: FSMContext):
    if not is_real_admin(message.from_user.id):
        # Не раскрываем посторонним существование админских функций.
        return
    await state.clear()
    command = message.text.split()[0].split("@")[0].lower()

    if command == "/usermode":
        admin_in_user_mode.add(message.from_user.id)
        active_admin_chats.pop(message.from_user.id, None)
        return await message.answer(
            "👤 <b>Режим пользователя включен.</b>\n"
            "Теперь вы можете пользоваться предложкой как обычный пользователь.",
            reply_markup=get_user_reply_kb(get_user_open_ticket(message.from_user.id) is not None)
        )

    if command in ("/admin", "/stats", "/adminmode"):
        admin_in_user_mode.discard(message.from_user.id)
        if command == "/adminmode":
            await message.answer("🛡 <b>Режим администратора включен.</b>")
        return await show_admin_panel(message.from_user.id, message=message)

    if command == "/getdb":
        return await export_db(message.from_user.id)

    if command == "/excel":
        return await export_excel(message.from_user.id)


# --- КНОПКИ АДМИНА ВНИЗУ ЧАТА ---
@dp.message(F.text == "📊 Админ панель")
async def btn_admin_panel(message: types.Message):
    await show_admin_panel(message.from_user.id, message=message)

@dp.message(F.text == "📥 Активные тикеты")
async def btn_active_tickets(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id not in VIP_ADMIN_IDS:
        return
    await list_tickets_impl(message=message)

@dp.message(F.text == "📅 Отложенные посты")
async def btn_scheduled_posts(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id not in VIP_ADMIN_IDS:
        return
    await list_scheduled_impl(message=message)

@dp.message(F.text == "📋 Список банов")
async def btn_ban_list(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id not in VIP_ADMIN_IDS:
        return
    await view_banlist_impl(message=message)

@dp.message(F.text == "🚪 Выйти из чата")
async def btn_exit_chat(message: types.Message):
    admin_id = message.from_user.id
    if admin_id in active_admin_chats:
        tid = active_admin_chats.pop(admin_id)
        await message.answer(f"🚪 Вы вышли из чата Тикета #{tid}.", reply_markup=get_admin_reply_kb(in_chat=False))
    else:
        await message.answer("ℹ️ Вы не находились в режиме чата.", reply_markup=get_admin_reply_kb(in_chat=False))

@dp.message(F.text == "🚀 Опубликовать пост")
async def btn_pub_chat(message: types.Message):
    admin_id = message.from_user.id
    if admin_id not in active_admin_chats:
        return await message.answer("⚠️ Вы не находитесь в режиме прямого чата!")

    ticket_id = active_admin_chats[admin_id]
    cursor.execute("SELECT post_id, user_id FROM tickets WHERE id = ?", (ticket_id,))
    row = cursor.fetchone()
    if not row:
        return await message.answer("❌ Тикет не найден.")

    post_id, user_id = row
    await show_publish_preview(admin_id, post_id, user_id)

@dp.message(F.text == "❌ Отклонить пост")
async def btn_rej_chat(message: types.Message):
    await cmd_rej_active_ticket(message)

# --- ПЕРЕКЛЮЧЕНИЕ АНОНИМНОСТИ В АДМИНКЕ ---
@dp.callback_query(F.data.startswith("toggleanon_"))
async def admin_toggle_anon_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    data = callback.data.split("_")
    is_confirm_mode = (data[1] == "confirm")
    
    if is_confirm_mode:
        post_id, user_id = int(data[2]), int(data[3])
    else:
        post_id, user_id = int(data[1]), int(data[2])

    cursor.execute("SELECT is_anonymous FROM posts WHERE id = ?", (post_id,))
    p_row = cursor.fetchone()
    cur_anon = p_row[0] if p_row else 1

    new_anon = 0 if cur_anon == 1 else 1
    cursor.execute("UPDATE posts SET is_anonymous = ? WHERE id = ?", (new_anon, post_id))
    conn.commit()

    if is_confirm_mode:
        await show_publish_preview(callback.from_user.id, post_id, user_id, message_to_edit=callback.message)
    else:
        cursor.execute("SELECT id FROM tickets WHERE post_id = ?", (post_id,))
        t_row = cursor.fetchone()
        ticket_id = t_row[0] if t_row else None
        try:
            await callback.message.edit_reply_markup(reply_markup=get_admin_kb(post_id, user_id, ticket_id, new_anon))
        except Exception:
            pass

    anon_str = "👤 Теперь: АНОНИМНО" if new_anon == 1 else "📛 Теперь: ПУБЛИЧНО"
    await callback.answer(f"⚙️ {anon_str}")

# --- ПРЕДПРОСМОТР ПЕРЕД ПУБЛИКАЦИЕЙ ---
async def show_publish_preview(admin_id: int, post_id: int, user_id: int, message_to_edit: types.Message = None):
    cursor.execute("SELECT text, file_id, media_type, is_anonymous FROM posts WHERE id = ?", (post_id,))
    res = cursor.fetchone()

    if not res:
        return await bot.send_message(admin_id, "⚠️ Пост не найден.")

    original_text, file_id, media_type, is_anon = res

    final_preview_text = build_final_post_text(original_text, user_id, is_anon)
    anon_label = "👤 Анонимно (юзернейм скрыт)" if is_anon == 1 else "📛 Публично (юзернейм указан)"

    header_info = (
        f"👁 <b>ПРЕДПРОСМОТР ПОСТА ПЕРЕД ПУБЛИКАЦИЕЙ В КАНАЛ</b>\n\n"
        f"📌 <b>Режим автора:</b> {anon_label}\n"
        f"────────────────────\n\n"
    )

    full_message_text = f"{header_info}{final_preview_text}"

    reply_markup = get_publish_confirm_kb(post_id, user_id, is_anon)

    try:
        if message_to_edit:
            await message_to_edit.edit_text(full_message_text, reply_markup=reply_markup)
        else:
            if media_type == "photo":
                await bot.send_photo(admin_id, photo=file_id, caption=full_message_text, reply_markup=reply_markup)
            elif media_type == "video":
                await bot.send_video(admin_id, video=file_id, caption=full_message_text, reply_markup=reply_markup)
            else:
                await bot.send_message(admin_id, text=full_message_text, reply_markup=reply_markup)
    except Exception as e:
        await bot.send_message(admin_id, full_message_text, reply_markup=reply_markup)

@dp.callback_query(F.data.startswith("prepub_"))
async def prepub_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    data = callback.data.split("_")
    post_id, user_id = int(data[1]), int(data[2])
    await show_publish_preview(callback.from_user.id, post_id, user_id)
    await callback.answer()

@dp.callback_query(F.data.startswith("cancelprepub_"))
async def cancel_prepub_callback(callback: types.CallbackQuery):
    if not is_real_admin(callback.from_user.id):
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)
    await callback.message.edit_text("❌ Предпросмотр публикации отменен.")
    await callback.answer("Отменено")

# --- РЕДАКТИРОВАНИЕ ТЕКСТА ПОСТА АДМИНОМ ПЕРЕД ВЫКЛАДКОЙ ---
@dp.callback_query(F.data.startswith("adminedittext_"))
async def admin_edit_text_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    data = callback.data.split("_")
    post_id, user_id = int(data[1]), int(data[2])

    await state.set_state(AdminStates.waiting_for_edited_text)
    await state.update_data(post_id=post_id, user_id=user_id)

    cursor.execute("SELECT text FROM posts WHERE id = ?", (post_id,))
    p_row = cursor.fetchone()
    cur_text = p_row[0] if p_row else ""

    await callback.message.reply(
        f"✏️ <b>Введите новый текст поста:</b>\n\n"
        f"Текущий текст:\n<code>{cur_text or 'Без текста'}</code>"
    )
    await callback.answer()

@dp.message(StateFilter(AdminStates.waiting_for_edited_text))
async def process_admin_text_edit(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id not in VIP_ADMIN_IDS:
        return

    data = await state.get_data()
    post_id = data.get("post_id")
    user_id = data.get("user_id")
    await state.clear()

    new_text = message.text or message.caption or ""
    cursor.execute("UPDATE posts SET text = ? WHERE id = ?", (new_text, post_id))
    conn.commit()

    await message.reply("✅ Текст поста обновлен! Показываем обновленный предпросмотр:")
    await show_publish_preview(message.from_user.id, post_id, user_id)

# --- ПОДТВЕРЖДЕНИЕ И ОКОНЧАТЕЛЬНАЯ ПУБЛИКАЦИЯ В КАНАЛ ---
@dp.callback_query(F.data.startswith("confirmpub_"))
async def confirm_publish_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    data = callback.data.split("_")
    post_id, user_id = int(data[1]), int(data[2])

    cursor.execute("SELECT status, text, file_id, media_type, is_anonymous FROM posts WHERE id = ?", (post_id,))
    res = cursor.fetchone()

    if not res:
        return await callback.answer("⚠️ Пост не найден.", show_alert=True)

    status, original_text, file_id, media_type, is_anon = res

    if status != "pending":
        return await callback.answer(f"⚠️ Этот пост уже обработан! Статус: {status}", show_alert=True)

    final_text = build_final_post_text(original_text, user_id, is_anon)

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

            sent_channel_messages = await bot.send_media_group(CHANNEL_ID, media=media_group)
        else:
            if media_type == "photo":
                sent_channel_messages = await bot.send_photo(CHANNEL_ID, file_id, caption=final_text)
            elif media_type == "video":
                sent_channel_messages = await bot.send_video(CHANNEL_ID, file_id, caption=final_text)
            else:
                sent_channel_messages = await bot.send_message(CHANNEL_ID, final_text)

        await register_channel_messages(post_id, sent_channel_messages)
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
                "✅ Твой пост успешно опубликован в канале!",
                reply_markup=get_user_reply_kb(False)
            )
        except Exception:
            pass

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.message.reply("🚀 Пост успешно опубликован в канале! Тикет закрыт.", reply_markup=get_admin_reply_kb(in_chat=False))
        await callback.answer("🚀 Пост опубликован!")

    except Exception as e:
        await callback.answer(f"Ошибка при публикации: {e}", show_alert=True)

# --- РЕЖИМ ЧАТА И КОМАНДЫ ---
@dp.message(Command("chat"))
async def cmd_chat(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id not in VIP_ADMIN_IDS:
        return

    args = message.text.split()
    if len(args) < 2:
        return await message.answer("⚠️ Использование: <code>/chat 12</code>")

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
            f"Все ваши обычные сообщения теперь уходят пользователю анонимно.",
            reply_markup=get_admin_reply_kb(in_chat=True)
        )
    except ValueError:
        await message.answer("⚠️ Номер тикета должен состоять из цифр.")

@dp.message(Command("exit"))
async def cmd_exit_chat(message: types.Message):
    admin_id = message.from_user.id
    if admin_id in active_admin_chats:
        tid = active_admin_chats.pop(admin_id)
        await message.answer(f"🚪 Вы вышли из чата Тикета #{tid}.", reply_markup=get_admin_reply_kb(in_chat=False))
    else:
        await message.answer("ℹ️ Вы не находились в режиме прямого чата.", reply_markup=get_admin_reply_kb(in_chat=False))

@dp.callback_query(F.data.startswith("exitchat_"))
async def callback_exit_chat(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    if admin_id in active_admin_chats:
        tid = active_admin_chats.pop(admin_id)
        await callback.message.answer(f"🚪 Вы вышли из чата Тикета #{tid}.", reply_markup=get_admin_reply_kb(in_chat=False))

    await show_admin_panel(admin_id, callback=callback)

@dp.message(Command("pub"))
@dp.message(Command("publish"))
async def cmd_pub_active_ticket(message: types.Message):
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
    await show_publish_preview(admin_id, post_id, user_id)

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

# --- ОТЛОЖЕННЫЕ ПОСТЫ И СПИСКИ ---
async def list_scheduled_impl(callback: types.CallbackQuery = None, message: types.Message = None):
    cursor.execute('''SELECT s.id, s.post_id, s.publish_time, p.text, p.media_type 
                      FROM scheduled_posts s 
                      JOIN posts p ON s.post_id = p.id 
                      WHERE p.status = 'scheduled' 
                      ORDER BY s.publish_time ASC''')
    sched_list = cursor.fetchall()

    builder = InlineKeyboardBuilder()

    if not sched_list:
        builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
        msg_text = "📅 <b>Запланированных постов нет!</b>"
        if callback:
            await callback.message.edit_text(msg_text, reply_markup=builder.as_markup())
            return await callback.answer()
        elif message:
            return await message.answer(msg_text, reply_markup=builder.as_markup())

    text = "📅 <b>Список отложенных постов:</b>\n\n"

    for sid, pid, ptime, ptext, mtype in sched_list:
        time_str = datetime.fromtimestamp(ptime).strftime("%d.%m %H:%M")
        preview = (ptext[:25] + "...") if ptext else f"[{mtype.upper()}]"
        text += f"• ⏱ <b>{time_str}</b> | Пост #{pid}\n   └ <i>{preview}</i>\n\n"
        builder.button(text=f"⏱ {time_str} (Пост #{pid})", callback_data=f"viewsched_{sid}")

    builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    builder.adjust(1)

    if callback:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        await callback.answer()
    elif message:
        await message.answer(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "list_scheduled")
async def list_scheduled_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)
    await list_scheduled_impl(callback=callback)

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

    cursor.execute('''SELECT s.post_id, p.user_id, p.text, p.file_id, p.media_type, p.is_anonymous 
                      FROM scheduled_posts s 
                      JOIN posts p ON s.post_id = p.id 
                      WHERE s.id = ?''', (sched_id,))
    item = cursor.fetchone()

    if not item:
        return await callback.answer("⚠️ Запись не найдена.", show_alert=True)

    post_id, user_id, original_text, file_id, media_type, is_anon = item
    final_text = build_final_post_text(original_text, user_id, is_anon)

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
            sent_channel_messages = await bot.send_media_group(CHANNEL_ID, media=media_group)
        else:
            if media_type == "photo":
                sent_channel_messages = await bot.send_photo(CHANNEL_ID, file_id, caption=final_text)
            elif media_type == "video":
                sent_channel_messages = await bot.send_video(CHANNEL_ID, file_id, caption=final_text)
            else:
                sent_channel_messages = await bot.send_message(CHANNEL_ID, final_text)

        await register_channel_messages(post_id, sent_channel_messages)
        cursor.execute("UPDATE posts SET status = 'published' WHERE id = ?", (post_id,))
        cursor.execute("DELETE FROM scheduled_posts WHERE id = ?", (sched_id,))
        conn.commit()

        try:
            await bot.send_message(
                user_id,
                "✅ Твой пост опубликован в канале!",
                reply_markup=get_user_reply_kb(False)
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
    cleanup_ticket_statuses()
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

async def show_admin_panel(user_id: int, message: types.Message = None, callback: types.CallbackQuery = None):
    cleanup_ticket_statuses()
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

    cursor.execute("SELECT COUNT(*) FROM tickets t JOIN posts p ON p.id=t.post_id WHERE t.status='open' AND p.status='pending'")
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
        f"🎛 <b>Все важные действия доступны на кнопках ниже!</b>"
    )

    in_chat = user_id in active_admin_chats
    reply_kb = get_admin_reply_kb(in_chat)

    if callback:
        await callback.message.edit_text(stats_text, reply_markup=get_admin_panel_kb())
        await callback.message.answer("🎛 Быстрые кнопки управления внизу:", reply_markup=reply_kb)
        await callback.answer()
    elif message:
        await message.answer(stats_text, reply_markup=get_admin_panel_kb())
        await message.answer("🎛 Быстрые кнопки управления внизу:", reply_markup=reply_kb)

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin_callback(callback: types.CallbackQuery):
    if not is_real_admin(callback.from_user.id):
        return await callback.answer("🔒 Доступ запрещен.", show_alert=True)
    await show_admin_panel(callback.from_user.id, callback=callback)

# --- АРХИВ ЗАКРЫТЫХ ТИКЕТОВ ---
async def show_closed(callback: types.CallbackQuery, page: int = 0):
    cleanup_ticket_statuses()
    if not is_real_admin(callback.from_user.id):
        return await callback.answer("🔒 Доступ запрещен.", show_alert=True)

    limit = 10
    offset = max(page, 0) * limit
    cursor.execute('''SELECT t.id, u.username, u.first_name, p.media_type, p.text,
                      p.status
                      FROM tickets t
                      LEFT JOIN users u ON u.user_id=t.user_id
                      LEFT JOIN posts p ON p.id=t.post_id
                      WHERE t.status='closed'
                      ORDER BY t.id DESC LIMIT ? OFFSET ?''', (limit, offset))
    rows = cursor.fetchall()

    builder = InlineKeyboardBuilder()
    text = "📁 <b>Архив закрытых тикетов</b>\n\n"

    for tid, username, first_name, media_type, post_text, post_status in rows:
        label = get_ticket_label(username, first_name, media_type, post_text, tid)
        icon = "✅" if post_status == "published" else "❌" if post_status == "rejected" else "🏁"
        text += f"{icon} {label}\n"
        builder.button(text=f"{icon} {label}", callback_data=f"open_ticket_{tid}")

    if page:
        builder.button(text="⬅️ Назад", callback_data=f"closed_{page-1}")
    if len(rows) == limit:
        builder.button(text="Вперед ➡️", callback_data=f"closed_{page+1}")
    builder.button(text="🔙 В админку", callback_data="back_to_admin")
    builder.adjust(1)

    await callback.message.edit_text(text if rows else "📁 <b>Архив пуст.</b>",
                                      reply_markup=builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data.in_({"export_excel", "export_db"}))
async def protected_exports(callback: types.CallbackQuery):
    if not is_real_admin(callback.from_user.id):
        return await callback.answer("🔒 Доступ запрещен.", show_alert=True)
    if callback.data == "export_excel":
        await export_excel(callback.from_user.id)
    else:
        await export_db(callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data.startswith("closed_"))
async def closed_callback(callback: types.CallbackQuery):
    if not is_real_admin(callback.from_user.id):
        return await callback.answer("🔒 Доступ запрещен.", show_alert=True)
    try:
        page = int(callback.data.split("_")[1])
    except (ValueError, IndexError):
        return await callback.answer("⚠️ Некорректная страница.", show_alert=True)
    await show_closed(callback, page)


# --- СПИСОК ТИКЕТОВ ---
async def list_tickets_impl(callback: types.CallbackQuery = None, message: types.Message = None):
    cleanup_ticket_statuses()
    cursor.execute('''SELECT t.id, t.user_id, t.post_id, u.username, u.first_name,
                      p.text, p.media_type
                      FROM tickets t
                      LEFT JOIN users u ON t.user_id = u.user_id
                      LEFT JOIN posts p ON t.post_id = p.id
                      WHERE t.status = 'open' AND p.status = 'pending'
                      ORDER BY t.id DESC LIMIT 20''')
    tickets = cursor.fetchall()

    builder = InlineKeyboardBuilder()

    if not tickets:
        builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
        msg_text = "📥 <b>Открытых тикетов нет!</b>"
        if callback:
            await callback.message.edit_text(msg_text, reply_markup=builder.as_markup())
            return await callback.answer()
        elif message:
            return await message.answer(msg_text, reply_markup=builder.as_markup())

    text = "💬 <b>Список активных диалогов (Тикетов):</b>\n\n"

    for tid, uid, pid, uname, first_name, ptext, media_type in tickets:
        label = get_ticket_label(uname, first_name, media_type, ptext, tid)
        text += f"• {label} (Заявка #{pid})\n\n"
        builder.button(text=f"💬 {label}", callback_data=f"open_ticket_{tid}")

    builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    builder.adjust(1)

    if callback:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        await callback.answer()
    elif message:
        await message.answer(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "list_tickets")
async def list_tickets_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)
    await list_tickets_impl(callback=callback)

# --- ПРОСМОТР И УПРАВЛЕНИЕ ТИКЕТОМ ---
@dp.callback_query(F.data.startswith("open_ticket_"))
async def open_ticket_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)

    ticket_id = int(callback.data.split("_")[2])
    audit(callback.from_user.id,'open_ticket',None,ticket_id,None)
    await open_ticket_callback_by_id(callback, ticket_id)

async def open_ticket_callback_by_id(callback: types.CallbackQuery, ticket_id: int):
    cursor.execute('''SELECT t.id, t.user_id, t.post_id, t.status, u.username, p.text, p.status, p.is_anonymous 
                      FROM tickets t 
                      LEFT JOIN users u ON t.user_id = u.user_id 
                      LEFT JOIN posts p ON t.post_id = p.id 
                      WHERE t.id = ?''', (ticket_id,))
    ticket = cursor.fetchone()

    if not ticket:
        return await callback.answer("⚠️ Тикет не найден.", show_alert=True)

    tid, uid, pid, t_status, uname, ptext, p_status, is_anon = ticket
    user_mention = f"@{uname}" if uname and uname != "None" else f"ID <code>{uid}</code>"
    anon_str = "🔒 Анонимно" if is_anon == 1 else "📛 Публично"

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
        f"⚙️ <b>Выбор анонимности:</b> {anon_str}\n"
        f"📝 <b>Заявка:</b> #{pid} (Статус: {p_status})\n"
        f"📌 <b>Статус тикета:</b> {'🟢 Открыт' if t_status == 'open' else '🔴 Закрыт'}"
        f"{history_str}"
    )

    builder = InlineKeyboardBuilder()
    if t_status == 'open':
        builder.button(text="🚀 Опубликовать (Предпросмотр)", callback_data=f"prepub_{pid}_{uid}")
        builder.button(text="💬 Войти в режим чата", callback_data=f"enter_chat_{tid}")
        builder.button(text="❌ Отклонить пост", callback_data=f"rej_{pid}_{uid}")

    builder.button(text="👤 Профиль пользователя", callback_data=f"user_profile_{uid}")
    builder.button(text="📄 Открыть полную историю HTML", callback_data=f"ticket_html_{tid}")
    builder.button(text="📦 Скачать тикет ZIP", callback_data=f"ticket_zip_{tid}")
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
        f"Все ваши сообщения внизу уходят пользователю анонимно.\n"
        f"Кнопки управления всегда под рукой на клавиатуре внизу!",
        reply_markup=get_active_chat_kb(ticket_id, post_id, user_id)
    )
    await callback.message.answer("💬 Режим чата активен!", reply_markup=get_admin_reply_kb(in_chat=True))
    await callback.answer("💬 Вход в чат выполнен!")

# --- ОБРАБОТКА СООБЩЕНИЙ МОДЕРАТОРА В РЕЖИМЕ ЧАТА ---
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
        return await message.reply("⚠️ Этот тикет уже закрыт. Вы вышли из режима чата.", reply_markup=get_admin_reply_kb(in_chat=False))

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
        await message.reply(f"✅ Сообщение доставлено в Тикет #{ticket_id}!")
    except Exception as e:
        await message.reply(f"❌ Ошибка отправки пользователю: {e}")

# --- ЗАКРЫТИЕ ТИКЕТА И ОТКЛОНЕНИЕ ---
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
    tid_for_audit=find_ticket_for_post(post_id)
    audit(callback.from_user.id if callback else (message.from_user.id if message else 0),'reject_post',post_id,tid_for_audit,user_id)

    for aid, tid in list(active_admin_chats.items()):
        cursor.execute("SELECT post_id FROM tickets WHERE id = ?", (tid,))
        t_post = cursor.fetchone()
        if t_post and t_post[0] == post_id:
            active_admin_chats.pop(aid, None)

    try:
        await bot.send_message(
            user_id,
            "❌ К сожалению, твой пост был отклонен модератором.",
            reply_markup=get_user_reply_kb(False)
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
        await message.reply("❌ Пост отклонен модератором! Тикет закрыт.", reply_markup=get_admin_reply_kb(in_chat=False))

@dp.callback_query(F.data.startswith("rej_"))
async def reject_post(callback: types.CallbackQuery):
    if not is_real_admin(callback.from_user.id):
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)
    data = callback.data.split("_")
    post_id, user_id = int(data[1]), int(data[2])
    await reject_post_by_id(post_id, user_id, callback=callback)

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
    if not is_real_admin(callback.from_user.id):
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)
    await state.clear()
    data = callback.data.split("_")
    post_id, user_id = data[1], data[2]

    cursor.execute("SELECT id FROM tickets WHERE user_id = ? AND post_id = ? AND status = 'open'", (user_id, post_id))
    t_row = cursor.fetchone()
    ticket_id = t_row[0] if t_row else None

    cursor.execute("SELECT is_anonymous FROM posts WHERE id = ?", (post_id,))
    p_row = cursor.fetchone()
    is_anon = p_row[0] if p_row else 1

    await callback.message.edit_reply_markup(reply_markup=get_admin_kb(post_id, user_id, ticket_id, is_anon))
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
    if not is_real_admin(callback.from_user.id):
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)
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
            cursor.execute('''SELECT s.id, s.post_id, p.user_id, p.text, p.file_id, p.media_type, p.is_anonymous 
                              FROM scheduled_posts s 
                              JOIN posts p ON s.post_id = p.id 
                              WHERE s.publish_time <= ? AND p.status = 'scheduled' ''', (now,))
            due_posts = cursor.fetchall()

            for sched_id, post_id, user_id, original_text, file_id, media_type, is_anon in due_posts:
                final_text = build_final_post_text(original_text, user_id, is_anon)

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
                        sent_channel_messages = await bot.send_media_group(CHANNEL_ID, media=media_group)
                    else:
                        if media_type == "photo":
                            sent_channel_messages = await bot.send_photo(CHANNEL_ID, file_id, caption=final_text)
                        elif media_type == "video":
                            sent_channel_messages = await bot.send_video(CHANNEL_ID, file_id, caption=final_text)
                        else:
                            sent_channel_messages = await bot.send_message(CHANNEL_ID, final_text)

                    await register_channel_messages(post_id, sent_channel_messages)
                    cursor.execute("UPDATE posts SET status = 'published' WHERE id = ?", (post_id,))
                    cursor.execute("DELETE FROM scheduled_posts WHERE id = ?", (sched_id,))
                    conn.commit()

                    try:
                        await bot.send_message(
                            user_id,
                            "✅ Твой запланированный пост опубликован в канале!",
                            reply_markup=get_user_reply_kb(False)
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

    active_ticket = get_user_open_ticket(user.id)

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

        await first_msg.answer("💬 Ваше сообщение с альбомом добавлено в текущий диалог!", reply_markup=get_user_reply_kb(True))
        return

    cursor.execute(
        "INSERT INTO posts (user_id, status, text, file_id, media_type, media_group_id, is_anonymous) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (user.id, "pending", text_content, messages[0].photo[-1].file_id if messages[0].photo else messages[0].video.file_id, "album", mg_id)
    )
    post_id = cursor.lastrowid

    for m in messages:
        m_type = "photo" if m.photo else "video"
        f_id = m.photo[-1].file_id if m.photo else m.video.file_id
        cursor.execute("INSERT INTO post_media (post_id, file_id, media_type) VALUES (?, ?, ?)", (post_id, f_id, m_type))
        if m_type == 'photo':
            await index_photo_file(f_id, 'photo')

    cursor.execute("INSERT INTO tickets (user_id, post_id, status) VALUES (?, ?, 'open')", (user.id, post_id))
    ticket_id = cursor.lastrowid
    conn.commit()

    log_ticket_message(ticket_id, "user", user.id, user.full_name, text_content or "[Альбом фотографий]", "album", None)
    ticket_label = get_ticket_label(
        user.username, user.first_name, "album",
        text_content or "[Альбом фотографий]", ticket_id
    )

    await first_msg.answer(
        f"🚀 <b>Альбом отправлен на модерацию!</b>\n"
        f"🏷 <b>{ticket_label}</b>\n\n"
        f"Вы можете настроить отображение вашего @username кнопкой «👤 Настройка анонимности» в меню ниже.",
        reply_markup=get_user_reply_kb(True)
    )

    user_link = f"<a href='tg://user?id={user.id}'>{user.full_name}</a>"
    username = f" (@{user.username})" if user.username else " (нет юзернейма)"

    admin_caption = (
        f"{text_content}\n\n"
        f"🖼 <b>Альбом из {len(messages)} медиафайлов</b>\n"
        f"👤 <b>Автор:</b> {user_link}{username}\n"
        f"⚙️ Выбор автора: 🔒 Анонимно\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"🏷 <b>Тикет:</b> {ticket_label} | 📝 <b>Заявка #{post_id}</b>"
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
                reply_markup=get_admin_kb(post_id, user.id, ticket_id, 1)
            )
            save_admin_msg_mapping(admin_id, ctrl_msg.message_id, user.id)
            for sm in sent_msgs:
                save_admin_msg_mapping(admin_id, sm.message_id, user.id)

        except Exception as e:
            logging.error(f"Не удалось отправить альбом админу {admin_id}: {e}")

# --- ПОИСК ПО ФОТО ---
@dp.message(F.chat.type == 'private', F.photo)
async def admin_photo_search(message: types.Message):
    admin_id = message.from_user.id
    if not is_real_admin(admin_id) or admin_id in active_admin_chats:
        return
    query_file_id = message.photo[-1].file_id
    query_hash = await index_photo_file(query_file_id, 'photo')
    if not query_hash:
        return await message.answer('❌ Не удалось обработать изображение для поиска.')

    # Если это старые данные до появления индекса, автоматически достраиваем индекс.
    cursor.execute("SELECT COUNT(*) FROM post_media pm LEFT JOIN media_hashes mh ON mh.file_id=pm.file_id WHERE pm.media_type='photo' AND mh.file_id IS NULL")
    missing_index = cursor.fetchone()[0]
    if missing_index:
        await index_all_photos(min(5000, missing_index))

    # Сначала точное совпадение Telegram file_id, затем похожие изображения по хешу.
    cursor.execute('''SELECT DISTINCT p.id,t.id,p.user_id,p.status,p.text,pm.file_id
                      FROM post_media pm
                      JOIN posts p ON p.id=pm.post_id
                      LEFT JOIN tickets t ON t.post_id=p.id
                      WHERE pm.media_type='photo' AND pm.file_id=?
                      ORDER BY p.id DESC LIMIT 10''', (query_file_id,))
    exact = cursor.fetchall()

    cursor.execute('''SELECT pm.file_id,mh.hash_value,p.id,t.id,p.user_id,p.status,p.text
                      FROM media_hashes mh
                      JOIN post_media pm ON pm.file_id=mh.file_id AND pm.media_type='photo'
                      JOIN posts p ON p.id=pm.post_id
                      LEFT JOIN tickets t ON t.post_id=p.id
                      WHERE mh.hash_value IS NOT NULL''')
    candidates=[]
    for fid,hv,pid,tid,uid,pstatus,ptext in cursor.fetchall():
        dist=hash_distance(query_hash,hv)
        if dist <= 20:
            candidates.append((dist,pid,tid,uid,pstatus,ptext,fid))
    candidates.sort(key=lambda x:(x[0],-x[1]))

    seen=set(); matches=[]
    for row in exact:
        key=row[0]
        if key not in seen:
            seen.add(key); matches.append((0,)+row)
    for row in candidates:
        key=row[1]
        if key not in seen:
            seen.add(key); matches.append(row)
        if len(matches)>=10: break

    if not matches:
        return await message.answer('🔎 <b>Совпадений не найдено.</b>\n\nПопробуй отправить оригинал фото или более полный фрагмент изображения.')

    text='🔎 <b>Результаты поиска по фото</b>\n\n'
    for idx,row in enumerate(matches[:10],1):
        if row[0]==0:
            _,pid,tid,uid,pstatus,ptext,fid=row
            distance='точное совпадение'
        else:
            distance,pid,tid,uid,pstatus,ptext,fid=row
            distance=f'похожесть: {max(0,100-distance*3)}%'
        cursor.execute('SELECT username,first_name,last_name FROM users WHERE user_id=?',(uid,))
        u=cursor.fetchone() or (None,None,None)
        uname=('@'+u[0]) if u[0] and u[0] != 'None' else ' '.join(x for x in [u[1],u[2]] if x) or f'ID {uid}'
        text += f'<b>{idx}. Пост #{pid}</b> · Тикет #{tid or "—"} · {distance}\n👤 {html.escape(uname)} · ID <code>{uid}</code>\n📌 Статус: {html.escape(str(pstatus))}\n'
        if ptext: text += f'📝 {html.escape(ptext[:100])}\n'
        text += '\n'
    await message.answer(text)
    # Отправляем найденные фотографии, чтобы модератор сразу визуально сопоставил источник.
    for row in matches[:10]:
        if row[0]==0:
            _,pid,tid,uid,pstatus,ptext,fid=row
        else:
            _,pid,tid,uid,pstatus,ptext,fid=row
        caption=f'📸 Пост #{pid} | Тикет #{tid or "—"} | ID автора: {uid}\nСтатус: {pstatus}'
        try:
            await bot.send_photo(admin_id, fid, caption=caption)
        except Exception:
            pass

@dp.message(Command('indexphotos'))
async def cmd_index_photos(message: types.Message):
    if not is_real_admin(message.from_user.id): return
    await message.answer('🔄 Индексирую фотографии. Это может занять время...')
    done,total=await index_all_photos()
    await message.answer(f'✅ Индексация завершена: обработано {done} из {total} новых фото.')

# --- ПРИЕМ ПРЕДЛОЖЕНИЙ И СООБЩЕНИЙ ОТ ПОЛЬЗОВАТЕЛЕЙ ---
@dp.message(F.chat.type == "private")
async def handle_suggestion(message: types.Message):
    if is_banned(message.from_user.id):
        return await message.answer("⚠️ Вы заблокированы в этой предложке.")

    register_user(message.from_user)

    if message.text and message.text.startswith("/"):
        return

    active_ticket = get_user_open_ticket(message.from_user.id)

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

        await message.answer("💬 Ваше сообщение добавлено в текущий диалог с администрацией!", reply_markup=get_user_reply_kb(True))
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
        "INSERT INTO posts (user_id, status, text, file_id, media_type, is_anonymous) VALUES (?, ?, ?, ?, ?, 1)",
        (message.from_user.id, "pending", text_content, file_id, media_type)
    )
    post_id = cursor.lastrowid

    if file_id:
        cursor.execute("INSERT INTO post_media (post_id, file_id, media_type) VALUES (?, ?, ?)", (post_id, file_id, media_type))
        if media_type == 'photo':
            await index_photo_file(file_id, 'photo')

    cursor.execute("INSERT INTO tickets (user_id, post_id, status) VALUES (?, ?, 'open')",
                   (message.from_user.id, post_id))
    ticket_id = cursor.lastrowid
    conn.commit()

    log_ticket_message(ticket_id, "user", message.from_user.id, message.from_user.full_name, text_content, media_type, file_id)
    ticket_label = get_ticket_label(
        message.from_user.username, message.from_user.first_name,
        media_type, text_content, ticket_id
    )

    await message.answer(
        f"🚀 <b>Пост отправлен на модерацию!</b>\n"
        f"🏷 <b>{ticket_label}</b>\n\n"
        f"Вы можете настроить отображение вашего @username кнопкой «👤 Настройка анонимности» в меню ниже.",
        reply_markup=get_user_reply_kb(True)
    )

    user_link = f"<a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a>"
    username = f" (@{message.from_user.username})" if message.from_user.username else " (нет юзернейма)"

    admin_caption = (
        f"{text_content}\n\n"
        f"👤 <b>Автор:</b> {user_link}{username}\n"
        f"⚙️ Выбор автора: 🔒 Анонимно (по умолчанию)\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"🏷 <b>Тикет:</b> {ticket_label} | 📝 <b>Заявка #{post_id}</b>"
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
                    reply_markup=get_admin_kb(post_id, message.from_user.id, ticket_id, 1)
                )
            elif media_type == "video":
                sent_msg = await bot.send_video(
                    chat_id=admin_id,
                    video=file_id,
                    caption=admin_caption,
                    reply_markup=get_admin_kb(post_id, message.from_user.id, ticket_id, 1)
                )
            else:
                sent_msg = await bot.send_message(
                    chat_id=admin_id,
                    text=admin_caption,
                    reply_markup=get_admin_kb(post_id, message.from_user.id, ticket_id, 1)
                )

            if sent_msg:
                save_admin_msg_mapping(admin_id, sent_msg.message_id, message.from_user.id)

        except Exception as e:
            logging.error(f"Не удалось отправить админу {admin_id}: {e}")

# --- БАНЫ И РАЗБАНЫ ---
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
    await callback.answer("🚫 Пользователь забанен!")

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
            await bot.send_message(target_id, "🔓 Администратор разблокировал вас в предложке.", reply_markup=get_user_reply_kb(False))
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
        await bot.send_message(target_id, "🔓 Администратор разблокировал вас в предложке.", reply_markup=get_user_reply_kb(False))
    except Exception:
        pass

    await callback.answer("🔓 Пользователь разблокирован!")
    await view_banlist_impl(callback=callback)

async def view_banlist_impl(callback: types.CallbackQuery = None, message: types.Message = None):
    cursor.execute("SELECT user_id, username FROM banned_users LIMIT 30")
    banned = cursor.fetchall()

    builder = InlineKeyboardBuilder()

    if not banned:
        builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
        msg_text = "📝 <b>Список блокировок пуст!</b>"
        if callback:
            await callback.message.edit_text(msg_text, reply_markup=builder.as_markup())
            return await callback.answer()
        elif message:
            return await message.answer(msg_text, reply_markup=builder.as_markup())

    text = "📋 <b>Список заблокированных пользователей:</b>\n\nНажмите на кнопку ниже, чтобы разбанить:\n\n"

    for uid, uname in banned:
        mention = f"@{uname}" if uname and uname != "None" else f"ID: {uid}"
        text += f"• {mention} (<code>{uid}</code>)\n"
        builder.button(text=f"🔓 Разбанить {uname if uname and uname != 'None' else uid}", callback_data=f"unb_{uid}")

    builder.button(text="🔙 Назад в админку", callback_data="back_to_admin")
    builder.adjust(1)

    if callback:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        await callback.answer()
    elif message:
        await message.answer(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "view_banlist")
async def view_banlist_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS and callback.from_user.id not in VIP_ADMIN_IDS:
        return await callback.answer("🔒 Отказано в доступе.", show_alert=True)
    await view_banlist_impl(callback=callback)

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
        "Отправьте мне сообщение (текст, фото или видео) для рассылки пользователям.",
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
        f"Все ваши сообщения уходят пользователю анонимно.",
        reply_markup=get_active_chat_kb(ticket_id, post_id, user_id)
    )
    await callback.message.answer("💬 Режим прямого чата активен!", reply_markup=get_admin_reply_kb(in_chat=True))
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
    asyncio.create_task(automatic_backup_loop())

    logging.info(f"Bot polling started on port {PORT}...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
