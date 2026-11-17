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

//! Synthetic mouse / keyboard input on the runner's real desktop, via `enigo`.
//!
//! Actions mirror the `computer_action` tool: move, click, double_click,
//! right_click, drag, scroll, type, key. Coordinates are absolute desktop
//! pixels. Synchronous (runs inside a blocking task).
//!
//! The blocking task cannot be aborted: the executor's ceiling only stops
//! waiting for it. So `type` and `scroll` are bounded by the command's own time
//! budget, never by a length: they work in small chunks, check the command's
//! deadline between chunks, and stop there, saying how far they got. The work
//! that cannot be stopped is one chunk, and nothing is typed after the budget,
//! which ends 30 s before the executor reports a timeout.
//!
//! On Windows, input sent while the lock screen or a UAC prompt holds the
//! input desktop is dropped by UIPI and `SendInput` still reports success, so a
//! click on a locked session used to answer `ok: true` with nothing done. The
//! input desktop is read first ([`input_desktop`]): anything but `Default`
//! refuses the action with `input blocked: session locked`. `browser_control`
//! goes through DevTools, not the input desktop, and keeps working there.

use std::time::Instant;

use anyhow::{anyhow, Result};
use enigo::{Axis, Button, Coordinate, Direction, Enigo, Key, Keyboard, Mouse, Settings};

use crate::capabilities::permissions::{ensure, Permission};
use crate::protocol::CommandOutcome;

/// Characters entered per `enigo::text` call: about a second even on the Linux
/// backend, which has no `fast_text` and replays one XTEST keystroke per
/// character (with a 12 ms delay whenever a keycode repeats).
const TYPE_CHUNK_CHARS: usize = 64;

/// Wheel clicks per `enigo::scroll` call. Small enough that no backend
/// multiplies it past `i32`: Windows sends `clicks * WHEEL_DELTA` (120) in one
/// event.
const SCROLL_STEP_CLICKS: u64 = 16;

/// How far a chunked action got: `done` of `total` characters or clicks.
#[derive(Debug, PartialEq, Eq)]
struct Progress {
    done: u64,
    total: u64,
}

impl Progress {
    fn finished(&self) -> bool {
        self.done == self.total
    }
}

/// How an action ended: done, or stopped by the command's time budget, with the
/// message that says how far it got.
enum Finished {
    Done(String),
    OutOfTime(String),
}

/// Wheel clicks a `scroll` payload asks for: `amount`, 3 when absent or not an
/// integer. Any integer is honoured: [`scroll_in_steps`] spends it in steps no
/// backend can overflow.
fn scroll_amount(payload: &serde_json::Value) -> i64 {
    payload.get("amount").and_then(|v| v.as_i64()).unwrap_or(3)
}

/// Enter `text` in chunks of [`TYPE_CHUNK_CHARS`] characters, asking `in_time`
/// before each one and stopping once it says the budget is spent.
fn type_in_chunks(
    text: &str,
    mut in_time: impl FnMut() -> bool,
    mut enter: impl FnMut(&str) -> Result<()>,
) -> Result<Progress> {
    let total = text.chars().count() as u64;
    let mut done = 0;
    let mut rest = text;
    while !rest.is_empty() && in_time() {
        let end = rest
            .char_indices()
            .nth(TYPE_CHUNK_CHARS)
            .map_or(rest.len(), |(at, _)| at);
        let (chunk, tail) = rest.split_at(end);
        enter(chunk)?;
        done += chunk.chars().count() as u64;
        rest = tail;
    }
    Ok(Progress { done, total })
}

/// Scroll `amount` wheel clicks (positive = up) in steps of at most
/// [`SCROLL_STEP_CLICKS`], asking `in_time` before each one. `step` gets a
/// signed click count that fits an `i32` and negates without overflow, whatever
/// the amount (`i64::MIN` included).
fn scroll_in_steps(
    amount: i64,
    mut in_time: impl FnMut() -> bool,
    mut step: impl FnMut(i32) -> Result<()>,
) -> Result<Progress> {
    let total = amount.unsigned_abs();
    let sign = if amount < 0 { -1 } else { 1 };
    let mut done = 0;
    while done < total && in_time() {
        let clicks = (total - done).min(SCROLL_STEP_CLICKS);
        step(sign * clicks as i32)?;
        done += clicks;
    }
    Ok(Progress { done, total })
}

