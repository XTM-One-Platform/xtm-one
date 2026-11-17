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

//! Real-browser control over the Chrome DevTools Protocol (feature = "browser").
//!
//! The runner ATTACHES to an already-running, authenticated browser (Chrome or
//! Edge started with `--remote-debugging-port`), so the live session - the
//! signed-in account the auditor wants to see - is the one being driven, not a
//! throwaway headless profile. The connection is held across commands so a
//! `navigate` then `screenshot` then `click` sequence operates on the same tab.
//!
//! The connection survives what a user does in that browser: a message
//! the protocol crate cannot parse - a newer browser's event, which a sign-in or
//! a new tab sends - is skipped instead of ending the CDP handler, and a
//! connection that is gone anyway is rebuilt on the next command, re-attached
//! to the tab being driven (or the browser's active tab once that one is
//! closed). `list_tabs` / `select_tab` choose the tab explicitly.
//!
//! `evaluate` runs a JavaScript expression in the driven tab (promises awaited,
//! the result as text, cut at [`EVALUATE_MAX_CHARS`]), the contract of the
//! platform's headless browser tool. `click`, `type` and `get_content` take an
//! optional `frame` (a CSS selector of an iframe of the top document); a
//! `click` or `type` whose selector the top document does not match also walks
//! its same-origin iframes, breadth first, and acts at the element's centre in
//! the top viewport. A cross-origin iframe's document is out of reach of the
//! top document: the error names the ones it could not search.
//!
//! DevTools does not go through the OS input desktop, so all of this keeps
//! working on a locked session, where `computer_action` is refused.
//!
//! Note: a CDP page screenshot captures the page viewport, not the OS window
//! chrome. For evidence that must show the browser frame + OS clock, the
//! full-desktop `screenshot` capability is the authoritative path; this tool is
//! for driving the page and capturing its content.

use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use chromiumoxide::browser::Browser;
use chromiumoxide::cdp::browser_protocol::input::InsertTextParams;
use chromiumoxide::cdp::browser_protocol::page::CaptureScreenshotFormat;
use chromiumoxide::cdp::browser_protocol::target::TargetId;
use chromiumoxide::cdp::js_protocol::runtime::EvaluateParams;
use chromiumoxide::error::{CdpError, ChannelError};
use chromiumoxide::layout::Point;
use chromiumoxide::page::ScreenshotParams;
use chromiumoxide::Page;
use futures_util::StreamExt;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::sync::Mutex;

use crate::protocol::{CommandOutcome, FilePart};

/// How long a tab fetched from the browser may take to be attached by the CDP
/// handler before `get_page` finds it.
const ATTACH_TIMEOUT: Duration = Duration::from_secs(5);
/// Bound on a request to the DevTools HTTP endpoint (`/json/list`).
const DEVTOOLS_HTTP_TIMEOUT: Duration = Duration::from_secs(5);
/// `evaluate`: the default and the largest time an expression may run.
const EVALUATE_DEFAULT_TIMEOUT_SECS: f64 = 30.0;
const EVALUATE_MAX_TIMEOUT_SECS: f64 = 120.0;
/// `evaluate`: the result text is cut there, as the headless browser tool does.
const EVALUATE_MAX_CHARS: usize = 20_000;
/// Locating a `click` / `type` target in the iframes the top document misses.
const FRAME_LOOKUP_TIMEOUT: Duration = Duration::from_secs(10);

/// Find `__SELECTOR__` in the top document or the iframe `__FRAME__` (`null`:
/// the top document), then their same-origin iframes breadth first; scroll it
/// into view (and focus it when `__FOCUS__`), and answer its centre in the top
/// viewport, or why it was not found.
const FIND_IN_FRAMES_JS: &str = r#"(() => {
  const selector = __SELECTOR__;
  const frameSelector = __FRAME__;
  const focus = __FOCUS__;
  const crossOrigin = [];
  const isFrame = (el) => !!el && /^i?frame$/i.test(el.tagName);
  const docOf = (el) => { try { return el.contentDocument; } catch (e) { return null; } };
  const label = (el) => el.id ? `${el.tagName.toLowerCase()}#${el.id}`
    : (el.getAttribute('name') ? `${el.tagName.toLowerCase()}[name="${el.getAttribute('name')}"]`
    : (el.getAttribute('src') || el.tagName.toLowerCase()));
  let start = document;
  let chain = [];
  if (frameSelector !== null) {
    let f;
    try { f = document.querySelector(frameSelector); } catch (e) { return { found: false, reason: `invalid frame selector: ${e.message}` }; }
    if (!isFrame(f)) return { found: false, reason: `no iframe matches the frame selector '${frameSelector}' in the top document` };
    const inner = docOf(f);
    if (!inner) return { found: false, reason: `the iframe '${frameSelector}' (${f.src || 'no src'}) is cross-origin: its document cannot be reached from the top document` };
    start = inner;
    chain = [f];
  }
  const queue = [[start, chain]];
  let hit = null;
  while (queue.length && !hit) {
    const [doc, path] = queue.shift();
    let el;
    try { el = doc.querySelector(selector); } catch (e) { return { found: false, reason: `invalid selector: ${e.message}` }; }
    if (el) { hit = [el, path]; break; }
    for (const f of doc.querySelectorAll('iframe, frame')) {
      const inner = docOf(f);
      if (inner) queue.push([inner, path.concat([f])]);
      else crossOrigin.push(f.src || label(f));
    }
  }
  if (!hit) {
    const where = frameSelector !== null ? `in the iframe '${frameSelector}' or its same-origin iframes` : 'in the top document or its same-origin iframes';
    const skipped = crossOrigin.length ? `; cross-origin iframes not searched: ${crossOrigin.slice(0, 5).join(', ')}` : '';
    return { found: false, reason: `no element matches '${selector}' ${where}${skipped}` };
  }
  const [el, path] = hit;
  el.scrollIntoView({ block: 'center', inline: 'center' });
  if (focus && typeof el.focus === 'function') el.focus();
  const r = el.getBoundingClientRect();
  let x = r.left + r.width / 2;
  let y = r.top + r.height / 2;
  for (const f of path) {
    const fr = f.getBoundingClientRect();
    x += fr.left + f.clientLeft;
    y += fr.top + f.clientTop;
  }
  return { found: true, x, y, frame: path.length ? path.map(label).join(' > ') : null };
})()"#;

