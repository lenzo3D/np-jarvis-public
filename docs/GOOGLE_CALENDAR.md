# Connecting Google Calendar (optional)

JARVIS can add your deadlines to your Google Calendar and show the calendar in the panel. Google needs you to create
a small "app" of your own for this, once. It stays private to you and costs nothing.

1. Go to https://console.cloud.google.com and create a project (any name).
2. **APIs & Services > Library:** search for **Google Calendar API** and press Enable.
3. **APIs & Services > OAuth consent screen:** choose External, fill in the app name and your email. Under
   **Audience / Publishing status**, press **Publish app**. (If you leave it in Testing, Google logs you out every
   7 days.)
4. **APIs & Services > Credentials > Create credentials > OAuth client ID.** Application type: **Desktop app.**
5. Download the JSON file and save it as `state/google_client.json` inside the JARVIS folder.
6. In the panel, open **Logins & Tools** and press the Google Calendar login, or run
   `.venv/bin/python -m core.gcal login`. A browser window opens; approve access to your calendar.

The Calendar indicator turns green once connected. JARVIS only creates and edits events it made itself, and marks
dates it is unsure about as tentative.
