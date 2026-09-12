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
            # Table for Users
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
            
            # Safe migration for existing databases
            try:
                await db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
            except Exception:
                pass 
            
            # Legacy Table for single Settings (like Start msg, DP channel, Dump channel)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    text_val TEXT,
                    media_id TEXT,
                    media_type TEXT
                )
            """)
            
            # NEW Table for Multi-message Step configuration
            await db.execute("""
                CREATE TABLE IF NOT EXISTS step_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_name TEXT,
                    msg_type TEXT,
                    media_id TEXT,
                    text_val TEXT,
                    order_index INTEGER
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
    waiting_for_dp_channel = State()
    waiting_for_dump_channel = State()
    waiting_for_broadcast = State()
    # New Multi-message setup states
    waiting_for_step_content = State()

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Set Start Msg", callback_data="admin_set_start")],
        [InlineKeyboardButton(text="Set Step 1", callback_data="admin_edit_step1"),
         InlineKeyboardButton(text="Set Step 2", callback_data="admin_edit_step2")],
        [InlineKeyboardButton(text="Set Step 3", callback_data="admin_edit_step3"),
         InlineKeyboardButton(text="Set Step 4", callback_data="admin_edit_step4")],
        [InlineKeyboardButton(text="Set DP Channel ID", callback_data="admin_set_dp_channel"),
         InlineKeyboardButton(text="Set Dump Channel ID", callback_data="admin_set_dump_channel")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats")]
    ])

@admin_router.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_panel_cmd(message: Message, state: FSMContext) -> None:
    await send_admin_panel(message.chat.id, state)

@admin_router.callback_query(F.data == "admin_panel_open", F.from_user.id == ADMIN_ID)
async def admin_panel_callback(call: CallbackQuery, state: FSMContext) -> None:
    await send_admin_panel(call.message.chat.id, state)
    await call.answer()

async def send_admin_panel(chat_id: int, state: FSMContext) -> None:
    try:
        await state.clear()
        help_text = (
            "🛠 <b>Admin Panel</b>\n"
            "Select what you want to customize below.\n\n"
            "<b>Ban/Unban Commands:</b>\n"
            "To ban a user: <code>/ban user_id</code>\n"
            "To unban a user: <code>/unban user_id</code>"
        )
        await bot.send_message(chat_id, help_text, reply_markup=admin_keyboard())
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
            await asyncio.sleep(0.1)
            
        report = (
            "✅ <b>Broadcast Completed!</b>\n\n"
            f"📨 Successfully sent to: {success} users\n"
            f"❌ Failed: {failed} users"
        )
        await processing_msg.edit_text(report)
    except Exception as e:
        logger.error(f"Broadcast execution error: {e}")
        await message.answer("❌ Error occurred during broadcast.")

# ----------------- ADMIN MULTI-MESSAGE STEP SETUP ----------------- #
@admin_router.callback_query(F.data.startswith("admin_edit_step"), F.from_user.id == ADMIN_ID)
async def admin_edit_step(call: CallbackQuery, state: FSMContext) -> None:
    try:
        step_name = call.data.replace("admin_edit_", "")
        await state.update_data(current_step=step_name)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT msg_type, text_val FROM step_messages WHERE step_name = ? ORDER BY order_index ASC", (step_name,)) as cursor:
                messages = await cursor.fetchall()
        
        parts_text = f"🛠 <b>Editing {step_name.upper()}</b>\n\nCurrent Assigned Messages:\n"
        if not messages:
            parts_text += "<i>No messages configured yet.</i>\n"
        else:
            for i, (m_type, txt) in enumerate(messages, 1):
                preview = txt[:25] + "..." if txt and len(txt) > 25 else (txt or "No Caption/Text")
                parts_text += f"{i}. <b>[{m_type.upper()}]</b> - {preview}\n"
                
        parts_text += "\n<i>What do you want to add next? The bot will send them in order (1, 2, 3...).</i>"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Set Text", callback_data="add_part_text"),
             InlineKeyboardButton(text="➕ Set Photo", callback_data="add_part_photo")],
            [InlineKeyboardButton(text="➕ Set Video", callback_data="add_part_video"),
             InlineKeyboardButton(text="➕ Set Voice", callback_data="add_part_voice")],
            [InlineKeyboardButton(text="🗑 Clear All in this Step", callback_data="clear_step_parts")],
            [InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_panel_open")]
        ])
        
        await call.message.edit_text(parts_text, reply_markup=keyboard)
        await call.answer()
    except Exception as e:
        logger.error(f"Admin edit step error: {e}")

