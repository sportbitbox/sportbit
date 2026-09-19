#!/usr/bin/env python3
"""
SportBit Auto Sign-Up for De Box Bunschoten.

Targets:
- Monday 07:00: WOD
- Tuesday 06:00: Strength
- Thursday 07:00: Power
- Saturday 09:15: Power
- Saturday 09:15: Buddy Workout / Buddy WOD

Dry-run is the default. Use --live to actually register.
Credentials must be supplied through SPORTBIT_USERNAME and SPORTBIT_PASSWORD.
"""

import argparse
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

BASE_WEB_URL = "https://deboxbunschoten.sportbitapp.nl/"
BASE_API_URL = urljoin(BASE_WEB_URL, "cbm/api/")
REFERER = urljoin(BASE_WEB_URL, "web/nl/events")

AMSTERDAM = ZoneInfo("Europe/Amsterdam")

REGISTRATION_OPENS_HOURS = 48
WINDOW_BEFORE_MINUTES = 15
WINDOW_AFTER_MINUTES = 15

# Try several roster IDs because the public portal does not expose De Box's ID.
# Set SPORTBIT_ROOSTER_IDS, for example "1" or "1,2", to override this list.
DEFAULT_ROOSTER_IDS = tuple(range(1, 11))

# (weekday, time, accepted exact titles)
# Weekday: Monday=0 ... Sunday=6
SCHEDULE = [
    (0, "07:00", ("WOD",)),
    (1, "06:00", ("Strength",)),
    (3, "07:00", ("Power",)),
    (5, "09:15", ("Power",)),
    (5, "09:15", ("Buddy Workout", "Buddy WOD")),
]

DAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sportbit-debox")


