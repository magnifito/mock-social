"""Real HTTP contract checks for the Cometmode Meta subset."""
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

APP = "cometmode-test"
ID = "12345678"
SECRET = "syntheticSecret123456"
CALLBACK = "http://localhost:3200/api/social/auth/callback/facebook"
PAGE = "100000000001"
IG = "200000000001"
SCOPE = "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish,pages_messaging,pages_manage_metadata,instagram_manage_messages"
BASE = "/facebook/v19.0"


def fixture(client, **overrides):
    data = {"app": APP, "app_id": ID, "app_secret": SECRET,
            "redirect_uris": [CALLBACK], **overrides}
    response = client.post("/facebook/fixtures", json=data)
    assert response.status_code == 200, response.text
    return data


def authorize(client, scope=SCOPE, **overrides):
    response = client.get(BASE + "/dialog/oauth", params={
        "client_id": ID, "redirect_uri": CALLBACK, "scope": scope,
        "state": "signed.state+with&reserved=characters", **overrides})
    assert response.status_code == 302, response.text
    values = parse_qs(urlsplit(response.headers["location"]).query)
    assert values["state"] == ["signed.state+with&reserved=characters"]
    return values


def user_token(client, scope=SCOPE):
    values = authorize(client, scope)
    response = client.post(BASE + "/oauth/access_token", data={
        "client_id": ID, "client_secret": SECRET, "code": values["code"][0], "redirect_uri": CALLBACK})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def page_token(client):
    token = user_token(client)
    response = client.get(BASE + "/me/accounts", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200, response.text
    return response.json()["data"][0]["access_token"]


def test_connect_discover_publish_and_inspect(client):
    fixture(client)
    token = user_token(client)
    long = client.post(BASE + "/oauth/access_token", data={"grant_type": "fb_exchange_token",
        "client_id": ID, "client_secret": SECRET, "fb_exchange_token": token})
    assert long.status_code == 200
    pages = client.get(BASE + "/me/accounts", headers={"Authorization": "Bearer " + long.json()["access_token"]}).json()["data"]
    assert pages[0]["instagram_business_account"]["id"] == IG
    token = pages[0]["access_token"]
    post = client.post(BASE + f"/{PAGE}/feed", json={"access_token": token, "message": "Local test post"})
    assert post.status_code == 200
    published = client.get(BASE + f"/{PAGE}/feed", params={"access_token": token}).json()["data"]
    assert published[0]["id"] == post.json()["id"]
    captured = client.get("/api/messages", params={"app": APP, "channel": "facebook"}).json()
    assert captured[0]["body"] == "Local test post"
    assert token not in json.dumps(captured)
    timeline = client.get("/timeline/markdown", params={"app": APP})
    assert "Local test post" in timeline.text


def test_oauth_rejects_wrong_callback_secret_and_code_reuse(client):
    fixture(client)
    assert client.get(BASE + "/dialog/oauth", params={"client_id": ID, "redirect_uri": "http://bad/cb"}).json()["error"]["code"] == 191
    code = authorize(client)["code"][0]
    data = {"client_id": ID, "client_secret": "wrong", "code": code, "redirect_uri": CALLBACK}
    assert client.post(BASE + "/oauth/access_token", data=data).status_code == 400
    data["client_secret"] = SECRET
    assert client.post(BASE + "/oauth/access_token", data=data).status_code == 200
    assert client.post(BASE + "/oauth/access_token", data=data).status_code == 400


def test_missing_permissions_and_unknown_endpoints_fail(client):
    fixture(client)
    token = user_token(client, "pages_show_list")
    assert client.post(BASE + f"/{PAGE}/feed", json={"access_token": token, "message": "Denied"}).status_code == 403
    token = page_token(client)
    assert client.post(BASE + f"/{PAGE}/invented", json={"access_token": token}).status_code == 400
    assert client.get(BASE + f"/{PAGE}/invented", params={"access_token": token}).status_code == 400
    assert client.get("/api/messages", params={"app": APP}).json() == []


@pytest.mark.parametrize("scenario,code,status", [("token_expired",190,400), ("permission_denied",200,403), ("rate_limited",4,429)])
def test_failure_scenarios_do_not_capture_success(client, scenario, code, status):
    fixture(client)
    token = page_token(client)
    assert client.put("/facebook/fixtures/scenario", headers={"X-MockPost-App": APP}, json={"scenario": scenario}).status_code == 200
    response = client.post(BASE + f"/{PAGE}/feed", json={"access_token": token, "message": "Not published"})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert client.get("/api/messages", params={"app": APP}).json() == []


def test_instagram_container_has_one_publish(client):
    fixture(client)
    token = page_token(client)
    container = client.post(BASE + f"/{IG}/media", json={"image_url": "https://never-fetched.invalid/image.jpg", "caption": "IG test", "access_token": token}).json()["id"]
    assert client.get(BASE + f"/{container}", params={"access_token": token}).json()["status_code"] == "FINISHED"
    body = {"creation_id": container, "access_token": token}
    assert client.post(BASE + f"/{IG}/media_publish", json=body).status_code == 200
    assert client.post(BASE + f"/{IG}/media_publish", json=body).status_code == 400
    assert len(client.get("/api/messages", params={"app": APP,"channel":"instagram"}).json()) == 1


@pytest.mark.parametrize("scenario,status", [("media_processing","IN_PROGRESS"),("media_error","ERROR")])
def test_media_not_ready_cannot_publish(client, scenario, status):
    fixture(client)
    token = page_token(client)
    client.put("/facebook/fixtures/scenario", headers={"X-MockPost-App": APP}, json={"scenario": scenario})
    identity = client.post(BASE + f"/{IG}/media", json={"video_url": "https://never-fetched.invalid/video.mp4", "access_token": token}).json()["id"]
    assert client.get(BASE + f"/{identity}", params={"access_token": token}).json()["status_code"] == status
    assert client.post(BASE + f"/{IG}/media_publish", json={"creation_id":identity,"access_token":token}).json()["error"]["code"] == 9007


def test_reel_hosted_upload_and_comment(client):
    fixture(client)
    token = page_token(client)
    start = client.post(BASE + f"/{PAGE}/video_reels", json={"upload_phase":"start","access_token":token}).json()
    assert client.post(start["upload_url"], headers={"Authorization":"OAuth " + token,"file_url":"https://never-fetched.invalid/video.mp4"}).json()["success"]
    assert client.post(BASE + f"/{PAGE}/video_reels", json={"upload_phase":"finish","video_id":start["video_id"],"description":"Local reel","access_token":token}).json()["success"]
    assert client.post(BASE + f"/{start['video_id']}/comments", json={"message":"First comment","access_token":token}).status_code == 200


def test_basic_messenger_and_app_isolation(client):
    fixture(client)
    token = page_token(client)
    sent = client.post(BASE + f"/{PAGE}/messages", json={"recipient":{"id":"400000000001"},"message":{"text":"Hello"},"access_token":token})
    assert sent.json()["recipient_id"] == "400000000001"
    fixture(client, app="other", app_id="87654321", pages=[{"id":"100000000002","name":"Other page"}])
    assert client.get(BASE + "/100000000002", params={"access_token":token}).status_code == 400
    assert client.get("/api/messages", params={"app":"other"}).json() == []


def test_cancel_login_no_pages_and_reset(client):
    fixture(client, scenario="cancel_login")
    assert authorize(client)["error"] == ["access_denied"]
    fixture(client, scenario="no_pages")
    token = user_token(client)
    assert client.get(BASE + "/me/accounts", params={"access_token":token}).json() == {"data":[]}
    assert client.post("/facebook/fixtures/reset", headers={"X-MockPost-App":APP}).status_code == 200
    assert client.get(BASE + "/me", params={"access_token":token}).json()["error"]["code"] == 190


def test_webhook_signature_matches_actual_utf8_bytes(client):
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((body, self.headers.get("X-Hub-Signature-256")))
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1",0), Receiver)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        client.post("/api/apps",json={"name":APP})
        client.post("/api/webhooks",json={"app":APP,"channel":"facebook","token":SECRET,"target_url":f"http://127.0.0.1:{server.server_port}/callback"})
        result = client.post("/facebook/simulate",headers={"X-MockPost-App":APP},json={"payload":{"object":"page","entry":[{"text":"Здравей 👋"}]}})
        assert result.json()["delivered"]["response_code"] == 200
        body, signature = received[0]
        assert signature == "sha256=" + hmac.new(SECRET.encode(),body,hashlib.sha256).hexdigest()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_media_recovery_revoke_and_subscription_state(client):
    fixture(client)
    token = page_token(client)
    headers = {"X-MockPost-App": APP}
    client.put("/facebook/fixtures/scenario", headers=headers, json={"scenario":"media_processing"})
    container = client.post(BASE + f"/{IG}/media",json={"image_url":"https://never-fetched.invalid/a.jpg","access_token":token}).json()["id"]
    assert client.put(f"/facebook/fixtures/media/{container}",headers=headers,json={"status_code":"FINISHED"}).status_code == 200
    assert client.post(BASE + f"/{IG}/media_publish",json={"creation_id":container,"access_token":token}).status_code == 200
    assert client.put(f"/facebook/fixtures/media/{container}",headers=headers,json={"status_code":"FINISHED"}).status_code == 404
    assert client.get(BASE + f"/{PAGE}/subscribed_apps",params={"access_token":token}).json() == {"data":[]}
    assert client.post(BASE + f"/{PAGE}/subscribed_apps",json={"access_token":token}).json()["success"]
    assert len(client.get(BASE + f"/{PAGE}/subscribed_apps",params={"access_token":token}).json()["data"]) == 1
    assert client.delete(BASE + f"/{PAGE}/subscribed_apps",params={"access_token":token}).json()["success"]
    assert client.get(BASE + f"/{PAGE}/subscribed_apps",params={"access_token":token}).json() == {"data":[]}
    assert client.post("/facebook/fixtures/revoke",headers=headers,json={"access_token":token}).json()["revoked"]
    assert client.get(BASE + f"/{PAGE}",params={"access_token":token}).json()["error"]["code"] == 190


def test_two_pages_cannot_use_each_others_tokens(client):
    fixture(client,pages=[{"id":PAGE,"name":"First"},{"id":"100000000002","name":"Second"}])
    token = page_token(client)
    response = client.post(BASE + "/100000000002/feed",json={"access_token":token,"message":"Wrong Page"})
    assert response.status_code == 403
    assert client.get("/api/messages",params={"app":APP}).json() == []


def test_facebook_carousel_rejects_foreign_and_missing_photos(client):
    fixture(client,pages=[{"id":PAGE,"name":"First"},{"id":"100000000002","name":"Second"}])
    user = user_token(client)
    pages = client.get(BASE+"/me/accounts",params={"access_token":user}).json()["data"]
    foreign = client.post(BASE+"/100000000002/photos",json={"access_token":pages[1]["access_token"],"url":"https://never-fetched.invalid/a.jpg","published":False}).json()["id"]
    for identity in [foreign,"does-not-exist"]:
        result = client.post(BASE+f"/{PAGE}/feed",json={"access_token":pages[0]["access_token"],"message":"Denied carousel","attached_media":[{"media_fbid":identity}]})
        assert result.status_code == 400
    assert client.get("/api/messages",params={"app":APP}).json() == []


@pytest.mark.parametrize("state",["ERROR","IN_PROGRESS","PUBLISHED"])
def test_instagram_carousel_cannot_publish_unready_or_consumed_children(client,state):
    fixture(client)
    token = page_token(client)
    children = [client.post(BASE+f"/{IG}/media",json={"image_url":"https://never-fetched.invalid/a.jpg","access_token":token}).json()["id"] for _ in range(2)]
    parent = client.post(BASE+f"/{IG}/media",json={"media_type":"CAROUSEL","children":children,"access_token":token}).json()["id"]
    if state == "PUBLISHED":
        assert client.post(BASE+f"/{IG}/media_publish",json={"creation_id":children[0],"access_token":token}).status_code == 200
    else:
        assert client.put("/facebook/fixtures/media/"+children[0],headers={"X-MockPost-App":APP},json={"status_code":state}).status_code == 200
    assert client.get(BASE+"/"+parent,params={"access_token":token}).json()["status_code"] != "FINISHED"
    assert client.post(BASE+f"/{IG}/media_publish",json={"creation_id":parent,"access_token":token}).status_code == 400


def test_concurrent_reel_finish_has_one_capture(client):
    from concurrent.futures import ThreadPoolExecutor
    fixture(client)
    token = page_token(client)
    start = client.post(BASE+f"/{PAGE}/video_reels",json={"access_token":token,"upload_phase":"start"}).json()
    assert client.post(start["upload_url"],headers={"Authorization":"OAuth "+token,"file_url":"https://never-fetched.invalid/a.mp4"}).status_code == 200
    def finish(_):
        return client.post(BASE+f"/{PAGE}/video_reels",json={"access_token":token,"upload_phase":"finish","video_id":start["video_id"]}).status_code
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(finish,range(6)))
    assert statuses.count(200) == 1
    assert statuses.count(400) == 5
    assert len(client.get("/api/messages",params={"app":APP}).json()) == 1


