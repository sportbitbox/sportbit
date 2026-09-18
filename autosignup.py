#!/usr/bin/env python3
"""
Automatische SportBit-inschrijving voor De Box Bunschoten.

Lessen:
- Maandag 07:00 WOD
- Dinsdag 06:00 Strength
- Woensdag 06:00 WOD
- Donderdag 07:00 Power
- Zaterdag 09:15 Power
- Zaterdag 09:15 Buddy Workout / Buddy WOD

De inschrijving opent 48 uur voor aanvang.

Als een les eenmaal door het script is gezien terwijl je stond
ingeschreven, wordt het les-ID onthouden. Schrijf je jezelf daarna
handmatig uit, dan schrijft het script je niet opnieuw in.
"""

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


# ------------------------------------------------------------
# Instellingen
# ------------------------------------------------------------

BASE_WEB_URL = "https://deboxbunschoten.sportbitapp.nl/"
BASE_API_URL = urljoin(BASE_WEB_URL, "cbm/api/")
REFERER = urljoin(BASE_WEB_URL, "web/nl/events")

AMSTERDAM = ZoneInfo("Europe/Amsterdam")

LEDGER_FILE = Path("booked_events.json")

REGISTRATION_OPENS_HOURS = 48

# De workflow mag tot 60 minuten na het openen inschrijven.
OPENING_WINDOW_MINUTES = 60

# Controleer rooster-ID 1 tot en met 10.
DEFAULT_ROSTER_IDS = range(1, 11)


# Weekdagen:
# 0 = maandag
# 1 = dinsdag
# 2 = woensdag
# 3 = donderdag
# 4 = vrijdag
# 5 = zaterdag
# 6 = zondag

SCHEDULE = [
    (0, "07:00", ["WOD"]),
    (1, "06:00", ["Strength"]),
    (2, "06:00", ["WOD"]),
    (3, "07:00", ["Power"]),
    (5, "09:15", ["Power"]),
    (5, "09:15", ["Buddy Workout", "Buddy WOD"]),
]

DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger("sportbit-debox")


# ------------------------------------------------------------
# Hulpfuncties
# ------------------------------------------------------------

def normalize(value):
    """
    Maak lestitels vergelijkbaar.
    """

    text = unicodedata.normalize(
        "NFKD",
        value or "",
    )

    text = "".join(
        character
        for character in text
        if not unicodedata.combining(character)
    )

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text.lower(),
    ).strip()

    return re.sub(
        r"\s+",
        " ",
        text,
    )


def get_roster_ids():
    """
    Lees optioneel SPORTBIT_ROOSTER_IDS.

    Zonder aparte instelling worden rooster-ID's
    1 tot en met 10 geprobeerd.
    """

    configured_ids = os.environ.get(
        "SPORTBIT_ROOSTER_IDS",
        "",
    ).strip()

    if not configured_ids:
        return list(DEFAULT_ROSTER_IDS)

    try:
        roster_ids = []

        for item in configured_ids.split(","):
            item = item.strip()

            if item:
                roster_id = int(item)

                if roster_id not in roster_ids:
                    roster_ids.append(roster_id)

        if roster_ids:
            return roster_ids

    except ValueError:
        log.error(
            "SPORTBIT_ROOSTER_IDS moet nummers bevatten, "
            "bijvoorbeeld 1 of 1,2."
        )

        sys.exit(2)

    return list(DEFAULT_ROSTER_IDS)


# ------------------------------------------------------------
# Boekingsgeheugen
# ------------------------------------------------------------

def load_ledger():
    """
    Lees eerder onthouden les-ID's.
    """

    if not LEDGER_FILE.exists():
        return set()

    try:
        content = LEDGER_FILE.read_text(
            encoding="utf-8",
        )

        data = json.loads(content)

    except Exception as error:
        log.error(
            "Kan boekingsgeheugen niet lezen: %s",
            error,
        )

        sys.exit(2)

    remembered_ids = data.get(
        "booked_event_ids",
        [],
    )

    if not isinstance(remembered_ids, list):
        log.error(
            "Het boekingsgeheugen heeft een ongeldige indeling."
        )

        sys.exit(2)

    return set(
        str(event_id)
        for event_id in remembered_ids
    )