/// The HTML of the iframe `__FRAME__` of the top document, or why it cannot
/// be read.
const FRAME_CONTENT_JS: &str = r#"(() => {
  const frameSelector = __FRAME__;
  let f;
  try { f = document.querySelector(frameSelector); } catch (e) { return { ok: false, reason: `invalid frame selector: ${e.message}` }; }
  if (!f || !/^i?frame$/i.test(f.tagName)) return { ok: false, reason: `no iframe matches the frame selector '${frameSelector}' in the top document` };
  let doc = null;
  try { doc = f.contentDocument; } catch (e) { doc = null; }
  if (!doc) return { ok: false, reason: `the iframe '${frameSelector}' (${f.src || 'no src'}) is cross-origin: its document cannot be reached from the top document` };
  return { ok: true, content: doc.documentElement ? doc.documentElement.outerHTML : '' };
})()"#;

const UNREACHABLE_ADVICE: &str = "Start Chrome or Edge with --remote-debugging-port=9222 \
     and its own --user-data-dir (recent versions ignore the port on the default \
     profile, and a launch while the browser already runs hands off to it and \
     exits), sign in once in that profile and keep it running.";

/// Holds the live CDP connection and the tab it drives across commands.
#[derive(Default)]
pub struct BrowserController {
    inner: Mutex<State>,
}

#[derive(Default)]
struct State {
    session: Option<Session>,
    /// The tab being driven (its CDP target id), kept across a reconnect so the
    /// new session re-attaches to it while it is open.
    tab: Option<String>,
}

struct Session {
    // Held for the lifetime of the session: dropping the `Browser` closes the
    // CDP connection, which would invalidate `page`.
    browser: Browser,
    page: Page,
    handler: HandlerTask,
}

/// The task polling the CDP handler. It owns the WebSocket, so a session that
/// is replaced, or fails halfway through connecting, must not leave it running.
struct HandlerTask(tokio::task::JoinHandle<()>);

impl Drop for HandlerTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// How a failed command relates to the CDP connection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Lost {
    /// The connection was already gone: the command never reached the browser.
    BeforeSending,
    /// The connection dropped while the command ran: it may have taken effect.
    WhileRunning,
}

/// One open page tab, as the DevTools HTTP endpoint lists it.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Tab {
    id: String,
    title: String,
    url: String,
}

impl BrowserController {
    pub fn new() -> Self {
        Self::default()
    }

    pub async fn execute(&self, cdp_url: &str, payload: &Value) -> CommandOutcome {
        match self.execute_inner(cdp_url, payload).await {
            Ok(outcome) => outcome,
            Err(err) => CommandOutcome::failed(format!("browser_control failed: {err:#}")),
        }
    }

    async fn execute_inner(&self, cdp_url: &str, payload: &Value) -> Result<CommandOutcome> {
        let action = payload
            .get("action")
            .and_then(|v| v.as_str())
            .ok_or_else(|| anyhow!("missing 'action'"))?;

        let mut state = self.inner.lock().await;
        if action == "list_tabs" {
            return list_tabs(cdp_url, state.tab.as_deref()).await;
        }
        match run(&mut state, cdp_url, action, payload).await {
            Ok(outcome) => Ok(outcome),
            Err(err) => {
                let Some(lost) = connection_loss(&err) else {
                    return Err(err);
                };
                // The session is dead: the next attempt reconnects and re-attaches.
                state.session = None;
                if !retry_after_reconnect(action, lost) {
                    return Err(err.context(format!(
                        "the browser connection dropped while '{action}' ran; it was not \
                         run again because it may already have taken effect (the next \
                         command reconnects)"
                    )));
                }
                tracing::warn!(
                    "browser_control: the CDP connection was lost ({err:#}); reconnecting \
                     and running '{action}' again"
                );
                run(&mut state, cdp_url, action, payload)
                    .await
                    .context("after reconnecting to the browser")
            }
        }
    }
}

