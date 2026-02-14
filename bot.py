import logging
import time
import datetime
import os
from pymongo import MongoClient

# --- HEROKU CONFIGURATION ---
# We use os.getenv so you don't expose secrets in your code
TOKEN = os.getenv("TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID", "6804892450"))
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "-1002918236314"))
DB_NAME = "dating_bot_main_stable"

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
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

# --- STATES ---
REG_NAME, REG_AGE, REG_GENDER, REG_BIO, REG_PHOTO = range(5)
EDIT_SELECT, EDIT_UPDATE = range(5, 7)

# --- START & LOGGING ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name
    user = get_user(user_id)
    
    # LOGGING
    try:
        log_text = (
            f"👤 **User Started Bot**\n"
            f"🆔 ID: `{user_id}`\n"
            f"📛 Name: {first_name}\n"
            f"🔗 Username: @{username if username else 'None'}"
        )
        await context.bot.send_message(LOG_GROUP_ID, log_text, parse_mode="Markdown")
    except: pass

    # REFERRAL
    if not user and context.args:
        referrer_arg = context.args[0]
        if referrer_arg.startswith("ref_"):
            try:
                referrer_id = int(referrer_arg.split("_")[1])
                if referrer_id != user_id:
                    add_coins(referrer_id, 100)
                    await context.bot.send_message(referrer_id, "🎉 **New Referral!**\nYou earned **100 Coins**!", parse_mode="Markdown")
            except: pass

    if user:
        update_activity(user_id)
        await send_profile_menu(update, context, user)
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "👋 Welcome! Let's set up your profile.\n\n👉 **What is your name?**",
            reply_markup=ReplyKeyboardRemove()
        )
        return REG_NAME

async def send_profile_menu(update, context, user):
    caption = (
        f"👤 **{user.get('name')}**, {user.get('age')}\n"
        f"⚧ **Gender:** {user.get('gender')}\n"
        f"💰 **Coins:** {user.get('coins', 0)}\n"
        f"📝 **Bio:** {user.get('bio')}\n\n"
        "➖➖➖➖➖➖➖➖➖➖\n"
        "**Welcome to our bot...**\n"
        "Chat anonymously with people!\n\n"
        "🔎 /search - Start Finding\n"
        "➡️ /next - Skip Partner\n"
        "🛑 /stop - Stop Chat\n"
        "💰 /balance - Check Coins\n"
        "🔗 /referral - Get Invite Link"
    )

    keyboard = [
        [InlineKeyboardButton("✏️ Edit Profile", callback_data="edit_profile")],
        [InlineKeyboardButton("💬 Start Chatting", callback_data="search_btn")]
    ]
    
    chat_id = update.effective_chat.id
    try:
        if user.get("photo_id"):
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=user.get("photo_id"),
                caption=caption,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
                protect_content=True 
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
                protect_content=True
            )
    except:
        await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# --- REGISTRATION STEPS ---

async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("Nice! How old are you?")
    return REG_AGE