/// Run one `computer_action`. `deadline` is the end of the command's time
/// budget (`None`: a budget too large to place on the clock); `type` and
/// `scroll` stop there.
pub fn execute(payload: &serde_json::Value, deadline: Option<Instant>) -> CommandOutcome {
    // macOS silently ignores synthetic input until Accessibility is granted;
    // refuse with an actionable message rather than reporting a no-op as success.
    if let Err(msg) = ensure(Permission::Accessibility) {
        return CommandOutcome::failed(msg);
    }
    if let Some(msg) = input_blocked(&input_desktop()) {
        return CommandOutcome::failed(msg);
    }
    match execute_inner(payload, deadline) {
        Ok(Finished::Done(action)) => CommandOutcome::completed(serde_json::json!({
            "action": action,
            "ok": true,
        })),
        Ok(Finished::OutOfTime(message)) => CommandOutcome::timeout(message),
        Err(err) => CommandOutcome::failed(format!("computer_action failed: {err:#}")),
    }
}

/// The desktop receiving keyboard and mouse input, as far as synthetic input
/// is concerned.
#[derive(Debug, Clone, PartialEq, Eq)]
// Only Windows reads another desktop; elsewhere the tests build the others.
#[cfg_attr(not(windows), allow(dead_code))]
enum InputDesktop {
    /// The user's desktop (`Default` on Windows; every other OS): input lands.
    User,
    /// Another desktop holds input: `Winlogon` (lock screen, UAC secure
    /// desktop, Ctrl+Alt+Del) or `Screen-saver`. Synthetic input is dropped.
    Other(String),
    /// The input desktop cannot even be opened: the secure desktop, which a
    /// user process may not open.
    Unreadable,
}

/// The error a `computer_action` answers instead of reporting dropped input
/// as done, `None` when input reaches the user's desktop.
fn input_blocked(desktop: &InputDesktop) -> Option<String> {
    let holder = match desktop {
        InputDesktop::User => return None,
        InputDesktop::Other(name) => format!("the '{name}' desktop holds input"),
        InputDesktop::Unreadable => "the input desktop cannot be opened".to_string(),
    };
    Some(format!(
        "input blocked: session locked ({holder}: the lock screen or a UAC prompt). Windows \
         drops synthetic mouse and keyboard input there, so nothing was done. Unlock the \
         session for computer_action; browser_control keeps working over DevTools on a \
         locked session."
    ))
}

/// Read the input desktop (Windows): its name through `OpenInputDesktop` and
/// `GetUserObjectInformationW(UOI_NAME)`.
#[cfg(windows)]
fn input_desktop() -> InputDesktop {
    use windows_sys::Win32::System::StationsAndDesktops::{
        CloseDesktop, GetUserObjectInformationW, OpenInputDesktop, DESKTOP_READOBJECTS, UOI_NAME,
    };

    // SAFETY: no pointer argument; the handle is closed below.
    let desk = unsafe { OpenInputDesktop(0, 0, DESKTOP_READOBJECTS) };
    if desk.is_null() {
        return InputDesktop::Unreadable;
    }
    let mut name = [0u16; 64];
    let mut needed = 0u32;
    // SAFETY: `name` is a writable buffer of the byte length passed, `needed`
    // a writable u32, and `desk` an open desktop handle.
    let read = unsafe {
        GetUserObjectInformationW(
            desk,
            UOI_NAME,
            name.as_mut_ptr().cast(),
            std::mem::size_of_val(&name) as u32,
            &mut needed,
        )
    };
    // SAFETY: `desk` was opened above and is not used after this.
    unsafe { CloseDesktop(desk) };
    if read == 0 {
        return InputDesktop::Unreadable;
    }
    let len = name.iter().position(|&c| c == 0).unwrap_or(name.len());
    let name = String::from_utf16_lossy(&name[..len]);
    if name.eq_ignore_ascii_case("Default") {
        InputDesktop::User
    } else {
        InputDesktop::Other(name)
    }
}

/// Elsewhere input always reaches the session's display (a locked macOS or X11
/// session is not a separate input desktop).
#[cfg(not(windows))]
fn input_desktop() -> InputDesktop {
    InputDesktop::User
}

