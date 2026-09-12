import os
import re
import json
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
    InlineKeyboardButton, FSInputFile, BufferedInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

import motor.motor_asyncio

# ----------------- CONFIGURATION ----------------- #
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
ADMIN_ID_2 = int(os.getenv("ADMIN_ID_2", 0))

# Store valid admin IDs dynamically
ADMIN_IDS = [aid for aid in (ADMIN_ID, ADMIN_ID_2) if aid != 0]
MONGO_URI = os.getenv("MONGO_URI")

if not BOT_TOKEN or not ADMIN_IDS or not MONGO_URI:
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
        await db.admins.create_index("user_id", unique=True)
        
        # Load dynamically added admins from DB into memory
        async for admin_doc in db.admins.find({}):
            aid = admin_doc.get("user_id")
            if aid and aid not in ADMIN_IDS:
                ADMIN_IDS.append(aid)
                
        logger.info(f"MongoDB initialized successfully. Total Admins: {len(ADMIN_IDS)}")
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
    is_admin = 1 if user_id in ADMIN_IDS else 0
    await db.users.update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "is_approved": 1, # Everyone is natively approved to use Step 1 & 2
                "step1_used": 0,
                "step2_used": 0,
                "step3_used": 0,
                "step4_used": 0,
                "step3_unlocked": is_admin, # Admins auto-unlocked
                "step4_unlocked": is_admin, # Admins auto-unlocked
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

async def is_user_approved(user_id: int) -> bool:
    user_data = await get_user(user_id)
    if user_data and user_data.get("is_approved", 0) == 1:
        return True
    return False

async def extract_user_id_from_message(message: Message) -> Optional[int]:
    """Helper to extract user ID from a forwarded message, @username, or raw ID."""
    if message.forward_origin:
        if message.forward_origin.type == 'user':
            return message.forward_origin.sender_user.id
    elif getattr(message, 'forward_from', None):
        return message.forward_from.id
    elif message.text:
        text = message.text.strip()
        if text.isdigit():
            return int(text)
        else:
            uname = text.replace("@", "").strip()
            user_doc = await db.users.find_one({"username": re.compile(f"^{uname}$", re.IGNORECASE)})
            if user_doc:
                return user_doc["user_id"]
    return None

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
    total_batches_available = total_videos // 6
    available_batches = [i for i in range(total_batches_available) if i not in user_sent_batches]
    
    if len(available_batches) < batches_to_send:
        logger.warning(f"Not enough unique videos left for user {user_id}")
        return user_sent_batches 

    selected_batches = random.sample(available_batches, batches_to_send)
    
    for idx, batch_idx in enumerate(selected_batches):
        try:
            await bot.send_message(chat_id=user_id, text=f"<b>Batch {idx + 1}</b>")
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Failed to send Batch {idx + 1} text to {user_id}: {e}")

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
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Failed to copy msg {start_msg_id + i} to {user_id}: {e}")
                
        if success_count > 0:
            user_sent_batches.append(batch_idx)
            
    return user_sent_batches

# ----------------- BACKUP SYSTEM (NEW) ----------------- #
async def generate_backup() -> str:
    """Creates a JSON backup of the entire MongoDB database."""
    backup_data = {}
    try:
        collections = await db.list_collection_names()
        for coll_name in collections:
            cursor = db[coll_name].find({})
            docs = await cursor.to_list(length=None)
            for doc in docs:
                if '_id' in doc:
                    doc['_id'] = str(doc['_id'])
            backup_data[coll_name] = docs
            
        backup_file = "database_backup.json"
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=4)
        return backup_file
    except Exception as e:
        logger.error(f"Error generating backup file: {e}")
        return ""

async def send_backup(bot: Bot, admin_ids: List[int]) -> None:
    """Sends the JSON backup to all Admins and deletes it from the VPS."""
    try:
        backup_file = await generate_backup()
        if backup_file and os.path.exists(backup_file):
            for admin_id in admin_ids:
                try:
                    document = FSInputFile(backup_file)
                    await bot.send_document(
                        chat_id=admin_id, 
                        document=document, 
                        caption="📦 <b>Automated Database Backup</b>\n\nAll users, settings, and step configurations are included. Your VPS remains clean (file is automatically deleted from the server)."
                    )
                except Exception as e:
                    logger.error(f"Failed to send automated backup to {admin_id}: {e}")
                    
            os.remove(backup_file)
            logger.info("Backup sent to admins and VPS cleaned successfully.")
    except Exception as e:
        logger.error(f"Failed to process backup operation: {e}")

async def auto_backup_task(bot: Bot, admin_ids: List[int]) -> None:
    """Runs continuously in the background, executing every 8 hours."""
    while True:
        await asyncio.sleep(8 * 3600)  # Wait for 8 hours
        await send_backup(bot, admin_ids)

