
import logging
import time
import datetime
import asyncio
import os
import html
from pymongo import MongoClient

# --- HEROKU CONFIGURATION ---
TOKEN = os.getenv("TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID", "6804892450"))
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "-1002918236314"))
UPDATE_CHANNEL_ID = int(os.getenv("UPDATE_CHANNEL_ID", "-1003491668063"))
DB_NAME = "dating_bot_main_stable"

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, ChatMember
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)

# --- LOGGING ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- MONGODB CONNECTION ---
try:
    client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
    db = client[DB_NAME] 
    users_collection = db['users']
    print(f"✅ Connected to MongoDB! Database: {DB_NAME}")
except Exception as e:
    print(f"❌ Connection Error: {e}")
    exit()

# --- DATABASE FUNCTIONS ---

def get_user(user_id):
    return users_collection.find_one({"_id": int(user_id)})

def get_user_by_query(query):
    if str(query).isdigit():
        return users_collection.find_one({"_id": int(query)})
    clean_username = str(query).lstrip('@')
    return users_collection.find_one({"username": {"$regex": f"^{clean_username}$", "$options": "i"}})

def add_user(user_id, name, age, gender, bio, photo_id, username):
    existing = users_collection.find_one({"_id": int(user_id)})
    coins = existing.get("coins", 0) if existing else 0
    blocked = existing.get("blocked_users", []) if existing else []
    
    users_collection.update_one(
        {"_id": int(user_id)},
        {
            "$set": {
                "name": name, "age": age, "gender": gender, "bio": bio, 
                "photo_id": photo_id, "username": username,
                "status": "idle", "chat_partner": None,
                "last_active": datetime.datetime.now()
            },
            "$setOnInsert": {"coins": coins, "blocked_users": blocked}
        },
        upsert=True
    )

def add_coins(user_id, amount):
    users_collection.update_one({"_id": int(user_id)}, {"$inc": {"coins": amount}})

def update_activity(user_id):
    users_collection.update_one({"_id": int(user_id)}, {"$set": {"last_active": datetime.datetime.now()}})

def set_status(user_id, status):
    users_collection.update_one({"_id": int(user_id)}, {"$set": {"status": status}})

def set_chat_pair(user1_id, user2_id):
    users_collection.update_one({"_id": int(user1_id)}, {"$set": {"status": "chatting", "chat_partner": int(user2_id)}})
    users_collection.update_one({"_id": int(user2_id)}, {"$set": {"status": "chatting", "chat_partner": int(user1_id)}})

def clear_chat_pair(user_id):
    user = get_user(user_id)
    if user and user.get("chat_partner"):
        partner_id = user["chat_partner"]
        users_collection.update_one({"_id": int(user_id)}, {"$set": {"status": "idle", "chat_partner": None}})
        users_collection.update_one({"_id": int(partner_id)}, {"$set": {"status": "idle", "chat_partner": None}})
        return partner_id
    return None

def block_user(user_id, target_id):
    users_collection.update_one({"_id": int(user_id)}, {"$addToSet": {"blocked_users": int(target_id)}})

def unblock_user(user_id, target_id):
    users_collection.update_one({"_id": int(user_id)}, {"$pull": {"blocked_users": int(target_id)}})

def is_blocked(user_id, target_id):
    target = users_collection.find_one({"_id": int(target_id)})
    if target and "blocked_users" in target:
        if int(user_id) in target["blocked_users"]:
            return True
    return False

def find_search_partner(my_id):
    me = get_user(my_id)
    my_blocks = me.get("blocked_users", []) if me else []
    pipeline = [
        {
            "$match": {
                "status": "searching", 
                "_id": {"$ne": int(my_id), "$nin": my_blocks}, 
                "blocked_users": {"$ne": int(my_id)} 
            }
        }, 
        {"$sample": {"size": 1}}
    ]
    result = list(users_collection.aggregate(pipeline))
    if result:
        return result[0]
    return None

# --- TIMEOUT LOGIC ---

