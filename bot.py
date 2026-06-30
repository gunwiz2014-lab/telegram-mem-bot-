import asyncio
import json
import logging
import os
import random

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("meme-sticker-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Шанс что бот ответит стикером на обычное сообщение в группе (0.0 - 1.0)
REPLY_CHANCE = float(os.environ.get("REPLY_CHANCE", "0.1"))

STICKERS_FILE = os.path.join(os.path.dirname(__file__), "stickers.json")


def load_stickers() -> list[str]:
    if not os.path.exists(STICKERS_FILE):
        return []
    with open(STICKERS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("file_ids", [])


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


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
    await message.answer_sticker(random.choice(stickers))


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

    if random.random() < REPLY_CHANCE:
        await message.answer_sticker(random.choice(stickers))


async def main():
    log.info("Бот запускается. Загружено стикеров: %d", len(load_stickers()))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
