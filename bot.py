import asyncio
import json
import logging
import os
import random
import time
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("meme-sticker-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]

BASE_REPLY_CHANCE = float(os.environ.get("BASE_REPLY_CHANCE", "0.1"))
MAX_REPLY_CHANCE  = float(os.environ.get("MAX_REPLY_CHANCE",  "0.4"))
ACTIVITY_THRESHOLD = int(os.environ.get("ACTIVITY_THRESHOLD", "8"))
ACTIVITY_WINDOW    = int(os.environ.get("ACTIVITY_WINDOW",    "60"))
STICKER_COOLDOWN   = 0.001  # 1 мс — фактически выключен

STICKERS_FILE = os.path.join(os.path.dirname(__file__), "stickers.json")

# { chat_id: deque of timestamps }
chat_activity: dict[int, deque] = defaultdict(deque)
chat_last_sticker: dict[int, float] = {}

# { chat_id: { "enabled": bool, "base_chance": float } }
chat_settings: dict[int, dict] = {}

# { user_id: { "duels": int, "wins": int, "slots": int } }
user_stats: dict[int, dict] = defaultdict(lambda: {"duels": 0, "wins": 0, "slots": 0})

# Активные дуэли: { chat_id: { "challenger_id": int, "challenger_name": str, "message_id": int, "ts": float } }
active_duels: dict[int, dict] = {}

DUEL_TIMEOUT = 60  # секунд чтобы принять дуэль


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_chat_setting(chat_id, key, default):
    return chat_settings.get(chat_id, {}).get(key, default)

def set_chat_setting(chat_id, key, value):
    if chat_id not in chat_settings:
        chat_settings[chat_id] = {}
    chat_settings[chat_id][key] = value

def get_activity_level(chat_id: int) -> float:
    now = time.time()
    h = chat_activity[chat_id]
    h.append(now)
    while h and now - h[0] > ACTIVITY_WINDOW:
        h.popleft()
    return min(len(h) / ACTIVITY_THRESHOLD, 1.0)

def get_reply_chance(chat_id: int) -> float:
    base  = get_chat_setting(chat_id, "base_chance", BASE_REPLY_CHANCE)
    level = get_activity_level(chat_id)
    return base + (MAX_REPLY_CHANCE - base) * level

def is_on_cooldown(chat_id: int) -> bool:
    return (time.time() - chat_last_sticker.get(chat_id, 0)) < STICKER_COOLDOWN

def update_cooldown(chat_id: int):
    chat_last_sticker[chat_id] = time.time()

def load_stickers() -> list[str]:
    if not os.path.exists(STICKERS_FILE):
        return []
    with open(STICKERS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("file_ids", [])

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

def slot_result(score: int) -> str:
    if score >= 90: return "🍒🍒🍒"
    if score >= 70: return "🍋🍋🍋"
    if score >= 50: return "🍊🍊🍊"
    if score >= 30: return "🍇🍇🍇"
    return "💀💀💀"


# ---------------------------------------------------------------------------
# Bot & Dispatcher
# ---------------------------------------------------------------------------

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

CB_MORE     = "more_sticker"
CB_ROLL     = "roll_slot"
CB_DUEL_ACC = "duel_accept"
CB_DUEL_DEC = "duel_decline"
CB_SETTINGS = "open_settings"
CB_TOG      = "settings_toggle"
CB_UP       = "settings_chance_up"
CB_DOWN     = "settings_chance_down"
CB_CLOSE    = "settings_close"


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎰", callback_data=CB_ROLL),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data=CB_SETTINGS),
        ],
    ])

def more_sticker_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Ещё стикер", callback_data=CB_MORE)]
    ])

def duel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚔️ Принять", callback_data=CB_DUEL_ACC),
            InlineKeyboardButton(text="🏳️ Отказать", callback_data=CB_DUEL_DEC),
        ]
    ])

def settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    enabled    = get_chat_setting(chat_id, "enabled", True)
    chance     = get_chat_setting(chat_id, "base_chance", BASE_REPLY_CHANCE)
    tog_label  = "🔴 Выключить бота" if enabled else "🟢 Включить бота"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tog_label, callback_data=CB_TOG)],
        [
            InlineKeyboardButton(text="➖", callback_data=CB_DOWN),
            InlineKeyboardButton(text=f"🎯 {int(chance*100)}%", callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=CB_UP),
        ],
        [InlineKeyboardButton(text="✅ Закрыть", callback_data=CB_CLOSE)],
    ])

