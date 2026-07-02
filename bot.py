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
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("meme-sticker-bot")

BOT_TOKEN          = os.environ["BOT_TOKEN"]
BASE_REPLY_CHANCE  = float(os.environ.get("BASE_REPLY_CHANCE", "0.1"))
MAX_REPLY_CHANCE   = float(os.environ.get("MAX_REPLY_CHANCE",  "0.4"))
ACTIVITY_THRESHOLD = int(os.environ.get("ACTIVITY_THRESHOLD", "8"))
ACTIVITY_WINDOW    = int(os.environ.get("ACTIVITY_WINDOW",    "60"))
STICKER_COOLDOWN   = 0.001

STICKERS_FILE = os.path.join(os.path.dirname(__file__), "stickers.json")

# ── State ────────────────────────────────────────────────────────────────────
chat_activity:    dict[int, deque]  = defaultdict(deque)
chat_last_sticker:dict[int, float]  = {}
chat_settings:    dict[int, dict]   = {}

STARTING_BALANCE  = 1000
DAILY_AMOUNT      = 200
DAILY_COOLDOWN    = 86400          # 24 h in seconds

user_coins: dict[int, int]   = defaultdict(lambda: STARTING_BALANCE)
user_daily: dict[int, float] = {}
user_stats: dict[int, dict]  = defaultdict(
    lambda: {"duels": 0, "wins": 0, "slots": 0, "fish": 0}
)

active_duels:     dict[int, dict] = {}   # chat_id -> duel info
active_bj:        dict[int, dict] = {}   # user_id -> blackjack game
pending_roulette: dict[int, int]  = {}   # user_id -> bet
pending_coin:     dict[int, int]  = {}   # user_id -> bet
DUEL_TIMEOUT = 60

# ── Helpers ──────────────────────────────────────────────────────────────────
def gcfg(chat_id, key, default): return chat_settings.get(chat_id, {}).get(key, default)
def scfg(chat_id, key, value):
    chat_settings.setdefault(chat_id, {})[key] = value

def get_activity_level(chat_id: int) -> float:
    now = time.time()
    h   = chat_activity[chat_id]
    h.append(now)
    while h and now - h[0] > ACTIVITY_WINDOW: h.popleft()
    return min(len(h) / ACTIVITY_THRESHOLD, 1.0)

def get_reply_chance(chat_id: int) -> float:
    base  = gcfg(chat_id, "base_chance", BASE_REPLY_CHANCE)
    level = get_activity_level(chat_id)
    return base + (MAX_REPLY_CHANCE - base) * level

def load_stickers() -> list[str]:
    if not os.path.exists(STICKERS_FILE): return []
    with open(STICKERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("file_ids", [])

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception: return False

def coins(uid: int) -> int:       return user_coins[uid]
def add_coins(uid: int, n: int):  user_coins[uid] = max(0, user_coins[uid] + n)
def take_coins(uid: int, n: int) -> bool:
    if user_coins[uid] < n: return False
    user_coins[uid] -= n; return True

# ── Cards ─────────────────────────────────────────────────────────────────────
SUITS  = ["♠️","♥️","♦️","♣️"]
RANKS  = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]

def card_val(r: str) -> int:
    if r in ("J","Q","K"): return 10
    if r == "A":           return 11
    return int(r)

def hand_val(hand: list) -> int:
    total = sum(card_val(r) for r,_ in hand)
    aces  = sum(1 for r,_ in hand if r=="A")
    while total > 21 and aces: total -= 10; aces -= 1
    return total

def hand_str(hand: list, hide=False) -> str:
    if hide and len(hand)>=2:
        return f"{hand[0][0]}{hand[0][1]} 🂠"
    return " ".join(f"{r}{s}" for r,s in hand)

def new_deck():
    d = [(r,s) for r in RANKS for s in SUITS]
    random.shuffle(d); return d

# ── Slots ─────────────────────────────────────────────────────────────────────
SLOT_SYM  = ["🍒","🍋","🍊","🍇","⭐","💎"]
SLOT_W    = [30, 25, 20, 15, 8, 2]
SLOT_MULT = {"🍒":2,"🍋":3,"🍊":4,"🍇":5,"⭐":10,"💎":50}

