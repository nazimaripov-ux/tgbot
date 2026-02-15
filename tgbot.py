import asyncio
import json
import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import FSInputFile

from states import AddNote, EditNote, Broadcast


# =======================
# !!! ВСТАВЬ СВОЙ ТОКЕН !!!
# =======================
TOKEN = "8500198434:AAGSDnBPVVyBV-FTcIUHYHOXv04oBWBlR7A"

# =======================
# OWNER — только он может добавлять/удалять админов
# =======================
OWNER_ID = 8214818758  # <-- твой user_id (только ты можешь управлять админами)

BASE_DIR = Path(__file__).resolve().parent
STORAGE_FILE = BASE_DIR / "storage.json"

# MP3 файл для кнопки "БАОБАБ"
BAOBAB_MP3 = BASE_DIR / "baobab.mp3"  # положи сюда mp3 с таким именем

SECTIONS = {
    "SQL": {
        "🖥 Функции": "functions",
        "🖥 Агрегатные функции": "aggregates",
        "🖥 Запросы": "queries",
        "🖥 Запросы на Редактирование": "notes",
    },
    "Tips": {
        "Общие": "general"
    }
}

# ---------- storage ----------
def default_data():
    return {
        "SQL": {"functions": [], "aggregates": [], "queries": [], "notes": []},
        "Tips": {"general": []},
        "DevOps и Сети": {"general": []},
        "_subscribers": [],      # chat_id для рассылки
        "_admins": [OWNER_ID],   # список админов (owner по умолчанию админ)
    }

def load_data():
    try:
        if not STORAGE_FILE.exists():
            return default_data()
        txt = STORAGE_FILE.read_text(encoding="utf-8").strip()
        if not txt:
            return default_data()
        data = json.loads(txt)

        base = default_data()
        for cat, subs in base.items():
            data.setdefault(cat, subs)
            if isinstance(subs, dict):
                for k, v in subs.items():
                    data[cat].setdefault(k, v)

        data.setdefault("_subscribers", [])
        if not isinstance(data["_subscribers"], list):
            data["_subscribers"] = []

        data.setdefault("_admins", [OWNER_ID])
        if not isinstance(data["_admins"], list):
            data["_admins"] = [OWNER_ID]

        # гарантируем, что OWNER_ID всегда админ
        if OWNER_ID not in data["_admins"]:
            data["_admins"].append(OWNER_ID)

        return data
    except (json.JSONDecodeError, OSError):
        return default_data()

def save_data(data):
    STORAGE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def add_subscriber(data, chat_id: int):
    subs = data.setdefault("_subscribers", [])
    if chat_id not in subs:
        subs.append(chat_id)

def remove_subscriber(data, chat_id: int):
    subs = data.setdefault("_subscribers", [])
    if chat_id in subs:
        subs.remove(chat_id)

def get_admin_ids(data) -> set[int]:
    admins = data.get("_admins", [])
    if not isinstance(admins, list):
        admins = []
    admins_set = set()
    for x in admins:
        try:
            admins_set.add(int(x))
        except Exception:
            pass
    admins_set.add(OWNER_ID)
    return admins_set

def is_owner(user_id: int | None) -> bool:
    return user_id == OWNER_ID

def is_admin_user(data, user_id: int | None) -> bool:
    return bool(user_id) and (user_id in get_admin_ids(data))

def add_admin(data, user_id: int) -> bool:
    admins = data.setdefault("_admins", [])
    if user_id not in admins:
        admins.append(user_id)
        return True
    return False

def remove_admin(data, user_id: int) -> bool:
    # owner нельзя удалить
    if user_id == OWNER_ID:
        return False
    admins = data.setdefault("_admins", [])
    if user_id in admins:
        admins.remove(user_id)
        return True
    return False

