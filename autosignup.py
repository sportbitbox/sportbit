#!/usr/bin/env python3
"""
Automatische SportBit-inschrijving voor De Box Bunschoten.

Vaste lessen:
- Maandag 07:00: WOD
- Dinsdag 06:00: Strength
- Woensdag 06:00: WOD
- Donderdag 07:00: Power
- Zaterdag 09:15: Power
- Zaterdag 09:15: Buddy Workout / Buddy WOD

Werking:
- Inschrijving opent 48 uur voor de les.
- Het script boekt alleen tijdens het eerste uur na opening.
- Volle lessen worden overgeslagen.
- Er wordt niet automatisch op een wachtlijst ingeschreven.
- Een eenmaal geboekte of aangetroffen inschrijving wordt onthouden.
- Als je daarna handmatig uitschrijft, wordt dezelfde les niet opnieuw geboekt.
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


# --------------------------------------------------------------
# Instellingen
# --------------------------------------------------------------

BASE_WEB_URL = "https://deboxbunschoten.sportbitapp.nl/"
BASE_API_URL = urljoin(BASE_WEB_URL, "cbm/api/")
REFERER = urljoin(BASE_WEB_URL, "web/nl/events")

AMSTERDAM = ZoneInfo("Europe/Amsterdam")

LEDGER_FILE = Path("booked_events.json")

REGISTRATION_OPENS_HOURS = 48

# GitHub Actions kan enkele minuten vertraagd starten.
# Daarom mag het script maximaal 60 minuten na opening boeken.
OPENING_WINDOW_MINUTES = 60

# De werkende dry-run vond lessen via deze roosterzoekmethode.
DEFAULT_ROSTER_IDS = tuple(range(1, 11))


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
    (1, "06:00", ("Strength",)),
    (2, "06:00", ("WOD",)),
    (3, "07:00", ("Power",)),
    (5, "09:15", ("Power",)),
    (5, "09:15", ("Buddy Workout", "Buddy WOD")),
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


# --------------------------------------------------------------
# Logging
# --------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger("sportbit-debox")


# --------------------------------------------------------------
# Hulpfuncties
# --------------------------------------------------------------

def normalize(value: str) -> str:
    """
    Maak lestitels vergelijkbaar.

    Voorbeelden:
    'Buddy Workout' en ' buddy workout ' worden gelijk behandeld.
    """

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

    return re.sub(r"\s+", " ", text)


def parse_roster_ids() -> tuple[int, ...]:
    """
    Lees optioneel SPORTBIT_ROOSTER_IDS.

    Zonder aparte instelling worden rooster-ID's 1 tot en met 10 bekeken.
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
                int(value.strip())
                for value in raw_value.split(",")
                if value.strip()
            )
        )
    except ValueError:
        log.error(
            "SPORTBIT_ROOSTER_IDS moet nummers bevatten, "
            "bijvoorbeeld 1 of 1,2."
        )
        sys.exit(2)

    if not roster_ids:
        return DEFAULT_ROSTER_IDS

    return roster_ids


# --------------------------------------------------------------
# Boekingsgeheugen
# --------------------------------------------------------------

def load_ledger() -> set"""
    Lees les-ID's die eerder geboekt of aangetroffen zijn.

    Als een ID in dit geheugen staat, wordt die les nooit opnieuw geboekt.
    Hierdoor blijft een latere handmatige uitschrijving intact.
    """

    if not LEDGER_FILE.exists():
        return set()

    try:
        data = json.loads(
            LEDGER_FILE.read_text(
                encoding="utf-8",
            )
        )
    except (OSError, ValueError, TypeError) as error:
        log.error(
            "Kan boekingsgeheugen %s niet lezen: %s",
            LEDGER_FILE,
            error,
        )
        sys.exit(2)

    remembered_ids = data.get(
        "booked_event_ids",
        [],
    )

    if not isinstance(remembered_ids, list):
        log.error(
            "Ongeldige inhoud in %s.",
            LEDGER_FILE,
        )
        sys.exit(2)

    return {
        str(event_id)
        for event_id in remembered_ids
    }