# ----------------- ADMIN STATES & HANDLERS ----------------- #
class AdminState(StatesGroup):
    waiting_for_start_msg = State()
    waiting_for_dp_channel = State()
    waiting_for_dump_channel = State()
    waiting_for_broadcast = State()
    waiting_for_step_content = State()
    waiting_for_add_user_id = State()
    waiting_for_remove_user_id = State()
    waiting_for_add_admin_id = State()

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Manage Users", callback_data="admin_manage_users"),
         InlineKeyboardButton(text="👑 Add Admin", callback_data="admin_add_admin_prompt")],
        [InlineKeyboardButton(text="Set Start Msg", callback_data="admin_set_start")],
        [InlineKeyboardButton(text="Set Step 1", callback_data="admin_edit_step1"),
         InlineKeyboardButton(text="Set Step 2", callback_data="admin_edit_step2")],
        [InlineKeyboardButton(text="Set Step 3", callback_data="admin_edit_step3"),
         InlineKeyboardButton(text="Set Step 4", callback_data="admin_edit_step4")],
        [InlineKeyboardButton(text="Set DP Channel ID", callback_data="admin_set_dp_channel"),
         InlineKeyboardButton(text="Set Dump Channel ID", callback_data="admin_set_dump_channel")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📦 Manual Backup", callback_data="admin_backup")]
    ])

def user_management_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 View Active Users", callback_data="admin_view_users")],
        [InlineKeyboardButton(text="➕ Add User", callback_data="admin_add_user_prompt"),
         InlineKeyboardButton(text="❌ Remove User", callback_data="admin_remove_user_prompt")],
        [InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_panel_open")]
    ])

@admin_router.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def admin_panel_cmd(message: Message, state: FSMContext) -> None:
    await send_admin_panel(message.chat.id, state)

@admin_router.callback_query(F.data == "admin_panel_open", F.from_user.id.in_(ADMIN_IDS))
async def admin_panel_callback(call: CallbackQuery, state: FSMContext) -> None:
    await send_admin_panel(call.message.chat.id, state)
    await call.answer()

async def send_admin_panel(chat_id: int, state: FSMContext) -> None:
    try:
        await state.clear()
        help_text = (
            "🛠 <b>Admin Panel</b>\n"
            "Select what you want to customize below."
        )
        await bot.send_message(chat_id, help_text, reply_markup=admin_keyboard())
    except TelegramAPIError as e:
        logger.error(f"Admin panel error: {e}")

