"""Install fake Meta fixtures into a running local mock-social server."""
import argparse
from urllib.parse import urlsplit
import httpx

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--base-url", default="http://127.0.0.1:3205")
parser.add_argument("--callback-origin", default="http://localhost:3200")
parser.add_argument("--app", default="cometmode-local")
parser.add_argument("--messaging", action="store_true", help="Mint a fake Page token for Messenger/Instagram channel setup")
args = parser.parse_args()
for origin in (args.base_url, args.callback_origin):
    parsed = urlsplit(origin)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.scheme not in {"http", "https"} or parsed.username:
        parser.error("Setup uses loopback HTTP(S) origins only")
payload = {"app": args.app, "app_id": "12345678", "app_secret": "mockSocialSecret1234567890",
           "redirect_uris": [args.callback_origin.rstrip("/") + "/api/social/auth/callback/" + platform
                             for platform in ("facebook", "instagram")]}
response = httpx.post(args.base_url.rstrip("/") + "/facebook/fixtures", json=payload, timeout=10)
response.raise_for_status()
print(f"Fake Meta app ready: {args.app}")
print("App ID: " + payload["app_id"])
print("App secret: " + payload["app_secret"])
print("These are synthetic credentials for local testing only.")
print("Timeline: " + args.base_url.rstrip("/") + "/?app=" + args.app)

if args.messaging:
    from urllib.parse import parse_qs
    base = args.base_url.rstrip("/") + "/facebook/v23.0"
    callback = payload["redirect_uris"][0]
    auth = httpx.get(base + "/dialog/oauth", params={"client_id": payload["app_id"], "redirect_uri": callback,
                     "scope": "pages_show_list,pages_read_engagement,pages_manage_posts,pages_manage_metadata,pages_messaging,instagram_basic,instagram_content_publish,instagram_manage_messages"}, follow_redirects=False)
    if auth.status_code != 302:
        auth.raise_for_status()
        raise RuntimeError("Mock OAuth did not return a redirect")
    code = parse_qs(urlsplit(auth.headers["location"]).query)["code"][0]
    grant = httpx.post(base + "/oauth/access_token", json={"client_id":payload["app_id"],"client_secret":payload["app_secret"],"redirect_uri":callback,"code":code})
    grant.raise_for_status()
    pages = httpx.get(base + "/me/accounts", headers={"Authorization":"Bearer " + grant.json()["access_token"]})
    pages.raise_for_status()
    print("Fake Page access token: " + pages.json()["data"][0]["access_token"])
    print("Webhook verify token: mockVerifyToken123456")
    print("Mock visitors: Messenger 400000000001; Instagram 500000000001")
