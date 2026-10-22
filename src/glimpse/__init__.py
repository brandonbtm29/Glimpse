"""The old name of Kalends, kept so existing installs keep working after the rename.

Shortcuts, services and update tools from before the rename run ``glimpse-service``,
``python -m glimpse.service`` or ``python -m glimpse.cli``; those still work and run Kalends.
New code should import ``kalends``.
"""

from kalends import __version__  # noqa: F401
