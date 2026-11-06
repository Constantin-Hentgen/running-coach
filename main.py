import os
import re
import json
import uuid
import asyncio
import logging
import datetime
import functools
from collections import defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests
import caldav
import anthropic
from caldav.lib.error import NotFoundError
from icalendar import Calendar, Event
from fastapi import FastAPI
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("RunningCoachAgent")

app = FastAPI()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RADICALE_URL = os.getenv("RADICALE_URL")
RADICALE_USER = os.getenv("RADICALE_USER")
RADICALE_PASS = os.getenv("RADICALE_PASS")
DREEVE_API_KEY = os.getenv("DREEVE_API_KEY")

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
TZ = ZoneInfo("Europe/Paris")
UTC = datetime.timezone.utc

raw_dreeve_url = os.getenv("DREEVE_API_URL", "https://sport.hentgen.ovh")
DREEVE_BASE_URL = raw_dreeve_url.split("/mcp")[0].split("/api")[0].rstrip("/")

CITY_LAT_LON = (48.8566, 2.3522)

anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
telegram_app = None
LAST_KNOWN_ACTIVITY_ID = None

USER_CONVERSATION_HISTORY = defaultdict(list)
MAX_HISTORY_MESSAGES = 6
MAX_TOOL_ROUNDS = 25          # garde-fou contre les boucles infinies
CONTEXT_DAYS_AHEAD = 45
AGENT_LOCK = asyncio.Lock()   # une seule exécution d'agent à la fois (évite les écritures concurrentes)

FR_DAYS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
FR_DAYS_LONG = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


# ---------------------------------------------------------------------------
# Connectivité (au démarrage)
# ---------------------------------------------------------------------------

