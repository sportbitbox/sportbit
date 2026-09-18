#!/usr/bin/env python3
"""SportBit auto-inschrijving voor De Box Bunschoten."""

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://deboxbunschoten.sportbitapp.nl/cbm/api/"
REFERER = "https://deboxbunschoten.sportbitapp.nl/web/nl/events"
TIMEZONE = ZoneInfo("Europe/Amsterdam")
MEMORY_FILE = Path("booked_events.json")
ROSTER_IDS = range(1, 11)
OPEN_HOURS_BEFORE = 48
BOOKING_WINDOW_MINUTES = 60

# Weekdag: maandag=0, dinsdag=1, woensdag=2, donderdag=3, zaterdag=5
LESSONS = [
    (0, "07:00", ["WOD"]),
    (1, "06:00", ["Strength"]),
    (2, "06:00", ["WOD"]),
    (3, "07:00", ["Power"]),
    (5, "09:15", ["Power"]),
    (5, "09:15", ["Buddy Workout", "Buddy WOD"]),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sportbit")


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def load_memory():
    if not MEMORY_FILE.exists():
        return set()
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        return set(str(item) for item in data.get("booked_event_ids", []))
    except Exception as error:
        log.error("Boekingsgeheugen kan niet worden gelezen: %s", error)
        sys.exit(2)


def save_memory(event_ids):
    data = {"booked_event_ids": sorted(event_ids)}
    MEMORY_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class SportBitClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 SportBitAutoSignup/2.0",
            "Referer": REFERER,
        })

    def url(self, path):
        return urljoin(BASE_URL, path)

    def set_xsrf(self):
        token = self.session.cookies.get("XSRF-TOKEN")
        if token:
            self.session.headers["X-XSRF-TOKEN"] = token

    def login(self):
        try:
            heartbeat = self.session.get(self.url("data/heartbeat/"), timeout=20)
            heartbeat.raise_for_status()
            self.set_xsrf()
            response = self.session.post(
                self.url("data/inloggen/"),
                json={
                    "username": self.username,
                    "password": self.password,
                    "remember": True,
                },
                timeout=20,
            )
        except requests.RequestException as error:
            log.error("Inloggen mislukt: %s", error)
            return False

        if response.status_code == 200:
            self.set_xsrf()
            log.info("Login successful.")
            return True

        log.error("Inloggen geweigerd, HTTP %s: %s", response.status_code, response.text[:200])
        return False

    def events_for_roster(self, date_text, roster_id):
        response = self.session.get(
            self.url("data/events/"),
            params={"datum": date_text, "rooster": roster_id},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        events = []
        for period in ["ochtend", "middag", "avond"]:
            if isinstance(data.get(period), list):
                events.extend(data[period])
        return events

    def all_events(self, date_text):
        events_by_id = {}
        successful_rosters = 0
        for roster_id in ROSTER_IDS:
            try:
                events = self.events_for_roster(date_text, roster_id)
                successful_rosters += 1
            except Exception as error:
                log.warning("Rooster %s kon niet worden gelezen: %s", roster_id, error)
                continue
            for event in events:
                event_id = event.get("id")
                if event_id is not None:
                    events_by_id[str(event_id)] = event
        if successful_rosters == 0:
            raise RuntimeError("Geen enkel rooster kon worden gelezen voor " + date_text)
        return list(events_by_id.values())

    def signup(self, event_id):
        self.set_xsrf()
        try:
            response = self.session.post(
                self.url("data/events/" + str(event_id) + "/deelname/"),
                json={},
                timeout=20,
            )
        except requests.RequestException as error:
            return False, str(error)
        if response.status_code in [200, 204]:
            return True, ""
        return False, "HTTP " + str(response.status_code) + ": " + response.text[:300]


def event_start(event):
    try:
        value = datetime.fromisoformat(str(event.get("start", "")))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=TIMEZONE)
    return value.astimezone(TIMEZONE)