# ----------------- ADMIN MANAGE USERS & ADMINS ----------------- #
@admin_router.callback_query(F.data == "admin_manage_users", F.from_user.id.in_(ADMIN_IDS))
async def manage_users_callback(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("👥 <b>User Management</b>\n\nChoose an action:", reply_markup=user_management_keyboard())
    await call.answer()

@admin_router.callback_query(F.data == "admin_view_users", F.from_user.id.in_(ADMIN_IDS))
async def view_active_users(call: CallbackQuery) -> None:
    try:
        await call.answer("Fetching active users...")
        cursor = db.users.find({"is_approved": 1, "is_banned": 0})
        users = await cursor.to_list(length=None)
        
        if not users:
            await call.message.answer("There are currently no active users.")
            return

        text_content = "🟢 <b>Active Users & Admins:</b>\n\n"
        for u in users:
            username = u.get("username") or "NoUsername"
            role = "👑 Admin" if u['user_id'] in ADMIN_IDS else "👤 User"
            text_content += f"{role} | @{username} (ID: <code>{u['user_id']}</code>)\n"

        if len(text_content) > 3500:
            file_bytes = text_content.replace('<code>', '').replace('</code>', '').replace('<b>', '').replace('</b>', '').encode("utf-8")
            doc = BufferedInputFile(file_bytes, filename="active_users.txt")
            await call.message.answer_document(doc, caption="The list is too long, here is the text file with active users.")
        else:
            await call.message.answer(text_content)
    except Exception as e:
        logger.error(f"Error fetching users: {e}")

@admin_router.callback_query(F.data == "admin_add_user_prompt", F.from_user.id.in_(ADMIN_IDS))
async def add_user_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.waiting_for_add_user_id)
    await call.message.edit_text("➕ Please send the <b>User ID</b>, <b>@username</b>, or <b>Forward a message</b> from the user you want to Add.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="admin_manage_users")]]))
    await call.answer()

@admin_router.message(AdminState.waiting_for_add_user_id, F.from_user.id.in_(ADMIN_IDS))
async def process_add_user(message: Message, state: FSMContext) -> None:
    try:
        target_id = await extract_user_id_from_message(message)
        if not target_id:
            await message.answer("❌ Could not determine user ID. Please send a valid User ID, @username, or forward a message from the user (ensure their privacy settings allow ID extraction).")
            return
            
        await db.users.update_one(
            {"user_id": target_id},
            {"$set": {"is_approved": 1, "is_banned": 0}},
            upsert=True
        )
        try:
            await bot.send_message(target_id, "✅ Your access to the bot has been restored/approved by the Admin! Send /start to begin.")
        except Exception:
            pass
        
        await message.answer(f"✅ User <code>{target_id}</code> has been successfully added/approved.", reply_markup=user_management_keyboard())
        await state.clear()
    except Exception as e:
        logger.error(f"Error adding user: {e}")
        await message.answer("❌ Error occurred while processing.")

@admin_router.callback_query(F.data == "admin_add_admin_prompt", F.from_user.id.in_(ADMIN_IDS))
async def add_admin_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.waiting_for_add_admin_id)
    await call.message.edit_text("👑 Please send the <b>User ID</b>, <b>@username</b>, or <b>Forward a message</b> from the user you want to promote to Admin.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="admin_panel_open")]]))
    await call.answer()

@admin_router.message(AdminState.waiting_for_add_admin_id, F.from_user.id.in_(ADMIN_IDS))
async def process_add_admin(message: Message, state: FSMContext) -> None:
    try:
        target_id = await extract_user_id_from_message(message)
        if not target_id:
            await message.answer("❌ Could not determine user ID. Please send a valid User ID, @username, or forward a message from the user (ensure their privacy settings allow ID extraction).")
            return
            
        if target_id not in ADMIN_IDS:
            ADMIN_IDS.append(target_id)
            
        await db.admins.update_one({"user_id": target_id}, {"$set": {"user_id": target_id}}, upsert=True)
        # Auto-approve new admin to use bot features
        await db.users.update_one(
            {"user_id": target_id},
            {"$set": {"is_approved": 1, "is_banned": 0, "step3_unlocked": 1, "step4_unlocked": 1}},
            upsert=True
        )
        
        try:
            await bot.send_message(target_id, "👑 <b>Congratulations!</b> You have been promoted to Admin. Send /admin to access the panel.")
        except Exception:
            pass
        
        await message.answer(f"✅ User <code>{target_id}</code> has been successfully promoted to Admin.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="admin_panel_open")]]))
        await state.clear()
    except Exception as e:
        logger.error(f"Error adding admin: {e}")
        await message.answer("❌ Error occurred while processing.")

@admin_router.callback_query(F.data == "admin_remove_user_prompt", F.from_user.id.in_(ADMIN_IDS))
async def remove_user_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.waiting_for_remove_user_id)
    await call.message.edit_text("❌ Please send the <b>User ID</b>, <b>@username</b>, or <b>Forward a message</b> from the user you want to Remove/Ban.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="admin_manage_users")]]))
    await call.answer()

@admin_router.message(AdminState.waiting_for_remove_user_id, F.from_user.id.in_(ADMIN_IDS))
async def process_remove_user(message: Message, state: FSMContext) -> None:
    try:
        target_id = await extract_user_id_from_message(message)
        if not target_id:
            await message.answer("❌ Could not determine user ID. Please send a valid User ID, @username, or forward a message from the user.")
            return
        
        # Don't let admins ban themselves
        if target_id == message.from_user.id:
            await message.answer("❌ You cannot remove yourself.")
            return

        await db.users.update_one(
            {"user_id": target_id},
            {"$set": {"is_approved": 0, "is_banned": 1}}
        )
        
        # If they were an admin, remove admin rights
        if target_id in ADMIN_IDS:
            ADMIN_IDS.remove(target_id)
            await db.admins.delete_one({"user_id": target_id})
            
        await message.answer(f"✅ User <code>{target_id}</code> has been successfully removed and banned from accessing the bot.", reply_markup=user_management_keyboard())
        await state.clear()
    except Exception as e:
        logger.error(f"Error removing user: {e}")
        await message.answer("❌ Error occurred while processing.")

@admin_router.message(Command("ban"), F.from_user.id.in_(ADMIN_IDS))
async def ban_user_cmd(message: Message) -> None:
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("⚠️ Usage: <code>/ban user_id</code>")
            return
        target_id = int(args[1])
        await db.users.update_one({"user_id": target_id}, {"$set": {"is_banned": 1, "is_approved": 0}})
        await message.answer(f"✅ User {target_id} has been permanently banned from using the bot.")
    except Exception as e:
        logger.error(f"Ban error: {e}")
        await message.answer("❌ Invalid User ID or Error occurred.")

@admin_router.message(Command("unban"), F.from_user.id.in_(ADMIN_IDS))
async def unban_user_cmd(message: Message) -> None:
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("⚠️ Usage: <code>/unban user_id</code>")
            return
        target_id = int(args[1])
        await db.users.update_one({"user_id": target_id}, {"$set": {"is_banned": 0, "is_approved": 1}})
        await message.answer(f"✅ User {target_id} has been unbanned successfully.")
    except Exception as e:
        logger.error(f"Unban error: {e}")
        await message.answer("❌ Invalid User ID or Error occurred.")

@admin_router.callback_query(F.data == "admin_stats", F.from_user.id.in_(ADMIN_IDS))
async def show_stats(call: CallbackQuery) -> None:
    try:
        total_users = await db.users.count_documents({})
        approved_users = await db.users.count_documents({"is_approved": 1, "is_banned": 0})
        banned_users = await db.users.count_documents({"is_banned": 1})
        
        stats_text = (
            "📊 <b>Bot Statistics</b>\n\n"
            f"👥 Total Users in DB: {total_users}\n"
            f"✅ Active Users: {approved_users}\n"
            f"👑 Total Admins: {len(ADMIN_IDS)}\n"
            f"🚫 Banned Users: {banned_users}\n\n"
            "<i>Note: Real-time blocked/inactive users are calculated after a broadcast.</i>"
        )
        await call.message.answer(stats_text)
        await call.answer()
    except Exception as e:
        logger.error(f"Stats error: {e}")

@admin_router.callback_query(F.data == "admin_backup", F.from_user.id.in_(ADMIN_IDS))
async def manual_backup_cmd(call: CallbackQuery) -> None:
    try:
        await call.message.answer("⏳ Generating and sending database backup...")
        await send_backup(bot, ADMIN_IDS)
        await call.answer("Backup generated and sent successfully!")
    except Exception as e:
        logger.error(f"Manual Backup error: {e}")

@admin_router.callback_query(F.data == "admin_broadcast", F.from_user.id.in_(ADMIN_IDS))
async def setup_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    try:
        await state.set_state(AdminState.waiting_for_broadcast)
        await call.message.answer("📢 Please send the message (Text/Photo/Video/Voice) you want to broadcast to all approved users.")
        await call.answer()
    except Exception as e:
        logger.error(f"Broadcast setup error: {e}")

@admin_router.message(AdminState.waiting_for_broadcast, F.from_user.id.in_(ADMIN_IDS))
async def execute_broadcast(message: Message, state: FSMContext) -> None:
    try:
        await state.clear()
        processing_msg = await message.answer("⏳ Broadcast started... Please wait.")
        
        success = 0
        failed = 0
        
        async for user_doc in db.users.find({"is_banned": 0, "is_approved": 1}):
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
@admin_router.callback_query(F.data.startswith("admin_edit_step"), F.from_user.id.in_(ADMIN_IDS))
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

@admin_router.callback_query(F.data.startswith("add_part_"), F.from_user.id.in_(ADMIN_IDS))
async def add_part_prompt(call: CallbackQuery, state: FSMContext) -> None:
    try:
        msg_type = call.data.replace("add_part_", "")
        data = await state.get_data()
        step_name = data.get("current_step", "Unknown Step")
        
        await state.update_data(expected_type=msg_type)
        await state.set_state(AdminState.waiting_for_step_content)
        
        await call.message.edit_text(f"📤 Please send the <b>{msg_type.upper()}</b> for {step_name.upper()}.\n\n<i>Note: You can add captions if sending media. MP3 and MP4 files are also accepted.</i>")
        await call.answer()
    except Exception as e:
        logger.error(f"Add part prompt error: {e}")

@admin_router.message(AdminState.waiting_for_step_content, F.from_user.id.in_(ADMIN_IDS))
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
        elif expected_type == 'video' and (message.video or message.animation or message.document):
            if message.video:
                media_id = message.video.file_id
            elif message.animation:
                media_id = message.animation.file_id
            elif message.document:
                media_id = message.document.file_id
            msg_type = 'video'
        elif expected_type == 'voice' and (message.voice or message.audio or message.document):
            if message.voice:
                media_id = message.voice.file_id
            elif message.audio:
                media_id = message.audio.file_id
            elif message.document:
                media_id = message.document.file_id
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

@admin_router.callback_query(F.data == "clear_step_parts", F.from_user.id.in_(ADMIN_IDS))
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

# ----------------- MEDIA/CHANNEL EXTRACTOR UTILS ----------------- #
async def extract_channel_info(message: Message) -> Tuple[Optional[str], Optional[int]]:
    """Extracts chat_id and message_id from a forwarded message or raw channel link."""
    chat_id = None
    base_msg_id = None

    if getattr(message, 'forward_origin', None):
        origin = message.forward_origin
        if origin.type == 'channel':
            chat_id = str(origin.chat.id)
            base_msg_id = origin.message_id
        elif origin.type == 'chat':
            chat_id = str(origin.sender_chat.id)
            base_msg_id = origin.message_id
    elif getattr(message, 'forward_from_chat', None):
        chat_id = str(message.forward_from_chat.id)
        base_msg_id = getattr(message, 'forward_from_message_id', 1)
    elif message.text:
        text = message.text.strip()
        match_private = re.search(r't\.me/c/(\d+)/(\d+)', text)
        if match_private:
            chat_id = f"-100{match_private.group(1)}"
            base_msg_id = int(match_private.group(2))
        else:
            match_public = re.search(r't\.me/([^/]+)/(\d+)', text)
            if match_public:
                chat_id = f"@{match_public.group(1)}"
                base_msg_id = int(match_public.group(2))
        
        if not chat_id:
            chat_id = text
            base_msg_id = 1 
            
    return chat_id, base_msg_id

async def get_channel_total_count(bot: Bot, chat_id: str, base_msg_id: int, admin_id: int) -> int:
    """Dynamically probes the channel to calculate the total available messages."""
    try:
        highest_found = base_msg_id
        step = 500
        current = base_msg_id + step
        
        for _ in range(20):
            success = False
            for offset in range(3):
                try:
                    msg = await bot.copy_message(chat_id=admin_id, from_chat_id=chat_id, message_id=current + offset, disable_notification=True)
                    await bot.delete_message(chat_id=admin_id, message_id=msg.message_id)
                    highest_found = current + offset
                    success = True
                    break
                except TelegramAPIError:
                    await asyncio.sleep(0.1)
            
            if success:
                current += step
            else:
                break
                
        last_valid = highest_found
        for check_id in range(highest_found + step, highest_found, -25):
            if check_id <= highest_found:
                break
            try:
                msg = await bot.copy_message(chat_id=admin_id, from_chat_id=chat_id, message_id=check_id, disable_notification=True)
                await bot.delete_message(chat_id=admin_id, message_id=msg.message_id)
                last_valid = check_id
                break
            except TelegramAPIError:
                await asyncio.sleep(0.1)
                
        total = (last_valid - base_msg_id) + 1
        return total if total > 0 else 500
    except Exception as e:
        logger.error(f"Error calculating total count: {e}")
        return 500

# ----------------- ADMIN SINGLE SETTINGS ----------------- #
@admin_router.callback_query(F.data.startswith("admin_set_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_setup_single_callbacks(call: CallbackQuery, state: FSMContext) -> None:
    try:
        action = call.data.replace("admin_set_", "")
        prompts = {
            "start": ("waiting_for_start_msg", "Send the new START message (Text/Photo/Video/Voice)."),
            "dp_channel": ("waiting_for_dp_channel", "To accurately fetch DPs, please FORWARD the FIRST photo message from your DP Channel here.\n\n(Alternatively, send the raw message link like <code>https://t.me/c/123456789/2</code>)\n\n⚠️ Invite links will not work directly."),
            "dump_channel": ("waiting_for_dump_channel", "To accurately fetch videos, please FORWARD the FIRST video message from your Dump Channel here.\n\n(Alternatively, send the raw message link like <code>https://t.me/c/123456789/2</code>)\n\n⚠️ Invite links will not work directly.")
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
        elif message.audio:
            media_id = message.audio.file_id
            media_type = 'voice'
        elif message.animation:
            media_id = message.animation.file_id
            media_type = 'video'
        elif message.document:
            media_id = message.document.file_id
            mime = message.document.mime_type or ""
            if "audio" in mime:
                media_type = 'voice'
            else:
                media_type = 'video'

        await set_setting(key_name, text_val, media_id, media_type)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_panel_open")]
        ])
        await message.answer(f"✅ Successfully saved {key_name.replace('_', ' ').title()}!", reply_markup=keyboard)
        await state.clear()
    except Exception as e:
        logger.error(f"Error saving media setting: {e}")
        await message.answer("❌ Error saving setting.")

@admin_router.message(AdminState.waiting_for_start_msg, F.from_user.id.in_(ADMIN_IDS))
async def save_start(msg: Message, state: FSMContext) -> None: 
    await save_media_setting(msg, state, "start_msg")

@admin_router.message(AdminState.waiting_for_dp_channel, F.from_user.id.in_(ADMIN_IDS))
async def save_dp_channel(message: Message, state: FSMContext) -> None:
    try:
        chat_id, base_msg_id = await extract_channel_info(message)
        if not chat_id:
            await message.answer("❌ Could not extract Channel ID. Please forward a message or send a valid link.")
            return

        processing_msg = await message.answer("⏳ <i>Extracting channel data and calculating totals... Please wait.</i>")
        
        try:
            await bot.get_chat(chat_id)
        except TelegramAPIError:
            await processing_msg.edit_text("❌ Bot cannot access this channel. Please ensure the bot is added as an Admin to the channel first!")
            return

        total_count = await get_channel_total_count(bot, chat_id, base_msg_id, ADMIN_IDS[0])
        await set_setting("dp_channel", chat_id)
        
        await db.settings.update_one(
            {"key": "dp_stats"},
            {"$set": {"base_msg_id": base_msg_id, "total": total_count}},
            upsert=True
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_panel_open")]])
        success_text = (
            "✅ <b>DP Channel Configured Successfully!</b>\n\n"
            f"🔗 <b>Chat ID:</b> <code>{chat_id}</code>\n"
            f"📍 <b>Base Msg ID:</b> {base_msg_id}\n"
            f"📊 <b>Total Auto-Fetched:</b> ~{total_count} media files\n\n"
            "<i>Note: Any new DPs sent to the channel will be tracked automatically.</i>"
        )
        await processing_msg.edit_text(success_text, reply_markup=keyboard)
        await state.clear()
    except Exception as e:
        logger.error(f"Error saving DP channel: {e}")
        await message.answer("❌ Error saving DP channel.")

@admin_router.message(AdminState.waiting_for_dump_channel, F.from_user.id.in_(ADMIN_IDS))
async def save_dump_channel(message: Message, state: FSMContext) -> None:
    try:
        chat_id, base_msg_id = await extract_channel_info(message)
        if not chat_id:
            await message.answer("❌ Could not extract Channel ID. Please forward a message or send a valid link.")
            return

        processing_msg = await message.answer("⏳ <i>Extracting channel data and calculating totals... Please wait.</i>")
        
        try:
            await bot.get_chat(chat_id)
        except TelegramAPIError:
            await processing_msg.edit_text("❌ Bot cannot access this channel. Please ensure the bot is added as an Admin to the channel first!")
            return

        total_count = await get_channel_total_count(bot, chat_id, base_msg_id, ADMIN_IDS[0])
        await set_setting("dump_channel", chat_id)
        
        await db.settings.update_one(
            {"key": "video_stats"},
            {"$set": {"base_msg_id": base_msg_id, "total": total_count}},
            upsert=True
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Admin", callback_data="admin_panel_open")]])
        success_text = (
            "✅ <b>Video Dump Channel Configured Successfully!</b>\n\n"
            f"🔗 <b>Chat ID:</b> <code>{chat_id}</code>\n"
            f"📍 <b>Base Msg ID:</b> {base_msg_id}\n"
            f"📊 <b>Total Auto-Fetched:</b> ~{total_count} media files\n\n"
            "<i>Note: Any new videos sent to the channel will be tracked automatically.</i>"
        )
        await processing_msg.edit_text(success_text, reply_markup=keyboard)
        await state.clear()
    except Exception as e:
        logger.error(f"Error saving Dump channel: {e}")
        await message.answer("❌ Error saving Dump channel.")

# ----------------- CHANNEL LISTENER (AUTO TRACK METADATA ONLY) ----------------- #
@channel_router.message(F.photo | F.video)
@channel_router.channel_post(F.photo | F.video)
async def listen_channels(message: Message) -> None:
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

        regular_msgs = [m for m in messages if m.get("msg_type") != 'voice']
        voice_msgs = [m for m in messages if m.get("msg_type") == 'voice']
        
        ordered_messages = regular_msgs + voice_msgs

        for i, msg in enumerate(ordered_messages):
            msg_type = msg.get("msg_type")
            media_id = msg.get("media_id")
            text_val = msg.get("text_val", "")
            
            markup = final_markup if i == len(ordered_messages) - 1 else None
            
            try:
                if msg_type == 'photo':
                    await bot.send_photo(chat_id, photo=media_id, caption=text_val, reply_markup=markup)
                elif msg_type == 'video':
                    try:
                        await bot.send_video(chat_id, video=media_id, caption=text_val, reply_markup=markup, supports_streaming=True)
                    except TelegramAPIError:
                        await bot.send_document(chat_id, document=media_id, caption=text_val, reply_markup=markup)
                elif msg_type == 'voice':
                    try:
                        await bot.send_voice(chat_id, voice=media_id, caption=text_val, reply_markup=markup)
                    except TelegramAPIError:
                        await bot.send_audio(chat_id, audio=media_id, caption=text_val, reply_markup=markup)
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
        user_id = message.from_user.id
        username = message.from_user.username
        
        if await is_user_banned(user_id):
            return
            
        await register_user(user_id, username)
        
        content = await get_setting("start_msg")
        is_admin = (user_id in ADMIN_IDS)
        keyboard = main_steps_keyboard(is_admin)
        
        if not content:
            await message.answer("Welcome! Please check Admin Panel and setup the start message.", reply_markup=keyboard)
            return

        text_val, media_id, media_type = content
        if media_type == 'photo':
            await message.answer_photo(photo=media_id, caption=text_val, reply_markup=keyboard)
        elif media_type == 'video':
            try:
                await message.answer_video(video=media_id, caption=text_val, reply_markup=keyboard, supports_streaming=True)
            except TelegramAPIError:
                await message.answer_document(document=media_id, caption=text_val, reply_markup=keyboard)
        elif media_type == 'voice':
            try:
                await message.answer_voice(voice=media_id, caption=text_val, reply_markup=keyboard)
            except TelegramAPIError:
                await message.answer_audio(audio=media_id, caption=text_val, reply_markup=keyboard)
        else:
            await message.answer(text=text_val, reply_markup=keyboard)
            
    except Exception as e:
        logger.error(f"Start command error: {e}")


@user_router.callback_query(F.data == "run_step1")
async def process_step1(call: CallbackQuery) -> None:
    try:
        user_id = call.from_user.id
        if await is_user_banned(user_id) or not await is_user_approved(user_id):
            await call.answer("🚫 Access Denied.", show_alert=True)
            return
            
        user_data = await get_user(user_id)
        if user_data and user_data.get("step1_used", 0) == 1:
            await call.answer("⚠️ You have already completed this step. Button locked.", show_alert=True)
            return

        await send_custom_step_content(call.message.chat.id, "step1")
        await db.users.update_one({"user_id": user_id}, {"$set": {"step1_used": 1}})
        
        await call.answer()
    except Exception as e:
        logger.error(f"Step 1 error: {e}")

@user_router.callback_query(F.data == "run_step2")
async def process_step2(call: CallbackQuery) -> None:
    try:
        user_id = call.from_user.id
        if await is_user_banned(user_id) or not await is_user_approved(user_id):
            await call.answer("🚫 Access Denied.", show_alert=True)
            return

        user_data = await get_user(user_id)
        if user_data and user_data.get("step2_used", 0) == 1:
            await call.answer("⚠️ You have already completed Step 2. Button locked.", show_alert=True)
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
                            chat_id=user_id,
                            from_chat_id=dp_chat_id,
                            message_id=msg_id
                        )
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.error(f"Failed to copy DP msg {msg_id}: {e}")
        
        await send_custom_step_content(call.message.chat.id, "step2", final_markup=None)
        
        # New text and button required after Step 2
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="I have done this work", callback_data="req_unlock_3")]
        ])
        await bot.send_message(
            chat_id=user_id,
            text="स्टेप 2 होने के बाद यहां पर क्लिक करें एंड एडमिन को स्क्रीनशॉट भेजें 👇",
            reply_markup=keyboard
        )
        
        await db.users.update_one({"user_id": user_id}, {"$set": {"step2_used": 1}})
        
        await call.answer()
    except Exception as e:
        logger.error(f"Step 2 error: {e}")