def settings_text(chat_id: int) -> str:
    enabled = get_chat_setting(chat_id, "enabled", True)
    chance  = get_chat_setting(chat_id, "base_chance", BASE_REPLY_CHANCE)
    return (
        f"⚙️ <b>Настройки бота</b>\n\n"
        f"Статус: {'🟢 Включён' if enabled else '🔴 Выключен'}\n"
        f"Базовый шанс стикера: <b>{int(chance*100)}%</b>"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@dp.message(Command("start", "menu"))
async def cmd_start(message: Message):
    await message.answer("🎰", reply_markup=main_menu_keyboard())


@dp.message(Command("me"))
async def cmd_me(message: Message):
    """Личная статистика пользователя."""
    u    = message.from_user
    stat = user_stats[u.id]
    wr   = (stat["wins"] / stat["duels"] * 100) if stat["duels"] else 0
    text = (
        f"👤 <b>{u.full_name}</b>\n\n"
        f"🎰 Прокруток слота: <b>{stat['slots']}</b>\n"
        f"⚔️ Дуэлей сыграно: <b>{stat['duels']}</b>\n"
        f"🏆 Побед: <b>{stat['wins']}</b>\n"
        f"📊 Винрейт: <b>{wr:.0f}%</b>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("duel"))
async def cmd_duel(message: Message):
    """Вызов на дуэль. Использование: /duel @username"""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply("Дуэли только в группах! ⚔️")
        return

    chat_id = message.chat.id

    # Проверяем нет ли уже активной дуэли
    if chat_id in active_duels:
        if time.time() - active_duels[chat_id]["ts"] < DUEL_TIMEOUT:
            await message.reply("В чате уже идёт дуэль! Подождите.")
            return
        else:
            del active_duels[chat_id]

    challenger    = message.from_user
    challenger_name = challenger.full_name

    msg = await message.answer(
        f"⚔️ <b>{challenger_name}</b> вызывает на дуэль!\n\n"
        f"Кто примет вызов? У вас есть {DUEL_TIMEOUT} секунд.",
        parse_mode="HTML",
        reply_markup=duel_keyboard(),
    )

    active_duels[chat_id] = {
        "challenger_id":   challenger.id,
        "challenger_name": challenger_name,
        "message_id":      msg.message_id,
        "ts":              time.time(),
    }


@dp.message(Command("sticker"))
async def cmd_sticker(message: Message):
    stickers = load_stickers()
    if not stickers:
        await message.reply("Список стикеров пуст.")
        return
    await message.answer_sticker(random.choice(stickers), reply_markup=more_sticker_keyboard())


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply("Только для групп.")
        return
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы.")
        return
    await message.answer(
        settings_text(message.chat.id),
        parse_mode="HTML",
        reply_markup=settings_keyboard(message.chat.id),
    )


@dp.message(Command("getid"))
async def cmd_getid(message: Message):
    if message.reply_to_message and message.reply_to_message.sticker:
        fid = message.reply_to_message.sticker.file_id
        await message.reply(f"file_id:\n<code>{fid}</code>", parse_mode="HTML")
    else:
        await message.reply("Ответь на стикер командой /getid.")


# ---------------------------------------------------------------------------
# Callbacks — слот и стикеры
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == CB_ROLL)
async def on_roll(callback: CallbackQuery):
    score = random.randint(0, 100)
    user_stats[callback.from_user.id]["slots"] += 1
    await callback.message.answer(
        f"🎰 {slot_result(score)}\n"
        f"<b>{callback.from_user.full_name}</b>: {score}/100",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == CB_MORE)
async def on_more_sticker(callback: CallbackQuery):
    stickers = load_stickers()
    if not stickers:
        await callback.answer("Список стикеров пуст", show_alert=True)
        return
    await callback.message.answer_sticker(random.choice(stickers), reply_markup=more_sticker_keyboard())
    await callback.answer()


# ---------------------------------------------------------------------------
# Callbacks — Дуэль
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == CB_DUEL_ACC)
async def on_duel_accept(callback: CallbackQuery):
    chat_id  = callback.message.chat.id
    acceptor = callback.from_user

    if chat_id not in active_duels:
        await callback.answer("Дуэль уже завершена или истекла.", show_alert=True)
        return

    duel = active_duels[chat_id]

    # Нельзя принять свою же дуэль
    if acceptor.id == duel["challenger_id"]:
        await callback.answer("Нельзя принять собственный вызов! 😅", show_alert=True)
        return

    # Дуэль истекла
    if time.time() - duel["ts"] > DUEL_TIMEOUT:
        del active_duels[chat_id]
        await callback.message.edit_text("⏰ Время дуэли истекло.")
        await callback.answer()
        return

    # Бросаем кубики
    score_c = random.randint(0, 100)
    score_a = random.randint(0, 100)

    challenger_name = duel["challenger_name"]
    acceptor_name   = acceptor.full_name
    challenger_id   = duel["challenger_id"]

    # Обновляем статистику
    user_stats[challenger_id]["duels"] += 1
    user_stats[acceptor.id]["duels"]   += 1

    if score_c > score_a:
        winner = f"🏆 Победил <b>{challenger_name}</b>!"
        user_stats[challenger_id]["wins"] += 1
    elif score_a > score_c:
        winner = f"🏆 Победил <b>{acceptor_name}</b>!"
        user_stats[acceptor.id]["wins"] += 1
    else:
        winner = "🤝 Ничья!"

    result_text = (
        f"⚔️ <b>ДУЭЛЬ</b>\n\n"
        f"{challenger_name}: 🎰 {slot_result(score_c)} — <b>{score_c}</b>\n"
        f"{acceptor_name}: 🎰 {slot_result(score_a)} — <b>{score_a}</b>\n\n"
        f"{winner}"
    )

    del active_duels[chat_id]
    await callback.message.edit_text(result_text, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == CB_DUEL_DEC)
