#!/usr/bin/env python3
"""
SportBit Auto Sign-Up voor De Box Bunschoten.

Vaste lessen:
- Maandag 07:00 WOD
- Dinsdag 06:00 Power
- Woensdag 06:00 WOD
- Donderdag 07:00 Power
- Zaterdag 09:15 Power
- Zaterdag 09:15 Buddy Workout / Buddy WOD

Werking:
- Inschrijven opent 48 uur voor de les.
- De workflow mag maximaal 15 minuten voor opening starten.
- Voor opening wacht het script zonder inschrijfpogingen.
- Poging 1 start exact bij opening.
- Daarna volgt iedere 30 seconden een nieuwe poging.
- Het script doet maximaal 7 pogingen.
- De laatste poging vindt 3 minuten na opening plaats.
- Na een geslaagde poging stopt het script direct.
- Bij een volle les probeert het script de wachtlijst.
- Bij een HTTP 429-melding stopt het script direct.
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
# Algemene instellingen
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


# ------------------------------------------------------------
# Inschrijfinstellingen
# ------------------------------------------------------------

REGISTRATION_OPENS_HOURS = 48

# GitHub mag maximaal 15 minuten voor opening starten.
# Voor opening doet het script nog geen inschrijfpogingen.
MONITOR_BEFORE_MINUTES = 15

# Zeven pogingen:
# 1. 00:00
# 2. 00:30
# 3. 01:00
# 4. 01:30
# 5. 02:00
# 6. 02:30
# 7. 03:00

MAX_ATTEMPTS_PER_LESSON = 7

RETRY_INTERVAL_SECONDS = 30

# De laatste poging vindt drie minuten na opening plaats.
BOOKING_WINDOW_AFTER_MINUTES = 3

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
    Maak lestitels geschikt voor een veilige vergelijking.
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
            "SPORTBIT_ROOSTER_IDS moet "
            "komma-gescheiden nummers bevatten. "
            "Gebruik bijvoorbeeld 1 of 1,2."
        )

        sys.exit(2)

    if not roster_ids:
        return DEFAULT_ROSTER_IDS

    return roster_ids


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
                    "SportBitAutoSignup/4.0"
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
                "Geen enkel rooster kon "
                "worden gelezen voor "
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

        Als de les vol is, verstuurt het script hetzelfde verzoek.
        SportBit bepaalt vervolgens of inschrijving op de wachtlijst
        mogelijk is.
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
            "Meerdere passende lessen gevonden "
            "om %s voor %s. "
            "Les wordt overgeslagen.",
            target_time,
            "/".join(allowed_titles),
        )

    return None


# ------------------------------------------------------------
# Automatische uitvoering
# ------------------------------------------------------------

def run(
    username,
    password,
    dry_run,
    days_ahead,
    max_signups,
):
    client = SportBitClient(
        username,
        password,
    )

    if not client.login():
        return 1

    roster_ids = parse_roster_ids()

    events_cache = {}

    successful_signups = 0
    failures = 0

    slots = get_target_slots(
        days_ahead
    )

    for slot in slots:
        date_value = slot[0]
        target_time = slot[1]
        allowed_titles = slot[2]

        date_text = date_value.isoformat()

        label = (
            DAY_NAMES[
                date_value.weekday()
            ]
            + " "
            + date_text
            + " "
            + target_time
            + " "
            + "/".join(
                allowed_titles
            )
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
                    date_text,
                    roster_ids,
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

        event_id = event.get(
            "id"
        )

        if event_id is None:
            log.error(
                "Les heeft geen geldig ID: %s",
                label,
            )

            failures += 1

            continue

        actual_title = str(
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

        if already_registered:
            log.info(
                "Already registered: "
                "%s [id=%s]",
                actual_title,
                event_id,
            )

            continue

        if already_on_waitlist:
            log.info(
                "Already on waitlist: "
                "%s [id=%s]",
                actual_title,
                event_id,
            )

            continue

        lesson_is_full = (
            maximum_participants > 0
            and participant_count
            >= maximum_participants
        )

        if lesson_is_full:
            log.info(
                "Lesson is full. "
                "The script will try to join "
                "the waiting list: %s",
                actual_title,
            )

        lesson_start = get_event_start(
            event
        )

        if lesson_start is None:
            log.error(
                "Ongeldige startdatum voor "
                "%s [id=%s]",
                actual_title,
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

        monitor_starts = (
            registration_opens
            - timedelta(
                minutes=MONITOR_BEFORE_MINUTES
            )
        )

        final_attempt_time = (
            registration_opens
            + timedelta(
                minutes=BOOKING_WINDOW_AFTER_MINUTES
            )
        )

        current_time = datetime.now(
            AMSTERDAM
        )

        if current_time < monitor_starts:
            log.info(
                "Monitoring window not open yet. "
                "Window starts at %s.",
                monitor_starts.isoformat(),
            )

            continue

        if current_time > final_attempt_time:
            log.info(
                "Booking window closed. "
                "No registration attempted for %s.",
                actual_title,
            )

            continue

        if dry_run:
            log.info(
                "[DRY RUN] Would attempt registration "
                "7 times from %s until %s: "
                "%s [id=%s]",
                registration_opens.isoformat(),
                final_attempt_time.isoformat(),
                actual_title,
                event_id,
            )

            continue

        if successful_signups >= max_signups:
            log.error(
                "Maximum number of registrations "
                "reached: %s",
                max_signups,
            )

            break

        # Wacht tot het officiële openingsmoment.
        # Voor opening worden geen deelnameverzoeken verstuurd.
        while (
            datetime.now(AMSTERDAM)
            < registration_opens
        ):
            current_time = datetime.now(
                AMSTERDAM
            )

            seconds_remaining = (
                registration_opens
                - current_time
            ).total_seconds()

            log.info(
                "Waiting for registration to open. "
                "Approximately %.0f seconds remaining.",
                max(
                    0,
                    seconds_remaining,
                ),
            )

            time.sleep(
                min(
                    10,
                    max(
                        1,
                        seconds_remaining,
                    ),
                )
            )

        registration_succeeded = False
        last_error = ""

        # Exact zeven pogingen.
        for attempt_number in range(
            1,
            MAX_ATTEMPTS_PER_LESSON + 1,
        ):
            scheduled_attempt_time = (
                registration_opens
                + timedelta(
                    seconds=(
                        attempt_number - 1
                    )
                    * RETRY_INTERVAL_SECONDS
                )
            )

            while (
                datetime.now(AMSTERDAM)
                < scheduled_attempt_time
            ):
                seconds_to_attempt = (
                    scheduled_attempt_time
                    - datetime.now(AMSTERDAM)
                ).total_seconds()

                time.sleep(
                    min(
                        1,
                        max(
                            0.1,
                            seconds_to_attempt,
                        ),
                    )
                )

            log.info(
                "Registration attempt %s of %s "
                "for %s [id=%s].",
                attempt_number,
                MAX_ATTEMPTS_PER_LESSON,
                actual_title,
                event_id,
            )

            success, error_message = client.signup(
                event_id
            )

            if success:
                successful_signups += 1
                registration_succeeded = True

                if lesson_is_full:
                    log.info(
                        "Waiting-list request accepted "
                        "on attempt %s.",
                        attempt_number,
                    )

                else:
                    log.info(
                        "Registration successful "
                        "on attempt %s.",
                        attempt_number,
                    )

                break

            last_error = error_message

            if "HTTP 429" in error_message:
                log.warning(
                    "SportBit rate limit received. "
                    "Stopping retries for %s.",
                    actual_title,
                )

                break

            if attempt_number < MAX_ATTEMPTS_PER_LESSON:
                log.warning(
                    "Attempt %s failed: %s. "
                    "Next attempt in %s seconds.",
                    attempt_number,
                    error_message,
                    RETRY_INTERVAL_SECONDS,
                )

        if not registration_succeeded:
            log.warning(
                "No successful registration or "
                "waiting-list request after %s attempts. "
                "Last response: %s",
                MAX_ATTEMPTS_PER_LESSON,
                last_error or "none",
            )

    if failures:
        return 1

    return 0


# ------------------------------------------------------------
# Startpunt
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "SportBit Auto Sign-Up "
            "voor De Box Bunschoten"
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
        default=7,
        help=(
            "Aantal dagen vooruit controleren."
        ),
    )

    parser.add_argument(
        "--max-signups",
        type=int,
        default=6,
        help=(
            "Maximaal aantal geslaagde "
            "inschrijvingen per workflow-run."
        ),
    )

    arguments = parser.parse_args()

    username = os.environ.get(
        "SPORTBIT_USERNAME"
    )

    password =
