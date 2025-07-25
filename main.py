import os
import json
import logging
import secrets
import asyncio
from datetime import datetime
import pytz
from collections import defaultdict, deque

from fastapi import FastAPI, Request, Response, HTTPException
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction
from dotenv import load_dotenv
from groq import Groq
import whisper
from gtts import gTTS
import requests

# --- Initial Setup & Configuration ---

# Load environment variables from a .env file for local development
load_dotenv()

# Securely fetch secrets from environment variables.
# The application will fail to start if any of these are missing.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
# A secret token to secure the webhook, preventing unauthorized requests.
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", secrets.token_hex(16))

# Validate that all necessary API keys are present.
if not all([TELEGRAM_BOT_TOKEN, GROQ_API_KEY, GOOGLE_MAPS_API_KEY]):
    raise ValueError("Missing one or more critical API keys in environment variables.")

# --- Logging Configuration ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- In-Memory Cache for Conversation History ---
# For a production system, this would be replaced with Redis or a similar cache.
# Stores the last 2 interactions for each user.
conversation_history = defaultdict(lambda: deque(maxlen=4)) # Stores (user_msg, bot_msg) pairs

# --- Initialize Clients & Load Data ---
try:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    groq_client = Groq(api_key=GROQ_API_KEY)
    whisper_model = whisper.load_model("base")

    with open('delhi_secrets.json', 'r', encoding='utf-8') as f:
        delhi_secrets = json.load(f)

except FileNotFoundError:
    logger.error("delhi_secrets.json not found! The bot will run without insider tips.")
    delhi_secrets = {}
except Exception as e:
    logger.critical(f"Failed to initialize a critical service: {e}")
    raise

# --- FastAPI App Initialization ---
app = FastAPI()
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()


# --- Core AI and Data Functions ---

async def get_current_time_in_delhi() -> str:
    """Returns a formatted string of the current time and day in Delhi."""
    delhi_tz = pytz.timezone("Asia/Kolkata")
    now = datetime.now(delhi_tz)
    return now.strftime("%A, %I:%M %p")

async def detect_language_and_vibe(text: str) -> (str, str):
    """Detects language and infers user vibe in a single, efficient LLM call."""
    if not text.strip():
        return "english", "neutral"
    try:
        prompt = f"""
        Analyze the following user query. Respond with a JSON object containing two keys:
        1. "language": The detected language of the text in lowercase (e.g., "english", "hindi").
        2. "vibe": Your best guess for the user's mood or intent. Choose one from: ["adventurous", "relaxed", "hungry", "curious", "in_a_hurry", "social", "neutral"].

        User Query: "{text}"
        """
        chat_completion = await asyncio.to_thread(
            groq_client.chat.completions.create,
            messages=[{"role": "system", "content": prompt}],
            model="llama3-8b-8192",
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        result = json.loads(chat_completion.choices[0].message.content)
        language = result.get("language", "english")
        vibe = result.get("vibe", "neutral")
        return language, vibe
    except Exception as e:
        logger.error(f"Error in language/vibe detection: {e}")
        return "english", "neutral"

async def get_places_data(query: str) -> str:
    """Fetches real-time data from Google Maps Places API asynchronously."""
    url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={requests.utils.quote(query)} in Delhi&key={GOOGLE_MAPS_API_KEY}"
    try:
        # Using asyncio-compatible HTTP library would be ideal, but for this scope,
        # running requests in a thread pool is a good compromise.
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, lambda: requests.get(url, timeout=5))
        response.raise_for_status()
        data = response.json().get('results', [])
        if not data:
            return "No relevant places found."
        return "\n".join([
            f"- Name: {place.get('name')}, Rating: {place.get('rating', 'N/A')}"
            for place in data[:3]
        ])
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching Google Places data: {e}")
        return "Sorry, I couldn't fetch live location data right now."

def generate_master_prompt(language: str, user_query: str, places_data: str, history: list, time_info: str, vibe: str) -> str:
    """Generates the advanced, context-aware prompt for the main LLM call."""
    secret_tip = "No specific insider tip found for this query."
    for place, data in delhi_secrets.items():
        if place.lower() in user_query.lower():
            secret_tip = f"Insider Tip for {place}: {data.get('universal_tip', '')}"
            if 'warning' in data:
                secret_tip += f" (Warning: {data['warning']})"
            break
    
    formatted_history = "\n".join([f"User: {h[0]}\nBot: {h[1]}" for h in history])
    persona_instruction = persona_instruction_map.get(language, persona_instruction_map["default"])

    return f"""
    You are NomadAI, an expert, friendly local guide for Delhi. Your personality MUST adapt based on the user's language.
    Your knowledge is your own; do not mention that you are using Google Maps or a database.

    **Current Context:**
    - Time in Delhi: {time_info}
    - User's Detected Vibe: {vibe}
    - Detected Language: {language}
    - Your Persona: {persona_instruction}

    **Conversation History (for context):**
    {formatted_history if formatted_history else "This is the beginning of the conversation."}

    **Your Task:**
    1. Based on the **Current User Query** below, and all the context provided, generate a helpful, conversational response.
    2. Respond ONLY in fluent, natural-sounding `{language}`.
    3. Synthesize [Live Data] and [Secret Tip] into your response. Don't just list them.
    4. Your recommendation should be appropriate for the current time and the user's vibe.
    5. If the query is a follow-up, use the conversation history to understand it (e.g., "how do I get there?").

    ---
    **[Live Data]:** {places_data}
    **[Secret Tip]:** {secret_tip}
    ---
    **Current User Query:** "{user_query}"
    ---

    Now, act as their friend and respond.
    """

