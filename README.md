# Glimpse

**Share your schedule without sharing your calendar.**

Glimpse merges your Google, Outlook/Microsoft 365 and iCloud calendars into one weekly view, then lets you decide, person by person and calendar by calendar, how much each viewer sees. Your partner can see everything; coworkers can see "Busy" blocks during work hours; a calendar can be hidden from someone entirely. Each audience gets its own link, with an optional password.

It runs on your own computer. Raw calendar data never leaves it; only already-filtered pages do.

![What a partner sees: full details across all calendars](docs/partner.png)

<table>
<tr>
<td width="62%"><img src="docs/coworkers.png" alt="What coworkers see: work hours only, busy blocks and labels"></td>
<td><img src="docs/phone-dark.png" alt="Phone view in dark mode"></td>
</tr>
<tr>
<td><sub>Coworkers: weekdays 8–6 only, work titles, classes shown as "Class", everything else "Busy"</sub></td>
<td><sub>Phones get a day-by-day list; dark mode follows the device</sub></td>
</tr>
</table>

## Features

- **Settings panel in your browser**: add calendar links, decide who sees what, and preview every audience's exact view. Works from any device on your Tailscale network (Mac, Windows, phone); never exposed publicly.
- **Any number of calendars** from Google, Outlook / Microsoft 365, iCloud, or anything else that publishes an ICS feed.
- **Five detail levels**, set per audience *and* per calendar: `full`, `title`, `label`, `busy`, `hidden`.
- **Calendar groups**: put related calendars together (e.g. "Work" = university + lab) and set one level per group, with per-calendar exceptions.
- **Time windows**: show an audience only certain days and hours (e.g. Mon–Fri 8:00–18:00).
- **Per-event overrides**: put `#private` in an event to make it "Busy" for everyone, or `#hide` to hide it. Events marked Private in Outlook/Google are treated as `#private` automatically.
- **Keyword privacy rules**: "anything mentioning *doctor* or *therapy* is Busy for everyone", or "hide anything with *interview*". Whole-word, case- and accent-insensitive, optionally limited to some calendars, with a preview of which upcoming events a rule catches.
- **Duplicate merging**: a meeting that's on two of your calendars (say, invited on work and copied to personal) shows once. If any copy is private or caught by a rule, that wins.
- **Optional passwords** per audience (Argon2-hashed, rate-limited, 30-day sign-in), and one-click "new link" to cut off access.
- **People with their own links**: put someone in an audience with a personal link. Move them between audiences, give the link an end date, see when they last looked, or cut off just them. Switch an audience's shared link off once everyone has their own.
- **Week, month, 3-month and year views**, chosen per audience. The longer views are heat maps of how busy each day is, never titles, plus multi-day trips at whatever level that audience may see.
- **Time zones**: viewers see your schedule in their own time (detected, or picked from a list), with your time a click away. Hour limits always stay on your clock.
- **Excel download** of any visible week or month, and a **subscribe-able ICS feed** so viewers can add your (filtered) schedule to their own calendar app.
- Recurring events, moved/cancelled instances, all-day and overnight events, and daylight-saving changes all handled.
- **One double-click install** on Windows and macOS, then a guided setup in the browser. No administrator rights needed.
- Runs on Windows, macOS or Linux. One Python process, no database, no tracking.

<table>
<tr>
<td width="50%"><img src="docs/admin-sharing.png" alt="Settings panel: who sees what, calendar by audience"></td>
<td><img src="docs/admin-preview.png" alt="Settings panel: previewing exactly what coworkers see"></td>
</tr>
<tr>
<td><sub>Who sees what: one dropdown per calendar and audience</sub></td>
<td><sub>Preview: the exact page an audience gets, before you share it</sub></td>
</tr>
</table>

## How it fits together

```
                         ┌──────────────── your always-on PC ────────────────┐
Google  ─┐               │                                                   │
Outlook ─┼── ICS feeds ──►  Glimpse  ── :8000 public pages ── Tailscale Funnel ──► anyone with a link
iCloud  ─┘               │           ── :8001 settings     ── Tailscale Serve  ──► only your devices
                         └───────────────────────────────────────────────────┘
```

- **Public pages** (port 8000) are what you share. Tailscale **Funnel** puts them on the internet at `https://<pc>.<tailnet>.ts.net`.
- **The settings panel** (port 8001) is only published with Tailscale **Serve**, which is reachable from devices signed in to *your* tailnet and nobody else, at `https://<pc>.<tailnet>.ts.net:8443`. It also needs its own admin password, and can be limited to specific Tailscale accounts.
- Both listen on `127.0.0.1` only, so nothing on your router is opened.