def test_carousel_parents_atomically_claim_shared_children(client):
    from concurrent.futures import ThreadPoolExecutor
    fixture(client)
    token = page_token(client)
    children = [client.post(BASE+f"/{IG}/media",json={"image_url":"https://never-fetched.invalid/a.jpg","access_token":token}).json()["id"] for _ in range(2)]
    parents = [client.post(BASE+f"/{IG}/media",json={"children":children,"media_type":"CAROUSEL","access_token":token}).json()["id"] for _ in range(2)]
    def publish(identity):
        return client.post(BASE+f"/{IG}/media_publish",json={"creation_id":identity,"access_token":token}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(publish,parents))
    assert sorted(statuses) == [200,400]
    assert len(client.get("/api/messages",params={"app":APP}).json()) == 1


def test_malformed_controls_and_graph_tokens_return_errors(client):
    fixture(client)
    assert client.post(BASE+f"/{PAGE}/feed",json={"access_token":{},"message":"invalid"}).json()["error"]["code"] == 190
    assert client.post(BASE+"/oauth/access_token",json={"client_id":ID,"client_secret":SECRET,"code":[],"redirect_uri":CALLBACK}).status_code == 400
    assert client.put("/facebook/fixtures/scenario",headers={"X-MockPost-App":APP},json={"scenario":{}}).status_code == 400
    assert client.put("/facebook/fixtures/media/missing",headers={"X-MockPost-App":APP},json={"status_code":{}}).status_code == 400