@admin_router.callback_query(F.data.startswith("add_part_"), F.from_user.id == ADMIN_ID)
async def add_part_prompt(call: CallbackQuery, state: FSMContext) -> None:
    try:
        msg_type = call.data.replace("add_part_", "")
        data = await state.get_data()
        step_name = data.get("current_step", "Unknown Step")
        
        await state.update_data(expected_type=msg_type)
        await state.set_state(AdminState.waiting_for_step_content)
        
        await call.message.edit_text(f"📤 Please send the <b>{msg_type.upper()}</b> for {step_name.upper()}.\n\n<i>Note: You can add captions if sending media.</i>")
        await call.answer()
    except Exception as e:
        logger.error(f"Add part prompt error: {e}")

@admin_router.message(AdminState.waiting_for_step_content, F.from_user.id == ADMIN_ID)
async def save_step_part(message: Message, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        step_name = data.get("current_step")
        expected_type = data.get("expected_type")
        
        if not step_name:
            await message.answer("❌ Session expired. Please open admin panel again.")
            await state.clear()
            return
            
        text_val = message.html_text or ""
        media_id = None
        msg_type = 'text'
        
        if expected_type == 'photo' and message.photo:
            media_id = message.photo[-1].file_id
            msg_type = 'photo'
        elif expected_type == 'video' and message.video:
            media_id = message.video.file_id
            msg_type = 'video'
        elif expected_type == 'voice' and message.voice:
            media_id = message.voice.file_id
            msg_type = 'voice'
        elif expected_type == 'text' and message.text:
            msg_type = 'text'
        else:
            await message.answer(f"⚠️ Invalid format! I am expecting a <b>{expected_type.upper()}</b>. Please try again.")
            return
            
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT MAX(order_index) FROM step_messages WHERE step_name = ?", (step_name,)) as cursor:
                res = await cursor.fetchone()
                order_index = (res[0] or 0) + 1
            await db.execute("INSERT INTO step_messages (step_name, msg_type, media_id, text_val, order_index) VALUES (?, ?, ?, ?, ?)",
                             (step_name, msg_type, media_id, text_val, order_index))
            await db.commit()
            
        success_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🔙 Go Back to {step_name.upper()}", callback_data=f"admin_edit_{step_name}")]
        ])
        await message.answer(f"✅ Successfully added <b>{msg_type.upper()}</b> as Part {order_index} in {step_name.upper()}!", reply_markup=success_keyboard)
        await state.set_state(None) # Clear state but keep data for easy back navigation
    except Exception as e:
        logger.error(f"Error saving step part: {e}")

@admin_router.callback_query(F.data == "clear_step_parts", F.from_user.id == ADMIN_ID)
async def clear_step_parts(call: CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        step_name = data.get("current_step")
        if step_name:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM step_messages WHERE step_name = ?", (step_name,))
                await db.commit()
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🔙 Go Back to {step_name.upper()}", callback_data=f"admin_edit_{step_name}")]
            ])
            await call.message.edit_text(f"🗑 All messages for <b>{step_name.upper()}</b> have been cleared!", reply_markup=keyboard)
            await call.answer()
        else:
            await call.answer("❌ Error: Step not found.", show_alert=True)
    except Exception as e:
        logger.error(f"Error clearing step parts: {e}")

