import os
import logging
import json
import uuid
from datetime import datetime, timedelta
from collections import defaultdict

# Importing Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore, auth

# Importing Telegram Bot libraries
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ConversationHandler
)

# --- Configuration and Initialization ---

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables (Read from PythonAnywhere environment) ---
# Set these on PythonAnywhere in your .bashrc or as environment variables
# when running an Always-on Task (if you upgrade)
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') # Your Telegram Bot API Token
APP_ID = os.environ.get('APP_ID', 'default-lottery-app') # Using a default for demonstration

# IMPORTANT: Path to your Firebase service account key file on PythonAnywhere
# You MUST upload this file to your PythonAnywhere home directory
# and replace 'your_username' with your actual PythonAnywhere username.
FIREBASE_SERVICE_ACCOUNT_FILE = os.path.join(os.path.expanduser('~'), 'serviceAccountKey.json')

# Replace with your actual announcement channel ID. It should be a negative integer.
# Example: If @RawDataBot gives you an ID like 'chat': {'id': -100123456789, ...}
# then ANNOUNCEMENT_CHANNEL_ID = -100123456789
ANNOUNCEMENT_CHANNEL_ID = int(os.environ.get('ANNOUNCEMENT_CHANNEL_ID', '-100123456789')) # Fallback for dev

# Initialize Firebase
db = None
firebase_auth = None
try:
    if os.path.exists(FIREBASE_SERVICE_ACCOUNT_FILE):
        cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_FILE)
        if not firebase_admin._apps: # Check if Firebase is already initialized
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        firebase_auth = auth
        logger.info("Firebase initialized successfully from service account file.")
    else:
        logger.error(f"Firebase service account file not found at: {FIREBASE_SERVICE_ACCOUNT_FILE}. "
                     "Firebase will not be initialized.")
        print(f"Error: Firebase service account file not found at: {FIREBASE_SERVICE_ACCOUNT_FILE}. "
              "Please upload it and update the path if necessary.")
except Exception as e:
    logger.error(f"Failed to initialize Firebase: {e}")
    print(f"Error initializing Firebase: {e}")


# --- Firestore Collection Paths ---
def get_collection_path(collection_name, user_id=None):
    if user_id:
        return f"artifacts/{APP_ID}/users/{user_id}/{collection_name}"
    return f"artifacts/{APP_ID}/public/data/{collection_name}"

LOTTERY_TICKETS_COLLECTION_PREFIX = "lotteryTickets_"
LOTTERY_REGISTRATIONS_COLLECTION = "lotteryRegistrations"
LOTTERY_DRAWS_COLLECTION = "lotteryDraws"
USER_DATA_COLLECTION = "userData" # Stored under user_id: artifacts/{appId}/users/{userId}/userData/{userId}

# --- Lottery Configuration ---
TICKET_VALUES = [100, 200, 300]
TOTAL_TICKETS_PER_VALUE = 100
PAYMENT_METHODS = [
    {"name": "Bank Transfer", "details": "Account: 123456789, Name: Grand Lottery PLC"},
    {"name": "Mobile Money (A)", "details": "Phone: +251912345678 (Grand Lottery)"},
    {"name": "Mobile Money (B)", "details": "Phone: +251987654321 (Lottery Payments)"},
]
REWARDS = {
    100: [5000, 2000, 1000],
    200: [10000, 4000, 2000],
    300: [15000, 6000, 3000],
}

# --- Conversation States ---
SELECTING_TICKET_VALUE, SELECTING_TICKET_NUMBER, UPLOADING_PAYMENT_PROOF = range(3)

# --- Global Bot Application Instance ---
application = None # Will be initialized in main

# --- Helper Functions ---

async def send_announcement(context, message, photo_url=None):
    """Sends a message to the predefined announcement channel."""
    try:
        if ANNOUNCEMENT_CHANNEL_ID and context.bot:
            if photo_url:
                await context.bot.send_photo(chat_id=ANNOUNCEMENT_CHANNEL_ID, photo=photo_url, caption=message, parse_mode='HTML')
            else:
                await context.bot.send_message(chat_id=ANNOUNCEMENT_CHANNEL_ID, text=message, parse_mode='HTML')
            logger.info(f"Announcement sent to channel {ANNOUNCEMENT_CHANNEL_ID}: {message}")
        else:
            logger.warning("ANNOUNCEMENT_CHANNEL_ID not set or context.bot not available. Cannot send announcements.")
    except Exception as e:
        logger.error(f"Error sending announcement to channel {ANNOUNCEMENT_CHANNEL_ID}: {e}")

