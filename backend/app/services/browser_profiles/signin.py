# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Signing a browser profile in, MFA included, and keeping its session.

The sign-in runs in the profile's own browser context, in the platform's
browser: a person signing in to the application in a tab of their own signs
nothing in here. It reads the page by STRUCTURE, never by its words (a button
label changes with the language and the tenant): a password field, a
one-time-code field (``autocomplete="one-time-code"``, or a numeric input of
6 to 8 characters, or one box per digit), an alert region, a large short
number on a page without fields (a number-match prompt), an unticked checkbox
and the form's default button on a page without fields (a "stay signed in" /
"don't ask again" interstitial), a form of hidden fields posting to another
site (a SAML ACS callback), a meta refresh, the host the browser is on. The
profile's own selectors (``profiles.LOGIN_CONFIG_KEYS``) come first. That
holds for a first-party form and for a hosted identity provider alike.

An MFA prompt without a TOTP secret opens a challenge
(``status="mfa_required"``, ``kind`` ``code`` | ``push_number`` | ``push``):
the browser stays open on the prompt for the profile's ``mfa_timeout_seconds``
while the person types the code - in the chat (``submit_browser_mfa_code``) or
on the Browser profiles page - or approves the push, whose screenshot (and the
number to match) the challenge carries. With a TOTP secret the code is
computed (``totp.py``); a refused one is retried once with the next period's.

The flow is complete when the browser has left the identity provider and the
start URL renders the application (:func:`check_authenticated`): the whole
redirect chain is followed - HTTP redirects, an auto-posting relay form, a
meta refresh, a script - with the profile's ``sign_in_timeout_seconds``
counted from the last navigation, and a timeout names its stage
(:data:`STAGES`) and the last URL. Only then is the session read - the
cookies of every site visited, local storage and IndexedDB, the application
origin's ``sessionStorage`` - within ``session_save_timeout_seconds``, and
probed in a fresh context before it is called signed in.

The challenge lives in Redis (its state and the codes sent to it), so the code
may arrive on any replica while the browser waits on the one that opened it;
without Redis, the same process only. A code is never logged or stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.core import totp
from app.core.database import async_session
from app.core.utils import format_exception_message, safe_uuid
from app.models.browser_profile import (
    SESSION_AWAITING_MFA,
    SESSION_FAILED,
    SESSION_SIGNING_IN,
    BrowserProfile,
)
from app.services.browser_profiles.browser import (
    ProfileBrowser,
    TargetRefusedError,
    origin_of,
    persistence_note,
    profile_browser,
)
from app.services.browser_profiles.profiles import ProfileSnapshot, record_probe, resolved_snapshot, save_storage_state

logger = logging.getLogger(__name__)

#: Where a sign-in stands, as the agent and the UI read it.
STATUS_RUNNING = "running"
STATUS_ACTIVE = "active"
STATUS_MFA_REQUIRED = "mfa_required"
STATUS_FAILED = "failed"
#: Signed in, but the probe of the saved session got no answer from the application (``http_status``):
#: neither signed in nor failed, the session is kept ``unverified``.
STATUS_UNVERIFIED = "unverified"
#: What an MFA challenge asks of the person.
KIND_CODE = "code"
KIND_PUSH_NUMBER = "push_number"
KIND_PUSH = "push"

#: The stages a sign-in times out at, and how a person reads each.
STAGE_IDP_FORM = "idp_form"
STAGE_MFA = "mfa"
STAGE_REDIRECT_CHAIN = "redirect_chain"
STAGE_APP_LOAD = "app_load"
STAGE_SESSION_SAVE = "session_save"
STAGES: dict[str, str] = {
    STAGE_IDP_FORM: "the sign-in form",
    STAGE_MFA: "the MFA prompt",
    STAGE_REDIRECT_CHAIN: "the redirects back to the application",
    STAGE_APP_LOAD: "the application loading signed in",
    STAGE_SESSION_SAVE: "saving the session",
}
#: A failure stage that is not a timeout (so it stays out of :data:`STAGES`): the
#: authenticator code was refused on two consecutive time steps.
STAGE_MFA_TOTP_REFUSED = "mfa_totp_refused"

#: A fieldless identity-provider page carrying one of these in its path is a
#: WebAuthn / passkey bridge (Microsoft Entra ``/<tenant>/bridge/fido``): a method
#: headless Chromium cannot use, backed out of rather than waited on as a push.
_FIDO_BRIDGE_PATHS: tuple[str, ...] = ("/bridge/fido",)
#: Controls that leave an unusable method for the credential picker ("Sign in
#: another way" / the back arrow), by stable id, tried when no ``cancelUrl`` is given.
_OTHER_WAYS_SELECTORS: tuple[str, ...] = (
    "#signInAnotherWay",
    "#idA_PWD_SwitchToCredPicker",
    "#idBtn_Back",
    '[data-value="SwitchToCredPicker"]',
)
#: Credential-picker methods that yield a code the platform can TYPE (an
#: authenticator code or an SMS), most preferred first. Chosen over a push,
#: because the platform computes or is handed a code but cannot approve a push.
_PICKER_CODE_VALUES: tuple[str, ...] = ("PhoneAppOTP", "OneWaySMS", "ConsolidatedTelephony")
#: A push method, chosen only when the picker offers no code method at all.
_PICKER_PUSH_VALUES: tuple[str, ...] = ("PhoneAppNotification",)
#: The password method of a sign-in method picker (Entra "Choose a way to sign in": a passkey / FIDO row and
#: "Use my password"), by ``data-value`` or id, chosen first when the profile holds a password. The passkey row is
#: never chosen: headless Chromium cannot run its WebAuthn ceremony.
_PICKER_FIRST_CHOICE_VALUES: tuple[str, ...] = ("Password", "PWD")
_PICKER_FIRST_CHOICE_SELECTORS: tuple[str, ...] = ("#idA_PWD_SwitchToPassword", "#idA_PWD_SwitchToPasswordLink")
#: The visible label of a password row, when no attribute names it.
_PICKER_FIRST_CHOICE_LABEL = r"^\s*(use (my|your|a) password|sign in with (my |your |a )?password|password)\s*$"
#: The "Stay signed in?" interstitial: tick "don't ask again", then confirm.
_KMSI_CONFIRM_SELECTOR = "#idSIButton9"
_KMSI_DONT_ASK_SELECTOR = "#idChkBx_SAOTCC_TD"
#: A "More information required" / proof-up interstitial's skip controls.
_PROOF_UP_SKIP_SELECTORS: tuple[str, ...] = ("#iShowSkip", "#idA_SAASTO_Skip", '[data-value="skip"]')
#: The password form's error banner (its presence is what makes a password refusal, #5).
_PASSWORD_ERROR_SELECTOR = "#passwordError"  # noqa: S105 - a DOM selector, not a secret

#: The ``FileAttachment.source`` of an MFA prompt's screenshot.
SOURCE_MFA_SCREENSHOT = "browser_profile_mfa"

_STATE_TTL_SECONDS = 1800
_POLL_SECONDS = 0.5
#: A redirect chain rests once the main frame has not moved for this long.
_QUIET_SECONDS = 1.5
#: How long a page with nothing to fill waits before it reads as a push approval.
_APPROVAL_STALL_SECONDS = 6.0
#: How long an action waits for the page to answer it before the page is read again.
_ACTION_WAIT_SECONDS = 15.0
#: A page read is bounded: a script waits while a navigation is in flight (a slow callback POST).
_READ_TIMEOUT_SECONDS = 5.0
#: How long the start URL is waited for again after it answered "unavailable" (a 5xx, a 429): a server
#: shedding load or a rate limit refills in seconds, and each load of a single-page application asks for
#: tens of files. One wait per retry; ``Retry-After`` lengthens one, up to the cap.
_UNAVAILABLE_BACKOFF_SECONDS: tuple[float, ...] = (3.0, 8.0, 15.0)
_RETRY_AFTER_CAP_SECONDS = 30.0
#: One probe (its own page and the signed-out render it compares with) retries within this budget, counted from
#: its start: a wait that would end past it is not taken, so a synchronous check answers in bounded time.
_UNAVAILABLE_BUDGET_SECONDS = 30.0

#: Structural selectors (attributes and input types, no visible text).
USERNAME_SELECTORS: tuple[str, ...] = (
    'input[autocomplete="username"]',
    'input[autocomplete~="username" i]',
    'input[autocomplete~="email" i]',
    'input[type="email"]',
    'input[name="username" i]',
    'input[name="email" i]',
    'input[name="login" i]',
    'input[id="username" i]',
    'input[id="email" i]',
    'input[name*="user" i]:not([type="hidden"])',
    'input[name*="mail" i]:not([type="hidden"])',
)
PASSWORD_SELECTORS: tuple[str, ...] = ('input[type="password"]',)
#: First-party one-time-code fields named for what they hold, read after the generic signals.
OTP_NAME_HINTS: tuple[str, ...] = (
    'input[name*="otp" i]',
    'input[id*="otp" i]',
    'input[name*="totp" i]',
    'input[name*="mfa" i]',
    'input[id*="mfa" i]',
    'input[name*="2fa" i]',
    'input[name*="passcode" i]',
    'input[name*="verification" i]',
)