def find_event(events, target_time, allowed_titles):
    allowed = set(normalize(title) for title in allowed_titles)
    matches = []
    for event in events:
        start = event_start(event)
        title = normalize(str(event.get("titel", "")))
        if start and start.strftime("%H:%M") == target_time and title in allowed:
            matches.append(event)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log.error("Meerdere passende lessen gevonden voor %s om %s; overslaan.", allowed_titles, target_time)
    return None


def target_slots(days_ahead):
    today = datetime.now(TIMEZONE).date()
    slots = []
    for offset in range(days_ahead + 1):
        date_value = today + timedelta(days=offset)
        for weekday, target_time, titles in LESSONS:
            if date_value.weekday() == weekday:
                slots.append((date_value, target_time, titles))
    return slots


def run(username, password, live, days_ahead):
    client = SportBitClient(username, password)
    if not client.login():
        return 1

    memory = load_memory()
    cache = {}
    failures = 0

    for date_value, target_time, titles in target_slots(days_ahead):
        date_text = date_value.isoformat()
        label = date_text + " " + target_time + " " + "/".join(titles)
        log.info("--- %s ---", label)

        if date_text not in cache:
            try:
                cache[date_text] = client.all_events(date_text)
            except RuntimeError as error:
                log.error("%s", error)
                failures += 1
                continue

        event = find_event(cache[date_text], target_time, titles)
        if event is None:
            log.warning("Target lesson not found: %s", label)
            continue

        event_id = event.get("id")
        if event_id is None:
            log.error("Les heeft geen ID: %s", label)
            failures += 1
            continue

        event_id_text = str(event_id)
        title = str(event.get("titel", "?"))
        registered = bool(event.get("aangemeld", False))
        waitlisted = bool(event.get("opWachtlijst", False))
        participants = int(event.get("aantalDeelnemers", 0) or 0)
        capacity = int(event.get("maxDeelnemers", 0) or 0)

        if event_id_text in memory:
            if registered or waitlisted:
                log.info("Already registered and remembered: %s [id=%s]", title, event_id)
            else:
                log.info("Manual cancellation respected; not registering again: %s [id=%s]", title, event_id)
            continue

        if registered or waitlisted:
            memory.add(event_id_text)
            save_memory(memory)
            log.info("Existing registration remembered: %s [id=%s]", title, event_id)
            continue

        if capacity > 0 and participants >= capacity:
            log.warning("Les is vol; automatische wachtlijst staat uit: %s", title)
            continue

        start = event_start(event)
        if start is None:
            log.error("Ongeldige startdatum: %s [id=%s]", title, event_id)
            failures += 1
            continue

        opens_at = start - timedelta(hours=OPEN_HOURS_BEFORE)
        window_ends = opens_at + timedelta(minutes=BOOKING_WINDOW_MINUTES)
        now = datetime.now(TIMEZONE)

        if now < opens_at:
            log.info("Inschrijving opent op %s", opens_at.isoformat())
            continue
        if now > window_ends:
            log.info("Boekingsvenster voorbij; geen inschrijving: %s", title)
            continue
        if not live:
            log.info("[DRY RUN] Would register: %s [id=%s]", title, event_id)
            continue

        log.info("Registering: %s [id=%s]", title, event_id)
        success, message = client.signup(event_id)
        if success:
            memory.add(event_id_text)
            save_memory(memory)
            log.info("Registration successful and remembered.")
        else:
            failures += 1
            log.error("Registration failed: %s", message)

    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args()

    username = os.environ.get("SPORTBIT_USERNAME")
    password = os.environ.get("SPORTBIT_PASSWORD")
    if not username or not password:
        log.error("SPORTBIT_USERNAME en SPORTBIT_PASSWORD ontbreken.")
        sys.exit(2)
    if args.days < 0 or args.days > 14:
        log.error("--days moet tussen 0 en 14 liggen.")
        sys.exit(2)

    log.warning("LIVE MODE enabled.") if args.live else log.info("DRY RUN enabled.")
    sys.exit(run(username, password, args.live, args.days))


if __name__ == "__main__":
    main()