async def initialize_tickets_firestore():
    """Initializes all lottery tickets in Firestore if they don't exist."""
    if not db:
        logger.error("Firestore DB not initialized. Cannot initialize tickets.")
        return

    for value in TICKET_VALUES:
        collection_ref = db.collection(get_collection_path(f"{LOTTERY_TICKETS_COLLECTION_PREFIX}{value}"))
        
        # Use a snapshot to get all documents at once, safer for smaller collections
        # For very large collections, pagination might be needed.
        docs = [doc async for doc in collection_ref.stream()] # Collect docs from async iterator
        existing_ticket_ids = {doc.id for doc in docs}

        if len(existing_ticket_ids) < TOTAL_TICKETS_PER_VALUE:
            batch = db.batch()
            tickets_added_count = 0
            for i in range(1, TOTAL_TICKETS_PER_VALUE + 1):
                ticket_id = str(i)
                if ticket_id not in existing_ticket_ids:
                    doc_ref = collection_ref.document(ticket_id)
                    batch.set(doc_ref, {
                        "id": ticket_id,
                        "value": value,
                        "isSold": False,
                        "buyerId": None,
                        "paymentProofUrl": None,
                        "purchaseDate": None
                    })
                    tickets_added_count += 1
            if tickets_added_count > 0:
                await batch.commit()
                logger.info(f"Initialized {tickets_added_count} new tickets for {value} Birr lottery.")
                if application and application.bot: # Ensure bot is ready to send
                    await send_announcement(application.bot, f"System Update: Initialized {tickets_added_count} new tickets for the {value} Birr lottery.")
            else:
                logger.info(f"{value} Birr tickets already fully initialized.")
        else:
            logger.info(f"{value} Birr tickets already fully initialized.")

async def get_user_data(user_id):
    """Fetches user data from Firestore."""
    if not db: return {}
    user_doc_ref = db.collection(get_collection_path(USER_DATA_COLLECTION, user_id)).document(user_id)
    doc = await user_doc_ref.get()
    return doc.to_dict() if doc.exists else {}

async def update_user_data(user_id, data, merge=True):
    """Updates user data in Firestore."""
    if not db: return
    user_doc_ref = db.collection(get_collection_path(USER_DATA_COLLECTION, user_id)).document(user_id)
    await user_doc_ref.set(data, merge=merge)

async def check_for_lottery_draw(ticket_value, context):
    """Checks if all tickets for a value are sold and triggers a draw."""
    if not db: return

    ticket_collection_ref = db.collection(get_collection_path(f"{LOTTERY_TICKETS_COLLECTION_PREFIX}{ticket_value}"))
    
    # Use a direct query for sold tickets
    sold_tickets_query = ticket_collection_ref.where("isSold", "==", True)
    sold_tickets_docs = [doc async for doc in sold_tickets_query.stream()]
    sold_count = len(sold_tickets_docs)

    if sold_count >= TOTAL_TICKETS_PER_VALUE:
        draws_collection_ref = db.collection(get_collection_path(LOTTERY_DRAWS_COLLECTION))
        # Check if a draw has already been performed for this set of 100 tickets.
        # This is a simple check. A more robust system would involve unique round IDs.
        # Here, we assume a draw implies resetting tickets for the next round.
        
        # Check if a draw for this value has happened recently AND all tickets were sold.
        # This prevents multiple draws immediately after 100 tickets are sold.
        # We look for a draw that occurred *after* the most recent ticket was sold.
        
        # Find the last purchase date for this ticket value
        last_purchase_date = None
        registrations_ref = db.collection(get_collection_path(LOTTERY_REGISTRATIONS_COLLECTION))
        q = registrations_ref.where("ticketValue", "==", ticket_value).order_by("purchaseDate", direction=firestore.Query.DESCENDING).limit(1)
        latest_purchase_doc = await q.get()
        if latest_purchase_doc:
            for doc in latest_purchase_doc: # stream returns an iterable
                last_purchase_date = doc.to_dict().get("purchaseDate")
                break
        
        draw_performed_recently = False
        if last_purchase_date:
            draw_q = draws_collection_ref.where("ticketValue", "==", ticket_value).where("drawDate", ">", last_purchase_date)
            recent_draws = await draw_q.get()
            if len(recent_draws) > 0:
                draw_performed_recently = True

        if draw_performed_recently:
            logger.info(f"Draw for {ticket_value} Birr already completed recently based on last purchase date.")
            return

        await conduct_lottery_draw(ticket_value, context)
        # After a draw, reset all tickets for that value to allow a new round
        await reset_tickets_for_value(ticket_value, context)