_CLASSIFY_JS = r"""
(cfg) => {
  const shown = (el) => {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 4 || r.height <= 4) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity || 1) > 0.05;
  };
  const usable = (el) => shown(el) && !el.disabled && !el.readOnly;
  for (const el of document.querySelectorAll('[data-xtm-signin]')) el.removeAttribute('data-xtm-signin');
  const mark = (el, name) => { el.setAttribute('data-xtm-signin', name); return `[data-xtm-signin="${name}"]`; };
  const taken = new Set();
  const pick = (selectors, name, accept) => {
    for (const sel of selectors || []) {
      let found;
      try { found = document.querySelectorAll(sel); } catch (e) { continue; }
      for (const el of found) {
        if (!taken.has(el) && usable(el) && (!accept || accept(el))) { taken.add(el); return mark(el, name); }
      }
    }
    return null;
  };
  const kind = (el) => (el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text').toLowerCase());
  const formOfField = (el) => (el && (el.form || el.closest('form'))) || null;
  const textKinds = ['text', 'email', 'tel', 'number', 'password', 'search', 'url', 'textarea'];
  const fields = [...document.querySelectorAll('input, textarea')].filter((el) => textKinds.includes(kind(el)) && usable(el));
  const maxLength = (el) => { const n = parseInt(el.getAttribute('maxlength') || '', 10); return Number.isFinite(n) && n > 0 ? n : null; };
  const numeric = (el) => (el.getAttribute('inputmode') || '').toLowerCase() === 'numeric'
    || ['tel', 'number'].includes(kind(el))
    || /^\^?(\\d|\[0-9\])/.test(el.getAttribute('pattern') || '');
  const oneTime = (el) => (el.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/).includes('one-time-code');
  const shortNumeric = (el) => { const n = maxLength(el); return numeric(el) && n !== null && n >= 6 && n <= 8; };

  let otp = pick(cfg.otpOwn ? [cfg.otpOwn] : [], 'otp');
  let otpStrong = !!otp;
  if (!otp) {
    const el = fields.find((f) => oneTime(f));
    if (el) { taken.add(el); otp = mark(el, 'otp'); otpStrong = true; }
  }
  if (!otp) {
    const el = fields.find((f) => kind(f) !== 'password' && shortNumeric(f));
    if (el) { taken.add(el); otp = mark(el, 'otp'); }
  }
  if (!otp) otp = pick(cfg.otpHints, 'otp');
  let otpBoxes = 0;
  const boxes = fields.filter((f) => !taken.has(f) && maxLength(f) === 1);
  if (!otp && boxes.length >= 4 && boxes.length <= 8) {
    boxes.forEach((f, i) => { taken.add(f); mark(f, `otp-box-${i}`); });
    otpBoxes = boxes.length;
  }
  const password = pick(cfg.password, 'password');
  let username = pick(cfg.username, 'username', (el) => kind(el) !== 'password');
  if (!username && password) {
    // No attribute names it: the text or email field placed before the password, in the same form.
    const pw = document.querySelector('[data-xtm-signin="password"]');
    const before = fields.filter((f) => !taken.has(f) && ['text', 'email'].includes(kind(f))
      && (pw.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_PRECEDING)
      && (!formOfField(pw) || formOfField(f) === formOfField(pw)));
    if (before.length) { const el = before[before.length - 1]; taken.add(el); username = mark(el, 'username'); }
  }

  let alert = null;
  for (const el of document.querySelectorAll('[role="alert"],[aria-live="assertive"]')) {
    const text = (el.innerText || '').trim();
    if (shown(el) && text) { alert = text.slice(0, 300); break; }
  }

  const isSubmit = (el) => (el.tagName === 'BUTTON'
    ? (el.getAttribute('type') || 'submit').toLowerCase() === 'submit'
    : ['submit', 'image'].includes((el.type || '').toLowerCase()));
  const submits = [...document.querySelectorAll('button, input[type="submit"], input[type="image"]')]
    .filter((el) => isSubmit(el) && usable(el));

  let ticks = 0;
  for (const el of document.querySelectorAll('input[type="checkbox"], [role="checkbox"]')) {
    const native = el.tagName === 'INPUT';
    const checked = native ? el.checked : el.getAttribute('aria-checked') === 'true';
    if (checked || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    let target = shown(el) ? el : null;
    if (!target && native) target = [...(el.labels || [])].find(shown) || null;
    if (target) mark(target, `tick-${ticks++}`);
  }
  const formOf = (el) => (el ? (el.form || (el.control && el.control.form) || el.closest('form')) : null);
  let form = formOf(document.querySelector('[data-xtm-signin="tick-0"]'))
    || formOf(document.querySelector('[data-xtm-signin="otp"], [data-xtm-signin="password"], [data-xtm-signin="username"]'));
  if (!form) {
    const forms = [...document.forms].filter((f) => submits.some((b) => b.form === f));
    if (forms.length === 1) form = forms[0];
  }
  const own = form ? submits.filter((b) => b.form === form) : submits;
  let accept = false;
  if (own.length && (form || own.length === 1)) { mark(own[0], 'accept'); accept = true; }

  let number = null;
  if (!fields.length && document.body) {
    const base = parseFloat(getComputedStyle(document.body).fontSize) || 16;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node, seen = 0;
    while ((node = walker.nextNode()) && seen++ < 5000) {
      const text = (node.nodeValue || '').trim();
      if (!/^\d{1,3}$/.test(text)) continue;
      const el = node.parentElement;
      if (!el || !shown(el) || (el.innerText || '').trim() !== text) continue;
      if ((parseFloat(getComputedStyle(el).fontSize) || 0) >= Math.max(24, base * 1.5)) { number = text; break; }
    }
  }

  let relay = null;
  for (const f of document.forms) {
    if ((f.getAttribute('method') || 'get').toLowerCase() !== 'post') continue;
    const controls = [...f.elements];
    const hidden = controls.filter((c) => c.tagName === 'INPUT' && (c.type || '').toLowerCase() === 'hidden');
    const open = controls.filter((c) => ['INPUT', 'SELECT', 'TEXTAREA'].includes(c.tagName)
      && !['hidden', 'submit', 'image', 'button', 'reset'].includes((c.type || '').toLowerCase()) && shown(c));
    if (!hidden.length || open.length) continue;
    let action;
    try { action = new URL(f.getAttribute('action') || location.href, location.href); } catch (e) { continue; }
    if (action.host !== location.host) { mark(f, 'relay'); relay = action.href; break; }
  }

  let metaRefresh = null;
  const meta = document.querySelector('meta[http-equiv="refresh" i]');
  if (meta) {
    const n = parseFloat((meta.getAttribute('content') || '').split(/[;,]/)[0]);
    if (Number.isFinite(n)) metaRefresh = n;
  }

  let signedIn = null;
  if (cfg.signedIn) {
    signedIn = false;
    try { for (const el of document.querySelectorAll(cfg.signedIn)) if (shown(el)) { signedIn = true; break; } } catch (e) {}
  }

  // A fieldless page that asks for an approval elsewhere (an authenticator push), as opposed to a method picker.
  let approval = false;
  let picker = false;
  if (!fields.length && document.body) {
    const said = ((document.title || '') + ' ' + (document.body.innerText || '').slice(0, 4000)).toLowerCase();
    const methods = [...document.querySelectorAll('[data-value]')].filter(shown).length;
    picker = methods >= 2 || /choose a way to sign in|sign in another way|verify your identity|choose how/.test(said);
    const marked = !!document.querySelector('#idDiv_SAOTCAS_Title, #idDiv_SAOTCAS_Description, #idRichContext_DisplaySign');
    approval = marked || (!picker
      && /approv|authenticator app|notification|push|check your (phone|device)|respond on your|tap (yes|approve)|verify it.?s you/.test(said));
  }

  let interactive = 0;
  for (const el of document.querySelectorAll('a[href], button, input, select, textarea, [role="button"], [role="link"], [role="menuitem"], [role="tab"]')) {
    if (shown(el)) interactive++;
    if (interactive > 2000) break;
  }
  return {
    url: location.href,
    title: (document.title || '').trim().slice(0, 200),
    password, otp, otpStrong, otpBoxes, username, signedIn, alert,
    webauthnAttempts: (window.__xtmWebauthn && window.__xtmWebauthn.attempts) || 0,
    documentStatus: ((performance.getEntriesByType('navigation') || [])[0] || {}).responseStatus || 0,
    assetStatus: (() => {
      // Same-origin (or CORS) scripts and stylesheets only: a cross-origin entry reports no status.
      for (const e of performance.getEntriesByType('resource')) {
        const st = e.responseStatus || 0;
        const asset = e.initiatorType === 'script' || e.initiatorType === 'link';
        if (asset && (st >= 500 || [408, 425, 429].includes(st))) return st;
      }
      return 0;
    })(),
    submitCount: submits.length,
    number,
    approval,
    picker,
    ticks,
    accept,
    textInputs: fields.length,
    relay,
    metaRefresh,
    signature: fields.map((el) => `${kind(el)}:${el.getAttribute('name') || el.id || ''}`).sort().join('|'),
    interactive,
    textLength: Math.min(((document.body && document.body.innerText) || '').length, 1000000),
  };
}
"""

_POST_RELAY_JS = """
() => {
  const form = document.querySelector('[data-xtm-signin="relay"]');
  if (form) HTMLFormElement.prototype.submit.call(form);
}
"""


@dataclass
class PageState:
    url: str
    title: str = ""
    password: str | None = None
    otp: str | None = None
    #: The one-time-code field says so (``autocomplete``) or is the profile's own selector.
    otp_strong: bool = False
    otp_boxes: int = 0
    username: str | None = None
    signed_in: bool | None = None
    alert: str | None = None
    submit_count: int = 0
    #: A large one-to-three-digit number on a page without fields: a number-match prompt.
    number: str | None = None
    #: A page without fields that asks for an approval elsewhere (Entra's "Approve sign in request", an
    #: authenticator push): the only fieldless page announced as a push.
    approval: bool = False
    #: A page without fields offering sign-in methods ("Choose a way to sign in"): never a push.
    picker: bool = False
    #: Unticked checkboxes (``[data-xtm-signin="tick-N"]``).
    ticks: int = 0
    #: The default button of the page's form is marked ``[data-xtm-signin="accept"]``.
    accept: bool = False
    text_inputs: int = 0
    #: The action of a form of hidden fields posting to another site (a relay such as a SAML ACS callback).
    relay: str | None = None
    meta_refresh: float | None = None
    signature: str = ""
    interactive: int = 0
    text_length: int = 0
    #: ``False`` when the page could not be read (navigating): nothing above is known.
    readable: bool = True
    #: How many ``navigator.credentials.get`` calls the page made (blocked and counted by the browser's
    #: WebAuthn override): a non-zero count on a fieldless page is a FIDO / passkey bridge waiting on a key.
    webauthn_attempts: int = 0
    #: The HTTP status that left the page unusable: the document's own (5xx, 429), or a script or stylesheet's
    #: when the page renders nothing. Such a page says nothing of the session.
    http_error: int | None = None

    @property
    def empty(self) -> bool:
        """Nothing to read or click: a page whose scripts did not run (or did not load)."""
        return self.interactive == 0 and self.text_length == 0

    @property
    def sign_in_shaped(self) -> bool:
        """A password or a one-time-code field: a sign-in form, never the signed-in application."""
        return bool(self.password or (self.otp and self.otp_strong) or self.otp_boxes)

    @property
    def view(self) -> tuple:
        return (self.url, self.title, self.signature, bool(self.otp), self.otp_boxes, self.number, self.alert)


def _host(url: str | None) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _page_id(url: str | None) -> str:
    """A page as a person tells two apart: origin, path and a hash route; never the query."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return url or ""
    fragment = parsed.fragment if parsed.fragment.startswith(("/", "!")) else ""
    return f"{origin_of(url)}{parsed.path.rstrip('/') or '/'}#{fragment.split('?')[0]}"


def _same_page(a: str | None, b: str | None) -> bool:
    return bool(a and b) and _page_id(a) == _page_id(b)


def _close(a: int, b: int) -> bool:
    return abs(a - b) <= max(2, 0.2 * max(a, b))


def _same_render(a: PageState, b: PageState) -> bool:
    """Whether two reads of a page show the same thing: title, fields, and about as much to click and read."""
    return (
        a.title == b.title
        and a.signature == b.signature
        and _close(a.interactive, b.interactive)
        and _close(a.text_length, b.text_length)
    )


def _selectors(config: dict, key: str, defaults: tuple[str, ...]) -> list[str]:
    own = config.get(key)
    return [own, *defaults] if own else list(defaults)


def _matches(pattern: str | None, url: str) -> bool:
    if not pattern:
        return False
    try:
        return re.search(pattern, url) is not None
    except re.error:
        return pattern in url


async def classify(page: Any, snap: ProfileSnapshot) -> PageState:
    """What the page is, read from its structure (and the marks it leaves for the elements found)."""
    cfg = snap.login_config
    args = {
        "username": _selectors(cfg, "username_selector", USERNAME_SELECTORS),
        "password": _selectors(cfg, "password_selector", PASSWORD_SELECTORS),
        "otpOwn": cfg.get("otp_selector"),
        "otpHints": list(OTP_NAME_HINTS),
        "signedIn": cfg.get("signed_in_selector"),
    }
    data: dict = {}
    for attempt in range(3):
        try:
            data = await asyncio.wait_for(page.evaluate(_CLASSIFY_JS, args), _READ_TIMEOUT_SECONDS) or {}
            break
        except TimeoutError:
            return PageState(url=str(getattr(page, "url", "") or ""), readable=False)
        except Exception as exc:  # the page navigated while it was read
            if attempt == 2:
                logger.debug("Sign-in page not read: %s", format_exception_message(exc))
                return PageState(url=str(getattr(page, "url", "") or ""), readable=False)
            await asyncio.sleep(0.4)
    meta = data.get("metaRefresh")
    state = PageState(
        url=str(data.get("url") or ""),
        title=str(data.get("title") or ""),
        password=data.get("password"),
        otp=data.get("otp"),
        otp_strong=bool(data.get("otpStrong")),
        otp_boxes=int(data.get("otpBoxes") or 0),
        username=data.get("username"),
        signed_in=data.get("signedIn"),
        alert=data.get("alert"),
        submit_count=int(data.get("submitCount") or 0),
        number=data.get("number"),
        approval=bool(data.get("approval")),
        picker=bool(data.get("picker")),
        ticks=int(data.get("ticks") or 0),
        accept=bool(data.get("accept")),
        text_inputs=int(data.get("textInputs") or 0),
        relay=data.get("relay"),
        meta_refresh=float(meta) if meta is not None else None,
        signature=str(data.get("signature") or ""),
        interactive=int(data.get("interactive") or 0),
        text_length=int(data.get("textLength") or 0),
        webauthn_attempts=int(data.get("webauthnAttempts") or 0),
    )
    # What the page says of its own load (Navigation and Resource Timing), for a page no _Navigation watched.
    asset = _status_error(data.get("assetStatus"))
    state.http_error = _status_error(data.get("documentStatus")) or (asset if state.empty else None)
    return state


def _status_error(status: object) -> int | None:
    """An HTTP status the page read of its own load, when it is an "unavailable" one."""
    try:
        code = int(status or 0)
    except (TypeError, ValueError):
        return None
    return code if code and _unavailable(code) else None


# ── Outcomes and errors ───────────────────────────────────────────────────


@dataclass
class TraceStep:
    """One decision the sign-in took, for the operator to see where it went (and where it stalled)."""

    stage: str
    url: str
    decision: str
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


class SignInRequiredError(RuntimeError):
    """The profile cannot sign in without a person (an MFA prompt and no TOTP secret, a push approval)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.stage: str | None = STAGE_MFA
        self.last_url: str | None = None
        self.trace: list[dict] = []


