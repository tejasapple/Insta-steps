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
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

import motor.motor_asyncio

# ----------------- CONFIGURATION ----------------- #
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
MONGO_URI = os.getenv("MONGO_URI")

if not BOT_TOKEN or not ADMIN_ID or not MONGO_URI:
    raise ValueError("Bhai, .env file mein BOT_TOKEN, ADMIN_ID aur MONGO_URI set karna zaruri hai!")

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

# ----------------- DATABASE SETUP ----------------- #
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo_client["telegram_bot_db"]

async def init_db() -> None:
    """Initialize the async MongoDB database and required indexes."""
    try:
        # Create unique index for users to prevent duplicates
        await db.users.create_index("user_id", unique=True)
        await db.settings.create_index("key", unique=True)
        logger.info("MongoDB initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

# Database Helper Functions
async def set_setting(key: str, text_val: str, media_id: Optional[str] = None, media_type: Optional[str] = None) -> None:
    await db.settings.update_one(
        {"key": key},
        {"$set": {"text_val": text_val, "media_id": media_id, "media_type": media_type}},
        upsert=True
    )

async def get_setting(key: str) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    doc = await db.settings.find_one({"key": key})
    if doc:
        return (doc.get("text_val", ""), doc.get("media_id"), doc.get("media_type"))
    return None

async def register_user(user_id: int, username: Optional[str]) -> None:
    await db.users.update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "step3_unlocked": 0,
                "step4_unlocked": 0,
                "video_batch": 0,
                "is_banned": 0,
                "user_sent_batches": []
            },
            "$set": {"username": username}
        },
        upsert=True
    )

async def get_user(user_id: int) -> Optional[dict]:
    return await db.users.find_one({"user_id": user_id})

async def is_user_banned(user_id: int) -> bool:
    user_data = await get_user(user_id)
    if user_data and user_data.get("is_banned", 0) == 1:
        return True
    return False

# ----------------- CORE VIDEO DELIVERY FUNCTION ----------------- #
async def deliver_random_dump_videos(
    bot: Bot, 
    user_id: int, 
    dump_chat_id: int, 
    base_msg_id: int, 
    total_videos: int, 
    user_sent_batches: list,
    batches_to_send: int = 1
) -> list:
    """
    Core method to fetch random unique videos from a dump channel.
    Returns the updated list of sent_batches for the user so you can save it to MongoDB.
    """
    # 1. Total available chunks of 6 videos
    total_batches_available = total_videos // 6
    
    # 2. Filter out batches the user has already received
    available_batches = [i for i in range(total_batches_available) if i not in user_sent_batches]
    
    # Check if we have enough unique batches left
    if len(available_batches) < batches_to_send:
        logger.warning(f"Not enough unique videos left for user {user_id}")
        return user_sent_batches # Return unchanged

    # 3. Randomly select the required number of batches
    selected_batches = random.sample(available_batches, batches_to_send)
    
    # 4. Extract and copy messages from the Dump Channel
    for batch_idx in selected_batches:
        start_msg_id = base_msg_id + (batch_idx * 6)
        
        success_count = 0
        for i in range(6):
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=dump_chat_id,
                    message_id=start_msg_id + i
                )
                success_count += 1
                await asyncio.sleep(0.3) # Anti-flood delay
            except Exception as e:
                logger.error(f"Failed to copy msg {start_msg_id + i} to {user_id}: {e}")
                
        if success_count > 0:
            user_sent_batches.append(batch_idx)
            
    return user_sent_batches

# ----------------- ADMIN STATES & HANDLERS ----------------- #
class AdminState(StatesGroup):
    waiting_for_start_msg = State()
    waiting_for_dp_channel = State()
    waiting_for_dump_channel = State()
    waiting_for_broadcast = State()
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
        await db.users.update_one({"user_id": target_id}, {"$set": {"is_banned": 1}})
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
        await db.users.update_one({"user_id": target_id}, {"$set": {"is_banned": 0}})
        await message.answer(f"✅ User {target_id} has been unbanned successfully.")
    except Exception as e:
        logger.error(f"Unban error: {e}")
        await message.answer("❌ Invalid User ID or Error occurred.")