@user_router.callback_query(F.data == "req_unlock_3")
async def request_step3(call: CallbackQuery) -> None:
    try:
        user_id = call.from_user.id
        if await is_user_banned(user_id) or not await is_user_approved(user_id):
            await call.answer("🚫 Access Denied.", show_alert=True)
            return

        username = call.from_user.username or "No Username"
        profile_link = f"<a href='tg://user?id={user_id}'>{username}</a>"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_3_{user_id}"),
             InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_3_{user_id}")]
        ])
        
        admin_msg = f"🔓 <b>Step 3 & 4 Unlock Request</b>\n\n👤 User: {profile_link}\n🆔 ID: <code>{user_id}</code>\n💬 User says: I have done this work."
        
        user_data = await get_user(user_id)
        if user_data:
            old_msgs = user_data.get("pending_step3_msgs", {})
            for adm_id_str, msg_id in old_msgs.items():
                try:
                    await bot.delete_message(chat_id=int(adm_id_str), message_id=msg_id)
                except Exception:
                    pass
        
        new_msgs = {}
        for admin in ADMIN_IDS:
            try:
                sent_msg = await bot.send_message(admin, admin_msg, reply_markup=keyboard)
                new_msgs[str(admin)] = sent_msg.message_id
            except Exception as e:
                logger.error(f"Failed to send Step 3 request to Admin {admin}: {e}")
                
        await db.users.update_one({"user_id": user_id}, {"$set": {"pending_step3_msgs": new_msgs}})
        
        await call.message.answer("⏳ Your request has been sent to the admin(s). Please wait for approval.")
        await call.answer()
    except Exception as e:
        logger.error(f"Request Step 3 error: {e}")