class SignInTimeoutError(RuntimeError):
    """A sign-in stage that did not move in time; ``str()`` names the stage and the last URL."""

    def __init__(self, stage: str, last_url: str | None, seconds: float) -> None:
        self.stage = stage
        self.last_url = last_url or ""
        self.seconds = seconds
        self.trace: list[dict] = []
        super().__init__(
            f"The sign-in timed out at stage {stage} ({STAGES.get(stage, stage)}) after {int(seconds)} s "
            f"without progress. Last URL: {self.last_url or 'none'}."
        )


@dataclass
class ProbeResult:
    """Whether ``start_url`` opened in a session renders the application, where it landed and why it reads so."""

    authenticated: bool
    url: str
    reason: str
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    #: Set when the server could not serve the page (a 5xx, a 429): the check says nothing of the session,
    #: which is then neither called expired nor signed in again.
    http_status: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MfaPrompt:
    """What the sign-in shows the person: the kind of prompt, where, its screenshot and the number to match."""

    kind: str
    url: str
    screenshot_file_id: str | None = None
    number: str | None = None
    #: Why the prompt is asked again (the page's answer to the last code).
    note: str | None = None


@dataclass
class SignInResult:
    ok: bool
    message: str
    probe: ProbeResult | None = None
    stage: str | None = None
    last_url: str | None = None
    #: The decisions the sign-in took (``[TraceStep.to_dict()]``), newest last.
    trace: list[dict] = field(default_factory=list)
    #: Whether the sign-in was driven (``False`` when the saved session was already signed in): only a driven
    #: sign-in says whether the credentials sign in without a person (``BrowserProfile.signs_in_unattended``).
    driven: bool = False


@dataclass
class SignInOutcome:
    """Where a sign-in stands, as the agent and the UI read it."""

    status: str  # running | active | mfa_required | failed | unverified
    message: str
    challenge_id: str | None = None
    kind: str | None = None  # mfa_required: code | push_number | push
    number: str | None = None
    screenshot_file_id: str | None = None
    expires_at: str | None = None
    profile_id: str | None = None
    stage: str | None = None
    last_url: str | None = None
    probe: dict | None = None
    #: The decisions the sign-in took (``[TraceStep.to_dict()]``), for the status trace.
    trace: list[dict] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── The navigation clock ──────────────────────────────────────────────────


def _unavailable(status: int) -> bool:
    """An HTTP status of a server that cannot answer right now (overloaded, rate limited, failing)."""
    return status >= 500 or status in (408, 425, 429)


def _retry_after(headers: dict | None) -> float | None:
    """A ``Retry-After`` in seconds (the HTTP-date form is not read)."""
    try:
        value = float(str((headers or {}).get("retry-after") or "").strip())
    except ValueError:
        return None
    return value if 0 <= value < 86400 else None


class _Navigation:
    """When the page's main frame last moved and how often: the clock every sign-in timeout counts from.

    It also keeps what the server answered the current document: its own
    status, and the first "unavailable" status of a script or stylesheet it
    loaded (:func:`_unavailable`), so a page the server could not serve is
    never read as the application's own page.
    """

    def __init__(self, page: Any) -> None:
        self.page = page
        self.last = time.monotonic()
        self.count = 0
        self.urls: list[str] = []
        self._pending: dict[Any, float] = {}
        self.document_status: int | None = None
        self.asset_status: int | None = None
        self.retry_after: float | None = None
        page.on("framenavigated", self._moved)
        page.on("request", self._request)
        page.on("requestfinished", self._settled)
        page.on("requestfailed", self._settled)
        page.on("response", self._response)

    def _moved(self, frame: Any) -> None:
        if frame.parent_frame is not None:
            return
        self.last = time.monotonic()
        self.count += 1
        self.urls.append(frame.url)
        del self.urls[:-50]
        # A committed document ends the navigation requests before it (a redirect hop is not always finished).
        self._pending.clear()

    def _request(self, request: Any) -> None:
        with contextlib.suppress(Exception):  # a worker's request has no frame
            if request.is_navigation_request() and request.frame.parent_frame is None:
                self._pending[request] = time.monotonic()
                self.last = time.monotonic()
                if request.redirected_from is None:  # a new document: forget what the last one was answered
                    self.document_status = self.asset_status = self.retry_after = None

    def _settled(self, request: Any) -> None:
        self._pending.pop(request, None)

    def _response(self, response: Any) -> None:
        with contextlib.suppress(Exception):  # a worker's response has no frame
            request = response.request
            status = int(response.status)
            if request.is_navigation_request() and request.frame.parent_frame is None:
                if not 300 <= status < 400:
                    self.document_status = status
                    self.retry_after = _retry_after(response.headers) if _unavailable(status) else None
            elif (
                self.asset_status is None and request.resource_type in ("script", "stylesheet") and _unavailable(status)
            ):
                self.asset_status = status
                self.retry_after = self.retry_after or _retry_after(response.headers)

    def unavailable(self, state: PageState) -> int | None:
        """The status that left *state* unusable: the document's, or a script / stylesheet's on an empty page."""
        if self.document_status is not None and _unavailable(self.document_status):
            return self.document_status
        if self.asset_status is not None and state.empty:
            return self.asset_status
        return None

    def bump(self) -> None:
        self.last = time.monotonic()

    @property
    def idle(self) -> float:
        return time.monotonic() - self.last

    @property
    def in_flight(self) -> bool:
        """Whether any main-frame navigation is still in flight, however long ago it started.

        A relay form whose own post is still waiting for its answer has posted
        itself: posting it again would restart the timeout clock and send the
        assertion twice, so a callback slower than the timeout would never fail.
        """
        return bool(self._pending)

    def navigating(self, within: float) -> bool:
        """Whether a main-frame navigation started less than *within* seconds ago is still in flight.

        The rest check of ``_follow``: a navigation stuck for longer than
        *within* does not keep the page from being read as resting. The relay
        branches use ``in_flight`` instead.
        """
        now = time.monotonic()
        return any(now - started < within for started in self._pending.values())

    def detach(self) -> None:
        for event, handler in (
            ("framenavigated", self._moved),
            ("request", self._request),
            ("requestfinished", self._settled),
            ("requestfailed", self._settled),
            ("response", self._response),
        ):
            with contextlib.suppress(Exception):
                self.page.remove_listener(event, handler)

    async def quiet(self, seconds: float, *, limit: float) -> None:
        """Wait until the main frame has not moved for *seconds* (at most *limit* seconds)."""
        end = time.monotonic() + max(0.0, limit)
        while self.idle < seconds and time.monotonic() < end:
            await asyncio.sleep(0.15)
        with contextlib.suppress(Exception):  # a page still loading is read as it is
            await self.page.wait_for_load_state(
                "domcontentloaded", timeout=max(500, int((end - time.monotonic()) * 1000))
            )


async def _follow(page: Any, nav: _Navigation, snap: ProfileSnapshot, *, inactivity: float) -> PageState:
    """Follow the redirects until the page rests (the main frame still for a moment); its state.

    HTTP redirects and scripts move the page by themselves; a relay form of
    hidden fields that does not post itself is posted once; a meta refresh is
    waited for. *inactivity* bounds the wait from the last navigation. The
    state carries ``http_error`` when the server could not serve the page.
    """
    posted: set[str] = set()
    hard = time.monotonic() + inactivity * 3
    while True:
        room = max(0.5, min(inactivity - nav.idle, hard - time.monotonic()))
        await nav.quiet(_QUIET_SECONDS, limit=room)
        with contextlib.suppress(Exception):  # long-polling applications never go idle
            await page.wait_for_load_state("networkidle", timeout=3000)
        await nav.quiet(_QUIET_SECONDS, limit=3.0)
        state = await classify(page, snap)
        state.http_error = nav.unavailable(state) or state.http_error
        if nav.idle >= inactivity or time.monotonic() >= hard:
            return state
        if not state.readable:
            await asyncio.sleep(0.3)
            continue
        if state.relay:
            key = _page_id(state.url)
            if key not in posted and nav.idle >= 2.0 and not nav.in_flight:
                posted.add(key)
                with contextlib.suppress(Exception):  # the form may have posted itself meanwhile
                    await page.evaluate(_POST_RELAY_JS)
                nav.bump()
            await asyncio.sleep(0.3)
            continue
        if state.meta_refresh is not None and state.meta_refresh <= 30 and nav.idle < state.meta_refresh + 2:
            await asyncio.sleep(0.3)
            continue
        if nav.idle >= _QUIET_SECONDS and not nav.navigating(inactivity):
            return state
        await asyncio.sleep(0.2)


def _retry_wait(attempt: int, retry_after: float | None) -> float:
    """The wait before opening a refused page again: the backoff step, or ``Retry-After`` when longer, capped."""
    return min(max(_UNAVAILABLE_BACKOFF_SECONDS[attempt], retry_after or 0.0), _RETRY_AFTER_CAP_SECONDS)


async def _open_start(
    rb: ProfileBrowser,
    snap: ProfileSnapshot,
    page: Any,
    nav: _Navigation,
    *,
    navigate: bool,
    inactivity: float,
    deadline: float | None = None,
) -> PageState:
    """Open ``start_url`` (unless *navigate* is off) and follow it until it rests; the rested state.

    While the server answers "unavailable" (``http_error``), ``start_url`` is
    opened again after a wait (:func:`_retry_wait`), as long as the wait ends
    before *deadline* (``time.monotonic()``; :data:`_UNAVAILABLE_BUDGET_SECONDS`
    from now by default): the state returned still carries ``http_error`` when
    every attempt was refused. Raises :class:`TargetRefusedError`.
    """
    deadline = deadline if deadline is not None else time.monotonic() + _UNAVAILABLE_BUDGET_SECONDS
    attempts = len(_UNAVAILABLE_BACKOFF_SECONDS)
    for attempt in range(attempts + 1):
        if navigate:
            nav.bump()
            try:
                await rb.goto(snap.start_url, page=page)
            except TargetRefusedError:
                raise
            except Exception as exc:  # read wherever the page is
                logger.debug("start_url did not load cleanly: %s", format_exception_message(exc))
        state = await _follow(page, nav, snap, inactivity=inactivity)
        if state.http_error is None or attempt == attempts:
            return state
        wait = _retry_wait(attempt, nav.retry_after)
        if time.monotonic() + wait > deadline:
            return state
        logger.info(
            "browser-profile %s: start_url answered HTTP %d, opened again in %.0f s", snap.id, state.http_error, wait
        )
        await asyncio.sleep(wait)
        navigate = True
    return state


# ── Is the application signed in? ─────────────────────────────────────────


def _passed(url: str, reason: str) -> ProbeResult:
    return ProbeResult(authenticated=True, url=url, reason=reason)


def _failed(url: str, reason: str) -> ProbeResult:
    return ProbeResult(authenticated=False, url=url, reason=reason)


def _unavailable_reason(status: int, what: str) -> str:
    return (
        f"{what} answered HTTP {status}: the application is unavailable or limits the rate of requests, so "
        "this check says nothing of the session (check again later)"
    )


def _unavailable_probe(url: str, status: int, what: str) -> ProbeResult:
    return ProbeResult(authenticated=False, url=url, reason=_unavailable_reason(status, what), http_status=status)


async def signed_out_render(
    rb: ProfileBrowser, snap: ProfileSnapshot, *, deadline: float | None = None
) -> PageState | None:
    """What ``start_url`` renders without any session (a fresh context of the same browser), once per browser.

    A render the server could not serve (``http_error`` still set after the
    retries of :func:`_open_start`, within the probe's *deadline*) is returned
    but not kept: the next probe reads it again.
    """
    key = f"signed_out:{snap.start_url}"
    if key in rb.memo:
        return rb.memo[key]
    state = None
    try:
        async with rb.fresh_page(None) as page:
            nav = _Navigation(page)
            try:
                state = await _open_start(
                    rb, snap, page, nav, navigate=True, inactivity=min(snap.sign_in_timeout, 30.0), deadline=deadline
                )
            finally:
                nav.detach()
    except Exception as exc:
        logger.debug("Signed-out render of %s not read: %s", snap.start_url, format_exception_message(exc))
    if state is None or state.http_error is None:
        rb.memo[key] = state
    return state


