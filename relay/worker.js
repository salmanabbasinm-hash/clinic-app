// Clinic relay v2 - Cloudflare Worker + Durable Object (SQLite). Stores only ENCRYPTED messages.
// Each clinic (box) gets its own Durable Object. The PC keeps one idle WebSocket (no polling); the phone waits briefly for the answer.
const H = {"Access-Control-Allow-Origin":"*","Access-Control-Allow-Headers":"Content-Type","Access-Control-Allow-Methods":"GET,POST,OPTIONS"};
const J = (o, s=200) => new Response(JSON.stringify(o), {status:s, headers:{...H,"Content-Type":"application/json"}});

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, {status:204, headers:H});
    const p = new URL(req.url).pathname.split("/").filter(Boolean);
    if (p[0] === "ping") return J({ok:true, name:"Clinic Relay", v:2});
    if (!/^[a-f0-9]{64}$/.test(p[1]||"")) return J({error:"bad box"}, 400);
    const stub = env.BOX.get(env.BOX.idFromName(p[1]));
    return stub.fetch(req);
  }
};

export class Box {
  constructor(state) {
    this.ctx = state; this.st = state.storage; this.owait = {};
    try { state.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong")); } catch (e) {}
  }
  async putBig(k, t) { const CH = 900000, n = Math.max(1, Math.ceil(t.length / CH)); await this.delBig(k); for (let i = 0; i < n; i++) await this.st.put(k + "#" + i, t.slice(i * CH, (i + 1) * CH)); await this.st.put(k, n); }
  async getBig(k) { const n = await this.st.get(k); if (!n) return null; let s = ""; for (let i = 0; i < n; i++) { const c = await this.st.get(k + "#" + i); if (c == null) return null; s += c; } return s; }
  async delBig(k) { const n = await this.st.get(k); if (n) { for (let i = 0; i < n; i++) await this.st.delete(k + "#" + i); await this.st.delete(k); } }
  webSocketMessage() {}
  webSocketClose(ws) { try { ws.close(); } catch (e) {} }
  async fetch(req) {
    const p = new URL(req.url).pathname.split("/").filter(Boolean), dev = p[2];
    if (p[0] === "ws") {                                     // PC keeps one idle (hibernating) socket; we poke it when a phone posts
      if (req.headers.get("Upgrade") !== "websocket") return J({error:"ws only"}, 426);
      const pr = new WebSocketPair(); this.ctx.acceptWebSocket(pr[1]);
      return new Response(null, {status:101, webSocket:pr[0]});
    }
    if (p[0] === "bk") {                                     // encrypted full backup, stored in chunks; last 2 versions kept
      if (p[2] === "put" && req.method === "POST") {
        const u = new URL(req.url).searchParams, id = u.get("id") || "", n = +u.get("n"), tot = +u.get("total"), ts = +u.get("ts") || Date.now();
        if (!/^[a-z0-9]{4,24}$/.test(id) || !(n >= 0) || !(tot >= 1 && tot <= 60) || n >= tot) return J({error:"bad"}, 400);
        if (u.get("create") === "1" && n === 0 && await this.st.get("bkmeta")) return J({error:"exists"}, 409);   // password already used by someone else
        const t = await req.text(); if (t.length > 1500000) return J({error:"big"}, 413);
        await this.st.put("bk:" + id + ":" + n, t);
        if (n === tot - 1) {
          const meta = await this.st.get("bkmeta"), prev = await this.st.get("bkprev");
          if (prev) for (let i = 0; i < prev.total; i++) await this.st.delete("bk:" + prev.id + ":" + i);
          if (meta) await this.st.put("bkprev", meta);
          await this.st.put("bkmeta", {id, total: tot, ts});
        }
        return J({ok:true});
      }
      if (p[2] === "meta") { const m = await this.st.get("bkmeta"); return m ? J(m) : J({none:true}, 404); }
      if (p[2] === "get") { const v = await this.st.get("bk:" + p[3] + ":" + p[4]); return v == null ? J({none:true}, 404) : new Response(v, {headers:{...H,"Content-Type":"text/plain"}}); }
      return J({error:"?"}, 404);
    }
    if ((p[0] === "q" || p[0] === "r" || p[0] === "o") && !/^[a-z0-9]{6,32}$/.test(dev||"")) return J({error:"bad dev"}, 400);
    if (p[0] === "q" && req.method === "POST") {            // phone -> PC
      const t = await req.text(); if (t.length > 20e6) return J({error:"big"}, 413);
      await this.delBig("out:"+dev); await this.putBig("in:"+dev, t);
      for (const s of this.ctx.getWebSockets()) { try { s.send("q"); } catch (e) {} }
      return J({ok:true});
    }
    if (p[0] === "poll") return J({items: await this.items()});   // PC polls (no waiting: keeps the free quota small)
    if (p[0] === "r" && req.method === "POST") {             // PC -> phone
      const t = await req.text(); if (t.length > 20e6) return J({error:"big"}, 413);
      await this.putBig("out:"+dev, t); await this.delBig("in:"+dev);
      const w = this.owait[dev] || []; delete this.owait[dev]; w.forEach(f => f());
      return J({ok:true});
    }
    if (p[0] === "o") {                                      // phone: waits up to 15s for the answer
      let v = await this.getBig("out:"+dev);
      if (!v) { await new Promise(r => { const f = () => { clearTimeout(tm); r(); }; const tm = setTimeout(r, 15000); (this.owait[dev] = this.owait[dev] || []).push(f); }); v = await this.getBig("out:"+dev); }
      return v ? new Response(v, {headers:{...H,"Content-Type":"application/json"}}) : J({none:true}, 404);
    }
    return J({error:"?"}, 404);
  }
  async items() {
    const m = await this.st.list({prefix:"in:"}), out = [];
    for (const [k, n] of m) { if (k.includes("#")) continue; const v = await this.getBig(k); if (v) out.push({dev:k.slice(3), msg:v}); }
    return out;
  }
}
