# Changelog

All notable changes to Glimpse. Versions follow [Semantic Versioning](https://semver.org/):
while Glimpse is below 1.0, new features bump the middle number and fixes bump the last.

The settings panel checks this repository's GitHub releases for new versions; each release's
notes come from the matching section below.

## [Unreleased]

- Donation links: GitHub Sponsors and Ko-fi in the README's Support section and the repository's
  Sponsor button.

## [0.6.0] - 2026-10-19

### Added
- **Glimpse calendars** (new **My calendars** page): make calendars right in Glimpse, with no
  Google, Outlook or iCloud account. Add, edit and delete events: timed or all day, repeating
  (daily, weekdays, weekly, every 2 weeks, monthly, yearly, with an end date), with a place,
  notes, "show as free" and "private". Skip single dates of a repeating event. They work like any
  calendar (detail levels, groups, rules, open times) and are plain .ics files in
  `local-calendars/` beside your settings, included in update backups.
- **Share a Glimpse calendar** with its own subscription link (full details) for your phone or
  someone else's calendar app. Make a new link or stop sharing at any time.
- **Instant booking**: per audience, picked open times can be booked straight away instead of
  waiting for you. Suggested times still need your OK.
- **Confirmed meetings go onto a Glimpse calendar** automatically, and come off it again when
  either side cancels.
- **Meeting links**: set your Zoom, Teams or Google Meet link (and/or a room) per audience, and
  change it per meeting when you accept. It's shown only after a meeting is confirmed: on the
  requester's page with a "Join the meeting" button, in the confirmation email and in the
  calendar file.
- **Public booking page**: a separate link per audience that shows only open times, with no
  password, for an email signature or website.
- **Cancel a confirmed meeting** from the Requests page; the other person is told by email.

## [0.5.1] - 2026-10-19

### Added
- **Click an event for its details**: on every view (and in the settings preview), click, tap
  or press Enter on an event to see its full title, time, location, calendar and notes in a small
  window. It shows only what that viewer may already see; busy blocks stay "Busy".
- **Leave calendars out of a subscription**: on an audience's page, pick calendars to leave out
  of its calendar-app subscription, such as a calendar you already share with them in iCloud or
  Google. Their events no longer show up twice in the viewer's calendar app; the Glimpse page
  still shows them.

## [0.5.0] - 2026-10-17

### Added
- **Meeting requests**: an audience can ask for a meeting from its shared page, either by picking
  one of your open times (a day, then a time, in their own time zone) or by suggesting one. You
  accept, decline or propose another time on the new **Requests** page. Requesters get a private
  status page to follow the answer, accept a new time, add the meeting to their calendar or
  cancel. Open times come from all your calendars, so hidden events still block the time without
  revealing anything about it. Set lengths, bookable hours, notice and how far ahead per audience.
- **Notifications** by phone (ntfy), email (SMTP) and/or Discord, each switched on separately,
  with a test button. Email can also send requesters a confirmation and your answer with a
  calendar file. Secrets are kept in `.env`.
- **Website widget**: an embeddable free/busy view of the next days (no titles) with an optional
  "Request a meeting" button. It has its own link and a list of sites allowed to embed it.
- **School mode**: terms, breaks and exam weeks, typed in or read from your school's academic
  calendar feed. Viewers see "Fall 2026 · Week 7" and break days; class calendars pause over
  breaks and between terms.
- **Support Glimpse**: a Support section in the README, a one-time note in the panel once setup is
  done, and an optional, off-by-default "Support the project" link on shared pages.
- A "How this was built" section in the README.

### Fixed
- Date ranges spanning the same month in different years (e.g. an audience's browse range of a
  year either way) showed only one year.

## [0.4.1] - 2026-10-13

### Added
- **Day view** for audiences: one day at a time, with previous/next day and Today. Tick "Day" on
  an audience's page to offer it (new audiences get it by default).
- **Emoji as calendar shapes**: pick one of 20 emoji, or type your own, as a calendar's or
  group's marker.
- **Suggested privacy rules, on by default**: new setups start with rules that turn health,
  job-search, and money/legal events (doctor, therapy, interview, bank, lawyer…) into a plain
  Busy block. Existing setups without any rules get them on upgrade (settings format 2, with a
  `config.yaml.v1.bak` backup); otherwise the Rules page offers "Add suggested rules".
- **"Don't apply to"** on each rule: exempt chosen audiences (e.g. your partner).
- **Drag-and-drop groups** on the Calendars page: drag a calendar card into a group lane (or
  use its Move menu on a phone). Group settings fold away under each lane.
- **About Glimpse** on shared pages: a fold-out explaining what the viewer is looking at, and a
  footer link for people who want to share their own schedule the same way.

### Changed
- **Distinct colours**: 12 colours instead of 8, and each new calendar gets one that isn't in
  use yet. "Give every calendar its own colour" on the Calendars page fixes existing repeats.
- Excel links say what they download ("Download this week (Excel)" / "this month"), and the
  day view offers its whole week. Shared pages print cleanly (Share > Print > Save as PDF on
  a phone).

## [0.4.0] - 2026-10-13

### Added
- **Shapes for calendars**: give a calendar (or group) a small shape (circle, triangle, star…)
  shown in the corner of its events and in the legend, so calendars differ by more than colour.
- **Group names for an audience**: an audience can see "Work" instead of the individual
  calendars in that group. Groups can have their own label, colour and shape.
- **Audiences at a glance** on Who sees what: a card per audience (people, views, hours) with
  Manage, Preview and Copy link, plus each audience listed under Who sees what in the sidebar.
- **Welcome banner** on shared pages with the Glimpse logo: "Welcome, Alex! Brandon's schedule,
  shared with you through Glimpse" (uses your first name from Settings).
- **Live refresh** on Overview: each calendar shows a spinner, then a tick or a cross with the
  problem, and a Stop button.
- **Browse limits** can be 0 to 104 weeks, or **All**. Calendar-app subscriptions still cover
  at most about two years ahead and one back.
- **Protection against conflicting saves**: if settings were changed from another device after
  you opened a page, saving it is refused (nothing is overwritten) and you're asked to reload.

### Changed
- The legend lists every calendar the audience can see by name, faded when it has nothing in
  the period shown, instead of only calendars with events that week.
- A fifth wrong password now locks that device out straight away for 15 minutes (was: a sixth
  try within one minute), and the message says how long to wait and warns before the last try.

### Fixed
- The Calendars page's browser-tab title contained the whole Groups section.

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

[Unreleased]: https://github.com/brandonbtm29/Glimpse/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/brandonbtm29/Glimpse/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/brandonbtm29/Glimpse/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/brandonbtm29/Glimpse/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/brandonbtm29/Glimpse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/brandonbtm29/Glimpse/releases/tag/v0.1.0