def _marked(state: PageState, cfg: dict) -> ProbeResult | None:
    """What the profile's own marks say of a page; ``None`` when it sets neither and leaves it to the other checks.

    A page the server could not serve fails it, ``signed_out_url_pattern``
    fails it, ``signed_in_selector`` decides it, else a sign-in form fails it
    and ``signed_in_url_pattern`` decides it.
    """
    url = state.url
    if state.http_error is not None:
        return _unavailable_probe(url, state.http_error, "the page")
    if _matches(cfg.get("signed_out_url_pattern"), url):
        return _failed(url, "the URL matches the profile's signed_out_url_pattern")
    if cfg.get("signed_in_selector"):
        if state.signed_in:
            return _passed(url, "the profile's signed_in_selector is on the page")
        return _failed(url, f"the profile's signed_in_selector ({_shown_selector(cfg)}) is not on the page")
    if state.sign_in_shaped:
        return _failed(url, "a sign-in form (a password or one-time-code field)")
    if cfg.get("signed_in_url_pattern"):
        if _matches(cfg.get("signed_in_url_pattern"), url):
            return _passed(url, "the URL matches the profile's signed_in_url_pattern")
        return _failed(url, "the URL does not match the profile's signed_in_url_pattern")
    return None


def _shown_selector(cfg: dict) -> str:
    """The profile's ``signed_in_selector`` as a message names it (cut at 120 characters)."""
    selector = str(cfg.get("signed_in_selector") or "")
    return selector if len(selector) <= 120 else selector[:117] + "..."


def _selector_missing(state: PageState, cfg: dict) -> bool:
    """Whether the only mark against *state* is the profile's ``signed_in_selector`` absent from it."""
    return bool(
        cfg.get("signed_in_selector")
        and not state.signed_in
        and state.readable
        and state.http_error is None
        and not _matches(cfg.get("signed_out_url_pattern"), state.url)
    )


async def _past_stale_selector(
    rb: ProfileBrowser, snap: ProfileSnapshot, state: PageState, *, deadline: float | None = None
) -> ProbeResult:
    """The verdict on a page that lacks the profile's ``signed_in_selector``: the structural judgement decides.

    A selector set for an older layout of the application must not fail a page that renders it signed in (a
    session restored from ``localStorage`` included): when the structural checks pass, the probe passes and
    says the selector is stale; when they fail, the failure names the selector and their reason.
    """
    cfg = snap.login_config
    url = state.url
    shown = _shown_selector(cfg)
    unmarked = {k: v for k, v in cfg.items() if k != "signed_in_selector"}
    fallback = await _judge_structure(rb, snap, state, unmarked, deadline=deadline)
    if fallback.http_status is not None:
        return fallback
    if fallback.authenticated:
        return _passed(
            url,
            f"{fallback.reason}; the profile's signed_in_selector ({shown}) is not on it: stale, update or clear it",
        )
    return _failed(url, f"the profile's signed_in_selector ({shown}) is not on the page; {fallback.reason}")


async def _judge(
    rb: ProfileBrowser, snap: ProfileSnapshot, page: Any, state: PageState, *, deadline: float | None = None
) -> ProbeResult:
    """Whether the rested page *state* is the signed-in application (*deadline*: the probe's retry budget)."""
    cfg = snap.login_config
    url = state.url
    if not url or url.startswith("about:"):
        return _failed(url, "start_url did not open")
    if not state.readable:
        return _failed(url, "the page was still loading when the check timed out")
    if state.http_error is not None:
        return _unavailable_probe(url, state.http_error, "start_url")
    if cfg.get("signed_in_selector") and not _matches(cfg.get("signed_out_url_pattern"), url):
        # Its own wait, apart from the probe's retry *deadline* the signed-out render below receives.
        selector_deadline = time.monotonic() + 8.0
        while not state.signed_in and time.monotonic() < selector_deadline:
            await asyncio.sleep(0.5)
            state = await classify(page, snap)
    if _selector_missing(state, cfg):
        return await _past_stale_selector(rb, snap, state, deadline=deadline)
    return await _judge_structure(rb, snap, state, cfg, deadline=deadline)


async def _judge_structure(
    rb: ProfileBrowser, snap: ProfileSnapshot, state: PageState, cfg: dict, *, deadline: float | None = None
) -> ProbeResult:
    """The marks of *cfg* on a readable, served page *state*, then its structure: the application's host, not
    its sign-in page, not what ``start_url`` renders without a session."""
    url = state.url
    if (verdict := _marked(state, cfg)) is not None:
        return verdict
    if _host(url) != snap.app_host:
        return _failed(
            url,
            f"another site answered ({_host(url)}), not the application: an identity provider's sign-in "
            "(set signed_in_url_pattern when the application lives on another host than start_url)",
        )
    if snap.login_url and _same_page(url, snap.login_url) and not _same_page(snap.login_url, snap.start_url):
        # When login_url is start_url's own page (a single-page application serving its sign-in form and its
        # dashboard at one URL), the URL says nothing: the signed-out render below decides.
        return _failed(url, "the profile's sign-in page (the URL is the profile's login_url)")
    baseline = await signed_out_render(rb, snap, deadline=deadline)
    if baseline is not None and baseline.http_error is not None:
        # Without the signed-out render, the application's own sign-in page cannot be told from it.
        return _unavailable_probe(url, baseline.http_error, "start_url without a session")
    if baseline is not None and _same_page(url, baseline.url) and _same_render(state, baseline):
        return _failed(
            url,
            "the page start_url shows without a session: the application's sign-in page (same URL, title, "
            "fields and amount of content as the signed-out render; set signed_in_selector if it is in fact "
            "the signed-in application)",
        )
    return _passed(url, "start_url renders the application")


async def check_authenticated(
    rb: ProfileBrowser,
    snap: ProfileSnapshot,
    *,
    page: Any = None,
    nav: _Navigation | None = None,
    navigate: bool = True,
) -> ProbeResult:
    """Open ``start_url`` (unless *navigate* is off), follow the redirect chain and tell whether it is signed in.

    Signed in means: the page rests on the application's host (or matches
    ``signed_in_url_pattern``), shows ``signed_in_selector`` when the profile
    sets one, holds no password or one-time-code field, is not the profile's
    sign-in page, and is not what ``start_url`` renders without any session.
    A page the server answered "unavailable" (a 5xx, a 429) is opened again
    after a wait, and fails the check with that status if it stays so; the
    waits of one check (its page and the signed-out render) share
    :data:`_UNAVAILABLE_BUDGET_SECONDS`.
    """
    page = page or rb.page
    own = nav is None
    nav = nav or _Navigation(page)
    deadline = time.monotonic() + _UNAVAILABLE_BUDGET_SECONDS
    try:
        try:
            state = await _open_start(
                rb, snap, page, nav, navigate=navigate, inactivity=snap.sign_in_timeout, deadline=deadline
            )
        except TargetRefusedError as exc:
            return _failed(snap.start_url, str(exc))
        return await _judge(rb, snap, page, state, deadline=deadline)
    finally:
        if own:
            nav.detach()


async def wait_signed_in(rb: ProfileBrowser, snap: ProfileSnapshot, *, page: Any = None) -> ProbeResult:
    """Wait, without navigating, until the page shows the signed-in application; the last probe.

    For a page just opened in a session the pre-flight passed: a single-page
    application may paint its sign-in form while it reads that session, then
    route to the application. Signed in is what the profile marks
    (:func:`_marked`: ``signed_in_selector`` on the page, else the URL
    matching ``signed_in_url_pattern`` with no sign-in form), read as soon as
    it shows; a profile that marks neither gets the pre-flight's own
    judgement of the rested page (:func:`_follow`, :func:`_judge`). After the
    profile's ``sign_in_timeout`` the last probe is returned, failed. A page
    the server could not serve is reloaded after the waits of
    :data:`_UNAVAILABLE_BACKOFF_SECONDS` (within the same timeout).
    """
    page = page or rb.page
    cfg = snap.login_config
    deadline = time.monotonic() + snap.sign_in_timeout
    marks = bool(cfg.get("signed_in_selector") or cfg.get("signed_in_url_pattern"))
    nav = None if marks else _Navigation(page)
    reloads = 0
    try:
        while True:
            left = max(0.5, deadline - time.monotonic())
            if nav is None:
                state = await classify(page, snap)
                verdict = _marked(state, cfg) if state.readable else None
                probe = verdict or _failed(state.url, "the page was still loading")
            else:
                try:
                    state = await asyncio.wait_for(_follow(page, nav, snap, inactivity=left), left)
                except TimeoutError:
                    state = await classify(page, snap)
                probe = await _judge(rb, snap, page, state)
            if probe.authenticated:
                return probe
            if time.monotonic() >= deadline:
                if nav is None and _selector_missing(state, cfg):
                    # The selector never showed: the structure of the rested page decides (a stale selector).
                    return await _past_stale_selector(rb, snap, state)
                return probe
            if probe.http_status is not None and reloads < len(_UNAVAILABLE_BACKOFF_SECONDS):
                await asyncio.sleep(min(_UNAVAILABLE_BACKOFF_SECONDS[reloads], max(0.0, deadline - time.monotonic())))
                reloads += 1
                if nav is not None:
                    nav.bump()
                with contextlib.suppress(Exception):  # read wherever the reload left the page
                    await page.reload(wait_until="domcontentloaded")
                continue
            await asyncio.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    finally:
        if nav is not None:
            nav.detach()


async def probe_in(rb: ProfileBrowser, snap: ProfileSnapshot) -> ProbeResult:
    """The probe of the session *rb* was launched with: ``start_url`` in its own context."""
    return await check_authenticated(rb, snap)


async def probe_state(rb: ProfileBrowser, snap: ProfileSnapshot, state: dict | None) -> ProbeResult:
    """The probe of a saved *state*: ``start_url`` in a new context holding it, as the next recording loads it."""
    async with rb.fresh_page(state) as page:
        return await check_authenticated(rb, snap, page=page)


async def probe_profile(snap: ProfileSnapshot) -> ProbeResult:
    """Launch a browser on the profile's saved session and probe it."""
    if not snap.storage_state:
        return _failed("", "the profile has no saved session")
    async with profile_browser(
        policy=snap.policy,
        viewport=(1440, 900),
        pixel_ratio=1,
        storage_state=snap.storage_state,
        color_scheme=snap.options.get("color_scheme"),
        locale=snap.options.get("locale") or "en-US",
        timezone=snap.options.get("timezone") or "UTC",
    ) as rb:
        return await probe_in(rb, snap)


def session_error_text(probe: ProbeResult, detail: str | None = None) -> str:
    """How a probe that stops a recording or an agent session reads: not authenticated, or not checked at all."""
    state = "session not checked" if probe.http_status is not None else "not authenticated"
    text = f"profile {state}: landed on {probe.url or 'nothing'} ({probe.reason})"
    if detail:
        text += f"; signing in again failed: {detail}"
    return text


class SessionNotSignedInError(RuntimeError):
    """The saved session does not open the application, and the profile could not sign in again by itself."""

    def __init__(self, probe: ProbeResult, detail: str | None = None) -> None:
        self.probe = probe
        self.detail = detail
        super().__init__(session_error_text(probe, detail))


async def ensure_signed_in(
    rb: ProfileBrowser, snap: ProfileSnapshot, *, on_renew: Callable[[], Awaitable[None]] | None = None
) -> ProbeResult | None:
    """Probe the session *rb* holds; when it does not open the application, sign in again without a person.

    ``None`` when the saved session was signed in; the failed probe when the
    profile signed in again (its credentials, a TOTP secret, a saved identity
    provider session). Raises :class:`SessionNotSignedInError` when it could
    not: no way to sign in alone, an MFA prompt waiting for a person, a stage
    that timed out, an application that could not answer (``http_status``).
    *on_renew* runs before the sign-in starts.
    """
    probe = await probe_in(rb, snap)
    if probe.authenticated:
        return None
    if not snap.renewable or probe.http_status is not None:  # an unavailable application is not signed in again
        raise SessionNotSignedInError(probe)
    if on_renew is not None:
        await on_renew()
    try:
        renewed = await drive_sign_in(rb, snap, user_id=None, ask=None, check_first=False)
    except (SignInRequiredError, SignInTimeoutError) as exc:
        raise SessionNotSignedInError(probe, str(exc)) from exc
    if not renewed.ok or renewed.probe is None or not renewed.probe.authenticated:
        raise SessionNotSignedInError(probe, renewed.message)
    return probe


