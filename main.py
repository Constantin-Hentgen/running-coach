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
LAST_KNOWN_ACTIVITY_ID = None

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

    if DREEVE_API_KEY:
        headers = {"Authorization": f"Bearer {DREEVE_API_KEY}", "Accept": "application/json"}
        
        target_status_url = f"{DREEVE_BASE_URL}/api/v1/status"
        try:
            res_status = requests.get(target_status_url, headers=headers, timeout=5)
            logger.info(f"[DREEVE] Réponse Statut — HTTP {res_status.status_code} : {res_status.text}")
        except Exception as e:
            logger.error(f"[DREEVE] Erreur de connexion à {target_status_url} : {e}")

        target_load_url = f"{DREEVE_BASE_URL}/api/v1/metrics/training-load"
        try:
            res_load = requests.get(target_load_url, headers=headers, timeout=5)
            logger.info(f"[DREEVE] Réponse Training Load — HTTP {res_load.status_code} : {res_load.text}")
        except Exception as e:
            logger.error(f"[DREEVE] Erreur training-load : {e}")

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
# OUTILS (LECTURE DREEVE & MÉTÉO / CRUD RADICALE AVEC VERIFICATION)
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
        
        res_load = requests.get(f"{DREEVE_BASE_URL}/api/v1/metrics/training-load", headers=headers, timeout=8)
        if res_load.status_code == 200:
            load_data = res_load.json()
            output_parts.append(
                f"📊 *Charge actuelle :*\n"
                f"- CTL (Forme) : {load_data.get('ctl', 'N/A')}\n"
                f"- ATL (Fatigue) : {load_data.get('atl', 'N/A')}\n"
                f"- TSB (Fraîcheur) : {load_data.get('tsb', {}).get('value', 'N/A')} ({load_data.get('tsb', {}).get('status', '')})"
            )

        res_act = requests.get(
            f"{DREEVE_BASE_URL}/api/v1/activities?filters[sportType]=Run&pagination[size]=100", 
            headers=headers, 
            timeout=8
        )
        
        if res_act.status_code == 200:
            data = res_act.json()
            activities = data.get("activities", [])
            
            if activities:
                def get_dist_km(act):
                    meters = act.get("distanceInMeter")
                    if meters is None:
                        meters = act.get("distance", 0)
                    return float(meters) / 1000.0 if meters else 0.0

                longest_run = max(activities, key=get_dist_km, default=None)
                
                summary_list = []
                for act in activities[:3]:
                    name = act.get("name", "Sortie")
                    date = act.get("startDateLocal", "Date inconnue")[:10]
                    dist = get_dist_km(act)
                    summary_list.append(f"- {date} : {name} ({dist:.2f} km)")
                
                output_parts.append("🏃 *Dernières courses :*\n" + "\n".join(summary_list))
                
                if longest_run:
                    l_dist = get_dist_km(longest_run)
                    if l_dist > 0:
                        l_date = longest_run.get("startDateLocal", "")[:10]
                        l_name = longest_run.get("name", "Course")
                        output_parts.append(f"🏆 *Record de distance (Run le plus long) :* {l_dist:.2f} km le {l_date} ('{l_name}')")

        return "\n\n".join(output_parts) if output_parts else "Aucune donnée Dreeve disponible."
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
            desc = str(component.description.value) if hasattr(component, "description") else ""
            results.append({
                "summary": str(component.summary.value),
                "start": str(component.dtstart.value),
                "end": str(component.dtend.value),
                "description": desc
            })
        return results
    except Exception:
        return []

def get_all_calendar_events(days_ahead=100):
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

def create_calendar_event(summary: str, start_time: str, end_time: str, description: str = None):
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        calendar = client_cal.calendar(url=RADICALE_URL)
        
        dt_start = datetime.datetime.fromisoformat(start_time)
        dt_end = datetime.datetime.fromisoformat(end_time)
        
        # 1. Écriture dans Radicale
        calendar.save_event(dtstart=dt_start, dtend=dt_end, summary=summary, description=description or "")
        
        # 2. Refetch de vérification immédiat pour s'assurer que c'est bien créé
        verify_events = calendar.search(start=dt_start, end=dt_end, event=True)
        created_ok = any(summary.lower() in str(e.vobject_instance.vevent.summary.value).lower() for e in verify_events)
        
        if created_ok:
            return f"Succès vérifié : événement '{summary}' bien enregistré et visible dans Radicale de {start_time} à {end_time}."
        else:
            return f"Attention : la création de '{summary}' a été envoyée mais n'apparaît pas encore au refetch immédiat (latence possible)."
    except Exception as e:
        return f"Erreur création : {str(e)}"

