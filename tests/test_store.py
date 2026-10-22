import pytest
import yaml

from kalends.config import ConfigError, Level, load_config
from kalends.store import ConfigStore, init_files, make_id


def test_init_creates_working_setup(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    init_files(cfg_path, admin_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def")
    cfg = load_config(cfg_path)
    assert cfg.calendars == {} and cfg.audiences == {}
    assert cfg.admin.password_hash.startswith("$argon2")
    assert len(cfg.server.secret_key) >= 32
    assert "$argon2" not in cfg_path.read_text()  # hash lives in .env only
    with pytest.raises(ConfigError):
        init_files(cfg_path)  # never overwrites


def test_add_calendar_keeps_link_out_of_yaml(config_path):
    store = ConfigStore(config_path)
    secret = "https://calendar.google.com/calendar/ical/abc%40gmail.com/private-SECRET123/basic.ics"
    cfg = store.update(lambda d: d.add_calendar("GelPure Board", secret, "Meeting"))
    assert cfg.calendars["gelpure_board"].url == secret
    assert "SECRET123" not in config_path.read_text()
    assert "KALENDS_CAL_GELPURE_BOARD_URL=" in (config_path.parent / ".env").read_text()
    assert (config_path.parent / "config.yaml.bak").exists()


def test_existing_env_names_and_comments_preserved(config_path):
    env = config_path.parent / ".env"
    env.write_text("# my notes\n" + env.read_text())
    store = ConfigStore(config_path)
    old_slug = load_config(config_path).audiences["partner"].slug
    cfg = store.update(lambda d: d.rotate_slug("partner"))
    text = env.read_text()
    assert text.startswith("# my notes\n")
    assert "PARTNER_SLUG=" in text and "KALENDS_AUD_PARTNER_SLUG" not in text  # reused the existing name
    assert cfg.audiences["partner"].slug != old_slug
    assert "\n\n" not in text


def test_invalid_change_writes_nothing(config_path):
    store = ConfigStore(config_path)
    before_cfg, before_env = config_path.read_text(), (config_path.parent / ".env").read_text()

    def bad(d):
        d.audiences["coworkers"]["default"] = "everything"

    with pytest.raises(ConfigError):
        store.update(bad)
    assert config_path.read_text() == before_cfg
    assert (config_path.parent / ".env").read_text() == before_env


def test_remove_calendar_cleans_up(config_path):
    store = ConfigStore(config_path)
    store.update(lambda d: d.add_calendar("Temp", "https://example.com/x.ics"))
    cfg = store.update(lambda d: d.remove_calendar("classes"))
    assert "classes" not in cfg.calendars
    assert "classes" not in cfg.audiences["coworkers"].calendars
    cfg = store.update(lambda d: d.remove_calendar("temp"))
    assert "KALENDS_CAL_TEMP_URL" not in (config_path.parent / ".env").read_text()


def test_audience_lifecycle(config_path):
    store = ConfigStore(config_path)
    box = {}

    def add(d):
        box["id"] = d.add_audience("Lab group", "busy")

    cfg = store.update(add)
    aud = cfg.audiences[box["id"]]
    assert box["id"] == "lab_group" and aud.default is Level.BUSY and len(aud.slug) >= 16
    assert aud.password_hash is None
    cfg = store.update(lambda d: d.set_audience_password("lab_group", "$argon2id$v=19$m=1,t=1,p=1$a$b"))
    assert cfg.audiences["lab_group"].password_hash
    cfg = store.update(lambda d: d.set_audience_password("lab_group", None))
    assert cfg.audiences["lab_group"].password_hash is None
    cfg = store.update(lambda d: d.remove_audience("lab_group"))
    assert "lab_group" not in cfg.audiences
    assert "KALENDS_AUD_LAB_GROUP" not in (config_path.parent / ".env").read_text()


def test_unquoted_yaml_times(config_path):
    text = config_path.read_text().replace('start: "08:00", end: "18:00"', "start: 8:00, end: 17:30")
    config_path.write_text(text)
    hours = load_config(config_path).audiences["coworkers"].hours
    assert (hours.start.hour, hours.start.minute, hours.end.hour, hours.end.minute) == (8, 0, 17, 30)


def test_written_yaml_round_trips(config_path):
    store = ConfigStore(config_path)
    store.update(lambda d: None)
    raw = yaml.safe_load(config_path.read_text())
    assert raw["audiences"]["coworkers"]["hours"]["days"][0] == "mon"
    assert load_config(config_path).audiences["coworkers"].hours.end.hour == 18


def test_make_id():
    assert make_id("My Classes!", {}) == "my_classes"
    assert make_id("Lab", {"lab": 1}) == "lab_2"
    assert make_id("2026 Conf", {}) == "c_2026_conf"