async fn run(
    state: &mut State,
    cdp_url: &str,
    action: &str,
    payload: &Value,
) -> Result<CommandOutcome> {
    if action == "select_tab" {
        return select_tab(state, cdp_url, payload).await;
    }
    let session = attach(state, cdp_url).await?;

    match action {
        "navigate" => {
            let url = payload
                .get("url")
                .and_then(|v| v.as_str())
                .ok_or_else(|| anyhow!("'navigate' requires 'url'"))?;
            session.page.goto(url).await.context("navigating")?;
            session
                .page
                .wait_for_navigation()
                .await
                .context("waiting for navigation")?;
            let current = session.page.url().await.ok().flatten();
            Ok(CommandOutcome::completed(json!({
                "action": "navigate",
                "url": current,
                "ok": true,
            })))
        }
        "click" => {
            let selector = payload
                .get("selector")
                .and_then(|v| v.as_str())
                .ok_or_else(|| anyhow!("'click' requires 'selector'"))?;
            let frame = frame_selector(payload);
            if frame.is_none() {
                if let Ok(element) = session.page.find_element(selector).await {
                    element.click().await.context("clicking")?;
                    return Ok(CommandOutcome::completed(json!({
                        "action": "click", "selector": selector, "ok": true,
                    })));
                }
            }
            let found = locate_in_frames(&session.page, selector, frame, false).await?;
            session.page.click(found.point).await.context("clicking")?;
            Ok(CommandOutcome::completed(json!({
                "action": "click", "selector": selector, "frame": found.frame, "ok": true,
            })))
        }
        "type" => {
            let selector = payload
                .get("selector")
                .and_then(|v| v.as_str())
                .ok_or_else(|| anyhow!("'type' requires 'selector'"))?;
            let text = payload
                .get("text")
                .and_then(|v| v.as_str())
                .ok_or_else(|| anyhow!("'type' requires 'text'"))?;
            let frame = frame_selector(payload);
            // Keys only reach the focused element of the foreground tab: show
            // the driven tab first, click the field as a person would, then
            // focus it in the page (a click on an overlay or a hidden tab
            // leaves the focus on the body), and insert the whole text in one
            // CDP call: per-key dispatch could outlast the command budget on a
            // password field and leave it empty. Each stage is bounded and a
            // timeout names the stage that hung.
            staged(
                "bringing the tab to the front",
                session.page.bring_to_front(),
            )
            .await?;
            let found = staged(
                "locating the field",
                locate_in_frames(&session.page, selector, frame, false),
            )
            .await?;
            staged("clicking the field", session.page.click(found.point))
                .await
                .ok();
            let found_frame = staged(
                "focusing the field",
                locate_in_frames(&session.page, selector, frame, true),
            )
            .await?
            .frame;
            staged(
                "inserting the text",
                session.page.execute(InsertTextParams::new(text)),
            )
            .await?;
            // The length of what the focused field holds now, never its value:
            // the text may be a password.
            let value_length = staged(
                "reading the field back",
                evaluate_value(&session.page, FOCUSED_VALUE_LENGTH, TYPE_STAGE_TIMEOUT),
            )
            .await
            .ok()
            .and_then(|(value, _)| value)
            .and_then(|value| value.as_u64());
            let text_length = text.chars().count();
            check_typed(selector, text_length, value_length)?;
            Ok(CommandOutcome::completed(json!({
                "action": "type",
                "selector": selector,
                "frame": found_frame,
                "text_length": text_length,
                "value_length": value_length,
                "ok": true,
            })))
        }
        "get_content" => {
            let content = match frame_selector(payload) {
                None => session.page.content().await.context("reading content")?,
                Some(frame) => frame_content(&session.page, frame).await?,
            };
            Ok(CommandOutcome::completed(json!({
                "action": "get_content",
                "frame": frame_selector(payload),
                "content": content,
            })))
        }
        "evaluate" => {
            let expression = payload
                .get("expression")
                .and_then(|v| v.as_str())
                .filter(|s| !s.trim().is_empty())
                .ok_or_else(|| anyhow!("'evaluate' requires 'expression'"))?;
            let timeout = evaluate_timeout(payload);
            let (value, description) = evaluate_value(&session.page, expression, timeout).await?;
            let (result, truncated) = evaluation_text(value.as_ref(), description.as_deref());
            Ok(CommandOutcome::completed(json!({
                "action": "evaluate",
                "result": result,
                "truncated": truncated,
            })))
        }
        "screenshot" => {
            // A tab in the background produces no frame to capture: show the
            // driven tab first, as the evidence is about it.
            session
                .page
                .bring_to_front()
                .await
                .context("bringing the tab to the front")?;
            let bytes = session
                .page
                .screenshot(
                    ScreenshotParams::builder()
                        .format(CaptureScreenshotFormat::Png)
                        .full_page(false)
                        .build(),
                )
                .await
                .context("capturing page screenshot")?;
            // Evidence provenance: WHERE the capture was taken (live URL +
            // page title of the authenticated session) and the hash of the
            // exact bytes, computed on the capturing machine. The platform
            // re-hashes on receipt and records the verification verdict.
            let url = session.page.url().await.ok().flatten();
            let title: Option<String> = session
                .page
                .evaluate("document.title")
                .await
                .ok()
                .and_then(|r| r.into_value::<String>().ok());
            let sha256 = hex::encode(Sha256::digest(&bytes));
            let now = chrono::Utc::now();
            let result = json!({
                "action": "screenshot",
                "captured_at": now.to_rfc3339(),
                "bytes": bytes.len(),
                "sha256": sha256,
                "url": url,
                "title": title,
            });
            let filename = format!("browser-{}.png", now.format("%Y%m%dT%H%M%SZ"));
            Ok(CommandOutcome::completed_file(
                result,
                FilePart {
                    bytes,
                    filename,
                    content_type: "image/png".to_string(),
                },
            ))
        }
        other => Err(anyhow!("unknown browser action '{other}'")),
    }
}

/// The `frame` of a payload: a CSS selector of an iframe of the top document,
/// `None` when absent or blank.
fn frame_selector(payload: &Value) -> Option<&str> {
    payload
        .get("frame")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
}

/// `type`: the bound on each of its stages (locate, focus, insert, read back),
/// so a field that never answers fails well inside the command budget.
const TYPE_STAGE_TIMEOUT: Duration = Duration::from_secs(15);

/// The length of the focused field's value (inside same-origin iframes too),
/// or of the text of a focused contenteditable element; `null` when the
/// focused element holds no text (the body, a button).
const FOCUSED_VALUE_LENGTH: &str = "(() => { let el = document.activeElement; \
    while (el && el.contentDocument && el.contentDocument.activeElement) { el = el.contentDocument.activeElement; } \
    if (el && typeof el.value === 'string') return el.value.length; \
    return el && el.isContentEditable ? (el.textContent || '').length : null; })()";

/// A `type` of a non-empty text that left the focused field empty (or left the
/// focus on an element that holds no text) failed: say so instead of `ok`.
/// The error names the selector, never the text, which may be a secret.
fn check_typed(selector: &str, text_length: usize, value_length: Option<u64>) -> Result<()> {
    if text_length == 0 || value_length.is_some_and(|length| length > 0) {
        return Ok(());
    }
    Err(anyhow!(
        "nothing typed into '{selector}': the focused element held no text afterwards \
         (the field could not take the focus, or the tab is not in the foreground); \
         check the selector, or select_tab the tab first"
    ))
}

/// Run one stage of a `type` within [`TYPE_STAGE_TIMEOUT`]; a timeout or an
/// error names the stage.
async fn staged<T, E, F>(stage: &str, future: F) -> Result<T>
where
    F: std::future::Future<Output = std::result::Result<T, E>>,
    E: Into<anyhow::Error>,
{
    match tokio::time::timeout(TYPE_STAGE_TIMEOUT, future).await {
        Err(_) => Err(anyhow!(
            "typing: {stage} did not finish within {} s",
            TYPE_STAGE_TIMEOUT.as_secs()
        )),
        Ok(result) => result
            .map_err(Into::<anyhow::Error>::into)
            .with_context(|| format!("typing: {stage}")),
    }
}

/// How long an `evaluate` may run: `timeout` seconds, 30 by default, within
/// 1 to 120.
fn evaluate_timeout(payload: &Value) -> Duration {
    let secs = payload
        .get("timeout")
        .and_then(Value::as_f64)
        .filter(|s| s.is_finite())
        .unwrap_or(EVALUATE_DEFAULT_TIMEOUT_SECS)
        .clamp(1.0, EVALUATE_MAX_TIMEOUT_SECS);
    Duration::from_secs_f64(secs)
}

/// The text of an evaluation result: a string as it is, any other JSON value
/// serialised, a value that does not serialise (a DOM node, a function) by its
/// description; cut at [`EVALUATE_MAX_CHARS`] characters, with whether it was.
fn evaluation_text(value: Option<&Value>, description: Option<&str>) -> (String, bool) {
    let text = match value {
        Some(Value::String(text)) => text.clone(),
        Some(other) => other.to_string(),
        None => description.unwrap_or("undefined").to_string(),
    };
    match text.char_indices().nth(EVALUATE_MAX_CHARS) {
        Some((end, _)) => (text[..end].to_string(), true),
        None => (text, false),
    }
}

