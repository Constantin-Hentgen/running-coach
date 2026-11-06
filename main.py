import os
import logging
import requests
import datetime
import caldav
import anthropic
from fastapi import FastAPI
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.background import BackgroundScheduler

# Configuration des logs en mode INFO visible dans les logs Docker
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("RunningCoachAgent")

app = FastAPI()

# Configuration des variables d'environnement
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RADICALE_URL = os.getenv("RADICALE_URL")
RADICALE_READONLY_URLS = os.getenv("RADICALE_READONLY_URLS", "")
RADICALE_USER = os.getenv("RADICALE_USER")
RADICALE_PASS = os.getenv("RADICALE_PASS")
DREEVE_API_KEY = os.getenv("DREEVE_API_KEY")

# Nettoyage automatique de l'URL de base Dreeve
raw_dreeve_url = os.getenv("DREEVE_API_URL", "https://sport.hentgen.ovh")
DREEVE_BASE_URL = raw_dreeve_url.split("/mcp")[0].split("/api")[0].rstrip("/")

CITY_LAT_LON = (48.8566, 2.3522)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
telegram_app = None

# ------------------------------------------------------------------
# TEST DE CONNECTIVITÉ LOGUÉ AU DÉMARRAGE
# ------------------------------------------------------------------

def verify_services_connectivity():
    logger.info("=== LANCE LE TEST DE CONNECTIVITÉ DES SERVICES ===")
    
    if not DREEVE_API_KEY:
        logger.error("[DREEVE] DREEVE_API_KEY est VIDE ou non définie !")
    else:
        masked_key = f"{DREEVE_API_KEY[:6]}...{DREEVE_API_KEY[-4:]}" if len(DREEVE_API_KEY) > 10 else "KEY_TOO_SHORT"
        logger.info(f"[DREEVE] Clé détectée (masquée) : {masked_key}")

    # 1. Test Dreeve (Status + Training Load)
    if DREEVE_API_KEY:
        headers = {"Authorization": f"Bearer {DREEVE_API_KEY}", "Accept": "application/json"}
        
        target_status_url = f"{DREEVE_BASE_URL}/api/v1/status"
        logger.info(f"[DREEVE] Test URL Statut : {target_status_url}")
        try:
            res_status = requests.get(target_status_url, headers=headers, timeout=5)
            logger.info(f"[DREEVE] Réponse Statut — HTTP {res_status.status_code} : {res_status.text}")
        except Exception as e:
            logger.error(f"[DREEVE] Erreur de connexion à {target_status_url} : {e}")

        target_load_url = f"{DREEVE_BASE_URL}/api/v1/metrics/training-load"
        logger.info(f"[DREEVE] Test URL Training Load : {target_load_url}")
        try:
            res_load = requests.get(target_load_url, headers=headers, timeout=5)
            logger.info(f"[DREEVE] Réponse Training Load — HTTP {res_load.status_code} : {res_load.text}")
        except Exception as e:
            logger.error(f"[DREEVE] Erreur training-load : {e}")

    # 2. Test Météo (Open-Meteo)
    try:
        lat, lon = CITY_LAT_LON
        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&timezone=auto"
        res_weather = requests.get(weather_url, timeout=5)
        if res_weather.status_code == 200:
            logger.info("[METEO] Open-Meteo OK : Prévisions accessibles.")
        else:
            logger.warning(f"[METEO] Code HTTP inattendu : {res_weather.status_code}")
    except Exception as e:
        logger.error(f"[METEO] Erreur Open-Meteo : {e}")

    # 3. Test Radicale / CalDAV
    if not RADICALE_URL:
        logger.warning("[RADICALE] RADICALE_URL n'est pas définie.")
    else:
        try:
            client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
            client_cal.principal()
            logger.info("[RADICALE] CalDAV OK : Connexion à l'agenda établie.")
        except Exception as e:
            logger.error(f"[RADICALE] Échec de connexion au calendrier CalDAV : {e}")

    logger.info("=== FIN DU TEST DE CONNECTIVITÉ ===")

# ------------------------------------------------------------------
# OUTILS (LECTURE DREEVE & MÉTÉO / CRUD RADICALE)
# ------------------------------------------------------------------

def get_weather_forecast():
    try:
        lat, lon = CITY_LAT_LON
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max&timezone=auto"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return res.json().get("daily", {})
        return {}
    except Exception:
        return {}

