"""Command line: ``kalends serve | preview | check | links | new-slug | new-secret | hash-password``."""

from __future__ import annotations

import argparse
import getpass
import logging
import secrets
import sys
from datetime import date
from pathlib import Path

from .config import ConfigError, Level, load_config


def _load(args: argparse.Namespace):
    try:
        return load_config(args.config, args.env)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


def cmd_new_slug(_: argparse.Namespace) -> None:
    print(secrets.token_urlsafe(16))  # 128 bits


def cmd_new_secret(_: argparse.Namespace) -> None:
    print(secrets.token_urlsafe(48))


def cmd_hash_password(args: argparse.Namespace) -> None:
    from argon2 import PasswordHasher

    if args.password is not None:
        pw = args.password
    else:
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Repeat it: "):
            print("error: passwords did not match", file=sys.stderr)
            sys.exit(1)
    if len(pw) < 8:
        print("error: use at least 8 characters", file=sys.stderr)
        sys.exit(1)
    h = PasswordHasher().hash(pw)
    print(h)
    print("\nPaste into .env with SINGLE quotes, e.g.:", file=sys.stderr)
    print(f"PARTNER_PW_HASH='{h}'", file=sys.stderr)


def cmd_check(args: argparse.Namespace) -> None:
    from .core import Kalends, week_range, week_start

    cfg = _load(args)
    print(f"config OK: {len(cfg.calendars)} calendars, {len(cfg.audiences)} audiences")
    g = Kalends(cfg)
    g.cache.refresh_all()
    s, e = week_range(week_start(g.today(), cfg), cfg)
    events = g.events_between(s, e)
    ok = True
    for cid, st in g.cache.states.items():
        n = sum(1 for ev in events if ev.calendar_id == cid)
        if st.last_error:
            ok = False
            stale = " (serving last good copy)" if st.text else ""
            print(f"  FAIL {cid:<16} {st.last_error}{stale}")
        else:
            print(f"  ok   {cid:<16} {n} events this week")
    for aid, aud in cfg.audiences.items():
        lock = "password" if aud.password_hash else "link only"
        print(f"  audience {aid:<12} {lock:<10} default={aud.default.value}")
    sys.exit(0 if ok else 1)


def cmd_preview(args: argparse.Namespace) -> None:
    from .core import Kalends, week_start
    from .render.week import fmt_day, fmt_time

    cfg = _load(args)
    if args.audience not in cfg.audiences:
        print(f"error: no audience {args.audience!r} (have: {', '.join(cfg.audiences)})", file=sys.stderr)
        sys.exit(2)
    aud = cfg.audiences[args.audience]
    g = Kalends(cfg)
    g.cache.refresh_all()
    day = date.fromisoformat(args.week) if args.week else g.today()
    start = week_start(day, cfg)
    events = g.week(aud, start)
    print(f"{aud.title} — week of {fmt_day(start)}  [{args.audience}]")
    current = None
    for ev in events:
        d = ev.start.date()
        if d != current:
            print(f"\n{fmt_day(d)}")
            current = d
        when = "all day" if ev.all_day else f"{fmt_time(ev.start)}–{fmt_time(ev.end)}"
        extra = f"  @ {ev.location}" if ev.location else ""
        tag = "" if ev.level is Level.FULL else f"  [{ev.level.value}]"
        print(f"  {when:<20} {ev.title}{extra}{tag}")
    if not events:
        print("\n  (nothing visible this week)")


def cmd_validate(args: argparse.Namespace) -> None:
    """Load and validate the settings without contacting any calendar (used by updates)."""
    cfg = _load(args)
    print(
        f"config OK: {len(cfg.calendars)} calendars, {len(cfg.audiences)} audiences, {len(cfg.people)} people"
    )


def cmd_version(args: argparse.Namespace) -> None:
    from . import __version__

    print(__version__)


def cmd_links(args: argparse.Namespace) -> None:
    from datetime import date

    from .web.auth import feed_token
    from .web.viewer import person_viewer

    cfg = _load(args)
    base = cfg.server.base_url.rstrip("/")
    for aid, aud in cfg.audiences.items():
        print(f"{aid}:")
        print(f"  page   {base}/a/{aud.slug}")
        if aud.allow_ics:
            print(f"  feed   {base}/a/{aud.slug}/feed/{feed_token(cfg, aid)}.ics")
        print(f"  login  {'password required' if aud.password_hash else 'link only'}")
        if not aud.shared_link:
            print("  (shared link switched off: only personal links work)")
        for pid, person in cfg.people.items():
            if person.audience != aid:
                continue
            v = person_viewer(cfg, pid)
            state = "" if not person.expired(date.today()) else "  [expired/off]"
            print(f"  person {person.name}: {base}{v.base}{state}")


def _ask_new_password(minimum: int, given: str | None = None) -> str:
    if given is not None:
        pw = given
    else:
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Repeat it: "):
            print("error: passwords did not match", file=sys.stderr)
            sys.exit(1)
    if len(pw) < minimum:
        print(f"error: use at least {minimum} characters", file=sys.stderr)
        sys.exit(1)
    return pw


