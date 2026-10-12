"""Read and write ``config.yaml`` + ``.env`` on behalf of the settings panel.

Rules:

* Secrets (feed links, link slugs, password hashes, keys) always live in ``.env``;
  ``config.yaml`` only holds ``${NAME}`` references to them.
* Every change is validated in memory first. Nothing is written unless the
  resulting config is valid.
* Writes are atomic (temp file + rename) and the previous version of each file
  is kept as ``*.bak``.
"""

from __future__ import annotations

import copy
import os
import re
import secrets
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .config import (
    AppConfig,
    ConfigError,
    build_config,
    default_env_path,
    read_env_file,
    read_raw_config,
)

_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_ID = re.compile(r"[^a-z0-9]+")

Mutation = Callable[["Draft"], None]


def make_id(name: str, taken: set[str] | dict[str, Any]) -> str:
    base = _ID.sub("_", name.lower()).strip("_")[:24] or "item"
    if base[0].isdigit():
        base = "c_" + base
    cand, n = base, 2
    while cand in taken:
        cand, n = f"{base}_{n}", n + 1
    return cand


def _env_name(*parts: str) -> str:
    return "GLIMPSE_" + "_".join(re.sub(r"[^A-Z0-9]+", "_", p.upper()).strip("_") for p in parts)


class Draft:
    """A mutable copy of the raw config plus pending ``.env`` changes."""

    def __init__(self, raw: dict[str, Any], env: dict[str, str]):
        self.raw = copy.deepcopy(raw)
        self.env = dict(env)
        self.env_changes: dict[str, str | None] = {}

    # -- secrets -------------------------------------------------------------

    def get_secret(self, node: dict[str, Any], key: str) -> str | None:
        value = node.get(key)
        if isinstance(value, str) and (m := _REF.match(value)):
            return self.env.get(m.group(1))
        return value

    def set_secret(self, node: dict[str, Any], key: str, value: str | None, var: str) -> None:
        """Store ``value`` in .env and point ``node[key]`` at it."""
        current = node.get(key)
        if isinstance(current, str) and (m := _REF.match(current)):
            var = m.group(1)
        if value is None:
            node[key] = None
            self._set_env(var, None)
            return
        node[key] = "${" + var + "}"
        self._set_env(var, value)

    def drop_secret(self, node: dict[str, Any], key: str) -> None:
        current = node.get(key)
        if isinstance(current, str) and (m := _REF.match(current)):
            self._set_env(m.group(1), None)

    def _set_env(self, var: str, value: str | None) -> None:
        self.env_changes[var] = value
        if value is None:
            self.env.pop(var, None)
        else:
            self.env[var] = value

    # -- sections ------------------------------------------------------------

    def section(self, key: str) -> dict[str, Any]:
        node = self.raw.get(key)
        if not isinstance(node, dict):
            node = {}
            self.raw[key] = node
        return node

    @property
    def calendars(self) -> dict[str, Any]:
        return self.section("calendars")

    @property
    def audiences(self) -> dict[str, Any]:
        return self.section("audiences")

    # -- high-level edits used by the panel ----------------------------------

    def add_calendar(self, name: str, url: str, label: str | None = None, color: str | None = None) -> str:
        cid = make_id(name, self.calendars)
        node: dict[str, Any] = {"name": name.strip()}
        self.calendars[cid] = node
        self.set_secret(node, "url", url.strip(), _env_name("CAL", cid, "URL"))
        if label:
            node["label"] = label.strip()
        if color:
            node["color"] = color.strip()
        return cid

    def remove_calendar(self, cid: str) -> None:
        node = self.calendars.pop(cid, None)
        if node is None:
            return
        self.drop_secret(node, "url")
        for aud in self.audiences.values():
            if isinstance(aud, dict) and isinstance(aud.get("calendars"), dict):
                aud["calendars"].pop(cid, None)

    def add_audience(self, title: str, default: str = "busy") -> str:
        aid = make_id(title, self.audiences)
        node: dict[str, Any] = {"title": title.strip(), "default": default, "calendars": {}}
        self.audiences[aid] = node
        self.set_secret(node, "slug", new_slug(), _env_name("AUD", aid, "SLUG"))
        node["password_hash"] = None
        return aid

    def remove_audience(self, aid: str) -> None:
        node = self.audiences.pop(aid, None)
        if node is None:
            return
        self.drop_secret(node, "slug")
        self.drop_secret(node, "password_hash")

    def rotate_slug(self, aid: str) -> None:
        node = self.audiences[aid]
        self.set_secret(node, "slug", new_slug(), _env_name("AUD", aid, "SLUG"))

    def set_audience_password(self, aid: str, password_hash: str | None) -> None:
        node = self.audiences[aid]
        if password_hash is None:
            self.drop_secret(node, "password_hash")
            node["password_hash"] = None
        else:
            self.set_secret(node, "password_hash", password_hash, _env_name("AUD", aid, "PASSWORD_HASH"))


def new_slug() -> str:
    return secrets.token_urlsafe(16)


