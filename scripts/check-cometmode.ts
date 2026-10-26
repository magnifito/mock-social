/** Exercise Cometmode's real publisher against a running local emulator.
 * Run from Cometmode with Bun; see docs/meta-emulator.md.
 */
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
const root = process.argv[2];
if (!root) throw new Error("Usage: check-cometmode.ts <Cometmode checkout>");
const origin = process.env.META_MOCK_ORIGIN ?? "http://127.0.0.1:3205";
const mock = new URL(origin);
assert(["localhost", "127.0.0.1", "[::1]"].includes(mock.hostname));
process.env.META_MOCK_ORIGIN = mock.origin;
process.env.NODE_ENV = "test";
const originalFetch = globalThis.fetch;
const fetchLocal: typeof fetch = (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input));
  assert.equal(url.origin, mock.origin, "Test must never contact a real provider or media URL");
  return originalFetch(input, init);
};
globalThis.fetch = fetchLocal;
const api = async (path: string, body?: object) => {
  const r = await fetch(origin + path, body ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)} : undefined);
  assert(r.ok, await r.clone().text()); return r.json();
};
const app = "cometmode-contract-" + crypto.randomUUID();
const appId = String(Date.now());
const callback = "http://localhost:3200/api/social/auth/callback/facebook";
await api("/facebook/fixtures",{app,app_id:appId,app_secret:"syntheticContract123456",redirect_uris:[callback]});
const authorize = new URL(origin+"/facebook/v19.0/dialog/oauth");
authorize.search = new URLSearchParams({client_id:appId,redirect_uri:callback,state:"test-state",scope:"pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish"}).toString();
const login = await fetch(authorize,{redirect:"manual"});
assert.equal(login.status,302);
const result = new URL(login.headers.get("location")!);
assert.equal(result.searchParams.get("state"),"test-state");
const grant = await api("/facebook/v19.0/oauth/access_token",{client_id:appId,client_secret:"syntheticContract123456",redirect_uri:callback,code:result.searchParams.get("code")});
const pages = await fetch(origin+"/facebook/v19.0/me/accounts",{headers:{Authorization:"Bearer "+grant.access_token}}).then(r=>r.json());
const page = pages.data[0];
const {publishPost} = await import(pathToFileURL(resolve(root,"libs/domain/marketing/src/social/publish.ts")).href);
let count=0;
for (const platform of ["FACEBOOK","INSTAGRAM"]) {
  for (const [postType,media] of [
    ["FEED",platform==="FACEBOOK"?[]:["https://never-fetched.invalid/image.jpg"]],
    ["FEED",["https://never-fetched.invalid/photo.jpg"]],
    ["CAROUSEL",["https://never-fetched.invalid/a.jpg","https://never-fetched.invalid/b.jpg"]],
    ["STORY",["https://never-fetched.invalid/story.jpg"]],
    ["REEL",[{url:"https://never-fetched.invalid/reel.mp4",type:"video"}]],
  ] as const) {
    const id="account-"+platform;
    const result=await publishPost({content:"Contract "+platform+" "+postType,mediaUrls:media},
      [{id:"target-"+count,accountId:id,postType,content:null,mediaUrls:null,firstComment:"Local first comment",thumbnailUrl:null}],
      [{id,platform,accountId:platform==="FACEBOOK"?page.id:page.instagram_business_account.id,accountName:"Mock "+platform,accessToken:page.access_token,isActive:true}]);
    assert.equal(result.finalStatus,"PUBLISHED",JSON.stringify(result)); count++;
  }
}
const captured=await api("/api/messages?limit=100&app="+encodeURIComponent(app));
assert.equal(captured.length,20); // Each of 10 posts plus its first comment.
console.log(JSON.stringify({published:count,captured:captured.length,realNetworkRequests:0,app}));