@user_router.callback_query(F.data == "run_step3")
async def process_step3(call: CallbackQuery) -> None:
    try:
        user_id = call.from_user.id
        if await is_user_banned(user_id) or not await is_user_approved(user_id):
            await call.answer("🚫 Access Denied.", show_alert=True)
            return

        user_data = await get_user(user_id)
        if not user_data or user_data.get("step3_unlocked", 0) == 0:
            await call.answer("❌ Access Denied. Contact Admin for Approval first.", show_alert=True)
            return
            
        if user_data.get("step3_used", 0) == 1:
            await call.answer("⚠️ You have already completed Step 3. Button locked.", show_alert=True)
            return

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
                user_id=user_id,
                dump_chat_id=dump_chat_id,
                base_msg_id=base_msg_id,
                total_videos=total_videos,
                user_sent_batches=user_sent_batches,
                batches_to_send=2  
            )
            
            await db.users.update_one(
                {"user_id": user_id},
                {
                    "$set": {"user_sent_batches": updated_batches, "step3_used": 1}, 
                    "$inc": {"video_batch": 2}
                }
            )

        # Extra approval for Step 4 is no longer required. It just runs the custom step content natively.
        await send_custom_step_content(call.message.chat.id, "step3", final_markup=None)
        await call.answer()
    except Exception as e:
        logger.error(f"Step 3 error: {e}")

