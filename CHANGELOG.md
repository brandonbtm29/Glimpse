# Changelog

All notable changes to Glimpse. Versions follow [Semantic Versioning](https://semver.org/):
while Glimpse is below 1.0, new features bump the middle number and fixes bump the last.

The settings panel checks this repository's GitHub releases for new versions; each release's
notes come from the matching section below.

## [Unreleased]

## [0.3.1] - 2026-10-13

### Changed
- New audiences can switch between all four views (week, month, 3 months, year) by default.
  Audiences saved before 0.3.1 keep their settings: tick the views on the audience's page.
- The audience page explains that a single ticked view means no view switcher.

## [0.3.0] - 2026-10-12

### Added
- **People**: personal links (`/p/<link>`) inside an audience. Move someone between audiences,
  give their link an end date, switch it off, or make them a new link without touching anyone
  else's. Each person can use the audience's password, their own, or none, and gets their own
  calendar-app subscription. "Last viewed" is shown in the panel (kept in `state.json`).
- An audience's shared link can be switched off once everyone has a personal link.
- **Time zones**: viewers can show times in their own zone (offered automatically, or picked from
  a list) or yours. Hour limits stay on your clock; all-day events keep their date. Audiences can
  have a default zone, and Settings has an optional first name ("Show Brandon's time").
- **Month, 3-month and year views**, chosen per audience with a default. The longer views are heat
  maps of busy hours (never titles) plus multi-day stretches at the audience's level. Month view
  downloads as Excel. Preview shows every view.
- **Calendar groups**: one level for several calendars, with per-calendar exceptions.
- **Privacy rules**: keywords (whole word, any case or accent) that make matching events Busy or
  hidden for everyone, with a preview of the upcoming events each rule catches.
- **Duplicate merging**: an event on several of your calendars shows once; the strictest
  privacy setting on any copy wins.
- **Updates**: the panel shows the running version, checks GitHub for new releases once a day,
  and can update a git-based install in one click (with a backup and automatic rollback).
- **Test from the internet** on the Setup page checks share links the way an outside visitor
  reaches them.
- `config_version` in `config.yaml`, so future releases can upgrade your settings automatically.

## [0.2.0] - 2026-10-12

### Added
- Settings panel in the browser: add calendars, choose who sees what, preview each audience,
  set passwords, all saved to `config.yaml` / `.env` with backups and applied live.
- One double-click installer for Windows and macOS, first-run setup in the browser, Tailscale
  Serve/Funnel set up from the panel, and start-at-sign-in without administrator rights.
- macOS and Linux support (launchd / systemd user services).

## [0.1.0] - 2026-10-12

### Added
- Merge Google, Outlook/Microsoft 365 and iCloud calendars from their ICS links.
- Audiences with their own link, optional password, and a detail level per calendar
  (full, title, label, busy, hidden), plus day/hour limits.
- `#private` and `#hide` tags and Outlook/Google private events respected for everyone.
- Week page for desktop and phone, Excel download, and a subscribe-able filtered ICS feed.

[Unreleased]: https://github.com/brandonbtm29/Glimpse/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/brandonbtm29/Glimpse/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/brandonbtm29/Glimpse/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/brandonbtm29/Glimpse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/brandonbtm29/Glimpse/releases/tag/v0.1.0
