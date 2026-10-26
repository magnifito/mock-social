/** Real channel services + webhook handlers against a local mock and approved scratch DB. */
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
const root = process.argv[2];
if (!root) throw Error("Usage: check-cometmode-messaging.ts <Cometmode checkout>");
const database = new URL(process.env.DATABASE_URL ?? "");
assert.equal(process.env.E2E_META_DATABASE_NAME, "cometmode_chatbot_cx0_20261022", "Explicit approved scratch DB name required");
assert(["localhost", "127.0.0.1"].includes(database.hostname) && database.port === "5544");
assert.notEqual(process.env.NODE_ENV, "production");
database.pathname = "/" + process.env.E2E_META_DATABASE_NAME;
process.env.DATABASE_URL = database.href;
process.env.NODE_ENV = "test";
process.env.META_MOCK_ORIGIN ??= "http://127.0.0.1:3205";
process.env.CENTRIFUGO_API_URL = ""; // Realtime is a separate proof; no unrelated network sends.
const origin = new URL(process.env.META_MOCK_ORIGIN);
assert(["localhost", "127.0.0.1", "[::1]"].includes(origin.hostname));
const realFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input));
  assert(["localhost", "127.0.0.1", "[::1]"].includes(url.hostname), "Non-local HTTP is forbidden in this proof");
  return realFetch(input, init);
};
const app = "meta-messaging-" + crypto.randomUUID();
const prefix = app + "-";
const secret = "syntheticMessagingSecret123456";
const appId = String(Date.now());
const callback = "http://localhost:3200/api/social/auth/callback/facebook";
const workspaceId = "wsp_test_primary";
const importFile = (path: string) => import(pathToFileURL(resolve(root, path)).href);
const { db, ConversationChannel } = await importFile("libs/data-access/db/src/index.ts");
const modules = {
  messenger: await importFile("libs/domain/chatbot/src/channels/messenger.ts"),
  instagram: await importFile("libs/domain/chatbot/src/channels/instagram.ts"),
};
const handlers = {
  messenger: await importFile("apps/web/app/api/webhooks/messenger/[connectionId]/route.ts"),
  instagram: await importFile("apps/web/app/api/webhooks/instagram/[connectionId]/route.ts"),
};
const { messenger, instagram } = await importFile("libs/data-access/channels/src/index.ts");
const { InboxConversationsService } = await importFile("apps/api/src/conversations/inbox.service.ts");
const request = async (path: string, data?: object, method = data ? "POST" : "GET") => {
  const response = await fetch(origin.origin + path, {method, headers:{"Content-Type":"application/json","X-MockPost-App":app}, ...(data ? {body:JSON.stringify(data)} : {})});
  assert(response.ok, await response.clone().text()); return response.json();
};
const server = Bun.serve({hostname:"127.0.0.1",port:0,async fetch(request) {
  const [,provider,id] = new URL(request.url).pathname.split("/");
  if (provider !== "messenger" && provider !== "instagram") return new Response("Not found",{status:404});
  return handlers[provider].POST(request,{params:Promise.resolve({connectionId:id})});
}});
try {
  await request("/facebook/fixtures",{app,app_id:appId,app_secret:secret,redirect_uris:[callback]});
  const auth = new URL(origin.origin+"/facebook/v23.0/dialog/oauth");
  auth.search = new URLSearchParams({client_id:appId,redirect_uri:callback,scope:"pages_show_list,pages_read_engagement,pages_manage_metadata,pages_messaging,instagram_basic,instagram_manage_messages"}).toString();
  const start = await fetch(auth,{redirect:"manual"});assert.equal(start.status,302);
  const code = new URL(start.headers.get("location")!).searchParams.get("code");
  const grant = await request("/facebook/v23.0/oauth/access_token",{client_id:appId,client_secret:secret,redirect_uri:callback,code});
  const pages = await fetch(origin.origin+"/facebook/v23.0/me/accounts",{headers:{Authorization:"Bearer "+grant.access_token}}).then(r=>r.json());
  const token = pages.data[0].access_token;
  let received=0, replies=0, denied=0, duplicates=0, failedSends=0, recovered=0;
  for (const provider of ["messenger","instagram"] as const) {
    const mod=modules[provider];
    const connect=provider==="messenger"?mod.connectMessengerPage:mod.connectInstagramAccount;
    const connection=await connect({workspaceId,pageAccessToken:token,appSecret:secret,appId,verifyToken:"mockVerifyToken",name:prefix+provider});
    const id=prefix+provider;
    const senderId=provider==="messenger"?"400000000001":"500000000001";
    const ctx=provider==="messenger"?mod.buildMessengerContext(connection):mod.buildInstagramContext(connection);
    const sdk=provider==="messenger"?messenger:instagram;
    const profile=await sdk.integration.runChannelHandler("contact","getProfile",{ctx,data:{sourceId:senderId}});
    assert(profile.firstName?.startsWith("Mock"));
    // Keep these owned test conversations in human handoff. Never start a real bot/AI or queue work.
    await db.contacts.create({data:{id,workspaceId,firstName:"Mock",lastName:provider,source:provider}});
    await db.contactChannels.create({data:{id,workspaceId,contactId:id,originalContactId:id,channelConnectionId:connection.id,source:provider,sourceId:senderId,lastIncomingMessageAt:new Date()}});
    await db.inboxConversations.create({data:{id,workspaceId,channel:provider==="messenger"?ConversationChannel.MESSENGER:ConversationChannel.INSTAGRAM,contactId:id,contactChannelId:id,status:"open",botEnabled:false,botResumeAt:null}});
    const target=`http://127.0.0.1:${server.port}/${provider}/${connection.id}`;
    const webhook=await request("/api/webhooks",{app,channel:"facebook",target_url:target,token:secret});
    const inbound="Local "+provider+" visitor "+app;
    const delivery=await request("/facebook/simulate/message",{platform:provider,sender_id:senderId,text:inbound,webhook_id:webhook.id,message_id:`mid.shared.${app}`});
    assert.equal(delivery.delivered.response_code,200,delivery.delivered.response_body);
    const stored=await db.inboxMessages.findMany({where:{conversationId:id}});
    assert.equal(stored.length,1);assert.equal(stored[0].content,inbound);received++;
    // Provider retries must not create a second Inbox message.
    const duplicate=await request("/facebook/simulate/message",{platform:provider,sender_id:senderId,text:inbound,webhook_id:webhook.id,message_id:delivery.message_id});
    assert.equal(duplicate.delivered.response_code,200);
    assert.equal(await db.inboxMessages.count({where:{conversationId:id}}),1);
    // Two retries can arrive together. The database must still keep one message.
    const concurrent=await Promise.all([0,1].map(()=>request("/facebook/simulate/message",{platform:provider,sender_id:senderId,text:inbound,webhook_id:webhook.id,message_id:delivery.message_id})));
    assert(concurrent.every(item=>item.delivered.response_code===200));
    assert.equal(await db.inboxMessages.count({where:{conversationId:id}}),1);
    duplicates+=3;
    // A bad signature must never append a message.
    const bad=await fetch(target,{method:"POST",headers:{"Content-Type":"application/json","X-Hub-Signature-256":"sha256="+"0".repeat(64)},body:JSON.stringify({object:provider==="messenger"?"page":"instagram",entry:[]})});
    assert.equal(bad.status,403);denied++;
    const reply="Local "+provider+" reply "+app;
    const response=await new InboxConversationsService().reply(workspaceId,"Test Workspace",id,{content:reply});
    assert.equal(response.deliveryError,undefined,JSON.stringify(response));
    assert.equal(response.message.deliveryStatus,"SENT");
    const outbox=await request("/api/messages?app="+app);
    assert(outbox.some((message: {direction:string;raw_payload:string})=>message.direction==="outbound"&&JSON.parse(message.raw_payload).message?.text===reply));replies++;
    for (const scenario of ["permission_denied", "rate_limited"]) {
      await request("/facebook/fixtures/scenario",{scenario},"PUT");
      const content=`Failed ${scenario} ${provider} ${app}`;
      try {
        const failed=await new InboxConversationsService().reply(workspaceId,"Test Workspace",id,{content});
        assert.equal(failed.message.deliveryStatus,"UNDELIVERABLE");
        assert.equal(typeof failed.deliveryError,"string");
        const persisted=await db.inboxMessages.findUniqueOrThrow({where:{id:failed.message.id}});
        assert.equal(persisted.deliveryStatus,"UNDELIVERABLE");
        assert(persisted.deliveryError);
        const captures=await request("/api/messages?app="+app);
        assert(!captures.some((message: {direction:string;raw_payload:string})=>message.direction==="outbound"&&JSON.parse(message.raw_payload).message?.text===content));
        failedSends++;
      } finally {
        await request("/facebook/fixtures/scenario",{scenario:"normal"},"PUT");
      }
      const recovery=await new InboxConversationsService().reply(workspaceId,"Test Workspace",id,{content:`Recovered ${scenario} ${provider} ${app}`});
      assert.equal(recovery.deliveryError,undefined);
      assert.equal(recovery.message.deliveryStatus,"SENT");
      recovered++;
    }
    const disconnect=provider==="messenger"?mod.disconnectMessengerConnection:mod.disconnectInstagramConnection;
    await disconnect({connectionId:connection.id,workspaceId});
    assert.equal((await db.channelConnections.findUniqueOrThrow({where:{id:connection.id}})).active,false);
    const refused=await fetch(target,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    assert.equal(refused.status,403);denied++;
  }
  console.log(JSON.stringify({app,scratch:database.pathname,received,replies,refused:denied,duplicates,failedSends,recovered}));
} finally {
  server.stop(true);
  await db.inboxConversations.deleteMany({where:{workspaceId,id:{startsWith:prefix}}});
  await db.contactChannels.deleteMany({where:{workspaceId,id:{startsWith:prefix}}});
  await db.contacts.deleteMany({where:{workspaceId,id:{startsWith:prefix}}});
  await db.channelConnections.deleteMany({where:{workspaceId,name:{startsWith:prefix}}});
  const remaining = {
    contacts: await db.contacts.count({where:{workspaceId,id:{startsWith:prefix}}}),
    identities: await db.contactChannels.count({where:{workspaceId,id:{startsWith:prefix}}}),
    conversations: await db.inboxConversations.count({where:{workspaceId,id:{startsWith:prefix}}}),
    messages: await db.inboxMessages.count({where:{conversationId:{startsWith:prefix}}}),
    connections: await db.channelConnections.count({where:{workspaceId,name:{startsWith:prefix}}}),
  };
  assert(Object.values(remaining).every(count => count === 0));
  console.log(JSON.stringify({cleanup:remaining}));
  await db.$disconnect();
}
process.exit(0);
