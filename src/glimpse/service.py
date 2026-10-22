"""``glimpse-service`` / ``glimpse-open`` (old names) run Kalends' background service."""

from kalends.service import *  # noqa: F403
from kalends.service import main, main_open  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
