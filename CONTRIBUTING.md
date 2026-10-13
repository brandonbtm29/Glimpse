# Contributing to Glimpse

Thanks for helping! A few ground rules keep this project safe to use.

## Privacy first

Glimpse exists to show people *less* of your calendar. Any change that touches what viewers see should:

1. Go through `policy.py`. Renderers must only ever receive `VisibleEvent` objects.
2. Never widen what a level reveals without a config option that defaults to the safer behaviour.
3. Come with a test that proves forbidden text does not appear in the HTML, Excel and ICS output.

## Never commit real calendar data

- `config.yaml` and `.env` are git-ignored, and the pre-commit hook blocks them. Please keep it that way.
- Test fixtures and demo calendars must be made up. Don't paste a real feed and "anonymize" it.
- Never put feed URLs in issues or logs. They work like passwords.

## Workflow

```bash
pip install -e ".[dev]"
pip install pre-commit && pre-commit install
pytest
ruff check src tests && ruff format src tests
```

CI runs the tests on Linux and Windows with Python 3.11 to 3.13, plus a gitleaks secret scan.

## Good first issues

- More calendar-feed edge cases (unusual time zones, malformed feeds) as test fixtures
- Translations of the page strings
- A small admin page on `127.0.0.1` for editing levels without touching YAML

## Releasing

1. Move the `[Unreleased]` notes in `CHANGELOG.md` under a new version heading, and set the same
   version in `src/glimpse/__init__.py` (the only place it lives).
2. If `config.yaml`'s shape changed, bump `CONFIG_VERSION` in `src/glimpse/config.py` and add a
   step to `src/glimpse/migrations.py` that converts the previous format (with a test).
3. Commit, tag `vX.Y.Z`, push the tag, and publish a GitHub release from it whose notes are that
   version's changelog section. Installs check the *latest release*, so nothing reaches users until
   the release is published; never retag a published version.
