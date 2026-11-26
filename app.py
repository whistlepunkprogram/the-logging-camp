import os
import re
import sqlite3
import time
from functools import wraps
from pathlib import Path

import click
import requests
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__, instance_relative_config=True)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY"),
    DATABASE=os.path.join(app.instance_path, "camp.sqlite3"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    MAX_CONTENT_LENGTH=64 * 1024,
)
if not app.config["SECRET_KEY"]:
    raise RuntimeError("Set SECRET_KEY to a long, random value before starting the app.")
Path(app.instance_path).mkdir(parents=True, exist_ok=True)
CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"))


def db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(error):
    connection = g.pop("db", None)
    if connection:
        connection.close()


@app.cli.command("init-db")
def init_db():
    db().executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0,
            is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
            body TEXT NOT NULL, author_id INTEGER NOT NULL REFERENCES users(id),
            pinned INTEGER NOT NULL DEFAULT 0, locked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
            author_id INTEGER NOT NULL REFERENCES users(id), body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    db().commit()
    click.echo("Database initialized.")


@app.cli.command("create-admin")
@click.argument("username")
@click.password_option(confirmation_prompt=True)
def create_admin(username, password):
    if not valid_username(username) or len(password) < 12:
        raise click.ClickException("Username must be 3–30 letters, numbers, underscores or hyphens; password must be at least 12 characters.")
    try:
        db().execute("INSERT INTO users (username, password_hash, approved, is_admin) VALUES (?, ?, 1, 1)",
                     (username, generate_password_hash(password)))
        db().commit()
    except sqlite3.IntegrityError as exc:
        raise click.ClickException("That username already exists.") from exc
    click.echo("Admin created.")


def valid_username(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{3,30}", value))


@app.before_request
def load_user():
    g.user = None
    if session.get("user_id"):
        g.user = db().execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not g.user or not g.user["approved"]:
            session.clear()
            g.user = None


def requires_user(admin=False):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not g.user:
                flash("Please sign in with an approved account.", "error")
                return redirect(url_for("login"))
            if admin and not g.user["is_admin"]:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def settings():
    return {row["key"]: row["value"] for row in db().execute("SELECT key, value FROM settings")}


CATEGORIES = ("General", "Build", "Programming", "Competition", "Outreach")
_nexus_cache = {}
_team_data_cache = {}


def queue_status(event, team):
    key = os.environ.get("NEXUS_API_KEY")
    if not event or not team:
        return None, "An admin can configure an event and team number."
    if not key:
        return None, "Nexus is not connected. An admin must configure the API key on the server."
    cache_key = (event, team, key)
    cached = _nexus_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1], None
    try:
        response = requests.get(f"https://frc.nexus/api/v1/event/{event}", headers={"Nexus-Api-Key": key}, timeout=5)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("matches"), list):
            raise ValueError("Invalid Nexus response")
        matches = [
            match for match in data["matches"]
            if isinstance(match, dict) and (team in match.get("redTeams", []) or team in match.get("blueTeams", []))
        ]
        result = {"matches": matches, "data_as_of": data.get("dataAsOfTime")}
        _nexus_cache.clear()
        _nexus_cache[cache_key] = (time.monotonic(), result)
        return result, None
    except (requests.RequestException, ValueError, TypeError):
        app.logger.warning("Unable to retrieve Nexus event data for %s", event, exc_info=True)
        return None, "Nexus data is unavailable right now. Check the event key and try again later."


def team_data(source):
    configs = {
        "match13": ("MATCH13_API_KEY", "https://actions.match13.com/v1/teams/4674/years/2026", "Authorization"),
        "tba": ("TBA_API_KEY", "https://www.thebluealliance.com/api/v3/team/frc4674/events/2026/statuses", "X-TBA-Auth-Key"),
    }
    env_name, url, header = configs[source]
    key = os.environ.get(env_name)
    if not key:
        return None, f"Set {env_name} on the server to load this data."
    cached = _team_data_cache.get(source)
    if cached and cached[0] == key and time.monotonic() - cached[1] < 600:
        return cached[2], None
    try:
        token = f"Bearer {key}" if source == "match13" else key
        response = requests.get(url, headers={header: token}, timeout=5)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, (dict, list)):
            raise ValueError("Invalid team data response")
        _team_data_cache[source] = (key, time.monotonic(), data)
        return data, None
    except (requests.RequestException, ValueError):
        app.logger.warning("Unable to retrieve %s team data", source, exc_info=True)
        return None, "Data is unavailable. Check the server API key or try again later."


@app.get("/team-data")
def team_stats():
    match13, match13_error = team_data("match13")
    tba, tba_error = team_data("tba")
    return render_template("team_data.html", match13=match13, match13_error=match13_error,
                           tba=tba, tba_error=tba_error)


