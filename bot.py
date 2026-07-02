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
MAX_REPLY_CHANCE = float(os.environ.get("MAX_REPLY_CHANCE", "0.4"))
ACTIVITY_THRESHOLD = int(os.environ.get("ACTIVITY_THRESHOLD", "8"))
ACTIVITY_WINDOW = int(os.environ.get("ACTIVITY_WINDOW", "60"))
STICKER_COOLDOWN = 0.001  # 1 миллисекунда

STICKERS_FILE = os.path.join(os.path.dirname(__file__), "stickers.json")

chat_activity: dict[int, deque] = defaultdict(deque)
chat_last_sticker: dict[int, float] = {}
chat_settings: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_chat_setting(chat_id: int, key: str, default):
    return chat_settings.get(chat_id, {}).get(key, default)


def set_chat_setting(chat_id: int, key: str, value):
    if chat_id not in chat_settings:
        chat_settings[chat_id] = {}
    chat_settings[chat_id][key] = value


def get_activity_level(chat_id: int) -> float:
    now = time.time()
    history = chat_activity[chat_id]
    history.append(now)
    while history and now - history[0] > ACTIVITY_WINDOW:
        history.popleft()
    return min(len(history) / ACTIVITY_THRESHOLD, 1.0)


def get_reply_chance(chat_id: int) -> float:
    base = get_chat_setting(chat_id, "base_chance", BASE_REPLY_CHANCE)
    level = get_activity_level(chat_id)
    return base + (MAX_REPLY_CHANCE - base) * level


def is_on_cooldown(chat_id: int) -> bool:
    last = chat_last_sticker.get(chat_id, 0)
    return (time.time() - last) < STICKER_COOLDOWN


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
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

MORE_BUTTON_CALLBACK = "more_sticker"
ROLL_CALLBACK = "roll_slot"
SETTINGS_TOGGLE_CALLBACK = "settings_toggle"
SETTINGS_CHANCE_UP = "settings_chance_up"
SETTINGS_CHANCE_DOWN = "settings_chance_down"
SETTINGS_CLOSE = "settings_close"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎰", callback_data=ROLL_CALLBACK)],
        ]
    )


def more_sticker_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Ещё стикер", callback_data=MORE_BUTTON_CALLBACK)]
        ]
    )


def settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    enabled = get_chat_setting(chat_id, "enabled", True)
    chance = get_chat_setting(chat_id, "base_chance", BASE_REPLY_CHANCE)
    toggle_label = "🔴 Выключить бота" if enabled else "🟢 Включить бота"
    chance_pct = int(chance * 100)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_label, callback_data=SETTINGS_TOGGLE_CALLBACK)],
            [
                InlineKeyboardButton(text="➖", callback_data=SETTINGS_CHANCE_DOWN),
                InlineKeyboardButton(text=f"🎯 {chance_pct}%", callback_data="noop"),
                InlineKeyboardButton(text="➕", callback_data=SETTINGS_CHANCE_UP),
            ],
            [InlineKeyboardButton(text="✅ Закрыть", callback_data=SETTINGS_CLOSE)],
        ]
    )


def settings_text(chat_id: int) -> str:
    enabled = get_chat_setting(chat_id, "enabled", True)
    chance = get_chat_setting(chat_id, "base_chance", BASE_REPLY_CHANCE)
    status = "🟢 Включён" if enabled else "🔴 Выключен"
    return (
        f"⚙️ <b>Настройки бота</b>\n\n"
        f"Статус: {status}\n"
        f"Базовый шанс стикера: <b>{int(chance * 100)}%</b>"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@dp.message(Command("start", "menu"))
async def cmd_start(message: Message):
    await message.answer("🎰", reply_markup=main_menu_keyboard())


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
        await message.reply("Команда работает только в группах.")
        return
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы могут изменять настройки.")
        return
    await message.answer(
        settings_text(message.chat.id),
        parse_mode="HTML",
        reply_markup=settings_keyboard(message.chat.id),
    )


@dp.message(Command("getid"))
async def cmd_getid(message: Message):
    if message.reply_to_message and message.reply_to_message.sticker:
        sticker = message.reply_to_message.sticker
        await message.reply(f"file_id:\n<code>{sticker.file_id}</code>", parse_mode="HTML")
    else:
        await message.reply("Перешли стикер и ответь на него /getid.")


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == ROLL_CALLBACK)
async def on_roll(callback: CallbackQuery):
    """Нажатие кнопки 🎰 — отправляет эмодзи-слот."""
    await callback.message.answer("🎰")
    await callback.answer()


@dp.callback_query(F.data == MORE_BUTTON_CALLBACK)
async def on_more_sticker(callback: CallbackQuery):
    stickers = load_stickers()
    if not stickers:
        await callback.answer("Список стикеров пуст", show_alert=True)
        return
    await callback.message.answer_sticker(random.choice(stickers), reply_markup=more_sticker_keyboard())
    await callback.answer()


@dp.callback_query(F.data == SETTINGS_TOGGLE_CALLBACK)
async def on_settings_toggle(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("⛔ Только для админов", show_alert=True)
        return
    current = get_chat_setting(chat_id, "enabled", True)
    set_chat_setting(chat_id, "enabled", not current)
    await callback.message.edit_text(
        settings_text(chat_id), parse_mode="HTML",
        reply_markup=settings_keyboard(chat_id),
    )
    await callback.answer()


@dp.callback_query(F.data == SETTINGS_CHANCE_UP)
async def on_chance_up(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("⛔ Только для админов", show_alert=True)
        return
    current = get_chat_setting(chat_id, "base_chance", BASE_REPLY_CHANCE)
    new_val = min(round(current + 0.05, 2), 1.0)
    set_chat_setting(chat_id, "base_chance", new_val)
    await callback.message.edit_text(
        settings_text(chat_id), parse_mode="HTML",
        reply_markup=settings_keyboard(chat_id),
    )
    await callback.answer(f"Шанс → {int(new_val * 100)}%")


@dp.callback_query(F.data == SETTINGS_CHANCE_DOWN)
async def on_chance_down(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("⛔ Только для админов", show_alert=True)
        return
    current = get_chat_setting(chat_id, "base_chance", BASE_REPLY_CHANCE)
    new_val = max(round(current - 0.05, 2), 0.0)
    set_chat_setting(chat_id, "base_chance", new_val)
    await callback.message.edit_text(
        settings_text(chat_id), parse_mode="HTML",
        reply_markup=settings_keyboard(chat_id),
    )
    await callback.answer(f"Шанс → {int(new_val * 100)}%")


@dp.callback_query(F.data == SETTINGS_CLOSE)
async def on_settings_close(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery):
    await callback.answer()


# ---------------------------------------------------------------------------
# Auto-sticker on group messages
# ---------------------------------------------------------------------------

@dp.message(lambda m: m.sticker is not None)
async def on_sticker_dm(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.reply(
            f"file_id:\n<code>{message.sticker.file_id}</code>", parse_mode="HTML"
        )


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

    chance = get_reply_chance(chat_id)
    if random.random() < chance:
        update_cooldown(chat_id)
        await message.answer_sticker(random.choice(stickers), reply_markup=more_sticker_keyboard())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    log.info("Бот запускается. Загружено стикеров: %d", len(load_stickers()))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
