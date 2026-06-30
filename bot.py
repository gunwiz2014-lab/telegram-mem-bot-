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

# Базовый шанс ответить стикером в спокойном чате (0.0 - 1.0)
BASE_REPLY_CHANCE = float(os.environ.get("BASE_REPLY_CHANCE", "0.1"))

# Максимальный шанс, когда чат "кипит" активностью (0.0 - 1.0)
MAX_REPLY_CHANCE = float(os.environ.get("MAX_REPLY_CHANCE", "0.4"))

# Сколько сообщений за ACTIVITY_WINDOW секунд считается "максимальной" активностью
ACTIVITY_THRESHOLD = int(os.environ.get("ACTIVITY_THRESHOLD", "8"))

# Окно времени (в секундах), за которое считаем активность чата
ACTIVITY_WINDOW = int(os.environ.get("ACTIVITY_WINDOW", "60"))

STICKERS_FILE = os.path.join(os.path.dirname(__file__), "stickers.json")

# История таймстемпов сообщений по каждому чату (для расчёта активности)
chat_activity: dict[int, deque] = defaultdict(deque)


def get_activity_level(chat_id: int) -> float:
    """Возвращает уровень активности чата от 0.0 (тихо) до 1.0 (максимум)."""
    now = time.time()
    history = chat_activity[chat_id]
    history.append(now)

    while history and now - history[0] > ACTIVITY_WINDOW:
        history.popleft()

    return min(len(history) / ACTIVITY_THRESHOLD, 1.0)


def get_reply_chance(chat_id: int) -> float:
    """Линейно интерполирует шанс ответа между BASE и MAX в зависимости от активности."""
    level = get_activity_level(chat_id)
    return BASE_REPLY_CHANCE + (MAX_REPLY_CHANCE - BASE_REPLY_CHANCE) * level


def load_stickers() -> list[str]:
    if not os.path.exists(STICKERS_FILE):
        return []
    with open(STICKERS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("file_ids", [])


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

MORE_BUTTON_CALLBACK = "more_sticker"


def more_sticker_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Ещё стикер", callback_data=MORE_BUTTON_CALLBACK)]
        ]
    )


@dp.message(Command("sticker"))
async def cmd_sticker(message: Message):
    """Принудительно прислать рандомный стикер из набора."""
    stickers = load_stickers()
    if not stickers:
        await message.reply(
            "Список стикеров пуст. Добавь file_id в stickers.json "
            "(используй команду /getid, переслав боту стикер)."
        )
        return
    await message.answer_sticker(random.choice(stickers), reply_markup=more_sticker_keyboard())


@dp.callback_query(F.data == MORE_BUTTON_CALLBACK)
async def on_more_sticker(callback: CallbackQuery):
    """Кнопка под стикером - прислать ещё один."""
    stickers = load_stickers()
    if not stickers:
        await callback.answer("Список стикеров пуст", show_alert=True)
        return
    await callback.message.answer_sticker(random.choice(stickers), reply_markup=more_sticker_keyboard())
    await callback.answer()


@dp.message(Command("getid"))
async def cmd_getid(message: Message):
    """Узнать file_id стикера: перешли боту стикер с этой командой в reply,
    или просто отправь стикер в личку боту."""
    if message.reply_to_message and message.reply_to_message.sticker:
        sticker = message.reply_to_message.sticker
        await message.reply(f"file_id:\n{sticker.file_id}")
    else:
        await message.reply(
            "Перешли мне стикер и ответь на него командой /getid, "
            "либо просто пришли стикер в личные сообщения."
        )


@dp.message(lambda m: m.sticker is not None)
async def on_sticker_dm(message: Message):
    """Если в личку прислали стикер - сразу вернуть его file_id."""
    if message.chat.type == ChatType.PRIVATE:
        await message.reply(f"file_id:\n{message.sticker.file_id}")


@dp.message()
async def on_any_message(message: Message):
    """Случайная реакция стикером на сообщения в группах."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if message.text and message.text.startswith("/"):
        return

    stickers = load_stickers()
    if not stickers:
        return

    chance = get_reply_chance(message.chat.id)
    if random.random() < chance:
        await message.answer_sticker(random.choice(stickers), reply_markup=more_sticker_keyboard())


async def main():
    log.info("Бот запускается. Загружено стикеров: %d", len(load_stickers()))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
