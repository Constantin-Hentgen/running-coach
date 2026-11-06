import os
import requests
import datetime
import caldav
import anthropic
from fastapi import FastAPI
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

app = FastAPI()

# Configuration via variables d'environnement
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RADICALE_URL = os.getenv("RADICALE_URL")
RADICALE_USER = os.getenv("RADICALE_USER")
RADICALE_PASS = os.getenv("RADICALE_PASS")
DREEVE_MCP_KEY = os.getenv("DREEVE_MCP_KEY")
DREEVE_MCP_URL = os.getenv("DREEVE_MCP_URL", "https://sport.hentgen.ovh/mcp")
CITY_LAT_LON = (48.8566, 2.3522)  # Coordonnées pour la météo (ex: Paris)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ------------------------------------------------------------------
# OUTILS ET INTÉGRATIONS
# ------------------------------------------------------------------

def get_weather_forecast():
    """Récupère la météo et les précipitations pour les 7 prochains jours."""
    try:
        lat, lon = CITY_LAT_LON
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max&timezone=auto"
        res = requests.get(url, timeout=5).json()
        return res.get("daily", {})
    except Exception as e:
        return f"Erreur météo: {str(e)}"

def get_calendar_events(days_ahead=7):
    """Lit les événements du calendrier dédié running sur Radicale."""
    try:
        client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
        calendar = client_cal.calendar(url=RADICALE_URL)
        
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
        return results if results else "Aucune séance prévue dans l'agenda."
    except Exception as e:
        return f"Erreur Radicale: {str(e)}"

def get_dreeve_data():
    """Interroge le serveur MCP Dreeve."""
    try:
        headers = {"Authorization": f"Bearer {DREEVE_MCP_KEY}"}
        res = requests.get(DREEVE_MCP_URL, headers=headers, timeout=5)
        if res.status_code == 200:
            return res.text[:2000]  # Limite pour éviter d'exploser le contexte
        return f"Dreeve indisponible (Code HTTP {res.status_code})"
    except Exception as e:
        return f"Erreur Dreeve MCP: {str(e)}"

# ------------------------------------------------------------------
# PROMPT SYSTEM
# ------------------------------------------------------------------

SYSTEM_PROMPT = """
Tu es un coach sportif personnel expert et un assistant d'organisation.
Tu communiques via Telegram.

CONTRAINTES & PRÉFÉRENCES UTILISATEUR :
1. Télétravail par défaut : Mardi et Jeudi.
2. Jours de course privilégiés : Mardi, Jeudi et le Weekend.
3. Durée maximale en semaine : 1h en porte-à-porte.
4. Prise en compte impérative de la MÉTÉO (éviter pluie forte/vent violent).
5. Évalue les données Dreeve si disponibles. Si Dreeve est indisponible, continue en te basant sur l'agenda et la météo.
6. Consulte l'agenda Radicale pour vérifier les créneaux libres.

RÔLE :
Propose des conseils et un planning optimisé. Format tes réponses avec un Markdown propre et lisible pour Telegram.
"""

# ------------------------------------------------------------------
# BOT TELEGRAM
# ------------------------------------------------------------------

async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return
    
    weather_data = get_weather_forecast()
    calendar_data = get_calendar_events()
    dreeve_data = get_dreeve_data()
    
    prompt_with_context = f"""
Message de l'utilisateur : "{user_text}"

Contexte actuel :
- Météo (7 prochains jours) : {weather_data}
- Agenda Radicale (Running) : {calendar_data}
- Données Dreeve (MCP) : {dreeve_data}
"""

    # Augmentation de max_tokens pour éviter la réponse coupée avec Haiku 5.5
    response = anthropic_client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt_with_context}]
    )

    # Extraction spécifique du bloc de texte
    reply_text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            reply_text = block.text
            break

    if not reply_text:
        reply_text = "Désolé, je n'ai pas pu générer de réponse."

    # Activation de parse_mode=ParseMode.MARKDOWN pour corriger le rendu
    try:
        await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        # Fallback si le Markdown renvoyé par le modèle contient des caractères non échappés
        await update.message.reply_text(reply_text)

telegram_app = None

@app.on_event("startup")
async def startup_event():
    global telegram_app
    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling()
        print("Bot Telegram prêt.")

@app.on_event("shutdown")
async def shutdown_event():
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

@app.get("/health")
def health_check():
    return {"status": "running"}