async def save_session(rb: ProfileBrowser, snap: ProfileSnapshot) -> tuple[dict, ProbeResult]:
    """The session to keep - read once the application rendered signed in - and the probe of it."""
    try:
        state = await asyncio.wait_for(
            rb.storage_state(session_storage_of=origin_of(snap.start_url)), snap.session_save_timeout
        )
    except TimeoutError:
        raise SignInTimeoutError(STAGE_SESSION_SAVE, rb.page.url, snap.session_save_timeout) from None
    probe = await probe_state(rb, snap, state)
    if not probe.authenticated and probe.http_status is None:
        # Rendered signed in on this page, not in a context holding the saved state: say what the state kept.
        page_keys = await rb.page_local_storage_keys() if origin_of(rb.page.url) == origin_of(snap.start_url) else []
        probe.reason = f"{probe.reason}; {persistence_note(state, snap.start_url, page_keys=page_keys)}"[:500]
    return state, probe


# ── Driving the sign-in ───────────────────────────────────────────────────


class _Flow:
    """One sign-in's page, its navigation clock, its stage and its bounds."""

    def __init__(self, rb: ProfileBrowser, snap: ProfileSnapshot) -> None:
        self.rb = rb
        self.snap = snap
        self.page = rb.page
        self.nav = _Navigation(rb.page)
        self.stage = STAGE_IDP_FORM
        self.started = time.monotonic()
        #: The flow as a whole, a person's wait excluded: a page that keeps moving still ends.
        self.budget = max(600.0, snap.sign_in_timeout * 5)
        self._waiting_since: float | None = None
        self.waiting_until = 0.0
        #: The decisions taken, newest last (bounded).
        self.trace: list[TraceStep] = []

    def record(self, decision: str, *, url: str | None = None) -> None:
        """Note a decision in the trace and the log, so an operator sees what the sign-in did and where it stalled."""
        step = TraceStep(stage=self.stage, url=url or str(getattr(self.page, "url", "") or ""), decision=decision)
        self.trace.append(step)
        del self.trace[:-60]
        logger.info(
            "browser-profile %s sign-in: stage=%s decision=%s url=%s",
            self.snap.id,
            step.stage,
            decision,
            step.url,
        )

    def trace_dicts(self) -> list[dict]:
        return [step.to_dict() for step in self.trace]

    def wait_for_person(self, seconds: float) -> None:
        self._waiting_since = time.monotonic()
        self.waiting_until = self._waiting_since + seconds

    def done_waiting(self) -> None:
        if self._waiting_since is not None:
            self.budget += time.monotonic() - self._waiting_since
        self._waiting_since = None
        self.waiting_until = 0.0
        self.nav.bump()

    def check(self) -> None:
        now = time.monotonic()
        if self._waiting_since is not None:
            if now > self.waiting_until:
                raise SignInTimeoutError(self.stage, self.page.url, self.waiting_until - self._waiting_since)
            return
        if self.nav.idle > self.snap.sign_in_timeout:
            raise SignInTimeoutError(self.stage, self.page.url, self.snap.sign_in_timeout)
        if now - self.started > self.budget:
            raise SignInTimeoutError(self.stage, self.page.url, now - self.started)

    async def after_action(self, before: PageState) -> None:
        """Wait for the page to answer what the sign-in did: a navigation, or a new view of it."""
        count = self.nav.count
        end = time.monotonic() + min(_ACTION_WAIT_SECONDS, self.snap.sign_in_timeout)
        while time.monotonic() < end:
            await asyncio.sleep(_POLL_SECONDS)
            if self.nav.count != count:
                break
            state = await classify(self.page, self.snap)
            if state.readable and state.view != before.view:
                self.nav.bump()
                break
        await self.nav.quiet(0.6, limit=5.0)

    async def marker(self) -> tuple:
        return (self.nav.count, (await classify(self.page, self.snap)).view)


async def _fill(page: Any, selector: str, value: str) -> None:
    field_ = page.locator(selector).first
    await field_.click(timeout=10_000)
    await field_.fill("")
    await field_.press_sequentially(value, delay=35)


async def _click(page: Any, selector: str) -> bool:
    try:
        await page.locator(selector).first.click(timeout=5_000)
        return True
    except Exception as exc:  # the page moved under the click
        logger.debug("Sign-in click on %s missed: %s", selector, format_exception_message(exc))
        return False


async def _tick(page: Any, state: PageState) -> None:
    """Tick the page's unticked checkboxes ("stay signed in", "don't ask again on this device")."""
    if state.password:
        return  # never next to a password: one of them may be "show the password"
    for index in range(state.ticks):
        await _click(page, f'[data-xtm-signin="tick-{index}"]')


async def _submit(page: Any, field_selector: str, snap: ProfileSnapshot, *, otp: bool = False) -> None:
    configured = snap.login_config.get("otp_submit_selector" if otp else "submit_selector")
    if configured and await _click(page, configured):
        return
    with contextlib.suppress(Exception):  # a form that posted itself on the last character
        await page.locator(field_selector).first.press("Enter")


async def _type_code(page: Any, state: PageState, code: str, snap: ProfileSnapshot) -> None:
    if state.otp_boxes:
        for index, char in enumerate(code[: state.otp_boxes]):
            with contextlib.suppress(Exception):  # a box that moved on to the next by itself
                await page.locator(f'[data-xtm-signin="otp-box-{index}"]').first.fill(char)
        await _submit(page, f'[data-xtm-signin="otp-box-{state.otp_boxes - 1}"]', snap, otp=True)
        return
    await _fill(page, state.otp or "", code)
    await _submit(page, state.otp or "", snap, otp=True)


async def _wait_for_step(secret: str, counter: int) -> None:
    """Wait until TOTP time step *counter* begins, so its code is the one the server expects."""
    await asyncio.sleep(totp.seconds_until(secret, counter) + 0.5)


async def _visible(page: Any, selector: str) -> bool:
    try:
        return await asyncio.wait_for(page.locator(selector).first.is_visible(), _READ_TIMEOUT_SECONDS)
    except Exception:
        return False


def _fido_bridge(state: PageState) -> tuple[bool, str | None]:
    """Whether *state* is a WebAuthn / passkey bridge, and the ``cancelUrl`` to back out to (``""`` = none given).

    A bridge is a fieldless page whose path says so (an identity provider's
    ``/bridge/fido``), or any fieldless page that tried ``navigator.credentials.get``
    (the browser blocked it and counted the attempt). It is never a push: the
    method cannot run here and the sign-in must choose another.
    """
    if state.password or state.otp or state.otp_boxes or state.text_inputs or state.relay:
        return False, None
    try:
        parsed = urlparse(state.url)
    except ValueError:
        return False, None
    path = (parsed.path or "").lower()
    by_path = any(marker in path for marker in _FIDO_BRIDGE_PATHS)
    if not by_path and state.webauthn_attempts <= 0:
        return False, None
    # ``parse_qs`` already percent-decodes the value, so the cancelUrl comes out ready to navigate to.
    params = parse_qs(parsed.query)
    cancel = params.get("cancelUrl") or params.get("cancelurl")
    return True, (cancel[-1] if cancel else "")


async def _back_out_of_fido(flow: _Flow, state: PageState, cancel_url: str | None) -> bool:
    """Leave a FIDO bridge: go to the decoded ``cancelUrl`` (no DOM needed), else click a "sign in another way" control."""
    if cancel_url:
        with contextlib.suppress(Exception):  # the policy may refuse it; the DOM controls are the fallback
            await flow.rb.goto(cancel_url)
            flow.record("fido_bridge_bypassed", url=cancel_url)
            flow.nav.bump()
            return True
    for selector in _OTHER_WAYS_SELECTORS:
        if await _visible(flow.page, selector) and await _click(flow.page, selector):
            flow.record(f"fido_bridge_bypassed:{selector}", url=state.url)
            flow.nav.bump()
            return True
    return False


_PICKER_METHOD_JS = r"""
(values) => {
  for (const value of values) {
    const el = document.querySelector(`[data-value="${value}"]`);
    if (!el) continue;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none') return value;
  }
  return null;
}
"""


async def _present_picker_method(page: Any, values: tuple[str, ...]) -> str | None:
    """The first of *values* the credential picker shows (by ``data-value``), or ``None``."""
    try:
        return await asyncio.wait_for(page.evaluate(_PICKER_METHOD_JS, list(values)), _READ_TIMEOUT_SECONDS)
    except Exception:
        return None


_PICKER_FIRST_CHOICE_JS = r"""
(args) => {
  const shown = (el) => {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  for (const el of document.querySelectorAll('[data-xtm-signin="picker-password"]')) el.removeAttribute('data-xtm-signin');
  const mark = (el) => { el.setAttribute('data-xtm-signin', 'picker-password'); return true; };
  for (const value of args.values) {
    const el = document.querySelector(`[data-value="${value}"]`);
    if (shown(el)) return mark(el);
  }
  for (const sel of args.selectors) {
    let el = null;
    try { el = document.querySelector(sel); } catch (e) { continue; }
    if (shown(el)) return mark(el);
  }
  const label = new RegExp(args.label, 'i');
  const rows = document.querySelectorAll('a, button, [role="button"], [role="link"], [data-value], [tabindex]');
  for (const el of rows) {
    if (shown(el) && label.test((el.innerText || '').trim())) return mark(el);
  }
  return false;
}
"""


async def _choose_password_method(flow: _Flow) -> bool:
    """On a sign-in method picker, click its password row ("Use my password"); never a passkey / FIDO row."""
    args = {
        "values": list(_PICKER_FIRST_CHOICE_VALUES),
        "selectors": list(_PICKER_FIRST_CHOICE_SELECTORS),
        "label": _PICKER_FIRST_CHOICE_LABEL,
    }
    try:
        found = await asyncio.wait_for(flow.page.evaluate(_PICKER_FIRST_CHOICE_JS, args), _READ_TIMEOUT_SECONDS)
    except Exception:
        return False
    if not found or not await _click(flow.page, '[data-xtm-signin="picker-password"]'):
        return False
    flow.record("picker_choice=password")
    flow.nav.bump()
    return True


async def _choose_picker_method(flow: _Flow) -> str | None:
    """On a credential picker, click a code method (authenticator code, then SMS), else a push; the value chosen."""
    page = flow.page
    for group in (_PICKER_CODE_VALUES, _PICKER_PUSH_VALUES):
        value = await _present_picker_method(page, group)
        if value and await _click(page, f'[data-value="{value}"]'):
            flow.record(f"picker_choice={value}")
            flow.nav.bump()
            return value
    return None


async def _accept_kmsi(flow: _Flow) -> bool:
    """A "Stay signed in?" interstitial: tick "don't ask again for 30 days", then confirm (never click "No")."""
    if not await _visible(flow.page, _KMSI_CONFIRM_SELECTOR):
        return False
    if await _visible(flow.page, _KMSI_DONT_ASK_SELECTOR):
        await _click(flow.page, _KMSI_DONT_ASK_SELECTOR)
    if await _click(flow.page, _KMSI_CONFIRM_SELECTOR):
        flow.record("kmsi_accepted")
        flow.nav.bump()
        return True
    return False


async def _skip_proof_up(flow: _Flow) -> bool:
    """A "More information required" / proof-up page with a skip control: skip it (the account keeps its method)."""
    for selector in _PROOF_UP_SKIP_SELECTORS:
        if await _visible(flow.page, selector) and await _click(flow.page, selector):
            flow.record(f"proofup_skipped:{selector}")
            flow.nav.bump()
            return True
    return False


