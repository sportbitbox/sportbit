#!/usr/bin/env python3
"""
Automatische SportBit-inschrijving voor De Box Bunschoten.

Lessen:
- Maandag 07:00 WOD
- Dinsdag 06:00 Power
- Woensdag 06:00 WOD
- Donderdag 07:00 Power
- Zaterdag 09:15 Power
- Zaterdag 09:15 Buddy Workout / Buddy WOD

Pogingen:
1. Exact bij opening
2. 30 seconden na opening
3. 60 seconden na opening
4. 90 seconden na opening
5. 120 seconden na opening
6. 150 seconden na opening
7. 180 seconden na opening

De workflow mag maximaal 15 minuten voor opening starten.
Voor opening wacht het script zonder inschrijfpogingen te versturen.
"""

import argparse
import logging
import os
import re
import sys
import time
import unicodedata

from datetime import datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests


# ------------------------------------------------------------
# Instellingen
# ------------------------------------------------------------

BASE_WEB_URL = "https://deboxbunschoten.sportbitapp.nl/"

BASE_API_URL = urljoin(
    BASE_WEB_URL,
    "cbm/api/",
)

REFERER = urljoin(
    BASE_WEB_URL,
    "web/nl/events",
)

AMSTERDAM = ZoneInfo(
    "Europe/Amsterdam"
)

REGISTRATION_OPENS_HOURS = 48

MONITOR_BEFORE_MINUTES = 15

RETRY_INTERVAL_SECONDS = 30

MAX_ATTEMPTS_PER_LESSON = 7

DEFAULT_ROSTER_IDS = tuple(
    range(1, 11)
)


# ------------------------------------------------------------
# Lesschema
# ------------------------------------------------------------

# Weekdagen:
# 0 = maandag
# 1 = dinsdag
# 2 = woensdag
# 3 = donderdag
# 4 = vrijdag
# 5 = zaterdag
# 6 = zondag

