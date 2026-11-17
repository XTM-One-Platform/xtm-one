#!/usr/bin/env node
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

/**
 * stdio -> Streamable HTTP bridge for the platform tools MCP server.
 *
 * Claude Desktop extensions (.mcpb) run a LOCAL server over stdio; the server
 * this bundle points at is remote and speaks MCP Streamable HTTP. This script
 * is the adapter between the two, and it is deliberately the whole of it:
 * no dependencies, no npx fetch at runtime, nothing to audit but this file.
 *
 * The transport it bridges to is request/response JSON, so the bridge is a
 * straight forward of each framed message: it mirrors what the 2026-07-28
 * revision wants in headers (MCP-Protocol-Version, Mcp-Method, Mcp-Name) and
 * relays an answer the server streams (subscriptions/listen, as SSE) event by
 * event, as each one arrives, never waiting for the stream to end. An event
 * that is not JSON is skipped, not the rest of the stream; a stream that ends
 * without answering its request is a transport failure. Two cases are not
 * straight:
 *
 *   - A notification (no ``id``) gets HTTP 202 and an empty body. There is no
 *     response to write back, and writing one would desynchronise the client.
 *   - A transport failure (unreachable, 401, 500) has no JSON-RPC answer of
 *     its own. Staying silent would hang the client on a request it can never
 *     resolve, so the failure is turned into a JSON-RPC error carrying the
 *     actual reason — an expired API key should say so, not time out.
 *
 * Configuration arrives as environment variables set by the bundle manifest
 * from the values Claude Desktop collected: XTM_MCP_URL and XTM_MCP_TOKEN.
 */

"use strict";

const ENDPOINT = process.env.XTM_MCP_URL;
const TOKEN = process.env.XTM_MCP_TOKEN;

if (!ENDPOINT) {
  process.stderr.write("XTM_MCP_URL is not set; nothing to connect to.\n");
  process.exit(1);
}

/** JSON-RPC error codes used for failures that never reached the server. */
const TRANSPORT_ERROR = -32000;
const PARSE_ERROR = -32700;

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function transportError(id, message) {
  send({ jsonrpc: "2.0", id, error: { code: TRANSPORT_ERROR, message } });
}

const META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion";

