"""``glimpse`` (old name) runs the ``kalends`` command."""

from kalends.cli import *  # noqa: F403
from kalends.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