def cmd_init(args: argparse.Namespace) -> None:
    from argon2 import PasswordHasher

    from .config import ConfigError
    from .store import init_files

    if args.config.exists():
        print(f"{args.config} already exists; nothing to do. (Set the admin password with `admin-password`.)")
        return
    admin_hash = None
    if not args.setup_later:
        print("Creating a new Kalends setup. Choose a password for the settings panel (10+ characters).")
        admin_hash = PasswordHasher().hash(_ask_new_password(10, args.password))
    try:
        init_files(args.config, args.env, admin_hash)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"Created {args.config} and {args.env or args.config.with_name('.env')}.")
    print("Next: run `kalends open` to finish setting up in your browser.")


def cmd_admin_password(args: argparse.Namespace) -> None:
    from argon2 import PasswordHasher

    from .config import ConfigError
    from .store import ConfigStore

    store = ConfigStore(args.config, args.env)
    pw = _ask_new_password(10, args.password)
    h = PasswordHasher().hash(pw)
    try:
        store.update(
            lambda d: d.set_secret(d.section("admin"), "password_hash", h, "KALENDS_ADMIN_PASSWORD_HASH")
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    print("Admin password set. A running Kalends picks it up automatically.")


def cmd_set_base_url(args: argparse.Namespace) -> None:
    from .config import ConfigError
    from .store import ConfigStore

    url = args.url.strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        print("error: the address should start with https://", file=sys.stderr)
        sys.exit(2)
    try:
        ConfigStore(args.config, args.env).update(lambda d: d.section("server").__setitem__("base_url", url))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"Public address set to {url}")


def cmd_serve(args: argparse.Namespace) -> None:
    from .server import run

    _load(args)  # friendly error if the config is broken
    run(args.config, args.env, args.host, args.port, args.admin_port, args.no_admin)


def cmd_service(args: argparse.Namespace) -> None:
    from .service import run_service

    sys.exit(run_service(args.config, args.env))


def cmd_open(args: argparse.Namespace) -> None:
    from .service import open_panel

    _load(args)
    url = open_panel(args.config, args.env)
    print(f"Opened {url.split('?')[0]}")


def cmd_autostart(args: argparse.Namespace) -> None:
    from . import autostart

    if args.action == "on":
        st = autostart.enable(args.config)
        autostart.add_start_menu_entry(args.config)
    elif args.action == "off":
        st = autostart.disable()
    else:
        st = autostart.status()
    if not st.supported:
        print("Automatic start isn't supported on this system.")
        sys.exit(1)
    print(("On: " if st.enabled else "Off: ") + st.detail + (f" ({st.where})" if st.enabled else ""))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines would print feed URLs
    p = argparse.ArgumentParser(prog="kalends", description="Share a filtered view of your calendars.")
    p.add_argument("-c", "--config", default="config.yaml", type=Path, help="path to config.yaml")
    p.add_argument("-e", "--env", default=None, type=Path, help="path to .env (default: beside config)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the public pages and the settings panel")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--admin-port", type=int)
    s.add_argument("--no-admin", action="store_true", help="don't start the settings panel")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("init", help="create config.yaml and .env for a fresh setup")
    s.add_argument("--password", help=argparse.SUPPRESS)
    s.add_argument(
        "--setup-later", action="store_true", help="choose the admin password in the browser instead"
    )
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("open", help="open the settings panel (starts Kalends in the background if needed)")
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("service", help="run in the background, logging to logs/kalends.log")
    s.set_defaults(fn=cmd_service)

    s = sub.add_parser("autostart", help="start Kalends automatically when you sign in")
    s.add_argument("action", choices=["on", "off", "status"])
    s.set_defaults(fn=cmd_autostart)

    s = sub.add_parser("set-base-url", help="set the public address used in share links")
    s.add_argument("url")
    s.set_defaults(fn=cmd_set_base_url)

    s = sub.add_parser("admin-password", help="set the settings-panel password")
    s.add_argument("--password", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_admin_password)

    s = sub.add_parser("preview", help="print what an audience sees for a week")
    s.add_argument("--audience", "-a", required=True)
    s.add_argument("--week", "-w", help="any date in the week, YYYY-MM-DD (default: this week)")
    s.set_defaults(fn=cmd_preview)

    sub.add_parser("check", help="validate config and fetch every feed").set_defaults(fn=cmd_check)
    sub.add_parser("validate", help="validate config without fetching anything").set_defaults(fn=cmd_validate)
    sub.add_parser("version", help="print the installed version").set_defaults(fn=cmd_version)
    sub.add_parser("links", help="print each audience's links").set_defaults(fn=cmd_links)
    sub.add_parser("new-slug", help="make a random link slug").set_defaults(fn=cmd_new_slug)
    sub.add_parser("new-secret", help="make a server secret key").set_defaults(fn=cmd_new_secret)
    s = sub.add_parser("hash-password", help="hash a password for an audience")
    s.add_argument("--password", help=argparse.SUPPRESS)  # for scripts/tests only
    s.set_defaults(fn=cmd_hash_password)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
