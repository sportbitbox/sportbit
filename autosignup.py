#!/usr/bin/env python3

import logging
import os
import sys
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger("sportbit-diagnostic")


# Alleen logische kandidaten.
# Het script voert GEEN reserveringen uit.
CANDIDATE_CLUBS = [
    "https://crossfitbunschoten.sportbitapp.nl/",
    "https://deboxbunschoten.sportbitapp.nl/",
    "https://debox.sportbitapp.nl/",
]


def try_club(base_web_url: str, username: str, password: str) -> bool:
    api_url = urljoin(base_web_url, "cbm/api/")

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "Referer": urljoin(base_web_url, "web/nl/events"),
        }
    )

    log.info("Clubadres controleren: %s", base_web_url)

    try:
        heartbeat = session.get(
            urljoin(api_url, "data/heartbeat/"),
            timeout=15,
        )
    except requests.RequestException as exc:
        log.info("Niet bereikbaar: %s", type(exc).__name__)
        return False

    if heartbeat.status_code not in (200, 204):
        log.info("Geen geldige heartbeat: HTTP %s", heartbeat.status_code)
        return False

    xsrf_token = session.cookies.get("XSRF-TOKEN")
    if xsrf_token:
        session.headers["X-XSRF-TOKEN"] = xsrf_token

    try:
        login_response = session.post(
            urljoin(api_url, "data/inloggen/"),
            json={
                "username": username,
                "password": password,
                "remember": True,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        log.info("Loginverzoek mislukt: %s", type(exc).__name__)
        return False

    if login_response.status_code != 200:
        log.info("Login niet geaccepteerd: HTTP %s", login_response.status_code)
        return False

    log.info("LOGIN GELUKT voor %s", base_web_url)

    if session.cookies.get("XSRF-TOKEN"):
        session.headers["X-XSRF-TOKEN"] = session.cookies["XSRF-TOKEN"]

    # Alleen roostergegevens uitlezen.
    # Er wordt nergens een deelname-POST uitgevoerd.
    for day_offset in range(0, 8):
        target_date = datetime.now().date() + timedelta(days=day_offset)
        date_text = target_date.strftime("%Y-%m-%d")

        for rooster_id in range(1, 6):
            try:
                response = session.get(
                    urljoin(api_url, "data/events/"),
                    params={
                        "datum": date_text,
                        "rooster": rooster_id,
                    },
                    timeout=15,
                )
            except requests.RequestException:
                continue

            if response.status_code != 200:
                continue

            try:
                data = response.json()
            except ValueError:
                continue

            events = []

            for period in ("ochtend", "middag", "avond"):
                period_events = data.get(period)

                if isinstance(period_events, list):
                    events.extend(period_events)

            if not events:
                continue

            log.info(
                "Rooster gevonden: ID %s, datum %s, aantal lessen %s",
                rooster_id,
                date_text,
                len(events),
            )

            for event in events:
                log.info(
                    "LES | id=%s | titel=%s | start=%s | deelnemers=%s/%s",
                    event.get("id", "?"),
                    event.get("titel", "?"),
                    event.get("start", "?"),
                    event.get("aantalDeelnemers", "?"),
                    event.get("maxDeelnemers", "?"),
                )

            return True

    log.info("Login werkte, maar geen lessen gevonden bij rooster-ID 1 t/m 5.")
    return True


def main():
    username = os.environ.get("SPORTBIT_USERNAME")
    password = os.environ.get("SPORTBIT_PASSWORD")

    if not username or not password:
        log.error(
            "SPORTBIT_USERNAME en SPORTBIT_PASSWORD ontbreken in GitHub Secrets."
        )
        sys.exit(1)

    for club_url in CANDIDATE_CLUBS:
        if try_club(club_url, username, password):
            log.info("Diagnose voltooid.")
            return

    log.error(
        "Geen kandidaatadres werkte. Het echte SportBit-subdomein is nog nodig."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
