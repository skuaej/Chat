
import logging
import time
import datetime
import asyncio
import os
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

def add_user(user_id, name, age, gender, bio, photo_id, username):
    existing = users_collection.find_one({"_id": int(user_id)})
    coins = existing.get("coins", 0) if existing else 0
    
    users_collection.update_one(
        {"_id": int(user_id)},
        {
            "$set": {
                "name": name, "age": age, "gender": gender, "bio": bio, 
                "photo_id": photo_id, "username": username,
                "status": "idle", "chat_partner": None,
                "last_active": datetime.datetime.now()
            },
            "$setOnInsert": {"coins": coins}
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

def find_search_partner(my_id):
    pipeline = [
        {"$match": {"status": "searching", "_id": {"$ne": int(my_id)}}}, 
        {"$sample": {"size": 1}}
    ]
    result = list(users_collection.aggregate(pipeline))
    if result:
        return result[0]
    return None

# --- TIMEOUT LOGIC ---

async def timeout_task(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.data
    user = get_user(user_id)
    
    if user and user.get("status") == "searching":
        set_status(user_id, "idle")
        try:
            await context.bot.send_message(
                user_id, 
                "💤 **No active partners found.**\n\nIt seems quiet right now. Please try searching again in a few minutes!", 
                parse_mode=ParseMode.MARKDOWN
            )
        except: pass

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
        link = chat.invite_link
        if not link and chat.username: link = f"https://t.me/{chat.username}"
    except: link = "https://t.me/telegram"

    text = "🔒 **Locked Access**\n\nTo use this bot, you must join our update channel first."
    keyboard = [[InlineKeyboardButton("📢 Join Channel", url=link)], [InlineKeyboardButton("✅ I Joined", callback_data="check_sub")]]
    
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# --- STATES ---
REG_NAME, REG_AGE, REG_GENDER, REG_BIO, REG_PHOTO = range(5)
EDIT_SELECT, EDIT_UPDATE = range(5, 7)

# --- START ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context): return
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    try:
        log_text = f"👤 **New Session**\n🆔 ID: `{user_id}`\n📛 Name: {update.effective_user.first_name}"
        await context.bot.send_message(LOG_GROUP_ID, log_text, parse_mode=ParseMode.MARKDOWN)
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
        await update.message.reply_text(
            "👋 **Welcome!**\nLet's create your profile to get started.\n\n👉 **What is your name?**",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN
        )
        return REG_NAME

async def send_profile_menu(update, context, user):
    caption = (
        f"💎 **YOUR PROFILE** 💎\n\n"
        f"👤 **Name:** {user.get('name')}\n"
        f"🎂 **Age:** {user.get('age')}\n"
        f"⚧ **Gender:** {user.get('gender')}\n"
        f"💰 **Coins:** `{user.get('coins', 0)}`\n"
        f"📝 **Bio:** {user.get('bio')}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🚀 **Ready to chat?**\n"
        "Click the button below to find a partner instantly!"
    )
    # FIXED: Changed callback_data to single words 'search' and 'edit'
    keyboard = [
        [InlineKeyboardButton("💬 Start Chatting", callback_data="search")],
        [InlineKeyboardButton("✏️ Edit Profile", callback_data="edit")]
    ]
    
    chat_id = update.effective_chat.id
    try:
        if user.get("photo_id"):
            await context.bot.send_photo(chat_id, user.get("photo_id"), caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard), protect_content=True)
        else:
            await context.bot.send_message(chat_id, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard), protect_content=True)
    except:
        await context.bot.send_message(chat_id, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

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

# --- SEARCH & CHAT ---

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if update.callback_query: 
        await update.callback_query.answer()

    if not await check_subscription(update, context): return

    user = get_user(user_id)
    if not user:
        await context.bot.send_message(chat_id, "⚠️ **Profile not found.**\nPlease type /start to register.", parse_mode=ParseMode.MARKDOWN)
        return

    update_activity(user_id)

    if user.get("status") == "chatting":
        await context.bot.send_message(chat_id, "⚠️ **You are already in a chat!**\nType /stop to leave current chat.", parse_mode=ParseMode.MARKDOWN)
        return

    # SEARCH ANIMATION
    status_msg = await context.bot.send_message(chat_id, "🔎 **Searching for a partner...**", parse_mode=ParseMode.MARKDOWN)
    await asyncio.sleep(1.5) 
    
    partner = find_search_partner(user_id)
    
    if partner:
        await status_msg.delete()
        set_chat_pair(user_id, partner["_id"])
        await send_match_message(context, user_id, partner["_id"])
        await send_match_message(context, partner["_id"], user_id)
    else:
        set_status(user_id, "searching")
        await status_msg.edit_text("📡 **Looking for a match...**\n(Waiting for someone else to join)")
        
        current_jobs = context.job_queue.get_jobs_by_name(str(user_id))
        for job in current_jobs: job.schedule_removal()
        
        context.job_queue.run_once(timeout_task, 60, data=user_id, name=str(user_id))

async def send_match_message(context, to_id, partner_id):
    partner = get_user(partner_id)
    text = (
        "🎉 **PARTNER FOUND!** 🎉\n\n"
        f"👤 **Name:** {partner.get('name')}, {partner.get('age')}\n"
        f"⚧ **Gender:** {partner.get('gender')}\n"
        f"📝 **Bio:** {partner.get('bio')}\n\n"
        "💬 **Say 'Hi' to start the conversation!**"
    )
    
    keyboard = [
        [InlineKeyboardButton("👀 View Photo", callback_data=f"view_{partner_id}")],
        [InlineKeyboardButton("➡️ Next Partner", callback_data="next"), InlineKeyboardButton("🛑 Stop", callback_data="stop")]
    ]
    
    await context.bot.send_message(
        to_id, 
        text, 
        parse_mode=ParseMode.MARKDOWN, 
        reply_markup=InlineKeyboardMarkup(keyboard),
        protect_content=True
    )

async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query: await update.callback_query.answer()
    
    current_jobs = context.job_queue.get_jobs_by_name(str(user_id))
    for job in current_jobs: job.schedule_removal()

    partner_id = clear_chat_pair(user_id)
    
    keyboard = [[InlineKeyboardButton("💬 Find New Partner", callback_data="search")]]
    await context.bot.send_message(user_id, "🚫 **Chat ended.**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    if partner_id:
        try: 
            await context.bot.send_message(partner_id, "⚠️ **Partner left the chat.**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        except: pass

async def next_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_handler(update, context)
    await search_handler(update, context)

# --- CALLBACKS ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split("_")
    action = data[0]

    # FIXED: Check simple action strings
    if action == "view":
        target = get_user(int(data[1]))
        if target:
            caption = f"👤 **{target.get('name')}**\n{target.get('bio')}"
            await context.bot.send_photo(query.from_user.id, target.get("photo_id"), caption=caption, protect_content=True, parse_mode=ParseMode.MARKDOWN)
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

    # Edit is handled by the ConversationHandler pattern below
    elif action == "edit":
        await query.answer()
        await query.message.reply_text("Type /edit to change your profile.")

    elif action == "check_sub":
        await query.answer()
        if await check_subscription(update, context):
            await query.message.delete()
            await start(update, context)
        else:
            await query.message.reply_text("❌ You have not joined yet!", ephemeral=True)

# --- COMMANDS ---

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user: await update.message.reply_text(f"💰 **Balance:** `{user.get('coins', 0)}` Coins", parse_mode=ParseMode.MARKDOWN)

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
    await update.message.reply_text(f"🎁 **Refer & Earn!**\nInvite friends and get 100 coins.\n\n🔗 `{link}`", parse_mode=ParseMode.MARKDOWN)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    total = users_collection.count_documents({})
    searching = users_collection.count_documents({"status": "searching"})
    chatting = users_collection.count_documents({"status": "chatting"})
    await update.message.reply_text(f"📊 **Stats**\n\n👥 Users: {total}\n🔎 Searching: {searching}\n💬 Pairs: {chatting // 2}", parse_mode=ParseMode.MARKDOWN)

# --- CHAT HANDLER ---

async def chat_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user and user.get("status") == "chatting" and user.get("chat_partner"):
        partner_id = user["chat_partner"]
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
    if update.callback_query: await update.callback_query.answer()
    await update.message.reply_text("✏️ **What do you want to edit?**", reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True), parse_mode=ParseMode.MARKDOWN)
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
    await update.message.reply_text("✅ **Profile Updated!**", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

def main():
    if not TOKEN: return
    app = Application.builder().token(TOKEN).build()
    
    # Conversations
    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)], 
        states={
            REG_NAME: [MessageHandler(filters.TEXT, reg_name)], REG_AGE: [MessageHandler(filters.TEXT, reg_age)], 
            REG_GENDER: [MessageHandler(filters.TEXT, reg_gender)], REG_BIO: [MessageHandler(filters.TEXT, reg_bio)], 
            REG_PHOTO: [MessageHandler(filters.PHOTO, reg_photo)]
        }, fallbacks=[CommandHandler("cancel", cancel)], allow_reentry=True
    )

    # FIXED: Added CallbackQueryHandler with pattern="^edit$"
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
    app.add_handler(CommandHandler("next", next_handler))
    app.add_handler(CommandHandler("stop", stop_handler))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CommandHandler("stats", admin_stats))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Sticker.ALL | filters.VOICE | filters.VIDEO, chat_message_handler))
    
    print("Bot Running: Fixed Buttons & Enhanced UI...")
    app.run_polling()

if __name__ == "__main__":
    main()

