const C='clinic-site-v4';
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(['./','icon-192.png','manifest.webmanifest']).catch(()=>{})));self.skipWaiting()});
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{
 const r=e.request;if(r.method!=='GET')return;const u=new URL(r.url);
 if(u.origin!==location.origin&&!/fonts\.(googleapis|gstatic)\.com$/.test(u.hostname))return;
 if(/version\.json$/.test(u.pathname)||u.search.indexOf('nocache')>=0)return;
 e.respondWith(caches.open(C).then(c=>c.match(r,{ignoreSearch:true}).then(m=>{
  const f=fetch(r).then(x=>{if(x&&x.ok)c.put(r,x.clone());return x}).catch(()=>null);
  if(m){e.waitUntil(f);return m}
  return f.then(x=>x||c.match('./')||new Response('offline',{status:503}))
 })))});
