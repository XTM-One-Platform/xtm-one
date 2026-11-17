// SPDX-License-Identifier: Apache-2.0

/**
 * Web Push on this device: whether it can receive notifications, and linking
 * it to the person's account (`POST /mobile-devices`).
 *
 * The phone links itself, signed in: the browser asks for permission, then
 * subscribes with the platform's VAPID key, and the subscription is handed to
 * the server, which keeps it encrypted (`models/mobile_device.py`). Nothing
 * here asks for permission on its own: only a person's tap does.
 *
 * On iPhone, Safari gives push only to the INSTALLED app (iOS 16.4+): in a
 * Safari tab the state is `needs-install`.
 */
import { api } from "@/lib/api";
import { deviceName, devicePlatform, isIosSafari } from "@/lib/install-app";
import { isStandalone } from "@/lib/pwa";

export type PushState =
  /** No service worker, Push API or notifications in this browser. */
  | "unsupported"
  /** iPhone / iPad Safari, not installed: install the app first. */
  | "needs-install"
  /** The person blocked notifications for the site (browser settings). */
  | "denied"
  /** Allowed or not yet asked: a tap can link the phone. */
  | "available";

export function pushState(): PushState {
  if (typeof window === "undefined") return "unsupported";
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  if (isIosSafari(navigator.userAgent, navigator.maxTouchPoints ?? 0) && !isStandalone()) return "needs-install";
  if (!supported || !window.isSecureContext) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  return "available";
}

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padded = `${base64}${"=".repeat((4 - (base64.length % 4)) % 4)}`.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

/** The SHA-256 of an endpoint, hex: how the server marks "this phone". */
export async function endpointHash(endpoint: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** This browser's push subscription, if it has one. */
export async function currentSubscription(): Promise<PushSubscription | null> {
  if (pushState() === "unsupported" || pushState() === "needs-install") return null;
  const registration = await navigator.serviceWorker.getRegistration();
  return (await registration?.pushManager.getSubscription()) ?? null;
}

/** Ask for permission (a tap must lead here), subscribe, and link this phone. */
export async function linkThisPhone(): Promise<"linked" | "denied"> {
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return "denied";
  const { public_key } = await api.get<{ public_key: string }>("/mobile-devices/vapid-public-key");
  const registration = await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(public_key),
    });
  }
  await api.post("/mobile-devices", {
    subscription: subscription.toJSON(),
    name: deviceName(navigator.userAgent),
    platform: devicePlatform(navigator.userAgent),
  });
  return "linked";
}