def verify_services_connectivity():
    logger.info("=== TEST DE CONNECTIVITÉ DES SERVICES ===")

    if not DREEVE_API_KEY:
        logger.error("[DREEVE] DREEVE_API_KEY est VIDE ou non définie !")
    else:
        masked_key = f"{DREEVE_API_KEY[:6]}...{DREEVE_API_KEY[-4:]}" if len(DREEVE_API_KEY) > 10 else "KEY_TOO_SHORT"
        logger.info(f"[DREEVE] Clé détectée (masquée) : {masked_key}")
        headers = {"Authorization": f"Bearer {DREEVE_API_KEY}", "Accept": "application/json"}
        for label, path in (("Statut", "/api/v1/status"), ("Training Load", "/api/v1/metrics/training-load")):
            try:
                res = requests.get(f"{DREEVE_BASE_URL}{path}", headers=headers, timeout=5)
                logger.info(f"[DREEVE] {label} — HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"[DREEVE] Erreur {label} : {e}")

    try:
        lat, lon = CITY_LAT_LON
        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&timezone=auto"
        res_weather = requests.get(weather_url, timeout=5)
        if res_weather.status_code == 200:
            logger.info("[METEO] Open-Meteo OK.")
        else:
            logger.warning(f"[METEO] Code HTTP inattendu : {res_weather.status_code}")
    except Exception as e:
        logger.error(f"[METEO] Erreur Open-Meteo : {e}")

    if not RADICALE_URL:
        logger.warning("[RADICALE] RADICALE_URL n'est pas définie.")
    else:
        try:
            # Vraie requête réseau (client.calendar(url=...) seul n'en fait aucune)
            now = datetime.datetime.now(UTC)
            events, skipped = list_events(now, now + datetime.timedelta(days=7))
            logger.info(f"[RADICALE] CalDAV OK : {len(events)} événement(s) sur 7 jours ({skipped} illisible(s)).")
        except Exception as e:
            logger.error(f"[RADICALE] Échec de connexion CalDAV : {e}")

    logger.info("=== FIN DU TEST DE CONNECTIVITÉ ===")


# ---------------------------------------------------------------------------
# Météo & Dreeve (inchangé)
# ---------------------------------------------------------------------------

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
                f"📊 *Charge actuelle (Dreeve) :*\n"
                f"- CTL (Forme) : {load_data.get('ctl', 'N/A')}\n"
                f"- ATL (Fatigue) : {load_data.get('atl', 'N/A')}\n"
                f"- TSB (Fraîcheur) : {load_data.get('tsb', {}).get('value', 'N/A')} ({load_data.get('tsb', {}).get('status', '')})\n"
                f"- Ratio A/C : {load_data.get('acRatio', {}).get('value', 'N/A')} ({load_data.get('acRatio', {}).get('status', '')})\n"
                f"- TRIMP hebdo : {load_data.get('weeklyTrimp', 'N/A')}\n"
                f"- Jours de repos (7j) : {load_data.get('restDaysLast7Days', 'N/A')}"
            )

        res_act = requests.get(
            f"{DREEVE_BASE_URL}/api/v1/activities?filters[sportType]=Run&pagination[size]=20",
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
                for act in activities:
                    name = act.get("name", "Sortie")
                    date = act.get("startDateLocal", "Date inconnue")[:10]
                    dist = get_dist_km(act)
                    summary_list.append(f"- {date} : {name} ({dist:.2f} km)")

                output_parts.append(f"🏃 *{len(activities)} derniers runs récupérés :*\n" + "\n".join(summary_list))

                if longest_run:
                    l_dist = get_dist_km(longest_run)
                    if l_dist > 0:
                        l_date = longest_run.get("startDateLocal", "")[:10]
                        l_name = longest_run.get("name", "Course")
                        output_parts.append(f"🏆 *Record de distance (Run le plus long) :* {l_dist:.2f} km le {l_date} ('{l_name}')")

        return "\n\n".join(output_parts) if output_parts else "Aucune donnée Dreeve disponible."
    except Exception as e:
        logger.error(f"Erreur Dreeve : {e}")
        return f"Erreur requête API Dreeve : {str(e)}"


# ---------------------------------------------------------------------------
# Radicale / CalDAV — couche robuste
#   - tout est identifié par UID (plus de recherche floue par titre)
#   - chaque écriture est vérifiée par RELECTURE sur le serveur
#   - chaque outil renvoie un dict structuré {"ok": bool, ...}
#   - chaque action est inscrite dans un registre (Ledger) qui sert au compte-rendu Telegram
# ---------------------------------------------------------------------------

@dataclass
class Action:
    kind: str       # "create" | "update" | "delete"
    label: str
    ok: bool
    detail: str = ""


def get_calendar():
    client_cal = caldav.DAVClient(url=RADICALE_URL, username=RADICALE_USER, password=RADICALE_PASS)
    return client_cal.calendar(url=RADICALE_URL)


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def parse_dt(value: str) -> datetime.datetime:
    """ISO -> datetime aware UTC. Sans fuseau, on suppose Europe/Paris."""
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(UTC)


def parse_range(start: str, end: str):
    """Une date seule en fin de plage inclut toute la journée."""
    s = parse_dt(start)
    e = parse_dt(end)
    if len(end.strip()) == 10:
        e += datetime.timedelta(days=1)
    return s, e


def to_aware(value) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ)
    return datetime.datetime.combine(value, datetime.time.min, tzinfo=TZ)  # date (journée entière)


def event_bounds(comp):
    start = to_aware(comp.dtstart.value)
    if hasattr(comp, "dtend"):
        end = to_aware(comp.dtend.value)
    elif hasattr(comp, "duration"):
        end = start + comp.duration.value
    else:
        end = start
    return start, end


def fmt_when(start: datetime.datetime, end: datetime.datetime) -> str:
    s, e = start.astimezone(TZ), end.astimezone(TZ)
    return f"{FR_DAYS[s.weekday()]} {s:%d/%m} {s:%H:%M}–{e:%H:%M}"


def serialize_event(ev) -> dict:
    comp = ev.vobject_instance.vevent
    start, end = event_bounds(comp)
    desc = str(comp.description.value) if hasattr(comp, "description") else ""
    return {
        "uid": str(comp.uid.value),
        "summary": str(comp.summary.value) if hasattr(comp, "summary") else "(sans titre)",
        "start": start.astimezone(TZ).isoformat(timespec="minutes"),
        "end": end.astimezone(TZ).isoformat(timespec="minutes"),
        "description": desc,
    }