fn execute_inner(payload: &serde_json::Value, deadline: Option<Instant>) -> Result<Finished> {
    let in_time = move || deadline.is_none_or(|end| Instant::now() < end);
    let action = payload
        .get("action")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("missing 'action'"))?
        .to_string();

    let mut enigo =
        Enigo::new(&Settings::default()).map_err(|e| anyhow!("initializing input backend: {e}"))?;

    let x = payload.get("x").and_then(|v| v.as_i64());
    let y = payload.get("y").and_then(|v| v.as_i64());
    let x2 = payload.get("x2").and_then(|v| v.as_i64());
    let y2 = payload.get("y2").and_then(|v| v.as_i64());
    let button = parse_button(payload.get("button").and_then(|v| v.as_str()));

    match action.as_str() {
        "move" => {
            move_to(&mut enigo, x, y)?;
        }
        "click" => {
            move_to(&mut enigo, x, y)?;
            enigo
                .button(button, Direction::Click)
                .map_err(|e| anyhow!("click: {e}"))?;
        }
        "double_click" => {
            move_to(&mut enigo, x, y)?;
            enigo
                .button(button, Direction::Click)
                .map_err(|e| anyhow!("click: {e}"))?;
            enigo
                .button(button, Direction::Click)
                .map_err(|e| anyhow!("click: {e}"))?;
        }
        "right_click" => {
            move_to(&mut enigo, x, y)?;
            enigo
                .button(Button::Right, Direction::Click)
                .map_err(|e| anyhow!("right_click: {e}"))?;
        }
        "drag" => {
            move_to(&mut enigo, x, y)?;
            enigo
                .button(button, Direction::Press)
                .map_err(|e| anyhow!("drag press: {e}"))?;
            move_to(&mut enigo, x2, y2)?;
            enigo
                .button(button, Direction::Release)
                .map_err(|e| anyhow!("drag release: {e}"))?;
        }
        "scroll" => {
            // Positive amount scrolls up; enigo scrolls down for positive, so negate.
            let progress = scroll_in_steps(scroll_amount(payload), in_time, |clicks| {
                enigo
                    .scroll(-clicks, Axis::Vertical)
                    .map_err(|e| anyhow!("scroll: {e}"))
            })?;
            if !progress.finished() {
                return Ok(Finished::OutOfTime(format!(
                    "'scroll' stopped after {} of {} clicks: the command's time budget ran out",
                    progress.done, progress.total
                )));
            }
        }
        "type" => {
            let text = payload
                .get("text")
                .and_then(|v| v.as_str())
                .ok_or_else(|| anyhow!("'type' requires 'text'"))?;
            let progress = type_in_chunks(text, in_time, |chunk| {
                enigo.text(chunk).map_err(|e| anyhow!("type: {e}"))
            })?;
            if !progress.finished() {
                return Ok(Finished::OutOfTime(format!(
                    "'type' stopped after {} of {} characters: the command's time budget ran \
                     out, so the rest was not typed",
                    progress.done, progress.total
                )));
            }
        }
        "key" => {
            let combo = payload
                .get("keys")
                .and_then(|v| v.as_str())
                .ok_or_else(|| anyhow!("'key' requires 'keys'"))?;
            press_combo(&mut enigo, combo)?;
        }
        other => return Err(anyhow!("unknown action '{other}'")),
    }

    Ok(Finished::Done(action))
}

fn move_to(enigo: &mut Enigo, x: Option<i64>, y: Option<i64>) -> Result<()> {
    if let (Some(x), Some(y)) = (x, y) {
        enigo
            .move_mouse(x as i32, y as i32, Coordinate::Abs)
            .map_err(|e| anyhow!("move: {e}"))?;
    }
    Ok(())
}

fn parse_button(name: Option<&str>) -> Button {
    match name.map(|s| s.to_ascii_lowercase()).as_deref() {
        Some("right") => Button::Right,
        Some("middle") => Button::Middle,
        _ => Button::Left,
    }
}

