import logging

import httpx

from glimpse.sources import FeedCache, normalize_url

SECRET_URL = "https://calendar.example.com/private-abc123SECRET/basic.ics"
ICS = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"


def _cache(cfg, tmp_path, url=SECRET_URL):
    cfg = cfg.model_copy(deep=True)
    cfg.cache_dir = str(tmp_path / "cache")
    cfg.calendars = {"personal": cfg.calendars["personal"].model_copy(update={"url": url})}
    return FeedCache(cfg)


def test_webcal_becomes_https():
    assert normalize_url("webcal://p01-caldav.icloud.com/published/2/x") == (
        "https://p01-caldav.icloud.com/published/2/x"
    )


def test_conditional_get_and_last_good_copy(cfg, tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if len(calls) == 1:
            return httpx.Response(200, text=ICS, headers={"ETag": '"v1"'})
        if len(calls) == 2:
            return httpx.Response(304)
        return httpx.Response(500)

    cache = _cache(cfg, tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    cache.refresh_all(client)
    assert cache.texts()["personal"] == ICS and cache.states["personal"].version == 1
    cache.refresh_all(client)
    assert calls[1]["if-none-match"] == '"v1"'
    cache.refresh_all(client)  # server error: keep serving the last good copy
    st = cache.states["personal"]
    assert cache.texts()["personal"] == ICS and st.last_error and "500" in st.last_error


def test_cache_survives_restart_and_hides_names(cfg, tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=ICS)))
    _cache(cfg, tmp_path).refresh_all(client)
    files = list((tmp_path / "cache").iterdir())
    assert len(files) == 1 and "personal" not in files[0].name
    assert _cache(cfg, tmp_path).texts()["personal"] == ICS  # loaded from disk, no fetch


def test_non_calendar_response_rejected(cfg, tmp_path):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>login</html>"))
    )
    cache = _cache(cfg, tmp_path)
    cache.refresh_all(client)
    assert "personal" not in cache.texts()
    assert "not an iCalendar" in cache.states["personal"].last_error


def test_feed_urls_never_logged_or_stored_in_errors(cfg, tmp_path, caplog):
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    logging.getLogger("httpx").setLevel(logging.WARNING)
    caplog.set_level(logging.DEBUG, logger="glimpse")
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    bad = httpx.Client(transport=httpx.MockTransport(boom))
    cache = _cache(cfg, tmp_path)
    cache.refresh_all(ok)
    cache.refresh_all(bad)
    assert "SECRET" not in caplog.text
    assert "SECRET" not in (cache.states["personal"].last_error or "")