/// A script with its `__NAME__` placeholders replaced by JSON literals, which
/// are valid JavaScript literals whatever quotes or newlines a selector holds.
fn with_literals(script: &str, literals: &[(&str, Value)]) -> String {
    literals
        .iter()
        .fold(script.to_string(), |script, (name, value)| {
            script.replace(name, &value.to_string())
        })
}

/// Run `expression` in the tab, promises awaited, the value returned by value.
/// Answers the value (`None` when it has no JSON form) and the remote object's
/// description.
async fn evaluate_value(
    page: &Page,
    expression: &str,
    timeout: Duration,
) -> Result<(Option<Value>, Option<String>)> {
    let params = EvaluateParams::builder()
        .expression(expression)
        .await_promise(true)
        .return_by_value(true)
        .build()
        .map_err(|e| anyhow!("building the evaluation: {e}"))?;
    let evaluated = tokio::time::timeout(timeout, page.evaluate_expression(params))
        .await
        .map_err(|_| {
            anyhow!(
                "the expression did not finish within {} s",
                timeout.as_secs_f64()
            )
        })?
        .context("evaluating the expression")?;
    Ok((
        evaluated.value().cloned(),
        evaluated.object().description.clone(),
    ))
}

/// Where a `click` / `type` target found in an iframe is.
#[derive(Debug)]
struct Located {
    /// Its centre in the top viewport, where a CDP mouse event lands.
    point: Point,
    /// The iframes it was found through (`None`: the top document).
    frame: Option<String>,
}

/// Find `selector` in the iframe `frame`, or in the top document's same-origin
/// iframes, and answer where to click it ([`FIND_IN_FRAMES_JS`]).
async fn locate_in_frames(
    page: &Page,
    selector: &str,
    frame: Option<&str>,
    focus: bool,
) -> Result<Located> {
    let script = with_literals(
        FIND_IN_FRAMES_JS,
        &[
            ("__SELECTOR__", json!(selector)),
            ("__FRAME__", json!(frame)),
            ("__FOCUS__", json!(focus)),
        ],
    );
    let (value, _) = evaluate_value(page, &script, FRAME_LOOKUP_TIMEOUT)
        .await
        .with_context(|| format!("finding '{selector}'"))?;
    located(value.unwrap_or(Value::Null), selector)
}

/// The answer of [`FIND_IN_FRAMES_JS`] as a [`Located`], or its reason.
fn located(answer: Value, selector: &str) -> Result<Located> {
    if answer.get("found").and_then(Value::as_bool) != Some(true) {
        let reason = answer
            .get("reason")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| format!("no element matches '{selector}'"));
        return Err(anyhow!(reason));
    }
    let coordinate = |key: &str| {
        answer
            .get(key)
            .and_then(Value::as_f64)
            .filter(|v| v.is_finite())
            .ok_or_else(|| anyhow!("'{selector}' was found but has no position on the page"))
    };
    Ok(Located {
        point: Point::new(coordinate("x")?, coordinate("y")?),
        frame: answer
            .get("frame")
            .and_then(Value::as_str)
            .map(str::to_string),
    })
}

/// The HTML of the iframe `frame` of the top document ([`FRAME_CONTENT_JS`]).
async fn frame_content(page: &Page, frame: &str) -> Result<String> {
    let script = with_literals(FRAME_CONTENT_JS, &[("__FRAME__", json!(frame))]);
    let (value, _) = evaluate_value(page, &script, FRAME_LOOKUP_TIMEOUT)
        .await
        .with_context(|| format!("reading the iframe '{frame}'"))?;
    let answer = value.unwrap_or(Value::Null);
    if answer.get("ok").and_then(Value::as_bool) != Some(true) {
        let reason = answer
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("the iframe could not be read");
        return Err(anyhow!(reason.to_string()));
    }
    Ok(answer
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string())
}

/// The session, connected and attached to the tab being driven.
///
/// Reconnects when there is no session or its CDP handler ended (the
/// connection closed), and re-attaches when the driven tab was closed: to the
/// same tab while it is open, else to the browser's active tab.
async fn attach<'a>(state: &'a mut State, cdp_url: &str) -> Result<&'a mut Session> {
    if state
        .session
        .as_ref()
        .is_some_and(|session| session.handler.0.is_finished())
    {
        tracing::warn!("browser_control: the CDP connection had ended; reconnecting");
        state.session = None;
    }
    let tabs = open_tabs(cdp_url).await?;
    let wanted = choose_tab(&tabs, state.tab.as_deref()).map(|tab| tab.id.clone());
    let session = match state.session.take() {
        Some(mut session) => {
            if wanted.as_deref() != Some(session.page.target_id().as_ref()) {
                session.page = page_for(&session.browser, wanted.as_deref()).await?;
            }
            session
        }
        None => Session::connect(cdp_url, wanted.as_deref()).await?,
    };
    state.tab = Some(session.page.target_id().as_ref().to_string());
    Ok(state.session.insert(session))
}

impl Session {
    async fn connect(cdp_url: &str, tab: Option<&str>) -> Result<Self> {
        let (browser, mut handler) = Browser::connect(cdp_url)
            .await
            .map_err(|e| unreachable_error(cdp_url, &e.to_string()))?;
        let handler = HandlerTask(tokio::spawn(async move {
            // A message the protocol crate cannot parse is skipped: ending the
            // handler on it dropped the command channel, and every later
            // command failed with "send failed because receiver is gone".
            // Only a broken connection ends the handler.
            while let Some(event) = handler.next().await {
                match event {
                    Ok(()) => {}
                    Err(err @ (CdpError::Ws(_) | CdpError::Io(_))) => {
                        tracing::warn!("browser_control: the CDP connection broke: {err}");
                        break;
                    }
                    Err(err) => tracing::debug!("browser_control: CDP message skipped: {err}"),
                }
            }
        }));
        // The handler's target discovery reports the tabs already open; never
        // `fetch_targets`, whose answer re-creates every target and attaches it
        // again, leaving a tab with several sessions that its events split over.
        let page = page_for(&browser, tab).await?;
        Ok(Self {
            browser,
            page,
            handler,
        })
    }
}