def normalize(value: str) -> str:
    """Normalize a lesson title for safe exact comparison."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return re.sub(r"\s+", " ", text)


def parse_roster_ids() -> tuple[int, ...]:
    raw = os.environ.get("SPORTBIT_ROOSTER_IDS", "").strip()
    if not raw:
        return DEFAULT_ROOSTER_IDS
    try:
        ids = tuple(dict.fromkeys(int(part.strip()) for part in raw.split(",") if part.strip()))
    except ValueError:
        log.error("SPORTBIT_ROOSTER_IDS must contain comma-separated numbers, e.g. 1 or 1,2.")
        sys.exit(2)
    if not ids:
        log.error("SPORTBIT_ROOSTER_IDS contains no roster IDs.")
        sys.exit(2)
    return ids


class SportBitClient:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "Referer": REFERER,
        })

    def _url(self, path: str) -> str:
        return urljoin(BASE_API_URL, path)

    def _set_xsrf_header(self) -> None:
        token = self.session.cookies.get("XSRF-TOKEN")
        if token:
            self.session.headers["X-XSRF-TOKEN"] = token

    def login(self) -> bool:
        log.info("Logging in to De Box Bunschoten SportBit portal...")
        try:
            heartbeat = self.session.get(self._url("data/heartbeat/"), timeout=20)
            heartbeat.raise_for_status()
            self._set_xsrf_header()
            response = self.session.post(
                self._url("data/inloggen/"),
                json={
                    "username": self.username,
                    "password": self.password,
                    "remember": True,
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            log.error("Login request failed: %s", exc)
            return False

        if response.status_code == 200:
            self._set_xsrf_header()
            log.info("Login successful.")
            return True

        log.error("Login failed (HTTP %s): %s", response.status_code, response.text[:200])
        return False

    def get_events(self, date_text: str, roster_id: int) -> list[dict]:
        response = self.session.get(
            self._url("data/events/"),
            params={"datum": date_text, "rooster": roster_id},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        events: list[dict] = []
        for period in ("ochtend", "middag", "avond"):
            period_events = data.get(period)
            if isinstance(period_events, list):
                events.extend(period_events)
        return events

    def get_events_all_rosters(self, date_text: str, roster_ids: tuple[int, ...]) -> list[dict]:
        """Collect and deduplicate events from candidate rosters."""
        by_id: dict[str, dict] = {}
        successful_rosters = 0
        for roster_id in roster_ids:
            try:
                events = self.get_events(date_text, roster_id)
                successful_rosters += 1
            except (requests.RequestException, ValueError) as exc:
                log.warning("Could not read roster %s for %s: %s", roster_id, date_text, exc)
                continue
            for event in events:
                event_id = str(event.get("id", ""))
                if event_id:
                    by_id[event_id] = event
        if successful_rosters == 0:
            raise RuntimeError(f"No roster could be read for {date_text}.")
        return list(by_id.values())

    def signup(self, event_id: int) -> tuple[bool, str]:
        self._set_xsrf_header()
        try:
            response = self.session.post(
                self._url(f"data/events/{event_id}/deelname/"),
                json={},
                timeout=20,
            )
        except requests.RequestException as exc:
            return False, str(exc)
        if response.status_code in (200, 204):
            return True, ""
        return False, f"HTTP {response.status_code}: {response.text[:300]}"


def target_slots(days_ahead: int) -> list[tuple]:
    today = datetime.now().date()
    slots = []
    for offset in range(days_ahead + 1):
        date_value = today + timedelta(days=offset)
        for weekday, target_time, titles in SCHEDULE:
            if date_value.weekday() == weekday:
                slots.append((date_value, target_time, titles))
    return slots


def event_start_datetime(event: dict):
    start = str(event.get("start", ""))

    try:
        start_datetime = datetime.fromisoformat(start)
    except ValueError:
        return None

    if start_datetime.tzinfo is None:
        start_datetime = start_datetime.replace(
            tzinfo=AMSTERDAM
        )

    return start_datetime.astimezone(
        AMSTERDAM
    )


def event_start_time(event: dict) -> str:
    start_datetime = event_start_datetime(event)

    if start_datetime is None:
        return ""

    return start_datetime.strftime("%H:%M")

def find_unique_event(events: list[dict], target_time: str, titles: tuple[str, ...]) -> dict | None:
    accepted = {normalize(title) for title in titles}
    matches = [
        event for event in events
        if event_start_time(event) == target_time
        and normalize(str(event.get("titel", ""))) in accepted
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        details = ", ".join(
            f"{event.get('titel', '?')} [id={event.get('id', '?')}]" for event in matches
        )
        log.error("Ambiguous match at %s for %s: %s", target_time, "/".join(titles), details)
    return None


def run(username: str, password: str, dry_run: bool, days_ahead: int, max_signups: int) -> int:
    client = SportBitClient(username, password)
    if not client.login():
        return 1

    roster_ids = parse_roster_ids()
    log.info("Checking roster IDs: %s", ", ".join(map(str, roster_ids)))

    slots = target_slots(days_ahead)
    cache: dict[str, list[dict]] = {}
    successful_signups = 0
    failures = 0

    results = {
        "would_signup": [], "signed_up": [], "already": [],
        "waitlist": [], "full": [], "not_found": [], "failed": [],
    }

    for date_value, target_time, titles in slots:
        date_text = date_value.isoformat()
        target_name = "/".join(titles)
        label = f"{DAY_NAMES[date_value.weekday()]} {date_text} {target_time} {target_name}"
        log.info("--- %s ---", label)

        if date_text not in cache:
            try:
                cache[date_text] = client.get_events_all_rosters(date_text, roster_ids)
            except RuntimeError as exc:
                log.error("%s", exc)
                results["failed"].append(label)
                failures += 1
                continue

        event = find_unique_event(cache[date_text], target_time, titles)
        if not event:
            log.warning("Target lesson not found: %s", label)
            results["not_found"].append(label)
            continue

        event_id = event.get("id")
        actual_title = str(event.get("titel", "?"))
        already = bool(event.get("aangemeld", False))
        on_waitlist = bool(event.get("opWachtlijst", False))
        participants = int(event.get("aantalDeelnemers", 0) or 0)
        capacity = int(event.get("maxDeelnemers", 0) or 0)
        spots = f"{participants}/{capacity}" if capacity else str(participants)

        if already:
            log.info("Already registered: %s (%s) [id=%s]", actual_title, spots, event_id)
            results["already"].append(label)
            continue

        if on_waitlist:
            log.info("Already on waitlist: %s (%s) [id=%s]", actual_title, spots, event_id)
            results["waitlist"].append(label)
            continue

        # Niet automatisch inschrijven op een wachtlijst.
        if capacity > 0 and participants >= capacity:
            log.warning(
                "Lesson is full; skipping automatic waitlist: %s (%s)",
                actual_title,
                spots,
            )
            results["full"].append(label)
            continue

        lesson_start = event_start_datetime(event)

        if lesson_start is None:
            log.error(
                "Invalid lesson start date: %s [id=%s]",
                actual_title,
                event_id,
            )
            results["failed"].append(label)
            failures += 1
            continue

        registration_opens = (
            lesson_start
            - timedelta(hours=REGISTRATION_OPENS_HOURS)
        )

        booking_window_starts = (
            registration_opens
            - timedelta(minutes=WINDOW_BEFORE_MINUTES)
        )

        booking_window_ends = (
            registration_opens
            + timedelta(minutes=WINDOW_AFTER_MINUTES)
        )

        current_time = datetime.now(AMSTERDAM)

        if current_time < booking_window_starts:
            log.info(
                "Booking window not open yet. Window: %s until %s",
                booking_window_starts.isoformat(),
                booking_window_ends.isoformat(),
            )
            continue

        if current_time > booking_window_ends:
            log.info(
                "Booking window closed. No registration attempted for %s.",
                actual_title,
            )
            continue

        log.info(
            "Inside booking window for %s: %s until %s",
            actual_title,
            booking_window_starts.isoformat(),
            booking_window_ends.isoformat(),
        )

        if dry_run:
            log.info("[DRY RUN] Would register: %s (%s) [id=%s]", actual_title, spots, event_id)
            results["would_signup"].append(label)
            continue

        if successful_signups >= max_signups:
            log.error("Safety limit of %s successful registrations reached; stopping.", max_signups)
            results["failed"].append(label)
            failures += 1
            break

        log.info("Registering: %s (%s) [id=%s]", actual_title, spots, event_id)
        ok, error = client.signup(int(event_id))
        if ok:
            successful_signups += 1
            results["signed_up"].append(label)
            log.info("Registration successful.")
        else:
            failures += 1
            results["failed"].append(label)
            log.error("Registration failed: %s", error)

    log.info("=== Summary ===")
    for key, values in results.items():
        if values:
            log.info("%-14s %s", key + ":", " | ".join(values))

    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="SportBit auto sign-up for De Box Bunschoten")
    parser.add_argument("--live", action="store_true", help="Actually register; default is dry-run")
    parser.add_argument("--days", type=int, default=3, help="Days ahead to inspect, default 3")
    parser.add_argument("--max-signups", type=int, default=5, help="Safety maximum per run, default 5")
    args = parser.parse_args()

    username = os.environ.get("SPORTBIT_USERNAME")
    password = os.environ.get("SPORTBIT_PASSWORD")
    if not username or not password:
        log.error("Set SPORTBIT_USERNAME and SPORTBIT_PASSWORD as environment variables/GitHub Secrets.")
        sys.exit(2)
    if args.days < 0 or args.days > 14:
        log.error("--days must be between 0 and 14.")
        sys.exit(2)
    if args.max_signups < 1 or args.max_signups > 5:
        log.error("--max-signups must be between 1 and 5.")
        sys.exit(2)

    dry_run = not args.live
    if dry_run:
        log.info("DRY RUN: no registrations will be made.")
    else:
        log.warning("LIVE MODE: matching lessons can be registered.")

    sys.exit(run(username, password, dry_run, args.days, args.max_signups))


if __name__ == "__main__":
    main()