def list_events(start: datetime.datetime, end: datetime.datetime):
    """Retourne (événements, nb d'événements illisibles ignorés)."""
    calendar = get_calendar()
    found = calendar.search(start=start, end=end, event=True)
    items, skipped = [], 0
    for ev in found:
        try:
            items.append(serialize_event(ev))
        except Exception as e:
            skipped += 1
            logger.warning(f"Événement illisible ignoré : {e}")
    items.sort(key=lambda x: x["start"])
    return items, skipped


def _clean_description(description):
    if description is None:
        return None
    return description.replace("\\n", "\n")


def tool_list(args: dict, ledger: list) -> dict:
    try:
        start, end = parse_range(args["start_date"], args["end_date"])
        events, skipped = list_events(start, end)
        return {"ok": True, "count": len(events), "unreadable_skipped": skipped, "events": events}
    except Exception as e:
        logger.error(f"Erreur lecture CalDAV : {e}")
        return {"ok": False, "error": str(e)}


def tool_create(args: dict, ledger: list) -> dict:
    summary = args.get("summary", "(sans titre)")
    try:
        start = parse_dt(args["start_time"])
        end = parse_dt(args["end_time"])
        if end <= start:
            raise ValueError("end_time doit être postérieur à start_time")
        if end < now_utc():
            raise ValueError("événement dans le passé : refusé")

        uid = f"{uuid.uuid4()}@running-coach"
        description = _clean_description(args.get("description")) or "👟 Séance planifiée par ton coach."

        cal = Calendar()
        cal.add("prodid", "-//running-coach-agent//FR")
        cal.add("version", "2.0")
        ev = Event()
        ev.add("uid", uid)
        ev.add("dtstamp", now_utc())
        ev.add("dtstart", start)
        ev.add("dtend", end)
        ev.add("summary", summary)
        ev.add("description", description)
        cal.add_component(ev)

        calendar = get_calendar()
        calendar.save_event(cal.to_ical().decode("utf-8"))

        # Vérification : relecture sur le serveur par UID
        check = calendar.event_by_uid(uid)
        c_start, c_end = event_bounds(check.vobject_instance.vevent)
        if abs((c_start - start).total_seconds()) > 60 or abs((c_end - end).total_seconds()) > 60:
            raise RuntimeError("l'événement relu sur Radicale ne correspond pas aux horaires demandés")

        ledger.append(Action("create", f"{summary} ({fmt_when(start, end)})", True))
        return {"ok": True, "uid": uid, "summary": summary, "start": start.astimezone(TZ).isoformat(timespec="minutes")}
    except Exception as e:
        logger.error(f"Erreur création CalDAV '{summary}' : {e}")
        ledger.append(Action("create", summary, False, str(e)))
        return {"ok": False, "error": str(e)}