def test_channel_profile_and_instagram_send_via_page_token(client):
    fixture(client)
    token = page_token(client)
    profile = client.get(BASE+"/me",params={"access_token":token}).json()
    assert profile["instagram_business_account"]["id"] == IG
    for person in ["400000000001","500000000001"]:
        assert client.get(BASE+"/"+person,params={"access_token":token}).json()["name"].startswith("Mock")
    for platform, person in [("facebook","400000000001"),("instagram","500000000001")]:
        action = client.post(BASE+"/me/messages",json={"access_token":token,"recipient":{"id":person},"sender_action":"typing_on"})
        assert action.status_code == 200
        sent = client.post(BASE+"/me/messages",json={"access_token":token,"recipient":{"id":person},"message":{"text":"Channel test"}})
        assert sent.status_code == 200
        assert len(client.get("/api/messages",params={"app":APP,"channel":platform}).json()) == 1


def test_visitor_profile_and_send_are_page_scoped(client):
    fixture(client,pages=[{"id":PAGE,"name":"First"},{"id":"100000000002","name":"Second"}])
    token = page_token(client)
    assert client.get(BASE+"/400000000002",params={"access_token":token}).status_code == 403
    assert client.post(BASE+"/me/messages",json={"access_token":token,"recipient":{"id":"400000000002"},"message":{"text":"Denied"}}).status_code == 403