/// Press a key combination like `ctrl+s`, `ctrl+shift+n`, `Enter`.
fn press_combo(enigo: &mut Enigo, combo: &str) -> Result<()> {
    let tokens: Vec<&str> = combo
        .split('+')
        .map(|t| t.trim())
        .filter(|t| !t.is_empty())
        .collect();
    if tokens.is_empty() {
        return Err(anyhow!("empty key combo"));
    }
    let (modifiers, key_token) = tokens.split_at(tokens.len() - 1);
    let mut held = Vec::new();
    for m in modifiers {
        let key = parse_modifier(m).ok_or_else(|| anyhow!("unknown modifier '{m}'"))?;
        enigo
            .key(key, Direction::Press)
            .map_err(|e| anyhow!("modifier press: {e}"))?;
        held.push(key);
    }
    let key = parse_key(key_token[0])?;
    let tap = enigo
        .key(key, Direction::Click)
        .map_err(|e| anyhow!("key: {e}"));
    // Always release modifiers, even if the key tap failed.
    for key in held.into_iter().rev() {
        let _ = enigo.key(key, Direction::Release);
    }
    tap
}

fn parse_modifier(name: &str) -> Option<Key> {
    match name.to_ascii_lowercase().as_str() {
        "ctrl" | "control" => Some(Key::Control),
        "shift" => Some(Key::Shift),
        "alt" | "option" => Some(Key::Alt),
        "meta" | "cmd" | "command" | "super" | "win" => Some(Key::Meta),
        _ => None,
    }
}