def update_calendar_event(summary_query: str, new_summary: str = None, new_start_time: str = None, new_end_time: str = None, description: str = None):
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        calendar = client_cal.calendar(url=RADICALE_URL)
        events = calendar.search(event=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        
        for e in events:
            comp = e.vobject_instance.vevent
            event_end = comp.dtend.value
            if isinstance(event_end, datetime.date) and not isinstance(event_end, datetime.datetime):
                event_end = datetime.datetime.combine(event_end, datetime.time.max).replace(tzinfo=datetime.timezone.utc)
            elif event_end.tzinfo is None:
                event_end = event_end.replace(tzinfo=datetime.timezone.utc)
            
            if event_end < now:
                continue

            if summary_query.lower() in str(comp.summary.value).lower():
                if new_summary:
                    comp.summary.value = new_summary
                if new_start_time:
                    comp.dtstart.value = datetime.datetime.fromisoformat(new_start_time)
                if new_end_time:
                    comp.dtend.value = datetime.datetime.fromisoformat(new_end_time)
                if description is not None:
                    comp.description = description
                e.save()
                return f"Succès vérifié : séance futur '{summary_query}' mise à jour."
        return f"Aucun événement futur correspondant à '{summary_query}'."
    except Exception as e:
        return f"Erreur mise à jour : {str(e)}"

def delete_calendar_event(summary_query: str = None, start_date: str = None, end_date: str = None):
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        calendar = client_cal.calendar(url=RADICALE_URL)
        now = datetime.datetime.now(datetime.timezone.utc)
        
        if start_date and end_date:
            dt_start = datetime.datetime.fromisoformat(start_date)
            dt_end = datetime.datetime.fromisoformat(end_date)
            events = calendar.search(start=dt_start, end=dt_end, event=True)
            count = 0
            for e in events:
                comp = e.vobject_instance.vevent
                event_end = comp.dtend.value
                if isinstance(event_end, datetime.date) and not isinstance(event_end, datetime.datetime):
                    event_end = datetime.datetime.combine(event_end, datetime.time.max).replace(tzinfo=datetime.timezone.utc)
                elif event_end.tzinfo is None:
                    event_end = event_end.replace(tzinfo=datetime.timezone.utc)
                
                if event_end >= now:
                    e.delete()
                    count += 1
            return f"Succès vérifié : {count} événement(s) futur(s) supprimé(s) du {start_date} au {end_date}."
            
        if summary_query:
            events = calendar.search(event=True)
            count = 0
            for e in events:
                comp = e.vobject_instance.vevent
                event_end = comp.dtend.value
                if isinstance(event_end, datetime.date) and not isinstance(event_end, datetime.datetime):
                    event_end = datetime.datetime.combine(event_end, datetime.time.max).replace(tzinfo=datetime.timezone.utc)
                elif event_end.tzinfo is None:
                    event_end = event_end.replace(tzinfo=datetime.timezone.utc)
                
                if event_end < now:
                    continue

                if summary_query.lower() in str(comp.summary.value).lower():
                    e.delete()
                    count += 1
            return f"Succès vérifié : {count} événement(s) futur(s) supprimé(s)." if count > 0 else f"Aucun événement futur trouvé pour '{summary_query}'."
            
        return "Précise summary_query ou une plage de dates (start_date, end_date)."
    except Exception as e:
        return f"Erreur suppression : {str(e)}"

# ------------------------------------------------------------------
# CONFIGURATION ANTHROPIC (TOOLS & PROMPT)
# ------------------------------------------------------------------

TOOLS = [
    {
        "name": "create_calendar_event",
        "description": "Crée un événement ou un repos dans le calendrier Radicale (futur uniquement). Effectue un refetch de validation automatique.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start_time": {"type": "string", "description": "Format ISO datetime futur."},
                "end_time": {"type": "string", "description": "Format ISO datetime futur."},
                "description": {"type": "string", "description": "Détails optionnels de la séance."}
            },
            "required": ["summary", "start_time", "end_time"]
        }
    },
    {
        "name": "update_calendar_event",
        "description": "Modifie une séance dans l'agenda Radicale par recherche de titre (futur uniquement).",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary_query": {"type": "string"},
                "new_summary": {"type": "string"},
                "new_start_time": {"type": "string"},
                "new_end_time": {"type": "string"},
                "description": {"type": "string"}
            },
            "required": ["summary_query"]
        }
    },
    {
        "name": "delete_calendar_event",
        "description": "Supprime ou wipe uniquement des événements futurs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary_query": {"type": "string"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"}
            }
        }
    }
]