_FIELD_READ_JS = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
  return { empty: !el.value, email: (el.type || '').toLowerCase() === 'email' || /\\bemail\\b/.test(auto)
    || (el.getAttribute('inputmode') || '').toLowerCase() === 'email' };
}
"""


async def _left_empty(page: Any, state: PageState, creds: dict) -> str:
    """Which field of the password form the submit left empty (``email field left empty``); ``""`` when none is
    known to be, so a form that comes back refusing the sign-in says what it was missing."""
    if not state.username:
        return (
            "no username or email field was recognised on the form: set username_selector"
            if creds.get("username")
            else ""
        )
    read = None
    with contextlib.suppress(Exception):  # the page moved: nothing known
        read = await asyncio.wait_for(page.evaluate(_FIELD_READ_JS, state.username), _READ_TIMEOUT_SECONDS)
    if isinstance(read, dict) and read.get("empty"):
        return f"{'email' if read.get('email') else 'username'} field left empty"
    return ""


async def _password_errored(page: Any, state: PageState) -> bool:
    """Whether the password form shows its error banner: the credentials were refused."""
    if await _visible(page, _PASSWORD_ERROR_SELECTOR):
        return True
    return bool(state.alert)


async def drive_sign_in(
    rb: ProfileBrowser,
    snap: ProfileSnapshot,
    *,
    user_id: object,
    ask: Callable[[MfaPrompt], Awaitable[str | None]] | None,
    announce_approval: Callable[[MfaPrompt], Awaitable[None]] | None = None,
    check_first: bool = True,
) -> SignInResult:
    """Sign the page in until ``start_url`` renders the application.

    *ask* answers a code prompt (``None`` = no person: :class:`SignInRequiredError`),
    *announce_approval* tells the person to approve a push. With *check_first*
    the session the browser holds is probed first. Raises
    :class:`SignInTimeoutError` when a stage does not move in time.
    """
    flow = _Flow(rb, snap)
    try:
        if check_first:
            probe = await check_authenticated(rb, snap, nav=flow.nav)
            if probe.authenticated:
                return SignInResult(
                    True, "Already signed in with the saved session: start_url renders the application.", probe
                )
        if snap.login_url:
            flow.nav.bump()
            await rb.goto(snap.login_url)
        elif not check_first:
            flow.nav.bump()
            await rb.goto(snap.start_url)
        result = await _drive(flow, user_id=user_id, ask=ask, announce_approval=announce_approval)
        result.trace = flow.trace_dicts()
        result.driven = True
        return result
    except (SignInRequiredError, SignInTimeoutError) as exc:
        exc.trace = flow.trace_dicts()
        raise
    finally:
        flow.nav.detach()


async def _drive(
    flow: _Flow,
    *,
    user_id: object,
    ask: Callable[[MfaPrompt], Awaitable[str | None]] | None,
    announce_approval: Callable[[MfaPrompt], Awaitable[None]] | None,
) -> SignInResult:
    snap, page = flow.snap, flow.page
    creds = snap.credentials
    secret = creds.get("totp_secret")
    done: dict[str, int] = {}  # "<what>@<page>" -> how often the sign-in did it there
    submitted = left_app = False
    otp_attempts = 0
    fido_attempts = 0
    totp_counter: int | None = None
    announced: tuple | None = None  # the view of the approval prompt the person was told about
    last_check: tuple | None = None
    last_refusal: ProbeResult | None = None
    last_view: tuple | None = None
    still_since = time.monotonic()

    def fail(message: str, stage: str, url: str) -> SignInResult:
        return SignInResult(False, message, stage=stage, last_url=url)

    while True:
        flow.check()
        await flow.nav.quiet(0.6, limit=3.0)
        state = await classify(page, snap)
        if not state.readable:
            await asyncio.sleep(_POLL_SECONDS)
            continue
        if state.view != last_view:
            if last_view is not None:
                flow.nav.bump()
            last_view = state.view
            still_since = time.monotonic()
        key = _page_id(state.url)
        host = _host(state.url)
        on_app = host == snap.app_host
        left_app = left_app or (bool(host) and not on_app)
        if announced is not None and state.view != announced:
            announced = None
            flow.done_waiting()

        # Back on the application: does start_url render it signed in?
        if on_app and not state.sign_in_shaped and not state.relay:
            if submitted or left_app:
                flow.stage = STAGE_APP_LOAD
            if (flow.nav.count, state.view) != last_check:
                rested = await _follow(page, flow.nav, snap, inactivity=snap.sign_in_timeout)
                if _host(rested.url) == snap.app_host and not rested.sign_in_shaped and not rested.relay:
                    probe = await check_authenticated(
                        flow.rb, snap, nav=flow.nav, navigate=not _same_page(rested.url, snap.start_url)
                    )
                    if probe.authenticated:
                        message = "Signed in" if (submitted or left_app) else "Already signed in"
                        flow.record("signed_in", url=probe.url)
                        return SignInResult(
                            True, f"{message}: start_url renders the application.", probe, last_url=probe.url
                        )
                    last_refusal = probe
                last_check = await flow.marker()
                continue

        # A WebAuthn / passkey bridge: an unusable method. Back out to a code method, never wait on it as a push.
        is_bridge, cancel_url = _fido_bridge(state)
        if is_bridge:
            flow.stage = STAGE_MFA
            if fido_attempts == 0:
                flow.record("fido_bridge_detected", url=state.url)
            if fido_attempts < 3 and await _back_out_of_fido(flow, state, cancel_url):
                fido_attempts += 1
                await flow.after_action(state)
                continue
            fido_attempts += 1
            if fido_attempts > 3:
                return fail(
                    "The account's sign-in offers only a passkey / FIDO security key, which cannot run in the "
                    "platform's headless browser. Add a TOTP secret to the profile, or enable an "
                    "authenticator-code or SMS method on the account.",
                    STAGE_MFA,
                    state.url,
                )
            await asyncio.sleep(_POLL_SECONDS)
            continue

        # A sign-in method picker (Entra "Choose a way to sign in": a passkey row and "Use my password"): the
        # password, when the profile holds one, before any passkey or push row.
        if (
            not on_app
            and not state.text_inputs
            and not state.relay
            and creds.get("password")
            and done.get(f"picker-password@{key}", 0) < 2
            and await _choose_password_method(flow)
        ):
            done[f"picker-password@{key}"] = done.get(f"picker-password@{key}", 0) + 1
            submitted = True
            flow.stage = STAGE_IDP_FORM
            await flow.after_action(state)
            continue

        # A credential picker ("Verify your identity" / "Choose a way to sign in"): choose a code method.
        if not on_app and not state.sign_in_shaped and not state.relay and done.get(f"picker@{key}", 0) < 2:
            method = await _choose_picker_method(flow)
            if method is not None:
                done[f"picker@{key}"] = done.get(f"picker@{key}", 0) + 1
                submitted = True
                flow.stage = STAGE_MFA if method in _PICKER_PUSH_VALUES else STAGE_REDIRECT_CHAIN
                await flow.after_action(state)
                continue

        # A "Stay signed in?" interstitial: tick "don't ask again for 30 days", then confirm (never "No").
        if not on_app and not state.sign_in_shaped and not done.get(f"kmsi@{key}") and await _accept_kmsi(flow):
            done[f"kmsi@{key}"] = 1
            flow.stage = STAGE_REDIRECT_CHAIN
            await flow.after_action(state)
            continue

        # A "More information required" / proof-up page with a skip control.
        if not on_app and not state.sign_in_shaped and not done.get(f"skip@{key}") and await _skip_proof_up(flow):
            done[f"skip@{key}"] = 1
            await flow.after_action(state)
            continue

        # The password or username form came back showing an error banner: the credentials were refused.
        if state.password and done.get(f"password@{key}") and await _password_errored(page, state):
            flow.record("credentials_refused", url=state.url)
            return fail(
                f"The sign-in page answered: {state.alert or 'your account or password is incorrect'}",
                STAGE_IDP_FORM,
                state.url,
            )
        if state.username and not state.password and state.alert and done.get(f"username@{key}"):
            flow.record("username_rejected", url=state.url)
            return fail(f"The sign-in page answered: {state.alert}", STAGE_IDP_FORM, state.url)

        # A one-time code (``input[name=otc]`` and its kin).
        if state.otp_boxes or (state.otp and (state.otp_strong or submitted)):
            flow.stage = STAGE_MFA
            await _tick(page, state)
            if secret and flow.snap.has_valid_totp:
                if otp_attempts >= 2:
                    flow.record("totp_refused", url=state.url)
                    return fail(
                        "The authenticator code was refused on two periods in a row: check the profile's TOTP "
                        "secret (and the clock of the application's server).",
                        STAGE_MFA_TOTP_REFUSED,
                        state.url,
                    )
                if totp_counter is None:
                    totp_counter = totp.current_counter(secret)
                else:
                    totp_counter += 1
                    await _wait_for_step(secret, totp_counter)
                    flow.nav.bump()
                code = totp.code_for_counter(secret, totp_counter)
                flow.record(f"totp_submitted:attempt-{otp_attempts + 1}", url=state.url)
            elif ask is not None:
                if otp_attempts >= 3:
                    return fail("The one-time code was refused three times.", STAGE_MFA, state.url)
                flow.record(f"otc_challenge:request-{otp_attempts + 1}", url=state.url)
                prompt = MfaPrompt(
                    kind=KIND_CODE,
                    url=state.url,
                    screenshot_file_id=await _screenshot(page, user_id),
                    note=state.alert if otp_attempts else None,
                )
                flow.wait_for_person(snap.mfa_timeout + 5)
                code = await ask(prompt)
                flow.done_waiting()
                if not code:
                    raise SignInTimeoutError(STAGE_MFA, state.url, snap.mfa_timeout)
                flow.record("code_submitted", url=state.url)
            else:
                raise SignInRequiredError(
                    "The sign-in asks for a one-time code and the profile has no usable TOTP secret: sign the "
                    "profile in (sign_in_browser_profile) and give the code."
                )
            await _type_code(page, state, code, snap)
            otp_attempts += 1
            submitted = True
            flow.stage = STAGE_REDIRECT_CHAIN
            flow.nav.bump()
            await flow.after_action(state)
            continue

        # A password (a one-step form, or the second step of a two-step one).
        if state.password:
            if done.get(f"password@{key}", 0) >= 2:
                left = await _left_empty(page, state, creds)
                return fail(
                    "The sign-in kept returning to the password form without an error banner; it did not progress"
                    + (f" ({left})." if left else "."),
                    STAGE_IDP_FORM,
                    state.url,
                )
            if not creds.get("password"):
                return fail("The sign-in page asks for a password and the profile has none.", STAGE_IDP_FORM, state.url)
            flow.stage = STAGE_IDP_FORM
            if state.username and creds.get("username") and not done.get(f"username@{key}"):
                await _fill(page, state.username, creds["username"])
                done[f"username@{key}"] = 1
            await _fill(page, state.password, creds["password"])
            done[f"password@{key}"] = done.get(f"password@{key}", 0) + 1
            await _submit(page, state.password, snap)
            flow.record("password_submitted", url=state.url)
            submitted = True
            flow.nav.bump()
            await flow.after_action(state)
            continue

        # A username alone (the first step of a two-step form).
        if state.username:
            if done.get(f"username@{key}", 0) >= 2:
                return fail("The sign-in page asked for the username again.", STAGE_IDP_FORM, state.url)
            if not creds.get("username"):
                return fail("The sign-in page asks for a username and the profile has none.", STAGE_IDP_FORM, state.url)
            flow.stage = STAGE_IDP_FORM
            await _fill(page, state.username, creds["username"])
            done[f"username@{key}"] = done.get(f"username@{key}", 0) + 1
            await _submit(page, state.username, snap)
            flow.record("username_submitted", url=state.url)
            submitted = True
            flow.nav.bump()
            await flow.after_action(state)
            continue

        # The way back: a relay form posting to the application, a meta refresh.
        if state.relay:
            flow.stage = STAGE_REDIRECT_CHAIN
            relay_key = f"relay@{key}"
            if flow.nav.idle > 2.5 and not flow.nav.in_flight and not done.get(relay_key):
                done[relay_key] = 1
                with contextlib.suppress(Exception):  # it posted itself meanwhile
                    await page.evaluate(_POST_RELAY_JS)
                flow.nav.bump()
            await asyncio.sleep(_POLL_SECONDS)
            continue
        if state.meta_refresh is not None and state.meta_refresh <= 30 and flow.nav.idle < state.meta_refresh + 2:
            flow.stage = STAGE_REDIRECT_CHAIN
            await asyncio.sleep(_POLL_SECONDS)
            continue

        # The profile's own button (an application's "sign in with SSO", a consent prompt).
        confirm = snap.login_config.get("confirm_selector")
        if confirm and done.get(f"confirm@{key}", 0) < 2 and await _visible(page, confirm):
            done[f"confirm@{key}"] = done.get(f"confirm@{key}", 0) + 1
            await _click(page, confirm)
            flow.nav.bump()
            await flow.after_action(state)
            continue

        # The identity provider's pages without fields: an interstitial, a push approval.
        if not on_app and not state.text_inputs and (submitted or left_app):
            if state.ticks and not done.get(f"tick@{key}"):
                done[f"tick@{key}"] = 1
                await _tick(page, state)
                continue
            if state.number is None and state.accept and done.get(f"accept@{key}", 0) < 2:
                done[f"accept@{key}"] = done.get(f"accept@{key}", 0) + 1
                flow.stage = STAGE_REDIRECT_CHAIN
                await _click(page, '[data-xtm-signin="accept"]')
                flow.nav.bump()
                await flow.after_action(state)
                continue
            stalled = submitted and time.monotonic() - still_since >= _APPROVAL_STALL_SECONDS
            if stalled and state.number is None and not state.approval:
                # Nothing was sent to a phone: never announced as a push. A method picker offering nothing the
                # platform can use fails naming the page; any other page waits for the redirect chain's own bound.
                if state.picker:
                    flow.record("unusable_method_picker", url=state.url)
                    return fail(
                        f"The sign-in stopped on a sign-in method picker offering no method the platform can use "
                        f"({state.title or 'untitled'}): enable a password, authenticator-code or SMS method on the "
                        "account, or set the profile's confirm_selector to the control that goes on.",
                        STAGE_MFA,
                        state.url,
                    )
                if not done.get(f"unknown@{key}"):
                    done[f"unknown@{key}"] = 1
                    flow.record("fieldless_page_not_an_approval", url=state.url)
                await asyncio.sleep(_POLL_SECONDS)
                continue
            if state.number is not None or stalled:
                flow.stage = STAGE_MFA
                if announced is None:
                    if announce_approval is None:
                        raise SignInRequiredError(
                            "The sign-in waits for an approval on a phone: sign the profile in "
                            "(sign_in_browser_profile) and approve it."
                        )
                    announced = state.view
                    flow.record("push_prompt" if state.number is None else f"push_number_prompt:{state.number}")
                    prompt = MfaPrompt(
                        kind=KIND_PUSH_NUMBER if state.number is not None else KIND_PUSH,
                        url=state.url,
                        screenshot_file_id=await _screenshot(page, user_id),
                        number=state.number,
                    )
                    flow.wait_for_person(snap.mfa_timeout)
                    await announce_approval(prompt)
                await asyncio.sleep(_POLL_SECONDS)
                continue

        # Back on the application, signed out, with nothing left to do there.
        if on_app and last_refusal is not None:
            patience = 8.0 if (submitted or left_app) else 2.0
            if time.monotonic() - still_since >= patience:
                if last_refusal.http_status is not None:
                    message = (
                        f"The application could not answer the browser back from the sign-in ({last_refusal.reason})."
                    )
                elif submitted or left_app:
                    held = None
                    with contextlib.suppress(Exception):  # a closing context says nothing of what it held
                        held = await asyncio.wait_for(
                            flow.rb.storage_state(session_storage_of=origin_of(snap.start_url)), _READ_TIMEOUT_SECONDS
                        )
                    message = (
                        "The browser came back to the application, which does not render signed in "
                        f"({last_refusal.reason}; {persistence_note(held, snap.start_url)})."
                    )
                else:
                    message = (
                        f"The application's page offers nothing to sign in with ({last_refusal.reason}): set "
                        "login_url to the page that starts the sign-in, or confirm_selector to the button that does."
                    )
                return fail(message, STAGE_APP_LOAD, state.url)
        await asyncio.sleep(_POLL_SECONDS)


async def _store_screenshot(data: bytes, user_id: object) -> str:
    """Store an MFA prompt's JPEG as a file of *user_id* (the person signing in); its id."""
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import file_storage

    owner = safe_uuid(str(user_id)) if user_id else None
    filename = "sign-in-prompt.jpg"
    object_name = await file_storage.upload(data, filename, "image/jpeg", prefix=f"browser-profiles/{owner or 'shared'}")
    row = FileAttachment(
        user_id=owner,
        filename=filename,
        content_type="image/jpeg",
        size=len(data),
        s3_object_name=object_name,
        sha256=hashlib.sha256(data).hexdigest(),
        source=SOURCE_MFA_SCREENSHOT,
    )
    async with async_session() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return str(row.id)