async def conduct_lottery_draw(ticket_value, context):
    """Conducts the lottery draw for a specific ticket value."""
    if not db: return
    logger.info(f"Conducting lottery draw for {ticket_value} Birr tickets...")
    await send_announcement(context, f"🎉 All {ticket_value} Birr tickets are sold! Preparing for the grand draw!", photo_url='https://placehold.co/600x400/FFD700/000000?text=Lottery+Draw')

    registrations_ref = db.collection(get_collection_path(LOTTERY_REGISTRATIONS_COLLECTION))
    eligible_tickets_query = registrations_ref.where("ticketValue", "==", ticket_value)
    
    # Fetch all eligible tickets for this value
    eligible_tickets_docs = [doc async for doc in eligible_tickets_query.stream()]
    eligible_tickets = [doc.to_dict() for doc in eligible_tickets_docs]
    
    if len(eligible_tickets) < 3:
        logger.warning(f"Not enough eligible tickets ({len(eligible_tickets)}) for {ticket_value} Birr draw.")
        await send_announcement(context, f"Warning: Not enough eligible tickets ({len(eligible_tickets)}) for {ticket_value} Birr draw. Draw postponed.")
        return

    # Shuffle for randomness
    import random
    random.shuffle(eligible_tickets)

    winners = []
    rewards = REWARDS.get(ticket_value, [0, 0, 0]) # Default to 0 if not found

    drawn_ticket_ids = set() # To ensure unique winners (a user might have multiple tickets)
    drawn_buyer_ids = set() # To ensure unique buyers for the top prizes (optional, depends on rules)

    # Draw 3 unique tickets (or fewer if not enough available)
    for i in range(min(3, len(eligible_tickets))):
        winner_ticket = None
        for ticket in eligible_tickets:
            # Ensure ticket has not been drawn AND the buyer (if unique winner per rank desired) hasn't won a top prize yet
            if ticket['ticketId'] not in drawn_ticket_ids and ticket['buyerId'] not in drawn_buyer_ids:
                winner_ticket = ticket
                drawn_ticket_ids.add(ticket['ticketId'])
                drawn_buyer_ids.add(ticket['buyerId']) # Mark buyer as having won a prize
                break
        
        if winner_ticket:
            winners.append({
                "rank": i + 1,
                "ticketId": winner_ticket['ticketId'],
                "winnerId": winner_ticket['buyerId'],
                "reward": rewards[i] if i < len(rewards) else 0,
            })
        else:
            logger.warning(f"Could not find a unique winner for rank {i+1} for {ticket_value} Birr.")
            break # No more unique tickets/buyers to draw

    draw_data = {
        "ticketValue": ticket_value,
        "drawDate": datetime.now().isoformat(),
        "winners": winners,
        "status": "completed"
    }

    draws_collection_ref = db.collection(get_collection_path(LOTTERY_DRAWS_COLLECTION))
    await draws_collection_ref.add(draw_data)

    announcement_message = (
        f"🎉 <b>Lottery Draw Results for {ticket_value} Birr Tickets!</b> 🎉\n"
        f"Draw Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )
    for winner in winners:
        user_id_short = winner['winnerId'][:8] + "..." if winner['winnerId'] else "N/A"
        announcement_message += (
            f"🏆 <b>{winner['rank']}. Winner:</b> Ticket #{winner['ticketId']} (User ID: <code>{user_id_short}</code>)\n"
            f"💰 Reward: <b>{winner['reward']} Birr</b>\n\n"
        )
    await send_announcement(context, announcement_message, photo_url='https://placehold.co/600x400/008000/FFFFFF?text=Winners!')
    logger.info(f"Lottery draw for {ticket_value} Birr completed and announced.")

async def reset_tickets_for_value(ticket_value, context):
    """Resets all tickets for a given value after a draw."""
    if not db: return

    logger.info(f"Resetting tickets for {ticket_value} Birr for a new round...")
    tickets_collection_ref = db.collection(get_collection_path(f"{LOTTERY_TICKETS_COLLECTION_PREFIX}{ticket_value}"))
    registrations_collection_ref = db.collection(get_collection_path(LOTTERY_REGISTRATIONS_COLLECTION))

    # Fetch all documents to delete in a batch
    tickets_snapshot = [doc async for doc in tickets_collection_ref.stream()]
    batch_tickets = db.batch()
    for doc in tickets_snapshot:
        batch_tickets.delete(doc.reference)
    await batch_tickets.commit()
    logger.info(f"Deleted all existing {ticket_value} Birr tickets.")

    # Fetch all relevant registration entries to delete in a batch
    registrations_snapshot = [doc async for doc in registrations_collection_ref.where("ticketValue", "==", ticket_value).stream()]
    batch_registrations = db.batch()
    for doc in registrations_snapshot:
        batch_registrations.delete(doc.reference)
    await batch_registrations.commit()
    logger.info(f"Deleted all {ticket_value} Birr lottery registrations.")

    # Re-initialize the tickets for a new round
    await initialize_tickets_firestore()
    if application and application.bot:
        await send_announcement(context, f"System Update: All {ticket_value} Birr tickets have been reset and are available for a new round!")


# --- Command Handlers ---

async def start(update: Update, context) -> int:
    """Sends a message with inline buttons for /start command."""
    user_id = str(update.effective_user.id)
    user_data = await get_user_data(user_id)
    if not user_data:
        # Initialize user data for new users
        await update_user_data(user_id, {
            "referralCode": user_id[:8], # Simple referral code
            "invitedUsersCount": 0,
            "referralBonusClaimed": False,
            "ticketsBoughtCount": {}
        }, merge=False)
        logger.info(f"Initialized new user data for {user_id}")

    keyboard = [
        [InlineKeyboardButton("Start Lottery", callback_data="start_lottery_conv")],
        [InlineKeyboardButton("Help", callback_data="help_cmd")],
        [InlineKeyboardButton("Rules", callback_data="rules_cmd")],
        [InlineKeyboardButton("My Tickets", callback_data="my_tickets_cmd")],
        [InlineKeyboardButton("Referral Program", callback_data="referral_cmd")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome to the Grand Lottery Bot! What would you like to do?",
        reply_markup=reply_markup
    )
    return ConversationHandler.END

async def help_command(update: Update, context) -> int:
    """Sends a message for /help command."""
    help_text = (
        "<b>Help & Support:</b>\n\n"
        "•  <b>How to play?</b> Use the 'Start Lottery' button, choose a ticket value, select an available number, and follow payment instructions.\n"
        "•  <b>How do I pay?</b> You'll be prompted to send money to listed payment methods and attach a screenshot of your payment proof.\n"
        "•  <b>What happens after I pay?</b> Your ticket number is registered. You can view your purchased tickets via 'My Tickets'.\n"
        "•  <b>When is the draw?</b> The draw for each ticket value happens automatically when all 100 tickets for that value are sold out.\n"
        "•  <b>How do I invite friends?</b> Visit the 'Referral Program' to get your code. Share it! When they join, you earn rewards.\n"
        "•  <b>What if I have issues?</b> System announcements will be posted in the main channel."
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(help_text, parse_mode='HTML')
    else:
        await update.message.reply_text(help_text, parse_mode='HTML')
    return ConversationHandler.END

async def rules_command(update: Update, context) -> int:
    """Sends a message for /rules command."""
    rules_text = (
        "<b>Official Lottery Rules:</b>\n\n"
        "1.  <b>Ticket Purchase:</b>\n"
        "    •  100 tickets per value (100, 200, 300 Birr), numbers 1-100.\n"
        "    •  Purchased numbers are removed from availability.\n"
        "2.  <b>Payment:</b>\n"
        "    •  Pay to displayed methods. Attach payment proof (screenshot/Txn ID).\n"
        "3.  <b>Loyalty Bonus:</b>\n"
        "    •  Buy 10 identical tickets, get 1 free ticket of same value.\n"
        "4.  <b>Referral Bonus:</b>\n"
        "    •  Invite 10 active users, get a FREE 200 Birr ticket.\n"
        "5.  <b>Lottery Draw:</b>\n"
        "    •  Draws for each ticket value are separate.\n"
        "    •  Triggered automatically when all 100 tickets of a value are sold.\n"
        "    •  Three winners per ticket value.\n"
        "6.  <b>Rewards:</b>\n"
        "    •  <b>100 Birr Ticket Draw:</b>\n"
        "        1st: 5,000 Birr, 2nd: 2,000 Birr, 3rd: 1,000 Birr\n"
        "    •  <b>200 Birr Ticket Draw:</b>\n"
        "        1st: 10,000 Birr, 2nd: 4,000 Birr, 3rd: 2,000 Birr\n"
        "    •  <b>300 Birr Ticket Draw:</b>\n"
        "        1st: 15,000 Birr, 2nd: 6,000 Birr, 3rd: 3,000 Birr\n"
        "7.  <b>Announcements:</b> Important updates, sales, and winners via the announcement channel.\n\n"
        "Good luck!"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(rules_text, parse_mode='HTML')
    else:
        await update.message.reply_text(rules_text, parse_mode='HTML')
    return ConversationHandler.END

# --- Ticket Purchase Flow ---

async def start_lottery_conversation(update: Update, context) -> int:
    """Starts the ticket purchase conversation by asking to select ticket value."""
    if update.callback_query:
        await update.callback_query.answer()
        query = update.callback_query
        chat_id = query.message.chat_id
    else: # If triggered by /start_lottery command directly
        chat_id = update.message.chat_id

    if not db:
        await context.bot.send_message(chat_id, "Bot is currently undergoing maintenance or Firebase is not configured. Please try again later.", parse_mode='HTML')
        return ConversationHandler.END

    keyboard = []
    
    # Fetch current sold counts for each ticket value
    ticket_status_counts = {}
    for value in TICKET_VALUES:
        collection_ref = db.collection(get_collection_path(f"{LOTTERY_TICKETS_COLLECTION_PREFIX}{value}"))
        sold_tickets_query = collection_ref.where("isSold", "==", True)
        sold_tickets_docs = [doc async for doc in sold_tickets_query.stream()]
        sold_count = len(sold_tickets_docs)
        ticket_status_counts[value] = sold_count
        
        keyboard.append([InlineKeyboardButton(f"{value} Birr Ticket ({sold_count}/{TOTAL_TICKETS_PER_VALUE} sold)", callback_data=f"select_value_{value}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        chat_id,
        "Please select a ticket value:",
        reply_markup=reply_markup
    )
    return SELECTING_TICKET_VALUE

async def select_ticket_value(update: Update, context) -> int:
    """Handles selection of ticket value and displays available numbers."""
    query = update.callback_query
    await query.answer()
    
    selected_value = int(query.data.split('_')[2])
    context.user_data['selected_ticket_value'] = selected_value
    
    if not db:
        await query.edit_message_text("Bot is currently undergoing maintenance. Please try again later.", parse_mode='HTML')
        return ConversationHandler.END

    collection_ref = db.collection(get_collection_path(f"{LOTTERY_TICKETS_COLLECTION_PREFIX}{selected_value}"))
    
    # Fetch all tickets and filter for unsold
    all_tickets_docs = [doc async for doc in collection_ref.stream()]
    available_numbers = []
    for doc in all_tickets_docs:
        ticket_data = doc.to_dict()
        if not ticket_data.get("isSold", False):
            available_numbers.append(int(ticket_data['id']))
    
    available_numbers.sort() # Ensure numbers are in order

    if not available_numbers:
        await query.edit_message_text(
            f"All {selected_value} Birr tickets are currently sold out. Please choose another value or wait for the next round!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Ticket Values", callback_data="start_lottery_conv")]])
        )
        return ConversationHandler.END

    context.user_data['available_numbers'] = available_numbers

    # Create a grid of available numbers
    keyboard = []
    row = []
    for i, num in enumerate(available_numbers):
        row.append(InlineKeyboardButton(str(num), callback_data=f"select_number_{num}"))
        if (i + 1) % 10 == 0: # 10 numbers per row
            keyboard.append(row)
            row = []
    if row: # Add any remaining numbers in the last row
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("Back to Ticket Values", callback_data="start_lottery_conv")])

    await query.edit_message_text(
        f"You selected {selected_value} Birr ticket. Please choose an available number:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECTING_TICKET_NUMBER

async def select_ticket_number(update: Update, context) -> int:
    """Handles selection of ticket number and prompts for payment."""
    query = update.callback_query
    await query.answer()
    
    selected_number = int(query.data.split('_')[2])
    selected_value = context.user_data.get('selected_ticket_value')

    if not selected_value:
        await query.edit_message_text("Error: Please select a ticket value first. Use /start_lottery.", parse_mode='HTML')
        return ConversationHandler.END

    if selected_number not in context.user_data.get('available_numbers', []):
        await query.edit_message_text(
            f"Number {selected_number} is no longer available. Please select another number.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data=f"select_value_{selected_value}")]])
        )
        return ConversationHandler.END

    context.user_data['selected_ticket_number'] = selected_number

    payment_info_text = (
        f"You selected: <b>{selected_value} Birr</b> Ticket, Number <b>{selected_number}</b>.\n\n"
        "Please send the exact amount to one of the following payment methods:\n\n"
    )
    for method in PAYMENT_METHODS:
        payment_info_text += f"<b>{method['name']}:</b> {method['details']}\n"
    
    payment_info_text += "\nAfter payment, send a <b>photo or document</b> of your payment proof (screenshot/transaction ID) to this chat to confirm your purchase."

    await query.edit_message_text(payment_info_text, parse_mode='HTML')
    return UPLOADING_PAYMENT_PROOF

async def process_payment_proof(update: Update, context) -> int:
    """Processes the payment proof (photo/document) and confirms ticket purchase."""
    user_id = str(update.effective_user.id)
    selected_value = context.user_data.get('selected_ticket_value')
    selected_number = context.user_data.get('selected_ticket_number')

    if not selected_value or not selected_number:
        await update.message.reply_text("It seems there was an issue with your ticket selection. Please start again with /start_lottery.")
        return ConversationHandler.END

    payment_proof_info = None
    if update.message.photo:
        payment_proof_info = update.message.photo[-1].file_id # Get the largest photo
        # In a real scenario, you'd download this photo and store it securely
        # For this demo, we'll just use the file_id as a placeholder
        await update.message.reply_text("Thank you for sending your payment proof (photo). Processing your purchase...")
    elif update.message.document:
        payment_proof_info = update.message.document.file_id
        await update.message.reply_text("Thank you for sending your payment proof (document). Processing your purchase...")
    else:
        await update.message.reply_text("Please send a <b>photo or document</b> as proof of payment.", parse_mode='HTML')
        return UPLOADING_PAYMENT_PROOF # Stay in this state until valid proof is sent

    if not db:
        await update.message.reply_text("Bot is currently undergoing maintenance. Please try again later.", parse_mode='HTML')
        return ConversationHandler.END

    try:
        # Check if the ticket is still available to prevent double-booking
        ticket_doc_ref = db.collection(get_collection_path(f"{LOTTERY_TICKETS_COLLECTION_PREFIX}{selected_value}")).document(str(selected_number))
        ticket_doc = await ticket_doc_ref.get()

        if ticket_doc.exists and not ticket_doc.to_dict().get("isSold", False):
            # Mark ticket as sold
            await ticket_doc_ref.update({
                "isSold": True,
                "buyerId": user_id,
                "paymentProofInfo": payment_proof_info,
                "purchaseDate": datetime.now().isoformat()
            })

            # Register in lottery registrations for draw tracking
            await db.collection(get_collection_path(LOTTERY_REGISTRATIONS_COLLECTION)).add({
                "ticketId": str(selected_number),
                "ticketValue": selected_value,
                "buyerId": user_id,
                "purchaseDate": datetime.now().isoformat(),
                "isFree": False,
                "freeReason": None
            })

            # Update user's tickets bought count for loyalty bonus
            user_data = await get_user_data(user_id)
            tickets_bought_count = user_data.get("ticketsBoughtCount", {})
            tickets_bought_count[str(selected_value)] = tickets_bought_count.get(str(selected_value), 0) + 1
            await update_user_data(user_id, {"ticketsBoughtCount": tickets_bought_count})

            await update.message.reply_text(
                f"🎉 Congratulations! You have successfully purchased the <b>{selected_value} Birr</b> Ticket Number <b>{selected_number}</b>.",
                parse_mode='HTML'
            )
            await send_announcement(context, f"Ticket Sold: A {selected_value} Birr ticket (Number #{selected_number}) was just purchased by User ID <code>{user_id[:8]}...</code>!")

            # Check for loyalty bonus (1 free ticket for every 10 identical tickets bought)
            if tickets_bought_count[str(selected_value)] % 10 == 0:
                await award_loyalty_bonus(user_id, selected_value, context)
            
            # Check if all tickets are sold for this value and trigger draw
            await check_for_lottery_draw(selected_value, context)

        else:
            await update.message.reply_text("Sorry, this ticket number has just been sold. Please choose another number or value via /start_lottery.")
            return ConversationHandler.END # End conversation if ticket is already sold
            
    except Exception as e:
        logger.error(f"Error during ticket purchase for user {user_id}: {e}")
        await update.message.reply_text("An error occurred during your purchase. Please try again later.")
    
    context.user_data.clear() # Clear user-specific data after purchase
    return ConversationHandler.END # End the conversation

async def cancel_purchase(update: Update, context) -> int:
    """Cancels the current purchase flow."""
    await update.message.reply_text("Ticket purchase cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

async def award_loyalty_bonus(user_id, ticket_value, context):
    """Awards a free ticket as a loyalty bonus."""
    if not db: return

    available_free_numbers = []
    collection_ref = db.collection(get_collection_path(f"{LOTTERY_TICKETS_COLLECTION_PREFIX}{ticket_value}"))
    
    # Fetch all tickets to find unsold ones
    all_tickets_docs = [doc async for doc in collection_ref.stream()]

    for doc in all_tickets_docs:
        ticket_data = doc.to_dict()
        if not ticket_data.get("isSold", False):
            available_free_numbers.append(int(ticket_data['id']))
    
    if available_free_numbers:
        import random
        free_ticket_number = random.choice(available_free_numbers)
        free_ticket_doc_ref = db.collection(get_collection_path(f"{LOTTERY_TICKETS_COLLECTION_PREFIX}{ticket_value}")).document(str(free_ticket_number))
        
        await free_ticket_doc_ref.update({
            "isSold": True,
            "buyerId": user_id,
            "paymentProofInfo": "Loyalty Bonus", # Mark as free
            "purchaseDate": datetime.now().isoformat(),
            "isFree": True,
            "freeReason": "Loyalty Bonus"
        })

        await db.collection(get_collection_path(LOTTERY_REGISTRATIONS_COLLECTION)).add({
            "ticketId": str(free_ticket_number),
            "ticketValue": ticket_value,
            "buyerId": user_id,
            "purchaseDate": datetime.now().isoformat(),
            "isFree": True,
            "freeReason": "Loyalty Bonus"
        })

        await context.bot.send_message(
            user_id,
            f"🎉 <b>Loyalty Bonus!</b> You've earned a FREE <b>{ticket_value} Birr</b> Ticket! Your free ticket number is <b>{free_ticket_number}</b>.",
            parse_mode='HTML'
        )
        await send_announcement(context, f"Loyalty Bonus: User ID <code>{user_id[:8]}...</code> received a free {ticket_value} Birr ticket (Number #{free_ticket_number})!")
        logger.info(f"User {user_id} awarded loyalty bonus for {ticket_value} Birr ticket #{free_ticket_number}.")
    else:
        await context.bot.send_message(
            user_id,
            f"You qualified for a free {ticket_value} Birr ticket, but no numbers are currently available. Please contact support."
        )
        logger.warning(f"User {user_id} qualified for loyalty bonus for {ticket_value} Birr, but no tickets available.")


# --- My Tickets Command ---

async def my_tickets_command(update: Update, context) -> None:
    """Displays user's purchased tickets and recent draw results."""
    user_id = str(update.effective_user.id)
    if update.callback_query:
        await update.callback_query.answer()
        chat_id = update.callback_query.message.chat_id
    else:
        chat_id = update.message.chat_id

    if not db:
        await context.bot.send_message(chat_id, "Bot is currently undergoing maintenance or Firebase is not configured. Please try again later.", parse_mode='HTML')
        return

    try:
        tickets_info = []
        registrations_ref = db.collection(get_collection_path(LOTTERY_REGISTRATIONS_COLLECTION))
        user_tickets_query = registrations_ref.where("buyerId", "==", user_id)
        user_tickets_docs = [doc async for doc in user_tickets_query.stream()]

        if not user_tickets_docs:
            tickets_info.append("You haven't purchased any tickets yet. Use /start_lottery to buy one!")
        else:
            tickets_info.append("<b>Your Purchased Tickets:</b>\n")
            for doc in user_tickets_docs:
                ticket = doc.to_dict()
                free_status = f" (FREE: {ticket['freeReason']})" if ticket.get('isFree') else ""
                tickets_info.append(f"• Value: {ticket['ticketValue']} Birr, Number: {ticket['ticketId']}{free_status} (Purchased: {datetime.fromisoformat(ticket['purchaseDate']).strftime('%Y-%m-%d %H:%M')})")

        tickets_message = "\n".join(tickets_info)
        await context.bot.send_message(chat_id, tickets_message, parse_mode='HTML')

        # Display recent draw results
        draws_info = ["\n<b>Recent Lottery Draws:</b>\n"]
        draws_ref = db.collection(get_collection_path(LOTTERY_DRAWS_COLLECTION))
        
        # Fetch all draws and sort in memory (Firestore's orderBy isn't supported without indexing)
        all_draws_docs = [doc async for doc in draws_ref.stream()]
        all_draws = sorted([doc.to_dict() for doc in all_draws_docs], key=lambda x: x.get('drawDate', ''), reverse=True)

        if not all_draws:
            draws_info.append("No lottery draws have occurred yet.")
        else:
            for draw in all_draws[:3]: # Show only latest 3 draws
                draws_info.append(f"<b>--- {draw['ticketValue']} Birr Lottery Draw ---</b>")
                draws_info.append(f"Date: {datetime.fromisoformat(draw['drawDate']).strftime('%Y-%m-%d %H:%M')}")
                for winner in draw['winners']:
                    user_id_short = winner['winnerId'][:8] + "..." if winner['winnerId'] else "N/A"
                    draws_info.append(f"  🏆 {winner['rank']}. Winner: Ticket #{winner['ticketId']} (User: <code>{user_id_short}</code>) - {winner['reward']} Birr")
                draws_info.append("\n") # Add a newline for spacing

        draws_message = "\n".join(draws_info)
        await context.bot.send_message(chat_id, draws_message, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Error fetching user tickets or draw results for {user_id}: {e}")
        await context.bot.send_message(chat_id, "An error occurred while fetching your tickets or draw results. Please try again later.")

# --- Referral Program ---

async def referral_command(update: Update, context) -> None:
    """Displays user's referral code and allows claiming bonus."""
    user_id = str(update.effective_user.id)
    if update.callback_query:
        await update.callback_query.answer()
        chat_id = update.callback_query.message.chat_id
    else:
        chat_id = update.message.chat_id

    if not db:
        await context.bot.send_message(chat_id, "Bot is currently undergoing maintenance or Firebase is not configured. Please try again later.", parse_mode='HTML')
        return

    user_data = await get_user_data(user_id)
    referral_code = user_data.get("referralCode", user_id[:8]) # Default to first 8 chars of user ID
    invited_users_count = user_data.get("invitedUsersCount", 0)
    referral_bonus_claimed = user_data.get("referralBonusClaimed", False)

    message_text = (
        f"<b>Your Referral Program:</b>\n\n"
        f"Share your unique referral code with friends and earn rewards!\n"
        f"Your Referral Code: <code>{referral_code}</code>\n\n"
        f"Invited Active Users: <b>{invited_users_count} / 10</b>\n"
        f"<i>(Note: 'Active user' is simulated; in a real system, it would track real user activity.)</i>\n\n"
    )

    keyboard = []
    if invited_users_count >= 10 and not referral_bonus_claimed:
        message_text += "You have qualified for a FREE 200 Birr ticket!\n"
        keyboard.append([InlineKeyboardButton("Claim FREE 200 Birr Ticket!", callback_data="claim_referral_bonus")])
    elif referral_bonus_claimed:
        message_text += "You have already claimed your referral bonus.\n"
    else:
        message_text += f"Invite {10 - invited_users_count} more active users to claim your bonus!\n"
    
    # Add a simulation button for testing
    keyboard.append([InlineKeyboardButton("Simulate New Invited User (Dev Tool)", callback_data="simulate_invite")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id, message_text, reply_markup=reply_markup, parse_mode='HTML')


async def claim_referral_bonus(update: Update, context) -> None:
    """Claims the referral bonus for the user."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id

    if not db:
        await context.bot.send_message(chat_id, "Bot is currently undergoing maintenance or Firebase is not configured. Please try again later.", parse_mode='HTML')
        return

    user_data = await get_user_data(user_id)
    invited_users_count = user_data.get("invitedUsersCount", 0)
    referral_bonus_claimed = user_data.get("referralBonusClaimed", False)

    if invited_users_count >= 10 and not referral_bonus_claimed:
        try:
            # Award a free 200 Birr ticket
            ticket_value = 200
            available_free_numbers = []
            collection_ref = db.collection(get_collection_path(f"{LOTTERY_TICKETS_COLLECTION_PREFIX}{ticket_value}"))
            
            # Fetch all tickets to find unsold ones
            all_tickets_docs = [doc async for doc in collection_ref.stream()]

            for doc in all_tickets_docs:
                ticket_data = doc.to_dict()
                if not ticket_data.get("isSold", False):
                    available_free_numbers.append(int(ticket_data['id']))
            
            if available_free_numbers:
                import random
                free_ticket_number = random.choice(available_free_numbers)
                free_ticket_doc_ref = db.collection(get_collection_path(f"{LOTTERY_TICKETS_COLLECTION_PREFIX}{ticket_value}")).document(str(free_ticket_number))
                
                await free_ticket_doc_ref.update({
                    "isSold": True,
                    "buyerId": user_id,
                    "paymentProofInfo": "Referral Bonus", # Mark as free
                    "purchaseDate": datetime.now().isoformat(),
                    "isFree": True,
                    "freeReason": "Referral Bonus"
                })

                await db.collection(get_collection_path(LOTTERY_REGISTRATIONS_COLLECTION)).add({
                    "ticketId": str(free_ticket_number),
                    "ticketValue": ticket_value,
                    "buyerId": user_id,
                    "purchaseDate": datetime.now().isoformat(),
                    "isFree": True,
                    "freeReason": "Referral Bonus"
                })

                # Mark bonus as claimed in user data
                await update_user_data(user_id, {"referralBonusClaimed": True}, merge=True)

                await query.edit_message_text(
                    f"🎉 <b>Referral Bonus Claimed!</b> You received a FREE <b>200 Birr</b> Ticket! Your free ticket number is <b>{free_ticket_number}</b>.",
                    parse_mode='HTML'
                )
                await send_announcement(context, f"Referral Bonus: User ID <code>{user_id[:8]}...</code> claimed a free 200 Birr ticket (Number #{free_ticket_number}) for inviting 10 active users!")
                logger.info(f"User {user_id} claimed referral bonus and received ticket #{free_ticket_number}.")
            else:
                await query.edit_message_text("No 200 Birr tickets available for referral bonus at the moment. Please try again later.")
                logger.warning(f"User {user_id} qualified for referral bonus, but no tickets available.")

        except Exception as e:
            logger.error(f"Error claiming referral bonus for user {user_id}: {e}")
            await query.edit_message_text("An error occurred while claiming your bonus. Please try again later.")
    else:
        await query.edit_message_text("You are not eligible to claim the referral bonus yet, or you have already claimed it.")

async def simulate_invite(update: Update, context) -> None:
    """Simulates a new invited user for testing purposes."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    
    if not db:
        await query.edit_message_text("Bot is currently undergoing maintenance or Firebase is not configured. Please try again later.", parse_mode='HTML')
        return

    user_data = await get_user_data(user_id)
    current_invited_count = user_data.get("invitedUsersCount", 0)
    
    await update_user_data(user_id, {"invitedUsersCount": current_invited_count + 1}, merge=True)
    
    updated_invited_count = current_invited_count + 1
    referral_code = user_data.get("referralCode", user_id[:8])
    referral_bonus_claimed = user_data.get("referralBonusClaimed", False)

    message_text = (
        f"<b>Your Referral Program:</b>\n\n"
        f"Share your unique referral code with friends and earn rewards!\n"
        f"Your Referral Code: <code>{referral_code}</code>\n\n"
        f"Invited Active Users: <b>{updated_invited_count} / 10</b>\n"
        f"<i>(Note: 'Active user' is simulated; in a real system, it would track real user activity.)</i>\n\n"
    )

    keyboard = []
    if updated_invited_count >= 10 and not referral_bonus_claimed:
        message_text += "You have qualified for a FREE 200 Birr ticket!\n"
        keyboard.append([InlineKeyboardButton("Claim FREE 200 Birr Ticket!", callback_data="claim_referral_bonus")])
    elif referral_bonus_claimed:
        message_text += "You have already claimed your referral bonus.\n"
    else:
        message_text += f"Invite {10 - updated_invited_count} more active users to claim your bonus!\n"
    
    keyboard.append([InlineKeyboardButton("Simulate New Invited User (Dev Tool)", callback_data="simulate_invite")])

    await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    logger.info(f"User {user_id} simulated a new invited user. Total: {updated_invited_count}")


# --- Main Function ---

def main() -> None:
    """Starts the bot."""
    global application

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set. Bot cannot start.")
        print("Error: TELEGRAM_BOT_TOKEN environment variable not set. Please set it before running.")
        return
    
    if not db: # Check if Firebase initialized successfully
        print("Error: Firebase was not initialized correctly. Bot cannot run without database access.")
        return

    # Create the Application and pass your bot's token.
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Define conversation handler for ticket purchase flow
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start_lottery", start_lottery_conversation),
            CallbackQueryHandler(start_lottery_conversation, pattern="^start_lottery_conv$")
        ],
        states={
            SELECTING_TICKET_VALUE: [
                CallbackQueryHandler(select_ticket_value, pattern="^select_value_"),
                CallbackQueryHandler(start_lottery_conversation, pattern="^start_lottery_conv$") # Allow going back
            ],
            SELECTING_TICKET_NUMBER: [
                CallbackQueryHandler(select_ticket_number, pattern="^select_number_"),
                CallbackQueryHandler(select_ticket_value, pattern="^select_value_") # Allow going back
            ],
            UPLOADING_PAYMENT_PROOF: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, process_payment_proof),
                CommandHandler("cancel", cancel_purchase), # Allow cancelling purchase
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_purchase)],
    )

    # Add handlers for basic commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("rules", rules_command))

    # Add callback handlers for inline buttons
    application.add_handler(CallbackQueryHandler(help_command, pattern="^help_cmd$"))
    application.add_handler(CallbackQueryHandler(rules_command, pattern="^rules_cmd$"))
    application.add_handler(CallbackQueryHandler(my_tickets_command, pattern="^my_tickets_cmd$"))
    application.add_handler(CallbackQueryHandler(referral_command, pattern="^referral_cmd$"))
    application.add_handler(CallbackQueryHandler(claim_referral_bonus, pattern="^claim_referral_bonus$"))
    application.add_handler(CallbackQueryHandler(simulate_invite, pattern="^simulate_invite$"))

    # Add the conversation handler
    application.add_handler(conv_handler)

    # Initialize tickets on bot startup (this will also send announcements if needed)
    # This runs asynchronously
    application.create_task(initialize_tickets_firestore())

    # Run the bot until the user presses Ctrl-C
    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