def get_ai_response(prompt: str) -> str:
    """Gets the final, synthesized response from the powerful LLM."""
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "system", "content": prompt}],
            model="llama3-70b-8192",
        )
        return chat_completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Error getting AI response: {e}")
        return "I'm sorry, I'm having a little trouble thinking right now. Please try again in a moment."

# --- Audio Processing Functions ---

async def transcribe_voice(audio_file_path: str) -> str:
    """Transcribes audio file to text using Whisper in a separate thread."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: whisper_model.transcribe(audio_file_path, fp16=False)
    )
    return result["text"]

async def text_to_speech(text: str, lang: str) -> str | None:
    """Converts text to speech and saves it as an OGG file asynchronously."""
    lang_code = lang_code_map.get(lang.split()[0], 'en')
    output_path = f"response_{secrets.token_hex(4)}.ogg"
    try:
        tts = gTTS(text=text, lang=lang_code, slow=False)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, tts.save, output_path)
        return output_path
    except Exception as e:
        logger.error(f"Error in text-to-speech conversion: {e}")
        return None

# --- Telegram Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message for the /start command."""
    conversation_history[update.effective_chat.id].clear()
    await update.message.reply_text(
        "Hey! I'm NomadAI. Send me a voice message in any language about what you want to do or see in Delhi!"
    )

async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles user feedback."""
    feedback_text = " ".join(context.args)
    if not feedback_text:
        await update.message.reply_text("Thanks! Please provide your feedback after the command, like: /feedback The bot was very helpful!")
        return
    logger.info(f"FEEDBACK from {update.effective_chat.id}: {feedback_text}")
    await update.message.reply_text("Thank you for your feedback! It helps me get better.")

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main handler for processing voice messages with advanced logic."""
    chat_id = update.effective_chat.id
    audio_file_path = None
    response_audio_path = None
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
        
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        audio_file_path = f"{voice.file_id}.ogg"
        await file.download_to_drive(audio_file_path)

        user_query_text = await transcribe_voice(audio_file_path)
        if not user_query_text:
            await update.message.reply_text("Sorry, I couldn't understand that. Could you please speak a bit more clearly?")
            return

        logger.info(f"User ({chat_id}): {user_query_text}")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # --- Asynchronous Data Gathering ---
        time_task = get_current_time_in_delhi()
        lang_vibe_task = detect_language_and_vibe(user_query_text)
        places_task = get_places_data(user_query_text)
        
        time_info, (language, vibe), places_data = await asyncio.gather(
            time_task, lang_vibe_task, places_task
        )
        logger.info(f"Context for {chat_id}: Lang={language}, Vibe={vibe}, Time={time_info}")
        
        # --- AI Response Generation ---
        history = list(conversation_history[chat_id])
        master_prompt = generate_master_prompt(language, user_query_text, places_data, history, time_info, vibe)
        ai_response_text = await asyncio.to_thread(get_ai_response, master_prompt)
        logger.info(f"Bot ({chat_id}): {ai_response_text}")

        # Update conversation history
        conversation_history[chat_id].append((user_query_text, ai_response_text))

        # --- Send Response ---
        response_audio_path = await text_to_speech(ai_response_text, language)
        if response_audio_path:
            await context.bot.send_voice(chat_id=chat_id, voice=open(response_audio_path, 'rb'))
        else:
            await update.message.reply_text("Sorry, I'm feeling a bit speechless right now. Please try again.")

    except Exception as e:
        logger.error(f"An unexpected error occurred in handle_voice_message: {e}", exc_info=True)
        await update.message.reply_text("Oops! Something went wrong on my end. Please try again in a moment.")
    finally:
        # Robust cleanup of temporary audio files
        if audio_file_path and os.path.exists(audio_file_path):
            os.remove(audio_file_path)
        if response_audio_path and os.path.exists(response_audio_path):
            os.remove(response_audio_path)

# --- FastAPI Webhook Endpoint ---

@app.post("/")
async def process_telegram_update(request: Request):
    """Handles all incoming updates from the Telegram webhook with security."""
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET_TOKEN:
        logger.warning("Received a request with an invalid secret token.")
        raise HTTPException(status_code=403, detail="Invalid secret token")
    try:
        data = await request.json()
        update = Update.de_json(data, bot)
        await telegram_app.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Error processing update: {e}", exc_info=True)
        return Response(status_code=500)

@app.on_event("startup")
async def startup_event():
    """Actions to take on application startup."""
    global persona_instruction_map, lang_code_map
    persona_instruction_map = {
        "hindi": "Your persona is 'Dilli Dost'. You are a witty, friendly best friend. You MUST speak in Hinglish... Be enthusiastic and informal.",
        "hinglish": "Your persona is 'Dilli Dost'. You are a witty, friendly best friend. You MUST speak in Hinglish... Be enthusiastic and informal.",
        "french": "Your persona is 'Votre ami à Delhi'. Be warm, encouraging, and polite...",
        "spanish": "Your persona is 'Tu amigo en Delhi'. Be friendly, enthusiastic, and helpful...",
        "default": "Your persona is a friendly and knowledgeable local guide. Be clear, helpful, and welcoming..."
    }
    lang_code_map = {"hindi": "hi", "hinglish": "hi", "french": "fr", "spanish": "es"}

    logger.info("Application startup...")
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("feedback", feedback_command))
    telegram_app.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handle_voice_message))

    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await telegram_app.bot.set_webhook(url=f"{webhook_url}/", secret_token=WEBHOOK_SECRET_TOKEN)
        logger.info(f"Webhook set successfully to {webhook_url}")
    else:
        logger.warning("WEBHOOK_URL environment variable not set. Webhook not configured.")

@app.get("/health")
async def health_check():
    """A simple health check endpoint to verify the service is running."""
    return {"status": "ok"}
