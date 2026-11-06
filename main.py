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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RADICALE_URL = os.getenv("RADICALE_URL")
RADICALE_READONLY_URLS = os.getenv("RADICALE_READONLY_URLS", "")
RADICALE_USER = os.getenv("RADICALE_USER")
RADICALE_PASS = os.getenv("RADICALE_PASS")
DREEVE_MCP_KEY = os.getenv("DREEVE_MCP_KEY")
DREEVE_MCP_URL = os.getenv("DREEVE_MCP_URL", "https://sport.hentgen.ovh/mcp")
CITY_LAT_LON = (48.8566, 2.3522)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
telegram_app = None

# ------------------------------------------------------------------
# OUTILS CALDAV (Lecture, Création, Modification, Suppression)
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
                "uid": str(component.uid.value) if hasattr(component, "uid") else "",
                "summary": str(component.summary.value),
                "start": str(component.dtstart.value),
                "end": str(component.dtend.value)
            })
        return results
    except Exception:
        return []

def get_all_calendar_events(days_ahead=7):
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        all_events = []
        if RADICALE_URL:
            all_events.extend(fetch_events_from_calendar(client_cal, RADICALE_URL, days_ahead))
        if RADICALE_READONLY_URLS:
            for ro_url in RADICALE_READONLY_URLS.split(","):
                if ro_url.strip():
                    all_events.extend(fetch_events_from_calendar(client_cal, ro_url, days_ahead))
        return all_events if all_events else "Aucun événement trouvé."
    except Exception as e:
        return f"Erreur Radicale: {str(e)}"

def create_calendar_event(summary: str, start_time: str, end_time: str):
    """Crée un événement dans le calendrier Radicale principal."""
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        calendar = client_cal.calendar(url=RADICALE_URL)
        
        dt_start = datetime.datetime.fromisoformat(start_time)
        dt_end = datetime.datetime.fromisoformat(end_time)
        
        calendar.save_event(dtstart=dt_start, dtend=dt_end, summary=summary)
        return f"Succès : l'événement '{summary}' a été créé."
    except Exception as e:
        return f"Erreur lors de la création : {str(e)}"

def update_calendar_event(summary_query: str, new_summary: str = None, new_start_time: str = None, new_end_time: str = None):
    """Modifie un événement existant en cherchant par son titre (summary_query)."""
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        calendar = client_cal.calendar(url=RADICALE_URL)
        
        events = calendar.search(event=True)
        target_event = None
        for e in events:
            comp = e.vobject_instance.vevent
            if summary_query.lower() in str(comp.summary.value).lower():
                target_event = e
                break
                
        if not target_event:
            return f"Aucun événement trouvé correspondant à '{summary_query}'."
            
        comp = target_event.vobject_instance.vevent
        if new_summary:
            comp.summary.value = new_summary
        if new_start_time:
            comp.dtstart.value = datetime.datetime.fromisoformat(new_start_time)
        if new_end_time:
            comp.dtend.value = datetime.datetime.fromisoformat(new_end_time)
            
        target_event.save()
        return f"Succès : l'événement '{summary_query}' a été mis à jour."
    except Exception as e:
        return f"Erreur lors de la modification : {str(e)}"

def delete_calendar_event(summary_query: str):
    """Supprime un événement du calendrier en cherchant par son titre."""
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        calendar = client_cal.calendar(url=RADICALE_URL)
        
        events = calendar.search(event=True)
        deleted_count = 0
        for e in events:
            comp = e.vobject_instance.vevent
            if summary_query.lower() in str(comp.summary.value).lower():
                e.delete()
                deleted_count += 1
                
        if deleted_count > 0:
            return f"Succès : {deleted_count} événement(s) correspondant à '{summary_query}' ont été supprimés."
        return f"Aucun événement trouvé à supprimer pour '{summary_query}'."
    except Exception as e:
        return f"Erreur lors de la suppression : {str(e)}"

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
# DÉFINITION DES OUTILS POUR ANTHROPIC (Création, Édition, Suppression)
# ------------------------------------------------------------------