# ---------- logging / admin notify ----------
def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_user_action(event: str, user_id: int | None = None, chat_id: int | None = None, extra: str = ""):
    s = f"[{now_str()}] {event}"
    if user_id is not None:
        s += f" user_id={user_id}"
    if chat_id is not None:
        s += f" chat_id={chat_id}"
    if extra:
        s += f" | {extra}"
    print(s, flush=True)

async def notify_admins(bot: Bot, admin_ids: set[int], text: str):
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except (TelegramForbiddenError, TelegramBadRequest):
            continue
        except Exception:
            continue

# ---------- middleware: log everything + mirror to admins ----------
class AuditMiddleware(BaseMiddleware):
    def __init__(self, bot: Bot, get_admins_callable: Callable[[], set[int]]):
        self.bot = bot
        self.get_admins_callable = get_admins_callable

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        try:
            admin_ids = self.get_admins_callable()

            if isinstance(event, Message):
                uid = event.from_user.id if event.from_user else None
                uname = f"@{event.from_user.username}" if (event.from_user and event.from_user.username) else "—"
                full_name = event.from_user.full_name if event.from_user else "—"

                log_user_action(
                    "MSG",
                    user_id=uid,
                    chat_id=event.chat.id,
                    extra=f"name={full_name} username={uname} text={repr(event.text)} content_type={event.content_type}"
                )

                # не шлём копию админам, если сообщение от админа
                if uid not in admin_ids:
                    msg_preview = event.text if event.text else f"<{event.content_type}>"
                    admin_text = (
                        "📩 Сообщение в бот\n"
                        f"👤 {full_name} {uname}\n"
                        f"🆔 user_id: {uid}\n"
                        f"💬 chat_id: {event.chat.id}\n"
                        f"📝 {msg_preview}"
                    )
                    await notify_admins(self.bot, admin_ids, admin_text)

            if isinstance(event, CallbackQuery):
                uid = event.from_user.id if event.from_user else None
                uname = f"@{event.from_user.username}" if (event.from_user and event.from_user.username) else "—"
                full_name = event.from_user.full_name if event.from_user else "—"
                chat_id = event.message.chat.id if event.message else None

                log_user_action(
                    "CALLBACK",
                    user_id=uid,
                    chat_id=chat_id,
                    extra=f"name={full_name} username={uname} data={repr(event.data)}"
                )

                if uid not in admin_ids:
                    admin_text = (
                        "🧷 Нажатие кнопки\n"
                        f"👤 {full_name} {uname}\n"
                        f"🆔 user_id: {uid}\n"
                        f"📌 data: {event.data}"
                    )
                    await notify_admins(self.bot, admin_ids, admin_text)

        except Exception as e:
            print(f"[{now_str()}] AUDIT_MW_ERROR: {e}", flush=True)

        return await handler(event, data)

# ---------- reply bottom navigation ----------
def kb_bottom(user_is_admin: bool) -> ReplyKeyboardMarkup:
    row1 = [KeyboardButton(text="🏠 Меню"), KeyboardButton(text="📚 Категории")]
    row2 = [KeyboardButton(text="📣 Рассылка")] if user_is_admin else [KeyboardButton(text="ℹ️ Помощь")]
    row3 = [KeyboardButton(text="🚫 Отписаться")]
    return ReplyKeyboardMarkup(
        keyboard=[row1, row2, row3],
        resize_keyboard=True,
        is_persistent=True
    )

# ---------- inline keyboards ----------
def kb_main(data):
    kb = InlineKeyboardBuilder()
    for cat in data.keys():
        if cat.startswith("_"):
            continue
        kb.button(text=f"📂 {cat}", callback_data=f"cat:{cat}")

    # кнопка "БАОБАБ" в главном меню
    kb.button(text="🌳 БАОБАБ", callback_data="baobab")

    kb.adjust(1)
    return kb.as_markup()

