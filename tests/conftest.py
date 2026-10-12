from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from glimpse.config import load_config
from glimpse.core import Glimpse

FIXTURES = Path(__file__).parent / "fixtures"
WEEK = date(2026, 10, 26)  # Mon Oct 26 - Sun Nov 1 2026 (US DST ends Nov 1)
PARTNER_PASSWORD = "correct horse battery"
PARTNER_SLUG = "partnerSlug_abcdefghij"
COWORKER_SLUG = "coworkerSlug_klmnopqrs"

CONFIG = """
timezone: America/New_York
refresh_minutes: 10
week_starts: monday
server:
  secret_key: ${SECRET}
  secure_cookies: false
  base_url: https://pc.example.ts.net
calendars:
  personal: { name: Personal, url: FIX/personal.ics }
  lab:      { name: Lab, url: FIX/lab.ics }
  classes:  { name: Classes, url: FIX/classes.ics, label: Class }
  family:   { name: Family, url: FIX/family.ics, color: "#2f9e44" }
audiences:
  partner:
    title: "Brandon's week"
    slug: ${PARTNER_SLUG}
    password_hash: ${PARTNER_HASH}
    default: full
    weeks_ahead: 52
    weeks_behind: 52
    calendars:
      lab: title
  coworkers:
    title: "Brandon - availability"
    slug: ${COWORKER_SLUG}
    default: busy
    weeks_ahead: 52
    weeks_behind: 52
    hours: { days: [mon, tue, wed, thu, fri], start: "08:00", end: "18:00" }
    calendars:
      lab: title
      classes: label
      family: hidden
"""


@pytest.fixture(scope="session")
def partner_hash() -> str:
    return PasswordHasher().hash(PARTNER_PASSWORD)


@pytest.fixture
def config_path(tmp_path: Path, partner_hash: str) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.replace("FIX/", str(FIXTURES) + "/"), encoding="utf-8")
    (tmp_path / ".env").write_text(
        "SECRET='" + "s" * 40 + "'\n"
        f"PARTNER_SLUG={PARTNER_SLUG}\n"
        f"COWORKER_SLUG={COWORKER_SLUG}\n"
        f"PARTNER_HASH='{partner_hash}'\n",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def cfg(config_path: Path):
    return load_config(config_path)


@pytest.fixture
def glimpse(cfg) -> Glimpse:
    g = Glimpse(cfg)
    g.cache.refresh_all()
    return g