async def search_timeout_task(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.data
    user = get_user(user_id)
    if user and user.get("status") == "searching":
        set_status(user_id, "idle")
        try:
            await context.bot.send_message(user_id, "💤 **No active partners found.**\nIt seems quiet right now. Please try searching again in a few minutes!", parse_mode=ParseMode.MARKDOWN)
        except: pass

async def inactivity_timeout_task(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.data
    user = get_user(user_id)
    if user and user.get("status") == "chatting":
        partner_id = clear_chat_pair(user_id)
        msg = "⏳ **Chat ended due to inactivity.**\n(No messages for 5 minutes)\n\nType /search to find a new partner."
        try: await context.bot.send_message(user_id, msg, parse_mode=ParseMode.MARKDOWN)
        except: pass
        if partner_id:
            try: await context.bot.send_message(partner_id, msg, parse_mode=ParseMode.MARKDOWN)
            except: pass

def reset_inactivity_timer(context, user_id, partner_id):
    if not context.job_queue: return
    # Remove old timers
    for uid in [user_id, partner_id]:
        jobs = context.job_queue.get_jobs_by_name(f"inactivity_{uid}")
        for job in jobs: job.schedule_removal()
    # Add new timer (300s = 5 mins)
    context.job_queue.run_once(inactivity_timeout_task, 300, data=user_id, name=f"inactivity_{user_id}")

# --- FORCE SUB LOGIC ---

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat_id=UPDATE_CHANNEL_ID, user_id=user_id)
        if member.status in [ChatMember.LEFT, ChatMember.BANNED]:
            await send_force_sub_message(update, context)
            return False
        return True
    except Exception as e:
        print(f"Force Sub Error: {e}")
        return True

async def send_force_sub_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = await context.bot.get_chat(UPDATE_CHANNEL_ID)
        link = chat.invite_link if chat.invite_link else f"https://t.me/{chat.username}"
    except: link = "https://t.me/telegram"
    text = "🔒 **Locked Access**\n\nTo use this bot, you must join our update channel first."
    keyboard = [[InlineKeyboardButton("📢 Join Channel", url=link)], [InlineKeyboardButton("✅ I Joined", callback_data="check_sub")]]
    if update.callback_query: await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# --- STATES ---
REG_NAME, REG_AGE, REG_GENDER, REG_BIO, REG_PHOTO = range(5)
EDIT_SELECT, EDIT_UPDATE = range(5, 7)

# --- START ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context): return
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    try:
        first_name = html.escape(update.effective_user.first_name or "Unknown")
        username = html.escape(update.effective_user.username or "None")
        log_text = f"👤 <b>New Session</b>\n🆔 ID: <code>{user_id}</code>\n📛 Name: {first_name}\n🔗 Username: @{username}"
        await context.bot.send_message(LOG_GROUP_ID, log_text, parse_mode=ParseMode.HTML)
    except: pass

    if not user and context.args:
        referrer_arg = context.args[0]
        if referrer_arg.startswith("ref_"):
            try:
                referrer_id = int(referrer_arg.split("_")[1])
                if referrer_id != user_id:
                    add_coins(referrer_id, 100)
                    await context.bot.send_message(referrer_id, "🎉 **Referral Bonus!**\nYou earned **100 Coins**!", parse_mode=ParseMode.MARKDOWN)
            except: pass

    if user:
        update_activity(user_id)
        await send_profile_menu(update, context, user)
        return ConversationHandler.END
    else:
        await update.message.reply_text("👋 **Welcome!**\nLet's create your profile to get started.\n\n👉 **What is your name?**", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
        return REG_NAME

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user:
        await send_profile_menu(update, context, user)
    else:
        await update.message.reply_text("⚠️ Register first with /start")

async def send_profile_menu(update, context, user):
    caption = (
        f"💎 **YOUR PROFILE** 💎\n\n"
        f"👤 **Name:** {user.get('name')}\n"
        f"🎂 **Age:** {user.get('age')}\n"
        f"⚧ **Gender:** {user.get('gender')}\n"
        f"💰 **Coins:** `{user.get('coins', 0)}`\n"
        f"📝 **Bio:** {user.get('bio')}\n\n"
        "📜 **COMMANDS LIST:**\n"
        "🔎 /search - Find Random Partner\n"
        "💌 /chat `[ID]` - Direct Request\n"
        "🛑 /stop - End Current Chat\n"
        "➡️ /next - Skip Partner\n"
        "🚫 /block - Block User\n"
        "🔓 /unblock `[ID]` - Unblock User\n"
        "💰 /balance - Check Coins\n"
        "🎁 /referral - Invite & Earn\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🚀 **Ready to chat?**\n"
        "Click the button below to find a partner!"
    )
    keyboard = [
        [InlineKeyboardButton("💬 Start Chatting", callback_data="search")],
        [InlineKeyboardButton("✏️ Edit Profile", callback_data="edit")]
    ]
    
    chat_id = update.effective_chat.id
    if user.get("photo_id"):
        try:
            await context.bot.send_photo(chat_id, user.get("photo_id"), caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard), protect_content=True)
        except:
            await context.bot.send_message(chat_id, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard), protect_content=True)
    else:
        await context.bot.send_message(chat_id, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard), protect_content=True)

