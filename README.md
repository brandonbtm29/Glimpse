# Glimpse

[![CI](https://github.com/brandonbtm29/Glimpse/actions/workflows/ci.yml/badge.svg)](https://github.com/brandonbtm29/Glimpse/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/brandonbtm29/Glimpse)](https://github.com/brandonbtm29/Glimpse/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

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
- **Distinct colours and shapes**: every calendar gets its own colour out of 12, and can have a small shape or emoji (triangle, star, 🎓…) so its events stand out without relying on colour.
- **Calendar groups**: drag related calendars into a group (e.g. "Work" = university + lab) and set one level per group, with per-calendar exceptions. Some audiences can see just the group's name ("Work") instead of each calendar.
- **Time windows**: show an audience only certain days and hours (e.g. Mon–Fri 8:00–18:00).
- **Per-event overrides**: put `#private` in an event to make it "Busy" for everyone, or `#hide` to hide it. Events marked Private in Outlook/Google are treated as `#private` automatically.
- **Keyword privacy rules, on from the start**: suggested rules already turn health, job-search, and money/legal events (*doctor*, *therapy*, *interview*, *lawyer*…) into a plain Busy block for everyone. Add your own ("hide anything with *surprise*"), exempt an audience such as your partner, and preview which upcoming events a rule catches. Whole-word, case- and accent-insensitive.
- **Duplicate merging**: a meeting that's on two of your calendars (say, invited on work and copied to personal) shows once. If any copy is private or caught by a rule, that wins.
- **Optional passwords** per audience (Argon2-hashed, rate-limited, 30-day sign-in), and one-click "new link" to cut off access.
- **People with their own links**: put someone in an audience with a personal link. Move them between audiences, give the link an end date, see when they last looked, or cut off just them. Switch an audience's shared link off once everyone has their own.
- **Day, week, month, 3-month and year views**, chosen per audience. The longer views are heat maps of how busy each day is, never titles, plus multi-day trips at whatever level that audience may see.
- **Time zones**: viewers see your schedule in their own time (detected, or picked from a list), with your time a click away. Hour limits always stay on your clock.
- **Glimpse calendars**: make calendars right in Glimpse (no other account needed), add one-off or repeating events in the panel, and share one as a subscription link for phones and calendar apps.
- **Meeting requests and booking**: an audience (say, coworkers) can ask for a meeting by picking one of your open times or suggesting one. You accept, decline or propose another time from the panel, or let open times be booked straight away. Confirmed meetings can land on a Glimpse calendar automatically, and there's an optional public booking page. Open times come from *all* your calendars, so a hidden appointment still blocks the slot without revealing anything about it.
- **A new Zoom, Teams or Google Meet meeting for every booking**: the person booking picks the service you offer, each confirmed meeting gets its own link (shared only once it's confirmed), and cancelling deletes it. Or just use one fixed link.
- **Notifications on your phone, by email or in Discord**: switch on any mix of the three to hear about new requests right away.
- **Website widget**: a small free/busy view of the next few days (no titles) for a lab, course or personal site, with an optional "Request a meeting" button. It has its own link and you choose which sites may embed it.
- **School mode**: tell Glimpse your semesters (typed in, or read from your school's academic calendar feed). Viewers see "Fall 2026 · Week 7" and break days, and class events pause over breaks and between terms.
- **Excel download** of any visible week or month (works on phones too), and a **subscribe-able ICS feed** so viewers can add your (filtered) schedule to their own calendar app.
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
git clone https://github.com/brandonbtm29/Glimpse.git
cd Glimpse
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

Get a copy into a permanent folder (e.g. your Documents folder). The best way is with [git](https://git-scm.com/downloads), because then Glimpse can update itself from the settings panel:

```bash
git clone https://github.com/brandonbtm29/Glimpse.git
```

(Or download the [latest release](https://github.com/brandonbtm29/Glimpse/releases/latest) as a ZIP and unzip it. Everything works the same, except updates are done by hand.) Then:

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

Glimpse checks GitHub once a day for a new [release](https://github.com/brandonbtm29/Glimpse/releases) (you can turn this off in Settings) and shows a note in the settings panel when there is one. Your version is at the bottom of the panel's sidebar; the [changelog](CHANGELOG.md) lists what changed.

- **Installed with git:** open **Setup → Updates** and press **Update now**. Glimpse backs up `config.yaml` and `.env` to `backups/`, switches to the new release, checks it can read your settings, and restarts. If the new version doesn't start, it goes back to the old one by itself. Details go to `logs/update.log`.
- **Installed from a ZIP:** unzip the new release over the same folder (your `config.yaml`, `.env` and calendar cache are never overwritten) and run the installer again.

Releases only ever move forward through tagged versions; unreleased work on `main` is never installed by the update button. If a release changes the format of `config.yaml`, Glimpse upgrades your file automatically on start and keeps the original as `config.yaml.v<N>.bak`.

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

### Glimpse calendars

On **My calendars**, make a calendar (say, "Plans" or "Office hours") and add events: timed or all day, one-off or repeating, with a place and notes. Mark an event **show as free** so it doesn't block meeting times, or **private** so everyone sees just "Busy". Skip a single date of a repeating event from its page.

They behave like any other calendar: set who sees them on **Who sees what**, put them in groups, and they count for open times. Each is a standard `.ics` file in `local-calendars/` beside your settings (included in update backups). **Create a subscription link** on a calendar's page to add it to your phone or share it; that link shows every event in full, so use an audience to share more carefully. Events are edited in the panel; phones show subscriptions read-only.

### Meeting requests

Turn on **Meeting requests** on an audience's page. Its shared page then gets a **Request a meeting** button that leads to:

- **Open times** to pick from, inside the hours you choose (by default the audience's hours, else weekdays 9–5), at least a day ahead and up to two weeks out (both adjustable), in the viewer's own time zone; and/or
- **Suggest another time**, for when none of those work.

Requests wait on the panel's **Requests** page, where you accept, decline, or propose a new time, with an optional message. Or choose **Book it straight away**: picked open times are confirmed at once (suggested times still wait for you).

**Meeting links and booked meetings.** On the audience's page, add your Zoom personal room, a standing Teams meeting or a Google Meet link (or a room). It's only revealed once a meeting is confirmed: on the requester's status page ("Join the meeting"), in the confirmation email and in the calendar file. You can change it per meeting when accepting. To give every booking its own meeting instead, see [Video calls](#video-calls) below. Choose a Glimpse calendar under **Add confirmed meetings to** and every confirmed meeting is added there (and removed if either of you cancels), so it shows on your pages and, through that calendar's subscription link, on your phone.

**Public booking page.** Tick **Also offer a public booking page** for a separate link (no password, open times only) to put in an email signature or on a website. Glimpse can't write to Google, Outlook or iCloud, so either let it add meetings to a Glimpse calendar (below) or use **Add to my calendar** (a one-event file your calendar app opens). The requester gets a private status page where they can see your answer, accept a new time, add the meeting to their calendar, or cancel. Waiting and accepted requests keep their time off the open list so nobody double-books you.

Open times are worked out from every calendar except ones you mark on the Requests page as never making you busy (holidays, birthdays), with an optional buffer between meetings. Events marked "free" never block time. Requests are kept in `requests.json` beside your config. There's a limit on how many requests one link or one network address can send.

### Video calls

The **Video calls** page connects Glimpse to **Zoom**, **Microsoft Teams** and **Google Meet**, so each booking gets a brand-new meeting with its own link, instead of everyone sharing your personal room. Then, under **Meeting requests** on an audience's page, tick which services that audience may choose from. The person booking picks one (or, if you tick only one, it's used for everyone). The link appears for them only once the meeting is confirmed, and cancelling deletes the Zoom or Teams meeting. When accepting by hand you can still paste a different link instead. If a meeting can't be made, Glimpse sends your fixed link (if you set one) and shows you why on the Requests page.

Each service needs a small developer app that belongs to you, so no outside server is involved. The panel walks you through it, and **Make a test meeting** checks it works:

| Service | What you make | Sign-in | Notes |
|---|---|---|---|
| **Zoom** | A *Server-to-Server OAuth* app at marketplace.zoom.us with the `meeting:write:meeting:admin` and `meeting:delete:meeting:admin` scopes | None: paste the account ID, client ID and secret | Free Zoom plans limit meetings with 3+ people to 40 minutes. |
| **Microsoft Teams** | An app registration in Microsoft Entra with *Allow public client flows* on and the delegated `OnlineMeetings.ReadWrite` permission | Once, with a code at microsoft.com/devicelogin | Work or school accounts only; Microsoft doesn't offer this for personal accounts. Your organisation may need to approve the app. |
| **Google Meet** | A Google Cloud project with the Meet REST API on, a *Web application* OAuth client (with the redirect address the panel shows), and the consent screen published "In production" | Once, with Google | Google warns the app is unverified; it's yours, so continue. Meet links can't be deleted, so a cancelled one just goes unused. |

Secrets (Zoom and Google client secrets) go in `.env`; the sign-ins are kept in `video-accounts.json` beside your config, which git ignores and update backups include.

### Notifications

On **Notifications**, switch on any of:

| Channel | You need | Setup |
| --- | --- | --- |
| **Phone** | the free [ntfy](https://ntfy.sh) app | Glimpse suggests a random topic; subscribe to it in the app. Tapping a notification opens your Requests page (with Tailscale on). |
| **Email** | an email account with SMTP | For Gmail: turn on 2-Step Verification, create an app password, use `smtp.gmail.com`, port 587. Can also email requesters a confirmation and your answer, with a calendar file. |
| **Discord** | a server you manage | Channel settings › Integrations › Webhooks › New Webhook › Copy Webhook URL. |

Each has a **Send a test** button. Topics, passwords and webhook links are stored in `.env`, never in `config.yaml`.

### Website widget

On an audience's page, turn on **Website widget** and copy the embed code into your site:

```html
<iframe src="https://your-pc.your-tailnet.ts.net/w/…" title="My availability" loading="lazy"
        style="width:100%;max-width:640px;height:340px;border:0;border-radius:10px"></iframe>
```

It shows busy blocks for the next days (no titles, no calendar names) in the visitor's time zone, plus a **Request a meeting** button when requests are on. List the sites allowed to embed it, or leave the list empty to allow any. It has its own link, separate from the audience's, so publishing it never reveals the audience's page; **Make a new widget link** retires the old one.

### School mode

On **School**, switch it on and either paste your school's academic calendar feed (if it publishes one) or type in each term's first and last day, then its breaks and exam weeks. Glimpse reads terms from a feed by looking for events like "Classes begin", "Last day of classes", "Thanksgiving break" and "Final exams"; check what it found, and use **Copy these so I can edit them** to fix anything. Typed-in terms always win.

Then tick the calendars that hold your classes. During breaks and between terms their events are hidden (handy when a class series runs straight through Thanksgiving), and viewers see the term, the week number and break days. Untick **Show viewers** to keep the term info to yourself.

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
- Already share a calendar with someone directly (say, an iCloud calendar with your partner)? On their audience's page, leave it out of the subscription so its events don't show up twice in their calendar app.
- Click or tap any event to see its full title and details (whatever that viewer is allowed to see).
- Zoom, Teams and Google Meet meetings are made with your own accounts through your own developer apps. Glimpse only asks permission to create (and for Zoom and Teams, delete) meetings, nothing else.
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

## Support

Glimpse is free and open source, and always will be. If it's useful to you:

- **Star the repository** and tell a friend who juggles several calendars.
- **Report bugs and ideas** in [Issues](https://github.com/brandonbtm29/Glimpse/issues).
- **Chip in toward development**, once or monthly, whatever amount you like:
  - [GitHub Sponsors](https://github.com/sponsors/brandonbtm29)
  - [Ko-fi](https://ko-fi.com/brandonbtm29)

Shared pages can show a small "Support the project" link at the bottom; it's off unless you switch it on in Settings.

## How this was built

Glimpse is written and maintained by Brandon T. McReynolds with the help of an AI coding assistant: it was co-developed with Claude (Anthropic) from the first version, and commits list Claude as a co-author. Brandon directs the work, reviews and tests the changes, and is responsible for the project.

## License

[MIT](LICENSE) © 2026 Brandon T. McReynolds