def save_ledger(event_ids):
    """
    Sla de onthouden les-ID's op.
    """

    content = {
        "booked_event_ids": sorted(event_ids),
    }

    LEDGER_FILE.write_text(
        json.dumps(
            content,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------
# SportBit
# ------------------------------------------------------------

class SportBitClient:

    def __init__(self, username, password):

        self.username = username
        self.password = password

        self.session = requests.Session()

        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": (
                    "Mozilla/5.0 "
                    "SportBitAutoSignup/1.0"
                ),
                "Referer": REFERER,
            }
        )

    def make_url(self, path):

        return urljoin(
            BASE_API_URL,
            path,
        )

    def set_xsrf_header(self):

        token = self.session.cookies.get(
            "XSRF-TOKEN"
        )

        if token:
            self.session.headers[
                "X-XSRF-TOKEN"
            ] = token

    def login(self):

        log.info(
            "Logging in to De Box Bunschoten SportBit portal..."
        )

        try:
            heartbeat = self.session.get(
                self.make_url("data/heartbeat/"),
                timeout=20,
            )

            heartbeat.raise_for_status()

            self.set_xsrf_header()

            response = self.session.post(
                self.make_url("data/inloggen/"),
                json={
                    "username": self.username,
                    "password": self.password,
                    "remember": True,
                },
                timeout=20,
            )

        except requests.RequestException as error:
            log.error(
                "Login request failed: %s",
                error,
            )

            return False

        if response.status_code == 200:
            self.set_xsrf_header()

            log.info(
                "Login successful."
            )

            return True

        log.error(
            "Login failed, HTTP %s: %s",
            response.status_code,
            response.text[:200],
        )

        return False

    def get_events(self, date_text, roster_id):

        response = self.session.get(
            self.make_url("data/events/"),
            params={
                "datum": date_text,
                "rooster": roster_id,
            },
            timeout=20,
        )

        response.raise_for_status()

        data = response.json()

        events = []

        for period in [
            "ochtend",
            "middag",
            "avond",
        ]:
            period_events = data.get(period)

            if isinstance(period_events, list):
                events.extend(period_events)

        return events

    def get_events_all_rosters(
        self,
        date_text,
        roster_ids,
    ):

        events_by_id = {}

        successfully_read = 0

        for roster_id in roster_ids:

            try:
                events = self.get_events(
                    date_text,
                    roster_id,
                )

                successfully_read += 1

            except Exception as error:
                log.warning(
                    "Could not read roster %s for %s: %s",
                    roster_id,
                    date_text,
                    error,
                )

                continue

            for event in events:
                event_id = event.get("id")

                if event_id is not None:
                    events_by_id[
                        str(event_id)
                    ] = event

        if successfully_read == 0:
            raise RuntimeError(
                "Geen enkel rooster kon worden gelezen "
                "voor " + date_text
            )

        return list(
            events_by_id.values()
        )

    def signup(self, event_id):

        self.set_xsrf_header()

        try:
            response = self.session.post(
                self.make_url(
                    "data/events/"
                    + str(event_id)
                    + "/deelname/"
                ),
                json={},
                timeout=20,
            )

        except requests.RequestException as error:
            return False, str(error)

        if response.status_code in [200, 204]:
            return True, ""

        error_message = (
            "HTTP "
            + str(response.status_code)
            + ": "
            + response.text[:300]
        )

        return False, error_message


# ------------------------------------------------------------
# Lesselectie
# ------------------------------------------------------------

def get_target_slots(days_ahead):

    today = datetime.now(
        AMSTERDAM
    ).date()

    slots = []

    for offset in range(days_ahead + 1):

        date_value = today + timedelta(
            days=offset
        )

        for schedule_item in SCHEDULE:

            weekday = schedule_item[0]
            target_time = schedule_item[1]
            titles = schedule_item[2]

            if date_value.weekday() == weekday:
                slots.append(
                    (
                        date_value,1)