/** A header value as the 2026-07-28 transport writes it: plain, or Base64. */
function headerValue(value) {
  const text = String(value);
  const plain =
    /^[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?$/.test(text) &&
    !(text.startsWith("=?base64?") && text.endsWith("?="));
  return plain ? text : `=?base64?${Buffer.from(text, "utf8").toString("base64")}?=`;
}

/** The request headers the server checks against the body (2026-07-28). */
function requestHeaders(message) {
  const headers = {};
  const params = message.params && typeof message.params === "object" ? message.params : {};
  const meta = params._meta && typeof params._meta === "object" ? params._meta : {};
  if (typeof meta[META_PROTOCOL_VERSION] === "string") {
    headers["MCP-Protocol-Version"] = meta[META_PROTOCOL_VERSION];
  }
  if (typeof message.method === "string") headers["Mcp-Method"] = message.method;
  if (message.method === "tools/call" && typeof params.name === "string") {
    headers["Mcp-Name"] = headerValue(params.name);
  }
  return headers;
}

/** The data of one SSE event (its ``data:`` lines joined), empty if none. */
function sseData(event) {
  return event
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
}

const EVENT_BOUNDARY = /\r?\n\r?\n/;

/**
 * Relays an SSE answer to request ``id`` event by event, as the server sends
 * them. An event that is not JSON is skipped (and said on stderr); the
 * request still needs its answer, so a stream that ends - or breaks - before
 * answering it becomes a transport error instead of a client left waiting.
 */
async function relayStream(response, id) {
  let answered = false;
  const relay = (event) => {
    const data = sseData(event);
    if (!data) return;
    let message;
    try {
      message = JSON.parse(data);
    } catch {
      process.stderr.write("Skipped a streamed event that is not valid JSON.\n");
      return;
    }
    if (id !== undefined && message && message.id === id && !("method" in message)) {
      answered = true;
    }
    send(message);
  };
  const decoder = new TextDecoder();
  let pendingText = "";
  try {
    for await (const chunk of response.body) {
      pendingText += decoder.decode(chunk, { stream: true });
      let boundary;
      while ((boundary = EVENT_BOUNDARY.exec(pendingText)) !== null) {
        relay(pendingText.slice(0, boundary.index));
        pendingText = pendingText.slice(boundary.index + boundary[0].length);
      }
    }
    pendingText += decoder.decode();
    if (pendingText.trim()) relay(pendingText);
  } catch (err) {
    if (id !== undefined && !answered) {
      transportError(id, `The stream from ${ENDPOINT} broke: ${err.message}`);
    }
    return;
  }
  if (id !== undefined && !answered) {
    transportError(id, "The server's stream ended without answering the request.");
  }
}

async function forward(message) {
  // Only a request expects an answer; a notification's id is absent, and
  // ``null`` is the JSON-RPC id for "we could not even read yours".
  const id = Object.prototype.hasOwnProperty.call(message, "id")
    ? message.id
    : undefined;

  let response;
  try {
    response = await fetch(ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json, text/event-stream",
        ...requestHeaders(message),
        ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
      },
      body: JSON.stringify(message),
    });
  } catch (err) {
    if (id !== undefined) {
      transportError(id, `Cannot reach ${ENDPOINT}: ${err.message}`);
    }
    return;
  }

  // 202 with no body is how the server acknowledges a notification.
  if (response.status === 202) return;

  if (response.ok && (response.headers.get("content-type") || "").includes("text/event-stream")) {
    await relayStream(response, id);
    return;
  }

  const text = await response.text();

  if (!response.ok) {
    if (id === undefined) return;
    // The server's own message is the actionable part (disabled server,
    // expired key, AI turned off), so it is passed through rather than
    // replaced by a generic status line.
    let detail = text.trim();
    try {
      const parsed = JSON.parse(text);
      // A JSON-RPC error the server answered (an unsupported protocol version
      // listing the supported ones, a header mismatch, an unknown method) is
      // relayed as is: the client reads its code to retry or fall back.
      if (parsed && parsed.jsonrpc === "2.0" && parsed.error && typeof parsed.error.code === "number") {
        send({ ...parsed, id });
        return;
      }
      detail = parsed.detail || parsed.error?.message || detail;
    } catch {
      /* not JSON — the raw body is the best available detail */
    }
    transportError(
      id,
      `${response.status} from the platform tools server${detail ? `: ${detail}` : ""}`,
    );
    return;
  }

  if (!text.trim()) return;

  try {
    send(JSON.parse(text));
  } catch {
    if (id !== undefined) {
      transportError(id, "The server returned a body that is not valid JSON.");
    }
  }
}

// Messages are newline-delimited on stdin. Chunks split anywhere, so the tail
// after the last newline is held until the rest of that line arrives.
let buffer = "";

// Requests are forwarded concurrently, so end-of-input is not end-of-work:
// exiting on "end" alone drops whatever is still in flight and the client
// never sees those answers. Shutdown waits for the last one to settle.
let pending = 0;
let inputEnded = false;

function exitWhenDrained() {
  if (inputEnded && pending === 0) process.exit(0);
}

process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let newline;
  while ((newline = buffer.indexOf("\n")) !== -1) {
    const line = buffer.slice(0, newline).trim();
    buffer = buffer.slice(newline + 1);
    if (!line) continue;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      send({
        jsonrpc: "2.0",
        id: null,
        error: { code: PARSE_ERROR, message: "Parse error: invalid JSON-RPC." },
      });
      continue;
    }
    // Deliberately not awaited: MCP allows concurrent in-flight requests, and
    // serialising them here would make a slow tool block every later call.
    pending += 1;
    forward(message).finally(() => {
      pending -= 1;
      exitWhenDrained();
    });
  }
});

process.stdin.on("end", () => {
  inputEnded = true;
  exitWhenDrained();
});
