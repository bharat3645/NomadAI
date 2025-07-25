import os
import json
import logging
import requests
from fastapi import FastAPI, Request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
from groq import Groq
import whisper
from gtts import gTTS

# Load environment variables from .env file for local development
load_dotenv()

# --- API Keys and Tokens ---
# Securely fetch secrets from environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# --- Initialize Clients ---
# Initialize the Telegram Bot client
bot = Bot(token=TELEGRAM_BOT_TOKEN)
# Initialize the Groq client for LLM access
groq_client = Groq(api_key=GROQ_API_KEY)
# Load the Whisper model. "base" is a good balance of speed and accuracy for a hackathon.
# Use "tiny" if the "base" model is too slow or memory-intensive on the free hosting tier.
whisper_model = whisper.load_model("base")

# --- Logging Configuration ---
# Set up basic logging to see bot activity and errors in the console or server logs.
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Load Secret Data ---
# Load our curated insider tips from the JSON file into memory.
with open('delhi_secrets.json', 'r') as f:
    delhi_secrets = json.load(f)

# --- FastAPI App Initialization ---
# This creates the main web application instance.
app = FastAPI()

def detect_language(text: str) -> str:
    """Detects the language of the text using a fast LLM."""
    try:
        # This is a specialized, low-cost API call just for language detection.
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are a language detection expert. Analyze the following text and respond with only the name of the language in lowercase. For example: 'english', 'hindi', 'french'."
                },
                {
                    "role": "user",
                    "content": text,
                }
            ],
            model="llama3-8b-8192", # Use the smaller, faster model for this simple task.
            temperature=0.1,
        )
        return chat_completion.choices[0].message.content.strip().lower()
    except Exception as e:
        logger.error(f"Error in language detection: {e}")
        return "english" # Default to English on error to ensure the bot can always respond.

def get_places_data(query: str) -> str:
    """Fetches real-time data from Google Maps Places API."""
    # The URL is constructed to search for the user's query specifically within Delhi.
    url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={query} in Delhi&key={GOOGLE_MAPS_API_KEY}"
    try:
        response = requests.get(url)
        response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
        data = response.json().get('results', [])
        if not data:
            return "No relevant places found."

        # Format the data cleanly for the LLM to easily parse.
        # Providing a few key details for the top 3 results is enough for a conversational response.
        formatted_data = "\n".join([
            f"- Name: {place.get('name')}, Rating: {place.get('rating', 'N/A')}, Address: {place.get('formatted_address', 'N/A')}"
            for place in data[:3] # Get top 3 results
        ])
        return formatted_data
    except Exception as e:
        logger.error(f"Error fetching Google Places data: {e}")
        return "Sorry, I couldn't fetch live location data right now."

def generate_master_prompt(language: str, user_query: str, places_data: str) -> str:
    """Generates the dynamic, persona-driven prompt for the main LLM call."""

    # Find relevant secret tips by checking if any landmark name is in the user's query.
    secret_tip = "No specific insider tip found for this query."
    for place, data in delhi_secrets.items():
        if place.lower() in user_query.lower():
            secret_tip = f"Insider Tip for {place}: {data['universal_tip']}"
            if 'warning' in data:
                secret_tip += f" (Warning: {data['warning']})"
            break

    # This is the core of the "Persona Chameleon". We dynamically generate the persona instructions.
    persona_instruction = ""
    if "hindi" in language or "hinglish" in language:
        persona_instruction = "Your persona is 'Dilli Dost'. You are a witty, friendly best friend. You MUST speak in Hinglish (a mix of Hindi and English). Use slang like 'yaar', 'bhai', 'scene', 'chill', 'mast'. Be enthusiastic and informal."
    elif "french" in language:
        persona_instruction = "Your persona is 'Votre ami à Delhi'. Be warm, encouraging, and polite. Use phrases like 'Bienvenue' and 'Profitez bien'. Respond in fluent, natural-sounding French."
    elif "spanish" in language:
        persona_instruction = "Your persona is 'Tu amigo en Delhi'. Be friendly, enthusiastic, and helpful. Use phrases like '¡Hola!' and '¡Qué disfrutes!'. Respond in fluent, natural-sounding Spanish."
    else: # Default persona for any other language
        persona_instruction = "Your persona is a friendly and knowledgeable local guide. Be clear, helpful, and welcoming. Respond in fluent, natural-sounding English."

    # The master prompt is a detailed instruction manual for the LLM.
    prompt = f"""
    You are NomadAI, a helpful and friendly local guide in Delhi. Your personality MUST adapt based on the user's language.

    **Detected Language:** {language}
    **Your Persona Instruction:** {persona_instruction}

    **Your Task:**
    1. Respond ONLY in fluent and natural-sounding `{language}`. Do not mix languages unless the persona is Hinglish.
    2. Weave the [Live Data] and [Secret Tip] together into a single, conversational, and helpful response. Do not just list the data like a robot. Synthesize it into a real recommendation.
    3. If the user query is a simple greeting (like 'hello' or 'how are you'), respond with a warm greeting in the detected language and persona. Do not perform a location search for a simple greeting.
    4. Translate the meaning and vibe of the [Secret Tip], not just a literal word-for-word translation. Capture the *feeling* of the tip.

    ---
    **User's Query (in their language):** "{user_query}"
    ---
    **[Live Data from Google Maps]:**
    {places_data}
    ---
    **[Secret Tip from Local Database]:**
    {secret_tip}
    ---

    Now, act as their friend and respond.
    """
    return prompt