@user_router.callback_query(F.data == "run_step4")
async def process_step4(call: CallbackQuery) -> None:
    try:
        user_id = call.from_user.id
        if await is_user_banned(user_id) or not await is_user_approved(user_id):
            await call.answer("🚫 Access Denied.", show_alert=True)
            return

        user_data = await get_user(user_id)
        if not user_data or user_data.get("step4_unlocked", 0) == 0:
            await call.answer("❌ Access Denied. Complete earlier steps & wait for approval.", show_alert=True)
            return
            
        if user_data.get("step4_used", 0) == 1:
            await call.answer("⚠️ You have already completed Step 4. Button locked.", show_alert=True)
            return

        await send_custom_step_content(call.message.chat.id, "step4")
        await db.users.update_one({"user_id": user_id}, {"$set": {"step4_used": 1}})
        
        await call.answer()
    except Exception as e:
        logger.error(f"Step 4 error: {e}")

# ----------------- ADMIN APPROVAL CALLBACKS ----------------- #
@admin_router.callback_query(F.data.startswith("approve_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_approve_request(call: CallbackQuery) -> None:
    try:
        # Robust parsing for old/corrupted callback data using regex
        nums = re.findall(r'\d+', call.data)
        if not nums:
            await call.answer("❌ Error: Invalid button data.", show_alert=True)
            return
            
        target_user_id = int(nums[-1])
        step = nums[0] if len(nums) > 1 else "3"

        if step == "3":
            # Unlocking Step 3 and Step 4 simultaneously based on one approval!
            await db.users.update_one({"user_id": target_user_id}, {"$set": {"step3_unlocked": 1, "step4_unlocked": 1}})
            msg_to_user = "✅ Successfully unlocked your access! You can now proceed with Step 3 & 4."

        user_data = await get_user(target_user_id)
        pending_msgs = user_data.get(f"pending_step{step}_msgs", {})
        
        await db.users.update_one({"user_id": target_user_id}, {"$unset": {f"pending_step{step}_msgs": ""}})
        
        new_text = f"{call.message.html_text}\n\n✅ <b>Approved by Admin {call.from_user.id}</b>"
        
        if not pending_msgs:
            await call.message.edit_text(new_text)
        else:
            for adm_id_str, msg_id in pending_msgs.items():
                try:
                    await bot.edit_message_text(chat_id=int(adm_id_str), message_id=msg_id, text=new_text)
                except Exception:
                    pass

        try:
            await bot.send_message(target_user_id, msg_to_user)
        except TelegramAPIError:
            logger.warning(f"Could not notify user {target_user_id} about approval. They might have blocked the bot.")
        
        await call.answer("Approved!")
    except Exception as e:
        logger.error(f"Approval callback error: {e}")

@admin_router.callback_query(F.data.startswith("deny_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_deny_request(call: CallbackQuery) -> None:
    try:
        # Robust parsing for old/corrupted callback data using regex
        nums = re.findall(r'\d+', call.data)
        if not nums:
            await call.answer("❌ Error: Invalid button data.", show_alert=True)
            return
            
        target_user_id = int(nums[-1])
        step = nums[0] if len(nums) > 1 else "3"

        user_data = await get_user(target_user_id)
        pending_msgs = user_data.get(f"pending_step{step}_msgs", {})
        
        await db.users.update_one({"user_id": target_user_id}, {"$unset": {f"pending_step{step}_msgs": ""}})
        
        new_text = f"{call.message.html_text}\n\n❌ <b>Denied by Admin {call.from_user.id}</b>"
        
        if not pending_msgs:
            await call.message.edit_text(new_text)
        else:
            for adm_id_str, msg_id in pending_msgs.items():
                try:
                    await bot.edit_message_text(chat_id=int(adm_id_str), message_id=msg_id, text=new_text)
                except Exception:
                    pass
        
        try:
            await bot.send_message(target_user_id, f"❌ Your request was denied by the Admin. Please check your tasks again.")
        except TelegramAPIError:
            logger.warning(f"Could not notify user {target_user_id} about denial.")
            
        await call.answer("Request Denied.")
    except Exception as e:
        logger.error(f"Denial callback error: {e}")

# ----------------- MAIN RUNNER ----------------- #
async def main() -> None:
    await init_db()
    
    # START AUTO BACKUP TASK IN BACKGROUND (Sends backup to all configured admins)
    asyncio.create_task(auto_backup_task(bot, ADMIN_IDS))
    
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