def get_dreeve_data():
    if not DREEVE_API_KEY:
        return "Clé Dreeve non configurée."

    headers = {
        "Authorization": f"Bearer {DREEVE_API_KEY}",
        "Accept": "application/json"
    }

    try:
        output_parts = []
        
        # 1. Métriques de charge
        res_load = requests.get(f"{DREEVE_BASE_URL}/api/v1/metrics/training-load", headers=headers, timeout=8)
        if res_load.status_code == 200:
            load_data = res_load.json()
            output_parts.append(
                f"📊 *Charge actuelle :*\n"
                f"- CTL (Forme) : {load_data.get('ctl', 'N/A')}\n"
                f"- ATL (Fatigue) : {load_data.get('atl', 'N/A')}\n"
                f"- TSB (Fraîcheur) : {load_data.get('tsb', {}).get('value', 'N/A')} ({load_data.get('tsb', {}).get('status', '')})"
            )

        # 2. Récupération des courses à pied
        res_act = requests.get(
            f"{DREEVE_BASE_URL}/api/v1/activities?filters[sportType]=Run&pagination[size]=100", 
            headers=headers, 
            timeout=8
        )
        
        if res_act.status_code == 200:
            data = res_act.json()
            activities = data.get("activities", [])
            
            if activities:
                longest_run = max(activities, key=lambda x: x.get("distance", 0), default=None)
                
                summary_list = []
                for act in activities[:3]:
                    name = act.get("name", "Sortie")
                    date = act.get("startDateLocal", "Date inconnue")[:10]
                    dist = act.get("distance", 0) / 1000
                    summary_list.append(f"- {date} : {name} ({dist:.2f} km)")
                
                output_parts.append("🏃 *Dernières courses :*\n" + "\n".join(summary_list))
                
                if longest_run and longest_run.get("distance", 0) > 0:
                    l_dist = longest_run.get("distance", 0) / 1000
                    l_date = longest_run.get("startDateLocal", "")[:10]
                    l_name = longest_run.get("name", "Course")
                    output_parts.append(f"🏆 *Record de distance (Run le plus long) :* {l_dist:.2f} km le {l_date} ('{l_name}')")

        return "\n\n".join(output_parts) if output_parts else "Aucuna donnée Dreeve disponible."
    except Exception as e:
        logger.error(f"Erreur lors de la récupération des données Dreeve : {e}")
        return f"Erreur requête API Dreeve : {str(e)}"

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
        
        # Gestion intelligente : si les formats sont YYYY-MM-DD, on crée un événement de journée entière (All-day)
        if len(start_time) == 10 and len(end_time) == 10 and "T" not in start_time:
            dt_start = datetime.date.fromisoformat(start_time)
            dt_end = datetime.date.fromisoformat(end_time)
            if dt_end <= dt_start:
                dt_end = dt_start + datetime.timedelta(days=1)
            calendar.save_event(dtstart=dt_start, dtend=dt_end, summary=summary)
        else:
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
                    if len(new_start_time) == 10 and "T" not in new_start_time:
                        comp.dtstart.value = datetime.date.fromisoformat(new_start_time)
                    else:
                        comp.dtstart.value = datetime.datetime.fromisoformat(new_start_time)
                if new_end_time:
                    if len(new_end_time) == 10 and "T" not in new_end_time:
                        comp.dtend.value = datetime.date.fromisoformat(new_end_time)
                    else:
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
# CONFIGURATION ANTHROPIC (TOOLS & PROMPT)
# ------------------------------------------------------------------

TOOLS = [
    {
        "name": "create_calendar_event",
        "description": "Crée une séance ou un repos dans le calendrier Radicale. Pour une journée entière (ex: Repos), utilise strictement le format 'YYYY-MM-DD' pour start_time et end_time (ex: start='2026-11-23', end='2026-11-23'). Pour une séance avec horaire précis, utilise le format ISO datetime.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start_time": {"type": "string", "description": "Format 'YYYY-MM-DD' pour une journée entière ou ISO datetime."},
                "end_time": {"type": "string", "description": "Format 'YYYY-MM-DD' pour une journée entière ou ISO datetime."}
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
Règles strictes :
1. INTERDICTION FORMELLE D'UTILISER DES TABLEAUX (pas de caractère `|`). Utilise exclusivement des listes à puces (`-`).
2. Utilise un Markdown simple et propre (gras avec `*`).
3. Utilise des émojis pertinents et dynamiques (ex: ☀️, 🌧️, ❄️, 🏃‍♂️, 👟, ⚠️, 🏆, 🛌) pour la météo, le statut et les séances.
4. Pour les jours de repos ou événements sur toute la journée, utilise TOUJOURS le format de date simple 'YYYY-MM-DD' (ex: start='2026-11-23', end='2026-11-23') afin qu'ils s'affichent proprement en journée entière dans le calendrier sans chevaucher les horaires.
5. Dreeve est en lecture seule stricte : tu consultes la charge, l'historique et les records, mais tu n'écris jamais dedans. Tu gères l'écriture/modification/suppression uniquement dans l'agenda Radicale.
6. Sois court, percutant et ne coupe jamais tes phrases.
"""

async def ask_claude_with_tools(user_prompt):
    weather_data = get_weather_forecast()
    calendar_data = get_all_calendar_events()
    dreeve_data = get_dreeve_data()
    
    full_prompt = f"""
Message de l'utilisateur : "{user_prompt}"

DONNÉES EN TEMPS RÉEL DU SYSTÈME :
- Prévisions Météo : {weather_data}
- Agendas Radicale : {calendar_data}
- Données Dreeve & Charge (Lecture seule) : {dreeve_data}
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
    
    verify_services_connectivity()

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