def save_ledger(event_ids: set[str]) -> None:
    """
    Sla de onthouden SportBit-les-ID's op.
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


# --------------------------------------------------------------
# SportBit-client
# --------------------------------------------------------------

class SportBitClient:
    def __init__(
        self,
        username: str,
        password: str,
    ):

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

    def _url(
        self,
        path: str,
    ) -> str:

        return urljoin(
            BASE_API_URL,
            path,
        )

    def _set_xsrf_header(self) -> None:
        """
        Neem het XSRF-token uit de sessiecookie over.
        """

        token = self.session.cookies.get(
            "XSRF-TOKEN"
        )

        if token:
            self.session.headers[
                "X-XSRF-TOKEN"
            ] = token

    def login(self) -> bool:
        """
        Log in bij het ledenportaal van De Box Bunschoten.
        """

        log.info(
            "Logging in to De Box Bunschoten SportBit portal..."
        )

        try:
            heartbeat_response = self.session.get(
                self._url("data/heartbeat/"),
                timeout=20,
            )

            heartbeat_response.raise_for_status()

            self._set_xsrf_header()

            login_response = self.session.post(
                self._url("data/inloggen/"),
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

        if login_response.status_code == 200:
            self._set_xsrf_header()

            log.info(
                "Login successful."
            )

            return True

        log.error(
            "Login failed (HTTP %s): %s",
            login_response.status_code,
            login_response.text[:200],
        )

        return False

    def get_events(
        self,
        date_text: str,
        roster_id: int,
    ) -> list"""
        Haal alle lessen voor een datum en rooster-ID op.
        """

        response = self.session.get(
            self._url("data/events/"),
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
            period_events = data.get(period)

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
        date_text: str,
        roster_ids: tuple[int, ...],
    ) -> list"""
        Bekijk meerdere mogelijke roosters en verwijder dubbele les-ID's.
        """

        events_by_id = {}

        successfully_read_rosters = 0

        for roster_id in roster_ids:

            try:
                events = self.get_events(
                    date_text,
                    roster_id,
                )

                successfully_read_rosters += 1

            except (
                requests.RequestException,
                ValueError,
            ) as error:

                log.warning(
                    "Could not read roster %s for %s: %s",
                    roster_id,
                    date_text,
                    error,
                )

                continue

            for event in events:
                event_id = str(
                    event.get(
                        "id",
                        "",
                    )
                )

                if event_id:
                    events_by_id[
                        event_id
                    ] = event

        if successfully_read_rosters == 0:
            raise RuntimeError(
                f"No roster could be read for {date_text}."
            )

        return list(
            events_by_id.values()
        )

    def signup(
        self,
        event_id: int,
    ) -> tuple[bool, str]:
        """
        Schrijf in voor één SportBit-les.
        """

        self._set_xsrf_header()

        try:
            response = self.session.post(
                self._url(
                    f"data/events/{event_id}/deelname/"
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

        return (
            False,
            (
                f"HTTP {response.status_code}: "
                f"{response.text[:300]}"
            ),
        )


# --------------------------------------------------------------
# Lesselectie
# --------------------------------------------------------------

def target_slots(
    days_ahead: int,
) -> list"""
    Maak een lijst met gewenste lessen binnen de zoekperiode.
    """

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

        for (
            weekday,
            target_time,
            titles,
        ) in SCHEDULE:

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


def event_start_datetime(
    event: dict,
) -> datetime | None:
    """
    Lees de startdatum en starttijd van een SportBit-les.
    """

    start_value = str(
        event.get(
            "start",
            "",
        )
    )

    try:
        parsed_start = datetime.fromisoformat(
            start_value
        )
    except ValueError:
        return None

    if parsed_start.tzinfo is None:
        parsed_start = parsed_start.replace(
            tzinfo=AMSTERDAM
        )

    return parsed_start.astimezone(
        AMSTERDAM
    )


def find_unique_event(
    events: list[dict],
    target_time: str,
    allowed_titles: tuple[str, ...],
) -> dict | None:
    """
    Zoek exact één les op tijd en toegestane titel.
    """

    normalized_titles = {
        normalize(title)
        for title in allowed_titles
    }

    matches = []

    for event in events:
        start_datetime = event_start_datetime(
            event
        )

        event_title = normalize(
            str(
                event.get(
                    "titel",
                    "",
                )
            )
        )

        if start_datetime is None:
            continue

        if (
            start_datetime.strftime(
                "%H:%M"
            )
            != target_time
        ):
            continue

        if event_title not in normalized_titles:
            continue

        matches.append(event)

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        match_details = ", ".join(
            (
                f"{event.get('titel', '?')} "
                f"[id={event.get('id', '?')}]"
            )
            for event in matches
        )

        log.error(
            "Multiple matching lessons at %s for %s: %s",
            target_time,
            "/".join(allowed_titles),
            match_details,
        )

    return None


# --------------------------------------------------------------
# Hoofdprogramma
# --------------------------------------------------------------

def run(
    username: str,
    password: str,
    dry_run: bool,
    days_ahead: int,
) -> int:

    client = SportBitClient(
        username,
        password,
    )

    if not client.login():
        return 1

    roster_ids = parse_roster_ids()

    remembered_event_ids = load_ledger()

    events_cache = {}

    failures = 0

    log.info(
        "Checking roster IDs: %s",
        ", ".join(
            str(roster_id)
            for roster_id in roster_ids
        ),
    )

    for (
        date_value,
        target_time,
        allowed_titles,
    ) in target_slots(days_ahead):

        date_text = date_value.isoformat()

        label = (
            f"{DAY_NAMES[date_value.weekday()]} "
            f"{date_text} "
            f"{target_time} "
            f"{'/'.join(allowed_titles)}"
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

        if not event:
            log.warning(
                "Target lesson not found: %s",
                label,
            )
            continue

        event_id = event.get("id")

        if event_id is None:
            log.error(
                "Event has no ID: %s",
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

        # Als de automatisering deze les eerder heeft onthouden,
        # wordt nooit opnieuw ingeschreven.
        if (
            event_id_text
            in remembered_event_ids
        ):
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

        # Ook een reeds bestaande inschrijving wordt onthouden.
        # Daardoor wordt een latere handmatige uitschrijving gerespecteerd.
        if already_registered:
            remembered_event_ids.add(
                event_id_text
            )

            save_ledger(
                remembered_event_ids
            )

            log.info(
                "Already registered; now remembered: "
                "%s [id=%s]",
                title,
                event_id,
            )

            continue

        # Ook een bestaande wachtlijstdeelname wordt onthouden.
        if already_on_waitlist:
            remembered_event_ids.add(
                event_id_text
            )

            save_ledger(
                remembered_event_ids
            )

            log.info(
                "Already on waitlist; now remembered: "
                "%s [id=%s]",
                title,
                event_id,
            )

            continue

        # Niet automatisch op de wachtlijst inschrijven.
        if (
            maximum_participants > 0
            and
            participant_count
            >= maximum_participants
        ):
            log.warning(
                "Lesson is full; "
                "automatic waitlist is disabled: %s",
                title,
            )

            continue

        lesson_start = event_start_datetime(
            event
        )

        if lesson_start is None:
            log.error(
                "Invalid start date for "
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
            int(event_id)
        )

        if success:
            remembered_event_ids.add(
                event_id_text
            )

            save_ledger(
                remembered_event_ids
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

    return 1 if failures else 0


def main() -> None:

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
            "Actually register. "
            "Default is dry-run."
        ),
    )

    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help=(
            "Number of days ahead to inspect."
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
            "SPORTBIT_USERNAME and "
            "SPORTBIT_PASSWORD are required."
        )
        sys.exit(2)

    if (
        arguments.days < 0
        or
        arguments.days > 14
    ):
        log.error(
            "--days must be between 0 and 14."
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
