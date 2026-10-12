# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Use GitHub's
"Report a vulnerability" button (Security tab → Advisories) to report privately.
I'll reply within a week.

Things that count as security issues here:

- Any way for a viewer to see more than their audience's level allows (in HTML, Excel, ICS, headers, or timing)
- Any way to reach an audience's page without its slug, or past its password
- Feed URLs or other secrets appearing in logs, error pages, the cache folder name, or responses

## Hardening already in place

- Filtering happens server-side before rendering; busy blocks carry no source details and are merged
- Argon2id password hashes; signed, HttpOnly, SameSite cookies scoped to one audience's path
- Login rate limiting per client IP and per audience
- Changing an audience's slug or password invalidates every existing sign-in and feed token
- `noindex`, `no-store`, `no-referrer`, strict CSP with no scripts, `frame-ancestors 'none'`
- Server binds to `127.0.0.1`; public access goes through a tunnel
- Feed URLs are redacted from every log line and error message