def test_high_level_inbound_uses_selected_webhook_and_requires_subscription(client):
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body=self.rfile.read(int(self.headers["Content-Length"]))
            assert self.headers["X-Hub-Signature-256"] == "sha256="+hmac.new(SECRET.encode(),body,hashlib.sha256).hexdigest()
            received.append((self.path,json.loads(body)))
            self.send_response(200);self.end_headers();self.wfile.write(b"ok")
        def log_message(self,*args): pass
    server=HTTPServer(("127.0.0.1",0),Receiver)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        fixture(client)
        token=page_token(client)
        webhook=client.post("/api/webhooks",json={"app":APP,"channel":"facebook","token":SECRET,"target_url":f"http://127.0.0.1:{server.server_port}/chosen"}).json()["id"]
        client.post("/api/webhooks",json={"app":APP,"channel":"facebook","token":SECRET,"target_url":f"http://127.0.0.1:{server.server_port}/other"})
        body={"platform":"instagram","sender_id":"500000000001","text":"Здравей","webhook_id":webhook,"message_id":"mid.stable"}
        headers={"X-MockPost-App":APP}
        assert client.post("/facebook/simulate/message",headers=headers,json=body).status_code == 409
        client.post(BASE+"/me/subscribed_apps",json={"access_token":token})
        result=client.post("/facebook/simulate/message",headers=headers,json=body)
        assert result.status_code == 200
        assert result.json()["delivered"]["response_code"] == 200
        assert received[0][0] == "/chosen"
        assert received[0][1]["object"] == "instagram"
        assert received[0][1]["entry"][0]["messaging"][0]["message"]["mid"] == "mid.stable"
        body["webhook_id"]="foreign"
        assert client.post("/facebook/simulate/message",headers=headers,json=body).status_code == 404
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_messages_reject_unknown_or_wrong_platform_recipients(client):
    fixture(client)
    token = page_token(client)
    for path, recipient in [("/me/messages", "999999999999"), (f"/{IG}/messages", "400000000001")]:
        result = client.post(BASE+path,json={"access_token":token,"recipient":{"id":recipient},"message":{"text":"Invalid recipient"}})
        assert result.status_code == 400
    assert client.get("/api/messages",params={"app":APP}).json() == []