@admin_router.callback_query(F.data == "admin_stats", F.from_user.id == ADMIN_ID)
async def show_stats(call: CallbackQuery) -> None:
    try:
        total_users = await db.users.count_documents({})
        banned_users = await db.users.count_documents({"is_banned": 1})
        
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
        
        async for user_doc in db.users.find({"is_banned": 0}):
            uid = user_doc["user_id"]
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
        
        cursor = db.step_messages.find({"step_name": step_name}).sort("order_index", 1)
        messages = await cursor.to_list(length=None)
        
        parts_text = f"🛠 <b>Editing {step_name.upper()}</b>\n\nCurrent Assigned Messages:\n"
        if not messages:
            parts_text += "<i>No messages configured yet.</i>\n"
        else:
            for i, msg in enumerate(messages, 1):
                m_type = msg.get("msg_type", "text")
                txt = msg.get("text_val", "")
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
            
        last_doc = await db.step_messages.find_one({"step_name": step_name}, sort=[("order_index", -1)])
        order_index = (last_doc["order_index"] + 1) if last_doc else 1
        
        await db.step_messages.insert_one({
            "step_name": step_name, 
            "msg_type": msg_type, 
            "media_id": media_id, 
            "text_val": text_val, 
            "order_index": order_index
        })
            
        success_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🔙 Go Back to {step_name.upper()}", callback_data=f"admin_edit_{step_name}")]
        ])
        await message.answer(f"✅ Successfully added <b>{msg_type.upper()}</b> as Part {order_index} in {step_name.upper()}!", reply_markup=success_keyboard)
        await state.set_state(None)
    except Exception as e:
        logger.error(f"Error saving step part: {e}")

