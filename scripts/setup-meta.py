"""Install fake Meta fixtures into a running local mock-social server."""
import argparse
from urllib.parse import urlsplit
import httpx

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--base-url", default="http://127.0.0.1:3205")
parser.add_argument("--callback-origin", default="http://localhost:3200")
parser.add_argument("--app", default="cometmode-local")
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
