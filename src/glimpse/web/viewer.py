"""Who is looking: an audience's shared link (/a/<slug>) or one person's link (/p/<slug>)."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import date

from ..config import AppConfig, AudienceCfg, PersonCfg


@dataclass(frozen=True)
class Viewer:
    kind: str  # "a" (audience link) or "p" (person link)
    slug: str
    audience_id: str
    audience: AudienceCfg
    person_id: str | None = None
    person: PersonCfg | None = None

    @property
    def key(self) -> str:
        """Stable id for cookies, rate limiting and "last viewed"."""
        return f"p:{self.person_id}" if self.person_id else self.audience_id

    @property
    def base(self) -> str:
        return f"/{self.kind}/{self.slug}"

    @property
    def password_hash(self) -> str | None:
        if self.person is None:
            return self.audience.password_hash
        if self.person.password_hash:
            return self.person.password_hash
        return self.audience.password_hash if self.person.use_audience_password else None

    @property
    def seen_key(self) -> str:
        return f"person:{self.person_id}" if self.person_id else f"audience:{self.audience_id}"


def audience_viewer(cfg: AppConfig, audience_id: str) -> Viewer:
    aud = cfg.audiences[audience_id]
    return Viewer("a", aud.slug, audience_id, aud)


def person_viewer(cfg: AppConfig, person_id: str) -> Viewer:
    person = cfg.people[person_id]
    return Viewer("p", person.slug, person.audience, cfg.audiences[person.audience], person_id, person)


def resolve(cfg: AppConfig, kind: str, slug: str, today: date) -> Viewer | str | None:
    """The viewer for a link, ``"expired"`` for a switched-off person link, or None."""
    found: Viewer | str | None = None
    enc = slug.encode()
    if kind == "a":
        for aid, aud in cfg.audiences.items():
            # compare every slug in constant time; don't stop early
            # your own view stays locked until it has a password
            usable = aud.shared_link and (aud.password_hash or not aud.self_view)
            if hmac.compare_digest(aud.slug.encode(), enc) and usable:
                found = Viewer("a", aud.slug, aid, aud)
    elif kind == "p":
        for pid, person in cfg.people.items():
            if hmac.compare_digest(person.slug.encode(), enc):
                found = "expired" if person.expired(today) else person_viewer(cfg, pid)
    return found
