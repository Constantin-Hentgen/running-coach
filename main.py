import os
import requests
import datetime
import caldav
import anthropic
from fastapi import FastAPI
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI()

# Configuration
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # Votre ID de chat privé pour le push de 8h
RADICALE_URL = os.getenv("RADICALE_URL")          # Agenda principal (running / écriture)
RADICALE_READONLY_URLS = os.getenv("RADICALE_READONLY_URLS", "") # Autres agendas séparés par des virgules
RADICALE_USER = os.getenv("RADICALE_USER")
RADICALE_PASS = os.getenv("RADICALE_PASS")
DREEVE_MCP_KEY = os.getenv("DREEVE_MCP_KEY")
DREEVE_MCP_URL = os.getenv("DREEVE_MCP_URL", "https://sport.hentgen.ovh/mcp")
CITY_LAT_LON = (48.8566, 2.3522)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
telegram_app = None

# ------------------------------------------------------------------
# OUTILS
# ------------------------------------------------------------------

def get_weather_forecast():
    try:
        lat, lon = CITY_LAT_LON
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max&timezone=auto"
        res = requests.get(url, timeout=5).json()
        return res.get("daily", {})
    except Exception as e:
        return f"Erreur météo: {str(e)}"

def fetch_events_from_calendar(client_cal, url, days_ahead):
    try:
        calendar = client_cal.calendar(url=url.strip())
        now = datetime.datetime.now(datetime.timezone.utc)
        end = now + datetime.timedelta(days=days_ahead)
        events = calendar.search(start=now, end=end, event=True)
        results = []
        for e in events:
            component = e.vobject_instance.vevent
            results.append({
                "summary": str(component.summary.value),
                "start": str(component.dtstart.value),
                "end": str(component.dtend.value)
            })
        return results
    except Exception:
        return []

def get_all_calendar_events(days_ahead=7):
    """Agrège l'agenda principal et les agendas read-only."""
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        all_events = []
        
        # Agenda principal
        if RADICALE_URL:
            all_events.extend(fetch_events_from_calendar(client_cal, RADICALE_URL, days_ahead))
            
        # Agendas read-only supplémentaires
        if RADICALE_READONLY_URLS:
            for ro_url in RADICALE_READONLY_URLS.split(","):
                if ro_url.strip():
                    all_events.extend(fetch_events_from_calendar(client_cal, ro_url, days_ahead))
                    
        return all_events if all_events else "Aucun événement trouvé."
    except Exception as e:
        return f"Erreur Radicale: {str(e)}"

def get_dreeve_data():
    try:
        headers = {"Authorization": f"Bearer {DREEVE_MCP_KEY}"}
        res = requests.get(DREEVE_MCP_URL, headers=headers, timeout=5)
        if res.status_code == 200:
            return res.text[:1500]
        return "Dreeve indisponible."
    except Exception:
        return "Dreeve indisponible."

# ------------------------------------------------------------------
# SYSTEM PROMPT (Concis & Markdown propre)
# ------------------------------------------------------------------

SYSTEM_PROMPT = """
Tu es un coach sportif expert, direct, concis et factuel. 
Tu communiques via Telegram en utilisant STRICTEMENT un format Markdown propre et valide (utilise `*` pour le gras, évite les caractères spéciaux non échappés comme les _ isolés).

Règles de style :
- Va droit au but, pas de fioritures.
- Fais des réponses courtes et percutantes.
- Télétravail : Mardi et Jeudi. Jours de course : Mardi, Jeudi, Weekend (max 1h en semaine).
- Analyse la météo et croise l'agenda (principal + read-only) pour caler les séances.
"""

# ------------------------------------------------------------------
# TÂCHE PLANIFIÉE (8H00)
# ------------------------------------------------------------------

async def send_morning_briefing():
    """Génère et envoie le checkup quotidien à 8h sur Telegram."""
    if not telegram_app or not TELEGRAM_CHAT_ID:
        return

    weather = get_weather_forecast()
    agenda = get_all_calendar_events(days_ahead=3)
    dreeve = get_dreeve_data()

    prompt = f"""
Fais un briefing matinal ultra-concis (checkup du jour) basé sur :
- Météo du jour : {weather}
- Agenda (prochains jours) : {agenda}
- Forme / Dreeve : {dreeve}
Rappelle si une séance est prévue aujourd'hui selon les contraintes.
"""

    response = anthropic_client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )

    reply_text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            reply_text = block.text
            break

    if reply_text:
        await telegram_app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=reply_text,
            parse_mode=ParseMode.MARKDOWN
        )

# ------------------------------------------------------------------
# BOT TELEGRAM (Handler)
# ------------------------------------------------------------------

async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return
    
    weather_data = get_weather_forecast()
    calendar_data = get_all_calendar_events()
    dreeve_data = get_dreeve_data()
    
    prompt = f"""
Requête utilisateur : "{user_text}"
- Météo : {weather_data}
- Agendas : {calendar_data}
- Dreeve : {dreeve_data}
"""

    response = anthropic_client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )

    reply_text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            reply_text = block.text
            break

    if not reply_text:
        reply_text = "Pas de réponse générée."

    try:
        await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        # Fallback texte brut si le markdown pose un souci de syntaxe
        await update.message.reply_text(reply_text)

# ------------------------------------------------------------------
# FASTAP & SCHEDULER STARTUP
# ------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    global telegram_app
    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling()

        # Planificateur pour le checkup de 8h00 chaque jour
        scheduler = BackgroundScheduler()
        scheduler.add_job(send_morning_briefing, 'cron', hour=8, minute=0)
        scheduler.start()
        print("Bot démarré et planificateur 8h actif.")

@app.on_event("shutdown")
async def shutdown_event():
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

@app.get("/health")
def health_check():
    return {"status": "running"}