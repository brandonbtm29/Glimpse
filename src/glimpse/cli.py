"""Command line: ``glimpse serve | preview | check | links | new-slug | new-secret | hash-password``."""

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
    from .core import Glimpse, week_range, week_start

    cfg = _load(args)
    print(f"config OK: {len(cfg.calendars)} calendars, {len(cfg.audiences)} audiences")
    g = Glimpse(cfg)
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
    from .core import Glimpse, week_start
    from .render.week import fmt_day, fmt_time

    cfg = _load(args)
    if args.audience not in cfg.audiences:
        print(f"error: no audience {args.audience!r} (have: {', '.join(cfg.audiences)})", file=sys.stderr)
        sys.exit(2)
    aud = cfg.audiences[args.audience]
    g = Glimpse(cfg)
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


def cmd_links(args: argparse.Namespace) -> None:
    from .web.auth import feed_token

    cfg = _load(args)
    base = cfg.server.base_url.rstrip("/")
    for aid, aud in cfg.audiences.items():
        print(f"{aid}:")
        print(f"  page   {base}/a/{aud.slug}")
        if aud.allow_ics:
            print(f"  feed   {base}/a/{aud.slug}/feed/{feed_token(cfg, aid)}.ics")
        print(f"  login  {'password required' if aud.password_hash else 'link only'}")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .web.app import create_app

    cfg = _load(args)
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"warning: listening on {host}; anyone on that network can reach Glimpse directly. "
            "Use 127.0.0.1 with a tunnel instead.",
            file=sys.stderr,
        )
    app = create_app(cfg)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        proxy_headers=cfg.server.trust_proxy_headers,
        forwarded_allow_ips="127.0.0.1",
    )


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines would print feed URLs
    p = argparse.ArgumentParser(prog="glimpse", description="Share a filtered view of your calendars.")
    p.add_argument("-c", "--config", default="config.yaml", type=Path, help="path to config.yaml")
    p.add_argument("-e", "--env", default=None, type=Path, help="path to .env (default: beside config)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("preview", help="print what an audience sees for a week")
    s.add_argument("--audience", "-a", required=True)
    s.add_argument("--week", "-w", help="any date in the week, YYYY-MM-DD (default: this week)")
    s.set_defaults(fn=cmd_preview)

    sub.add_parser("check", help="validate config and fetch every feed").set_defaults(fn=cmd_check)
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
