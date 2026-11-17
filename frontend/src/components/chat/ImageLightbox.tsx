/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

import { useEffect, useRef, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "@filigran/design-system";
import { useFormatter } from "@/i18n";

/**
 * A button on the lightbox scrim. Its focus ring is the scrim's own token
 * (`ring-on-scrim`, white): `ring-focus` paints #651fe5 in light, about 1.05:1
 * on the scrim, so it is for controls on a panel (dialog.tsx Close) only.
 * `inline-flex` centres the glyph: the design system `<Icon>` is
 * `inline-block align-middle` and would otherwise sit on a text line box.
 */
const LIGHTBOX_BUTTON_CLASS =
  "inline-flex items-center justify-center rounded-sm bg-on-scrim/10 p-2 text-on-scrim transition-colors hover:bg-on-scrim/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-on-scrim";

/**
 * The full-size view of a chat image, the one lightbox of the chat:
 * the pictures and charts an answer carries (ChatMarkdown) open it.
 *
 * A modal dialog named after the image: Escape, the Close button and a
 * click outside the image close it; focus moves to Close when it opens and
 * back to what had it when it closes; Tab and Shift+Tab cycle through the
 * dialog's own buttons, so the page under the scrim is never reached. The
 * page behind does not scroll while it is open: the body's overflow is
 * hidden on open and given back as it was on close. It is portalled to
 * document.body, so it never sits inside a scroller of the page (the chat
 * transcript scrolled behind it), and a mouse press inside it that
 * lands on no button keeps the focus in the dialog.
 */
export function ImageLightbox({
  src,
  alt,
  onClose,
  actions,
  onImageError,
}: {
  src: string;
  alt: string;
  onClose: () => void;
  /** Extra buttons shown before Close (a Download button). */
  actions?: ReactNode;
  onImageError?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  // The latest handler, so the listener below is attached once per opening.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const { body } = document;
    const previousOverflow = body.style.overflow;
    body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Tab") {
        event.preventDefault();
        const buttons = Array.from(
          dialogRef.current?.querySelectorAll<HTMLElement>("button:not([disabled])") ?? [],
        );
        if (buttons.length === 0) return;
        const at = buttons.indexOf(document.activeElement as HTMLElement);
        const step = event.shiftKey ? -1 : 1;
        const next = at === -1 ? 0 : (at + step + buttons.length) % buttons.length;
        buttons[next].focus();
        return;
      }
      if (event.key !== "Escape") return;
      event.preventDefault();
      onCloseRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      body.style.overflow = previousOverflow;
      opener?.focus();
    };
  }, []);

  // A press on the image or the scrim would move the focus to the nearest
  // focusable ancestor; keep it where it is, inside the dialog.
  const keepFocus = (event: MouseEvent<HTMLDivElement>) => {
    if (!(event.target instanceof Element) || !event.target.closest("button")) {
      event.preventDefault();
    }
  };

  return createPortal(
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={alt.trim() || t_i18n("Image preview")}
      tabIndex={-1}
      data-testid="chat-image-lightbox"
      className="fixed inset-0 z-50 flex items-center justify-center bg-scrim backdrop-blur-sm outline-none"
      onClick={onClose}
      onMouseDown={keepFocus}
    >
      <div className="absolute top-4 right-4 flex items-center gap-2">
        {actions}
        {/* data-ds-exempt: on the scrim, the design system has no on-scrim button. */}
        <button
          ref={closeRef}
          data-ds-exempt="scrim"
          type="button"
          aria-label={t_i18n("Close")}
          onClick={onClose}
          className={LIGHTBOX_BUTTON_CLASS}
        >
          <Icon name="x" size={20} />
        </button>
      </div>
      <img
        src={src}
        alt={alt}
        className="max-h-[90vh] max-w-[90vw] rounded-sm object-contain"
        onClick={(e) => e.stopPropagation()}
        onError={onImageError}
      />
    </div>,
    document.body,
  );
}
