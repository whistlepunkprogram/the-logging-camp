# The Logging Camp

An FRC team discussion board: public reading, admin-approved accounts for posting, and a link to the existing [offline scouting app](https://whistlepunkprogram.github.io/rebuilt-scouting/). The scouting app remains separate; its data is not synchronized with this board.

## Run locally

Requires Python 3.10 or later.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export COOKIE_SECURE=0
flask --app app init-db
flask --app app create-admin campadmin
flask --app app run
```

Open http://127.0.0.1:5000. Keep the same `SECRET_KEY` across restarts or everyone will be signed out. Back up `instance/camp.sqlite3`; it contains the board and accounts. Create additional admins with the same CLI command. Members request an account, and an admin approves or revokes access in Admin. Admins can pin, lock, and delete discussions and delete replies from the discussion page.

## Nexus

Obtain an API key from [Nexus](https://frc.nexus/api), set `NEXUS_API_KEY` in the server environment, and restart the app. In Admin, set the Nexus event key and team number. The board shows that team's matches and their Nexus statuses, cached for 30 seconds; Nexus data is only accurate at events using Nexus for queuing. This is informational, not a replacement for official event instructions. See the [Nexus API documentation](https://frc.nexus/api/v1/docs). The key stays on the server.

## Team data

The Team data tab displays team 4674's 2026 Match13 season data and The Blue Alliance event statuses. Obtain keys from your [Match13 account](https://www.match13.com/docs/api) and [The Blue Alliance account dashboard](https://www.thebluealliance.com/apidocs), set `MATCH13_API_KEY` and `TBA_API_KEY` on the server, and restart the app. The keys are never sent to the browser. Results are cached for ten minutes per server process. Both sources are independent: if one is unavailable, the other can still load. The tab also links to the original team pages.

## Deployment

Use a production WSGI server behind HTTPS, not Flask's development server. Set a persistent, random `SECRET_KEY`, leave `COOKIE_SECURE` at its default (`1`), and persist the instance directory on a volume. SQLite is intended for a single host with a persistent filesystem; do not deploy multiple independent instances against separate databases. The default login/registration rate limiter is process-local; for multiple workers configure `RATELIMIT_STORAGE_URI` to a shared Flask-Limiter supported backend and install its required client. Behind a reverse proxy the limiter sees the proxy address by default; configure a trusted proxy middleware such as Werkzeug's ProxyFix for your exact proxy topology, and block direct access to the app, before relying on per-client limits. Review hosting, backups, privacy, moderation, and account-recovery procedures before publishing. There is no password reset or email notification; administrators must communicate account approvals separately.

## Tests

```sh
python -m unittest discover -s tests
```
