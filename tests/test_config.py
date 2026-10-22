import pytest

from kalends.config import ConfigError, Level, load_config


def test_loads_and_substitutes(cfg):
    assert cfg.audiences["partner"].slug == "partnerSlug_abcdefghij"
    assert cfg.audiences["partner"].password_hash.startswith("$argon2")
    assert cfg.audiences["coworkers"].password_hash is None
    assert cfg.audiences["coworkers"].calendars["classes"] is Level.LABEL
    assert cfg.calendars["classes"].display_label == "Class"


def test_missing_env_var_is_named(config_path):
    (config_path.parent / ".env").write_text("SECRET='" + "s" * 40 + "'\n")
    with pytest.raises(ConfigError) as exc:
        load_config(config_path)
    assert "PARTNER_SLUG" in str(exc.value)


def test_unknown_calendar_reference(config_path):
    text = config_path.read_text().replace("family: hidden", "famly: hidden")
    config_path.write_text(text)
    with pytest.raises(ConfigError) as exc:
        load_config(config_path)
    assert "famly" in str(exc.value)


def test_bad_level_rejected(config_path):
    config_path.write_text(config_path.read_text().replace("lab: title", "lab: everything", 1))
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_short_slug_rejected(config_path):
    env = config_path.parent / ".env"
    env.write_text(env.read_text().replace("coworkerSlug_klmnopqrs", "short"))
    with pytest.raises(ConfigError) as exc:
        load_config(config_path)
    assert "slug" in str(exc.value)


def test_env_wins_over_dotenv(config_path, monkeypatch):
    monkeypatch.setenv("COWORKER_SLUG", "fromEnvironment_12345")
    assert load_config(config_path).audiences["coworkers"].slug == "fromEnvironment_12345"


def test_level_ordering():
    assert Level.FULL.at_most(Level.BUSY) is Level.BUSY
    assert Level.HIDDEN.at_most(Level.BUSY) is Level.HIDDEN


def test_empty_password_hash_is_an_error_not_link_only(config_path):
    env = config_path.parent / ".env"
    lines = [ln for ln in env.read_text().splitlines() if not ln.startswith("PARTNER_HASH")]
    env.write_text("\n".join(lines + ["PARTNER_HASH=''"]) + "\n")
    with pytest.raises(ConfigError) as exc:
        load_config(config_path)
    assert "password_hash is empty" in str(exc.value)


def test_example_config_is_valid(tmp_path):
    import shutil
    from pathlib import Path

    root = Path(__file__).parent.parent
    shutil.copy(root / "config.example.yaml", tmp_path / "config.yaml")
    from argon2 import PasswordHasher

    (tmp_path / ".env").write_text(
        "KALENDS_BASE_URL=https://x.ts.net\nKALENDS_SECRET_KEY=" + "k" * 40 + "\n"
        "GOOGLE_PERSONAL_ICS=https://a\nOUTLOOK_LAB_ICS=https://b\n"
        "OUTLOOK_CLASSES_ICS=https://c\nICLOUD_FAMILY_ICS=webcal://d\n"
        "PARTNER_SLUG=" + "p" * 22 + "\nCOWORKER_SLUG=" + "c" * 22 + "\n"
        "KALENDS_PERSON_ALEX_SLUG=" + "a" * 22 + "\nKALENDS_PERSON_PRIYA_SLUG=" + "b" * 22 + "\n"
        f"PARTNER_PW_HASH='{PasswordHasher().hash('x' * 10)}'\n"
        f"KALENDS_ADMIN_PASSWORD_HASH='{PasswordHasher().hash('y' * 10)}'\n"
        "KALENDS_AUD_COWORKERS_WIDGET_SLUG=" + "w" * 22 + "\nKALENDS_NTFY_TOPIC=glimpse-topic\n"
        "KALENDS_SMTP_PASSWORD=pw\nKALENDS_DISCORD_WEBHOOK=https://discord.com/api/webhooks/1/x\n"
    )
    cfg = load_config(tmp_path / "config.yaml")
    assert set(cfg.audiences) == {"partner", "coworkers"}
    assert cfg.audiences["coworkers"].requests.enabled and cfg.school.terms


def test_demo_config_is_valid():
    from pathlib import Path

    cfg = load_config(Path(__file__).parent.parent / "examples/demo/config.yaml")
    assert cfg.audiences["partner"].password_hash
