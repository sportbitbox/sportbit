#!/usr/bin/env python3
"""
Automatische SportBit-inschrijving voor De Box Bunschoten.

Vaste lessen:
- Maandag 07:00 WOD
- Dinsdag 06:00 Strength
- Woensdag 06:00 WOD
- Donderdag 07:00 Power
- Zaterdag 09:15 Power
- Zaterdag 09:15 Buddy Workout of Buddy WOD

Werking:
- De inschrijving opent 48 uur voor aanvang.
- Het script mag binnen 60 minuten na opening inschrijven.
- Volle lessen worden overgeslagen.
- Het script schrijft niet automatisch in op een wachtlijst.
- Een eenmaal geboekte of bestaande inschrijving wordt onthouden.
- Na een latere handmatige uitschrijving wordt niet opnieuw ingeschreven.
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


BASE_WEB_URL = "https://deboxbunschoten.sportbitapp.nl/"
BASE_API_URL = urljoin(BASE_WEB_URL, "cbm/api/")
REFERER = urljoin(BASE_WEB_URL, "web/nl/events")

AMSTERDAM = ZoneInfo("Europe/Amsterdam")

LEDGER_FILE = Path("booked_events.json")

REGISTRATION_OPENS_HOURS = 48
OPENING_WINDOW_MINUTES = 60

ROSTER_IDS = list(range(1, 11))


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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger("sportbit-debox")


def normalize(value):
    text = unicodedata.normalize("NFKD", value or "")

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


def load_ledger():
    if not LEDGER_FILE.exists():
        return set()

    try:
        content = LEDGER_FILE.read_text(
            encoding="utf-8"
        )

        data = json.loads(content)

    except Exception as error:
        log.error(
            "Kan booked_events.json niet lezen: %s",
            error,
        )

        sys.exit(2)

    remembered_ids = data.get(
        "booked_event_ids",
        [],
    )

    if not isinstance(remembered_ids, list):
        log.error(
            "booked_events.json heeft een ongeldige indeling."
        )

        sys.exit(2)

    return set(
        str(event_id)
        for event_id in remembered_ids
    )


def save_ledger(event_ids):
    data = {
        "booked_event_ids": sorted(event_ids),
    }

    LEDGER_FILE.write_text(
        json.dumps(
            data,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


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

        periods = [
            "ochtend",
            "middag",
            "avond",
        ]

        for period in periods:
            period_events = data.get(period)

            if isinstance(period_events, list):
                events.extend(period_events)

        return events

    def get_events_all_rosters(
        self,
        date_text,
    ):
        events_by_id = {}
        successful_rosters = 0

        for roster_id in ROSTER_IDS:

            try:
                events = self.get_events(
                    date_text,
                    roster_id,
                )

                successful_rosters += 1

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

        if successful_rosters == 0:
            raise RuntimeError(
                "Geen enkel rooster kon worden gelezen voor "
                + date_text
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

        if response.status_code in [200, 204\]:
            return True, ""

        error_message = (
            "HTTP "
            + str(response.status_code)
            + ": "
            + response.text[:300]
        )

        return False, error_message


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
                        date_value,
                        target_time,
                        titles,
                    )
                )

    return slots


def get_event_start(event):
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
            "Meerdere overeenkomende lessen gevonden "
            "om %s voor %s. Les wordt overgeslagen.",
            target_time,
            "/".join(allowed_titles),
        )

    return None


def run(
    username,
    password,
    dry_run,
    days_ahead,
):
    client = SportBitClient(
        username,
        password,
    )

    if not client.login():
        return 1

    remembered_ids = load_ledger()

    events_cache = {}

    failures = 0

    log.info(
        "Checking roster IDs: %s",
        ", ".join(
            str(roster_id)
            for roster_id in ROSTER_IDS
        ),
    )

    slots = get_target_slots(
        days_ahead
    )

    for slot in slots:
        date_value = slot[0]
        target_time = slot[1]
        allowed_titles = slot[2]

        date_text = date_value.isoformat()

        label = (
            DAY_NAMES[date_value.weekday()]
            + " "
            + date_text
            + " "
            + target_time
            + " "
            + "/".join(allowed_titles)
        )

        log.info(
            "--- %s ---",
            label,
        )

        if date_text not in events_cache:

            try:
                events_cache[
                    date_text
                ] = client.get_events_all_rosters(
                    date_text
                )

            except RuntimeError as error:
                log.error(
                    "%s",
                    error,
                )

                failures += 1

                continue

        event = find_unique_event(
            events_cache[date_text],
            target_time,
            allowed_titles,
        )

        if event is None:
            log.warning(
                "Target lesson not found: %s",
                label,
            )

            continue

        event_id = event.get("id")

        if event_id is None:
            log.error(
                "Les heeft geen geldig ID: %s",
                label,
            )

            failures += 1

            continue

        event_id_text = str(event_id)

        title = str(
            event.get(
                "titel",
                "?",
            )
        )

        already_registered = bool(
            event.get(
                "aangemeld",
                False,
            )
        )

        already_on_waitlist = bool(
            event.get(
                "opWachtlijst",
                False,
            )
        )

        participant_count = int(
            event.get(
                "aantalDeelnemers",
                0,
            )
            or 0
        )

        maximum_participants = int(
            event.get(
                "maxDeelnemers",
                0,
            )
            or 0
        )

        if event_id_text in remembered_ids:

            if already_registered:
                log.info(
                    "Already registered and remembered: "
                    "%s [id=%s]",
                    title,
                    event_id,
                )

            else:
                log.info(
                    "Manual cancellation respected; "
                    "not registering again: "
                    "%s [id=%s]",
                    title,
                    event_id,
                )

            continue

        if already_registered:
            remembered_ids.add(
                event_id_text
            )

            save_ledger(
                remembered_ids
            )

            log.info(
                "Already registered; now remembered: "
                "%s [id=%s]",
                title,
                event_id,
            )

            continue

        if already_on_waitlist:
            remembered_ids.add(
                event_id_text
            )

            save_ledger(
                remembered_ids
            )

            log.info(
                "Already on waitlist; now remembered: "
                "%s [id=%s]",
                title,
                event_id,
            )

            continue

        if (
            maximum_participants > 0
            and participant_count >= maximum_participants
        ):
           log.warning(
                "Lesson is full. "
                "Automatic waitlist is disabled: %s",
                title,
            )

            continue

        lesson_start = get_event_start(
            event
        )

        if lesson_start is None:
            log.error(
                "Ongeldige startdatum voor "
                "%s [id=%s]",
                title,
                event_id,
            )

            failures += 1

            continue

        registration_opens = (
            lesson_start
            - timedelta(
                hours=REGISTRATION_OPENS_HOURS
            )
        )

        booking_window_ends = (
            registration_opens
            + timedelta(
                minutes=OPENING_WINDOW_MINUTES
            )
        )

        current_time = datetime.now(
            AMSTERDAM
        )

        if current_time < registration_opens:
            log.info(
                "Registration not open yet; opens at %s",
                registration_opens.isoformat(),
            )

            continue

        if current_time > booking_window_ends:
            log.info(
                "Automatic booking window passed; "
                "no registration attempted: %s",
                title,
            )

            continue

        if dry_run:
            log.info(
                "[DRY RUN] Would register: "
                "%s [id=%s]",
                title,
                event_id,
            )

            continue

        log.info(
            "Registering: %s [id=%s]",
            title,
            event_id,
        )

        success, error_message = client.signup(
            event_id
        )

        if success:
            remembered_ids.add(
                event_id_text
            )

            save_ledger(
                remembered_ids
            )

            log.info(
                "Registration successful and remembered."
            )

        else:
            failures += 1

            log.error(
                "Registration failed: %s",
                error_message,
            )

    if failures:
        return 1

    return 0


def main():
    parser = argparse.ArgumentParser(
        description=(
            "SportBit auto sign-up "
            "for De Box Bunschoten"
        )
    )

    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Werkelijk inschrijven. "
            "Zonder deze optie is het een dry-run."
        ),
    )

    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help=(
            "Aantal dagen vooruit controleren."
        ),
    )

    arguments = parser.parse_args()

    username = os.environ.get(
        "SPORTBIT_USERNAME"
    )

    password = os.environ.get(
        "SPORTBIT_PASSWORD"
    )

    if not username or not password:
        log.error(
            "SPORTBIT_USERNAME en "
            "SPORTBIT_PASSWORD ontbreken."
        )

        sys.exit(2)

    if arguments.days < 0:
        log.error(
            "--days mag niet lager zijn dan 0."
        )

        sys.exit(2)

    if arguments.days > 14:
        log.error(
            "--days mag niet hoger zijn dan 14."
        )

        sys.exit(2)

    if arguments.live:
        log.warning(
            "LIVE MODE enabled."
        )

    else:
        log.info(
            "DRY RUN enabled."
        )

    exit_code = run(
        username=username,
        password=password,
        dry_run=not arguments.live,
        days_ahead=arguments.days,
    )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
