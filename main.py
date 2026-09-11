import os
import asyncio
import logging
import random
from typing import Optional, List, Tuple
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, InputMediaVideo, InputMediaPhoto
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError
import aiosqlite

# ----------------- CONFIGURATION ----------------- #
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

if not BOT_TOKEN or not ADMIN_ID:
    raise ValueError("Bhai, .env file mein BOT_TOKEN aur ADMIN_ID set karna zaruri hai!")

# Setup robust logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Initialize Bot and Dispatcher
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# Routers
user_router = Router()
admin_router = Router()
channel_router = Router()

dp.include_router(user_router)
dp.include_router(admin_router)
dp.include_router(channel_router)

# Database path
DB_PATH = "bot_database.sqlite"

# ----------------- DATABASE SETUP ----------------- #
async def init_db() -> None:
    """Initialize the async SQLite database and required tables."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Table for Users (Added is_banned column)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    step3_unlocked INTEGER DEFAULT 0,
                    step4_unlocked INTEGER DEFAULT 0,
                    video_batch INTEGER DEFAULT 0,
                    is_banned INTEGER DEFAULT 0
                )
            """)
            
            # Safe migration for existing databases to add is_banned column if missing
            try:
                await db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
            except Exception:
                pass # Column already exists
            
            # Table for Admin Settings
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    text_val TEXT,
                    media_id TEXT,
                    media_type TEXT
                )
            """)
            # DP Bank Table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS dp_bank (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id TEXT
                )
            """)
            # Video Dump Table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS video_dump (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id TEXT
                )
            """)
            await db.commit()
            logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

# Database Helper Functions
async def set_setting(key: str, text_val: str, media_id: Optional[str] = None, media_type: Optional[str] = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, text_val, media_id, media_type) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET text_val=excluded.text_val, media_id=excluded.media_id, media_type=excluded.media_type",
            (key, text_val, media_id, media_type)
        )
        await db.commit()

async def get_setting(key: str) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT text_val, media_id, media_type FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row if row else None

async def register_user(user_id: int, username: Optional[str]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username, is_banned) VALUES (?, ?, 0)", (user_id, username))
        await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        await db.commit()

async def get_user(user_id: int) -> Optional[Tuple[int, int, int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT step3_unlocked, step4_unlocked, video_batch, is_banned FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def is_user_banned(user_id: int) -> bool:
    user_data = await get_user(user_id)
    if user_data and len(user_data) >= 4 and user_data[3] == 1:
        return True
    return False

# ----------------- ADMIN STATES & HANDLERS ----------------- #
class AdminState(StatesGroup):
    waiting_for_start_msg = State()
    waiting_for_step1 = State()
    waiting_for_step2 = State()
    waiting_for_step3 = State()
    waiting_for_step4 = State()
    waiting_for_dp_channel = State()
    waiting_for_dump_channel = State()
    waiting_for_broadcast = State()

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Set Start Msg", callback_data="admin_set_start")],
        [InlineKeyboardButton(text="Set Step 1", callback_data="admin_set_step1"),
         InlineKeyboardButton(text="Set Step 2", callback_data="admin_set_step2")],
        [InlineKeyboardButton(text="Set Step 3", callback_data="admin_set_step3"),
         InlineKeyboardButton(text="Set Step 4", callback_data="admin_set_step4")],
        [InlineKeyboardButton(text="Set DP Channel ID", callback_data="admin_set_dp_channel"),
         InlineKeyboardButton(text="Set Dump Channel ID", callback_data="admin_set_dump_channel")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats")]
    ])

@admin_router.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_panel(message: Message, state: FSMContext) -> None:
    try:
        await state.clear()
        help_text = (
            "🛠 <b>Admin Panel</b>\n"
            "Select what you want to customize below.\n\n"
            "<b>Ban/Unban Commands:</b>\n"
            "To ban a user: <code>/ban user_id</code>\n"
            "To unban a user: <code>/unban user_id</code>"
        )
        await message.answer(help_text, reply_markup=admin_keyboard())
    except TelegramAPIError as e:
        logger.error(f"Admin panel error: {e}")

@admin_router.message(Command("ban"), F.from_user.id == ADMIN_ID)
async def ban_user_cmd(message: Message) -> None:
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("⚠️ Usage: <code>/ban user_id</code>")
            return
        target_id = int(args[1])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_id,))
            await db.commit()
        await message.answer(f"✅ User {target_id} has been permanently banned from using the bot.")
    except Exception as e:
        logger.error(f"Ban error: {e}")
        await message.answer("❌ Invalid User ID or Error occurred.")

@admin_router.message(Command("unban"), F.from_user.id == ADMIN_ID)
async def unban_user_cmd(message: Message) -> None:
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("⚠️ Usage: <code>/unban user_id</code>")
            return
        target_id = int(args[1])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_id,))
            await db.commit()
        await message.answer(f"✅ User {target_id} has been unbanned successfully.")
    except Exception as e:
        logger.error(f"Unban error: {e}")
        await message.answer("❌ Invalid User ID or Error occurred.")

@admin_router.callback_query(F.data == "admin_stats", F.from_user.id == ADMIN_ID)
async def show_stats(call: CallbackQuery) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cursor:
                total_users = (await cursor.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1") as cursor:
                banned_users = (await cursor.fetchone())[0]
        
        stats_text = (
            "📊 <b>Bot Statistics</b>\n\n"
            f"👥 Total Users: {total_users}\n"
            f"✅ Active Users: {total_users - banned_users}\n"
            f"🚫 Banned Users: {banned_users}\n\n"
            "<i>Note: Real-time blocked/inactive users are calculated after a broadcast.</i>"
        )
        await call.message.answer(stats_text)
        await call.answer()
    except Exception as e:
        logger.error(f"Stats error: {e}")

@admin_router.callback_query(F.data == "admin_broadcast", F.from_user.id == ADMIN_ID)
async def setup_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    try:
        await state.set_state(AdminState.waiting_for_broadcast)
        await call.message.answer("📢 Please send the message (Text/Photo/Video/Voice) you want to broadcast to all users.")
        await call.answer()
    except Exception as e:
        logger.error(f"Broadcast setup error: {e}")

@admin_router.message(AdminState.waiting_for_broadcast, F.from_user.id == ADMIN_ID)
async def execute_broadcast(message: Message, state: FSMContext) -> None:
    try:
        await state.clear()
        processing_msg = await message.answer("⏳ Broadcast started... Please wait.")
        
        success = 0
        failed = 0
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM users WHERE is_banned = 0") as cursor:
                users = await cursor.fetchall()
                
        for (uid,) in users:
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
                success += 1
            except Exception:
                failed += 1
            # Prevent Telegram API FloodWait error
            await asyncio.sleep(0.1)
            
        report = (
            "✅ <b>Broadcast Completed!</b>\n\n"
            f"📨 Successfully sent to: {success} users\n"
            f"❌ Failed (Blocked bot/Didn't reply): {failed} users"
        )
        await processing_msg.edit_text(report)
    except Exception as e:
        logger.error(f"Broadcast execution error: {e}")
        await message.answer("❌ Error occurred during broadcast.")

@admin_router.callback_query(F.data.startswith("admin_set_"), F.from_user.id == ADMIN_ID)
async def admin_setup_callbacks(call: CallbackQuery, state: FSMContext) -> None:
    try:
        action = call.data.replace("admin_set_", "")
        prompts = {
            "start": ("waiting_for_start_msg", "Send the new START message (Text/Photo/Video/Voice).\n(Hint: Mention your Instagram Work/Reels providing business here)"),
            "step1": ("waiting_for_step1", "Send the new STEP 1 message (Text/Photo/Video/Voice)."),
            "step2": ("waiting_for_step2", "Send the new STEP 2 message (Text/Photo/Video/Voice)."),
            "step3": ("waiting_for_step3", "Send the new STEP 3 message (Text/Photo/Video/Voice)."),
            "step4": ("waiting_for_step4", "Send the new STEP 4 message (Text/Photo/Video/Voice)."),
            "dp_channel": ("waiting_for_dp_channel", "Send the Channel ID for DP Bank (e.g. -100123456789). Bot must be admin there."),
            "dump_channel": ("waiting_for_dump_channel", "Send the Channel ID for Video Dump (e.g. -100123456789). Bot must be admin there.")
        }
        
        if action in prompts:
            state_name, prompt_msg = prompts[action]
            await state.set_state(getattr(AdminState, state_name))
            await call.message.answer(prompt_msg)
        await call.answer()
    except Exception as e:
        logger.error(f"Admin callback error: {e}")

async def save_media_setting(message: Message, state: FSMContext, key_name: str) -> None:
    try:
        text_val = message.html_text or ""
        media_id = None
        media_type = None

        if message.photo:
            media_id = message.photo[-1].file_id
            media_type = 'photo'
        elif message.video:
            media_id = message.video.file_id
            media_type = 'video'
        elif message.voice:
            media_id = message.voice.file_id
            media_type = 'voice'

        await set_setting(key_name, text_val, media_id, media_type)
        await message.answer(f"✅ Successfully saved {key_name.replace('_', ' ').title()}!")
        await state.clear()
    except Exception as e:
        logger.error(f"Error saving media setting: {e}")
        await message.answer("❌ Error saving setting.")

@admin_router.message(AdminState.waiting_for_start_msg, F.from_user.id == ADMIN_ID)
async def save_start(msg: Message, state: FSMContext) -> None: await save_media_setting(msg, state, "start_msg")

@admin_router.message(AdminState.waiting_for_step1, F.from_user.id == ADMIN_ID)
async def save_step1(msg: Message, state: FSMContext) -> None: await save_media_setting(msg, state, "step1_msg")

@admin_router.message(AdminState.waiting_for_step2, F.from_user.id == ADMIN_ID)
async def save_step2(msg: Message, state: FSMContext) -> None: await save_media_setting(msg, state, "step2_msg")

@admin_router.message(AdminState.waiting_for_step3, F.from_user.id == ADMIN_ID)
async def save_step3(msg: Message, state: FSMContext) -> None: await save_media_setting(msg, state, "step3_msg")

@admin_router.message(AdminState.waiting_for_step4, F.from_user.id == ADMIN_ID)
async def save_step4(msg: Message, state: FSMContext) -> None: await save_media_setting(msg, state, "step4_msg")

@admin_router.message(AdminState.waiting_for_dp_channel, F.from_user.id == ADMIN_ID)
async def save_dp_channel(message: Message, state: FSMContext) -> None:
    try:
        await set_setting("dp_channel", message.text.strip())
        await message.answer("✅ DP Channel ID saved! Bot will now auto-save any photos posted there.")
        await state.clear()
    except Exception as e:
        logger.error(f"Error saving DP channel: {e}")

@admin_router.message(AdminState.waiting_for_dump_channel, F.from_user.id == ADMIN_ID)
async def save_dump_channel(message: Message, state: FSMContext) -> None:
    try:
        await set_setting("dump_channel", message.text.strip())
        await message.answer("✅ Video Dump Channel ID saved! Bot will now auto-save any videos posted there.")
        await state.clear()
    except Exception as e:
        logger.error(f"Error saving Dump channel: {e}")

# ----------------- CHANNEL LISTENER (AUTO SAVE TO BANK) ----------------- #
@channel_router.channel_post()
async def listen_channels(message: Message) -> None:
    try:
        dp_setting = await get_setting("dp_channel")
        dump_setting = await get_setting("dump_channel")
        chat_id = str(message.chat.id)

        if dp_setting and chat_id == dp_setting[0]:
            if message.photo:
                file_id = message.photo[-1].file_id
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("INSERT INTO dp_bank (file_id) VALUES (?)", (file_id,))
                    await db.commit()
                logger.info("Saved new DP to DP Bank.")
        
        if dump_setting and chat_id == dump_setting[0]:
            if message.video:
                file_id = message.video.file_id
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("INSERT INTO video_dump (file_id) VALUES (?)", (file_id,))
                    await db.commit()
                logger.info("Saved new Video to Video Dump.")
    except Exception as e:
        logger.error(f"Error in channel listener: {e}")

# ----------------- USER HANDLERS ----------------- #
def main_steps_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Step 1", callback_data="run_step1")],
        [InlineKeyboardButton(text="Step 2", callback_data="run_step2")],
        [InlineKeyboardButton(text="Step 3", callback_data="run_step3")],
        [InlineKeyboardButton(text="Step 4", callback_data="run_step4")]
    ])

async def send_custom_content(chat_id: int, key: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    content = await get_setting(key)
    if not content:
        default_msgs = {
            "start_msg": "Welcome! Please check Admin Panel and setup this message.",
            "step1_msg": "Admin hasn't set Step 1 yet.",
            "step2_msg": "Admin hasn't set Step 2 yet.",
            "step3_msg": "Admin hasn't set Step 3 yet.",
            "step4_msg": "Admin hasn't set Step 4 yet."
        }
        fallback_text = default_msgs.get(key, "Admin hasn't set this content yet.")
        await bot.send_message(chat_id, fallback_text, reply_markup=reply_markup)
        return

    text_val, media_id, media_type = content
    try:
        if media_type == 'photo':
            await bot.send_photo(chat_id, photo=media_id, caption=text_val, reply_markup=reply_markup)
        elif media_type == 'video':
            await bot.send_video(chat_id, video=media_id, caption=text_val, reply_markup=reply_markup)
        elif media_type == 'voice':
            await bot.send_voice(chat_id, voice=media_id, caption=text_val, reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id, text=text_val, reply_markup=reply_markup)
    except TelegramAPIError as e:
        logger.error(f"Failed to send custom content for {key}: {e}")

@user_router.message(CommandStart())
async def start_cmd(message: Message) -> None:
    try:
        if await is_user_banned(message.from_user.id):
            return
            
        await register_user(message.from_user.id, message.from_user.username)
        await send_custom_content(message.chat.id, "start_msg", reply_markup=main_steps_keyboard())
    except Exception as e:
        logger.error(f"Start command error: {e}")

@user_router.callback_query(F.data == "run_step1")
async def process_step1(call: CallbackQuery) -> None:
    try:
        if await is_user_banned(call.from_user.id):
            await call.answer("🚫 You are banned from using this bot.", show_alert=True)
            return
            
        await send_custom_content(call.message.chat.id, "step1_msg")
        await call.answer()
    except Exception as e:
        logger.error(f"Step 1 error: {e}")

@user_router.callback_query(F.data == "run_step2")
async def process_step2(call: CallbackQuery) -> None:
    try:
        if await is_user_banned(call.from_user.id):
            await call.answer("🚫 You are banned from using this bot.", show_alert=True)
            return

        # Fetch 2 random DPs from DP bank
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT file_id FROM dp_bank ORDER BY RANDOM() LIMIT 2") as cursor:
                dps = await cursor.fetchall()
        
        if len(dps) > 0:
            media_group = [InputMediaPhoto(media=dp[0]) for dp in dps]
            await bot.send_media_group(call.message.chat.id, media=media_group)
        else:
            pass # Keep it clean if no DP exists, just send the instructions below

        # Send Step 2 main message with Unlock button for Step 3
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="I have done this step", callback_data="req_unlock_3")]
        ])
        await send_custom_content(call.message.chat.id, "step2_msg", reply_markup=keyboard)
        await call.answer()
    except Exception as e:
        logger.error(f"Step 2 error: {e}")

@user_router.callback_query(F.data == "req_unlock_3")
async def request_step3(call: CallbackQuery) -> None:
    try:
        if await is_user_banned(call.from_user.id):
            await call.answer("🚫 You are banned.", show_alert=True)
            return

        user_id = call.from_user.id
        username = call.from_user.username or "No Username"
        profile_link = f"<a href='tg://user?id={user_id}'>{username}</a>"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_3_{user_id}"),
             InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_3_{user_id}")]
        ])
        
        admin_msg = f"🔓 <b>Step 3 Unlock Request</b>\n\n👤 User: {profile_link}\n🆔 ID: <code>{user_id}</code>\n💬 User says: I have done this step."
        await bot.send_message(ADMIN_ID, admin_msg, reply_markup=keyboard)
        
        await call.message.answer("⏳ Your request for Step 3 has been sent to the admin. Please wait for approval.")
        await call.answer()
    except Exception as e:
        logger.error(f"Request Step 3 error: {e}")

@user_router.callback_query(F.data == "run_step3")
async def process_step3(call: CallbackQuery) -> None:
    try:
        if await is_user_banned(call.from_user.id):
            await call.answer("🚫 You are banned.", show_alert=True)
            return

        user_data = await get_user(call.from_user.id)
        if not user_data or user_data[0] == 0:
            await call.answer("❌ Access Denied. Contact Admin.", show_alert=True)
            return

        batch_counter = user_data[2]
        batch_size = 6
        offset = batch_counter * batch_size

        # Fetch videos for this batch
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT file_id FROM video_dump ORDER BY id ASC LIMIT ? OFFSET ?", (batch_size, offset)) as cursor:
                videos = await cursor.fetchall()

        if not videos:
            await call.message.answer("📭 No more videos available in the batch at the moment.")
        else:
            media_group = [InputMediaVideo(media=vid[0]) for vid in videos]
            await bot.send_media_group(call.message.chat.id, media=media_group)
            
            # Increment batch
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET video_batch = video_batch + 1 WHERE user_id = ?", (call.from_user.id,))
                await db.commit()

        # Send Step 3 custom content with Unlock Step 4 button
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="I have done this step", callback_data="req_unlock_4")]
        ])
        await send_custom_content(call.message.chat.id, "step3_msg", reply_markup=keyboard)
        await call.answer()
    except Exception as e:
        logger.error(f"Step 3 error: {e}")

@user_router.callback_query(F.data == "req_unlock_4")
async def request_step4(call: CallbackQuery) -> None:
    try:
        if await is_user_banned(call.from_user.id):
            await call.answer("🚫 You are banned.", show_alert=True)
            return

        user_id = call.from_user.id
        username = call.from_user.username or "No Username"
        profile_link = f"<a href='tg://user?id={user_id}'>{username}</a>"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_4_{user_id}"),
             InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_4_{user_id}")]
        ])
        
        admin_msg = f"🔓 <b>Step 4 Unlock Request</b>\n\n👤 User: {profile_link}\n🆔 ID: <code>{user_id}</code>\n💬 User says: I have done this step."
        await bot.send_message(ADMIN_ID, admin_msg, reply_markup=keyboard)
        
        await call.message.answer("⏳ Your request for Step 4 has been sent to the admin. Please wait for approval.")
        await call.answer()
    except Exception as e:
        logger.error(f"Request Step 4 error: {e}")

@user_router.callback_query(F.data == "run_step4")
async def process_step4(call: CallbackQuery) -> None:
    try:
        if await is_user_banned(call.from_user.id):
            await call.answer("🚫 You are banned.", show_alert=True)
            return

        user_data = await get_user(call.from_user.id)
        if not user_data or user_data[1] == 0:
            await call.answer("❌ Access Denied. Contact Admin.", show_alert=True)
            return

        await send_custom_content(call.message.chat.id, "step4_msg")
        await call.answer()
    except Exception as e:
        logger.error(f"Step 4 error: {e}")

# ----------------- ADMIN APPROVAL CALLBACKS ----------------- #
@admin_router.callback_query(F.data.startswith("approve_"), F.from_user.id == ADMIN_ID)
async def admin_approve_request(call: CallbackQuery) -> None:
    try:
        parts = call.data.split('_')
        step = parts[1]
        target_user_id = int(parts[2])

        async with aiosqlite.connect(DB_PATH) as db:
            if step == "3":
                await db.execute("UPDATE users SET step3_unlocked = 1 WHERE user_id = ?", (target_user_id,))
                msg_to_user = "✅ Successfully unlocked your Step 3. Please check and run Step 3 from the main menu."
            elif step == "4":
                await db.execute("UPDATE users SET step4_unlocked = 1 WHERE user_id = ?", (target_user_id,))
                msg_to_user = "✅ Successfully unlocked your Step 4. Please check and run Step 4 from the main menu."
            await db.commit()

        await call.message.edit_text(f"{call.message.html_text}\n\n✅ <b>Approved Successfully</b>")
        
        # Notify the user
        try:
            await bot.send_message(target_user_id, msg_to_user)
        except TelegramAPIError:
            logger.warning(f"Could not notify user {target_user_id} about approval. They might have blocked the bot.")
        
        await call.answer("Approved!")
    except Exception as e:
        logger.error(f"Approval callback error: {e}")

@admin_router.callback_query(F.data.startswith("deny_"), F.from_user.id == ADMIN_ID)
async def admin_deny_request(call: CallbackQuery) -> None:
    try:
        parts = call.data.split('_')
        step = parts[1]
        target_user_id = int(parts[2])

        await call.message.edit_text(f"{call.message.html_text}\n\n❌ <b>Denied by Admin</b>")
        
        # Notify the user
        try:
            await bot.send_message(target_user_id, f"❌ Your request to unlock Step {step} was denied by the Admin. Please check your tasks again.")
        except TelegramAPIError:
            logger.warning(f"Could not notify user {target_user_id} about denial.")
            
        await call.answer("Request Denied.")
    except Exception as e:
        logger.error(f"Denial callback error: {e}")

# ----------------- MAIN RUNNER ----------------- #
async def main() -> None:
    await init_db()
    logger.info("Bot is successfully running...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
