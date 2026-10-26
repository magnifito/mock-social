# Local Meta emulator

Requires SQLite 3.35 or newer (atomic claims use `RETURNING` and materialized CTEs).

This fork adds a stateful HTTP simulation for the Meta calls used by Cometmode.
It never forwards Graph requests, fetches media URLs, or contacts Meta. Use fake
credentials only. The local panel and fixture API have no authentication.

## Start and connect

From this repository:

```sh
uv sync --group dev
MOCKPOST_HTTP_ONLY=1 MOCKPOST_HTTP_PORT=3205 MOCKPOST_URL=http://127.0.0.1:3205 uv run mockpost
```

The HTTP-only flag skips the SMTP, FTP and SFTP listeners.

In another terminal:

```sh
uv run python scripts/setup-meta.py
```

The script registers App ID `12345678`, App secret
`mockSocialSecret1234567890`, one Facebook Page (`100000000001`) and its linked
Instagram account (`200000000001`). It registers the two callback URLs under
`http://localhost:3200`. Use `--callback-origin` if the local app uses another origin.

In Cometmode's **web and API runtime environments**, set:

```dotenv
META_MOCK_ORIGIN=http://127.0.0.1:3205
```

Set it in the worker/scheduler too if those processes execute social publishing.
Restart the affected processes. Cometmode rejects this setting in production and
accepts only loopback origins. Remove it and restart to use real Meta again.
This requires Cometmode's `getMetaEndpoints` integration; a checkout with hardcoded
Meta URLs does not use the mock simply because this server is running.

Open Supervisor → Settings. Save the fake App ID and secret as the shared Meta
app, with an audit reason. Alternatively save them as the workspace-owned app in
Connections. Use a **dedicated test workspace**: the callback writes real local
Cometmode account rows and simulated publish calls update local post status.
Do not overwrite an existing real shared app just to test the simulator.

Open Connections → Facebook or Instagram, select the configured app source and
connect. The emulator returns immediately through the real signed-state callback.
There is no Facebook password form. Create a post in Social Planner and publish.
Open `http://127.0.0.1:3205/?app=cometmode-local` to inspect captured sends.
`/docs` provides interactive API controls; `/api/config/facebook` lists endpoints.

## HTTP contract coverage

Graph base: `http://127.0.0.1:3205/facebook/v19.0`. The version path is accepted
for client routing; it does not certify version-specific Meta behavior.

- `GET /dialog/oauth`: registered client/callback, exact state round trip, cancellation.
- `GET|POST /oauth/access_token`: one-use code exchange, app secret, callback match,
  code expiry, long-lived user token exchange.
- `GET /me`, `/me/accounts`, `/{page-id}`, `/{instagram-id}` and `/me/permissions`:
  fixture identities, Page tokens, linked Instagram account and granted permissions.
- `POST /{page-id}/feed`, `/photos`, `/photo_stories`, `/video_reels`:
  feed, photo, carousel feed, image story and hosted reel upload control flow.
- `POST /{instagram-id}/media`, `GET /{container-id}`,
  `POST /{instagram-id}/media_publish`: image/video/story/carousel containers,
  processing/error states and one-publish claims.
- `POST /{post-id}/comments`: first comments.
- `GET /{page-id}/feed`, `/posts`, `/{instagram-id}/media`: inspect stored posts.
- `POST /{page-or-instagram-id}/messages`: basic text/attachment message payloads.
- `GET|POST|DELETE /{page-id}/subscribed_apps`: subscription state.

Bearer headers and `access_token` parameters work. Fake tokens expire and scope
checks reject missing permissions. Page tokens cannot access another Page.
Apps cannot access each other's tokens or objects. Unknown endpoints return a
Graph-shaped error instead of pretending to succeed. Captured sends omit tokens.
Messages and published posts use the existing app-filtered timeline and MCP tools.

## Failure and recovery controls

Change a scenario without restarting:

```sh
curl -X PUT http://127.0.0.1:3205/facebook/fixtures/scenario \
  -H 'X-MockPost-App: cometmode-local' -H 'Content-Type: application/json' \
  -d '{"scenario":"rate_limited"}'
```

Scenarios: `normal`, `cancel_login`, `no_pages`, `token_expired`,
`permission_denied`, `rate_limited`, `media_processing`, `media_error`.
Set `normal` to clear a scenario. It does not recreate revoked tokens.

`PUT /facebook/fixtures/media/{id}` with `{"status_code":"FINISHED"}` completes
an existing processing container. `IN_PROGRESS` and `ERROR` are also accepted.
Published containers cannot be reset to unpublished states.
`POST /facebook/fixtures/revoke` with `{"access_token":"<fake-token>"}` revokes
one token. Both controls require `X-MockPost-App`.
`POST /facebook/fixtures/reset` invalidates only that app's tokens and removes
its Graph objects. It retains captured timeline history. Reconnect Cometmode after reset.

MCP tools: `configure_meta_app`, `set_meta_scenario`, `reset_meta_app`, plus the
existing `list_sent_messages`, `get_timeline_markdown` and `simulate_facebook_event`.
Use HTTP for individual media state and token revocation controls.

## Inbound messaging