def spin_slots():
    return random.choices(SLOT_SYM, weights=SLOT_W, k=3)

def slots_result(sym: list, bet: int) -> tuple[int,str]:
    if sym[0]==sym[1]==sym[2]:
        m = SLOT_MULT[sym[0]]
        return bet*m, f"🎉 Джекпот x{m}!"
    if sym[0]==sym[1] or sym[1]==sym[2]:
        return int(bet*1.5), "✨ Два одинаковых x1.5"
    return 0, "😔 Мимо"

# ── Roulette ──────────────────────────────────────────────────────────────────
RED_N = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

def roulette_result(number: int, bet_type: str, bet: int) -> tuple[int,str]:
    color = "🟢" if number==0 else ("🔴" if number in RED_N else "⚫")
    if bet_type == "red":
        if number in RED_N: return bet*2, f"{color} {number} — Красное! +{bet}🪙"
        return 0, f"{color} {number} — Проигрыш"
    if bet_type == "black":
        if number not in RED_N and number!=0: return bet*2, f"{color} {number} — Чёрное! +{bet}🪙"
        return 0, f"{color} {number} — Проигрыш"
    if bet_type == "zero":
        if number==0: return bet*14, f"🟢 0 — Зеро! +{bet*13}🪙"
        return 0, f"{color} {number} — Проигрыш"
    return 0, "?"

# ── Fishing ────────────────────────────────────────────────────────────────────
FISH_TABLE = [
    ("🐟 Мелкая рыбёшка",    15,  35),
    ("🐠 Красивая рыбка",    30,  25),
    ("🐡 Шар-рыба",          60,  15),
    ("🦑 Кальмар",          100,  10),
    ("🦈 Акула!",           250,   7),
    ("🎣 Старый ботинок",     0,   6),
    ("💎 Бриллиант со дна!", 700,   2),
]
FISH_COOLDOWN = 30  # секунд
fish_last: dict[int, float] = {}

def catch_fish() -> tuple[str, int]:
    total = sum(w for _,_,w in FISH_TABLE)
    r     = random.randint(1, total); cum = 0
    for name, prize, w in FISH_TABLE:
        cum += w
        if r <= cum: return name, prize
    return FISH_TABLE[0][:2]

# ─────────────────────────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ── Callback constants ────────────────────────────────────────────────────────
CB_ROLL       = "roll_slot"
CB_MORE       = "more_sticker"
CB_SETTINGS   = "open_settings"
CB_TOG        = "s_toggle"
CB_UP         = "s_up"
CB_DOWN       = "s_down"
CB_CLOSE      = "s_close"
CB_DUEL_ACC   = "duel_accept"
CB_DUEL_DEC   = "duel_decline"
CB_BJ_HIT     = "bj_hit"
CB_BJ_STAND   = "bj_stand"
CB_RL_RED     = "rl_red"
CB_RL_BLACK   = "rl_black"
CB_RL_ZERO    = "rl_zero"
CB_COIN_H     = "coin_heads"
CB_COIN_T     = "coin_tails"

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎰 Слоты",     callback_data=CB_ROLL),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data=CB_SETTINGS),
    ]])

def more_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎲 Ещё стикер", callback_data=CB_MORE)
    ]])

def duel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Принять",  callback_data=CB_DUEL_ACC),
        InlineKeyboardButton(text="🏳️ Отказать", callback_data=CB_DUEL_DEC),
    ]])

def bj_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👊 Ещё карту", callback_data=CB_BJ_HIT),
        InlineKeyboardButton(text="✋ Стоп",       callback_data=CB_BJ_STAND),
    ]])

def roulette_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔴 Красное", callback_data=CB_RL_RED),
        InlineKeyboardButton(text="⚫ Чёрное",  callback_data=CB_RL_BLACK),
        InlineKeyboardButton(text="🟢 Зеро x14",callback_data=CB_RL_ZERO),
    ]])

