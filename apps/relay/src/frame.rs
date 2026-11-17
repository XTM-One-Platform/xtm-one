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

//! The relay wire format, byte for byte the platform's
//! (`backend/app/services/relay_transport.py`).
//!
//! A frame is `[u32 BE header length][JSON header][payload]`. The header always
//! carries a `type`; `len` (the payload length) is set whenever there is a
//! payload. One WebSocket message may carry one frame without `len` (the
//! payload is then the rest of the message) or several concatenated frames,
//! each with `len` - which is also the shape of a long-poll body and of a
//! `/frames` upload.

use anyhow::{anyhow, bail, Result};
use serde_json::{Map, Value};

/// Largest JSON header accepted.
pub const MAX_HEADER_BYTES: usize = 64 * 1024;
/// Largest payload of one `data` frame the platform accepts.
pub const MAX_FRAME_PAYLOAD_BYTES: usize = 256 * 1024;
/// Largest body of one `/frames` upload.
pub const MAX_UPLOAD_BODY_BYTES: usize = 8 * 1024 * 1024;
/// Response headers passed on at most.
pub const MAX_RESPONSE_HEADERS: usize = 200;
/// Longest response header value passed on.
pub const MAX_HEADER_VALUE_CHARS: usize = 16 * 1024;
/// Longest error message the platform keeps.
pub const MAX_ERROR_MESSAGE_CHARS: usize = 500;

/// One frame: its JSON header and its payload.
#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub header: Map<String, Value>,
    pub payload: Vec<u8>,
}

impl Frame {
    /// A frame from a header built with `serde_json::json!` (an object).
    pub fn new(header: Value, payload: Vec<u8>) -> Self {
        let header = match header {
            Value::Object(map) => map,
            _ => Map::new(),
        };
        Self { header, payload }
    }

    /// The `type` of the frame (`""` when absent).
    pub fn kind(&self) -> &str {
        self.header
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("")
    }

    /// The request a frame belongs to.
    pub fn sid(&self) -> Option<&str> {
        self.header.get("sid").and_then(Value::as_str)
    }

    /// A string field of the header.
    pub fn str_field(&self, name: &str) -> Option<&str> {
        self.header.get(name).and_then(Value::as_str)
    }

    /// An unsigned integer field of the header (booleans are not integers).
    pub fn u64_field(&self, name: &str) -> Option<u64> {
        match self.header.get(name) {
            Some(Value::Number(n)) => n.as_u64(),
            _ => None,
        }
    }

    /// The frame on the wire: `len` is set exactly when there is a payload, so
    /// the platform reads a frame of ours the way it reads its own.
    pub fn encode(&self) -> Result<Vec<u8>> {
        let mut head = self.header.clone();
        head.remove("len");
        if !self.payload.is_empty() {
            head.insert("len".into(), Value::from(self.payload.len()));
        }
        Self::write(&head, &self.payload)
    }

    /// The frame as one of several concatenated ones: `len` always set.
    fn encode_counted(&self) -> Result<Vec<u8>> {
        let mut head = self.header.clone();
        head.insert("len".into(), Value::from(self.payload.len()));
        Self::write(&head, &self.payload)
    }

    fn write(head: &Map<String, Value>, payload: &[u8]) -> Result<Vec<u8>> {
        let raw = serde_json::to_vec(head)?;
        if raw.len() > MAX_HEADER_BYTES {
            bail!("frame header too large");
        }
        let mut out = Vec::with_capacity(4 + raw.len() + payload.len());
        out.extend_from_slice(&(raw.len() as u32).to_be_bytes());
        out.extend_from_slice(&raw);
        out.extend_from_slice(payload);
        Ok(out)
    }
}

/// Concatenated frames, each carrying `len` (a WebSocket message of several
/// frames, a `/frames` upload).
pub fn encode_frames(frames: &[Frame]) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    for frame in frames {
        out.extend(frame.encode_counted()?);
    }
    Ok(out)
}

/// Read one frame at `offset`; returns it and the offset after it.
///
/// A header without `len` owns the rest of `data` (one frame per WebSocket
/// message); concatenated frames must each carry `len`.
pub fn decode_frame(data: &[u8], offset: usize) -> Result<(Frame, usize)> {
    if data.len().saturating_sub(offset) < 4 {
        bail!("truncated frame");
    }
    let header_len =
        u32::from_be_bytes(data[offset..offset + 4].try_into().expect("4 bytes")) as usize;
    if header_len == 0 || header_len > MAX_HEADER_BYTES {
        bail!("invalid frame header length");
    }
    let start = offset + 4;
    let end = start + header_len;
    if end > data.len() {
        bail!("truncated frame header");
    }
    let header: Value = serde_json::from_slice(&data[start..end])
        .map_err(|_| anyhow!("frame header is not JSON"))?;
    let Value::Object(header) = header else {
        bail!("frame header has no type");
    };
    if !matches!(header.get("type"), Some(Value::String(_))) {
        bail!("frame header has no type");
    }
    let payload_end = match header.get("len") {
        None | Some(Value::Null) => data.len(),
        Some(Value::Number(n)) => {
            let length = n
                .as_u64()
                .ok_or_else(|| anyhow!("invalid frame payload length"))?
                as usize;
            let payload_end = end
                .checked_add(length)
                .ok_or_else(|| anyhow!("invalid frame payload length"))?;
            if payload_end > data.len() {
                bail!("truncated frame payload");
            }
            payload_end
        }
        Some(_) => bail!("invalid frame payload length"),
    };
    Ok((
        Frame {
            header,
            payload: data[end..payload_end].to_vec(),
        },
        payload_end,
    ))
}

