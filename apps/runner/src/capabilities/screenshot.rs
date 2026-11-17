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

//! Full-desktop screenshot capture (the audit-evidence primary use case).
//!
//! Captures the ENTIRE screen through `xcap` - OS clock/taskbar, signed-in
//! identity, real browser chrome - not a headless render. `display` selects the
//! primary monitor (default), a specific monitor index, or `all` for a single
//! composited image of the whole virtual desktop.

use std::io::Cursor;

use anyhow::{anyhow, Context, Result};
use image::{DynamicImage, ImageFormat, RgbaImage};
use sha2::{Digest, Sha256};
use xcap::Monitor;

use crate::capabilities::permissions::{ensure, Permission};
use crate::protocol::{CommandOutcome, FilePart};

pub fn capture(payload: &serde_json::Value) -> CommandOutcome {
    // macOS returns a black/wallpaper frame instead of erroring when Screen
    // Recording is not granted - a silent, evidence-invalidating failure. Refuse
    // up front with an actionable message (and raise the OS prompt) rather than
    // filing a worthless capture.
    if let Err(msg) = ensure(Permission::ScreenRecording) {
        return CommandOutcome::failed(msg);
    }

    let display = payload
        .get("display")
        .and_then(|v| v.as_str())
        .unwrap_or("primary")
        .to_string();

    match capture_inner(&display) {
        Ok(outcome) => outcome,
        Err(err) => CommandOutcome::failed(format!("screenshot failed: {err:#}{}", capture_hint())),
    }
}

/// OS-specific tail appended to a capture failure. On Linux the usual cause is a
/// Wayland session with no screencast portal (or a headless/SSH login with no
/// graphical session), which the generic `xcap` error does not spell out.
#[cfg(target_os = "linux")]
fn capture_hint() -> String {
    let session = std::env::var("XDG_SESSION_TYPE").unwrap_or_default();
    if session.eq_ignore_ascii_case("wayland") {
        " (Wayland session: full-desktop capture needs a running \
         xdg-desktop-portal with the ScreenCast backend, e.g. \
         xdg-desktop-portal-wlr/-gnome/-kde. Ensure the runner runs inside your \
         graphical login, not an SSH/headless shell.)"
            .to_string()
    } else {
        " (Ensure the runner runs inside your active X11/Wayland graphical login \
         - a session-0 service or SSH/headless shell captures nothing. On \
         Wayland, install an xdg-desktop-portal ScreenCast backend.)"
            .to_string()
    }
}

#[cfg(not(target_os = "linux"))]
fn capture_hint() -> String {
    String::new()
}

fn capture_inner(display: &str) -> Result<CommandOutcome> {
    let monitors = Monitor::all().map_err(|e| anyhow!("enumerating monitors: {e}"))?;
    if monitors.is_empty() {
        return Err(anyhow!("no monitors detected"));
    }

    let (image, monitor_label) = match display {
        "all" => (capture_all(&monitors)?, "all".to_string()),
        "primary" => {
            let m = monitors
                .iter()
                .find(|m| m.is_primary().unwrap_or(false))
                .unwrap_or(&monitors[0]);
            (
                m.capture_image().map_err(|e| anyhow!("capture: {e}"))?,
                m.name().unwrap_or_else(|_| "primary".into()),
            )
        }
        other => {
            let index: usize = other.parse().with_context(|| {
                format!("display '{other}' is not 'primary', 'all', or a monitor index")
            })?;
            let m = monitors.get(index).ok_or_else(|| {
                anyhow!(
                    "monitor index {index} out of range (have {})",
                    monitors.len()
                )
            })?;
            (
                m.capture_image().map_err(|e| anyhow!("capture: {e}"))?,
                m.name().unwrap_or_else(|_| format!("{index}")),
            )
        }
    };

    let width = image.width();
    let height = image.height();

    let mut buf = Vec::new();
    DynamicImage::ImageRgba8(image)
        .write_to(&mut Cursor::new(&mut buf), ImageFormat::Png)
        .context("encoding PNG")?;

    let now = chrono::Utc::now();
    let now_local = chrono::Local::now();
    // Integrity red-line: hash the EXACT encoded bytes at the moment of
    // capture, on the capturing machine. The platform re-hashes what it stores
    // and records whether the two match, so any in-transit or at-rest
    // alteration of the evidence is detectable.
    let sha256 = hex::encode(Sha256::digest(&buf));
    let result = serde_json::json!({
        "captured_at": now.to_rfc3339(),
        "captured_at_local": now_local.to_rfc3339(),
        "display": display,
        "monitor": monitor_label,
        "width": width,
        "height": height,
        "bytes": buf.len(),
        "sha256": sha256,
    });

    let filename = format!("desktop-{}.png", now.format("%Y%m%dT%H%M%SZ"));
    Ok(CommandOutcome::completed_file(
        result,
        FilePart {
            bytes: buf,
            filename,
            content_type: "image/png".to_string(),
        },
    ))
}

/// Composite every monitor into one image of the whole virtual desktop, placing
/// each capture at its virtual-screen offset.
fn capture_all(monitors: &[Monitor]) -> Result<RgbaImage> {
    let mut min_x = i32::MAX;
    let mut min_y = i32::MAX;
    let mut max_x = i32::MIN;
    let mut max_y = i32::MIN;

    let mut shots = Vec::with_capacity(monitors.len());
    for m in monitors {
        let x = m.x().map_err(|e| anyhow!("monitor x: {e}"))?;
        let y = m.y().map_err(|e| anyhow!("monitor y: {e}"))?;
        let img = m.capture_image().map_err(|e| anyhow!("capture: {e}"))?;
        let w = img.width() as i32;
        let h = img.height() as i32;
        min_x = min_x.min(x);
        min_y = min_y.min(y);
        max_x = max_x.max(x + w);
        max_y = max_y.max(y + h);
        shots.push((x, y, img));
    }

    let total_w = (max_x - min_x).max(1) as u32;
    let total_h = (max_y - min_y).max(1) as u32;
    let mut canvas = RgbaImage::new(total_w, total_h);
    for (x, y, img) in shots {
        let dx = (x - min_x) as i64;
        let dy = (y - min_y) as i64;
        image::imageops::overlay(&mut canvas, &img, dx, dy);
    }
    Ok(canvas)
}
