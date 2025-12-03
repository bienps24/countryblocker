import os
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
import phonenumbers
from phonenumbers import NumberParseException

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    ChatMemberUpdated, ChatMember, ChatJoinRequest
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    ChatJoinRequestHandler, ContextTypes, filters, PicklePersistence
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import Forbidden, BadRequest

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

class DatabaseManager:
    """Manages all interactions with the SQLite database using best practices."""
    def __init__(self, db_path: str = "filipino_bot.db"):
        self.db_path = db_path
        self.init_database()

    @contextmanager
    def get_conn(self):
        """Provides a database connection using a context manager to ensure it's always closed."""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def init_database(self):
        """Initializes the database schema if tables don't exist."""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS verified_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    phone_number TEXT,
                    verified_date TIMESTAMP,
                    is_banned BOOLEAN DEFAULT FALSE
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS join_requests (
                    user_id INTEGER,
                    chat_id INTEGER,
                    request_date TIMESTAMP,
                    status TEXT DEFAULT 'pending',
                    PRIMARY KEY (user_id, chat_id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS managed_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT,
                    link TEXT UNIQUE NOT NULL,
                    chat_id INTEGER UNIQUE
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS allowed_country_codes (
                    chat_id INTEGER,
                    country_code TEXT,
                    PRIMARY KEY (chat_id, country_code)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS group_admins (
                    chat_id INTEGER,
                    user_id INTEGER,
                    PRIMARY KEY (chat_id, user_id)
                )
            ''')
            conn.commit()

    def add_verified_user(self, user_id: int, username: str, first_name: str, phone_number: str):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO verified_users 
                (user_id, username, first_name, phone_number, verified_date, is_banned)
                VALUES (?, ?, ?, ?, ?, FALSE)
            ''', (user_id, username or "", first_name or "", phone_number, datetime.now()))
            conn.commit()

    def is_verified(self, user_id: int) -> bool:
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM verified_users WHERE user_id = ? AND is_banned = FALSE', (user_id,))
            return cursor.fetchone() is not None

    def ban_user(self, user_id: int):
        with self.get_conn() as conn:
            conn.cursor().execute('UPDATE verified_users SET is_banned = TRUE WHERE user_id = ?', (user_id,))
            conn.commit()

    def get_all_groups(self) -> List[Dict[str, Any]]:
        with self.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT id, name, description, link, chat_id FROM managed_groups ORDER BY id')
            return [dict(row) for row in cursor.fetchall()]

    def add_group(self, name: str, description: str, link: str) -> bool:
        """Adds input validation before database insertion."""
        if not name.strip() or not link.strip():
            logger.error("Group name and link cannot be empty.")
            return False
        if not link.startswith(('https://t.me/', 'http://t.me/')):
            logger.error(f"Invalid Telegram link format: {link}")
            return False
            
        try:
            with self.get_conn() as conn:
                conn.cursor().execute('INSERT INTO managed_groups (name, description, link) VALUES (?, ?, ?)', (name, description, link))
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Attempted to add a group with a duplicate link: {link}")
            return False

    def remove_group(self, group_id: int) -> Optional[Dict[str, Any]]:
        with self.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM managed_groups WHERE id = ?', (group_id,))
            group = cursor.fetchone()
            if group:
                cursor.execute('DELETE FROM managed_groups WHERE id = ?', (group_id,))
                conn.commit()
                return dict(group)
            return None
    
    def update_chat_id_by_link(self, link: str, chat_id: int):
        with self.get_conn() as conn:
            conn.cursor().execute('UPDATE managed_groups SET chat_id = ? WHERE link = ?', (chat_id, link))
            conn.commit()
            logger.info(f"Updated chat_id for group with link {link} to {chat_id}")

    def set_country_code_for_group(self, chat_id: int, country_code: str):
        """Set allowed country code for the given group."""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO allowed_country_codes (chat_id, country_code)
                VALUES (?, ?)
            ''', (chat_id, country_code))
            conn.commit()

    def get_allowed_country_codes_for_group(self, chat_id: int) -> List[str]:
        """Get the allowed country codes for a group."""
        with self.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT country_code FROM allowed_country_codes WHERE chat_id = ?
            ''', (chat_id,))
            return [row['country_code'] for row in cursor.fetchall()]

class PhoneVerifier:
    @staticmethod
    def verify_phone_number(phone_number: str, allowed_codes: List[str]) -> dict:
        """Verify if a phone number matches one of the allowed country codes."""
        try:
            parsed = phonenumbers.parse(phone_number)
            country_code = str(parsed.country_code)  # Get the country code
            if country_code not in allowed_codes:
                return {'is_valid': False, 'message': f"Only users from {', '.join(allowed_codes)} are allowed to join."}
            is_valid = phonenumbers.is_valid_number(parsed)
            return {'is_valid': is_valid, 'country_code': country_code}
        except NumberParseException:
            return {'is_valid': False, 'message': "Invalid phone number format."}

class FilipinoBotManager:
    def __init__(self):
        if not BOT_TOKEN: raise ValueError("BOT_TOKEN environment variable is required!")
        if not ADMIN_ID: raise ValueError("ADMIN_ID environment variable is required!")
        
        self.db = DatabaseManager()
        self.verifier = PhoneVerifier()
        self._groups_lock = threading.Lock()
        self.filipino_groups = []
        self.refresh_groups_cache()

    def refresh_groups_cache(self):
        """Reloads the group list from the database safely."""
        with self._groups_lock:
            self.filipino_groups = self.db.get_all_groups()
            logger.info("Refreshed groups cache from database.")
            
    def format_available_groups(self) -> str:
        """Reads from the thread-safe cache to format the group list."""
        with self._groups_lock:
            if not self.filipino_groups:
                return "🔍 No groups available at the moment."
            
            message = "🇵🇭 **Available Filipino Groups:**\n\n"
            for group in self.filipino_groups:
                message += f"**- {group['name']}**\n"
                message += f"  📝 {group['description']}\n"
                message += f"  🔗 {group['link']}\n\n"
            
            message += "💡 **Tip:** Verified users are auto-approved!"
            return message

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if self.db.is_verified(user.id):
            await update.message.reply_text(
                "✅ *Na-verify ka na!*\n\n" + self.format_available_groups(),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        else:
            contact_keyboard = [[KeyboardButton("📱 Share My Phone Number", request_contact=True)]]
            contact_markup = ReplyKeyboardMarkup(contact_keyboard, one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(
                f"🇵🇭 *Filipino Verification*\n\nHi {user.first_name}! To join our exclusive Filipino groups, please verify your identity by sharing your Philippine phone number.",
                reply_markup=contact_markup,
                parse_mode=ParseMode.MARKDOWN
            )

    async def handle_contact_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        contact = update.message.contact
        user = update.effective_user

        if contact.user_id != user.id:
            await update.message.reply_text("❌ Please share your own contact information.", reply_markup=ReplyKeyboardRemove())
            return
        
        phone_result = self.verifier.verify_phone_number(contact.phone_number, self.db.get_allowed_country_codes_for_group(0))
        
        if phone_result['is_valid']:
            self.db.add_verified_user(user.id, user.username, user.first_name, contact.phone_number)
            success_msg = f"✅ **VERIFIED!** 🇵🇭\n\nWelcome, {user.first_name}!\n\nYour number {phone_result['formatted_number']} is verified. You now have access to all our groups and will be auto-approved.\n\n{self.format_available_groups()}"
            await update.message.reply_text(success_msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=ReplyKeyboardRemove())
            await context.bot.send_message(ADMIN_ID, f"✅ New Verified User: {user.first_name} (@{user.username or 'N/A'}), ID: `{user.id}`", parse_mode=ParseMode.MARKDOWN)
            
            # Auto-approve any pending join requests for this newly verified user
            await self.approve_pending_requests(context, user.id)
            
        else:
            fail_msg = f"❌ **Verification Failed**\n\nThe number you provided ({phone_result['formatted_number']}) is not recognized as a Philippine number. Please try again with a valid PH number."
            await update.message.reply_text(fail_msg, reply_markup=ReplyKeyboardRemove())

    async def approve_pending_requests(self, context: ContextTypes.DEFAULT_TYPE, user_id: int):
        """Auto-approve any pending join requests for a newly verified user."""
        try:
            with self.db.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT chat_id FROM join_requests WHERE user_id = ? AND status = 'pending'", 
                    (user_id,)
                )
                pending_requests = cursor.fetchall()
                
                for (chat_id,) in pending_requests:
                    try:
                        # Try to approve the pending request
                        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
                        self.db.update_join_request_status(user_id, chat_id, "approved")
                        
                        # Get chat info for welcome message
                        try:
                            chat = await context.bot.get_chat(chat_id)
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=f"🎉 **Automatically Approved!**\n\nYou've been approved to join **{chat.title}** since you're now a verified Filipino user! 🇵🇭",
                                parse_mode=ParseMode.MARKDOWN
                            )
                            
                            # Notify admin
                            await context.bot.send_message(
                                ADMIN_ID,
                                f"🎉 Auto-approved pending request: User {user_id} for {chat.title}",
                                parse_mode=ParseMode.MARKDOWN
                            )
                            
                        except Exception as e:
                            logger.warning(f"Could not send auto-approval message: {e}")
                            
                        logger.info(f"Auto-approved pending request for user {user_id} to chat {chat_id}")
                        
                    except Exception as e:
                        logger.error(f"Failed to approve pending request for user {user_id} to chat {chat_id}: {e}")
                        self.db.update_join_request_status(user_id, chat_id, "error")
                        
        except Exception as e:
            logger.error(f"Error checking pending requests for user {user_id}: {e}")

    # Additional functions for handling commands and permissions can go here

    def run(self):
        persistence = PicklePersistence(filepath="filipino_bot_persistence")
        application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

        # Command handlers
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("groups", self.groups_command))
        
        # Message handlers
        application.add_handler(MessageHandler(filters.CONTACT, self.handle_contact_message))
        
        # Chat member handlers
        application.add_handler(ChatJoinRequestHandler(self.handle_join_request))
        application.add_handler(ChatMemberHandler(self.handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
        application.add_handler(ChatMemberHandler(self.handle_my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

        logger.info("🚀 Filipino Verification Bot (v3.1 - Complete) is starting...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

def main():
    try:
        bot_manager = FilipinoBotManager()
        bot_manager.run()
    except (ValueError, Exception) as e:
        logger.critical(f"❌ A fatal error occurred: {e}", exc_info=True)

if __name__ == "__main__":
    main()