Register a webhook through `POST /api/webhooks` with `channel: "facebook"`, the
app name, target URL and a synthetic `token` equal to the app's webhook signing
secret. Call `/facebook/simulate` with `X-MockPost-App` and a Meta-shaped payload.
The emulator sends `X-Hub-Signature-256` over the exact UTF-8 request bytes and
records the receiver's response. Re-send the same message ID to test duplicates.
Use the payload's `object: "instagram"` for Instagram events where the consumer
uses the same Meta webhook transport. Cometmode's channel SDK endpoints use
`META_MOCK_ORIGIN` in the web/API runtime. Messenger and Instagram share this
switch with publishing. WhatsApp keeps its normal provider URLs.

## Limits

This is a controlled contract fixture, not a complete Meta emulator. It does not
validate actual images/video, deliver content to a real device, reproduce Meta's
review or business verification, model messaging-policy windows, or certify every
message template/attachment. Media URLs stay in captured metadata and are never
fetched. Real Meta compatibility remains a separate acceptance check.

## Tests

```sh
uv run pytest -q
```

The Meta tests use real loopback HTTP and temporary SQLite databases. They cover
OAuth/state, code replay, app/Page isolation, publishing, media recovery,
permissions, provider errors, revocation, subscriptions and signed webhook bytes.

## Exercise the real Cometmode publisher

With the mock running, execute from the Cometmode checkout:

```sh
META_MOCK_ORIGIN=http://127.0.0.1:3205 bun --cwd apps/web ../../../mock-social/scripts/check-cometmode.ts ../..
RUN_META_MOCK_E2E=1 bun run e2e e2e/tests/meta-mock.e2e.ts
```

The first command uses the actual publisher for 10 Facebook/Instagram paths and
checks 20 captured events including first comments. It then checks six failed
sends without new captures and six recovery sends, for 26 captures in total. It blocks all non-local
network requests in that process. It retains its uniquely named synthetic app
and captured messages for inspection. The second uses real browser OAuth,
local credential/account persistence and the actual publish API. It refuses to
overwrite a test workspace's existing own-app credentials and cleans its own
Cometmode posts, accounts and credentials afterward. Its mock timeline remains.

The broader inherited suite currently emits FTP worker-shutdown warnings on
macOS. They also occur without the Meta changes. The Meta service can use
`MOCKPOST_HTTP_ONLY=1` to avoid starting unrelated protocol listeners.

## Exercise Cometmode messaging

Run `uv run python scripts/setup-meta.py --messaging` to obtain a synthetic Page
access token, verification token and declared visitor IDs. Enter these fake values
in the existing Messenger or Instagram connection form. Use the linked Page for
Instagram. Never enter a real Page token into this emulator.

Register each callback separately through `POST /api/webhooks` with channel
`facebook`, the fixture app name, callback URL and signing secret as `token`.
Call `POST /facebook/simulate/message` with `X-MockPost-App` and JSON fields
`platform` (`messenger` or `instagram`), `sender_id`, `text`, and `webhook_id`.
Use the returned webhook ID so events reach the correct callback. The MCP tool
`simulate_meta_message` exposes the same operation. The Page must subscribe first.
Declared visitors support profile reads, typing actions and replies. Cross-Page
visitors fail. Outbound messages appear in the existing timeline.

To exercise the actual Cometmode connection services, webhook handlers, database
and Inbox reply service, run from this fork with the mock active:

```sh
uv run python scripts/check-cometmode-messaging.py ../cometmode --scratch-database cometmode_chatbot_cx0_20261022
```

This check accepts only the named local scratch database on port 5544. It creates
human-handoff conversations with the bot disabled, receives two signed messages,
sends two replies, and checks four refusals. It replays six duplicate deliveries,
including concurrent retries. It checks four failed sends and four recovery sends.
It removes its own records afterward.
It blocks non-local HTTP requests. It does not run an AI provider, background
worker, realtime subscriber or browser. The launcher supplies empty Next.js marker
packages in a temporary directory for the standalone Bun process.

## Verified local boundaries

The contract tests reject unknown message recipients, visitors from another Page,
Messenger visitors addressed through an Instagram account, invalid or overlapping
fixture user IDs, duplicate or nested carousel children, and repeated reel uploads.
Declare every messaging visitor in `people` with an `id`, `page_id` and `platform`.
Omitting `people` generates visitors for each configured Page. The
`configure_meta_app` MCP tool accepts custom visitors too.

| Path | Proof |
| --- | --- |
| OAuth, token isolation, publishing and media state | `tests/test_facebook_graph.py` |
| Publisher success, permission/rate/token errors and recovery | `scripts/check-cometmode.ts` |
| Signed inbound text, Inbox reply, duplicates and recovery | `scripts/check-cometmode-messaging.py` |
| Browser OAuth, account persistence and publish API | Cometmode `e2e/tests/meta-mock.e2e.ts` |
| Setup forms and localized guides | Cometmode `e2e/tests/messaging-connections.e2e.ts` and `channel-setup-guides.e2e.ts` |

The messaging proof deliberately exercises human replies with bots disabled.
It does not certify automated flows, AI calls, realtime events, arbitrary media
payloads, Meta policy windows, or live delivery. These are outside this fixture's
verified scope. Use the existing raw-event endpoint for additional contract cases.

Named timeline app filters fail closed: `GET` or `DELETE /api/messages?app=<name>`
returns 404 when that name is unknown. Omitting the filter still selects the
global timeline. Cleanup clients must resolve their owned app before clearing
its messages and must not replace a missing app with an unfiltered delete.