def tool_update(args: dict, ledger: list) -> dict:
    uid = args.get("uid", "")
    label = uid
    try:
        calendar = get_calendar()
        try:
            ev = calendar.event_by_uid(uid)
        except NotFoundError:
            raise LookupError(f"aucun événement avec l'UID {uid}")

        comp = ev.vobject_instance.vevent
        old_start, old_end = event_bounds(comp)
        label = str(comp.summary.value) if hasattr(comp, "summary") else uid
        if old_end < now_utc():
            raise PermissionError("événement passé : modification refusée")

        new_start = parse_dt(args["new_start_time"]) if args.get("new_start_time") else None
        new_end = parse_dt(args["new_end_time"]) if args.get("new_end_time") else None
        if new_start and not new_end:
            new_end = new_start + (old_end - old_start)   # on conserve la durée
        if new_start and new_start < now_utc():
            raise PermissionError("nouvelle date dans le passé : refusée")

        if args.get("new_summary"):
            comp.summary.value = args["new_summary"]
        if new_start:
            comp.dtstart.value = new_start
            comp.dtstart.params.pop("TZID", None)
        if new_end:
            if hasattr(comp, "dtend"):
                comp.dtend.value = new_end
                comp.dtend.params.pop("TZID", None)
            else:
                comp.add("dtend").value = new_end
        if args.get("description") is not None:
            desc = _clean_description(args["description"])
            if hasattr(comp, "description"):
                comp.description.value = desc
            else:
                comp.add("description").value = desc
        ev.save()

        # Vérification par relecture
        check = calendar.event_by_uid(uid)
        c = check.vobject_instance.vevent
        c_start, c_end = event_bounds(c)
        if new_start and abs((c_start - new_start).total_seconds()) > 60:
            raise RuntimeError("relecture : la date de début n'a pas été modifiée sur Radicale")
        if new_end and abs((c_end - new_end).total_seconds()) > 60:
            raise RuntimeError("relecture : la date de fin n'a pas été modifiée sur Radicale")
        if args.get("new_summary") and str(c.summary.value) != args["new_summary"]:
            raise RuntimeError("relecture : le titre n'a pas été modifié sur Radicale")

        new_label = args.get("new_summary") or label
        ledger.append(Action("update", f"{new_label} ({fmt_when(c_start, c_end)})", True))
        return {"ok": True, "uid": uid, "summary": new_label}
    except Exception as e:
        logger.error(f"Erreur mise à jour CalDAV {uid} : {e}")
        ledger.append(Action("update", label, False, str(e)))
        return {"ok": False, "error": str(e)}


def tool_delete(args: dict, ledger: list) -> dict:
    uid = args.get("uid", "")
    label = uid
    try:
        calendar = get_calendar()
        try:
            ev = calendar.event_by_uid(uid)
        except NotFoundError:
            raise LookupError(f"aucun événement avec l'UID {uid} (déjà supprimé ?)")

        comp = ev.vobject_instance.vevent
        start, end = event_bounds(comp)
        summary = str(comp.summary.value) if hasattr(comp, "summary") else uid
        label = f"{summary} ({fmt_when(start, end)})"
        if end < now_utc():
            raise PermissionError("événement passé : suppression refusée")

        ev.delete()

        # Vérification : l'événement ne doit plus exister
        try:
            calendar.event_by_uid(uid)
        except NotFoundError:
            ledger.append(Action("delete", label, True))
            return {"ok": True, "uid": uid, "deleted": summary}
        raise RuntimeError("l'événement est toujours présent sur Radicale après suppression")
    except Exception as e:
        logger.error(f"Erreur suppression CalDAV {uid} : {e}")
        ledger.append(Action("delete", label, False, str(e)))
        return {"ok": False, "error": str(e)}


TOOL_IMPL = {
    "list_calendar_events": tool_list,
    "create_calendar_event": tool_create,
    "update_calendar_event": tool_update,
    "delete_calendar_event": tool_delete,
}


def run_tool(name: str, args: dict, ledger: list) -> dict:
    impl = TOOL_IMPL.get(name)
    if not impl:
        return {"ok": False, "error": f"outil inconnu : {name}"}
    return impl(args or {}, ledger)