@app.route("/")
def index():
    category = request.args.get("category", "")
    search = request.args.get("q", "").strip()[:100]
    page = request.args.get("page", 1, type=int)
    page = max(1, min(page or 1, 100000))
    query = """SELECT topics.*, users.username, COUNT(replies.id) AS reply_count
               FROM topics JOIN users ON users.id = topics.author_id
               LEFT JOIN replies ON replies.topic_id = topics.id WHERE 1=1"""
    args = []
    if category in CATEGORIES:
        query += " AND topics.category = ?"
        args.append(category)
    if search:
        query += " AND (topics.title LIKE ? ESCAPE '\\' OR topics.body LIKE ? ESCAPE '\\')"
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.extend([f"%{escaped}%", f"%{escaped}%"])
    query += " GROUP BY topics.id ORDER BY topics.pinned DESC, topics.created_at DESC, topics.id DESC LIMIT 21 OFFSET ?"
    topics = db().execute(query, [*args, (page - 1) * 20]).fetchall()
    has_next = len(topics) > 20
    topics = topics[:20]
    config = settings()
    queue, queue_error = queue_status(config.get("event", ""), config.get("team", ""))
    return render_template("index.html", topics=topics, categories=CATEGORIES, category=category,
                           search=search, config=config, queue=queue, queue_error=queue_error,
                           page=page, has_next=has_next)


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not valid_username(username) or len(password) < 12 or len(password) > 256:
            flash("Use a 3–30 character username (letters, numbers, _ or -) and a password of 12–256 characters.", "error")
        else:
            try:
                db().execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, generate_password_hash(password)))
                db().commit()
                flash("Account requested. An admin must approve it before you can sign in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("That username is already taken.", "error")
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        user = db().execute("SELECT * FROM users WHERE username = ?", (request.form.get("username", "").strip(),)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")) and user["approved"]:
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
        flash("Invalid credentials or account not yet approved.", "error")
    return render_template("auth.html", mode="login")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/new", methods=["GET", "POST"])
@requires_user()
def new_topic():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "")
        body = request.form.get("body", "").strip()
        if not 1 <= len(title) <= 120 or category not in CATEGORIES or not 1 <= len(body) <= 10000:
            flash("Choose a category and provide a title (up to 120 characters) and message (up to 10,000 characters).", "error")
        else:
            cursor = db().execute("INSERT INTO topics (title, category, body, author_id) VALUES (?, ?, ?, ?)",
                                  (title, category, body, g.user["id"]))
            db().commit()
            return redirect(url_for("topic", topic_id=cursor.lastrowid))
    return render_template("new.html", categories=CATEGORIES)


@app.route("/topic/<int:topic_id>", methods=["GET", "POST"])
def topic(topic_id):
    item = db().execute("SELECT topics.*, users.username FROM topics JOIN users ON users.id = topics.author_id WHERE topics.id = ?", (topic_id,)).fetchone()
    if not item:
        abort(404)
    if request.method == "POST":
        if not g.user:
            abort(403)
        if item["locked"]:
            abort(403)
        body = request.form.get("body", "").strip()
        if not 1 <= len(body) <= 10000:
            flash("Replies must be between 1 and 10,000 characters.", "error")
        else:
            db().execute("INSERT INTO replies (topic_id, author_id, body) VALUES (?, ?, ?)", (topic_id, g.user["id"], body))
            db().commit()
            return redirect(url_for("topic", topic_id=topic_id) + "#replies")
    replies = db().execute("SELECT replies.*, users.username FROM replies JOIN users ON users.id = replies.author_id WHERE topic_id = ? ORDER BY replies.id", (topic_id,)).fetchall()
    return render_template("topic.html", topic=item, replies=replies)


@app.route("/admin")
@requires_user(admin=True)
def admin():
    users = db().execute("SELECT id, username, approved, is_admin, created_at FROM users ORDER BY approved, username").fetchall()
    return render_template("admin.html", users=users, config=settings())


@app.post("/admin/users/<int:user_id>/<action>")
@requires_user(admin=True)
def manage_user(user_id, action):
    user = db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or action not in ("approve", "revoke"):
        abort(404)
    if user["is_admin"]:
        abort(403)
    db().execute("UPDATE users SET approved = ? WHERE id = ?", (int(action == "approve"), user_id))
    db().commit()
    flash("Account updated.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/settings")
@requires_user(admin=True)
def update_settings():
    event = request.form.get("event", "").strip().lower()
    team = request.form.get("team", "").strip()
    if (event and not re.fullmatch(r"[a-z0-9]{1,30}", event)) or (team and not re.fullmatch(r"[1-9][0-9]{0,5}", team)):
        flash("Use an alphanumeric Nexus event key and a numeric team number.", "error")
    else:
        for key, value in (("event", event), ("team", team)):
            db().execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        db().commit()
        _nexus_cache.clear()
        flash("Nexus settings saved.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/topics/<int:topic_id>/<action>")
@requires_user(admin=True)
def manage_topic(topic_id, action):
    item = db().execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if not item or action not in ("pin", "lock", "delete"):
        abort(404)
    if action == "delete":
        db().execute("DELETE FROM topics WHERE id = ?", (topic_id,))
        destination = url_for("index")
    else:
        column = "pinned" if action == "pin" else "locked"
        db().execute(f"UPDATE topics SET {column} = ? WHERE id = ?", (int(not item[column]), topic_id))
        destination = url_for("topic", topic_id=topic_id)
    db().commit()
    flash("Discussion updated.", "success")
    return redirect(destination)


@app.post("/admin/replies/<int:reply_id>/delete")
@requires_user(admin=True)
def delete_reply(reply_id):
    reply = db().execute("SELECT topic_id FROM replies WHERE id = ?", (reply_id,)).fetchone()
    if not reply:
        abort(404)
    db().execute("DELETE FROM replies WHERE id = ?", (reply_id,))
    db().commit()
    flash("Reply deleted.", "success")
    return redirect(url_for("topic", topic_id=reply["topic_id"]))