## How privacy works

- **Filtering happens on the server, before rendering.** If an audience may not see a title, the title is never put in the HTML, the Excel file, or the feed. There's nothing hidden "in the page" to dig out.
- **Busy blocks are anonymous**: no calendar name, no colour, no original event ID. Overlapping busy blocks are merged, so viewers can't count your meetings.
- **Overrides and privacy rules only reduce detail**, never increase it. When duplicates are merged, the strictest restriction on any copy applies.
- **Feed links stay on your PC** in `.env` (owner-only file permissions). The panel shows them masked; they're never logged, even in error messages, and the on-disk cache uses hashed file names.
- **Unguessable links**: each audience's address contains a random 128-bit slug. Pages send `noindex`, `no-store`, `no-referrer` and a strict Content-Security-Policy.

The test suite checks these rules directly, e.g. that no forbidden word appears anywhere in a coworker's HTML, Excel file or ICS feed, and that the panel never prints a feed link.

## Try the demo (2 minutes)

Requires Python 3.11+.

```bash
git clone https://github.com/<you>/glimpse.git
cd glimpse
python -m venv .venv
.venv/bin/pip install -e .            # Windows: .venv\Scripts\pip install -e .
.venv/bin/glimpse -c examples/demo/config.yaml serve --no-admin
```

Then open:

- Coworker view (no password): <http://127.0.0.1:8000/a/demo-coworker-link-00>
- Partner view (password `demo-password`): <http://127.0.0.1:8000/a/demo-partner-link-000>

## Set it up

### 1. Install Tailscale (once)