def test_reel_upload_replay_fails_after_upload_and_publication(client):
    fixture(client)
    token = page_token(client)
    video = client.post(BASE+f"/{PAGE}/video_reels",json={"access_token":token,"upload_phase":"start"}).json()
    headers={"Authorization":"OAuth "+token,"file_url":"https://never-fetched.invalid/a.mp4"}
    assert client.post(video["upload_url"],headers=headers).status_code == 200
    assert client.post(video["upload_url"],headers=headers).status_code == 400
    assert client.post(BASE+f"/{PAGE}/video_reels",json={"access_token":token,"upload_phase":"finish","video_id":video["video_id"]}).status_code == 200
    assert client.post(video["upload_url"],headers=headers).status_code == 400


def test_carousel_rejects_duplicate_nested_and_non_string_children(client):
    fixture(client)
    token = page_token(client)
    children=[client.post(BASE+f"/{IG}/media",json={"access_token":token,"image_url":"https://never-fetched.invalid/a.jpg"}).json()["id"] for _ in range(2)]
    parent=client.post(BASE+f"/{IG}/media",json={"access_token":token,"media_type":"CAROUSEL","children":children}).json()["id"]
    for invalid in [[children[0],children[0]],[parent,children[0]],[{},children[0]]]:
        result=client.post(BASE+f"/{IG}/media",json={"access_token":token,"media_type":"CAROUSEL","children":invalid})
        assert result.status_code == 400


@pytest.mark.parametrize("user_id",["not-numeric",PAGE,IG])
def test_fixture_rejects_invalid_or_overlapping_user_id(client,user_id):
    result=client.post("/facebook/fixtures",json={"app":APP,"app_id":ID,"app_secret":SECRET,"redirect_uris":[CALLBACK],"user_id":user_id})
    assert result.status_code in {400,422}


def test_unknown_timeline_app_never_reads_or_deletes_another_apps_messages(client):
    fixture(client)
    token = page_token(client)
    assert client.post(BASE+f"/{PAGE}/feed",json={"access_token":token,"message":"Keep this capture"}).status_code == 200
    assert client.get("/api/messages",params={"app":"missing-app"}).status_code == 404
    assert client.delete("/api/messages",params={"app":"missing-app"}).status_code == 404
    assert len(client.get("/api/messages",params={"app":APP}).json()) == 1