def coin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🦅 Орёл",   callback_data=CB_COIN_H),
        InlineKeyboardButton(text="🪙 Решка",  callback_data=CB_COIN_T),
    ]])

def settings_kb(chat_id: int) -> InlineKeyboardMarkup:
    enabled = gcfg(chat_id, "enabled", True)
    chance  = gcfg(chat_id, "base_chance", BASE_REPLY_CHANCE)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔴 Выключить" if enabled else "🟢 Включить",
            callback_data=CB_TOG)],
        [
            InlineKeyboardButton(text="➖", callback_data=CB_DOWN),
            InlineKeyboardButton(text=f"🎯 {int(chance*100)}%", callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=CB_UP),
        ],
        [InlineKeyboardButton(text="✅ Закрыть", callback_data=CB_CLOSE)],
    ])

def settings_text(chat_id: int) -> str:
    enabled = gcfg(chat_id, "enabled", True)
    chance  = gcfg(chat_id, "base_chance", BASE_REPLY_CHANCE)
    return (
        f"⚙️ <b>Настройки бота</b>\n\n"
        f"Статус: {'🟢 Включён' if enabled else '🔴 Выключен'}\n"
        f"Базовый шанс стикера: <b>{int(chance*100)}%</b>"
    )

def bj_text(game: dict, reveal=False) -> str:
    ph = game["player"]; dh = game["dealer"]; bet = game["bet"]
    return (
        f"🃏 <b>Блэкджек</b> | Ставка: <b>{bet}🪙</b>\n\n"
        f"🤖 Дилер: {hand_str(dh, hide=not reveal)} "
        f"({'?' if not reveal else hand_val(dh)})\n"
        f"👤 Вы:    {hand_str(ph)} (<b>{hand_val(ph)}</b>)"
    )

# ─────────────────────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

@dp.message(Command("start","menu"))
async def cmd_start(message: Message):
    bal = coins(message.from_user.id)
    await message.answer(
        f"👋 Привет, <b>{message.from_user.full_name}</b>!\n"
        f"💰 Баланс: <b>{bal}🪙</b>\n\n"
        f"Используй команды ниже или кнопки:",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )

# ── Экономика ─────────────────────────────────────────────────────────────────

@dp.message(Command("balance","bal"))
async def cmd_balance(message: Message):
    uid = message.from_user.id
    await message.answer(
        f"💰 <b>{message.from_user.full_name}</b>\n"
        f"Баланс: <b>{coins(uid)}🪙</b>",
        parse_mode="HTML",
    )