/// The page of tab `tab`, or a new blank tab when the browser has none open.
async fn page_for(browser: &Browser, tab: Option<&str>) -> Result<Page> {
    let Some(id) = tab else {
        return browser
            .new_page("about:blank")
            .await
            .context("opening a tab");
    };
    // The handler attaches a discovered tab asynchronously and initialises it
    // before it answers any of the page's messages: wait for its main frame,
    // bounded.
    let deadline = tokio::time::Instant::now() + ATTACH_TIMEOUT;
    loop {
        let attached = match browser.get_page(TargetId::from(id.to_string())).await {
            Ok(page) => match page.mainframe().await {
                Ok(Some(_)) => return Ok(page),
                Ok(None) => None,
                Err(err) => Some(err),
            },
            Err(CdpError::NotFound) => None,
            Err(err) => Some(err),
        };
        if let Some(err) = attached {
            return Err(anyhow::Error::from(err).context(format!("attaching to tab {id}")));
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(anyhow!(
                "tab {id} was not ready within {}s; select another tab with select_tab",
                ATTACH_TIMEOUT.as_secs()
            ));
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

async fn list_tabs(cdp_url: &str, driven: Option<&str>) -> Result<CommandOutcome> {
    let tabs = open_tabs(cdp_url).await?;
    Ok(CommandOutcome::completed(json!({
        "action": "list_tabs",
        "count": tabs.len(),
        "tabs": tabs_json(&tabs, driven),
    })))
}

/// The tabs as `list_tabs` reports them: `active` is the tab the user used
/// last, `driven` the one browser_control acts on.
fn tabs_json(tabs: &[Tab], driven: Option<&str>) -> Vec<Value> {
    tabs.iter()
        .enumerate()
        .map(|(index, tab)| {
            json!({
                "tab_id": tab.id,
                "title": tab.title,
                "url": tab.url,
                "active": index == 0,
                "driven": Some(tab.id.as_str()) == driven,
            })
        })
        .collect()
}

async fn select_tab(state: &mut State, cdp_url: &str, payload: &Value) -> Result<CommandOutcome> {
    let tabs = open_tabs(cdp_url).await?;
    let requested = payload
        .get("tab_id")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|id| !id.is_empty());
    let tab = match requested {
        Some(id) => tabs.iter().find(|tab| tab.id == id).ok_or_else(|| {
            anyhow!("no open tab has id '{id}': list_tabs gives the ids of the open tabs")
        })?,
        None => tabs
            .first()
            .ok_or_else(|| anyhow!("the browser has no open tab"))?,
    }
    .clone();
    state.tab = Some(tab.id.clone());
    let session = attach(state, cdp_url).await?;
    session
        .page
        .bring_to_front()
        .await
        .context("bringing the tab to the front")?;
    Ok(CommandOutcome::completed(json!({
        "action": "select_tab",
        "tab_id": tab.id,
        "title": tab.title,
        "url": tab.url,
    })))
}

/// The open page tabs, most recently active first.
async fn open_tabs(cdp_url: &str) -> Result<Vec<Tab>> {
    let base = devtools_http_base(cdp_url)?;
    let client = reqwest::Client::builder()
        .timeout(DEVTOOLS_HTTP_TIMEOUT)
        // The endpoint is on this machine: an environment proxy has no business
        // relaying it.
        .no_proxy()
        .build()
        .context("building the DevTools HTTP client")?;
    let response = client
        .get(format!("{base}/json/list"))
        .send()
        .await
        .map_err(|e| unreachable_error(cdp_url, &e.to_string()))?;
    let list: Value = response
        .error_for_status()
        .context("listing the browser's tabs")?
        .json()
        .await
        .context("reading the browser's tab list")?;
    Ok(page_tabs(&list))
}

/// The page tabs of a `/json/list` answer, in its order: the browser lists its
/// targets most recently active first. DevTools windows are not tabs.
fn page_tabs(list: &Value) -> Vec<Tab> {
    list.as_array()
        .into_iter()
        .flatten()
        .filter_map(|target| {
            if target.get("type")?.as_str()? != "page" {
                return None;
            }
            let url = target
                .get("url")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if url.starts_with("devtools://") {
                return None;
            }
            Some(Tab {
                id: target.get("id")?.as_str()?.to_string(),
                title: target
                    .get("title")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
                url: url.to_string(),
            })
        })
        .collect()
}

/// The tab to drive: `driven` while it is open, else the active one.
fn choose_tab<'a>(tabs: &'a [Tab], driven: Option<&str>) -> Option<&'a Tab> {
    driven
        .and_then(|id| tabs.iter().find(|tab| tab.id == id))
        .or_else(|| tabs.first())
}

/// The HTTP origin of the DevTools endpoint, from its `http(s)` or `ws(s)` URL.
fn devtools_http_base(cdp_url: &str) -> Result<String> {
    let url = url::Url::parse(cdp_url.trim())
        .with_context(|| format!("the browser CDP URL '{cdp_url}' is not a URL"))?;
    let scheme = match url.scheme() {
        "http" | "ws" => "http",
        "https" | "wss" => "https",
        other => {
            return Err(anyhow!(
                "the browser CDP URL '{cdp_url}' must be http(s) or ws(s), not {other}"
            ))
        }
    };
    let host = url
        .host_str()
        .ok_or_else(|| anyhow!("the browser CDP URL '{cdp_url}' names no host"))?;
    Ok(match url.port() {
        Some(port) => format!("{scheme}://{host}:{port}"),
        None => format!("{scheme}://{host}"),
    })
}

/// How a failed command relates to the CDP connection, `None` when it is not
/// about the connection at all.
fn connection_loss(err: &anyhow::Error) -> Option<Lost> {
    err.chain()
        .find_map(|cause| match cause.downcast_ref::<CdpError>()? {
            CdpError::ChannelSendError(ChannelError::Send(_)) => Some(Lost::BeforeSending),
            CdpError::ChannelSendError(ChannelError::Canceled(_)) | CdpError::Ws(_) => {
                Some(Lost::WhileRunning)
            }
            _ => None,
        })
}

/// Whether an action is run again once the connection is back. A command that
/// never reached the browser always is; one cut while it ran only when running
/// it twice changes nothing (a read, a navigation to the same address).
fn retry_after_reconnect(action: &str, lost: Lost) -> bool {
    lost == Lost::BeforeSending
        || matches!(
            action,
            "navigate" | "screenshot" | "get_content" | "select_tab"
        )
}

fn unreachable_error(cdp_url: &str, detail: &str) -> anyhow::Error {
    let advice = browser_policy_block().unwrap_or_else(|| UNREACHABLE_ADVICE.to_string());
    anyhow!("could not reach the browser's DevTools endpoint at {cdp_url} ({detail}). {advice}")
}