# Admin Single Settings (Start Msg, Channels)
@admin_router.callback_query(F.data.startswith("admin_set_"), F.from_user.id == ADMIN_ID)
async def admin_setup_single_callbacks(call: CallbackQuery, state: FSMContext) -> None:
    try:
        action = call.data.replace("admin_set_", "")
        prompts = {
            "start": ("waiting_for_start_msg", "Send the new START message (Text/Photo/Video/Voice)."),
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
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_panel_open")]
        ])
        await message.answer(f"✅ Successfully saved {key_name.replace('_', ' ').title()}!", reply_markup=keyboard)
        await state.clear()
    except Exception as e:
        logger.error(f"Error saving media setting: {e}")
        await message.answer("❌ Error saving setting.")

@admin_router.message(AdminState.waiting_for_start_msg, F.from_user.id == ADMIN_ID)
async def save_start(msg: Message, state: FSMContext) -> None: 
    await save_media_setting(msg, state, "start_msg")

@admin_router.message(AdminState.waiting_for_dp_channel, F.from_user.id == ADMIN_ID)
async def save_dp_channel(message: Message, state: FSMContext) -> None:
    try:
        await set_setting("dp_channel", message.text.strip())
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_panel_open")]])
        await message.answer("✅ DP Channel ID saved! Bot will now auto-save any photos posted there.", reply_markup=keyboard)
        await state.clear()
    except Exception as e:
        logger.error(f"Error saving DP channel: {e}")

@admin_router.message(AdminState.waiting_for_dump_channel, F.from_user.id == ADMIN_ID)
async def save_dump_channel(message: Message, state: FSMContext) -> None:
    try:
        await set_setting("dump_channel", message.text.strip())
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_panel_open")]])
        await message.answer("✅ Video Dump Channel ID saved! Bot will now auto-save any videos posted there.", reply_markup=keyboard)
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
def main_steps_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="1️⃣ Step 1", callback_data="run_step1")],
        [InlineKeyboardButton(text="2️⃣ Step 2", callback_data="run_step2")],
        [InlineKeyboardButton(text="3️⃣ Step 3", callback_data="run_step3")],
        [InlineKeyboardButton(text="4️⃣ Step 4", callback_data="run_step4")]
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin_panel_open")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def send_custom_step_content(chat_id: int, step_name: str, final_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT msg_type, media_id, text_val FROM step_messages WHERE step_name = ? ORDER BY order_index ASC", (step_name,)) as cursor:
                messages = await cursor.fetchall()
        
        if not messages:
            await bot.send_message(chat_id, f"⚠️ Admin hasn't set any messages for {step_name.title()} yet.", reply_markup=final_markup)
            return

        for i, (msg_type, media_id, text_val) in enumerate(messages):
            # Only attach the inline keyboard to the LAST message of the step
            markup = final_markup if i == len(messages) - 1 else None
            
            try:
                if msg_type == 'photo':
                    await bot.send_photo(chat_id, photo=media_id, caption=text_val, reply_markup=markup)
                elif msg_type == 'video':
                    await bot.send_video(chat_id, video=media_id, caption=text_val, reply_markup=markup)
                elif msg_type == 'voice':
                    await bot.send_voice(chat_id, voice=media_id, caption=text_val, reply_markup=markup)
                else:
                    await bot.send_message(chat_id, text=text_val, reply_markup=markup)
            except TelegramAPIError as e:
                logger.error(f"Failed to send part of {step_name}: {e}")
            
            # Anti-flood delay between sending multiple parts
            await asyncio.sleep(0.3)
            
    except Exception as e:
        logger.error(f"Error sending step content: {e}")