TOOLS = [
    {
        "name": "list_calendar_events",
        "description": "Relit les événements du calendrier Radicale sur une plage (dates ISO, la date de fin seule inclut toute la journée). Utile pour vérifier l'état réel après tes modifications ou pour voir au-delà des données fournies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "ex: 2026-11-09"},
                "end_date": {"type": "string", "description": "ex: 2026-11-15 (inclus)"}
            },
            "required": ["start_date", "end_date"]
        }
    },
    {
        "name": "create_calendar_event",
        "description": "Crée un événement (séance ou repos) dans Radicale, futur uniquement. Heures sans fuseau = Europe/Paris. La description doit être multi-lignes avec émojis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start_time": {"type": "string", "description": "ISO datetime, ex: 2026-11-10T18:30:00"},
                "end_time": {"type": "string", "description": "ISO datetime"},
                "description": {"type": "string", "description": "Détails structurés multi-lignes avec émojis."}
            },
            "required": ["summary", "start_time", "end_time"]
        }
    },
    {
        "name": "update_calendar_event",
        "description": "Modifie un événement futur identifié par son UID (visible dans les données de l'agenda). Si tu changes new_start_time sans new_end_time, la durée est conservée.",
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string"},
                "new_summary": {"type": "string"},
                "new_start_time": {"type": "string"},
                "new_end_time": {"type": "string"},
                "description": {"type": "string"}
            },
            "required": ["uid"]
        }
    },
    {
        "name": "delete_calendar_event",
        "description": "Supprime UN événement futur identifié par son UID. Pour en supprimer plusieurs, appelle l'outil une fois par UID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string"}
            },
            "required": ["uid"]
        }
    }
]


# ---------------------------------------------------------------------------
# Compte-rendu : généré par le CODE à partir de ce qui s'est réellement passé
# ---------------------------------------------------------------------------

def md_escape(text: str) -> str:
    return re.sub(r"([_*`\[])", r"\\\1", text)


def build_report(ledger: list, truncated: bool, error: str = None) -> str:
    lines = []
    if ledger:
        verbs = {"create": "Créé", "update": "Modifié", "delete": "Supprimé"}
        ok_actions = [a for a in ledger if a.ok]
        ko_actions = [a for a in ledger if not a.ok]
        lines.append("*📋 Compte-rendu Radicale (relu sur le serveur)*")
        for a in ok_actions:
            lines.append(f"- ✅ {verbs[a.kind]} : {md_escape(a.label)}")
        for a in ko_actions:
            lines.append(f"- ❌ ÉCHEC ({verbs[a.kind].lower()}) : {md_escape(a.label)} — {md_escape(a.detail)}")
        lines.append(f"_{len(ok_actions)} réussie(s), {len(ko_actions)} échec(s)_")
    if truncated:
        lines.append("⚠️ Traitement interrompu (limite atteinte) : l'opération est peut-être incomplète, vérifie l'agenda.")
    if error:
        lines.append(f"⚠️ Erreur technique en cours de route : {md_escape(error)}")
    return "\n".join(lines)


CLAIM_PATTERN = re.compile(r"supprim|cré[ée]|planifi|modifi|mis à jour|déplac|ajout|réorganis|remplac", re.I)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
Tu es un coach sportif personnel expert, direct, concis et ultra-factuel.

RÈGLES DE TRANSPARENCE (IMPÉRATIVES) :
1. Tu ne peux affirmer qu'une modification de l'agenda a été faite que si un outil l'a confirmée ("ok": true). Si un outil renvoie "ok": false, dis-le franchement et explique ce qu'il reste à faire.
2. Le SYSTÈME ajoute lui-même, sous ta réponse, un compte-rendu vérifié des créations/modifications/suppressions réellement effectuées dans Radicale. Ne le recopie pas, ne le réécris pas : concentre-toi sur le fond (pourquoi ces choix, charge, météo, conseils).
3. Si la demande est ambiguë ou si tu ne trouves pas l'événement visé, demande une précision au lieu de supposer.
4. Descriptions d'événements : multi-lignes (`\\n`) avec émojis.

MÉTHODE POUR MODIFIER L'AGENDA :
- Les événements existants sont fournis avec leur UID. Identifie précisément ceux à toucher (dates + titre) et opère par UID.
- Pour "refaire une semaine" : 1) supprime uniquement les événements concernés de la période, 2) crée les nouveaux, 3) si utile, appelle list_calendar_events pour relire la période.
- Ne touche jamais à des événements hors de la période demandée (ex : une course ou un trail à une autre date).
- Passé : jamais de modification (le système refuse de toute façon).

CONTRAINTES COACHING :
- Présentiel bureau : lundi, mercredi, vendredi = séances légères ou repos.
- Repos : planifiés de 08:00 à 23:00.
- Dreeve en lecture seule stricte.