class ConfigStore:
    def __init__(self, config_path: str | os.PathLike[str], env_path: str | os.PathLike[str] | None = None):
        self.config_path = Path(config_path)
        self.env_path = Path(env_path) if env_path else default_env_path(self.config_path)
        self._lock = threading.Lock()

    # -- reading -------------------------------------------------------------

    def _env_for_build(self, file_env: dict[str, str]) -> dict[str, str]:
        env = dict(file_env)
        env.update(os.environ)  # same precedence as load_config
        return env

    def load(self) -> tuple[dict[str, Any], dict[str, str]]:
        return read_raw_config(self.config_path), read_env_file(self.env_path)

    def current(self) -> AppConfig:
        raw, env = self.load()
        return build_config(raw, self._env_for_build(env), self.config_path.parent, str(self.config_path))

    def draft(self) -> Draft:
        raw, env = self.load()
        return Draft(raw, env)

    # -- writing -------------------------------------------------------------

    def update(self, mutate: Mutation) -> AppConfig:
        """Apply ``mutate`` to a draft, validate, then write both files."""
        with self._lock:
            d = self.draft()
            mutate(d)
            cfg = build_config(
                d.raw, self._env_for_build(d.env), self.config_path.parent, str(self.config_path)
            )
            self.write(d)
            return cfg

    def write(self, d: Draft) -> None:
        if d.env_changes:
            _atomic_write(self.env_path, _render_env(self.env_path, d.env_changes), private=True)
        text = yaml.safe_dump(d.raw, sort_keys=False, allow_unicode=True, default_flow_style=False)
        header = (
            "# Glimpse configuration. Edited by the settings panel; hand edits are fine too.\n"
            "# Secrets live in .env; this file only references them as ${NAME}.\n"
        )
        _atomic_write(self.config_path, header + text)


def _quote(value: str) -> str:
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _render_env(path: Path, changes: dict[str, str | None]) -> str:
    """Update KEY= lines in place, keep comments, append new keys, drop removed ones."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    out, seen = [], set()
    key_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in lines:
        m = key_re.match(line)
        if m and m.group(1) in changes:
            key = m.group(1)
            seen.add(key)
            if changes[key] is not None:
                out.append(f"{key}={_quote(changes[key])}")
            continue
        out.append(line)
    new = [k for k, v in changes.items() if k not in seen and v is not None]
    while out and not out[-1].strip():
        out.pop()
    out.extend(f"{k}={_quote(changes[k])}" for k in new)
    return "\n".join(out) + "\n"


def _atomic_write(path: Path, text: str, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        backup.write_bytes(path.read_bytes())
        if private:
            _restrict(backup)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if private:
        _restrict(tmp)
    os.replace(tmp, path)


def _restrict(path: Path) -> None:
    if os.name == "posix":
        os.chmod(path, 0o600)


def init_files(config_path: Path, env_path: Path | None = None, admin_hash: str | None = None) -> None:
    """Create a minimal working config.yaml + .env (refuses to overwrite)."""
    env_path = env_path or default_env_path(config_path)
    if config_path.exists():
        raise ConfigError(f"{config_path} already exists; not overwriting it")
    raw: dict[str, Any] = {
        "timezone": "America/New_York",
        "refresh_minutes": 10,
        "week_starts": "monday",
        "server": {
            "host": "127.0.0.1",
            "port": 8000,
            "base_url": "http://127.0.0.1:8000",
            "secret_key": "${GLIMPSE_SECRET_KEY}",
        },
        "admin": {"port": 8001, "password_hash": "${GLIMPSE_ADMIN_PASSWORD_HASH}"},
        "calendars": {},
        "audiences": {},
    }
    file_env = read_env_file(env_path)
    d = Draft(raw, file_env)
    if "GLIMPSE_SECRET_KEY" not in file_env:
        d._set_env("GLIMPSE_SECRET_KEY", secrets.token_urlsafe(48))
    if admin_hash:
        d._set_env("GLIMPSE_ADMIN_PASSWORD_HASH", admin_hash)
    elif "GLIMPSE_ADMIN_PASSWORD_HASH" not in file_env:
        # No password yet: the first-run page sets it, guarded by a one-time token.
        d.raw["admin"]["password_hash"] = None
        d.set_secret(d.raw["admin"], "setup_token", secrets.token_urlsafe(24), "GLIMPSE_SETUP_TOKEN")
    env = dict(d.env)
    env.update(os.environ)
    build_config(d.raw, env, config_path.parent)  # validate before writing
    ConfigStore(config_path, env_path).write(d)


def ensure_setup_token(store: ConfigStore) -> str:
    """Return the first-run token, creating one if the config has none."""
    cfg = store.current()
    if cfg.admin.setup_token:
        return cfg.admin.setup_token
    token = secrets.token_urlsafe(24)
    store.update(lambda d: d.set_secret(d.section("admin"), "setup_token", token, "GLIMPSE_SETUP_TOKEN"))
    return token


def finish_setup(d: Draft, password_hash: str) -> None:
    """Set the admin password and retire the one-time setup token."""
    admin = d.section("admin")
    d.set_secret(admin, "password_hash", password_hash, "GLIMPSE_ADMIN_PASSWORD_HASH")
    d.drop_secret(admin, "setup_token")
    admin.pop("setup_token", None)
