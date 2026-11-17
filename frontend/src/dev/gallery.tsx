/**
 * Entry point for the primitive gallery — a second Vite HTML entry, reachable
 * at /gallery.html in dev and absent from the production bundle (vite builds
 * index.html only).
 *
 * Deliberately does NOT mount the app's providers. `PlatformProvider` fetches
 * the platform configuration at boot and the query client is configured with
 * `throwOnError`, so without a backend the whole app is replaced by an error
 * boundary — which would make the gallery useless as a visual reference. A bare
 * `IntlProvider`, a `MemoryRouter` and the toasts are everything the primitives
 * need.
 */
import React from "react";
import ReactDOM from "react-dom/client";
import { IntlProvider } from "react-intl";
import { MemoryRouter } from "react-router-dom";
import messages_en from "@/i18n/lang/en.json";
import { ToastProvider } from "@/components/ui/toast";
import { PrimitiveGallery } from "./PrimitiveGallery";
import "../index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <IntlProvider locale="en-us" defaultLocale="en-us" messages={messages_en}>
      <ToastProvider>
        <MemoryRouter>
          <PrimitiveGallery />
        </MemoryRouter>
      </ToastProvider>
    </IntlProvider>
  </React.StrictMode>,
);
