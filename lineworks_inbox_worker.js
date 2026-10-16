// LINE WORKS の返信を受け取る口（Cloudflare Workers）
//
// LINE WORKS の Bot は「あとから発言を読みに行く」ことができず、担当者の返信は
// Developer Console に登録した Callback URL へ LINE WORKS 側から送られてくる。
// この Worker がそれを受け取って KV に貯め、main.py が1時間ごとに読みに来る。
//
// 設定（Cloudflare のダッシュボード）
//   KV namespace をこの Worker に INBOX という名前でつなぐ
//   Secret: BOT_SECRET   … Developer Console > Bot の Bot Secret（改ざん検証に使う）
//   Secret: INBOX_TOKEN  … main.py が読みに来るときの合言葉。環境変数 LINEWORKS_INBOX_TOKEN と同じ値
//
// Developer Console の Bot の Callback URL には  https://<この Worker>/callback  を入れる。
// main.py の config.json の lineworks.inbox_url には  https://<この Worker>  を入れる。

const KEEP = 200; // トークルームごとに残す件数

export default {
  async fetch(req, env) {
    const url = new URL(req.url);

    // LINE WORKS からの通知
    if (req.method === "POST" && url.pathname === "/callback") {
      const raw = await req.text();
      if (!(await verify(raw, req.headers.get("x-works-signature"), env.BOT_SECRET))) {
        return new Response("bad signature", { status: 401 });
      }
      const ev = JSON.parse(raw);
      if (ev.type === "message" && ev.content?.type === "text" && ev.source?.channelId) {
        const key = "ch:" + ev.source.channelId;
        const list = JSON.parse((await env.INBOX.get(key)) || "[]");
        list.push({
          room_id: ev.source.channelId,
          message_id: "in" + Date.parse(ev.issuedTime) + "-" + list.length,
          account_id: ev.source.userId,
          send_time: Math.floor(Date.parse(ev.issuedTime) / 1000),
          body: ev.content.text,
        });
        await env.INBOX.put(key, JSON.stringify(list.slice(-KEEP)));
      }
      // 処理の結果にかかわらず 200 を返す（LINE WORKS の仕様）
      return new Response("ok");
    }

    // main.py からの読み出し
    if (req.method === "GET" && url.pathname === "/messages") {
      if (req.headers.get("authorization") !== "Bearer " + env.INBOX_TOKEN) {
        return new Response("unauthorized", { status: 401 });
      }
      const ch = url.searchParams.get("channel") || "";
      const body = (await env.INBOX.get("ch:" + ch)) || "[]";
      return new Response(body, { headers: { "content-type": "application/json; charset=utf-8" } });
    }

    return new Response("not found", { status: 404 });
  },
};

// X-WORKS-Signature = Base64( HMAC-SHA256( Bot Secret, リクエストボディ ) )
async function verify(raw, sig, secret) {
  if (!sig || !secret) return false;
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(raw));
  const calc = btoa(String.fromCharCode(...new Uint8Array(mac)));
  if (calc.length !== sig.length) return false;
  let diff = 0;
  for (let i = 0; i < calc.length; i++) diff |= calc.charCodeAt(i) ^ sig.charCodeAt(i);
  return diff === 0;
}