/// The browsers whose remote debugging an enterprise policy can turn off: the
/// name, the policy registry key (Windows) and the managed policy folder (Linux).
const POLICY_BROWSERS: &[(&str, &str, &str)] = &[
    (
        "Microsoft Edge",
        r"SOFTWARE\Policies\Microsoft\Edge",
        "/etc/opt/edge/policies/managed",
    ),
    (
        "Google Chrome",
        r"SOFTWARE\Policies\Google\Chrome",
        "/etc/opt/chrome/policies/managed",
    ),
    (
        "Chromium",
        r"SOFTWARE\Policies\Chromium",
        "/etc/chromium/policies/managed",
    ),
];

/// One line naming a browser whose `RemoteDebuggingAllowed` policy is off, if any.
///
/// Such a browser starts with `--remote-debugging-port` and exits at once, so
/// "could not reach the endpoint" alone sends the user after the wrong cause.
fn browser_policy_block() -> Option<String> {
    POLICY_BROWSERS.iter().find_map(|(name, key, folder)| {
        policy_source(key, folder).map(|source| {
            format!(
                "{name} blocks remote debugging by policy (RemoteDebuggingAllowed is off in \
                 {source}): it cannot expose a DevTools endpoint on this machine - use \
                 another browser, or ask the administrator to allow it."
            )
        })
    })
}

#[cfg(windows)]
fn policy_source(key: &str, _folder: &str) -> Option<String> {
    use windows_sys::Win32::System::Registry::{
        RegGetValueW, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, RRF_RT_REG_DWORD,
    };

    let wide = |text: &str| text.encode_utf16().chain([0]).collect::<Vec<u16>>();
    let (subkey, value) = (wide(key), wide("RemoteDebuggingAllowed"));
    for (hive, label) in [(HKEY_LOCAL_MACHINE, "HKLM"), (HKEY_CURRENT_USER, "HKCU")] {
        let mut data: u32 = 1;
        let mut size = std::mem::size_of::<u32>() as u32;
        // SAFETY: both names are NUL-terminated UTF-16 buffers that outlive the
        // call, and `data` / `size` describe the 4-byte buffer RRF_RT_REG_DWORD
        // writes; nothing else is written.
        let status = unsafe {
            RegGetValueW(
                hive,
                subkey.as_ptr(),
                value.as_ptr(),
                RRF_RT_REG_DWORD,
                std::ptr::null_mut(),
                (&mut data as *mut u32).cast(),
                &mut size,
            )
        };
        if status == 0 && data == 0 {
            return Some(format!(r"{label}\{key}"));
        }
    }
    None
}

#[cfg(all(unix, not(target_os = "macos")))]
fn policy_source(_key: &str, folder: &str) -> Option<String> {
    std::fs::read_dir(folder)
        .ok()?
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.extension().is_some_and(|ext| ext == "json"))
        .find_map(|path| {
            let policy: Value = serde_json::from_str(&std::fs::read_to_string(&path).ok()?).ok()?;
            policy_disallows_debugging(&policy).then(|| path.display().to_string())
        })
}

#[cfg(target_os = "macos")]
fn policy_source(_key: &str, _folder: &str) -> Option<String> {
    // Managed preferences are binary property lists; not read here.
    None
}