async def reg_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        age = int(update.message.text)
        if age < 18:
            await update.message.reply_text("Must be 18+.")
            return ConversationHandler.END
        context.user_data['age'] = age
        keyboard = [["Male", "Female"]]
        await update.message.reply_text("Select your gender:", reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
        return REG_GENDER
    except:
        await update.message.reply_text("Please enter a number.")
        return REG_AGE

async def reg_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gender = update.message.text
    if gender not in ["Male", "Female"]:
        await update.message.reply_text("Please select Male or Female.")
        return REG_GENDER
    context.user_data['gender'] = gender
    await update.message.reply_text("Write a short bio.", reply_markup=ReplyKeyboardRemove())
    return REG_BIO

async def reg_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['bio'] = update.message.text
    await update.message.reply_text("Send a photo for your profile.")
    return REG_PHOTO

async def reg_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo: return REG_PHOTO
    photo_file = update.message.photo[-1].file_id
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    add_user(
        user_id, 
        context.user_data['name'], 
        context.user_data['age'], 
        context.user_data['gender'],
        context.user_data['bio'], 
        photo_file,
        username
    )
    add_coins(user_id, 50) 
    await update.message.reply_text("✅ Profile set! Type /search to find a partner.", parse_mode="Markdown")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Canceled.")
    return ConversationHandler.END

# --- SEARCH & CHAT LOGIC ---

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if update.callback_query: await update.callback_query.answer()

    user = get_user(user_id)
    if not user:
        await context.bot.send_message(chat_id, "⚠️ Register first with /start")
        return

    update_activity(user_id)

    if user.get("status") == "chatting":
        await context.bot.send_message(chat_id, "You are in a chat! Type /next or /stop.")
        return

    await context.bot.send_message(chat_id, "🔎 **Searching for a partner...**", parse_mode="Markdown")
    
    partner = find_search_partner(user_id)
    if partner:
        set_chat_pair(user_id, partner["_id"])
        await send_match_message(context, user_id, partner["_id"])
        await send_match_message(context, partner["_id"], user_id)
    else:
        set_status(user_id, "searching")

async def send_match_message(context, to_id, partner_id):
    partner = get_user(partner_id)
    text = (
        "✨ **A partner has been found!**\n\n"
        f"👤 **{partner.get('name')}**, {partner.get('age')}\n"
        f"⚧ **Gender:** {partner.get('gender')}\n"
        f"📝 **Bio:** {partner.get('bio')}\n\n"
        "Say, 'Hi' to reply to your partner 😊\n\n"
        "➡️ /next - Search new partner\n"
        "🛑 /stop - Stop chat"
    )
    keyboard = [[InlineKeyboardButton("👀 View Partner Photo", callback_data=f"view_{partner_id}")]]
    await context.bot.send_message(
        to_id, 
        text, 
        parse_mode="Markdown", 
        reply_markup=InlineKeyboardMarkup(keyboard),
        protect_content=True
    )

async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    partner_id = clear_chat_pair(user_id)
    
    await update.message.reply_text("🛑 Chat stopped.\nType /search to find someone else.")
    
    if partner_id:
        try:
            await context.bot.send_message(partner_id, "⚠️ **The other person left the conversation.**\nType /search to find a new partner.")
        except: pass

async def next_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_handler(update, context)
    await search_handler(update, context)

# --- USER COMMANDS ---

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user:
        await update.message.reply_text(f"💰 **Your Balance:** {user.get('coins', 0)} Coins", parse_mode="Markdown")
    else:
        await update.message.reply_text("Register first.")

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    
    text = (
        "🎁 **Invite Friends & Earn Coins!**\n\n"
        "Share your link below. When someone joins, you get **100 Coins**!\n\n"
        f"`{link}`\n\n"
        "(Click link to copy)"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# --- ADMIN COMMANDS ---

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    total_users = users_collection.count_documents({})
    searching_users = users_collection.count_documents({"status": "searching"})
    chatting_users = users_collection.count_documents({"status": "chatting"})
    text = (
        "📊 **Bot Statistics**\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🔎 Searching: {searching_users}\n"
        f"💬 Chatting Pairs: {chatting_users // 2}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def admin_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    start_time = time.time()
    msg = await update.message.reply_text("🏓 Pinging...")
    end_time = time.time()
    ms = (end_time - start_time) * 1000
    await msg.edit_text(f"🏓 Pong! `{int(ms)}ms`", parse_mode="Markdown")

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    if not context.args:
        await update.message.reply_text("Usage: `/broadcast [message]`", parse_mode="Markdown")
        return

    msg = " ".join(context.args)
    users = users_collection.find({}, {"_id": 1})
    total = users_collection.count_documents({})
    success = 0
    status_msg = await update.message.reply_text(f"📢 Starting broadcast to {total} users...")
    
    for user in users:
        try:
            await context.bot.send_message(user["_id"], f"📢 **Announcement**\n\n{msg}", parse_mode="Markdown", protect_content=True)
            success += 1
        except: pass
    
    await status_msg.edit_text(f"✅ Broadcast Complete!\nSent to: {success}/{total} users.")

# --- HANDLERS ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split("_")
    action = data[0]

    if action == "view":
        target = get_user(int(data[1]))
        if target:
            caption = f"👤 {target.get('name')}, {target.get('age')}\nBio: {target.get('bio')}"
            await context.bot.send_photo(
                query.from_user.id, 
                target.get("photo_id"), 
                caption=caption,
                protect_content=True
            )
        await query.answer()

    elif action == "search_btn":
        await query.answer()
        await search_handler(update, context)

    elif action == "edit_profile":
        await query.answer()
        await query.message.reply_text("Type /edit to change your profile.")

async def chat_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if user and user.get("status") == "chatting" and user.get("chat_partner"):
        partner_id = user["chat_partner"]
        try:
            if update.message.text: 
                await context.bot.send_message(partner_id, update.message.text, protect_content=True)
            elif update.message.photo: 
                await context.bot.send_photo(partner_id, update.message.photo[-1].file_id, protect_content=True)
            elif update.message.sticker: 
                await context.bot.send_sticker(partner_id, update.message.sticker.file_id, protect_content=True)
        except:
            await update.message.reply_text("❌ Partner disconnected.")
            await stop_handler(update, context)

# --- EDIT PROFILE ---
async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer()
    
    keyboard = [["Name", "Age"], ["Gender", "Bio"], ["Photo", "Cancel"]]
    text = "What would you like to edit?"
    
    if query:
        await query.message.reply_text(text, reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
    else:
        await update.message.reply_text(text, reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
    return EDIT_SELECT

async def edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    selection = update.message.text
    if selection == "Cancel": return ConversationHandler.END
    context.user_data['edit_field'] = selection.lower()
    await update.message.reply_text(f"Send new {selection}.", reply_markup=ReplyKeyboardRemove())
    return EDIT_UPDATE

async def edit_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    field = context.user_data['edit_field']
    if field == "photo":
        val = update.message.photo[-1].file_id
        users_collection.update_one({"_id": user_id}, {"$set": {"photo_id": val}})
    else:
        val = update.message.text
        if field == "age": 
            try: val = int(val)
            except: 
                await update.message.reply_text("Age must be number.")
                return EDIT_UPDATE
        users_collection.update_one({"_id": user_id}, {"$set": {field: val}})
    await update.message.reply_text("✅ **Profile Updated!**\nType /start to see it.", parse_mode="Markdown")
    return ConversationHandler.END

def main():
    if not TOKEN:
        print("Error: TOKEN environment variable not set")
        return

    app = Application.builder().token(TOKEN).build()
    
    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)], 
        states={
            REG_NAME: [MessageHandler(filters.TEXT, reg_name)], 
            REG_AGE: [MessageHandler(filters.TEXT, reg_age)], 
            REG_GENDER: [MessageHandler(filters.TEXT, reg_gender)],
            REG_BIO: [MessageHandler(filters.TEXT, reg_bio)], 
            REG_PHOTO: [MessageHandler(filters.PHOTO, reg_photo)]
        }, 
        fallbacks=[CommandHandler("cancel", cancel)], 
        allow_reentry=True
    )

    edit_conv = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit_start),
            CallbackQueryHandler(edit_start, pattern="^edit_profile$")
        ],
        states={
            EDIT_SELECT: [MessageHandler(filters.TEXT, edit_select)],
            EDIT_UPDATE: [MessageHandler(filters.ALL, edit_update)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    
    app.add_handler(reg_conv)
    app.add_handler(edit_conv)
    app.add_handler(CommandHandler("search", search_handler))
    app.add_handler(CommandHandler("next", next_handler))
    app.add_handler(CommandHandler("stop", stop_handler))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(CommandHandler("ping", admin_ping))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Sticker.ALL, chat_message_handler))
    
    print("Bot Running on Heroku...")
    app.run_polling()

if __name__ == "__main__":
    main()
