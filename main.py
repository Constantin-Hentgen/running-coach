import os
import requests
import datetime
import caldav
import anthropic
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

app = FastAPI()

# Configuration depuis les variables d'environnement
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RADICALE_URL = os.getenv("RADICALE_URL")  # ex: http://radicale:5232/user/running/
RADICALE_USER = os.getenv("RADICALE_USER")
RADICALE_PASS = os.getenv("RADICALE_PASS")
DREEVE_MCP_KEY = os.getenv("DREEVE_MCP_KEY")
DREEVE_API_URL = os.getenv("DREEVE_API_URL", "https://sport.hentgen.ovh/api/v1")
CITY_LAT_LON = (48.8566, 2.3522)  # Vos coordonnées (ex: Paris)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ------------------------------------------------------------------
# OUTILS DE RÉCUPÉRATION DE DONNÉES
# ------------------------------------------------------------------

def get_weather_forecast():
    """Récupère la météo et les précipitations pour les 7 prochains jours (Open-Meteo)."""
    lat, lon = CITY_LAT_LON
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max&timezone=auto"
    res = requests.get(url).json()
    return res.get("daily", {})

def get_calendar_events(days_ahead=7):
    """Lit les événements du calendrier dédié running sur Radicale."""
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        principal = client_cal.principal()
        calendars = principal.calendars()
        if not calendars:
            return "Aucun calendrier trouvé sur Radicale."
        
        calendar = calendars[0]
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
    except Exception as e:
        return f"Erreur de lecture Radicale: {str(e)}"

def get_dreeve_activities():
    """Récupère les dernières activités sportives via l'API Dreeve."""
    try:
        headers = {"Authorization": f"Bearer {DREEVE_MCP_KEY}"}
        res = requests.get(f"{DREEVE_API_URL}/activities", headers=headers)
        if res.status_code == 200:
            return res.json()[:3]  # Les 3 dernières activités
        return f"Erreur Dreeve (Code {res.status_code})"
    except Exception as e:
        return f"Erreur de connexion Dreeve: {str(e)}"

# ------------------------------------------------------------------
# SYSTEM PROMPT DU COACH
# ------------------------------------------------------------------

SYSTEM_PROMPT = """
Tu es un coach sportif personnel expert et un assistant d'organisation.
Tu communiques via Telegram en langage naturel.

CONTRAINTES & PRÉFÉRENCES UTILISATEUR :
1. Télétravail par défaut : Mardi et Jeudi.
2. Jours de course privilégiés : Mardi, Jeudi et le Weekend.
3. Durée maximale en semaine : 1h en porte-à-porte (créneau de pause déjeuner ou fin de journée).
4. Prise en compte impérative de la MÉTÉO (éviter la pluie forte ou les températures extrêmes).
5. Utilise les données d'entraînement Dreeve pour évaluer la charge et la fatigue.
6. Consulte l'agenda Radicale dédié au running pour vérifier les créneaux libres.

RÔLE :
Analyse les requêtes de l'utilisateur, croise les données météo, l'agenda et l'historique Dreeve pour lui donner des conseils de coaching pertinents ou l'aider à planifier ses séances.
"""

# ------------------------------------------------------------------
# GESTION DU BOT TELEGRAM (Mode Pooling pour simplifier)
# ------------------------------------------------------------------

async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return
    
    # Collecte des données contextuelles en direct
    weather_data = get_weather_forecast()
    calendar_data = get_calendar_events()
    dreeve_data = get_dreeve_activities()
    
    prompt_with_context = f"""
Message de l'utilisateur : "{user_text}"

Contexte actuel :
- Météo (7 prochains jours) : {weather_data}
- Agenda Radicale (Running) : {calendar_data}
- Dernières activités Dreeve : {dreeve_data}
"""

    # Appel à Claude 3.5 Haiku
    response = anthropic_client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": prompt_with_context}
        ]
    )

    reply_text = response.content[0].text
    await update.message.reply_text(reply_text)

# Initialisation du bot Telegram au démarrage de l'app FastAPI
telegram_app = None

@app.on_event("startup")
async def startup_event():
    global telegram_app
    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
        await telegram_app.initialize()
        await telegram_app.start()
        # Lancement du polling en arrière-plan
        await telegram_app.updater.start_polling()
        print("Bot Telegram démarré en arrière-plan sur FastAPI.")

@app.on_event("shutdown")
async def shutdown_event():
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

@app.get("/health")
def health_check():
    return {"status": "running", "service": "running-coach-autonomous-agent"}
