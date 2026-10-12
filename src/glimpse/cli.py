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
    print("Creating a new Glimpse setup. Choose a password for the settings panel (10+ characters).")
    pw = _ask_new_password(10, args.password)
    try:
        init_files(args.config, args.env, PasswordHasher().hash(pw))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"Created {args.config} and {args.env or args.config.with_name('.env')}.")
    print("Next: run `glimpse serve` and open http://127.0.0.1:8001 to add calendars.")


def cmd_admin_password(args: argparse.Namespace) -> None:
    from argon2 import PasswordHasher

    from .config import ConfigError
    from .store import ConfigStore

    store = ConfigStore(args.config, args.env)
    pw = _ask_new_password(10, args.password)
    h = PasswordHasher().hash(pw)
    try:
        store.update(
            lambda d: d.set_secret(d.section("admin"), "password_hash", h, "GLIMPSE_ADMIN_PASSWORD_HASH")
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    print("Admin password set. A running Glimpse picks it up automatically.")


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
    import asyncio

    import uvicorn

    from .store import ConfigStore
    from .web.app import create_app
    from .web.runtime import Runtime

    cfg = _load(args)
    store = ConfigStore(args.config, args.env)
    rt = Runtime(cfg, store)
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    for h in (host, cfg.admin.host):
        if h not in ("127.0.0.1", "localhost", "::1"):
            print(
                f"warning: listening on {h}; anyone on that network can reach it directly. "
                "Use 127.0.0.1 with Tailscale instead.",
                file=sys.stderr,
            )

    servers = [
        uvicorn.Server(
            uvicorn.Config(
                create_app(cfg, glimpse=rt.glimpse, background_refresh=False),
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                proxy_headers=cfg.server.trust_proxy_headers,
                forwarded_allow_ips="127.0.0.1",
            )
        )
    ]
    log = logging.getLogger("glimpse")
    if cfg.admin.enabled and not args.no_admin:
        from .web.admin import create_admin_app

        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    create_admin_app(rt),
                    host=cfg.admin.host,
                    port=args.admin_port or cfg.admin.port,
                    log_level="warning",
                    access_log=False,
                    proxy_headers=True,
                    forwarded_allow_ips="127.0.0.1",
                )
            )
        )
        if not cfg.admin.password_hash:
            log.warning("settings panel is locked until you run `glimpse admin-password`")
    log.info("public pages on http://%s:%s", host, port)
    if len(servers) > 1:
        log.info("settings panel on http://%s:%s", cfg.admin.host, args.admin_port or cfg.admin.port)

    async def run() -> None:
        refresher = asyncio.create_task(rt.refresh_loop())
        try:
            await asyncio.gather(*(s.serve() for s in servers))
        finally:
            refresher.cancel()

    asyncio.run(run())


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines would print feed URLs
    p = argparse.ArgumentParser(prog="glimpse", description="Share a filtered view of your calendars.")
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
    s.set_defaults(fn=cmd_init)

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