@user_router.message(CommandStart())
async def start_cmd(message: Message) -> None:
    try:
        if await is_user_banned(message.from_user.id):
            return
            
        await register_user(message.from_user.id, message.from_user.username)
        
        content = await get_setting("start_msg")
        is_admin = (message.from_user.id == ADMIN_ID)
        keyboard = main_steps_keyboard(is_admin)
        
        if not content:
            await message.answer("Welcome! Please check Admin Panel and setup the start message.", reply_markup=keyboard)
            return

        text_val, media_id, media_type = content
        if media_type == 'photo':
            await message.answer_photo(photo=media_id, caption=text_val, reply_markup=keyboard)
        elif media_type == 'video':
            await message.answer_video(video=media_id, caption=text_val, reply_markup=keyboard)
        elif media_type == 'voice':
            await message.answer_voice(voice=media_id, caption=text_val, reply_markup=keyboard)
        else:
            await message.answer(text=text_val, reply_markup=keyboard)
            
    except Exception as e:
        logger.error(f"Start command error: {e}")

@user_router.callback_query(F.data == "run_step1")
async def process_step1(call: CallbackQuery) -> None:
    try:
        if await is_user_banned(call.from_user.id):
            await call.answer("🚫 You are banned from using this bot.", show_alert=True)
            return
            
        await send_custom_step_content(call.message.chat.id, "step1")
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
        
        try:
            # Fix: Handle cases where only 1 or >= 2 photos are available to avoid send_media_group errors
            if len(dps) == 1:
                await bot.send_photo(call.message.chat.id, photo=dps[0][0])
            elif len(dps) >= 2:
                media_group = [InputMediaPhoto(media=dp[0]) for dp in dps]
                await bot.send_media_group(call.message.chat.id, media=media_group)
        except TelegramAPIError as e:
            logger.error(f"Failed to send DPs in Step 2: {e}")
        
        # Send Multi-message Step 2 content with Unlock button for Step 3 at the very end
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="I have done this step", callback_data="req_unlock_3")]
        ])
        await send_custom_step_content(call.message.chat.id, "step2", final_markup=keyboard)
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
        
        # Checking if user already exhausted their batches
        if batch_counter >= 2:
            await call.message.answer("📭 You have already received all the video batches available.")
        else:
            # Fix: Fetch up to 12 Random videos for sending TWO batches at once
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT file_id FROM video_dump ORDER BY RANDOM() LIMIT 12") as cursor:
                    videos = await cursor.fetchall()

            if not videos:
                await call.message.answer("📭 No more videos available in the bank right now.")
            else:
                # Divide into Batch 1 and Batch 2
                batch1 = videos[:6]
                batch2 = videos[6:12]

                try:
                    # Send Batch 1
                    if len(batch1) == 1:
                        await bot.send_video(call.message.chat.id, video=batch1[0][0])
                    elif len(batch1) > 1:
                        media_group1 = [InputMediaVideo(media=vid[0]) for vid in batch1]
                        await bot.send_media_group(call.message.chat.id, media=media_group1)
                    
                    # Short delay to prevent Telegram FloodWait API error between sending large media chunks
                    if batch2:
                        await asyncio.sleep(0.5)
                        
                        # Send Batch 2
                        if len(batch2) == 1:
                            await bot.send_video(call.message.chat.id, video=batch2[0][0])
                        elif len(batch2) > 1:
                            media_group2 = [InputMediaVideo(media=vid[0]) for vid in batch2]
                            await bot.send_media_group(call.message.chat.id, media=media_group2)
                    
                    # Increment batch counter by 2 since we sent two batches at once
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE users SET video_batch = video_batch + 2 WHERE user_id = ?", (call.from_user.id,))
                        await db.commit()

                except TelegramAPIError as e:
                    logger.error(f"Failed to send video batches in Step 3: {e}")

        # Send Multi-message Step 3 content with Unlock Step 4 button at the very end
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="I have done this step", callback_data="req_unlock_4")]
        ])
        await send_custom_step_content(call.message.chat.id, "step3", final_markup=keyboard)
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

        await send_custom_step_content(call.message.chat.id, "step4")
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