async def on_duel_decline(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in active_duels:
        await callback.answer("Дуэль уже завершена.", show_alert=True)
        return

    duel = active_duels[chat_id]
    if callback.from_user.id == duel["challenger_id"]:
        await callback.answer("Ты не можешь отказать сам себе 😄", show_alert=True)
        return

    del active_duels[chat_id]
    await callback.message.edit_text(
        f"🏳️ <b>{callback.from_user.full_name}</b> отказался от дуэли. Трус!",
        parse_mode="HTML",
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Callbacks — Настройки
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == CB_SETTINGS)
async def on_open_settings(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("⛔ Только для администраторов.", show_alert=True)
        return
    await callback.message.answer(
        settings_text(chat_id),
        parse_mode="HTML",
        reply_markup=settings_keyboard(chat_id),
    )
    await callback.answer()


@dp.callback_query(F.data == CB_TOG)
async def on_toggle(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("⛔ Только для админов", show_alert=True)
        return
    set_chat_setting(chat_id, "enabled", not get_chat_setting(chat_id, "enabled", True))
    await callback.message.edit_text(settings_text(chat_id), parse_mode="HTML",
                                     reply_markup=settings_keyboard(chat_id))
    await callback.answer()


@dp.callback_query(F.data == CB_UP)
async def on_chance_up(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("⛔ Только для админов", show_alert=True)
        return
    val = min(round(get_chat_setting(chat_id, "base_chance", BASE_REPLY_CHANCE) + 0.05, 2), 1.0)
    set_chat_setting(chat_id, "base_chance", val)
    await callback.message.edit_text(settings_text(chat_id), parse_mode="HTML",
                                     reply_markup=settings_keyboard(chat_id))
    await callback.answer(f"Шанс → {int(val*100)}%")


@dp.callback_query(F.data == CB_DOWN)
async def on_chance_down(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("⛔ Только для админов", show_alert=True)
        return
    val = max(round(get_chat_setting(chat_id, "base_chance", BASE_REPLY_CHANCE) - 0.05, 2), 0.0)
    set_chat_setting(chat_id, "base_chance", val)
    await callback.message.edit_text(settings_text(chat_id), parse_mode="HTML",
                                     reply_markup=settings_keyboard(chat_id))
    await callback.answer(f"Шанс → {int(val*100)}%")


@dp.callback_query(F.data == CB_CLOSE)
async def on_close(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery):
    await callback.answer()


# ---------------------------------------------------------------------------
# Auto-sticker
# ---------------------------------------------------------------------------

@dp.message(lambda m: m.sticker is not None)
async def on_sticker_dm(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.reply(f"file_id:\n<code>{message.sticker.file_id}</code>", parse_mode="HTML")


@dp.message()
async def on_any_message(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if message.text and message.text.startswith("/"):
        return
    chat_id = message.chat.id
    if not get_chat_setting(chat_id, "enabled", True):
        return
    stickers = load_stickers()
    if not stickers:
        return
    if is_on_cooldown(chat_id):
        return
    if random.random() < get_reply_chance(chat_id):
        update_cooldown(chat_id)
        await message.answer_sticker(random.choice(stickers), reply_markup=more_sticker_keyboard())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    log.info("Бот запускается. Стикеров: %d", len(load_stickers()))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