SYSTEM_PROMPT = """
Tu es un coach sportif personnel expert, direct, concis et ultra-factuel.

TRANSPARENCE ET RÉCAPITULATIF DE CRÉATION :
- Quand tu lances des modifications ou des créations d'événements dans le calendrier, informe l'utilisateur que l'action est en cours ou confirmée par le serveur.
- Fais TOUJOURS un petit message de récapitulatif clair après chaque création/modification de planning pour qu'il voie ce qui a été fait (titre, date, détails de la description).

PLANIFICATION GLISSANTE SUR 30 JOURS :
- Maintiens la planification sur les 30 prochains jours.

STYLE & TON SOBRE :
- Sois très sobre, direct et peu verbeux.
- Conseils d'outfit/nutrition uniquement si pertinent (pluie, froid < 8°C, chaleur > 22°C, grosse séance).
- Listes à puces (`-`), gras (`*`), émojis sobres. ZÉRO TABLEAU (pas de `|`).
- Dreeve en lecture seule stricte. Pas de modification du passé.
"""

async def ask_claude_with_tools(user_prompt):
    weather_data = get_weather_forecast()
    calendar_data = get_all_calendar_events(days_ahead=30)
    dreeve_data = get_dreeve_data()
    
    full_prompt = f"""
Message de l'utilisateur : "{user_prompt}"

DONNÉES EN TEMPS RÉEL DU SYSTÈME :
- Prévisions Météo : {weather_data}
- Agendas Radicale (30 prochains jours) : {calendar_data}
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
# JOBS AUTO : DEBRIEF NOUVEAU RUN & BRIEFING MATINAL (30 JOURS)
# ------------------------------------------------------------------

async def check_new_activities_job():
    global LAST_KNOWN_ACTIVITY_ID, telegram_app, TELEGRAM_CHAT_ID
    if not DREEVE_API_KEY or not telegram_app or not TELEGRAM_CHAT_ID:
        return

    headers = {"Authorization": f"Bearer {DREEVE_API_KEY}", "Accept": "application/json"}
    try:
        res = requests.get(f"{DREEVE_BASE_URL}/api/v1/activities?pagination[size]=1", headers=headers, timeout=5)
        if res.status_code == 200:
            activities = res.json().get("activities", [])
            if activities:
                latest = activities[0]
                latest_id = latest.get("id")
                
                if LAST_KNOWN_ACTIVITY_ID is None:
                    LAST_KNOWN_ACTIVITY_ID = latest_id
                    return
                
                if latest_id != LAST_KNOWN_ACTIVITY_ID:
                    LAST_KNOWN_ACTIVITY_ID = latest_id
                    name = latest.get("name", "Sortie")
                    meters = latest.get("distanceInMeter") or latest.get("distance", 0)
                    dist = float(meters) / 1000.0 if meters else 0.0
                    
                    prompt = f"Je viens de terminer et d'uploader une nouvelle séance : '{name}' ({dist:.2f} km). Donne-moi un débrief très court, factuel et concis."
                    reply_text = await ask_claude_with_tools(prompt)
                    await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"⚡ *Nouveau run détecté !*\n\n{reply_text}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Erreur vérification automatique Dreeve : {e}")

async def send_morning_briefing():
    if not telegram_app or not TELEGRAM_CHAT_ID:
        return
    prompt = (
        "Réalise le briefing matinal. Analyse l'agenda Radicale existant et la météo disponible. "
        "Met à jour / planifie les 30 prochains jours dans Radicale en créant/ajustant les événements futurs. "
        "Fais un résumé Telegram ultra-concis, sobre, axé sur aujourd'hui et les grandes lignes du mois."
    )
    reply_text = await ask_claude_with_tools(prompt)
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
        scheduler.add_job(check_new_activities_job, 'interval', minutes=15)
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