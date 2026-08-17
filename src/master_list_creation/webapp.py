"""Admin interface and token-authenticated anime-list API."""

from __future__ import annotations

from functools import wraps
import hmac
import math
import os
from pathlib import Path
import secrets
import time
from typing import Any, Callable, TypeVar

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from .store import SCHEMA_VERSION, VersionStore


View = TypeVar("View", bound=Callable[..., Any])
LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("SESSION_SECRET"),
        ADMIN_EMAIL=os.getenv("ADMIN_EMAIL", "ege@rousoftware.com"),
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD"),
        APP_TOKEN=os.getenv("APP_TOKEN"),
        DATA_DIR=os.getenv("DATA_DIR", "/data"),
        SEED_LIST_PATH=os.getenv(
            "SEED_LIST_PATH", "/app/seed/libraryMasterList.json"
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() == "true",
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    if config:
        app.config.update(config)
    for key in ("SECRET_KEY", "ADMIN_PASSWORD", "APP_TOKEN"):
        if not app.config.get(key):
            raise RuntimeError(f"required application setting {key} is missing")

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]
    store = VersionStore(Path(app.config["DATA_DIR"]))
    store.initialize(Path(app.config["SEED_LIST_PATH"]))
    app.extensions["version_store"] = store

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    def require_csrf() -> None:
        expected = session.get("csrf_token", "")
        supplied = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, "Invalid or expired form token")

    def admin_required(view: View) -> View:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not session.get("admin"):
                return redirect(url_for("login", next=request.full_path))
            return view(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    def app_token_required(view: View) -> View:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            authorization = request.headers.get("Authorization", "")
            bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
            supplied = bearer or request.headers.get("X-App-Token", "")
            if not supplied or not hmac.compare_digest(
                str(app.config["APP_TOKEN"]), supplied
            ):
                response = jsonify(
                    {"error": "unauthorized", "message": "A valid app token is required."}
                )
                response.status_code = 401
                response.headers["WWW-Authenticate"] = "Bearer"
                return response
            return view(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https:; style-src 'self'; "
            "script-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        return response

    @app.get("/healthz")
    def health() -> Response:
        try:
            latest = store.latest()
        except (LookupError, OSError):
            return jsonify({"status": "not_ready"}), 503
        return jsonify({"status": "ok", "version": latest["version"]})

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response | str:
        if session.get("admin"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            require_csrf()
            now = time.monotonic()
            client = request.remote_addr or "unknown"
            attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(client, []) if now - stamp < 900]
            if len(attempts) >= 10:
                return render_template("login.html", rate_limited=True), 429
            email = request.form.get("email", "").strip().casefold()
            password = request.form.get("password", "")
            valid = hmac.compare_digest(
                email, str(app.config["ADMIN_EMAIL"]).casefold()
            ) and hmac.compare_digest(password, str(app.config["ADMIN_PASSWORD"]))
            if valid:
                LOGIN_ATTEMPTS.pop(client, None)
                session.clear()
                session["admin"] = True
                session["csrf_token"] = secrets.token_urlsafe(32)
                return redirect(url_for("dashboard"))
            attempts.append(now)
            LOGIN_ATTEMPTS[client] = attempts
            return render_template("login.html", invalid=True), 401
        return render_template("login.html")

    @app.post("/logout")
    @admin_required
    def logout() -> Response:
        require_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @admin_required
    def dashboard() -> str:
        return render_template(
            "dashboard.html",
            latest=store.latest(),
            versions=store.versions(),
            jobs=store.recent_jobs(),
        )

    @app.post("/admin/updates")
    @admin_required
    def request_update() -> Response:
        require_csrf()
        job_id, created = store.enqueue_update(str(app.config["ADMIN_EMAIL"]))
        if created:
            flash(f"Update job #{job_id} is queued.", "success")
        else:
            flash(f"Job #{job_id} already covers this request.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/admin/asset-refresh")
    @admin_required
    def request_asset_refresh() -> Response:
        require_csrf()
        job_id, created = store.enqueue_update(
            str(app.config["ADMIN_EMAIL"]), kind="asset_refresh"
        )
        if created:
            flash(
                f"Asset refresh job #{job_id} is queued. Every upstream source is "
                "re-downloaded and cover art is re-selected, which takes several "
                "minutes; a new version is published only if records changed.",
                "success",
            )
        else:
            flash(f"Job #{job_id} already covers this asset refresh.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/anime")
    @admin_required
    def anime_browser() -> str:
        query = request.args.get("q", "").strip()[:120]
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        anime, total = store.anime_page(query, page)
        pages = max(1, math.ceil(total / 48))
        if page > pages:
            page = pages
            anime, total = store.anime_page(query, page)
        return render_template(
            "anime.html",
            anime=anime,
            query=query,
            page=page,
            pages=pages,
            total=total,
            latest=store.latest(),
        )

    @app.get("/api/v1/anime-list/version")
    @app_token_required
    def api_version() -> Response:
        latest = store.latest()
        return jsonify(
            {
                "schemaVersion": SCHEMA_VERSION,
                "version": latest["version"],
                "sourceVersion": latest["source_version"],
                "checksum": latest["checksum"],
                "animeCount": latest["anime_count"],
                "createdAt": latest["created_at"],
            }
        )

    @app.get("/api/v1/anime-list")
    @app_token_required
    def api_full_list() -> Response:
        latest = store.latest()
        response = send_file(
            store.snapshot_path(),
            mimetype="application/json",
            conditional=True,
            etag=latest["checksum"].removeprefix("sha256:"),
            download_name="libraryMasterList.json",
        )
        response.headers["X-Anime-List-Version"] = str(latest["version"])
        response.headers["X-Anime-List-Checksum"] = latest["checksum"]
        response.headers["Cache-Control"] = "private, max-age=300"
        return response

    @app.get("/api/v1/anime-list/updates")
    @app_token_required
    def api_updates() -> Response:
        try:
            from_version = int(request.args["fromVersion"])
            latest_version = int(store.latest()["version"])
            to_version = int(request.args.get("toVersion", latest_version))
            path = store.get_or_create_diff(from_version, to_version)
        except KeyError:
            return jsonify({"error": "invalid_request", "message": "fromVersion is required."}), 400
        except ValueError as error:
            return jsonify({"error": "invalid_request", "message": str(error)}), 400
        except LookupError as error:
            return jsonify({"error": "version_not_found", "message": str(error)}), 404
        response = send_file(path, mimetype="application/json", conditional=True)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    return app


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    create_app().run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
