# 🏃‍Autonomous Telegram Running Coach Agent

Agent de coaching sportif intelligent auto-hébergé sur un serveur **Lenovo ThinkCentre SFF** (i3, 16 Go RAM), piloté en langage naturel via **Telegram**, alimenté par **Claude 5.5 Haiku**, synchronisé avec **Radicale (CalDAV)** sur un calendrier dédié au running, et connecté à l'instance **Dreeve** (via API/MCP).

---

## 🏗️ Architecture & Composants

* **Infrastructure local :** Lenovo ThinkCentre SFF sous Docker.
* **Moteur IA :** API Anthropic Claude 3.5 Haiku (`claude-3-5-haiku-20241022`).
* **Interface :** Bot Telegram (mode polling natif via FastAPI).
* **Calendrier :** Serveur Radicale CalDAV (calendrier spécifique running).
* **Données Sportives :** Dreeve (détection & notifs d'imports directes sur Telegram, lecture de la charge via API/MCP).
* **Météo :** API Open-Meteo (prévisions 7 jours sans clé API).

---

## 📂 Structure du projet

```text
running-coach/
├── Dockerfile
├── docker-compose.yml
├── main.py
└── requirements.txt
