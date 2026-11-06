import os
import requests
import datetime
import traceback
import caldav
import anthropic
from fastapi import FastAPI
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.background import BackgroundScheduler

from mcp import ClientSession
from mcp.client.sse import sse_client

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
# OUTILS
# ------------------------------------------------------------------

def get_weather_forecast():
    try:
        lat, lon = CITY_LAT_LON
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max&timezone=auto"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return res.json().get("daily", {})
        return {}
    except Exception as e:
        print(f"[ERROR] Météo: {e}")
        return {}

async def get_dreeve_data():
    """
    Connexion asynchrone native au serveur MCP Dreeve via SSE client.
    """
    if not DREEVE_MCP_URL or not DREEVE_MCP_KEY:
        print("[WARN] Dreeve MCP non configuré.")
        return "Dreeve non configuré."

    headers = {"Authorization": f"Bearer {DREEVE_MCP_KEY}"}

    try:
        print(f"[DEBUG] Tentative de connexion MCP à {DREEVE_MCP_URL}...")
        async with sse_client(DREEVE_MCP_URL, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                resources = await session.list_resources()
                if resources.resources:
                    res_content = await session.read_resource(resources.resources[0].uri)
                    print("[DEBUG] Données MCP Dreeve récupérées avec succès.")
                    return f"Données Dreeve (MCP) : {res_content}"
                
                tools = await session.list_tools()
                print("[DEBUG] Outils MCP Dreeve récupérés avec succès.")
                return f"Outils Dreeve disponibles : {[t.name for t in tools.tools]}"
    except Exception as e:
        print(f"[ERROR] Échec connexion Dreeve MCP: {e}")
        traceback.print_exc()
        return f"Dreeve MCP injoignable ({str(e)})"

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
        return f"Erreur Radicale : {str(e)}"

def create_calendar_event(summary: str, start_time: str, end_time: str):
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        calendar = client_cal.calendar(url=RADICALE_URL)
        dt_start = datetime.datetime.fromisoformat(start_time)
        dt_end = datetime.datetime.fromisoformat(end_time)
        calendar.save_event(dtstart=dt_start, dtend=dt_end, summary=summary)
        return f"Succès : séance '{summary}' créée."
    except Exception as e:
        return f"Erreur création : {str(e)}"

def update_calendar_event(summary_query: str, new_summary: str = None, new_start_time: str = None, new_end_time: str = None):
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        calendar = client_cal.calendar(url=RADICALE_URL)
        events = calendar.search(event=True)
        for e in events:
            comp = e.vobject_instance.vevent
            if summary_query.lower() in str(comp.summary.value).lower():
                if new_summary:
                    comp.summary.value = new_summary
                if new_start_time:
                    comp.dtstart.value = datetime.datetime.fromisoformat(new_start_time)
                if new_end_time:
                    comp.dtend.value = datetime.datetime.fromisoformat(new_end_time)
                e.save()
                return f"Succès : séance '{summary_query}' mise à jour."
        return f"Aucun événement correspondant à '{summary_query}'."
    except Exception as e:
        return f"Erreur mise à jour : {str(e)}"

def delete_calendar_event(summary_query: str):
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        calendar = client_cal.calendar(url=RADICALE_URL)
        events = calendar.search(event=True)
        count = 0
        for e in events:
            comp = e.vobject_instance.vevent
            if summary_query.lower() in str(comp.summary.value).lower():
                e.delete()
                count += 1
        return f"Succès : {count} événement(s) supprimé(s)." if count > 0 else f"Aucun événement trouvé pour '{summary_query}'."
    except Exception as e:
        return f"Erreur suppression : {str(e)}"

# ------------------------------------------------------------------
# CONFIGURATION ANTHROPIC
# ------------------------------------------------------------------

TOOLS = [
    {
        "name": "create_calendar_event",
        "description": "Crée une séance de course dans le calendrier Radicale principal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"}
            },
            "required": ["summary", "start_time", "end_time"]
        }
    },
    {
        "name": "update_calendar_event",
        "description": "Modifie une séance dans l'agenda Radicale par recherche de titre.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary_query": {"type": "string"},
                "new_summary": {"type": "string"},
                "new_start_time": {"type": "string"},
                "new_end_time": {"type": "string"}
            },
            "required": ["summary_query"]
        }
    },
    {
        "name": "delete_calendar_event",
        "description": "Supprime une séance du calendrier Radicale.",
        "input_schema": {
            "type": "object",
            "properties": {"summary_query": {"type": "string"}},
            "required": ["summary_query"]
        }
    }
]

SYSTEM_PROMPT = """
Tu es un coach sportif personnel expert, direct, concis et ultra-factuel.
Règles strictes de formatage pour Telegram :
1. INTERDICTION FORMELLE D'UTILISER DES TABLEAUX (pas de caractère `|`). Utilise exclusivement des listes à puces (`-`).
2. Utilise un Markdown simple et propre (gras avec `*`).
3. Sois court, percutant et ne coupe jamais tes phrases.
"""

async def ask_claude_with_tools(user_prompt):
    weather_data = get_weather_forecast()
    calendar_data = get_all_calendar_events()
    dreeve_data = await get_dreeve_data()
    
    full_prompt = f"""
Message de l'utilisateur : "{user_prompt}"

DONNÉES EN TEMPS RÉEL DU SYSTÈME :
- Prévisions Météo : {weather_data}
- Agendas Radicale : {calendar_data}
- Données Dreeve (MCP) : {dreeve_data}
"""

    messages = [{"role": "user", "content": full_prompt}]
    
    response = anthropic_client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=2048,
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
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": tool_result
                    }]
                })
        
        final_response = anthropic_client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=2048,
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

    return reply_text if reply_text else "Opération terminée."

# ------------------------------------------------------------------
# TELEGRAM & FASTAPI
# ------------------------------------------------------------------

async def send_morning_briefing():
    if not telegram_app or not TELEGRAM_CHAT_ID:
        return
    reply_text = await ask_claude_with_tools("Fais un briefing matinal ultra-concis pour aujourd'hui.")
    await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=reply_text, parse_mode=ParseMode.MARKDOWN)

async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return
    
    reply_text = await ask_claude_with_tools(user_text)

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