[Download Tailscale](https://tailscale.com/download) and sign in on the computer that will host Glimpse, and on the phone or laptop you'll manage it from, using the same account. The free Personal plan is enough.

### 2. Install Glimpse

Download this repository (green **Code** button → *Download ZIP*, then unzip it somewhere permanent, e.g. your Documents folder), or `git clone` it. Then:

| Host | Do this |
| --- | --- |
| **Windows** | Double-click **`Install Glimpse.cmd`**. If Windows asks, choose *Run*. |
| **macOS** | Right-click **`Install Glimpse.command`** → *Open* (only the first time; after that a double-click works). |
| **Linux** | Run `./scripts/unix/install.sh` |

The installer needs no administrator rights. It installs Python 3.12 for you if it's missing (Windows, with your OK), sets everything up in the folder, makes Glimpse start automatically when you sign in, adds **Glimpse settings** to the Start menu (Windows), and opens your browser.

### 3. Finish in the browser

1. **Choose an admin password.** This first page only works on the computer running Glimpse.
2. **Setup page:** press **Turn on** for *Settings panel on your devices* and *Public share links*. The first time, Tailscale may ask you to approve HTTPS/Funnel; Glimpse shows you the link, so approve it and press the button again. Glimpse fills in your public address on its own.
3. **Calendars → Add a calendar**: paste each feed link (see below), press **Test link**, then **Add**.
4. **Who sees what → New audience**, then pick a level for each calendar. **Preview** shows exactly what each audience will see.
5. **Overview** lists every share link with a Copy button.

From then on, manage everything from any of your devices at `https://<host-name>.<tailnet>.ts.net:8443` (shown on the Setup page).

### Updating

Put the new version in the same folder (`git pull`, or unzip over it; your `config.yaml`, `.env` and calendar cache are never overwritten) and run the installer again. It stops the running copy, updates it, and starts it again.

### Optional extras

- **Start even before anyone signs in (Windows):** if the PC sometimes reboots and sits at the sign-in screen, run `scripts\windows\start-before-login.ps1` from an administrator PowerShell.
- **Turn it off:** `scripts\windows\uninstall.ps1` (Windows) or `.venv/bin/glimpse autostart off` (macOS/Linux) stops Glimpse and its auto-start; your files stay put.
- **Prefer a terminal?** Everything the installer and Setup page do is also a `glimpse` command (below).

### Getting your feed links

Each calendar has a private link that apps can read. Treat these like passwords.

| Provider | Where to find the link |
| --- | --- |
| **Google Calendar** | Settings ⚙ → Settings → pick the calendar under "Settings for my calendars" → *Integrate calendar* → **Secret address in iCal format**. ([help](https://support.google.com/calendar/answer/37648)) |
| **Outlook.com / Microsoft 365** | Settings ⚙ → Calendar → Shared calendars → **Publish a calendar** → choose the calendar and the most detailed option → Publish → copy the **ICS** link. ([help](https://support.microsoft.com/en-US/Outlook/share-your-calendar-in-outlook-com)) |
| **iCloud** | Calendar on Mac → hover the calendar → Share → tick **Public Calendar** → copy the link (iPhone: Calendars → ⓘ → Public Calendar → Share Link). `webcal://` links work as-is. ([help](https://support.apple.com/guide/calendar/icl32362)) |

Publish with full details. Glimpse does the filtering, so it needs the full data to work with. Work and school accounts sometimes disable these options; if so, ask your IT admin.

If a link ever leaks, reset it at the source: Google has a **Reset** button, Outlook lets you unpublish and republish, and iCloud lets you turn public sharing off and on. Then paste the new link under **Calendars → Edit**.

### Sharing and revoking

Send the link and the password through different channels (e.g. link by email, password by text). To cut someone off, open their audience and press **Make a new link** (and change the password). The old link and every existing sign-in stop working at once.

With **People**, each person gets their own link instead, so cutting off one person (or letting their link expire on a date) leaves everyone else's link alone. "Last viewed" times are kept in `state.json` beside your config, never in `config.yaml`.

**Testing a link from outside:** on the Setup page, **Test from the internet** checks your share links the way someone without Tailscale opens them. If a link works there but not on your own laptop with Tailscale switched off, your laptop is still remembering the private Tailscale address: try a phone on mobile data, or clear the browser's DNS cache.

## Detail levels

| Level | The viewer sees |
| --- | --- |
| `full` | title, time, location, notes |
| `title` | title and time |
| `label` | the calendar's label (e.g. "Class") and time |
| `busy` | an anonymous "Busy" block |
| `hidden` | nothing |

## Editing config by hand (optional)

Everything the panel does is stored in `config.yaml`, with secrets in `.env` referenced as `${NAME}`. Hand edits are fine; a running Glimpse picks them up on its own, and if an edit is invalid it keeps the previous settings and shows the error in the panel. See the commented [config.example.yaml](config.example.yaml).

## Commands

Run these from the Glimpse folder as `.venv\Scripts\glimpse …` (Windows) or `.venv/bin/glimpse …` (macOS/Linux).

| Command | What it does |
| --- | --- |
| `glimpse open` | open the settings panel, starting Glimpse in the background if needed |
| `glimpse service` | run in the background, logging to `logs/glimpse.log` (what auto-start runs) |
| `glimpse serve` | run in this window, printing to the screen; `--no-admin` skips the panel |
| `glimpse autostart on\|off\|status` | start automatically when you sign in |
| `glimpse init [--setup-later]` | create `config.yaml` + `.env` for a fresh setup |
| `glimpse admin-password` | set or reset the settings-panel password |
| `glimpse set-base-url URL` | set the public address used in share links |
| `glimpse check` | validate config and fetch every feed; exit code 0 = all OK |
| `glimpse preview -a NAME [-w YYYY-MM-DD]` | print what an audience sees for a week |
| `glimpse links` | print each audience's links |
| `glimpse new-slug` / `new-secret` / `hash-password` | generate values for hand-edited configs |

All commands take `-c path/to/config.yaml` (default `./config.yaml`) and `-e path/to/.env` (default: beside the config).

## Good to know

- Feeds refresh every 10 minutes by default (Settings). Google and Outlook may themselves update their published feeds only every few minutes to hours, so very recent changes can take a while to show up.
- If a feed fails, Glimpse keeps showing the last good copy; the Overview shows which calendar has a problem.
- All-day events at the `busy` level are hidden by default (they're usually birthdays and reminders). There's a checkbox per audience to show them.
- Events marked "show as free" never appear as Busy, only at `title`/`full`.
- The calendar-subscription link works without the password (calendar apps can't sign in), so treat it like the password, or turn subscriptions off for that audience.
- Forgot the admin password? On the host, run `glimpse admin-password`.
- Something not working? Check `logs/glimpse.log` in the Glimpse folder. It never contains your feed links.
- Testing over plain `http://127.0.0.1` works in Chrome and Edge. If another browser keeps returning you to a login page, it's refusing the secure cookie over plain http; use the `https://…ts.net` address instead.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests && ruff format --check src tests
pip install pre-commit && pre-commit install   # secret scanning + lint on every commit
```

The privacy rules live in [`src/glimpse/policy.py`](src/glimpse/policy.py) as pure functions with no web or network code, so they're easy to test. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © 2026 Brandon T. McReynolds