/// Whether a managed policy document turns remote debugging off.
#[cfg(any(test, all(unix, not(target_os = "macos"))))]
fn policy_disallows_debugging(policy: &Value) -> bool {
    match policy.get("RemoteDebuggingAllowed") {
        Some(Value::Bool(allowed)) => !allowed,
        Some(Value::Number(number)) => number.as_u64() == Some(0),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tab(id: &str) -> Tab {
        Tab {
            id: id.to_string(),
            title: format!("title {id}"),
            url: format!("https://{id}.example"),
        }
    }

    #[test]
    fn the_tab_list_keeps_pages_in_the_browsers_order_and_drops_the_rest() {
        let list = json!([
            {"id": "B", "type": "page", "title": "Signed in", "url": "https://app.example/home"},
            {"id": "W", "type": "service_worker", "url": "https://app.example/sw.js"},
            {"id": "D", "type": "page", "url": "devtools://devtools/bundled/inspector.html"},
            {"id": "A", "type": "page", "title": "Login", "url": "https://app.example/login"},
            {"type": "page", "url": "https://no-id.example"},
        ]);
        let tabs = page_tabs(&list);
        assert_eq!(
            tabs.iter().map(|t| t.id.as_str()).collect::<Vec<_>>(),
            ["B", "A"]
        );
        assert_eq!(tabs[0].title, "Signed in");
        assert!(page_tabs(&json!({"not": "a list"})).is_empty());
    }

    #[test]
    fn the_driven_tab_is_kept_while_open_else_the_active_one_is_taken() {
        let tabs = [tab("new"), tab("old")];
        assert_eq!(choose_tab(&tabs, Some("old")).unwrap().id, "old");
        assert_eq!(choose_tab(&tabs, Some("closed")).unwrap().id, "new");
        assert_eq!(choose_tab(&tabs, None).unwrap().id, "new");
        assert!(choose_tab(&[], Some("old")).is_none());
    }

    #[test]
    fn list_tabs_marks_the_active_and_the_driven_tab() {
        let listed = tabs_json(&[tab("new"), tab("old")], Some("old"));
        assert_eq!(listed[0]["active"], true);
        assert_eq!(listed[0]["driven"], false);
        assert_eq!(listed[1]["active"], false);
        assert_eq!(listed[1]["driven"], true);
        assert_eq!(listed[1]["tab_id"], "old");
    }

    #[test]
    fn the_http_base_comes_from_an_http_or_a_websocket_url() {
        for (url, base) in [
            ("http://127.0.0.1:9222", "http://127.0.0.1:9222"),
            (
                "http://127.0.0.1:9222/json/version",
                "http://127.0.0.1:9222",
            ),
            (
                "ws://127.0.0.1:9222/devtools/browser/abc",
                "http://127.0.0.1:9222",
            ),
            (
                "wss://cdp.example/devtools/browser/abc",
                "https://cdp.example",
            ),
            ("http://[::1]:9222/", "http://[::1]:9222"),
        ] {
            assert_eq!(devtools_http_base(url).unwrap(), base, "{url}");
        }
        assert!(devtools_http_base("ftp://127.0.0.1:9222").is_err());
        assert!(devtools_http_base("not a url").is_err());
    }

    #[test]
    fn a_command_is_run_again_only_when_that_is_safe() {
        for action in [
            "navigate",
            "screenshot",
            "get_content",
            "select_tab",
            "click",
            "type",
        ] {
            assert!(
                retry_after_reconnect(action, Lost::BeforeSending),
                "{action}"
            );
        }
        for action in ["navigate", "screenshot", "get_content", "select_tab"] {
            assert!(
                retry_after_reconnect(action, Lost::WhileRunning),
                "{action}"
            );
        }
        for action in ["click", "type"] {
            assert!(
                !retry_after_reconnect(action, Lost::WhileRunning),
                "{action}"
            );
        }
    }

    #[test]
    fn an_error_that_is_not_about_the_connection_is_not_a_loss() {
        let err = anyhow::Error::from(CdpError::NotFound).context("attaching");
        assert_eq!(connection_loss(&err), None);
        assert_eq!(connection_loss(&anyhow!("finding '#login'")), None);
    }

    #[tokio::test]
    async fn a_command_after_the_handler_ended_never_reached_the_browser() {
        // The failure: the handler is gone, so the command
        // channel's receiver is too.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let _socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            std::future::pending::<()>().await;
        });
        let (browser, handler) = Browser::connect(format!("ws://{address}")).await.unwrap();
        drop(handler);
        let err = anyhow::Error::from(browser.pages().await.unwrap_err()).context("listing tabs");
        assert!(format!("{err:#}").contains("receiver is gone"), "{err:#}");
        assert_eq!(connection_loss(&err), Some(Lost::BeforeSending));
    }

    fn completed(outcome: &CommandOutcome) -> &Value {
        assert_eq!(
            outcome.status,
            crate::protocol::OutcomeStatus::Completed,
            "{:?}",
            outcome.error
        );
        outcome
            .result
            .as_ref()
            .expect("a completed outcome has a result")
    }

    /// Against a real browser: set `XTM_RUNNER_E2E_CDP_URL` to the DevTools
    /// URL of a browser started with `--remote-debugging-port` and run with
    /// `--ignored`. A tab opened the way a user opens one, then a CDP handler
    /// that ended, must not break the next screenshot.
    #[tokio::test]
    #[ignore = "needs a browser with a DevTools endpoint (XTM_RUNNER_E2E_CDP_URL)"]
    async fn a_tab_opened_by_hand_and_a_lost_connection_do_not_break_a_screenshot() {
        let Ok(cdp_url) = std::env::var("XTM_RUNNER_E2E_CDP_URL") else {
            return;
        };
        let controller = BrowserController::new();
        let navigate =
            json!({"action": "navigate", "url": "data:text/html,<title>first</title>one"});
        completed(&controller.execute(&cdp_url, &navigate).await);

        // A tab opened by hand (PUT /json/new is what the browser's own "new
        // tab" does over DevTools).
        let base = devtools_http_base(&cdp_url).unwrap();
        reqwest::Client::builder()
            .no_proxy()
            .build()
            .unwrap()
            .put(format!("{base}/json/new?about:blank"))
            .send()
            .await
            .unwrap()
            .error_for_status()
            .unwrap();

        let shot = controller
            .execute(&cdp_url, &json!({"action": "screenshot"}))
            .await;
        assert_eq!(completed(&shot)["title"], "first");
        assert!(shot
            .file
            .as_ref()
            .is_some_and(|file| !file.bytes.is_empty()));

        // The handler ends (what a message it could not parse used to do):
        // the next screenshot reconnects to the same tab.
        {
            let state = controller.inner.lock().await;
            let session = state.session.as_ref().unwrap();
            session.handler.0.abort();
            while !session.handler.0.is_finished() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }
        let shot = controller
            .execute(&cdp_url, &json!({"action": "screenshot"}))
            .await;
        assert_eq!(completed(&shot)["title"], "first");

        let tabs = controller
            .execute(&cdp_url, &json!({"action": "list_tabs"}))
            .await;
        let listed = completed(&tabs)["tabs"].as_array().unwrap().clone();
        assert!(listed.len() >= 2, "{listed:?}");
        let other = listed.iter().find(|tab| tab["driven"] == false).unwrap();
        let select = json!({"action": "select_tab", "tab_id": other["tab_id"]});
        assert_eq!(
            completed(&controller.execute(&cdp_url, &select).await)["tab_id"],
            other["tab_id"]
        );
        completed(
            &controller
                .execute(&cdp_url, &json!({"action": "screenshot"}))
                .await,
        );
        let refused = controller
            .execute(
                &cdp_url,
                &json!({"action": "select_tab", "tab_id": "no-such-tab"}),
            )
            .await;
        assert!(refused
            .error
            .unwrap()
            .contains("no open tab has id 'no-such-tab'"));
    }

    /// Against a real browser (`XTM_RUNNER_E2E_CDP_URL`, `--ignored`): a button
    /// and a field inside a same-origin iframe are clicked and typed into, by
    /// walking the iframes and through an explicit `frame`, and `evaluate`
    /// answers the page's title.
    #[tokio::test]
    #[ignore = "needs a browser with a DevTools endpoint (XTM_RUNNER_E2E_CDP_URL)"]
    async fn a_button_inside_a_same_origin_iframe_is_clicked_and_evaluate_reads_the_title() {
        let Ok(cdp_url) = std::env::var("XTM_RUNNER_E2E_CDP_URL") else {
            return;
        };
        let controller = BrowserController::new();
        // `&quot;` keeps the handler's quotes inside the srcdoc attribute.
        let inner = "<button id=go onclick=&quot;parent.document.title='clicked'&quot;>Go</button>\
                     <input id=name>";
        let page = format!(
            "data:text/html,<title>outer</title><p>top</p>\
             <iframe id=blade srcdoc=\"{inner}\" style=\"margin-top:200px\"></iframe>"
        );
        completed(
            &controller
                .execute(&cdp_url, &json!({"action": "navigate", "url": page}))
                .await,
        );
        let title = json!({"action": "evaluate", "expression": "document.title"});
        assert_eq!(
            completed(&controller.execute(&cdp_url, &title).await)["result"],
            "outer"
        );

        // Not in the top document: found by walking the iframes.
        let click = controller
            .execute(&cdp_url, &json!({"action": "click", "selector": "#go"}))
            .await;
        assert_eq!(completed(&click)["frame"], "iframe#blade");
        assert_eq!(
            completed(&controller.execute(&cdp_url, &title).await)["result"],
            "clicked"
        );

        // No prior focus on the field: the focus is on the body.
        let blur = json!({"action": "evaluate", "expression": "document.activeElement.blur()"});
        completed(&controller.execute(&cdp_url, &blur).await);
        let typed = json!({
            "action": "type", "selector": "#name", "text": "Contoso", "frame": "#blade",
        });
        let typed = controller.execute(&cdp_url, &typed).await;
        assert_eq!(completed(&typed)["value_length"], 7);
        let value = json!({
            "action": "evaluate",
            "expression": "document.querySelector('#blade').contentDocument.querySelector('#name').value",
        });
        assert_eq!(
            completed(&controller.execute(&cdp_url, &value).await)["result"],
            "Contoso"
        );

        let content = json!({"action": "get_content", "frame": "#blade"});
        let content = controller.execute(&cdp_url, &content).await;
        let html = completed(&content)["content"].as_str().unwrap();
        assert!(html.contains("id=\"go\""), "{html}");

        let missing = json!({"action": "click", "selector": "#nowhere"});
        let error = controller.execute(&cdp_url, &missing).await.error.unwrap();
        assert!(error.contains("no element matches '#nowhere'"), "{error}");
        assert!(error.contains("same-origin iframes"), "{error}");
    }

    #[test]
    fn the_frame_selector_is_optional_and_trimmed() {
        let blade = json!({"frame": "  #blade "});
        assert_eq!(frame_selector(&blade), Some("#blade"));
        assert_eq!(frame_selector(&json!({"frame": "  "})), None);
        assert_eq!(frame_selector(&json!({})), None);
    }

    #[tokio::test(start_paused = true)]
    async fn a_type_stage_that_hangs_fails_naming_the_stage() {
        let hung = std::future::pending::<std::result::Result<(), anyhow::Error>>();
        let err = staged("inserting the text", hung).await.unwrap_err();
        let expected = "typing: inserting the text did not finish within 15 s";
        assert_eq!(err.to_string(), expected);
    }

    #[tokio::test]
    async fn a_type_stage_error_names_the_stage() {
        let failed = async { Err::<(), _>(anyhow!("no node")) };
        let err = staged("locating the field", failed).await.unwrap_err();
        let chain = format!("{err:#}");
        assert!(chain.starts_with("typing: locating the field"));
        let ok = async { Ok::<u8, anyhow::Error>(7) };
        assert_eq!(staged("reading the field back", ok).await.unwrap(), 7);
    }

    #[test]
    fn a_type_that_left_the_field_empty_fails_without_echoing_the_text() {
        assert!(check_typed("#password", 12, Some(12)).is_ok());
        // An empty text has nothing to land.
        assert!(check_typed("#password", 0, None).is_ok());
        assert!(check_typed("#password", 0, Some(0)).is_ok());
        // The focus stayed on the body (no text value) or the field is empty.
        for read_back in [None, Some(0)] {
            let err = check_typed("#password", 12, read_back)
                .unwrap_err()
                .to_string();
            assert!(err.starts_with("nothing typed into '#password'"), "{err}");
            assert!(err.contains("foreground"), "{err}");
        }
    }

    #[test]
    fn the_evaluate_timeout_defaults_to_30_s_and_stays_within_1_and_120() {
        let timeout = |payload: Value| evaluate_timeout(&payload).as_secs();
        assert_eq!(timeout(json!({})), 30);
        assert_eq!(timeout(json!({"timeout": 5})), 5);
        assert_eq!(timeout(json!({"timeout": 900})), 120);
        assert_eq!(timeout(json!({"timeout": 0})), 1);
    }

    #[test]
    fn an_evaluation_is_answered_as_text_and_cut_past_the_limit() {
        let as_text = |value: Value| evaluation_text(Some(&value), None).0;
        assert_eq!(as_text(json!("Entra")), "Entra");
        assert_eq!(as_text(json!({"a": 1})), r#"{"a":1}"#);
        let node = evaluation_text(None, Some("HTMLDivElement"));
        assert_eq!(node.0, "HTMLDivElement");
        assert_eq!(evaluation_text(None, None).0, "undefined");
        let long = "\u{e9}".repeat(EVALUATE_MAX_CHARS + 5);
        let (cut, truncated) = evaluation_text(Some(&json!(long)), None);
        assert!(truncated);
        assert_eq!(cut.chars().count(), EVALUATE_MAX_CHARS);
    }

    #[test]
    fn a_selector_is_put_in_the_script_as_a_javascript_literal() {
        let selector = json!("button[aria-label=\"Save\"]\n");
        let literals = [
            ("__SELECTOR__", selector),
            ("__FRAME__", Value::Null),
            ("__FOCUS__", json!(false)),
        ];
        let script = with_literals(FIND_IN_FRAMES_JS, &literals);
        let expected = r#"const selector = "button[aria-label=\"Save\"]\n";"#;
        assert!(script.contains(expected), "{script}");
        assert!(script.contains("const frameSelector = null;"));
        assert!(!script.contains("__"));
    }

    #[test]
    fn a_found_element_gives_its_point_and_frame_and_a_miss_its_reason() {
        let answer = json!({"found": true, "x": 10.5, "y": 20.0, "frame": "iframe#blade"});
        let hit = located(answer, "#go").unwrap();
        assert_eq!(hit.point, Point::new(10.5, 20.0));
        assert_eq!(hit.frame.as_deref(), Some("iframe#blade"));
        let miss = json!({"found": false, "reason": "cross-origin"});
        let miss = located(miss, "#go").unwrap_err();
        assert_eq!(miss.to_string(), "cross-origin");
        let empty = located(Value::Null, "#go").unwrap_err();
        assert_eq!(empty.to_string(), "no element matches '#go'");
    }

    #[test]
    fn evaluate_is_not_run_again_after_a_drop_while_it_ran() {
        // An expression may change the page: running it twice is not safe.
        assert!(!retry_after_reconnect("evaluate", Lost::WhileRunning));
        assert!(retry_after_reconnect("evaluate", Lost::BeforeSending));
    }

    #[test]
    fn a_managed_policy_that_turns_debugging_off_is_recognised() {
        assert!(policy_disallows_debugging(
            &json!({"RemoteDebuggingAllowed": false})
        ));
        assert!(policy_disallows_debugging(
            &json!({"RemoteDebuggingAllowed": 0})
        ));
        assert!(!policy_disallows_debugging(
            &json!({"RemoteDebuggingAllowed": true})
        ));
        assert!(!policy_disallows_debugging(
            &json!({"HomepageLocation": "x"})
        ));
    }

    #[test]
    fn an_unreachable_endpoint_says_how_to_start_the_browser_or_names_the_policy() {
        let message = format!(
            "{}",
            unreachable_error("http://127.0.0.1:9222", "connection refused")
        );
        assert!(message.starts_with(
            "could not reach the browser's DevTools endpoint at http://127.0.0.1:9222 (connection refused). "
        ));
        assert!(
            message.contains("--user-data-dir")
                || message.contains("blocks remote debugging by policy"),
            "{message}"
        );
        assert!(!message.contains('\n'));
    }
}
