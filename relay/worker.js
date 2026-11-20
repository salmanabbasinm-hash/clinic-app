// Clinic relay v2 - Cloudflare Worker + Durable Object (SQLite). Stores only ENCRYPTED messages.
// Each clinic (box) gets its own Durable Object. The PC polls every 5-30 s; the phone waits briefly for the answer.
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
  constructor(state) { this.st = state.storage; this.owait = {}; }
  async fetch(req) {
    const p = new URL(req.url).pathname.split("/").filter(Boolean), dev = p[2];
    if ((p[0] === "q" || p[0] === "r" || p[0] === "o") && !/^[a-z0-9]{6,32}$/.test(dev||"")) return J({error:"bad dev"}, 400);
    if (p[0] === "q" && req.method === "POST") {            // phone -> PC
      const t = await req.text(); if (t.length > 20e6) return J({error:"big"}, 413);
      await this.st.put("in:"+dev, t);
      return J({ok:true});
    }
    if (p[0] === "poll") return J({items: await this.items()});   // PC polls (no waiting: keeps the free quota small)
    if (p[0] === "r" && req.method === "POST") {             // PC -> phone
      const t = await req.text(); if (t.length > 20e6) return J({error:"big"}, 413);
      await this.st.put("out:"+dev, t); await this.st.delete("in:"+dev);
      const w = this.owait[dev] || []; delete this.owait[dev]; w.forEach(f => f());
      return J({ok:true});
    }
    if (p[0] === "o") {                                      // phone: waits up to 15s for the answer
      let v = await this.st.get("out:"+dev);
      if (!v) { await new Promise(r => { const f = () => { clearTimeout(tm); r(); }; const tm = setTimeout(r, 15000); (this.owait[dev] = this.owait[dev] || []).push(f); }); v = await this.st.get("out:"+dev); }
      return v ? new Response(v, {headers:{...H,"Content-Type":"application/json"}}) : J({none:true}, 404);
    }
    return J({error:"?"}, 404);
  }
  async items() {
    const m = await this.st.list({prefix:"in:"}), out = [];
    for (const [k, v] of m) out.push({dev:k.slice(3), msg:v});
    return out;
  }
}
