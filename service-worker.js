const VERSION="price-search-v31";
const SHELL_CACHE=`${VERSION}-shell`;
const APP_SHELL=["/","/index.html","/manifest.webmanifest","/app-icon.svg","/offline.html"];

self.addEventListener("install",event=>{
  event.waitUntil(caches.open(SHELL_CACHE).then(cache=>cache.addAll(APP_SHELL)).then(()=>self.skipWaiting()));
});

self.addEventListener("activate",event=>{
  event.waitUntil(
    caches.keys().then(keys=>Promise.all(keys.filter(key=>!key.startsWith(VERSION)).map(key=>caches.delete(key))))
      .then(()=>self.clients.claim())
  );
});

async function cachedWithFlag(response){
  const body=await response.clone().blob();
  const headers=new Headers(response.headers);
  headers.set("X-Offline-Cache","1");
  return new Response(body,{status:response.status,statusText:response.statusText,headers});
}

async function networkFirst(request,cacheName,fallbackUrl=""){
  const cache=await caches.open(cacheName);
  try{
    const response=await fetch(request);
    if(response&&response.ok)await cache.put(request,response.clone());
    return response;
  }catch(error){
    const cached=await cache.match(request);
    if(cached)return cachedWithFlag(cached);
    if(fallbackUrl){
      const fallback=await caches.match(fallbackUrl);
      if(fallback)return cachedWithFlag(fallback);
    }
    throw error;
  }
}

self.addEventListener("fetch",event=>{
  const request=event.request;
  if(request.method!=="GET")return;
  const url=new URL(request.url);
  if(url.origin!==self.location.origin)return;

  if(request.mode==="navigate"){
    event.respondWith(networkFirst(request,SHELL_CACHE,"/index.html"));
    return;
  }

  if(APP_SHELL.includes(url.pathname)){
    event.respondWith(caches.match(request).then(cached=>cached||fetch(request)));
  }
});