fn parse_key(name: &str) -> Result<Key> {
    let lower = name.to_ascii_lowercase();
    let key = match lower.as_str() {
        "enter" | "return" => Key::Return,
        "tab" => Key::Tab,
        "esc" | "escape" => Key::Escape,
        "space" => Key::Space,
        "backspace" => Key::Backspace,
        "delete" | "del" => Key::Delete,
        "up" => Key::UpArrow,
        "down" => Key::DownArrow,
        "left" => Key::LeftArrow,
        "right" => Key::RightArrow,
        "home" => Key::Home,
        "end" => Key::End,
        "pageup" => Key::PageUp,
        "pagedown" => Key::PageDown,
        "f1" => Key::F1,
        "f2" => Key::F2,
        "f3" => Key::F3,
        "f4" => Key::F4,
        "f5" => Key::F5,
        "f6" => Key::F6,
        "f7" => Key::F7,
        "f8" => Key::F8,
        "f9" => Key::F9,
        "f10" => Key::F10,
        "f11" => Key::F11,
        "f12" => Key::F12,
        _ => {
            let mut chars = name.chars();
            match (chars.next(), chars.next()) {
                (Some(c), None) => Key::Unicode(c),
                _ => return Err(anyhow!("unknown key '{name}'")),
            }
        }
    };
    Ok(key)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn amount(value: serde_json::Value) -> i64 {
        scroll_amount(&json!({ "action": "scroll", "amount": value }))
    }

    /// An `in_time` that says yes `chunks` times, then no: the command's budget
    /// runs out after that many chunks.
    fn budget_of(chunks: usize) -> impl FnMut() -> bool {
        let mut left = chunks;
        move || {
            let in_time = left > 0;
            left = left.saturating_sub(1);
            in_time
        }
    }

    fn typed(text: &str, in_time: impl FnMut() -> bool) -> (Progress, Vec<String>) {
        let mut chunks = Vec::new();
        let progress = type_in_chunks(text, in_time, |chunk| {
            chunks.push(chunk.to_string());
            Ok(())
        })
        .unwrap();
        (progress, chunks)
    }

    fn scrolled(amount: i64, in_time: impl FnMut() -> bool) -> (Progress, Vec<i32>) {
        let mut steps = Vec::new();
        let progress = scroll_in_steps(amount, in_time, |clicks| {
            steps.push(clicks);
            Ok(())
        })
        .unwrap();
        (progress, steps)
    }

    #[test]
    fn input_on_the_users_desktop_is_not_blocked() {
        assert_eq!(input_blocked(&InputDesktop::User), None);
    }

    #[test]
    fn a_locked_session_or_a_uac_prompt_blocks_input_with_a_clear_error() {
        for desktop in [
            InputDesktop::Other("Winlogon".to_string()),
            InputDesktop::Unreadable,
        ] {
            let msg = input_blocked(&desktop).expect("blocked");
            assert!(msg.starts_with("input blocked: session locked"), "{msg}");
            assert!(msg.contains("browser_control keeps working"), "{msg}");
        }
        let named = input_blocked(&InputDesktop::Other("Winlogon".to_string())).unwrap();
        assert!(named.contains("'Winlogon'"), "{named}");
    }

    #[cfg(not(windows))]
    #[test]
    fn outside_windows_the_input_desktop_is_the_users() {
        assert_eq!(input_desktop(), InputDesktop::User);
    }

    #[test]
    fn a_text_of_any_length_is_typed_whole_while_the_budget_lasts() {
        // Far past the 4096 characters `type` used to refuse up front, with
        // characters of every UTF-8 width: chunks split between characters.
        let text = "a\u{e9}\u{20ac}\u{1F600}".repeat(2_500);
        let (progress, chunks) = typed(&text, || true);
        assert!(progress.finished());
        assert_eq!(progress.total, 10_000);
        assert_eq!(chunks.concat(), text);
        assert!(chunks
            .iter()
            .all(|chunk| chunk.chars().count() <= TYPE_CHUNK_CHARS));
    }

    #[test]
    fn typing_stops_between_chunks_once_the_budget_ran_out() {
        let text = "x".repeat(1_000);
        let (progress, chunks) = typed(&text, budget_of(2));
        let typed_chars = 2 * TYPE_CHUNK_CHARS;
        assert_eq!(
            progress,
            Progress {
                done: typed_chars as u64,
                total: 1_000
            }
        );
        assert_eq!(chunks.concat(), text[..typed_chars]);
    }

    #[test]
    fn nothing_is_typed_when_the_budget_ran_out_before_typing_started() {
        let (progress, chunks) = typed("hello", budget_of(0));
        assert_eq!(progress, Progress { done: 0, total: 5 });
        assert!(chunks.is_empty());
    }

    #[test]
    fn an_empty_text_is_done_at_once() {
        let (progress, chunks) = typed("", budget_of(0));
        assert!(progress.finished());
        assert!(chunks.is_empty());
    }

    #[test]
    fn a_typing_error_is_the_result() {
        let err = type_in_chunks("abc", || true, |_| Err(anyhow!("no display"))).unwrap_err();
        assert!(err.to_string().contains("no display"));
    }

    #[test]
    fn scroll_defaults_to_three_clicks() {
        assert_eq!(scroll_amount(&json!({ "action": "scroll" })), 3);
        assert_eq!(amount(json!(null)), 3);
        assert_eq!(amount(json!("10")), 3);
        assert_eq!(amount(json!(2.5)), 3);
        // Above i64::MAX serde_json holds a u64, which as_i64 refuses.
        assert_eq!(amount(json!(u64::MAX)), 3);
    }

    #[test]
    fn a_scroll_runs_every_click_asked_for_while_the_budget_lasts() {
        // Past the 1024 clicks a scroll used to be clamped to, both ways.
        for asked in [10_000_i64, -10_000, 1_025, 7, -7, 1, 0] {
            assert_eq!(amount(json!(asked)), asked);
            let (progress, steps) = scrolled(asked, || true);
            assert!(progress.finished(), "{asked}");
            assert_eq!(steps.iter().map(|&s| i64::from(s)).sum::<i64>(), asked);
            assert!(steps
                .iter()
                .all(|&s| s != 0 && u64::from(s.unsigned_abs()) <= SCROLL_STEP_CLICKS));
        }
    }

    #[test]
    fn scrolling_stops_between_steps_once_the_budget_ran_out() {
        let (progress, steps) = scrolled(-1_000, budget_of(3));
        assert_eq!(
            progress,
            Progress {
                done: 3 * SCROLL_STEP_CLICKS,
                total: 1_000
            }
        );
        assert_eq!(steps, vec![-(SCROLL_STEP_CLICKS as i32); 3]);
    }

    #[test]
    fn extreme_amounts_never_overflow() {
        // `as i32` turned 2^32 into 0 and negating i32::MIN overflows: every
        // step fits an i32 and negates, whatever the amount asked for.
        for asked in [i64::MIN, i64::MAX, 4_294_967_296, i64::from(i32::MIN)] {
            let (progress, steps) = scrolled(asked, budget_of(2));
            assert_eq!(progress.done, 2 * SCROLL_STEP_CLICKS, "{asked}");
            assert_eq!(progress.total, asked.unsigned_abs(), "{asked}");
            assert!(steps.iter().all(|s| s.checked_neg().is_some()));
            assert!(steps
                .iter()
                .all(|s| i64::from(s.signum()) == asked.signum()));
        }
    }
}