@dp.message(Command("daily"))
async def cmd_daily(message: Message):
    uid  = message.from_user.id
    last = user_daily.get(uid, 0)
    diff = time.time() - last
    if diff < DAILY_COOLDOWN:
        left = int(DAILY_COOLDOWN - diff)
        h, m = divmod(left//60, 60)
        await message.reply(f"⏰ Следующий бонус через <b>{h}ч {m}м</b>.", parse_mode="HTML")
        return
    user_daily[uid] = time.time()
    add_coins(uid, DAILY_AMOUNT)
    await message.reply(
        f"🎁 Ежедневный бонус: +<b>{DAILY_AMOUNT}🪙</b>\n"
        f"Баланс: <b>{coins(uid)}🪙</b>",
        parse_mode="HTML",
    )

@dp.message(Command("top"))
async def cmd_top(message: Message):
    if not user_coins:
        await message.answer("Пока нет данных.")
        return
    sorted_users = sorted(user_coins.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = []
    medals = ["🥇","🥈","🥉"] + ["4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    for i, (uid, bal) in enumerate(sorted_users):
        stat = user_stats[uid]
        lines.append(f"{medals[i]} <b>ID{uid}</b> — {bal}🪙 | ⚔️{stat['wins']}побед")
    await message.answer("🏆 <b>Топ игроков</b>\n\n" + "\n".join(lines), parse_mode="HTML")

@dp.message(Command("me"))
async def cmd_me(message: Message):
    uid  = message.from_user.id
    stat = user_stats[uid]
    wr   = int(stat["wins"]/stat["duels"]*100) if stat["duels"] else 0
    await message.answer(
        f"👤 <b>{message.from_user.full_name}</b>\n\n"
        f"💰 Монет: <b>{coins(uid)}🪙</b>\n"
        f"🎰 Прокруток слота: <b>{stat['slots']}</b>\n"
        f"⚔️ Дуэлей: <b>{stat['duels']}</b> | Побед: <b>{stat['wins']}</b> ({wr}%)\n"
        f"🎣 Поймано рыб: <b>{stat['fish']}</b>",
        parse_mode="HTML",
    )

# ── Слоты ──────────────────────────────────────────────────────────────────────

@dp.message(Command("slots"))
async def cmd_slots(message: Message):
    uid  = message.from_user.id
    args = (message.text or "").split()
    bet  = 50
    if len(args) > 1:
        try:   bet = max(1, int(args[1]))
        except ValueError: pass

    if not take_coins(uid, bet):
        await message.reply(f"❌ Недостаточно монет. У тебя: {coins(uid)}🪙")
        return

    sym = spin_slots()
    won, desc = slots_result(sym, bet)
    add_coins(uid, won)
    user_stats[uid]["slots"] += 1

    net = won - bet
    sign = "+" if net >= 0 else ""
    await message.reply(
        f"🎰 {' '.join(sym)}\n\n"
        f"{desc}\n"
        f"Ставка: {bet}🪙 | Итог: <b>{sign}{net}🪙</b>\n"
        f"Баланс: <b>{coins(uid)}🪙</b>",
        parse_mode="HTML",
    )

# ── Блэкджек ──────────────────────────────────────────────────────────────────

@dp.message(Command("bj","blackjack"))
async def cmd_bj(message: Message):
    uid  = message.from_user.id
    args = (message.text or "").split()
    bet  = 100
    if len(args) > 1:
        try:   bet = max(1, int(args[1]))
        except ValueError: pass

    if not take_coins(uid, bet):
        await message.reply(f"❌ Недостаточно монет. У тебя: {coins(uid)}🪙")
        return

    deck   = new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]

    active_bj[uid] = {"player": player, "dealer": dealer, "deck": deck, "bet": bet}
    game = active_bj[uid]

    # Natural blackjack
    if hand_val(player) == 21:
        win = int(bet * 2.5)
        add_coins(uid, win)
        del active_bj[uid]
        await message.reply(
            bj_text(game, reveal=True) + f"\n\n🃏 <b>Блэкджек! +{win-bet}🪙</b>\nБаланс: <b>{coins(uid)}🪙</b>",
            parse_mode="HTML",
        )
        return

    await message.reply(bj_text(game), parse_mode="HTML", reply_markup=bj_kb())

@dp.callback_query(F.data == CB_BJ_HIT)
async def on_bj_hit(callback: CallbackQuery):
    uid  = callback.from_user.id
    game = active_bj.get(uid)
    if not game:
        await callback.answer("У тебя нет активной игры.", show_alert=True); return

    game["player"].append(game["deck"].pop())
    val = hand_val(game["player"])

    if val > 21:
        add_coins(uid, 0)  # already charged
        del active_bj[uid]
        await callback.message.edit_text(
            bj_text(game, reveal=True) + f"\n\n💥 <b>Перебор! ({val})</b> Потерял {game['bet']}🪙\nБаланс: <b>{coins(uid)}🪙</b>",
            parse_mode="HTML",
        )
    elif val == 21:
        await on_bj_stand_logic(callback, uid, game)
    else:
        await callback.message.edit_text(bj_text(game), parse_mode="HTML", reply_markup=bj_kb())
    await callback.answer()

@dp.callback_query(F.data == CB_BJ_STAND)
async def on_bj_stand(callback: CallbackQuery):
    uid  = callback.from_user.id
    game = active_bj.get(uid)
    if not game:
        await callback.answer("У тебя нет активной игры.", show_alert=True); return
    await on_bj_stand_logic(callback, uid, game)
    await callback.answer()

async def on_bj_stand_logic(callback, uid, game):
    deck = game["deck"]
    while hand_val(game["dealer"]) < 17:
        game["dealer"].append(deck.pop())

    pv = hand_val(game["player"])
    dv = hand_val(game["dealer"])
    bet = game["bet"]

    if dv > 21 or pv > dv:
        add_coins(uid, bet*2)
        result = f"🏆 Вы победили! +{bet}🪙"
    elif pv == dv:
        add_coins(uid, bet)
        result = "🤝 Ничья — ставка возвращена"
    else:
        result = f"😔 Дилер победил ({dv} vs {pv})"

    del active_bj[uid]
    await callback.message.edit_text(
        bj_text(game, reveal=True) + f"\n\n{result}\nБаланс: <b>{coins(uid)}🪙</b>",
        parse_mode="HTML",
    )

# ── Рулетка ────────────────────────────────────────────────────────────────────

@dp.message(Command("roulette","rl"))
async def cmd_roulette(message: Message):
    uid  = message.from_user.id
    args = (message.text or "").split()
    bet  = 50
    if len(args) > 1:
        try:   bet = max(1, int(args[1]))
        except ValueError: pass

    if not take_coins(uid, bet):
        await message.reply(f"❌ Недостаточно монет. У тебя: {coins(uid)}🪙")
        return

    pending_roulette[uid] = bet
    await message.reply(
        f"🎡 <b>Рулетка</b> | Ставка: <b>{bet}🪙</b>\n\nВыбери цвет:",
        parse_mode="HTML",
        reply_markup=roulette_kb(),
    )

async def resolve_roulette(callback: CallbackQuery, bet_type: str):
    uid = callback.from_user.id
    bet = pending_roulette.pop(uid, None)
    if bet is None:
        await callback.answer("Сначала введи /roulette [ставка]", show_alert=True); return

    number = random.randint(0, 36)
    won, desc = roulette_result(number, bet_type, bet)
    add_coins(uid, won)
    net  = won - bet
    sign = "+" if net >= 0 else ""

    await callback.message.edit_text(
        f"🎡 <b>Рулетка</b>\n\n{desc}\n"
        f"Итог: <b>{sign}{net}🪙</b> | Баланс: <b>{coins(uid)}🪙</b>",
        parse_mode="HTML",
    )
    await callback.answer()

@dp.callback_query(F.data == CB_RL_RED)
async def on_rl_red(cb: CallbackQuery):   await resolve_roulette(cb, "red")
@dp.callback_query(F.data == CB_RL_BLACK)
async def on_rl_black(cb: CallbackQuery): await resolve_roulette(cb, "black")
@dp.callback_query(F.data == CB_RL_ZERO)
async def on_rl_zero(cb: CallbackQuery):  await resolve_roulette(cb, "zero")

# ── Монетка ────────────────────────────────────────────────────────────────────

@dp.message(Command("coin"))
async def cmd_coin(message: Message):
    uid  = message.from_user.id
    args = (message.text or "").split()
    bet  = 50
    if len(args) > 1:
        try:   bet = max(1, int(args[1]))
        except ValueError: pass

    if not take_coins(uid, bet):
        await message.reply(f"❌ Недостаточно монет. У тебя: {coins(uid)}🪙")
        return

    pending_coin[uid] = bet
    await message.reply(
        f"🪙 <b>Орёл или решка?</b> | Ставка: <b>{bet}🪙</b>",
        parse_mode="HTML",
        reply_markup=coin_kb(),
    )

async def resolve_coin(callback: CallbackQuery, choice: str):
    uid = callback.from_user.id
    bet = pending_coin.pop(uid, None)
    if bet is None:
        await callback.answer("Сначала введи /coin [ставка]", show_alert=True); return

    result = random.choice(["heads", "tails"])
    won    = result == choice
    if won: add_coins(uid, bet*2)

    icon   = "🦅 Орёл" if result=="heads" else "🪙 Решка"
    verdict= f"✅ Угадал! +{bet}🪙" if won else f"❌ Не угадал, -{bet}🪙"

    await callback.message.edit_text(
        f"{icon}\n\n{verdict}\nБаланс: <b>{coins(uid)}🪙</b>",
        parse_mode="HTML",
    )
    await callback.answer()

@dp.callback_query(F.data == CB_COIN_H)
async def on_coin_h(cb: CallbackQuery): await resolve_coin(cb, "heads")
@dp.callback_query(F.data == CB_COIN_T)
async def on_coin_t(cb: CallbackQuery): await resolve_coin(cb, "tails")

# ── Рыбалка ────────────────────────────────────────────────────────────────────

@dp.message(Command("fish"))
async def cmd_fish(message: Message):
    uid  = message.from_user.id
    last = fish_last.get(uid, 0)
    if time.time() - last < FISH_COOLDOWN:
        left = int(FISH_COOLDOWN - (time.time()-last))
        await message.reply(f"🎣 Рыба ещё не пришла... Подожди <b>{left}с</b>.", parse_mode="HTML")
        return

    fish_last[uid] = time.time()
    name, prize    = catch_fish()
    add_coins(uid, prize)
    user_stats[uid]["fish"] += 1

    if prize > 0:
        text = f"🎣 Поймал: <b>{name}</b>\n+<b>{prize}🪙</b> | Баланс: <b>{coins(uid)}🪙</b>"
    else:
        text = f"🎣 Поймал: <b>{name}</b>\nНичего не заработал 😂"
    await message.reply(text, parse_mode="HTML")

# ── Дуэль ──────────────────────────────────────────────────────────────────────

@dp.message(Command("duel"))
async def cmd_duel(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply("⚔️ Дуэли только в группах!"); return

    chat_id = message.chat.id
    if chat_id in active_duels and time.time()-active_duels[chat_id]["ts"] < DUEL_TIMEOUT:
        await message.reply("В чате уже идёт дуэль!"); return
    active_duels.pop(chat_id, None)

    args = (message.text or "").split()
    bet  = 0
    if len(args) > 1:
        try: bet = max(0, int(args[1]))
        except ValueError: pass

    uid = message.from_user.id
    if bet and not take_coins(uid, bet):
        await message.reply(f"❌ Недостаточно монет. У тебя: {coins(uid)}🪙"); return

    msg = await message.answer(
        f"⚔️ <b>{message.from_user.full_name}</b> вызывает на дуэль!\n"
        f"{'Ставка: <b>' + str(bet) + '🪙</b>' if bet else 'Без ставки'}\n\n"
        f"У вас {DUEL_TIMEOUT} секунд чтобы принять.",
        parse_mode="HTML",
        reply_markup=duel_kb(),
    )
    active_duels[chat_id] = {
        "challenger_id":   uid,
        "challenger_name": message.from_user.full_name,
        "bet":             bet,
        "ts":              time.time(),
        "message_id":      msg.message_id,
    }

@dp.callback_query(F.data == CB_DUEL_ACC)
async def on_duel_accept(callback: CallbackQuery):
    chat_id  = callback.message.chat.id
    acceptor = callback.from_user
    duel     = active_duels.get(chat_id)

    if not duel:
        await callback.answer("Дуэль истекла.", show_alert=True); return
    if acceptor.id == duel["challenger_id"]:
        await callback.answer("Нельзя принять свой вызов!", show_alert=True); return
    if time.time()-duel["ts"] > DUEL_TIMEOUT:
        active_duels.pop(chat_id, None)
        await callback.message.edit_text("⏰ Время дуэли истекло.")
        await callback.answer(); return

    bet = duel["bet"]
    if bet and not take_coins(acceptor.id, bet):
        await callback.answer(f"У тебя нет {bet}🪙 для ставки!", show_alert=True); return

    sc = random.randint(0,100); sa = random.randint(0,100)
    cid = duel["challenger_id"]; cn = duel["challenger_name"]
    an  = acceptor.full_name

    user_stats[cid]["duels"]           += 1
    user_stats[acceptor.id]["duels"]   += 1

    if sc > sa:
        verdict = f"🏆 Победил <b>{cn}</b>!"
        user_stats[cid]["wins"] += 1
        if bet: add_coins(cid, bet*2)
    elif sa > sc:
        verdict = f"🏆 Победил <b>{an}</b>!"
        user_stats[acceptor.id]["wins"] += 1
        if bet: add_coins(acceptor.id, bet*2)
    else:
        verdict = "🤝 Ничья! Ставки возвращены."
        if bet: add_coins(cid, bet); add_coins(acceptor.id, bet)

    active_duels.pop(chat_id, None)
    await callback.message.edit_text(
        f"⚔️ <b>ДУЭЛЬ</b>\n\n"
        f"{cn}: 🎲 <b>{sc}</b>\n"
        f"{an}: 🎲 <b>{sa}</b>\n\n"
        f"{verdict}",
        parse_mode="HTML",
    )
    await callback.answer()

@dp.callback_query(F.data == CB_DUEL_DEC)
async def on_duel_decline(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    duel    = active_duels.pop(chat_id, None)
    if not duel:
        await callback.answer("Дуэль уже завершена.", show_alert=True); return
    if callback.from_user.id == duel["challenger_id"]:
        await callback.answer("Нельзя отказать самому себе!", show_alert=True); return
    if duel["bet"]: add_coins(duel["challenger_id"], duel["bet"])  # refund
    await callback.message.edit_text(
        f"🏳️ <b>{callback.from_user.full_name}</b> — трус! Дуэль отменена.",
        parse_mode="HTML",
    )
    await callback.answer()

# ── Настройки ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == CB_SETTINGS)
async def on_open_settings(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("⛔ Только для администраторов.", show_alert=True); return
    await callback.message.answer(settings_text(chat_id), parse_mode="HTML",
                                  reply_markup=settings_kb(chat_id))
    await callback.answer()

@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply("Только для групп."); return
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы."); return
    await message.answer(settings_text(message.chat.id), parse_mode="HTML",
                         reply_markup=settings_kb(message.chat.id))

@dp.callback_query(F.data == CB_TOG)
async def on_toggle(cb: CallbackQuery):
    cid = cb.message.chat.id
    if not await is_admin(bot, cid, cb.from_user.id):
        await cb.answer("⛔", show_alert=True); return
    scfg(cid, "enabled", not gcfg(cid, "enabled", True))
    await cb.message.edit_text(settings_text(cid), parse_mode="HTML", reply_markup=settings_kb(cid))
    await cb.answer()

@dp.callback_query(F.data == CB_UP)
async def on_sup(cb: CallbackQuery):
    cid = cb.message.chat.id
    if not await is_admin(bot, cid, cb.from_user.id):
        await cb.answer("⛔", show_alert=True); return
    val = min(round(gcfg(cid,"base_chance",BASE_REPLY_CHANCE)+0.05, 2), 1.0)
    scfg(cid,"base_chance",val)
    await cb.message.edit_text(settings_text(cid), parse_mode="HTML", reply_markup=settings_kb(cid))
    await cb.answer(f"{int(val*100)}%")

@dp.callback_query(F.data == CB_DOWN)
async def on_sdown(cb: CallbackQuery):
    cid = cb.message.chat.id
    if not await is_admin(bot, cid, cb.from_user.id):
        await cb.answer("⛔", show_alert=True); return
    val = max(round(gcfg(cid,"base_chance",BASE_REPLY_CHANCE)-0.05, 2), 0.0)
    scfg(cid,"base_chance",val)
    await cb.message.edit_text(settings_text(cid), parse_mode="HTML", reply_markup=settings_kb(cid))
    await cb.answer(f"{int(val*100)}%")

@dp.callback_query(F.data == CB_CLOSE)
async def on_close(cb: CallbackQuery):
    await cb.message.delete(); await cb.answer()

@dp.callback_query(F.data == CB_ROLL)
async def on_roll_btn(callback: CallbackQuery):
    uid = callback.from_user.id
    sym = spin_slots()
    user_stats[uid]["slots"] += 1
    await callback.message.answer(
        f"🎰 {' '.join(sym)}\n<b>{callback.from_user.full_name}</b>",
        parse_mode="HTML",
    )
    await callback.answer()

@dp.callback_query(F.data == CB_MORE)
async def on_more(cb: CallbackQuery):
    stickers = load_stickers()
    if not stickers:
        await cb.answer("Список стикеров пуст", show_alert=True); return
    await cb.message.answer_sticker(random.choice(stickers), reply_markup=more_kb())
    await cb.answer()

@dp.callback_query(F.data == "noop")
async def on_noop(cb: CallbackQuery): await cb.answer()

# ── Стикеры ────────────────────────────────────────────────────────────────────

@dp.message(Command("sticker"))
async def cmd_sticker(message: Message):
    stickers = load_stickers()
    if not stickers: await message.reply("Список стикеров пуст."); return
    await message.answer_sticker(random.choice(stickers), reply_markup=more_kb())

@dp.message(Command("getid"))
async def cmd_getid(message: Message):
    if message.reply_to_message and message.reply_to_message.sticker:
        fid = message.reply_to_message.sticker.file_id
        await message.reply(f"<code>{fid}</code>", parse_mode="HTML")
    else:
        await message.reply("Ответь командой /getid на стикер.")

def save_sticker(file_id: str) -> bool:
    """Добавляет стикер в stickers.json. Возвращает True если добавлен, False если уже есть."""
    data = {"file_ids": []}
    if os.path.exists(STICKERS_FILE):
        with open(STICKERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    if file_id in data["file_ids"]:
        return False
    data["file_ids"].append(file_id)
    with open(STICKERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True

@dp.message(lambda m: m.sticker is not None)
async def on_sticker_dm(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        fid   = message.sticker.file_id
        added = save_sticker(fid)
        total = len(load_stickers())
        if added:
            await message.reply(
                f"✅ Стикер сохранён! Всего: <b>{total}</b>",
                parse_mode="HTML",
            )
        else:
            await message.reply(
                f"⚠️ Уже есть. Всего: <b>{total}</b>",
                parse_mode="HTML",
            )
        # Отправить обратно тот же стикер
        await message.answer_sticker(fid)

@dp.message(lambda m: m.sticker is not None and m.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))
async def on_sticker_group(message: Message):
    """Кто-то прислал стикер в группу — бот отвечает стикером."""
    stickers = load_stickers()
    if not stickers:
        return
    if not gcfg(message.chat.id, "enabled", True):
        return
    await message.answer_sticker(random.choice(stickers))

@dp.message()
async def on_any_message(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    if message.text and message.text.startswith("/"): return
    chat_id = message.chat.id
    if not gcfg(chat_id, "enabled", True): return
    stickers = load_stickers()
    if not stickers: return
    if (time.time()-chat_last_sticker.get(chat_id,0)) < STICKER_COOLDOWN: return
    chat_last_sticker[chat_id] = time.time()
    await message.answer_sticker(random.choice(stickers), reply_markup=more_kb())

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    commands = [
        BotCommand(command="start",    description="🎰 Главное меню"),
        BotCommand(command="me",       description="👤 Мой профиль и статистика"),
        BotCommand(command="balance",  description="💰 Мой баланс монет"),
        BotCommand(command="daily",    description="🎁 Ежедневный бонус (+200🪙)"),
        BotCommand(command="top",      description="🏆 Топ игроков"),
        BotCommand(command="slots",    description="🎰 Слот-машина  /slots 100"),
        BotCommand(command="bj",       description="🃏 Блэкджек  /bj 100"),
        BotCommand(command="roulette", description="🎡 Рулетка  /roulette 50"),
        BotCommand(command="coin",     description="🪙 Орёл или решка  /coin 50"),
        BotCommand(command="fish",     description="🎣 Рыбалка (бесплатно)"),
        BotCommand(command="duel",     description="⚔️ Дуэль  /duel 100"),
        BotCommand(command="sticker",  description="🖼 Случайный стикер"),
        BotCommand(command="settings", description="⚙️ Настройки (только админ)"),
        BotCommand(command="getid",    description="🔍 file_id стикера"),
    ]
    # Команды видны в личке
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    # Команды видны в группах (кнопка / в чате)
    await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    log.info("Бот запущен. Стикеров: %d", len(load_stickers()))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
