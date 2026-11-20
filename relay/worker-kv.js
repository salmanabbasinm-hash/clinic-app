// Clinic relay (Cloudflare Worker + KV). Stores only ENCRYPTED messages; it cannot read patient data.
// Setup: Workers & Pages -> Create Worker -> paste this -> Settings -> Bindings -> KV namespace, variable name: BOX
const H = {"Access-Control-Allow-Origin":"*","Access-Control-Allow-Headers":"Content-Type","Access-Control-Allow-Methods":"GET,POST,OPTIONS"};
const J = (o, s=200) => new Response(JSON.stringify(o), {status:s, headers:{...H,"Content-Type":"application/json"}});
export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, {status:204, headers:H});
    const u = new URL(req.url), p = u.pathname.split("/").filter(Boolean);
    const box = p[1], dev = p[2];
    if (p[0] === "ping") return J({ok:true, name:"Clinic Relay"});
    if (!/^[a-f0-9]{64}$/.test(box||"")) return J({error:"bad box"}, 400);
    const TTL = {expirationTtl: 86400};
    if (p[0] === "q" && req.method === "POST") {      // phone -> PC request
      if (!/^[a-z0-9]{6,32}$/.test(dev||"")) return J({error:"bad dev"}, 400);
      const t = await req.text(); if (t.length > 20e6) return J({error:"big"}, 413);
      await env.BOX.put("in:"+box+":"+dev, t, TTL);
      let d = []; try { d = JSON.parse(await env.BOX.get("dev:"+box) || "[]"); } catch(e) {}
      if (d.indexOf(dev) < 0) { d.push(dev); await env.BOX.put("dev:"+box, JSON.stringify(d.slice(-20)), {expirationTtl: 2592000}); }
      return J({ok:true});
    }
    if (p[0] === "poll") {                            // PC asks for waiting requests
      let d = []; try { d = JSON.parse(await env.BOX.get("dev:"+box) || "[]"); } catch(e) {}
      const items = [];
      for (const x of d) { const v = await env.BOX.get("in:"+box+":"+x); if (v) items.push({dev:x, msg:v}); }
      return J({items});
    }
    if (p[0] === "r" && req.method === "POST") {      // PC -> phone response
      const t = await req.text(); if (t.length > 20e6) return J({error:"big"}, 413);
      await env.BOX.put("out:"+box+":"+dev, t, TTL);
      await env.BOX.delete("in:"+box+":"+dev);
      return J({ok:true});
    }
    if (p[0] === "o") {                               // phone fetches response
      const v = await env.BOX.get("out:"+box+":"+dev);
      return v ? new Response(v, {headers:{...H,"Content-Type":"application/json"}}) : J({none:true}, 404);
    }
    return J({error:"?"}, 404);
  }
};