@admin_router.callback_query(F.data == "clear_step_parts", F.from_user.id == ADMIN_ID)
async def clear_step_parts(call: CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        step_name = data.get("current_step")
        if step_name:
            await db.step_messages.delete_many({"step_name": step_name})
            
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
            "dp_channel": ("waiting_for_dp_channel", "Send the Channel/Group ID for DP Bank (e.g. -100123456789). Bot must be admin there."),
            "dump_channel": ("waiting_for_dump_channel", "Send the Channel/Group ID for Video Dump (e.g. -100123456789). Bot must be admin there.")
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
        await db.settings.delete_one({"key": "dp_stats"}) # Reset stats on new channel
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_panel_open")]])
        await message.answer("✅ DP Channel/Group ID saved! Forward DPs to track them without saving files to DB.", reply_markup=keyboard)
        await state.clear()
    except Exception as e:
        logger.error(f"Error saving DP channel: {e}")

@admin_router.message(AdminState.waiting_for_dump_channel, F.from_user.id == ADMIN_ID)
async def save_dump_channel(message: Message, state: FSMContext) -> None:
    try:
        await set_setting("dump_channel", message.text.strip())
        await db.settings.delete_one({"key": "video_stats"}) # Reset stats on new channel
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_panel_open")]])
        await message.answer("✅ Video Dump Channel/Group ID saved! Forward videos to track them without saving files to DB.", reply_markup=keyboard)
        await state.clear()
    except Exception as e:
        logger.error(f"Error saving Dump channel: {e}")

# ----------------- CHANNEL LISTENER (AUTO TRACK METADATA ONLY) ----------------- #
@channel_router.message(F.photo | F.video)
@channel_router.channel_post(F.photo | F.video)
async def listen_channels(message: Message) -> None:
    """
    Keeps photos/videos OUT of MongoDB. 
    Only records the base_msg_id (lowest msg id) and total count.
    """
    try:
        dp_setting = await get_setting("dp_channel")
        dump_setting = await get_setting("dump_channel")
        chat_id = str(message.chat.id)

        if dp_setting and chat_id == dp_setting[0] and message.photo:
            await db.settings.update_one(
                {"key": "dp_stats"},
                {
                    "$inc": {"total": 1}, 
                    "$min": {"base_msg_id": message.message_id}
                },
                upsert=True
            )
            logger.info("Tracked new DP metadata. No media saved in DB.")
        
        if dump_setting and chat_id == dump_setting[0] and message.video:
            await db.settings.update_one(
                {"key": "video_stats"},
                {
                    "$inc": {"total": 1}, 
                    "$min": {"base_msg_id": message.message_id}
                },
                upsert=True
            )
            logger.info("Tracked new Video metadata. No media saved in DB.")
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
        cursor = db.step_messages.find({"step_name": step_name}).sort("order_index", 1)
        messages = await cursor.to_list(length=None)
        
        if not messages:
            await bot.send_message(chat_id, f"⚠️ Admin hasn't set any messages for {step_name.title()} yet.", reply_markup=final_markup)
            return

        for i, msg in enumerate(messages):
            msg_type = msg.get("msg_type")
            media_id = msg.get("media_id")
            text_val = msg.get("text_val", "")
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

        stats = await db.settings.find_one({"key": "dp_stats"})
        dp_chat_setting = await get_setting("dp_channel")
        
        if not stats or not dp_chat_setting:
            await call.message.answer("📭 No DPs available. Please ensure DP Channel is setup and DPs are posted.")
        else:
            base_msg_id = stats.get("base_msg_id", 0)
            total = stats.get("total", 0)
            dp_chat_id = int(dp_chat_setting[0])
            
            if total > 0:
                count_to_send = min(2, total)
                selected_offsets = random.sample(range(total), count_to_send)
                for offset in selected_offsets:
                    msg_id = base_msg_id + offset
                    try:
                        await bot.copy_message(
                            chat_id=call.from_user.id,
                            from_chat_id=dp_chat_id,
                            message_id=msg_id
                        )
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.error(f"Failed to copy DP msg {msg_id}: {e}")
        
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
        if not user_data or user_data.get("step3_unlocked", 0) == 0:
            await call.answer("❌ Access Denied. Contact Admin.", show_alert=True)
            return

        # Core logic: Pull directly from Dump Channel via msg ID ranges
        stats = await db.settings.find_one({"key": "video_stats"})
        dump_chat_setting = await get_setting("dump_channel")

        if not stats or not dump_chat_setting:
            await call.message.answer("📭 Video dump is not configured or empty. Group me nayi videos send karo.")
        else:
            base_msg_id = stats.get("base_msg_id", 0)
            total_videos = stats.get("total", 0)
            dump_chat_id = int(dump_chat_setting[0])
            user_sent_batches = user_data.get("user_sent_batches", [])

            updated_batches = await deliver_random_dump_videos(
                bot=bot,
                user_id=call.from_user.id,
                dump_chat_id=dump_chat_id,
                base_msg_id=base_msg_id,
                total_videos=total_videos,
                user_sent_batches=user_sent_batches,
                batches_to_send=2  # Fulfill the TWO batches requirement from old code
            )
            
            # Save strictly text/metadata (user_sent_batches) back to MongoDB
            await db.users.update_one(
                {"user_id": call.from_user.id},
                {"$set": {"user_sent_batches": updated_batches}, "$inc": {"video_batch": 2}}
            )

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
        if not user_data or user_data.get("step4_unlocked", 0) == 0:
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

        if step == "3":
            await db.users.update_one({"user_id": target_user_id}, {"$set": {"step3_unlocked": 1}})
            msg_to_user = "✅ Successfully unlocked your Step 3. Please check and run Step 3 from the main menu."
        elif step == "4":
            await db.users.update_one({"user_id": target_user_id}, {"$set": {"step4_unlocked": 1}})
            msg_to_user = "✅ Successfully unlocked your Step 4. Please check and run Step 4 from the main menu."

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