TOOLS = [
    {
        "name": "create_calendar_event",
        "description": "Crée une nouvelle séance dans le calendrier Radicale principal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Titre de la séance"},
                "start_time": {"type": "string", "description": "Début au format ISO 8601 (ex: 2026-11-09T18:00:00)"},
                "end_time": {"type": "string", "description": "Fin au format ISO 8601 (ex: 2026-11-09T19:00:00)"}
            },
            "required": ["summary", "start_time", "end_time"]
        }
    },
    {
        "name": "update_calendar_event",
        "description": "Modifie un événement existant dans le calendrier Radicale en recherchant par son titre actuel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary_query": {"type": "string", "description": "Le texte ou titre pour retrouver l'événement à modifier"},
                "new_summary": {"type": "string", "description": "Nouveau titre (optionnel)"},
                "new_start_time": {"type": "string", "description": "Nouvelle date/heure de début ISO 8601 (optionnel)"},
                "new_end_time": {"type": "string", "description": "Nouvelle date/heure de fin ISO 8601 (optionnel)"}
            },
            "required": ["summary_query"]
        }
    },
    {
        "name": "delete_calendar_event",
        "description": "Supprime un ou plusieurs événements du calendrier Radicale en cherchant par leur titre.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary_query": {"type": "string", "description": "Le titre ou mot-clé de l'événement à supprimer"}
            },
            "required": ["summary_query"]
        }
    }
]

SYSTEM_PROMPT = """
Tu es un coach sportif expert, direct, concis et factuel. 
Tu communiques via Telegram en utilisant STRICTEMENT un format Markdown propre et valide.
Tu as la capacité totale de gérer l'agenda Radicale de l'utilisateur : tu peux créer, modifier ou supprimer des séances selon ses demandes.
"""

# ------------------------------------------------------------------
# BOUCLE D'EXÉCUTION DES OUTILS
# ------------------------------------------------------------------

def ask_claude_with_tools(user_prompt):
    weather_data = get_weather_forecast()
    calendar_data = get_all_calendar_events()
    dreeve_data = get_dreeve_data()
    
    full_prompt = f"""
Requête utilisateur : "{user_prompt}"
- Météo : {weather_data}
- Agendas : {calendar_data}
- Dreeve : {dreeve_data}
"""

    messages = [{"role": "user", "content": full_prompt}]
    
    response = anthropic_client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages
    )

    if response.stop_reason == "tool_use":
        messages.append({"role": "assistant", "content": response.content})
        
        for block in response.content:
            if block.type == "tool_use":
                tool_name = block.name
                tool_args = block.input
                tool_use_id = block.id
                
                tool_result = ""
                if tool_name == "create_calendar_event":
                    tool_result = create_calendar_event(**tool_args)
                elif tool_name == "update_calendar_event":
                    tool_result = update_calendar_event(**tool_args)
                elif tool_name == "delete_calendar_event":
                    tool_result = delete_calendar_event(**tool_args)
                
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": tool_result
                        }
                    ]
                })
        
        final_response = anthropic_client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )
        response = final_response

    reply_text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            reply_text = block.text
            break

    return reply_text if reply_text else "Action effectuée."

# ------------------------------------------------------------------
# TÂCHE PLANIFIÉE & TELEGRAM
# ------------------------------------------------------------------

async def send_morning_briefing():
    if not telegram_app or not TELEGRAM_CHAT_ID:
        return
    reply_text = ask_claude_with_tools("Fais un briefing matinal ultra-concis (checkup du jour).")
    await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=reply_text, parse_mode=ParseMode.MARKDOWN)

async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return
    
    reply_text = ask_claude_with_tools(user_text)

    try:
        await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text(reply_text)

@app.on_event("startup")
async def startup_event():
    global telegram_app
    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling()

        scheduler = BackgroundScheduler()
        scheduler.add_job(send_morning_briefing, 'cron', hour=8, minute=0)
        scheduler.start()

@app.on_event("shutdown")
async def shutdown_event():
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

@app.get("/health")
def health_check():
    return {"status": "running"}