SCHEDULE = [
    (0, "07:00", ("WOD",)),
    (1, "06:00", ("Power",)),
    (2, "06:00", ("WOD",)),
    (3, "07:00", ("Power",)),
    (5, "09:15", ("Power",)),
    (
        5,
        "09:15",
        (
            "Buddy Workout",
            "Buddy WOD",
        ),
    ),
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
    format=(
        "%(asctime)s "
        "[%(levelname)s] "
        "%(message)s"
    ),
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger(
    "sportbit-debox"
)


# ------------------------------------------------------------
# Hulpfuncties
# ------------------------------------------------------------

def normalize(value):
    """
    Maak lestitels geschikt voor vergelijking.
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


def parse_roster_ids():
    """
    Lees optioneel SPORTBIT_ROOSTER_IDS.

    Zonder aparte instelling probeert het script
    rooster-ID 1 tot en met 10.
    """

    raw_value = os.environ.get(
        "SPORTBIT_ROOSTER_IDS",
        "",
    ).strip()

    if not raw_value:
        return DEFAULT_ROSTER_IDS

    try:
        roster_ids = tuple(
            dict.fromkeys(
                int(part.strip())
                for part in raw_value.split(",")
                if part.strip()
            )
        )

    except ValueError:
        log.error(
            "SPORTBIT_ROOSTER_IDS moet nummers bevatten. "
            "Gebruik bijvoorbeeld 1 of 1,2."
        )

        sys.exit(2)

    if not roster_ids:
        return DEFAULT_ROSTER_IDS

    return roster_ids


def sleep_until(target_time):
    """
    Wacht tot het opgegeven tijdstip.
    """

    while True:
        current_time = datetime.now(
            AMSTERDAM
        )

        remaining_seconds = (
            target_time
            - current_time
        ).total_seconds()

        if remaining_seconds <= 0:
            return

        time.sleep(
            min(
                10,
                max(
                    0.1,
                    remaining_seconds,
                ),
            )
        )


# ------------------------------------------------------------
# SportBit-client
# ------------------------------------------------------------

class SportBitClient:

    def __init__(
        self,
        username,
        password,
    ):
        self.username = username
        self.password = password

        self.session = requests.Session()

        self.session.headers.update(
            {
                "Accept": (
                    "application/json, "
                    "text/plain, */*"
                ),
                "User-Agent": (
                    "Mozilla/5.0 "
                    "SportBitAutoSignup/5.0"
                ),
                "Referer": REFERER,
            }
        )

    def make_url(
        self,
        path,
    ):
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
            "Logging in to De Box Bunschoten "
            "SportBit portal..."
        )

        try:
            heartbeat = self.session.get(
                self.make_url(
                    "data/heartbeat/"
                ),
                timeout=20,
            )

            heartbeat.raise_for_status()

            self.set_xsrf_header()

            response = self.session.post(
                self.make_url(
                    "data/inloggen/"
                ),
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

    def get_events(
        self,
        date_text,
        roster_id,
    ):
        response = self.session.get(
            self.make_url(
                "data/events/"
            ),
            params={
                "datum": date_text,
                "rooster": roster_id,
            },
            timeout=20,
        )

        response.raise_for_status()

        data = response.json()

        events = []

        for period in (
            "ochtend",
            "middag",
            "avond",
        ):
            period_events = data.get(
                period
            )

            if isinstance(
                period_events,
                list,
            ):
                events.extend(
                    period_events
                )

        return events

    def get_events_all_rosters(
        self,
        date_text,
        roster_ids,
    ):
        events_by_id = {}

        successful_rosters = 0

        for roster_id in roster_ids:

            try:
                events = self.get_events(
                    date_text,
                    roster_id,
                )

                successful_rosters += 1

            except (
                requests.RequestException,
                ValueError,
            ) as error:

                log.warning(
                    "Could not read roster %s "
                    "for %s: %s",
                    roster_id,
                    date_text,
                    error,
                )

                continue

            for event in events:
                event_id = event.get(
                    "id"
                )

                if event_id is not None:
                    events_by_id[
                        str(event_id)
                    ] = event

        if successful_rosters == 0:
            raise RuntimeError(
                "Geen enkel rooster kon worden gelezen voor "
                + date_text
            )

        return list(
            events_by_id.values()
        )

    def signup(
        self,
        event_id,
    ):
        """
        Verstuur een deelnameverzoek.

        Bij een volle les verstuurt het script hetzelfde
        deelnameverzoek. SportBit bepaalt of daarmee een
        wachtlijstinschrijving ontstaat.
        """

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

        if response.status_code in (
            200,
            204,
        ):
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

def get_target_slots(
    days_ahead,
):
    today = datetime.now(
        AMSTERDAM
    ).date()

    slots = []

    for offset in range(
        days_ahead + 1
    ):
        date_value = (
            today
            + timedelta(
                days=offset
            )
        )

        for schedule_item in SCHEDULE:
            weekday = schedule_item[0]
            target_time = schedule_item[1]
            titles = schedule_item[2]

            if (
                date_value.weekday()
                == weekday
            ):
                slots.append(
                    (
                        date_value,
                        target_time,
                        titles,
                    )
                )

    return slots


def get_event_start(
    event,
):
    start_value = str(
        event.get(
            "start",
            "",
        )
    )

    try:
        start_datetime = datetime.fromisoformat(
            start_value
        )

    except ValueError:
        return None

    if start_datetime.tzinfo is None:
        start_datetime = start_datetime.replace(
            tzinfo=AMSTERDAM
        )

    return start_datetime.astimezone(
        AMSTERDAM
    )


def find_unique_event(
    events,
    target_time,
    allowed_titles,
):
    normalized_titles = set(
        normalize(title)
        for title in allowed_titles
    )

    matches = []

    for event in events:
        start_datetime = get_event_start(
            event
        )

        if start_datetime is None:
            continue

        actual_time = start_datetime.strftime(
            "%H:%M"
        )

        actual_title = normalize(
            str(
                event.get(
                    "titel",
                    "",
                )
            )
        )

        if actual_time != target_time:
            continue

        if actual_title not in normalized_titles:
            continue

        matches.append(event)

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        log.error(
            "Meerdere passende lessen gevonden om %s "
            "voor %s. Les wordt overgeslagen.",
            target_time,
            "/".join(allowed_titles),
        )

    return None