def get_ai_response(prompt: str) -> str:
    """Gets the final, synthesized response from the powerful LLM."""
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": prompt,
                }
            ],
            model="llama3-70b-8192", # Use the more powerful model for the final creative response.
        )
        return chat_completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Error getting AI response: {e}")
        return "I'm sorry, I'm having a little trouble thinking right now. Please try again in a moment."


def transcribe_voice(audio_file_path: str) -> str:
    """Transcribes audio file to text using Whisper."""
    try:
        # The core transcription call.
        result = whisper_model.transcribe(audio_file_path)
        return result["text"]
    except Exception as e:
        logger.error(f"Error in transcription: {e}")
        return ""

def text_to_speech(text: str, lang: str) -> str:
    """Converts text to speech and saves it as an OGG file for Telegram."""
    # gTTS requires specific language codes. This map handles our primary languages.
    # It's easily extensible with more languages supported by gTTS.
    lang_code_map = {"hindi": "hi", "hinglish": "hi", "french": "fr", "spanish": "es"}
    # Default to English ('en') if the detected language isn't in our map.
    lang_code = lang_code_map.get(lang.split()[0], 'en')

    try:
        tts = gTTS(text=text, lang=lang_code, slow=False)
        # Telegram prefers .ogg, but we save as .mp3 and let Telegram handle it.
        # Using a consistent filename is fine for this single-threaded hackathon app.
        audio_file_path = "response.ogg"
        tts.save(audio_file_path)
        return audio_file_path
    except Exception as e:
        logger.error(f"Error in text-to-speech conversion: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    await update.message.reply_text(
        "Hey! I'm NomadAI. Send me a voice message in any language about what you want to do or see in Delhi!"
    )

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles voice messages, processes them, and sends a voice response."""
    # Let the user know the message was received and is being processed.
    await update.message.reply_text("Got it! Let me think for a moment...")

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)

    # Download the voice note to a temporary file.
    audio_file_path = f"{voice.file_id}.ogg"
    await file.download_to_drive(audio_file_path)

    # --- Main Processing Pipeline ---
    # 1. Transcribe (Ears)
    user_query_text = transcribe_voice(audio_file_path)
    if not user_query_text:
        await update.message.reply_text("Sorry, I couldn't understand that. Could you please speak a bit more clearly?")
        os.remove(audio_file_path) # Cleanup
        return

    logger.info(f"Transcribed Text: {user_query_text}")

    # 2. Understand (Brain - Part 1: Language Detection)
    language = detect_language(user_query_text)
    logger.info(f"Detected Language: {language}")

    # 3. Gather Data (Knowledge)
    places_data = get_places_data(user_query_text)

    # 4. Generate Final Prompt (Brain - Part 2: Persona Infusion)
    master_prompt = generate_master_prompt(language, user_query_text, places_data)

    # 5. Get AI Response (Brain - Part 3: Creative Synthesis)
    ai_response_text = get_ai_response(master_prompt)
    logger.info(f"AI Response Text: {ai_response_text}")

    # 6. Convert to Speech (Voice)
    response_audio_path = text_to_speech(ai_response_text, language)
    if not response_audio_path:
        await update.message.reply_text("Sorry, I'm feeling a bit speechless right now. Please try again.")
        os.remove(audio_file_path) # Cleanup
        return

    # 7. Send Response
    await context.bot.send_voice(chat_id=update.effective_chat.id, voice=open(response_audio_path, 'rb'))

    # 8. Cleanup temporary files
    os.remove(audio_file_path)
    os.remove(response_audio_path)

# This part is for setting up the webhook with FastAPI for deployment.
# It allows Telegram to send updates to our web server.
@app.post("/")
async def process_update(request: Request):
    """Processes update from Telegram via webhook."""
    data = await request.json()
    update = Update.de_json(data, bot)

    # Manually dispatching the update to the correct handler.
    # This is a simplified approach for our specific use case.
    if update.message:
        if update.message.text and update.message.text.startswith('/start'):
            await start(update, None) # Context is not needed for this simple handler
        elif update.message.voice:
            # We create a dummy context for the handler to work within the FastAPI environment.
            application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
            dummy_context = ContextTypes.DEFAULT_TYPE(application=application, chat_id=update.effective_chat.id)
            await handle_voice_message(update, dummy_context)

    return {"status": "ok"}