async def _screenshot(page: Any, user_id: object) -> str | None:
    """The MFA prompt as a file of *user_id*, its id; ``None`` when it failed."""
    try:
        data = await asyncio.wait_for(page.screenshot(type="jpeg", quality=85), _READ_TIMEOUT_SECONDS * 2)
        return await _store_screenshot(data, user_id)
    except Exception as exc:
        logger.debug("Sign-in screenshot failed: %s", format_exception_message(exc))
        return None


# ── The challenge channel (Redis, else this process) ──────────────────────

_local_states: dict[str, dict] = {}
_local_codes: dict[str, asyncio.Queue[str]] = {}
_running: dict[str, asyncio.Task] = {}
#: The login settings each running sign-in started from (its snapshot), by challenge id: a challenge opened on
#: settings changed since is restarted rather than answered as it stands.
_started_config: dict[str, str] = {}


def _config_digest(login_config: dict[str, Any] | None) -> str:
    return json.dumps(login_config or {}, sort_keys=True, default=str)


# The key names are read by every replica: renamed, a challenge opened by an
# older one never receives its code during a rolling upgrade.
def _state_key(cid: str) -> str:
    return f"video_studio:sign_in:{cid}:state"


def _codes_key(cid: str) -> str:
    return f"video_studio:sign_in:{cid}:codes"


async def _redis() -> Any | None:
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.ping()
        return client
    except Exception:
        return None


async def publish(cid: str, outcome: SignInOutcome) -> None:
    payload = json.dumps(outcome.to_dict())
    _local_states[cid] = outcome.to_dict()
    client = await _redis()
    if client is not None:
        try:
            await client.set(_state_key(cid), payload, ex=_STATE_TTL_SECONDS)
        except Exception as exc:
            logger.debug("Sign-in state not published: %s", format_exception_message(exc))


async def read_state(cid: str) -> SignInOutcome | None:
    client = await _redis()
    raw = None
    if client is not None:
        try:
            raw = await client.get(_state_key(cid))
        except Exception:
            raw = None
    data = json.loads(raw) if raw else _local_states.get(cid)
    if not data:
        return None
    return SignInOutcome(**{k: data.get(k) for k in SignInOutcome.__dataclass_fields__})


async def send_code(cid: str, code: str) -> None:
    client = await _redis()
    if client is not None:
        try:
            await client.rpush(_codes_key(cid), code)
            await client.expire(_codes_key(cid), 120)
            return
        except Exception as exc:  # this process then
            logger.debug("MFA code not pushed to Redis: %s", format_exception_message(exc))
    queue = _local_codes.setdefault(cid, asyncio.Queue())
    await queue.put(code)


async def _next_code(cid: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client = await _redis()
        if client is not None:
            try:
                item = await client.lpop(_codes_key(cid))
                if item:
                    return item.decode() if isinstance(item, bytes) else str(item)
            except Exception as exc:  # read this process's queue then
                logger.debug("MFA code not read from Redis: %s", format_exception_message(exc))
        queue = _local_codes.get(cid)
        if queue is not None and not queue.empty():
            return queue.get_nowait()
        await asyncio.sleep(0.75)
    return None


async def _wait_for_change(
    cid: str, *, not_status: set[str], timeout: float, since: str | None = None
) -> SignInOutcome | None:
    deadline = time.monotonic() + timeout
    state = await read_state(cid)
    while time.monotonic() < deadline:
        state = await read_state(cid)
        if state is not None and state.status not in not_status and (since is None or state.message != since):
            return state
        await asyncio.sleep(0.5)
    return state


# ── Interactive sign-in ───────────────────────────────────────────────────


async def _update_profile(profile_id: str, **changes: Any) -> None:
    async with async_session() as db:
        profile = (
            await db.execute(select(BrowserProfile).where(BrowserProfile.id == uuid.UUID(profile_id)))
        ).scalar_one_or_none()
        if profile is None:
            return
        state = changes.pop("storage_state", None)
        probe = changes.pop("probe", None)
        message = changes.pop("message", None)
        if state is not None:
            save_storage_state(profile, state, probe=probe, message=message)
        else:
            if probe is not None:
                record_probe(profile, probe)
            if message is not None:
                profile.last_sign_in_message = message[:1000]
        for key, value in changes.items():
            setattr(profile, key, value)
        await db.commit()


async def _learn_unattended(profile_id: str, unattended: bool) -> None:
    """Keep whether the stored credentials signed in without a person; nothing else of the profile moves.

    A dry run writes it too: it says how the sign-in goes, never what the
    saved session or its status are. A failure to write it is logged, never raised.
    """
    try:
        await _update_profile(profile_id, signs_in_unattended=unattended)
    except Exception as exc:
        logger.debug(
            "Browser profile %s: unattended sign-in not recorded: %s", profile_id, format_exception_message(exc)
        )


async def store_probe(profile_id: str, probe: ProbeResult, *, message: str | None = None) -> None:
    """Keep a probe of the profile's saved session: its status follows it."""
    await _update_profile(profile_id, probe=probe.to_dict(), message=message)


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) < datetime.now(UTC)
    except ValueError:
        return True


def _approval_message(prompt: MfaPrompt) -> str:
    if prompt.kind == KIND_PUSH_NUMBER:
        return (
            f"The sign-in waits for an approval on the phone: open the authenticator app and enter {prompt.number} "
            "(the attached screenshot shows the prompt). The sign-in goes on by itself once it is approved."
        )
    return (
        "The sign-in waits for an approval on the phone (the attached screenshot shows the prompt). "
        "It goes on by itself once it is approved."
    )


def _saved_session_status(probe: ProbeResult) -> str:
    """How a sign-in that reached the application ends, from the probe of the session it saved.

    ``active`` when that session opens the application, ``failed`` when it does
    not, ``unverified`` when the application did not answer the probe
    (``http_status``): that says nothing of the session, never a failure.
    """
    if probe.authenticated:
        return STATUS_ACTIVE
    return STATUS_UNVERIFIED if probe.http_status is not None else STATUS_FAILED