# --- REGISTRATION STEPS ---

async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("✨ Nice name! **How old are you?**", parse_mode=ParseMode.MARKDOWN)
    return REG_AGE

async def reg_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        age = int(update.message.text)
        if age < 18:
            await update.message.reply_text("🔞 Sorry, you must be 18+ to use this bot.")
            return ConversationHandler.END
        context.user_data['age'] = age
        keyboard = [["Male", "Female"]]
        await update.message.reply_text("⚧ **Select your gender:**", reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True), parse_mode=ParseMode.MARKDOWN)
        return REG_GENDER
    except:
        await update.message.reply_text("🔢 Please enter a valid number for age.")
        return REG_AGE

async def reg_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gender = update.message.text
    if gender not in ["Male", "Female"]:
        await update.message.reply_text("Please select a valid gender option.")
        return REG_GENDER
    context.user_data['gender'] = gender
    await update.message.reply_text("📝 **Write a short bio about yourself:**", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
    return REG_BIO

async def reg_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['bio'] = update.message.text
    await update.message.reply_text("📸 **Almost done! Send a photo for your profile.**", parse_mode=ParseMode.MARKDOWN)
    return REG_PHOTO

async def reg_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo: return REG_PHOTO
    photo_file = update.message.photo[-1].file_id
    add_user(update.effective_user.id, context.user_data['name'], context.user_data['age'], context.user_data['gender'], context.user_data['bio'], photo_file, update.effective_user.username)
    add_coins(update.effective_user.id, 50) 
    await update.message.reply_text("✅ **All set!** You received **50 Free Coins**.\n\nType /search to start chatting!", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Operation canceled.")
    return ConversationHandler.END

# --- DIRECT CHAT REQUEST ---

async def direct_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("⚠️ **Usage:** `/chat [ID or @username]`", parse_mode=ParseMode.MARKDOWN)
        return
    target_query = context.args[0]
    my_user = get_user(user_id)
    if not my_user: return
    if my_user.get("status") == "chatting":
        await update.message.reply_text("❌ **You are already in a chat!**")
        return
    target_user = get_user_by_query(target_query)
    if not target_user:
        await update.message.reply_text("❌ **User not found.**", parse_mode=ParseMode.MARKDOWN)
        return
    target_id = target_user["_id"]
    if target_id == user_id:
        await update.message.reply_text("❌ You cannot chat with yourself.")
        return
    if is_blocked(user_id, target_id):
        await update.message.reply_text("🚫 **Blocked.** You cannot message this user.", parse_mode=ParseMode.MARKDOWN)
        return
    if target_user.get("status") == "chatting":
        await update.message.reply_text("❌ **User is busy.**", parse_mode=ParseMode.MARKDOWN)
        return

    keyboard = [[InlineKeyboardButton("✅ Accept", callback_data=f"connect_{user_id}"), InlineKeyboardButton("❌ Decline", callback_data=f"reject_{user_id}")]]
    caption = f"📩 **CHAT REQUEST!**\n\n👤 **{my_user.get('name')}**, {my_user.get('age')}\n📝 **Bio:** {my_user.get('bio')}\n\nAccept chat?"
    try:
        if my_user.get("photo_id"):
            try: await context.bot.send_photo(target_id, my_user.get("photo_id"), caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            except: await context.bot.send_message(target_id, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
        else: await context.bot.send_message(target_id, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
        await update.message.reply_text("✅ **Request Sent!**")
    except: await update.message.reply_text("❌ **Failed to send.**")

# --- BLOCK / UNBLOCK ---

async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user and user.get("status") == "chatting" and user.get("chat_partner"):
        partner_id = user["chat_partner"]
        block_user(user_id, partner_id)
        await stop_handler(update, context)
        await update.message.reply_text(f"🚫 **User {partner_id} blocked.**", parse_mode=ParseMode.MARKDOWN)
        return
    if context.args:
        try:
            target_id = int(context.args[0])
            block_user(user_id, target_id)
            await update.message.reply_text(f"🚫 **ID {target_id} blocked.**", parse_mode=ParseMode.MARKDOWN)
        except: pass
    else: await update.message.reply_text("⚠️ **Usage:** `/block` inside chat, or `/block [ID]`", parse_mode=ParseMode.MARKDOWN)

async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ **Usage:** `/unblock [ID]`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
        unblock_user(update.effective_user.id, target_id)
        await update.message.reply_text(f"✅ **ID {target_id} unblocked.**", parse_mode=ParseMode.MARKDOWN)
    except: pass

# --- SEARCH & CHAT ---

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if update.callback_query: await update.callback_query.answer()
    if not await check_subscription(update, context): return
    user = get_user(user_id)
    if not user: return
    update_activity(user_id)
    if user.get("status") == "chatting":
        await context.bot.send_message(chat_id, "⚠️ **Already in a chat!**", parse_mode=ParseMode.MARKDOWN)
        return

    status_msg = await context.bot.send_message(chat_id, "🔎 **Searching for a partner...**", parse_mode=ParseMode.MARKDOWN)
    await asyncio.sleep(1.5) 
    partner = find_search_partner(user_id)
    
    if partner:
        await status_msg.delete()
        set_chat_pair(user_id, partner["_id"])
        await send_match_message(context, user_id, partner["_id"])
        await send_match_message(context, partner["_id"], user_id)
        reset_inactivity_timer(context, user_id, partner["_id"])
    else:
        set_status(user_id, "searching")
        await status_msg.edit_text("📡 **Looking for a match...**\n(Waiting for someone else to join)")
        # SAFETY CHECK FOR JOB QUEUE
        if context.job_queue:
            # Clean old search jobs
            current_jobs = context.job_queue.get_jobs_by_name(f"search_{user_id}")
            for job in current_jobs: job.schedule_removal()
            # 60s timeout
            context.job_queue.run_once(search_timeout_task, 60, data=user_id, name=f"search_{user_id}")

async def send_match_message(context, to_id, partner_id):
    partner = get_user(partner_id)
    text = f"🎉 **PARTNER FOUND!** 🎉\n\n👤 **Name:** {partner.get('name')}, {partner.get('age')}\n⚧ **Gender:** {partner.get('gender')}\n📝 **Bio:** {partner.get('bio')}\n\n💬 **Say 'Hi'!**"
    keyboard = [
        [InlineKeyboardButton("👀 View Photo", callback_data=f"view_{partner_id}")],
        [InlineKeyboardButton("➡️ Next", callback_data="next"), InlineKeyboardButton("🛑 Stop", callback_data="stop")],
        [InlineKeyboardButton("🚫 Block User", callback_data=f"block_match_{partner_id}")]
    ]
    try:
        await context.bot.send_message(to_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard), protect_content=True)
    except: pass

async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query: await update.callback_query.answer()
    
    # Clean Jobs
    if context.job_queue:
        for prefix in ["search_", "inactivity_"]:
            current_jobs = context.job_queue.get_jobs_by_name(f"{prefix}{user_id}")
            for job in current_jobs: job.schedule_removal()

    partner_id = clear_chat_pair(user_id)
    keyboard = [[InlineKeyboardButton("💬 Find New Partner", callback_data="search")]]
    await context.bot.send_message(user_id, "🚫 **Chat ended.**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    if partner_id:
        try: await context.bot.send_message(partner_id, "⚠️ **Partner left the chat.**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        except: pass

async def next_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_handler(update, context)
    await search_handler(update, context)

# --- CALLBACKS & COMMANDS ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split("_")
    action = data[0]

    if action == "view":
        target = get_user(int(data[1]))
        if target:
            caption = f"👤 **{target.get('name')}**\n{target.get('bio')}"
            try: await context.bot.send_photo(query.from_user.id, target.get("photo_id"), caption=caption, protect_content=True, parse_mode=ParseMode.MARKDOWN)
            except: await context.bot.send_message(query.from_user.id, caption, parse_mode=ParseMode.MARKDOWN)
        await query.answer()
    elif action == "search":
        await query.answer()
        await search_handler(update, context)
    elif action == "next":
        await query.answer()
        await next_handler(update, context)
    elif action == "stop":
        await query.answer()
        await stop_handler(update, context)
    elif action == "edit":
        await query.answer()
        await query.message.reply_text("Type /edit to change your profile.")
    elif action == "check_sub":
        await query.answer()
        if await check_subscription(update, context):
            await query.message.delete()
            await start(update, context)
        else: await query.message.reply_text("❌ Not joined yet!", ephemeral=True)
    
    # FIXED BLOCK LOGIC
    elif action == "block_match":
        target_id = int(data[1])
        my_id = query.from_user.id
        block_user(my_id, target_id)
        await stop_handler(update, context)
        await query.message.reply_text("🚫 **User blocked.**", parse_mode=ParseMode.MARKDOWN)

    elif action == "connect":
        sender_id = int(data[1])
        my_id = query.from_user.id
        if get_user(sender_id).get("status") == "chatting" or get_user(my_id).get("status") == "chatting":
            await query.message.edit_text("❌ Connection failed.")
            return
        set_chat_pair(my_id, sender_id)
        await query.message.delete()
        await send_match_message(context, my_id, sender_id)
        await send_match_message(context, sender_id, my_id)
        reset_inactivity_timer(context, my_id, sender_id)

    elif action == "reject":
        sender_id = int(data[1])
        await query.message.delete()
        await context.bot.send_message(query.from_user.id, "❌ **Declined.**", parse_mode=ParseMode.MARKDOWN)
        try: await context.bot.send_message(sender_id, "🚫 **Request declined.**", parse_mode=ParseMode.MARKDOWN)
        except: pass

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user: await update.message.reply_text(f"💰 **Balance:** `{user.get('coins', 0)}` Coins", parse_mode=ParseMode.MARKDOWN)

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
    await update.message.reply_text(f"🎁 **Refer & Earn!**\n\n🔗 `{link}`", parse_mode=ParseMode.MARKDOWN)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    total = users_collection.count_documents({})
    searching = users_collection.count_documents({"status": "searching"})
    chatting = users_collection.count_documents({"status": "chatting"})
    await update.message.reply_text(f"📊 **Stats**\n\n👥 Users: {total}\n🔎 Searching: {searching}\n💬 Pairs: {chatting // 2}", parse_mode=ParseMode.MARKDOWN)

async def chat_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user and user.get("status") == "chatting" and user.get("chat_partner"):
        partner_id = user["chat_partner"]
        reset_inactivity_timer(context, user_id, partner_id)
        try:
            if update.message.text: await context.bot.send_message(partner_id, update.message.text, protect_content=True)
            elif update.message.photo: await context.bot.send_photo(partner_id, update.message.photo[-1].file_id, protect_content=True)
            elif update.message.sticker: await context.bot.send_sticker(partner_id, update.message.sticker.file_id, protect_content=True)
            elif update.message.voice: await context.bot.send_voice(partner_id, update.message.voice.file_id, protect_content=True)
            elif update.message.video: await context.bot.send_video(partner_id, update.message.video.file_id, protect_content=True)
        except:
            await update.message.reply_text("❌ Partner disconnected.")
            await stop_handler(update, context)

# --- EDIT PROFILE ---
async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["Name", "Age"], ["Gender", "Bio"], ["Photo", "Cancel"]]
    markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    text = "✏️ **What do you want to edit?**"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    return EDIT_SELECT

async def edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    selection = update.message.text
    if selection == "Cancel": return ConversationHandler.END
    context.user_data['edit_field'] = selection.lower()
    await update.message.reply_text(f"📝 Send your new **{selection}**:", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
    return EDIT_UPDATE

async def edit_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    field = context.user_data['edit_field']
    if field == "photo": users_collection.update_one({"_id": user_id}, {"$set": {"photo_id": update.message.photo[-1].file_id}})
    else:
        val = update.message.text
        if field == "age": 
            try: val = int(val)
            except: 
                await update.message.reply_text("⚠️ Age must be a number.")
                return EDIT_UPDATE
        users_collection.update_one({"_id": user_id}, {"$set": {field: val}})
    await update.message.reply_text("✅ **Profile Updated!**\nUse /profile to check.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

def main():
    if not TOKEN: return
    app = Application.builder().token(TOKEN).build()
    
    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)], 
        states={
            REG_NAME: [MessageHandler(filters.TEXT, reg_name)], REG_AGE: [MessageHandler(filters.TEXT, reg_age)], 
            REG_GENDER: [MessageHandler(filters.TEXT, reg_gender)], REG_BIO: [MessageHandler(filters.TEXT, reg_bio)], 
            REG_PHOTO: [MessageHandler(filters.PHOTO, reg_photo)]
        }, fallbacks=[CommandHandler("cancel", cancel)], allow_reentry=True
    )

    edit_conv = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit_start),
            CallbackQueryHandler(edit_start, pattern="^edit$")
        ],
        states={EDIT_SELECT: [MessageHandler(filters.TEXT, edit_select)], EDIT_UPDATE: [MessageHandler(filters.ALL, edit_update)]},
        fallbacks=[CommandHandler("cancel", cancel)], allow_reentry=True
    )
    
    app.add_handler(reg_conv)
    app.add_handler(edit_conv)
    
    app.add_handler(CommandHandler("search", search_handler))
    app.add_handler(CommandHandler("chat", direct_chat_handler))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("next", next_handler))
    app.add_handler(CommandHandler("stop", stop_handler))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("block", block_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Sticker.ALL | filters.VOICE | filters.VIDEO, chat_message_handler))
    
    print("Bot Running: Fixed Job Queue & Block Button...")
    app.run_polling()

if __name__ == "__main__":
    main()