def kb_cat(cat: str, user_is_admin: bool):
    kb = InlineKeyboardBuilder()
    for title, key in SECTIONS.get(cat, {}).items():
        kb.button(text=title, callback_data=f"list:{cat}:{key}")
    if user_is_admin:
        kb.button(text="➕ Добавить", callback_data=f"add:{cat}")
    kb.button(text="🏠 Главное меню", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()

def kb_choose_section(cat: str):
    kb = InlineKeyboardBuilder()
    for title, key in SECTIONS.get(cat, {}).items():
        kb.button(text=title, callback_data=f"addsec:{cat}:{key}")
    kb.button(text="🔙 Назад", callback_data=f"cat:{cat}")
    kb.adjust(1)
    return kb.as_markup()

def kb_list(cat: str, subcat: str, notes: list[dict]):
    kb = InlineKeyboardBuilder()
    for i, note in enumerate(notes):
        kb.button(text=note["title"], callback_data=f"show:{cat}:{subcat}:{i}")
    kb.button(text="🔙 К разделам", callback_data=f"cat:{cat}")
    kb.adjust(1)
    return kb.as_markup()

def kb_note(cat: str, subcat: str, idx: int, user_is_admin: bool):
    kb = InlineKeyboardBuilder()
    if user_is_admin:
        kb.button(text="✏️ Заголовок", callback_data=f"edit_title:{cat}:{subcat}:{idx}")
        kb.button(text="📝 Текст", callback_data=f"edit_content:{cat}:{subcat}:{idx}")
        kb.button(text="🗑 Удалить", callback_data=f"del:{cat}:{subcat}:{idx}")
    kb.button(text="🔙 К списку", callback_data=f"list:{cat}:{subcat}")
    kb.button(text="🔙 К разделам", callback_data=f"cat:{cat}")
    kb.adjust(2, 1, 2)
    return kb.as_markup()

def kb_del_confirm(cat: str, subcat: str, idx: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"del_yes:{cat}:{subcat}:{idx}")
    kb.button(text="❌ Отмена", callback_data=f"show:{cat}:{subcat}:{idx}")
    kb.adjust(1)
    return kb.as_markup()

# ---------- render ----------
def render_note(cat: str, title: str, content: str) -> str:
    if cat == "SQL":
        return f"*{title}*\n\n```sql\n{content}\n```"
    return f"*{title}*\n\n{content}"

# ---------- bot ----------
async def main():
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    data = load_data()

    def admins_now() -> set[int]:
        return get_admin_ids(data)

    # middleware
    dp.message.middleware(AuditMiddleware(bot, admins_now))
    dp.callback_query.middleware(AuditMiddleware(bot, admins_now))

    async def deny_if_not_admin(c_or_m):
        uid = c_or_m.from_user.id if c_or_m.from_user else None
        if not is_admin_user(data, uid):
            if isinstance(c_or_m, Message):
                await c_or_m.answer("⛔ Это действие доступно только админу.")
            else:
                await c_or_m.answer("⛔ Только админ.", show_alert=True)
            return True
        return False

    # ---------- commands ----------
    @dp.message(CommandStart())
    async def start(m: Message):
        add_subscriber(data, m.chat.id)
        save_data(data)

        user_is_admin = is_admin_user(data, m.from_user.id if m.from_user else None)
        log_user_action("START", user_id=m.from_user.id, chat_id=m.chat.id)

        await m.answer(
            "📚 *База знаний*\nВыбери категорию:",
            reply_markup=kb_main(data),
            parse_mode="Markdown"
        )
        await m.answer("⬇️ Навигация:", reply_markup=kb_bottom(user_is_admin))

    @dp.message(Command("unsubscribe"))
    async def unsubscribe(m: Message):
        remove_subscriber(data, m.chat.id)
        save_data(data)
        log_user_action("UNSUBSCRIBE", user_id=m.from_user.id, chat_id=m.chat.id)
        await m.answer("✅ Ты отписался от рассылки.")

    # ===== OWNER ONLY: управление админами =====
    @dp.message(Command("admins"))
    async def admins_list(m: Message):
        uid = m.from_user.id if m.from_user else None
        if not is_owner(uid):
            return await m.answer("⛔ Команда доступна только владельцу бота.")
        admins = sorted(list(get_admin_ids(data)))
        text = "👑 *Админы бота*\n\n" + "\n".join([f"- `{a}`" for a in admins])
        await m.answer(text, parse_mode="Markdown")

    @dp.message(Command("addadmin"))
    async def admin_add(m: Message):
        uid = m.from_user.id if m.from_user else None
        if not is_owner(uid):
            return await m.answer("⛔ Команда доступна только владельцу бота.")

        parts = (m.text or "").split()
        if len(parts) != 2:
            return await m.answer("Использование: `/addadmin 123456789`", parse_mode="Markdown")

        try:
            new_id = int(parts[1])
        except ValueError:
            return await m.answer("⛔ user_id должен быть числом.")

        if add_admin(data, new_id):
            save_data(data)
            await m.answer(f"✅ Добавил админа: `{new_id}`", parse_mode="Markdown")
            log_user_action("ADMIN_ADD", user_id=uid, chat_id=m.chat.id, extra=f"new_admin={new_id}")
        else:
            await m.answer("ℹ️ Этот user_id уже админ.")

    @dp.message(Command("deladmin"))
    async def admin_del(m: Message):
        uid = m.from_user.id if m.from_user else None
        if not is_owner(uid):
            return await m.answer("⛔ Команда доступна только владельцу бота.")

        parts = (m.text or "").split()
        if len(parts) != 2:
            return await m.answer("Использование: `/deladmin 123456789`", parse_mode="Markdown")

        try:
            del_id = int(parts[1])
        except ValueError:
            return await m.answer("⛔ user_id должен быть числом.")

        if del_id == OWNER_ID:
            return await m.answer("⛔ Владельца нельзя удалить из админов.")

        if remove_admin(data, del_id):
            save_data(data)
            await m.answer(f"✅ Удалил админа: `{del_id}`", parse_mode="Markdown")
            log_user_action("ADMIN_DEL", user_id=uid, chat_id=m.chat.id, extra=f"del_admin={del_id}")
        else:
            await m.answer("ℹ️ Этого user_id нет в админах.")

    # ===== Broadcast (admins) =====
    @dp.message(Command("broadcast"))
    async def broadcast_start(m: Message, state: FSMContext):
        if not is_admin_user(data, m.from_user.id if m.from_user else None):
            log_user_action("BROADCAST_DENY", user_id=m.from_user.id, chat_id=m.chat.id)
            return await m.answer("⛔ Команда доступна только админу.")

        await state.clear()
        await state.set_state(Broadcast.text)
        log_user_action("BROADCAST_START", user_id=m.from_user.id, chat_id=m.chat.id)
        await m.answer("📣 Введи текст для рассылки всем подписчикам.\n\n/cancel — отмена")

    @dp.message(Command("cancel"))
    async def cancel_any(m: Message, state: FSMContext):
        await state.clear()
        log_user_action("CANCEL", user_id=m.from_user.id, chat_id=m.chat.id)
        await m.answer("✅ Отменено.")

    @dp.message(Broadcast.text)
    async def broadcast_send(m: Message, state: FSMContext):
        if not is_admin_user(data, m.from_user.id if m.from_user else None):
            await state.clear()
            log_user_action("BROADCAST_DENY_STATE", user_id=m.from_user.id, chat_id=m.chat.id)
            return await m.answer("⛔ Команда доступна только админу.")

        text = m.text or ""
        subs = list(data.get("_subscribers", []))

        if not subs:
            await state.clear()
            log_user_action("BROADCAST_EMPTY", user_id=m.from_user.id, chat_id=m.chat.id)
            return await m.answer("📭 Подписчиков нет.")

        sent = 0
        failed = 0

        log_user_action("BROADCAST_SENDING", user_id=m.from_user.id, chat_id=m.chat.id, extra=f"targets={len(subs)}")
        await m.answer(f"⏳ Начинаю рассылку: {len(subs)} чатов...")

        for chat_id in subs:
            try:
                await bot.send_message(chat_id, text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1

        await state.clear()
        log_user_action("BROADCAST_DONE", user_id=m.from_user.id, chat_id=m.chat.id, extra=f"sent={sent} failed={failed}")
        await m.answer(f"✅ Готово.\nОтправлено: {sent}\nОшибок: {failed}")

    # ---------- bottom nav (reply keyboard) ----------
    @dp.message(F.text == "🏠 Меню")
    async def bottom_menu(m: Message):
        user_is_admin = is_admin_user(data, m.from_user.id if m.from_user else None)
        await m.answer("📚 *База знаний*\nВыбери категорию:", reply_markup=kb_main(data), parse_mode="Markdown")
        await m.answer("⬇️ Навигация:", reply_markup=kb_bottom(user_is_admin))

    @dp.message(F.text == "📚 Категории")
    async def bottom_categories(m: Message):
        user_is_admin = is_admin_user(data, m.from_user.id if m.from_user else None)
        await m.answer("📚 *Категории*:", reply_markup=kb_main(data), parse_mode="Markdown")
        await m.answer("⬇️ Навигация:", reply_markup=kb_bottom(user_is_admin))

    @dp.message(F.text == "📣 Рассылка")
    async def bottom_broadcast(m: Message, state: FSMContext):
        if not is_admin_user(data, m.from_user.id if m.from_user else None):
            return await m.answer("⛔ Только админ.")
        await state.clear()
        await state.set_state(Broadcast.text)
        await m.answer("📣 Введи текст для рассылки всем подписчикам.\n\n/cancel — отмена")

    @dp.message(F.text == "🚫 Отписаться")
    async def bottom_unsub(m: Message):
        remove_subscriber(data, m.chat.id)
        save_data(data)
        await m.answer("✅ Ты отписался от рассылки.")

    @dp.message(F.text == "ℹ️ Помощь")
    async def bottom_help(m: Message):
        await m.answer(
            "Команды:\n"
            "/start — меню\n"
            "/unsubscribe — отписка\n\n"
            "Админ:\n"
            "/broadcast — рассылка\n"
            "/cancel — отмена\n\n"
            "Владелец (только ты):\n"
            "/admins — список админов\n"
            "/addadmin <id> — добавить админа\n"
            "/deladmin <id> — удалить админа"
        )

    # ---------- callbacks ----------
    @dp.callback_query(F.data == "home")
    async def home(c: CallbackQuery):
        await c.message.edit_text(
            "📚 *База знаний*\nВыбери категорию:",
            reply_markup=kb_main(data),
            parse_mode="Markdown"
        )
        await c.answer()

    @dp.callback_query(F.data == "baobab")
    async def baobab_send(c: CallbackQuery):
        # Отправка mp3
        try:
            if not BAOBAB_MP3.exists():
                await c.message.answer("❌ Файл `baobab.mp3` не найден рядом с ботом.")
            else:
                audio = FSInputFile(str(BAOBAB_MP3))
                await c.message.answer_audio(audio=audio, caption="🌳 БАОБАБ")
        except Exception as e:
            await c.message.answer(f"❌ Не смог отправить mp3: {e}")
        await c.answer()

    @dp.callback_query(F.data.startswith("cat:"))
    async def open_cat(c: CallbackQuery):
        cat = c.data.split(":")[1]
        user_is_admin = is_admin_user(data, c.from_user.id if c.from_user else None)
        await c.message.edit_text(
            f"📂 *{cat}*\nВыбери раздел:",
            reply_markup=kb_cat(cat, user_is_admin),
            parse_mode="Markdown"
        )
        await c.answer()

    @dp.callback_query(F.data.startswith("list:"))
    async def list_notes(c: CallbackQuery):
        _, cat, subcat = c.data.split(":")
        notes = data.get(cat, {}).get(subcat, [])
        await c.message.edit_text(
            f"📜 *{cat} / {subcat}*\n\n" + ("_Пока нет заметок_" if not notes else "Выбери заметку:"),
            reply_markup=kb_list(cat, subcat, notes),
            parse_mode="Markdown"
        )
        await c.answer()

    @dp.callback_query(F.data.startswith("show:"))
    async def show_note(c: CallbackQuery):
        _, cat, subcat, idx = c.data.split(":")
        idx = int(idx)
        note = data[cat][subcat][idx]
        user_is_admin = is_admin_user(data, c.from_user.id if c.from_user else None)

        await c.message.answer(
            render_note(cat, note["title"], note["content"]),
            reply_markup=kb_note(cat, subcat, idx, user_is_admin),
            parse_mode="Markdown"
        )
        await c.answer()

    # ---- add (ADMIN ONLY) ----
    @dp.callback_query(F.data.startswith("add:"))
    async def add_choose(c: CallbackQuery, state: FSMContext):
        if await deny_if_not_admin(c):
            return
        cat = c.data.split(":")[1]
        await state.clear()
        await state.update_data(cat=cat)
        await state.set_state(AddNote.choose_section)
        log_user_action("ADD_CHOOSE", user_id=c.from_user.id, chat_id=c.message.chat.id, extra=f"cat={cat}")
        await c.message.answer("➕ В какой раздел добавить?", reply_markup=kb_choose_section(cat))
        await c.answer()

    @dp.callback_query(F.data.startswith("addsec:"))
    async def add_section(c: CallbackQuery, state: FSMContext):
        if await deny_if_not_admin(c):
            return
        _, cat, subcat = c.data.split(":")
        await state.update_data(cat=cat, subcat=subcat)
        await state.set_state(AddNote.title)
        log_user_action("ADD_SECTION", user_id=c.from_user.id, chat_id=c.message.chat.id, extra=f"cat={cat} subcat={subcat}")
        await c.message.answer("✏️ Введи *заголовок*:", parse_mode="Markdown")
        await c.answer()

    @dp.message(AddNote.title)
    async def add_title(m: Message, state: FSMContext):
        if not is_admin_user(data, m.from_user.id if m.from_user else None):
            await state.clear()
            return await m.answer("⛔ Только админ может добавлять заметки.")
        await state.update_data(title=m.text)
        await state.set_state(AddNote.content)
        log_user_action("ADD_TITLE", user_id=m.from_user.id, chat_id=m.chat.id, extra=f"title={m.text}")
        await m.answer("📝 Введи *текст/SQL*:", parse_mode="Markdown")

    @dp.message(AddNote.content)
    async def add_content(m: Message, state: FSMContext):
        if not is_admin_user(data, m.from_user.id if m.from_user else None):
            await state.clear()
            return await m.answer("⛔ Только админ может добавлять заметки.")
        st = await state.get_data()
        cat, subcat, title = st["cat"], st["subcat"], st["title"]

        data.setdefault(cat, {})
        data[cat].setdefault(subcat, [])
        data[cat][subcat].append({"title": title, "content": m.text})
        save_data(data)
        await state.clear()

        log_user_action("ADD_SAVED", user_id=m.from_user.id, chat_id=m.chat.id, extra=f"cat={cat} subcat={subcat} title={title}")
        await m.answer("✅ Сохранено")

    # ---- delete (ADMIN ONLY) ----
    @dp.callback_query(F.data.startswith("del:"))
    async def del_ask(c: CallbackQuery):
        if await deny_if_not_admin(c):
            return
        _, cat, subcat, idx = c.data.split(":")
        idx = int(idx)
        title = data[cat][subcat][idx]["title"]
        log_user_action("DEL_ASK", user_id=c.from_user.id, chat_id=c.message.chat.id, extra=f"cat={cat} subcat={subcat} idx={idx} title={title}")
        await c.message.edit_text(f"🗑 Удалить *{title}*?", reply_markup=kb_del_confirm(cat, subcat, idx), parse_mode="Markdown")
        await c.answer()

    @dp.callback_query(F.data.startswith("del_yes:"))
    async def del_yes(c: CallbackQuery):
        if await deny_if_not_admin(c):
            return
        _, cat, subcat, idx = c.data.split(":")
        idx = int(idx)
        deleted = data[cat][subcat].pop(idx)
        save_data(data)

        log_user_action("DEL_DONE", user_id=c.from_user.id, chat_id=c.message.chat.id, extra=f"cat={cat} subcat={subcat} idx={idx} title={deleted.get('title')}")
        notes = data[cat][subcat]
        await c.message.edit_text(
            "✅ Удалено.\n\n" + ("_Пока нет заметок_" if not notes else "Выбери заметку:"),
            reply_markup=kb_list(cat, subcat, notes),
            parse_mode="Markdown"
        )
        await c.answer()

    # ---- edit title (ADMIN ONLY) ----
    @dp.callback_query(F.data.startswith("edit_title:"))
    async def edit_title_start(c: CallbackQuery, state: FSMContext):
        if await deny_if_not_admin(c):
            return
        _, cat, subcat, idx = c.data.split(":")
        await state.clear()
        await state.update_data(cat=cat, subcat=subcat, idx=int(idx))
        await state.set_state(EditNote.title)
        log_user_action("EDIT_TITLE_START", user_id=c.from_user.id, chat_id=c.message.chat.id, extra=f"cat={cat} subcat={subcat} idx={idx}")
        await c.message.answer("✏️ Введи *новый заголовок*:", parse_mode="Markdown")
        await c.answer()

    @dp.message(EditNote.title)
    async def edit_title_finish(m: Message, state: FSMContext):
        if not is_admin_user(data, m.from_user.id if m.from_user else None):
            await state.clear()
            return await m.answer("⛔ Только админ может редактировать заметки.")
        st = await state.get_data()
        cat, subcat, idx = st["cat"], st["subcat"], st["idx"]

        old = data[cat][subcat][idx]["title"]
        data[cat][subcat][idx]["title"] = m.text
        save_data(data)
        await state.clear()

        log_user_action("EDIT_TITLE_DONE", user_id=m.from_user.id, chat_id=m.chat.id, extra=f"cat={cat} subcat={subcat} idx={idx} old={old} new={m.text}")
        await m.answer("✅ Заголовок обновлён")

    # ---- edit content (ADMIN ONLY) ----
    @dp.callback_query(F.data.startswith("edit_content:"))
    async def edit_content_start(c: CallbackQuery, state: FSMContext):
        if await deny_if_not_admin(c):
            return
        _, cat, subcat, idx = c.data.split(":")
        await state.clear()
        await state.update_data(cat=cat, subcat=subcat, idx=int(idx))
        await state.set_state(EditNote.content)
        log_user_action("EDIT_CONTENT_START", user_id=c.from_user.id, chat_id=c.message.chat.id, extra=f"cat={cat} subcat={subcat} idx={idx}")
        await c.message.answer("📝 Введи *новый текст/SQL*:", parse_mode="Markdown")
        await c.answer()

    @dp.message(EditNote.content)
    async def edit_content_finish(m: Message, state: FSMContext):
        if not is_admin_user(data, m.from_user.id if m.from_user else None):
            await state.clear()
            return await m.answer("⛔ Только админ может редактировать заметки.")
        st = await state.get_data()
        cat, subcat, idx = st["cat"], st["subcat"], st["idx"]

        data[cat][subcat][idx]["content"] = m.text
        save_data(data)
        await state.clear()

        log_user_action("EDIT_CONTENT_DONE", user_id=m.from_user.id, chat_id=m.chat.id, extra=f"cat={cat} subcat={subcat} idx={idx}")
        await m.answer("✅ Текст обновлён")

    print(">>> ПОШЛА ЖАРА <<<", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