async def _run_sign_in(cid: str, snap: ProfileSnapshot, user_id: object) -> None:
    asked = 0
    approval_asked = False

    async def ask(prompt: MfaPrompt) -> str | None:
        nonlocal asked
        asked += 1
        expires = datetime.now(UTC) + timedelta(seconds=snap.mfa_timeout)
        retry = ""
        if asked > 1:
            retry = " The previous code was refused" + (f" ({prompt.note})." if prompt.note else ".")
        outcome = SignInOutcome(
            status=STATUS_MFA_REQUIRED,
            kind=KIND_CODE,
            message=(
                f"The sign-in asks for a one-time code (request {asked}).{retry} Give the current code from the "
                f"authenticator app: the platform's browser waits on the prompt until {expires:%H:%M:%S} UTC."
            ),
            challenge_id=cid,
            screenshot_file_id=prompt.screenshot_file_id,
            expires_at=expires.isoformat(),
            profile_id=snap.id,
            stage=STAGE_MFA,
            last_url=prompt.url,
        )
        await publish(cid, outcome)
        await _update_profile(
            snap.id,
            session_status=SESSION_AWAITING_MFA,
            signs_in_unattended=False,  # a person is asked: the platform cannot renew it alone
            mfa_challenge_id=cid,
            mfa_challenge_kind=KIND_CODE,
            mfa_expires_at=expires,
            mfa_screenshot_file_id=uuid.UUID(prompt.screenshot_file_id) if prompt.screenshot_file_id else None,
            message=outcome.message,
        )
        code = await _next_code(cid, snap.mfa_timeout)
        if code:
            await publish(
                cid,
                SignInOutcome(
                    status=STATUS_RUNNING,
                    message="Checking the one-time code...",
                    challenge_id=cid,
                    profile_id=snap.id,
                    stage=STAGE_MFA,
                ),
            )
        return code

    async def announce(prompt: MfaPrompt) -> None:
        nonlocal approval_asked
        approval_asked = True
        expires = datetime.now(UTC) + timedelta(seconds=snap.mfa_timeout)
        outcome = SignInOutcome(
            status=STATUS_MFA_REQUIRED,
            kind=prompt.kind,
            number=prompt.number,
            message=_approval_message(prompt),
            challenge_id=cid,
            screenshot_file_id=prompt.screenshot_file_id,
            expires_at=expires.isoformat(),
            profile_id=snap.id,
            stage=STAGE_MFA,
            last_url=prompt.url,
        )
        await publish(cid, outcome)
        await _update_profile(
            snap.id,
            session_status=SESSION_AWAITING_MFA,
            signs_in_unattended=False,  # an approval on a phone: a person again
            mfa_challenge_id=cid,
            mfa_challenge_kind=prompt.kind,
            mfa_expires_at=expires,
            mfa_screenshot_file_id=uuid.UUID(prompt.screenshot_file_id) if prompt.screenshot_file_id else None,
            message=outcome.message,
        )

    async def failed(
        message: str, *, stage: str | None = None, last_url: str | None = None, trace: list[dict] | None = None
    ) -> None:
        await _update_profile(
            snap.id,
            session_status=SESSION_FAILED,
            message=message,
            mfa_challenge_id=None,
            mfa_challenge_kind=None,
            mfa_expires_at=None,
        )
        await publish(
            cid,
            SignInOutcome(
                status=STATUS_FAILED,
                message=message,
                challenge_id=cid,
                profile_id=snap.id,
                stage=stage,
                last_url=last_url,
                trace=trace or None,
            ),
        )

    try:
        if snap.credential_problem:
            await failed(f"The sign-in cannot start: {snap.credential_problem}.")
            return
        async with profile_browser(
            policy=snap.policy,
            viewport=(1440, 900),
            pixel_ratio=1,
            storage_state=snap.storage_state,
            color_scheme=snap.options.get("color_scheme"),
            locale=snap.options.get("locale") or "en-US",
            timezone=snap.options.get("timezone") or "UTC",
        ) as rb:
            result = await drive_sign_in(rb, snap, user_id=user_id, ask=ask, announce_approval=announce)
            if not result.ok:
                await failed(result.message, stage=result.stage, last_url=result.last_url, trace=result.trace)
                return
            await publish(
                cid,
                SignInOutcome(
                    status=STATUS_RUNNING,
                    message="Signed in: saving the session...",
                    challenge_id=cid,
                    profile_id=snap.id,
                    stage=STAGE_SESSION_SAVE,
                ),
            )
            state, probe = await save_session(rb, snap)
            if probe.authenticated:
                message = f"{result.message} The session is saved; opened again, start_url renders the application."
            elif probe.http_status is not None:
                message = (
                    f"{result.message} The session is saved, but it could not be checked: start_url lands on "
                    f"{probe.url} ({probe.reason})."
                )
            else:
                message = (
                    "The application rendered signed in, but the saved session does not open it again: start_url "
                    f"lands on {probe.url} ({probe.reason})."
                )
            # A driven sign-in that reached the application asking no person renews by itself.
            unattended = result.driven and probe.authenticated and not asked and not approval_asked
            learnt = {"signs_in_unattended": True} if unattended else {}
            try:
                await asyncio.wait_for(
                    _update_profile(snap.id, storage_state=state, probe=probe.to_dict(), message=message, **learnt),
                    snap.session_save_timeout,
                )
            except TimeoutError:
                raise SignInTimeoutError(STAGE_SESSION_SAVE, probe.url, snap.session_save_timeout) from None
            status = _saved_session_status(probe)
            await publish(
                cid,
                SignInOutcome(
                    status=status,
                    message=message,
                    challenge_id=cid,
                    profile_id=snap.id,
                    stage=STAGE_APP_LOAD if status == STATUS_FAILED else None,
                    last_url=probe.url,
                    probe=probe.to_dict(),
                    trace=result.trace or None,
                ),
            )
    except SignInTimeoutError as exc:
        await failed(str(exc), stage=exc.stage, last_url=exc.last_url, trace=exc.trace)
    except SignInRequiredError as exc:
        await failed(str(exc), stage=getattr(exc, "stage", None), last_url=getattr(exc, "last_url", None), trace=exc.trace)
    except TargetRefusedError as exc:
        await failed(str(exc))
    except Exception as exc:
        logger.warning("Browser profile %s sign-in failed: %s", snap.id, format_exception_message(exc))
        await failed(f"The sign-in failed: {format_exception_message(exc)}"[:1000])
    finally:
        _running.pop(cid, None)
        _local_codes.pop(cid, None)
        _started_config.pop(cid, None)


async def _close_challenge(cid: str, profile_id: str | None) -> None:
    """End the sign-in waiting on *cid* (its browser closed) so a new one can start."""
    task = _running.pop(cid, None)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _local_codes.pop(cid, None)
    _started_config.pop(cid, None)
    await publish(
        cid,
        SignInOutcome(
            status=STATUS_FAILED,
            message="This sign-in was restarted: a new challenge replaces it.",
            challenge_id=cid,
            profile_id=profile_id,
            stage=STAGE_MFA,
        ),
    )


async def start_sign_in(
    profile: BrowserProfile, *, user_id: object, wait_seconds: float = 75.0, restart: bool = False
) -> SignInOutcome:
    """Start signing *profile* in and wait until it is signed in, failed or waiting for a person.

    A challenge already waiting for the person (its browser open on the
    prompt) is answered as it stands instead of opening a second browser -
    unless *restart* is asked, or the profile's login settings changed since
    that challenge opened: the old browser is then closed and a new sign-in
    starts from the settings as they are now.
    """
    if profile.mfa_challenge_id and profile.session_status == SESSION_AWAITING_MFA:
        old = profile.mfa_challenge_id
        live = await read_state(old)
        if live is not None and live.status == STATUS_MFA_REQUIRED and not _expired(live.expires_at):
            started = _started_config.get(old)
            changed = started is not None and started != _config_digest(profile.login_config)
            if not restart and not changed:
                return live
            await _close_challenge(old, str(profile.id))
    cid = secrets.token_urlsafe(18)
    snap = await resolved_snapshot(profile)
    _started_config[cid] = _config_digest(snap.login_config)
    await publish(
        cid, SignInOutcome(status=STATUS_RUNNING, message="Signing in...", challenge_id=cid, profile_id=snap.id)
    )
    await _update_profile(snap.id, session_status=SESSION_SIGNING_IN, message="Signing in...")
    task = asyncio.create_task(_run_sign_in(cid, snap, user_id))
    _running[cid] = task
    state = await _wait_for_change(cid, not_status={STATUS_RUNNING}, timeout=wait_seconds)
    return state or SignInOutcome(
        status=STATUS_RUNNING, message="Still signing in.", challenge_id=cid, profile_id=snap.id
    )


async def submit_code(cid: str, code: str, *, wait_seconds: float = 60.0) -> SignInOutcome:
    """Hand a one-time code to the sign-in waiting on *cid*, and wait for what it leads to."""
    digits = re.sub(r"\s+", "", code or "")
    if not re.fullmatch(r"[A-Za-z0-9-]{4,12}", digits):
        return SignInOutcome(
            status=STATUS_FAILED, message="A one-time code is 4 to 12 letters or digits.", challenge_id=cid
        )
    state = await read_state(cid)
    if state is None:
        return SignInOutcome(
            status=STATUS_FAILED, message="This sign-in is no longer waiting (it expired or ended).", challenge_id=cid
        )
    if state.status != STATUS_MFA_REQUIRED or state.kind != KIND_CODE:
        return state
    await send_code(cid, digits)
    # A refused code asks again with a new message: that is a change too.
    changed = await _wait_for_change(cid, not_status={STATUS_RUNNING}, timeout=wait_seconds, since=state.message)
    return changed or SignInOutcome(
        status=STATUS_RUNNING, message="Checking the code...", challenge_id=cid, profile_id=state.profile_id
    )


async def dry_run_sign_in(profile: BrowserProfile, *, user_id: object) -> SignInOutcome:
    """Run the whole sign-in once WITHOUT saving anything, and return its trace so an operator sees where it stalls.

    A profile that renews unattended (credentials + a valid TOTP secret, or a
    saved identity-provider session) runs end to end; one that would need a
    person (an MFA code with no TOTP secret, a push) stops at that point and
    says so. The saved session, the profile status and the stored message are
    never touched - a dry run never burns the live session. What it learns of
    the sign-in itself is kept (``signs_in_unattended``): reached the
    application asking no person, the profile renews by itself.
    """
    snap = await resolved_snapshot(profile)
    if snap.credential_problem:
        return SignInOutcome(
            status=STATUS_FAILED, message=f"The sign-in cannot start: {snap.credential_problem}.", profile_id=snap.id
        )
    screenshot_id: str | None = None
    try:
        async with profile_browser(
            policy=snap.policy,
            viewport=(1440, 900),
            pixel_ratio=1,
            storage_state=snap.storage_state,
            color_scheme=snap.options.get("color_scheme"),
            locale=snap.options.get("locale") or "en-US",
            timezone=snap.options.get("timezone") or "UTC",
        ) as rb:
            try:
                result = await drive_sign_in(rb, snap, user_id=user_id, ask=None, announce_approval=None)
            except SignInRequiredError as exc:
                screenshot_id = await _screenshot(rb.page, user_id)
                await _learn_unattended(snap.id, False)
                return SignInOutcome(
                    status=STATUS_FAILED,
                    message=f"Would need a person to finish: {exc}",
                    profile_id=snap.id,
                    kind=KIND_CODE,
                    stage=getattr(exc, "stage", STAGE_MFA),
                    last_url=getattr(exc, "last_url", None) or str(getattr(rb.page, "url", "") or ""),
                    screenshot_file_id=screenshot_id,
                    trace=exc.trace or None,
                )
            except SignInTimeoutError as exc:
                screenshot_id = await _screenshot(rb.page, user_id)
                return SignInOutcome(
                    status=STATUS_FAILED,
                    message=str(exc),
                    profile_id=snap.id,
                    stage=exc.stage,
                    last_url=exc.last_url,
                    screenshot_file_id=screenshot_id,
                    trace=exc.trace or None,
                )
            screenshot_id = await _screenshot(rb.page, user_id)
            if result.ok:
                _, probe = await save_session(rb, snap)  # probed, never stored
                if result.driven and probe.authenticated:
                    await _learn_unattended(snap.id, True)
                status = _saved_session_status(probe)
                if status == STATUS_ACTIVE:
                    message = "Dry run reached the application signed in; nothing was saved."
                elif status == STATUS_UNVERIFIED:
                    message = (
                        "Dry run reached the application signed in, but the session it would save could not be "
                        f"checked: start_url lands on {probe.url} ({probe.reason}). Nothing was saved."
                    )
                else:
                    message = f"Reached the application but it did not render signed in: {probe.url} ({probe.reason})."
                return SignInOutcome(
                    status=status,
                    message=message,
                    profile_id=snap.id,
                    stage=STAGE_APP_LOAD if status == STATUS_FAILED else None,
                    last_url=probe.url,
                    probe=probe.to_dict(),
                    screenshot_file_id=screenshot_id,
                    trace=result.trace or None,
                )
            return SignInOutcome(
                status=STATUS_FAILED,
                message=result.message,
                profile_id=snap.id,
                stage=result.stage,
                last_url=result.last_url,
                screenshot_file_id=screenshot_id,
                trace=result.trace or None,
            )
    except TargetRefusedError as exc:
        return SignInOutcome(status=STATUS_FAILED, message=str(exc), profile_id=snap.id, stage=STAGE_IDP_FORM)
    except Exception as exc:  # a dry run never raises into the caller
        logger.warning("Browser profile %s dry-run sign-in failed: %s", snap.id, format_exception_message(exc))
        return SignInOutcome(
            status=STATUS_FAILED,
            message=f"The dry run could not complete: {format_exception_message(exc)}"[:1000],
            profile_id=snap.id,
        )
