"""Local Meta contract emulator. No request is forwarded to Meta or a media URL.

Only the documented Cometmode subset is implemented. Unknown edges fail rather
than returning success. Fixtures are private to a registered MockPost app.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from mockpost.apps import get_app, get_app_by_name, list_apps, upsert_app
from mockpost.config import expire_at, utcnow
from mockpost.db import get_db
from mockpost.request_ctx import get_test_id
from mockpost.store import insert_message

router = APIRouter(prefix="/facebook", tags=["facebook-graph"])
SCOPES = {"pages_show_list", "pages_read_engagement", "pages_manage_posts",
          "instagram_basic", "instagram_content_publish", "pages_messaging",
          "instagram_manage_messages", "pages_manage_metadata"}
SCENARIOS = {"normal", "cancel_login", "no_pages", "token_expired",
             "permission_denied", "rate_limited", "media_processing", "media_error"}


class MetaError(Exception):
    def __init__(self, message: str, code: int = 100, status: int = 400):
        self.message, self.code, self.status = message, code, status


async def meta_error_handler(request: Request, error: MetaError):
    headers = {"Retry-After": "1"} if error.status == 429 else None
    return JSONResponse({"error": {"message": error.message, "type": "OAuthException",
                                   "code": error.code, "fbtrace_id": "mock-social"}},
                        status_code=error.status, headers=headers)


class PageFixture(BaseModel):
    id: str = Field(pattern=r"^[0-9]{5,40}$")
    name: str = Field(min_length=1, max_length=200)
    instagram_id: str | None = Field(default=None, pattern=r"^[0-9]{5,40}$")
    instagram_username: str = "mock_instagram"


class PersonFixture(BaseModel):
    id: str = Field(pattern=r"^[0-9]{5,40}$")
    page_id: str = Field(pattern=r"^[0-9]{5,40}$")
    platform: Literal["messenger", "instagram"]
    name: str = "Mock Visitor"
    username: str = "mock_visitor"


class Fixture(BaseModel):
    app: str = Field(min_length=1)
    app_id: str = Field(pattern=r"^[0-9]{5,40}$")
    app_secret: str = Field(min_length=16, max_length=256)
    redirect_uris: list[str] = Field(min_length=1, max_length=20)
    pages: list[PageFixture] = Field(default_factory=lambda: [PageFixture(
        id="100000000001", name="Mock Facebook Page", instagram_id="200000000001")], max_length=100)
    people: list[PersonFixture] | None = Field(default=None, max_length=200)
    user_id: str = Field(default="300000000001", pattern=r"^[0-9]{5,40}$")
    scenario: str = "normal"

    @field_validator("scenario")
    @classmethod
    def scenario_known(cls, value):
        if value not in SCENARIOS:
            raise ValueError("Unknown Meta scenario")
        return value

    @field_validator("redirect_uris")
    @classmethod
    def redirects_valid(cls, values):
        for value in values:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.fragment or url.username:
                raise ValueError("Callback must be an absolute HTTP(S) URL without credentials or fragment")
        return values

    @field_validator("pages")
    @classmethod
    def distinct_accounts(cls, pages):
        ids = [p.id for p in pages] + [p.instagram_id for p in pages if p.instagram_id]
        if len(ids) != len(set(ids)):
            raise ValueError("Page and Instagram IDs must be unique")
        return pages


@router.post("/fixtures")
async def configure(payload: Fixture):
    # Reuse the app registry instead of introducing a second credential registry.
    for app in await list_apps():
        if app["name"] != payload.app and app["creds"].get("meta", {}).get("app_id") == payload.app_id:
            raise MetaError("This fake client ID belongs to another app")
    data = payload.model_dump(exclude={"app"})
    if any(data["user_id"] in {page["id"], page.get("instagram_id")} for page in data["pages"]):
        raise MetaError("User ID must be distinct from Page and Instagram IDs")
    if data["people"] is None:
        data["people"] = [{"id": str(int(page["id"]) + offset), "page_id": page["id"],
                           "platform": platform, "name": f"Mock {platform.title()} Visitor", "username": "mock_visitor"}
                          for page in data["pages"] for platform, offset in [("messenger", 300000000000), ("instagram", 400000000000)]
                          if platform != "instagram" or page.get("instagram_id")]
    ids = [person["id"] for person in data["people"]]
    account_ids = {data["user_id"], *[p["id"] for p in data["pages"]], *[p["instagram_id"] for p in data["pages"] if p.get("instagram_id")]}
    if len(ids) != len(set(ids)) or set(ids) & account_ids:
        raise MetaError("Visitor IDs must be unique and distinct from account IDs")
    for person in data["people"]:
        page = next((p for p in data["pages"] if p["id"] == person["page_id"]), None)
        if not page or (person["platform"] == "instagram" and not page.get("instagram_id")):
            raise MetaError("Each visitor requires an accessible Page and linked Instagram account")
    app = await upsert_app(payload.app, {"meta": data})
    return {"app": app["name"], "app_id": payload.app_id, "pages": data["pages"],
            "people": data["people"], "scenario": payload.scenario, "note": "Local simulation only; use fake credentials."}


async def explicit_app(request):
    name = request.headers.get("X-MockPost-App")
    app = await get_app_by_name(name) if name else None
    if not app or "meta" not in app["creds"]:
        raise MetaError("Select a configured fake app with X-MockPost-App", status=404)
    return app


@router.put("/fixtures/scenario")
async def scenario(request: Request):
    app = await explicit_app(request)
    data = await params(request)
    choice = data.get("scenario")
    if not isinstance(choice, str) or choice not in SCENARIOS:
        raise MetaError("Unknown Meta scenario")
    await upsert_app(app["name"], {"meta": {**app["creds"]["meta"], "scenario": choice}})
    return {"scenario": choice}


@router.post("/fixtures/reset")
async def reset(request: Request):
    app = await explicit_app(request)
    db = get_db()
    await db.execute("DELETE FROM facebook_tokens WHERE app_id=?", (app["id"],))
    await db.execute("DELETE FROM facebook_objects WHERE app_id=?", (app["id"],))
    await db.execute("DELETE FROM oauth_sessions WHERE provider='facebook-graph' AND client_id=?",
                     (app["creds"]["meta"]["app_id"],))
    await db.commit()
    return {"ok": True, "note": "Tokens and Graph objects reset; captured timeline retained."}


@router.put("/fixtures/media/{identity}")
async def media_state(identity: str, request: Request):
    app = await explicit_app(request)
    data = await params(request)
    status = data.get("status_code")
    if not isinstance(status, str) or status not in {"IN_PROGRESS", "FINISHED", "ERROR"}:
        raise MetaError("Unknown media status")
    cur = await get_db().execute("UPDATE facebook_objects SET status=? WHERE id=? AND app_id=? AND kind='container' AND status!='PUBLISHED' RETURNING id",
                                (status, identity, app["id"]))
    row = await cur.fetchone()
    await get_db().commit()
    if not row:
        raise MetaError("Unknown or already published media container", status=404)
    return {"id": identity, "status_code": status}


@router.post("/fixtures/revoke")
async def revoke(request: Request):
    app = await explicit_app(request)
    data = await params(request)
    if not isinstance(data.get("access_token"), str):
        raise MetaError("access_token must be a string")
    cur = await get_db().execute("DELETE FROM facebook_tokens WHERE token=? AND app_id=? RETURNING token",
                                (data.get("access_token", ""), app["id"]))
    row = await cur.fetchone()
    await get_db().commit()
    return {"revoked": row is not None}


async def params(request):
    data = dict(request.query_params)
    if request.method not in {"GET", "HEAD"}:
        try:
            if "application/json" in request.headers.get("content-type", ""):
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError()
                data.update(body)
            else:
                data.update(dict(await request.form()))
        except (ValueError, TypeError):
            raise MetaError("Invalid request body")
    return data


def redirect(url, **values):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in {"code", "state", "error", "error_reason"}]
    return RedirectResponse(urlunsplit(parts._replace(query=urlencode(query + list(values.items())))),
                            status_code=302)


async def client_for(client_id):
    for app in await list_apps():
        if app["creds"].get("meta", {}).get("app_id") == client_id:
            return app
    raise MetaError("Unknown fake App ID", 101)


@router.get("/dialog/oauth")
@router.get("/v{version}/dialog/oauth")
async def authorize(request: Request, version: str = "19.0"):
    data = await params(request)
    app = await client_for(data.get("client_id"))
    fixture = app["creds"]["meta"]
    callback = data.get("redirect_uri")
    if callback not in fixture["redirect_uris"]:
        raise MetaError("Redirect URI is not registered for this fake app", 191)
    if fixture["scenario"] == "cancel_login":
        return redirect(callback, error="access_denied", error_reason="user_denied", state=data.get("state", ""))
    requested = set(str(data.get("scope", "")).replace(",", " ").split())
    if requested - SCOPES:
        raise MetaError("Unsupported simulated permission: " + ", ".join(sorted(requested - SCOPES)))
    code = secrets.token_urlsafe(24)
    db = get_db()
    await db.execute("INSERT INTO oauth_sessions (id,provider,client_id,redirect_uri,code,fake_user_profile,scope,state,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (secrets.token_hex(16), "facebook-graph", fixture["app_id"], callback, code,
                      json.dumps({"id": fixture["user_id"]}), " ".join(sorted(requested)), data.get("state", ""), utcnow(), expire_at(10)))
    await db.commit()
    return redirect(callback, code=code, state=data.get("state", ""))


async def mint(app_id, kind, owner, scopes, ttl=3600):
    token = "mp_meta_" + secrets.token_urlsafe(32)
    db = get_db()
    await db.execute("INSERT INTO facebook_tokens (token,app_id,kind,owner_id,scopes,expires_at) VALUES (?,?,?,?,?,?)",
                     (token, app_id, kind, owner, scopes, time.time() + ttl))
    await db.commit()
    return token


@router.api_route("/v{version}/oauth/access_token", methods=["GET", "POST"])
async def exchange(version: str, request: Request):
    data = await params(request)
    app = await client_for(data.get("client_id"))
    fixture = app["creds"]["meta"]
    if not secrets.compare_digest(str(data.get("client_secret", "")), fixture["app_secret"]):
        raise MetaError("Invalid fake App secret", 101)
    if data.get("grant_type") == "fb_exchange_token":
        grant, _ = await authenticate(request, {"access_token": data.get("fb_exchange_token")})
        if grant["app_id"] != app["id"] or grant["kind"] != "user":
            raise MetaError("Token belongs to another app", 190)
        token = await mint(app["id"], "user", grant["owner_id"], grant["scopes"], 5184000)
    else:
        if data.get("grant_type", "authorization_code") != "authorization_code":
            raise MetaError("Unsupported grant type")
        if not isinstance(data.get("code"), str) or not isinstance(data.get("redirect_uri"), str):
            raise MetaError("code and redirect_uri must be strings")
        db = get_db()
        cur = await db.execute("UPDATE oauth_sessions SET code=NULL WHERE provider='facebook-graph' AND code=? AND client_id=? AND redirect_uri=? AND expires_at>? RETURNING fake_user_profile,scope",
                               (data.get("code", ""), fixture["app_id"], data.get("redirect_uri", ""), utcnow()))
        row = await cur.fetchone()
        await db.commit()
        if not row:
            raise MetaError("Invalid, expired or already used authorization code", 100)
        token = await mint(app["id"], "user", json.loads(row["fake_user_profile"])["id"], row["scope"])
    return {"access_token": token, "token_type": "bearer", "expires_in": 5184000 if data.get("grant_type") == "fb_exchange_token" else 3600}


async def authenticate(request, data):
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else data.get("access_token")
    if not isinstance(token, str) or not token:
        raise MetaError("Invalid OAuth access token", 190)
    db = get_db()
    cur = await db.execute("SELECT * FROM facebook_tokens WHERE token=?", (token or "",))
    row = await cur.fetchone()
    if not row or row["expires_at"] <= time.time():
        raise MetaError("Invalid or expired OAuth access token", 190)
    app = await get_app(row["app_id"])
    if not app or "meta" not in app["creds"]:
        raise MetaError("Fake app no longer exists", 190)
    scenario = app["creds"]["meta"]["scenario"]
    if scenario == "token_expired":
        raise MetaError("Session has expired", 190)
    if scenario == "permission_denied":
        raise MetaError("Permissions error", 200, 403)
    if scenario == "rate_limited":
        raise MetaError("Application request limit reached", 4, 429)
    return dict(row), app


def require(grant, *scopes):
    if set(scopes) - set(grant["scopes"].split()):
        raise MetaError("Missing permission: " + ", ".join(scopes), 200, 403)


def account(app, identity):
    for page in app["creds"]["meta"]["pages"]:
        if identity == page["id"]:
            return "page", page
        if identity == page.get("instagram_id"):
            return "instagram", page
    raise MetaError("Unsupported or inaccessible account")


def owns(grant, identity, app):
    kind, page = account(app, identity)
    if grant["kind"] == "page" and grant["owner_id"] != page["id"]:
        raise MetaError("Token does not grant access to this Page", 200, 403)
    return kind, page


async def object_for(identity, app_id):
    cur = await get_db().execute("SELECT * FROM facebook_objects WHERE id=? AND app_id=?", (identity, app_id))
    row = await cur.fetchone()
    return dict(row) if row else None


async def save_object(app, owner, kind, data, status="FINISHED"):
    identity = str(secrets.randbelow(9 * 10**15) + 10**15)
    await get_db().execute("INSERT INTO facebook_objects (id,app_id,owner_id,kind,data,status) VALUES (?,?,?,?,?,?)",
                           (identity, app["id"], owner, kind, json.dumps(data), status))
    await get_db().commit()
    return identity


async def capture(request, app, owner, kind, data, identity):
    # Tokens and credentials never enter the captured request body.
    safe = {k: v for k, v in data.items() if k not in {"access_token", "client_secret", "appsecret_proof"}}
    channel = "instagram" if any(p.get("instagram_id") == owner for p in app["creds"]["meta"]["pages"]) else "facebook"
    await insert_message(channel, "outbound", str(safe.get("message") or safe.get("caption") or kind),
                         sender=owner, recipient=str(safe.get("recipient", owner)),
                         raw_payload=safe, meta={"graph_id": identity, "operation": kind},
                         test_id=get_test_id(request), app_id=app["id"])


@router.api_route("/v{version}/{identity}/{edge}", methods=["GET", "POST", "DELETE"])
async def graph_edge(version: str, identity: str, edge: str, request: Request):
    data = await params(request)
    grant, app = await authenticate(request, data)
    fixture = app["creds"]["meta"]
    if identity == "me" and edge == "accounts" and request.method == "GET":
        require(grant, "pages_show_list")
        if grant["kind"] != "user":
            raise MetaError("User token required")
        pages = []
        if fixture["scenario"] != "no_pages":
            for page in fixture["pages"]:
                token = await mint(app["id"], "page", page["id"], grant["scopes"], 5184000)
                entry = {"id": page["id"], "name": page["name"], "access_token": token,
                         "tasks": ["CREATE_CONTENT", "MESSAGING", "MANAGE"]}
                if page.get("instagram_id"):
                    entry["instagram_business_account"] = {"id": page["instagram_id"], "username": page["instagram_username"]}
                pages.append(entry)
        return {"data": pages}
    if identity == "me":
        identity = grant["owner_id"]
    if edge == "permissions" and request.method == "GET":
        return {"data": [{"permission": s, "status": "granted"} for s in grant["scopes"].split()]}
    if edge == "subscribed_apps":
        owns(grant, identity, app)
        require(grant, "pages_manage_metadata")
        cur = await get_db().execute("SELECT id FROM facebook_objects WHERE app_id=? AND owner_id=? AND kind='subscription'", (app["id"], identity))
        existing = await cur.fetchone()
        if request.method == "GET":
            return {"data": [{"id": fixture["app_id"], "name": app["name"]}] if existing else []}
        if request.method == "DELETE":
            await get_db().execute("DELETE FROM facebook_objects WHERE app_id=? AND owner_id=? AND kind='subscription'", (app["id"], identity))
            await get_db().commit()
        elif not existing:
            await save_object(app, identity, "subscription", {})
        return {"success": True}
    # Comments address a previously returned post ID instead of an account ID.
    parent = await object_for(identity, app["id"])
    owner = parent["owner_id"] if parent else identity
    kind, page = owns(grant, owner, app)
    if request.method == "GET":
        if edge not in {"feed", "posts", "media"}:
            raise MetaError("Unsupported Graph read edge")
        require(grant, "instagram_basic" if kind == "instagram" else "pages_read_engagement")
        cur = await get_db().execute("SELECT id,data FROM facebook_objects WHERE app_id=? AND owner_id=? AND kind='post' ORDER BY rowid", (app["id"], owner))
        return {"data": [{"id": r["id"], **json.loads(r["data"])} for r in await cur.fetchall()]}
    if request.method != "POST":
        raise MetaError("Unsupported Graph operation")
    if edge == "messages":
        recipient = data.get("recipient")
        message = data.get("message")
        if not isinstance(recipient, dict) or not isinstance(recipient.get("id"), str) or not recipient["id"]:
            raise MetaError("recipient.id is required")
        person = next((p for p in fixture.get("people", []) if p["id"] == recipient["id"]), None)
        if person:
            if person["page_id"] != page["id"]:
                raise MetaError("Recipient belongs to another Page", 200, 403)
            if kind == "instagram" and person["platform"] != "instagram":
                raise MetaError("Recipient belongs to another messaging platform")
            if person["platform"] == "instagram":
                kind, owner = "instagram", page["instagram_id"]
        else:
            raise MetaError("Unknown messaging recipient; declare the visitor in the fixture")
        require(grant, "instagram_manage_messages" if kind == "instagram" else "pages_messaging")
        action = data.get("sender_action")
        if isinstance(action, str) and action in {"typing_on", "typing_off", "mark_seen"} and message is None:
            return {"recipient_id": recipient["id"]}
        if not isinstance(message, dict) or not message:
            raise MetaError("message or a supported sender_action is required")
        identity = await save_object(app, owner, "message", {"recipient": recipient, "message": message})
        await capture(request, app, owner, "message", {"recipient": recipient["id"], "message": message}, identity)
        return {"recipient_id": recipient["id"], "message_id": "mid." + identity}
    require(grant, "instagram_content_publish" if kind == "instagram" else "pages_manage_posts")
    if edge == "media" and kind == "instagram":
        if not (data.get("image_url") or data.get("video_url") or data.get("children")):
            raise MetaError("Media URL or carousel children required")
        if data.get("children"):
            children = data["children"] if isinstance(data["children"], list) else str(data["children"]).split(",")
            if not 2 <= len(children) <= 10:
                raise MetaError("Carousel requires 2 to 10 children")
            if any(not isinstance(child, str) or not child for child in children) or len(children) != len(set(children)):
                raise MetaError("Carousel children must be distinct container IDs")
            for child in children:
                obj = await object_for(child, app["id"])
                if not obj or obj["owner_id"] != owner or obj["kind"] != "container":
                    raise MetaError("Unknown carousel child")
                if json.loads(obj["data"]).get("children"):
                    raise MetaError("A carousel cannot contain another carousel")
        status = {"media_processing": "IN_PROGRESS", "media_error": "ERROR"}.get(fixture["scenario"], "FINISHED")
        identity = await save_object(app, owner, "container", {k: v for k, v in data.items() if k != "access_token"}, status)
        return {"id": identity}
    if edge == "media_publish" and kind == "instagram":
        container = await object_for(str(data.get("creation_id", "")), app["id"])
        if not container or container["owner_id"] != owner or container["kind"] != "container":
            raise MetaError("Unknown media container")
        if await container_status(container, app["id"]) != "FINISHED":
            raise MetaError("Media is not ready for publishing", 9007)
        post_data = json.loads(container["data"])
        children = post_data.get("children", [])
        if isinstance(children, str):
            children = children.split(",")
        identities = [container["id"], *map(str, children)]
        placeholders = ",".join("?" for _ in identities)
        # Materialize eligibility before changing any row. One SQLite statement
        # claims the parent AND every child, even when two parents race.
        cur = await get_db().execute(
            f"WITH eligible AS MATERIALIZED (SELECT COUNT(*) AS n FROM facebook_objects WHERE app_id=? AND id IN ({placeholders}) AND status='FINISHED') "
            f"UPDATE facebook_objects SET status='PUBLISHED' WHERE app_id=? AND id IN ({placeholders}) AND (SELECT n FROM eligible)=? RETURNING id",
            (app["id"], *identities, app["id"], *identities, len(identities)))
        claimed = await cur.fetchall()
        await get_db().commit()
        if len(claimed) != len(identities):
            raise MetaError("Media container or carousel child already published")
        identity = await save_object(app, owner, "post", post_data)
        await capture(request, app, owner, "media_publish", post_data, identity)
        return {"id": identity}
    if edge == "video_reels" and kind == "page":
        if data.get("upload_phase") == "start":
            identity = await save_object(app, owner, "video", {}, "WAITING_UPLOAD")
            return {"video_id": identity, "upload_url": str(request.base_url).rstrip("/") + f"/facebook/upload/{identity}"}
        video = await object_for(str(data.get("video_id", "")), app["id"])
        if not video or video["owner_id"] != owner or video["status"] != "UPLOADED" or data.get("upload_phase") != "finish":
            raise MetaError("Video upload is incomplete")
        cur = await get_db().execute("UPDATE facebook_objects SET status='PUBLISHED' WHERE id=? AND status='UPLOADED' RETURNING id", (video["id"],))
        claimed = await cur.fetchone()
        await get_db().commit()
        if not claimed:
            raise MetaError("Reel already published")
        await capture(request, app, owner, "reel", data, video["id"])
        return {"success": True}
    if edge not in {"feed", "photos", "photo_stories", "comments"} or (kind == "instagram" and edge != "comments"):
        raise MetaError("Unsupported Graph publishing edge")
    if edge == "comments" and not parent:
        raise MetaError("Unknown post")
    if edge == "feed" and not (data.get("message") or data.get("attached_media")):
        raise MetaError("Message or attached media required")
    if edge == "feed" and data.get("attached_media"):
        attachments = data["attached_media"]
        if isinstance(attachments, str):
            try:
                attachments = json.loads(attachments)
            except ValueError:
                raise MetaError("Invalid attached_media")
        if not isinstance(attachments, list) or not attachments:
            raise MetaError("attached_media must be a nonempty array")
        for attachment in attachments:
            photo = await object_for(str(attachment.get("media_fbid", "")), app["id"]) if isinstance(attachment, dict) else None
            if not photo or photo["owner_id"] != owner or photo["kind"] != "photo":
                raise MetaError("Unknown or inaccessible attached photo")
    if edge == "photos" and not data.get("url"):
        raise MetaError("Photo URL required")
    if edge == "photo_stories":
        photo = await object_for(str(data.get("photo_id", "")), app["id"])
        if not photo or photo["owner_id"] != owner or photo["kind"] != "photo":
            raise MetaError("Unknown photo")
    if edge == "comments" and not data.get("message"):
        raise MetaError("Comment text required")
    safe = {k: v for k, v in data.items() if k != "access_token"}
    is_draft = edge == "photos" and str(data.get("published", True)).lower() == "false"
    identity = await save_object(app, owner, "comment" if edge == "comments" else "photo" if is_draft else "post", safe)
    if not is_draft:
        await capture(request, app, owner, edge, safe, identity)
    return {"id": identity, **({"post_id": identity} if edge in {"photos", "photo_stories"} else {})}


@router.post("/upload/{identity}")
async def upload(identity: str, request: Request):
    auth = request.headers.get("authorization", "")
    # Meta's hosted upload uses Authorization: OAuth <page-token>.
    data = {"access_token": auth[6:] if auth.startswith("OAuth ") else ""}
    grant, app = await authenticate(request, data)
    video = await object_for(identity, app["id"])
    if not video or video["kind"] != "video":
        raise MetaError("Unknown video upload")
    owns(grant, video["owner_id"], app)
    require(grant, "pages_manage_posts")
    if not request.headers.get("file_url"):
        raise MetaError("Hosted upload requires file_url")
    cur = await get_db().execute("UPDATE facebook_objects SET status='UPLOADED' WHERE id=? AND app_id=? AND status='WAITING_UPLOAD' RETURNING id", (identity, app["id"]))
    claimed = await cur.fetchone()
    await get_db().commit()
    if not claimed:
        raise MetaError("Video upload is already complete")
    return {"success": True}


async def container_status(obj, app_id):
    if obj["status"] != "FINISHED":
        return obj["status"]
    children = json.loads(obj["data"]).get("children", [])
    if isinstance(children, str):
        children = children.split(",")
    for child in children:
        item = await object_for(str(child), app_id)
        if not item or item["status"] in {"ERROR", "PUBLISHED"}:
            return "ERROR"
        if item["status"] != "FINISHED":
            return "IN_PROGRESS"
    return "FINISHED"


@router.get("/v{version}/{identity}")
async def graph_node(version: str, identity: str, request: Request):
    data = await params(request)
    grant, app = await authenticate(request, data)
    if identity == "me":
        identity = grant["owner_id"]
    if identity == app["creds"]["meta"]["user_id"] and grant["kind"] == "user":
        return {"id": identity, "name": "Mock Facebook User"}
    obj = await object_for(identity, app["id"])
    if obj:
        owns(grant, obj["owner_id"], app)
        return {"id": identity, "status_code": await container_status(obj, app["id"]) if obj["kind"] == "container" else obj["status"], **json.loads(obj["data"])}
    person = next((p for p in app["creds"]["meta"].get("people", []) if p["id"] == identity), None)
    if person:
        owns(grant, person["page_id"], app)
        require(grant, "instagram_manage_messages" if person["platform"] == "instagram" else "pages_messaging")
        first, _, last = person["name"].partition(" ")
        return {"id": identity, "name": person["name"], "first_name": first, "last_name": last,
                "username": person["username"], "locale": "en_US", "timezone": 0}
    kind, page = owns(grant, identity, app)
    require(grant, "instagram_basic" if kind == "instagram" else "pages_read_engagement")
    return {"id": identity, "name": page["name"], **({"username": page["instagram_username"]} if kind == "instagram" else
            {"instagram_business_account": {"id": page["instagram_id"], "username": page["instagram_username"]}} if page.get("instagram_id") else {})}


class IncomingMessage(BaseModel):
    platform: Literal["messenger", "instagram"]
    sender_id: str
    text: str = Field(min_length=1, max_length=10000)
    message_id: str | None = Field(default=None, max_length=200)
    webhook_id: str


@router.post("/simulate/message")
async def simulate_message(payload: IncomingMessage, request: Request):
    from mockpost.routers.social import _deliver_social
    from mockpost.signing import facebook_signature
    app = await explicit_app(request)
    fixture = app["creds"]["meta"]
    person = next((p for p in fixture.get("people", []) if p["id"] == payload.sender_id and p["platform"] == payload.platform), None)
    if not person:
        raise MetaError("Unknown visitor for this platform")
    page = next(p for p in fixture["pages"] if p["id"] == person["page_id"])
    cur = await get_db().execute("SELECT id FROM facebook_objects WHERE app_id=? AND owner_id=? AND kind='subscription'", (app["id"], page["id"]))
    if not await cur.fetchone():
        raise MetaError("Page is not subscribed", status=409)
    cur = await get_db().execute("SELECT * FROM webhooks_registry WHERE id=? AND app_id=? AND channel='facebook'", (payload.webhook_id, app["id"]))
    webhook = await cur.fetchone()
    if not webhook:
        raise MetaError("Webhook does not belong to this fake app", status=404)
    recipient = page["id"] if payload.platform == "messenger" else page["instagram_id"]
    mid = payload.message_id or "mid.mock." + secrets.token_hex(16)
    timestamp = int(time.time() * 1000)
    event = {"object": "page" if payload.platform == "messenger" else "instagram", "entry": [{"id": recipient,
             "time": timestamp, "messaging": [{"sender": {"id": person["id"]}, "recipient": {"id": recipient},
             "timestamp": timestamp, "message": {"mid": mid, "text": payload.text}}]}]}
    body = json.dumps(event, ensure_ascii=False)
    signature = facebook_signature(body, webhook["token"] or fixture["app_secret"])
    result = await _deliver_social("facebook", "facebook", "messages", event, request, signature, webhook_id=webhook["id"])
    await insert_message("facebook" if payload.platform == "messenger" else "instagram", "inbound", payload.text,
                         sender=person["id"], recipient=recipient, app_id=app["id"], test_id=get_test_id(request),
                         raw_payload=event, meta={"message_id": mid, "operation": "inbound"})
    return {"message_id": mid, "delivered": result}