/// Every frame of a body of concatenated frames.
pub fn decode_frames(data: &[u8]) -> Result<Vec<Frame>> {
    let mut frames = Vec::new();
    let mut offset = 0;
    while offset < data.len() {
        let (frame, next) = decode_frame(data, offset)?;
        frames.push(frame);
        offset = next;
    }
    Ok(frames)
}

/// `message` cut to what the platform keeps, on a character boundary.
pub fn clip_message(message: &str) -> String {
    message.chars().take(MAX_ERROR_MESSAGE_CHARS).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_frame_round_trips_and_carries_len_only_with_a_payload() {
        let bare = Frame::new(json!({"type": "end", "sid": "a"}), vec![]);
        let wire = bare.encode().unwrap();
        let header_len = u32::from_be_bytes(wire[..4].try_into().unwrap()) as usize;
        let header: Value = serde_json::from_slice(&wire[4..4 + header_len]).unwrap();
        assert!(header.get("len").is_none());
        assert_eq!(decode_frame(&wire, 0).unwrap().0, bare);

        let data = Frame::new(json!({"type": "data", "sid": "a"}), b"hello".to_vec());
        let (decoded, end) = decode_frame(&data.encode().unwrap(), 0).unwrap();
        assert_eq!(decoded.payload, b"hello");
        assert_eq!(decoded.u64_field("len"), Some(5));
        assert_eq!(end, data.encode().unwrap().len());
    }

    #[test]
    fn concatenated_frames_each_carry_len() {
        let frames = vec![
            Frame::new(
                json!({"type": "response_head", "sid": "a", "status": 200}),
                vec![],
            ),
            Frame::new(json!({"type": "data", "sid": "a"}), b"abc".to_vec()),
            Frame::new(json!({"type": "end", "sid": "a"}), vec![]),
        ];
        let decoded = decode_frames(&encode_frames(&frames).unwrap()).unwrap();
        assert_eq!(decoded.len(), 3);
        assert_eq!(decoded[0].u64_field("len"), Some(0));
        assert_eq!(decoded[1].payload, b"abc");
        assert_eq!(decoded[2].kind(), "end");
    }

    #[test]
    fn a_frame_without_len_owns_the_rest_of_the_message() {
        let header = br#"{"type":"open","sid":"x"}"#;
        let mut wire = (header.len() as u32).to_be_bytes().to_vec();
        wire.extend_from_slice(header);
        wire.extend_from_slice(b"inline body");
        let (frame, end) = decode_frame(&wire, 0).unwrap();
        assert_eq!(frame.payload, b"inline body");
        assert_eq!(end, wire.len());
    }

    #[test]
    fn malformed_frames_are_refused() {
        assert!(decode_frame(b"\x00\x00", 0).is_err());
        assert!(decode_frame(b"\x00\x00\x00\x00", 0).is_err());
        assert!(decode_frame(b"\x00\x00\x00\x09{\"a\":1}", 0).is_err());
        let no_type = br#"{"sid":"x"}"#;
        let mut wire = (no_type.len() as u32).to_be_bytes().to_vec();
        wire.extend_from_slice(no_type);
        assert!(decode_frame(&wire, 0).is_err());
        let long = br#"{"type":"data","len":99}"#;
        let mut wire = (long.len() as u32).to_be_bytes().to_vec();
        wire.extend_from_slice(long);
        assert!(decode_frame(&wire, 0).is_err());
        let negative = br#"{"type":"data","len":-1}"#;
        let mut wire = (negative.len() as u32).to_be_bytes().to_vec();
        wire.extend_from_slice(negative);
        assert!(decode_frame(&wire, 0).is_err());
    }

    #[test]
    fn a_boolean_is_not_an_integer_field() {
        let frame = Frame::new(json!({"type": "window", "increment": true}), vec![]);
        assert_eq!(frame.u64_field("increment"), None);
    }

    #[test]
    fn an_error_message_is_clipped_on_a_character_boundary() {
        let long = "é".repeat(MAX_ERROR_MESSAGE_CHARS + 10);
        assert_eq!(clip_message(&long).chars().count(), MAX_ERROR_MESSAGE_CHARS);
    }
}
