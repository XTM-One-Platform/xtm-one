/*
 * The service worker of the installed web app (the mobile companion).
 *
 * It caches NOTHING. Every request goes to the network exactly as without it,
 * so a new deployment is never hidden behind a stale copy and no API answer,
 * token or file ever lands in a cache. What it adds:
 *
 * - an offline page when a page navigation fails because the device is offline;
 * - phone notifications (`push`): what the platform pushes (`services/mobile_push.py`),
 *   shown as is; a tap opens the notification's page of THIS app (a path only,
 *   never another origin); an Approve / Reject button sends its one-time token
 *   to `POST /api/v1/mobile/push-actions` and says how it went;
 * - "Share to" (Android): what another app shares arrives as a POST to `share`
 *   (the manifest's `share_target`); text and files are kept in IndexedDB only
 *   until the app's Share page picks them up (`src/lib/shared-inbox.ts`).
 *
 * Registered from `src/lib/pwa.ts` with the platform's base path as its scope.
 */
const OFFLINE_PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Offline</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #0a0f1c; color: #e8ecf6; font-family: system-ui, sans-serif; text-align: center; padding: 24px; }
  h1 { font-size: 20px; margin: 0 0 8px; }
  p { margin: 0 0 20px; color: #9aa4bf; line-height: 1.5; }
  a { display: inline-flex; align-items: center; min-height: 44px; padding: 0 20px; border-radius: 12px; background: #5e1ad5; color: #fff; font: 600 15px system-ui, sans-serif; text-decoration: none; }
</style></head>
<body><main>
  <h1>You are offline</h1>
  <p>Your notifications and your assistant need a connection.<br>They will be here as soon as you are back online.</p>
  <a href="">Try again</a>
</main></body></html>`;

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // Lets the browser start a navigation's request while the worker boots:
      // the worker adds no latency to a page load.
      if (self.registration.navigationPreload) await self.registration.navigationPreload.enable();
      await self.clients.claim();
    })(),
  );
});

/** The app's own root, the base path included (`https://host/xtm-one/`). */
const SCOPE = () => new URL(self.registration.scope);

/** A notification's page: a path of this app only, else the app's root. */
function appUrl(path) {
  const scope = SCOPE();
  if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//") || path.includes("\\")) {
    return scope.href;
  }
  const url = new URL(scope.pathname.replace(/\/$/, "") + path, scope.origin);
  return url.origin === scope.origin ? url.href : scope.href;
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {};
  }
  const title = typeof data.title === "string" && data.title ? data.title.slice(0, 120) : "New notification";
  const actions = Array.isArray(data.actions)
    ? data.actions
        .filter((a) => a && (a.action === "approve" || a.action === "reject") && typeof a.token === "string")
        .slice(0, 2)
    : [];
  event.waitUntil(
    self.registration.showNotification(title, {
      body: typeof data.body === "string" ? data.body.slice(0, 300) : "",
      tag: typeof data.tag === "string" ? data.tag : undefined,
      icon: typeof data.icon === "string" ? appUrl(data.icon) : undefined,
      requireInteraction: data.urgent === true,
      data: { url: appUrl(data.url), actions },
      actions: actions.map((a) => ({ action: a.action, title: String(a.title || a.action).slice(0, 20) })),
    }),
  );
});

async function openApp(url) {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const client of windows) {
    if (new URL(client.url).origin === new URL(url).origin && "focus" in client) {
      await client.focus();
      if ("navigate" in client) return client.navigate(url);
      return client;
    }
  }
  return self.clients.openWindow(url);
}

async function pressAction(notification, action) {
  const scope = SCOPE();
  try {
    const response = await fetch(new URL("api/v1/mobile/push-actions", scope).href, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: action.token }),
      credentials: "omit",
    });
    const done = response.ok;
    await self.registration.showNotification(done ? (action.action === "approve" ? "Approved" : "Rejected") : "Not done", {
      body: done ? notification.title : "This action is no longer available. Open the app to decide.",
      tag: notification.tag,
      data: { url: notification.data && notification.data.url },
    });
  } catch {
    await openApp((notification.data && notification.data.url) || scope.href);
  }
}

self.addEventListener("notificationclick", (event) => {
  const notification = event.notification;
  notification.close();
  const actions = (notification.data && notification.data.actions) || [];
  const pressed = actions.find((a) => a.action === event.action);
  if (pressed) {
    event.waitUntil(pressAction(notification, pressed));
    return;
  }
  event.waitUntil(openApp((notification.data && notification.data.url) || SCOPE().href));
});

// ── Share to the app (Android share sheet) ──────────────────────────────────
// Kept in step with `src/lib/shared-inbox.ts`.
const SHARE_DB = "xtm-one-share";
const SHARE_STORE = "pending";
const SHARE_MAX_FILES = 5;
const SHARE_MAX_BYTES = 20 * 1024 * 1024;

function openShareDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(SHARE_DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(SHARE_STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function stashShare(form) {
  const files = [];
  let total = 0;
  for (const entry of form.getAll("files")) {
    if (!(entry instanceof File) || files.length >= SHARE_MAX_FILES) continue;
    if (total + entry.size > SHARE_MAX_BYTES) continue;
    total += entry.size;
    files.push(entry);
  }
  const text = ["title", "text", "url"]
    .map((key) => form.get(key))
    .filter((value) => typeof value === "string" && value.trim())
    .join("\n")
    .slice(0, 4000);
  const db = await openShareDb();
  await new Promise((resolve, reject) => {
    const tx = db.transaction(SHARE_STORE, "readwrite");
    tx.objectStore(SHARE_STORE).put({ text, files, at: Date.now() }, "latest");
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
  db.close();
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const scope = SCOPE();
  if (request.method === "POST" && new URL(request.url).pathname === scope.pathname.replace(/\/$/, "") + "/share") {
    event.respondWith(
      (async () => {
        try {
          await stashShare(await request.formData());
        } catch {
          // Nothing kept: the Share page says nothing arrived.
        }
        return Response.redirect(new URL("share?pending=1", scope).href, 303);
      })(),
    );
    return;
  }
  // Only page navigations of this app: API calls, streams, files, assets and
  // any other origin are never touched. The browser's own request goes out
  // unchanged; this handler only answers when the network does not.
  if (request.mode !== "navigate" || new URL(request.url).origin !== self.location.origin) return;
  event.respondWith(
    (async () => {
      try {
        const preloaded = await event.preloadResponse;
        return preloaded || (await fetch(event.request));
      } catch {
        return new Response(OFFLINE_PAGE, {
          status: 503,
          headers: {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
          },
        });
      }
    })(),
  );
});