FORMAT TELEGRAM : listes à puces (`-`), gras (`*texte*`), émojis sobres. ZÉRO TABLEAU (pas de `|`).
"""


def build_context_block(weather, events, skipped, dreeve) -> str:
    now = datetime.datetime.now(TZ)
    return f"""
DONNÉES EN TEMPS RÉEL DU SYSTÈME :
- Date et heure actuelles : {FR_DAYS_LONG[now.weekday()]} {now:%d/%m/%Y %H:%M} (Europe/Paris)
- Prévisions Météo : {weather}
- Agenda Radicale ({CONTEXT_DAYS_AHEAD} prochains jours, {len(events)} événements, {skipped} illisibles ignorés) avec UID :
{json.dumps(events, ensure_ascii=False)}
- Données Dreeve & Charge / 20 derniers runs (Lecture seule) : {dreeve}
"""


async def ask_claude_with_tools(user_prompt, chat_id="default"):
    async with AGENT_LOCK:
        ledger: list = []
        truncated = False
        error = None
        reply_text = ""

        try:
            today = datetime.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            weather, cal_result, dreeve = await asyncio.gather(
                asyncio.to_thread(get_weather_forecast),
                asyncio.to_thread(list_events, today.astimezone(UTC), today.astimezone(UTC) + datetime.timedelta(days=CONTEXT_DAYS_AHEAD)),
                asyncio.to_thread(get_dreeve_data),
                return_exceptions=True,
            )
            if isinstance(cal_result, Exception):
                logger.error(f"Lecture Radicale impossible : {cal_result}")
                events, skipped = f"ERREUR DE LECTURE RADICALE : {cal_result}. N'effectue AUCUNE modification et préviens l'utilisateur.", 0
            else:
                events, skipped = cal_result
            if isinstance(weather, Exception):
                weather = {}
            if isinstance(dreeve, Exception):
                dreeve = f"Erreur Dreeve : {dreeve}"

            context_block = build_context_block(weather, events, skipped, dreeve)

            messages = list(USER_CONVERSATION_HISTORY[chat_id][-MAX_HISTORY_MESSAGES:])
            messages.append({"role": "user", "content": f"{context_block}\n\nMessage de l'utilisateur : \"{user_prompt}\""})

            for _ in range(MAX_TOOL_ROUNDS):
                response = await anthropic_client.messages.create(
                    model=MODEL,
                    max_tokens=8192,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )

                if response.stop_reason == "max_tokens":
                    truncated = True
                    break

                if response.stop_reason != "tool_use":
                    reply_text = "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
                    break

                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = await asyncio.to_thread(run_tool, block.name, block.input, ledger)
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, ensure_ascii=False),
                            "is_error": not result.get("ok", False),
                        })
                messages.append({"role": "user", "content": results})
            else:
                truncated = True   # MAX_TOOL_ROUNDS atteint
        except Exception as e:
            logger.exception("Erreur dans la boucle agent")
            error = f"{type(e).__name__}: {e}"

        ok_count = sum(1 for a in ledger if a.ok)
        if not ledger and CLAIM_PATTERN.search(reply_text or ""):
            reply_text += "\n\n⚠️ Aucune modification n'a réellement été effectuée dans Radicale (aucun outil n'a été exécuté)."
        elif not reply_text and not ledger:
            reply_text = "Je n'ai rien produit de concret sur cette demande (aucune réponse du modèle, aucune action dans Radicale)."

        report = build_report(ledger, truncated, error)
        final_text = f"{reply_text}\n\n{report}".strip() if report else reply_text

        # L'historique contient ce qui s'est RÉELLEMENT passé (compte-rendu inclus)
        USER_CONVERSATION_HISTORY[chat_id].append({"role": "user", "content": user_prompt})
        USER_CONVERSATION_HISTORY[chat_id].append({"role": "assistant", "content": final_text})
        USER_CONVERSATION_HISTORY[chat_id] = USER_CONVERSATION_HISTORY[chat_id][-MAX_HISTORY_MESSAGES:]

        logger.info(f"Agent terminé : {ok_count}/{len(ledger)} action(s) Radicale réussie(s), truncated={truncated}, error={error}")
        return final_text


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _plain(text: str) -> str:
    return re.sub(r"\\([_*`\[])", r"\1", text)


def split_message(text: str, limit: int = 3900):
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks or [text]


async def send_text(send, text: str):
    """send : coroutine function(text, **kwargs). Markdown d'abord, texte brut en secours."""
    for chunk in split_message(text):
        try:
            await send(chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await send(_plain(chunk))


async def check_new_activities_job():
    global LAST_KNOWN_ACTIVITY_ID
    if not DREEVE_API_KEY or not telegram_app or not TELEGRAM_CHAT_ID:
        return

    headers = {"Authorization": f"Bearer {DREEVE_API_KEY}", "Accept": "application/json"}
    try:
        res = await asyncio.to_thread(
            requests.get, f"{DREEVE_BASE_URL}/api/v1/activities?pagination[size]=1", headers=headers, timeout=5
        )
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

                    prompt = f"Je viens de terminer et d'uploader une nouvelle séance : '{name}' ({dist:.2f} km). Donne-moi un débrief très court, factuel et concis. Ne modifie pas l'agenda."
                    reply_text = await ask_claude_with_tools(prompt, chat_id=TELEGRAM_CHAT_ID)
                    send = functools.partial(telegram_app.bot.send_message, chat_id=TELEGRAM_CHAT_ID)
                    await send_text(send, f"⚡ *Nouveau run détecté !*\n\n{reply_text}")
    except Exception as e:
        logger.error(f"Erreur vérification automatique Dreeve : {e}")


async def send_morning_briefing():
    if not telegram_app or not TELEGRAM_CHAT_ID:
        return
    prompt = (
        "Réalise le briefing matinal. Analyse l'agenda Radicale, la météo, les métriques de charge et les 20 derniers runs récupérés de Dreeve. "
        "Met à jour / planifie les 30 prochains jours dans Radicale. "
        "Fais un résumé Telegram ultra-concis, sobre, avec des descriptions structurées et des émojis."
    )
    try:
        reply_text = await ask_claude_with_tools(prompt, chat_id=TELEGRAM_CHAT_ID)
    except Exception as e:
        logger.exception("Briefing matinal en échec")
        reply_text = f"❌ Briefing matinal impossible : {type(e).__name__}"
    send = functools.partial(telegram_app.bot.send_message, chat_id=TELEGRAM_CHAT_ID)
    await send_text(send, reply_text)


async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = str(update.effective_chat.id)

    # Le bot peut modifier ton agenda : on ignore tout autre chat que le tien
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        logger.warning(f"Message ignoré d'un chat non autorisé : {chat_id}")
        return

    try:
        await update.message.chat.send_action("typing")
    except Exception:
        pass

    try:
        reply_text = await ask_claude_with_tools(update.message.text, chat_id=chat_id)
    except Exception as e:
        logger.exception("Erreur non gérée")
        reply_text = f"❌ Erreur technique : {type(e).__name__}. Rien n'est garanti côté Radicale, vérifie l'agenda."

    await send_text(update.message.reply_text, reply_text)


scheduler = None


@app.on_event("startup")
async def startup_event():
    global telegram_app, scheduler

    await asyncio.to_thread(verify_services_connectivity)

    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID non défini : briefing matinal et détection de runs désactivés, bot ouvert à tout chat !")

    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling()

        # AsyncIOScheduler : exécute vraiment les jobs async (BackgroundScheduler ne les "await" pas)
        scheduler = AsyncIOScheduler(timezone="Europe/Paris")
        scheduler.add_job(send_morning_briefing, "cron", hour=8, minute=0)
        scheduler.add_job(check_new_activities_job, "interval", minutes=15)
        scheduler.start()


@app.on_event("shutdown")
async def shutdown_event():
    if scheduler:
        scheduler.shutdown(wait=False)
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


@app.get("/health")
def health_check():
    return {"status": "running"}