/**
 * Embeddable Chat Widget
 *
 * Drop-in JavaScript widget that adds an AI assistant chat to any website.
 * Uses Shadow DOM for full style isolation. Branded dynamically from
 * ``/api/v1/public/chat/{token}/config`` (``platform_name``) so the same
 * static asset works for any deployment mode and any admin-customised
 * platform name.
 *
 * Usage:
 *   <script
 *     src="https://your-xtm-one.example.com/api/v1/public/widget.js"
 *     data-token="YOUR_WEB_TOKEN"
 *     data-context='{"page":"docs","product":"OpenCTI","version":"6.4"}'
 *   ></script>
 */
(function () {
  "use strict";

  // ── Configuration ──────────────────────────────────────────────────────
  const script = document.currentScript;
  if (!script) return;

  const TOKEN = script.getAttribute("data-token");
  if (!TOKEN) {
    console.error("[chat widget] Missing data-token attribute.");
    return;
  }

  // Optional JSON context from the embedding page (e.g. current page, product, user role)
  var CONTEXT = null;
  var _ctxRaw = script.getAttribute("data-context");
  if (_ctxRaw) {
    try { CONTEXT = JSON.parse(_ctxRaw); } catch (e) {
      console.warn("[chat widget] Invalid data-context JSON, ignoring.", e);
    }
  }

  // api-base:start
  // The embed snippet loads ``<platform>/api/v1/public/widget.js``: whatever
  // precedes ``/api/v1/public/widget.js`` in the script's path is the URL path
  // prefix the platform is served under (``https://host/xtm-one``). A
  // script served from any other path - a copy behind the embedding site's own
  // proxy - keeps the API at its origin's ``/api/v1/public``.
  const SCRIPT_URL = new URL(script.src);
  const PREFIX_MATCH = /^(.*)\/api\/v1\/public\/widget\.js$/.exec(SCRIPT_URL.pathname);
  const API = SCRIPT_URL.origin + (PREFIX_MATCH ? PREFIX_MATCH[1] : "") + "/api/v1/public";
  // api-base:end
  // The platform itself, BASE_PATH included: in strict rendering an answer's
  // images are shown only when the platform serves them.
  const PLATFORM_ROOT = API.replace(/\/api\/v1\/public$/, "");
  // Set from the public config (PUBLIC_WIDGET_STRICT_RENDERING) before any
  // answer is rendered.
  var STRICT_RENDERING = false;
  // What a visitor may attach (accept, max_bytes, max_files), from the public
  // config; null when the widget takes no files.
  var UPLOADS = null;

  const LOGO_URL = API + "/logo.svg";
  const SESSION_KEY = "filigran_copilot_session_" + TOKEN;
  const VISITOR_KEY = "filigran_copilot_visitor_" + TOKEN;
  const VISITOR_COOKIE = "fc_vid_" + TOKEN.slice(0, 8);
  const SIZE_KEY = "filigran_copilot_size_" + TOKEN;

  // ── Design tokens ─────────────────────────────────────────────────────
  // One dark palette on purpose: the widget is a self-contained
  // script embedded in third-party pages, so it cannot read the platform's
  // design-system tokens or the viewer's theme preference. Its colours are
  // literal values kept here, and every text pair on them reads at 4.5:1.
  const T = {
    bg: "#0a0f1e",
    bgPanel: "#0d1225",
    card: "#111830",
    cardBorder: "#1a2342",
    fg: "#ffffff",
    fgSoft: "#e0e4ef",
    fgMuted: "#8b92a8",
    purple: "#9575cd",
    purpleHover: "#a68ad9",
    purpleDark: "#7e57c2",
    purpleDeep: "#5e3d9e",
    purpleGlow: "rgba(149,117,205,0.25)",
    purpleSoft: "rgba(149,117,205,0.08)",
    purpleBorder: "rgba(149,117,205,0.2)",
    border: "#1e2744",
    borderLight: "#2a3456",
    input: "#111830",
    inputBorder: "#1e2744",
    inputFocus: "#9575cd",
    danger: "#f87171",
    radius: "4px",
    radiusSm: "2px",
    shadow: "0 20px 60px rgba(0,0,0,0.5), 0 8px 24px rgba(0,0,0,0.3)",
    shadowGlow: "0 0 40px rgba(149,117,205,0.12)",
    fontTitle: "'Geologica','IBM Plex Sans',system-ui,-apple-system,sans-serif",
    fontBody: "'IBM Plex Sans','Geologica',system-ui,-apple-system,sans-serif",
  };

  // ── Default & max panel dimensions ─────────────────────────────────────
  var DEFAULT_W = 400;
  var DEFAULT_H = 600;
  var panelW = DEFAULT_W;
  var panelH = DEFAULT_H;
  var MIN_W = 320;
  var MAX_W = 700;
  var MIN_H = 400;
  var MAX_H = 850;

  // ── State ─────────────────────────────────────────────────────────────
  var isOpen = false;
  var sessionId = null;
  var messages = [];
  var agentConfig = null;
  var isLoading = false;
  var isMaximized = false;
  var thinkingPhase = 0;
  var thinkingTextBuffer = "";
  // The reasoning box shows its last few lines; a long turn streams hundreds
  // of KB of reasoning, so only a bounded tail is kept and laid out.
  var THINKING_TEXT_MAX_CHARS = 2000;
  var thinkingTimer = null;
  // Files the visitor attached and has not sent yet: { file, uploaded }, where
  // uploaded is the server's answer once the file is stored.
  var pendingFiles = [];

  // ── Load saved panel size ─────────────────────────────────────────────
  (function loadSize() {
    try {
      var s = localStorage.getItem(SIZE_KEY);
      if (s) {
        var p = JSON.parse(s);
        if (p.w >= MIN_W && p.w <= MAX_W) panelW = p.w;
        if (p.h >= MIN_H && p.h <= MAX_H) panelH = p.h;
      }
    } catch (e) {}
  })();

  // ── Shadow DOM host ───────────────────────────────────────────────────
  var host = document.createElement("div");
  host.id = "filigran-copilot-widget";
  host.style.cssText = "position:fixed;z-index:2147483647;bottom:0;right:0;pointer-events:none;";
  document.body.appendChild(host);
  var shadow = host.attachShadow({ mode: "open" });

  // ── Styles ────────────────────────────────────────────────────────────
  // Load fonts via <link> instead of @import to avoid blocking Shadow DOM
  // style parsing (causes flash on Firefox/Ubuntu).
  var fontLink = document.createElement("link");
  fontLink.rel = "stylesheet";
  fontLink.href = "https://fonts.googleapis.com/css2?family=Geologica:wght@400;500;600;700&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;0,700&display=swap";
  shadow.appendChild(fontLink);

  var style = document.createElement("style");
  style.textContent = `
    :host { all: initial; font-family: ${T.fontBody}; }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    .fc-widget { pointer-events: auto; }

    /* ── Floating button ─────────────────────────────────────── */
    .fc-btn {
      position: fixed; bottom: 24px; right: 24px;
      width: 56px; height: 56px; border-radius: 50%;
      background: ${T.purple};
      border: 2px solid rgba(149,117,205,0.4);
      cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 4px 20px ${T.purpleGlow}, ${T.shadowGlow};
      transition: transform 0.3s cubic-bezier(0.34,1.56,0.64,1),
                  box-shadow 0.3s cubic-bezier(0.34,1.56,0.64,1),
                  background 0.3s cubic-bezier(0.34,1.56,0.64,1);
      z-index: 2147483647;
      padding: 0;
      overflow: hidden;
      opacity: 0;
      pointer-events: none;
    }
    .fc-btn.fc-ready {
      opacity: 1;
      pointer-events: auto;
      animation: fc-btn-entrance 0.4s cubic-bezier(0.34,1.56,0.64,1) both;
    }
    @keyframes fc-btn-entrance {
      from { opacity: 0; transform: scale(0.5) translateY(12px); }
      to   { opacity: 1; transform: scale(1) translateY(0); }
    }
    .fc-btn:hover {
      transform: scale(1.08);
      box-shadow: 0 6px 28px rgba(149,117,205,0.4), 0 0 50px rgba(149,117,205,0.15);
      background: ${T.purpleHover};
    }
    .fc-btn:active {
      transform: scale(0.96);
    }
    .fc-btn .fc-btn-logo {
      width: 32px; height: 32px;
      object-fit: contain;
      filter: brightness(1.1);
      transition: opacity 0.25s, transform 0.25s;
    }
    .fc-btn .fc-btn-close {
      position: absolute;
      width: 22px; height: 22px;
      opacity: 0; transform: rotate(-90deg) scale(0.5);
      transition: opacity 0.25s, transform 0.25s;
    }
    .fc-btn .fc-btn-close line {
      stroke: white; stroke-width: 2.2; stroke-linecap: round;
    }
    .fc-btn.open .fc-btn-logo {
      opacity: 0; transform: rotate(90deg) scale(0.5);
    }
    .fc-btn.open .fc-btn-close {
      opacity: 1; transform: rotate(0deg) scale(1);
    }

    /* ── Panel ───────────────────────────────────────────────── */
    .fc-panel {
      position: fixed; bottom: 96px; right: 24px;
      width: ${DEFAULT_W}px; max-width: calc(100vw - 32px);
      height: ${DEFAULT_H}px; max-height: calc(100vh - 120px);
      background: ${T.bgPanel};
      border: 1px solid ${T.border};
      border-radius: ${T.radius};
      box-shadow: ${T.shadow};
      display: flex; flex-direction: column;
      overflow: hidden;
      opacity: 0; transform: translateY(12px) scale(0.97);
      transition: opacity 0.3s cubic-bezier(0.4,0,0.2,1),
                  transform 0.3s cubic-bezier(0.4,0,0.2,1);
      pointer-events: none;
      z-index: 2147483646;
    }
    .fc-panel.visible {
      opacity: 1; transform: translateY(0) scale(1);
      pointer-events: auto;
    }
    .fc-panel.maximized {
      bottom: 24px !important;
      width: calc(100vw - 48px) !important;
      max-width: calc(100vw - 48px) !important;
      height: calc(100vh - 48px) !important;
      max-height: calc(100vh - 48px) !important;
      border-radius: 8px;
    }

    /* ── Resize handle (top-left corner) ─────────────────── */
    .fc-resize {
      position: absolute; top: 0; left: 0;
      width: 20px; height: 20px;
      cursor: nw-resize; z-index: 20;
      border-radius: ${T.radius} 0 0 0;
    }
    .fc-resize::after {
      content: '';
      position: absolute; top: 5px; left: 5px;
      width: 8px; height: 8px;
      border-top: 2px solid ${T.fgMuted};
      border-left: 2px solid ${T.fgMuted};
      border-radius: 2px 0 0 0;
      opacity: 0;
      transition: opacity 0.2s;
    }
    .fc-panel:hover .fc-resize::after,
    .fc-resize:hover::after {
      opacity: 0.6;
    }
    .fc-panel.maximized .fc-resize {
      display: none;
    }

    /* ── Header ──────────────────────────────────────────────── */
    .fc-header {
      display: flex; align-items: center; gap: 12px;
      padding: 14px 16px;
      background: ${T.bg};
      border-bottom: 1px solid ${T.border};
      flex-shrink: 0;
      -webkit-user-select: none; user-select: none;
    }
    .fc-header-icon {
      width: 34px; height: 34px; border-radius: 50%;
      background: ${T.purple};
      display: flex; align-items: center; justify-content: center;
      flex-shrink: 0;
      box-shadow: 0 0 12px ${T.purpleGlow};
    }
    .fc-header-icon img {
      width: 20px; height: 20px; object-fit: contain;
    }
    .fc-header-info { flex: 1; min-width: 0; }
    .fc-header-title {
      font-family: ${T.fontTitle};
      font-weight: 600; font-size: 14px; color: ${T.fg};
      letter-spacing: -0.01em;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .fc-header-status {
      font-size: 11px; color: ${T.fgMuted}; margin-top: 1px;
      display: flex; align-items: center; gap: 5px;
    }
    .fc-header-dot {
      width: 6px; height: 6px; border-radius: 50%;
      background: #4caf50;
      box-shadow: 0 0 6px rgba(76,175,80,0.5);
    }
    .fc-header-actions { display: flex; gap: 2px; }
    .fc-header-actions button {
      width: 30px; height: 30px; border-radius: 4px;
      background: transparent; border: none;
      cursor: pointer; display: flex; align-items: center; justify-content: center;
      transition: background 0.2s, opacity 0.2s;
      color: ${T.fgMuted};
    }
    .fc-header-actions button:hover {
      background: rgba(255,255,255,0.06);
      color: ${T.fg};
    }
    .fc-header-actions button svg {
      width: 15px; height: 15px;
      stroke: currentColor; fill: none;
      stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
    }
    .fc-header-actions button.fc-hidden {
      display: none;
    }

    /* ── Messages ─────────────────────────────────────────────── */
    .fc-messages {
      flex: 1; overflow-y: auto; padding: 16px 14px;
      display: flex; flex-direction: column; gap: 10px;
      scrollbar-width: thin;
      scrollbar-color: transparent transparent;
    }
    .fc-messages:hover {
      scrollbar-color: rgba(42,52,86,0.6) transparent;
    }
    .fc-messages::-webkit-scrollbar { width: 5px; }
    .fc-messages::-webkit-scrollbar-track { background: transparent; }
    .fc-messages::-webkit-scrollbar-thumb {
      background: transparent;
      border-radius: 10px;
    }
    .fc-messages:hover::-webkit-scrollbar-thumb {
      background: rgba(42,52,86,0.7);
    }
    .fc-messages::-webkit-scrollbar-thumb:hover {
      background: rgba(42,52,86,1);
    }

    .fc-msg {
      max-width: 85%; padding: 10px 14px;
      font-size: 13.5px; line-height: 1.6;
      word-wrap: break-word;
      animation: fc-fade-in 0.3s ease-out;
    }
    @keyframes fc-fade-in {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .fc-msg.user {
      align-self: flex-end;
      background: ${T.purple}; color: #fff;
      border-radius: 4px 4px 1px 4px;
      font-weight: 500;
    }
    .fc-msg.assistant {
      align-self: flex-start;
      background: ${T.card}; color: ${T.fgSoft};
      border: 1px solid ${T.cardBorder};
      border-radius: 4px 4px 4px 1px;
    }
    .fc-msg.assistant p { margin-bottom: 8px; }
    .fc-msg.assistant p:last-child { margin-bottom: 0; }
    .fc-msg.assistant strong { color: ${T.purple}; font-weight: 600; }
    .fc-msg.assistant em { color: ${T.fgSoft}; font-style: italic; }
    .fc-msg.assistant code {
      background: rgba(149,117,205,0.1); color: ${T.purpleHover};
      padding: 1px 6px; border-radius: 2px;
      font-size: 12px; font-family: 'SF Mono','Fira Code',monospace;
    }
    .fc-msg.assistant pre {
      background: ${T.bg}; padding: 12px 14px;
      border-radius: 4px; border: 1px solid ${T.border};
      overflow-x: auto; margin: 8px 0;
      font-size: 12px; font-family: 'SF Mono','Fira Code',monospace;
      line-height: 1.5;
    }
    .fc-msg.assistant pre code {
      background: none; padding: 0; color: ${T.fgSoft};
    }
    .fc-msg.assistant ul, .fc-msg.assistant ol {
      padding-left: 18px; margin: 6px 0;
    }
    .fc-msg.assistant li { margin-bottom: 3px; }
    .fc-msg.assistant a {
      color: ${T.purple}; text-decoration: underline;
      text-underline-offset: 2px;
    }
    .fc-msg.assistant a:hover { color: ${T.purpleHover}; }
    .fc-msg.assistant img.fc-img {
      display: block; max-width: 100%; height: auto;
      margin: 6px 0; border-radius: ${T.radius};
    }
    .fc-msg .fc-files {
      display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px;
    }
    .fc-msg a.fc-file {
      display: inline-block; max-width: 100%; padding: 4px 10px;
      border: 1px solid ${T.cardBorder}; border-radius: ${T.radiusSm};
      text-decoration: none; font-size: 12px;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .fc-msg.user a.fc-file {
      color: #fff; border-color: rgba(255,255,255,0.35);
    }
    .fc-msg.assistant h1, .fc-msg.assistant h2, .fc-msg.assistant h3 {
      font-family: ${T.fontTitle}; color: ${T.fg}; letter-spacing: -0.01em;
    }
    .fc-msg.assistant h1 { font-size: 18px; font-weight: 700; margin: 12px 0 6px; }
    .fc-msg.assistant h2 { font-size: 15px; font-weight: 600; margin: 10px 0 4px; }
    .fc-msg.assistant h3 { font-size: 13.5px; font-weight: 600; margin: 8px 0 4px; }
    .fc-msg.assistant h1:first-child,
    .fc-msg.assistant h2:first-child,
    .fc-msg.assistant h3:first-child { margin-top: 0; }
    .fc-msg.assistant blockquote {
      border-left: 3px solid ${T.purple}; padding: 6px 12px; margin: 8px 0;
      color: ${T.fgMuted}; background: rgba(149,117,205,0.05);
      border-radius: 0 ${T.radiusSm} ${T.radiusSm} 0;
    }
    .fc-msg.assistant table {
      width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 12.5px;
    }
    .fc-msg.assistant th, .fc-msg.assistant td {
      border: 1px solid ${T.border}; padding: 6px 10px; text-align: left;
    }
    .fc-msg.assistant th {
      background: ${T.bg}; color: ${T.fg}; font-weight: 600; font-size: 12px;
    }
    .fc-msg.assistant td { color: ${T.fgSoft}; }
    .fc-msg.assistant hr {
      border: none; border-top: 1px solid ${T.border}; margin: 10px 0;
    }
    .fc-msg.assistant del { color: ${T.fgMuted}; }
    .fc-msg.assistant ol { list-style-type: decimal; }

    /* ── Greeting ─────────────────────────────────────────────── */
    .fc-greeting {
      text-align: center; padding: 40px 24px 20px;
      animation: fc-fade-in 0.5s ease-out;
    }
    .fc-greeting-icon {
      width: 52px; height: 52px; border-radius: 50%;
      background: ${T.purpleSoft};
      border: 1px solid ${T.purpleBorder};
      display: inline-flex; align-items: center; justify-content: center;
      margin-bottom: 16px;
      box-shadow: 0 0 24px rgba(149,117,205,0.1);
    }
    .fc-greeting-icon img {
      width: 28px; height: 28px; object-fit: contain;
      /* Make the white logo visible on the soft purple bg */
      filter: brightness(0) saturate(100%) invert(58%) sepia(50%) saturate(400%) hue-rotate(230deg);
    }
    .fc-greeting h3 {
      font-family: ${T.fontTitle}; font-weight: 700;
      font-size: 17px; color: ${T.fg}; margin-bottom: 8px;
      letter-spacing: -0.02em;
    }
    .fc-greeting p {
      font-size: 13px; color: ${T.fgMuted}; line-height: 1.6;
      max-width: 280px; margin: 0 auto;
    }

    /* ── Suggested prompts ─────────────────────────────────── */
    .fc-suggestions {
      display: flex; flex-direction: column; gap: 6px;
      padding: 12px 0 0; margin-top: 16px;
      max-width: 300px; margin-left: auto; margin-right: auto;
    }
    .fc-suggestion-btn {
      background: ${T.purpleSoft};
      border: 1px solid ${T.purpleBorder};
      border-radius: 20px; padding: 8px 16px;
      font-family: ${T.fontBody}; font-size: 12.5px;
      color: ${T.fgSoft}; cursor: pointer;
      transition: all 0.2s;
      text-align: left; line-height: 1.4;
    }
    .fc-suggestion-btn:hover {
      background: rgba(149,117,205,0.15);
      border-color: ${T.purple};
      color: ${T.fg};
      box-shadow: 0 0 12px rgba(149,117,205,0.1);
    }
    .fc-suggestion-btn:active {
      transform: scale(0.98);
    }

    /* ── Thinking / reflection indicator ────────────────────── */
    .fc-thinking-text {
      margin-top: 6px;
      max-height: 4.5em;
      line-height: 1.35em;
      overflow: hidden;
      position: relative;
      font-size: 12px;
      color: ${T.fgMuted};
      opacity: 0.6;
      word-break: break-word;
      border-left: 2px solid ${T.purpleBorder};
      padding-left: 10px;
      background: ${T.purpleSoft};
      border-radius: 0 4px 4px 0;
      padding: 6px 10px 6px 10px;
      animation: fc-reasoning-glow 3s ease-in-out infinite;
    }
    .fc-thinking-text:empty { display: none; }
    /* The box is pinned to its newest line: once it overflows, the oldest
       lines dissolve at the top and the line being written stays sharp. */
    .fc-thinking-text.fc-overflowing {
      -webkit-mask-image: linear-gradient(to bottom, transparent 0, black 1.6em);
      mask-image: linear-gradient(to bottom, transparent 0, black 1.6em);
    }
    @keyframes fc-reasoning-glow {
      0%, 100% { border-color: rgba(149,117,205,0.15); box-shadow: inset 2px 0 8px -4px rgba(149,117,205,0.08); }
      50% { border-color: rgba(149,117,205,0.4); box-shadow: inset 2px 0 12px -4px rgba(149,117,205,0.2); }
    }
    .fc-thinking {
      align-self: flex-start;
      display: flex; flex-direction: column; gap: 4px;
      padding: 12px 16px; border-radius: 4px 4px 4px 1px;
      background: ${T.card}; border: 1px solid ${T.cardBorder};
      position: relative; overflow: clip;
      animation: fc-fade-in 0.3s ease-out;
    }
    .fc-thinking::before {
      content: '';
      position: absolute; inset: 0;
      background: linear-gradient(90deg, transparent 0%, rgba(149,117,205,0.06) 50%, transparent 100%);
      animation: fc-shimmer 2.5s ease-in-out infinite;
      pointer-events: none;
    }
    @keyframes fc-shimmer {
      0% { transform: translateX(-100%); }
      100% { transform: translateX(100%); }
    }
    .fc-thinking-icon {
      display: flex; align-items: center; justify-content: center;
      width: 16px; height: 16px; flex-shrink: 0;
      position: relative; z-index: 1;
      color: ${T.purple};
    }
    .fc-thinking-icon svg {
      width: 14px; height: 14px;
      stroke: currentColor; fill: none;
      stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
    }
    .fc-thinking-icon.fc-pulse svg {
      animation: fc-icon-pulse 1.5s ease-in-out infinite;
    }
    @keyframes fc-icon-pulse {
      0%, 100% { opacity: 0.5; transform: scale(0.9); }
      50% { opacity: 1; transform: scale(1.1); }
    }
    .fc-thinking-dots {
      display: flex; gap: 4px; align-items: center;
      width: 16px; height: 16px; justify-content: center;
      position: relative; z-index: 1;
    }
    .fc-thinking-dots span {
      width: 5px; height: 5px; border-radius: 50%;
      background: ${T.purple};
      opacity: 0.5;
    }
    .fc-thinking-dots span:nth-child(1) { animation: fc-dot-bounce 1.2s ease-in-out 0s infinite; }
    .fc-thinking-dots span:nth-child(2) { animation: fc-dot-bounce 1.2s ease-in-out 0.15s infinite; }
    .fc-thinking-dots span:nth-child(3) { animation: fc-dot-bounce 1.2s ease-in-out 0.3s infinite; }
    @keyframes fc-dot-bounce {
      0%, 60%, 100% { transform: translateY(0); opacity: 0.35; }
      30% { transform: translateY(-4px); opacity: 1; }
    }
    .fc-thinking-label {
      font-size: 13px; color: ${T.fgMuted};
      position: relative; z-index: 1;
      transition: opacity 0.3s;
    }
    .fc-notice {
      align-self: stretch; text-align: center;
      font-size: 12px; line-height: 1.5; color: ${T.fgMuted};
      padding: 4px 8px;
    }

    /* ── Streaming cursor ─────────────────────────────────────── */
    .fc-streaming-cursor {
      display: inline-block;
      color: ${T.cyan};
      animation: fc-cursor-blink 0.8s step-end infinite;
      font-size: 0.9em;
      line-height: 1;
      vertical-align: text-bottom;
      margin-left: 1px;
    }
    @keyframes fc-cursor-blink {
      0%, 100% { opacity: 1; }
      50% { opacity: 0; }
    }

    /* ── Input area ───────────────────────────────────────────── */
    .fc-input-area {
      padding: 12px 14px 14px; flex-shrink: 0;
      border-top: 1px solid ${T.border};
      background: ${T.bg};
    }
    .fc-input-row {
      display: flex; gap: 8px; align-items: flex-end;
    }
    .fc-input {
      flex: 1; resize: none;
      border: 1px solid ${T.inputBorder};
      background: ${T.input}; color: ${T.fg};
      border-radius: 4px; padding: 9px 14px;
      font-family: ${T.fontBody}; font-size: 13.5px;
      line-height: 1.5; max-height: 100px;
      outline: none; transition: border-color 0.2s, box-shadow 0.2s;
      overflow: hidden;
      scrollbar-width: thin;
      scrollbar-color: rgba(42,52,86,0.6) transparent;
    }
    .fc-input::-webkit-scrollbar { width: 5px; }
    .fc-input::-webkit-scrollbar-track { background: transparent; }
    .fc-input::-webkit-scrollbar-thumb {
      background: rgba(42,52,86,0.7);
      border-radius: 10px;
    }
    .fc-input::-webkit-scrollbar-thumb:hover {
      background: rgba(42,52,86,1);
    }
    .fc-input::placeholder { color: ${T.fgMuted}; }
    .fc-input:focus {
      border-color: ${T.purple};
      box-shadow: 0 0 0 2px rgba(149,117,205,0.15);
    }
    .fc-send-btn {
      width: 40px; min-height: 40px;
      align-self: flex-end;
      border-radius: 4px;
      background: ${T.purple}; border: none; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: all 0.2s; flex-shrink: 0;
    }
    .fc-send-btn:hover:not(:disabled) {
      background: ${T.purpleHover};
      box-shadow: 0 0 12px ${T.purpleGlow};
    }
    .fc-send-btn:disabled { opacity: 0.35; cursor: not-allowed; }
    .fc-send-btn svg {
      width: 16px; height: 16px;
      stroke: white; fill: none;
      stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
    }
    .fc-input-area .fc-hidden { display: none; }
    .fc-attach-btn {
      width: 40px; min-height: 40px;
      align-self: flex-end;
      border-radius: 4px;
      background: transparent; border: 1px solid ${T.inputBorder}; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: all 0.2s; flex-shrink: 0;
    }
    .fc-attach-btn:hover:not(:disabled) { border-color: ${T.purple}; }
    .fc-attach-btn:disabled { opacity: 0.35; cursor: not-allowed; }
    .fc-attach-btn svg {
      width: 16px; height: 16px;
      stroke: ${T.fgMuted}; fill: none;
      stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
    }
    .fc-attachments {
      display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px;
    }
    .fc-attachment {
      display: inline-flex; align-items: center; gap: 4px; max-width: 100%;
      padding: 3px 4px 3px 10px; font-size: 12px; color: ${T.fgSoft};
      background: ${T.purpleSoft}; border: 1px solid ${T.purpleBorder};
      border-radius: ${T.radiusSm};
    }
    .fc-attachment-name {
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .fc-attachment-remove {
      background: none; border: none; cursor: pointer;
      color: ${T.fgMuted}; font-size: 14px; line-height: 1; padding: 0 4px;
    }
    .fc-attachment-remove:hover { color: ${T.fg}; }
    .fc-attach-error {
      margin-bottom: 8px; font-size: 12px; color: ${T.danger};
    }
    .fc-panel.fc-dragover .fc-input-area {
      box-shadow: inset 0 0 0 2px ${T.purple};
    }

    /* ── Footer ──────────────────────────────────────────────── */
    .fc-disclaimer {
      text-align: center; padding: 6px 14px 0;
      font-size: 10px; color: ${T.fgMuted};
      background: ${T.bg};
      letter-spacing: 0.02em;
    }
    .fc-footer {
      text-align: center; padding: 8px 14px 10px;
      font-size: 10px; color: ${T.fgMuted};
      background: ${T.bg};
      border-top: 1px solid rgba(30,39,68,0.5);
      letter-spacing: 0.02em;
    }
    .fc-footer a {
      color: ${T.purple}; text-decoration: none; font-weight: 600;
    }
    .fc-footer a:hover { text-decoration: underline; }

    /* ── Mobile ───────────────────────────────────────────────── */
    @media (max-width: 480px) {
      .fc-panel {
        bottom: 0 !important; right: 0 !important; left: 0 !important;
        width: 100% !important; max-width: 100% !important;
        height: 100vh !important; max-height: 100vh !important;
        border-radius: 0;
        border: none;
      }
      .fc-resize { display: none; }
      .fc-maximize-btn, .fc-restore-btn { display: none !important; }
      .fc-btn { bottom: 16px; right: 16px; width: 52px; height: 52px; }
      .fc-btn .fc-btn-logo { width: 28px; height: 28px; }
    }
  `;
  shadow.appendChild(style);

  // ── SVG Icons (stroke-based, Lucide style) ────────────────────────────
  var ICONS = {
    send: '<svg viewBox="0 0 24 24"><path d="M22 2 11 13"/><path d="m22 2-7 20-4-9-9-4z"/></svg>',
    paperclip: '<svg viewBox="0 0 24 24"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
    refresh: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/></svg>',
    close: '<svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
    minimize: '<svg viewBox="0 0 24 24"><path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/></svg>',
    maximize: '<svg viewBox="0 0 24 24"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>',
    brain: '<svg viewBox="0 0 24 24"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/></svg>',
    sparkle: '<svg viewBox="0 0 24 24"><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/></svg>',
  };

  // ── Answer rendering ─────────────────────────────────────────────────
  // renderAssistant() is the only way an answer may reach innerHTML, the
  // streamed bubble included: this script runs in the host site's origin.
  // md() turns the text into HTML; sanitizeHtml() re-emits only the tags and
  // attributes allowed below, in canonical form, and escapes every other "<".
  // The block uses no DOM and no widget state because its unit test evaluates
  // it on its own (frontend/tests/embedded-markdown-xss.test.ts).
  // render-core:start
  var ALLOWED_TAGS = {
    a: 1, abbr: 1, b: 1, blockquote: 1, br: 1, caption: 1, cite: 1, code: 1,
    col: 1, colgroup: 1, dd: 1, del: 1, details: 1, dfn: 1, div: 1, dl: 1,
    dt: 1, em: 1, figcaption: 1, figure: 1, h1: 1, h2: 1, h3: 1, h4: 1,
    h5: 1, h6: 1, hr: 1, i: 1, img: 1, ins: 1, kbd: 1, li: 1, mark: 1,
    ol: 1, p: 1, pre: 1, q: 1, s: 1, samp: 1, small: 1, span: 1, strike: 1,
    strong: 1, sub: 1, summary: 1, sup: 1, table: 1, tbody: 1, td: 1,
    tfoot: 1, th: 1, thead: 1, time: 1, tr: 1, u: 1, ul: 1, "var": 1, wbr: 1,
  };
  var VOID_TAGS = { br: 1, col: 1, hr: 1, img: 1, wbr: 1 };
  // Dropped with their content: script, styles, embedded documents, and the
  // elements whose content the browser parses by different rules. So is an
  // SVG or MathML drawing in strict rendering (DRAWINGS below).
  var DROPPED_TAGS = {
    applet: 1, embed: 1, frame: 1, frameset: 1, head: 1, iframe: 1,
    noembed: 1, noframes: 1, noscript: 1, object: 1, plaintext: 1,
    script: 1, select: 1, style: 1, template: 1, textarea: 1,
    title: 1, xmp: 1,
  };
  var TAG_ATTRS = {
    a: { href: 1, target: 1, rel: 1 },
    blockquote: { cite: 1 },
    col: { span: 1 },
    colgroup: { span: 1 },
    img: { src: 1, alt: 1, width: 1, height: 1 },
    ol: { start: 1 },
    q: { cite: 1 },
    td: { colspan: 1, rowspan: 1 },
    th: { colspan: 1, rowspan: 1, scope: 1 },
    time: { datetime: 1 },
  };
  // Default rendering also keeps the presentational elements and attributes
  // and an inline style that cannot reach script (isSafeStyle); strict
  // rendering keeps neither. "class" and "id" stay out in both: the widget
  // finds its own elements in the transcript by class.
  var LENIENT_TAGS = { big: 1, center: 1, font: 1, tt: 1 };
  var LENIENT_ATTRS = {
    align: 1, background: 1, bgcolor: 1, border: 1, cellpadding: 1,
    cellspacing: 1, clear: 1, color: 1, dir: 1, face: 1, frame: 1, height: 1,
    hidden: 1, hspace: 1, lang: 1, noshade: 1, nowrap: 1, open: 1,
    reversed: 1, rules: 1, size: 1, start: 1, style: 1, type: 1, valign: 1,
    value: 1, vspace: 1, width: 1,
  };
  var URL_ATTRS = { background: 1, cite: 1, href: 1, src: 1 };
  // A new tab gets no handle on the host page and no referrer. Markdown links
  // always open one; in strict rendering every link of an answer does, so no
  // link navigates the page embedding the widget.
  var NEW_TAB = ' target="_blank" rel="noopener noreferrer"';
  // The only schemes of strict rendering; by default a URL may use any scheme
  // but the ones that run script and the ones that launch a Windows program.
  var SAFE_SCHEMES = /^(?:https?|mailto|tel)$/i;
  var SCRIPT_SCHEMES = /^(?:javascript|vbscript|data)$/i;
  // Windows protocol handlers with a public exploitation history. Not the
  // Office URI schemes (ms-word:, ms-excel:, ...): answers use them to open a
  // SharePoint document in its desktop app.
  var PROGRAM_SCHEMES = /^(?:hcp|its|mhtml|mk|ms-appinstaller|ms-cxh|ms-cxh-full|ms-its|ms-itss|ms-msdt|ms-officecmd|search|search-ms|shell)$/i;
  // What CSS can reach script with: IE's expression() and behaviors,
  // Firefox's XBL bindings, a script URL. Matched on cssProbe()'s reading.
  var SCRIPT_CSS = /expression\(|javascript:|vbscript:|(?:^|;)(?:-ms-)?behaviou?r:|(?:^|;)-moz-binding:/;
  // What makes a drawing load something from outside itself.
  var DRAWING_REFS = /url\(|image-set\(|image\(|cross-fade\(|element\(|src\(/;
  // What a drawing inherits from the HTML around it (custom properties, the
  // paint and marker properties): an HTML style setting one of them may not
  // load anything either, nor may a drawing ask for "inherit".
  var DRAWING_INHERITED = /(?:^|;)(?:--|fill:|stroke:|marker(?:-start|-mid|-end)?:)/;
  // Inline SVG and MathML in default rendering: drawings made of these
  // elements and attributes only. No script, event handler, foreignObject,
  // link or animation, and nothing loaded from outside the page (a url() may
  // only name an element of it by its "#id").
  var DRAWINGS = {
    svg: {
      tags: {
        circle: 1, clippath: 1, defs: 1, ellipse: 1, g: 1, line: 1,
        lineargradient: 1, marker: 1, mask: 1, path: 1, pattern: 1,
        polygon: 1, polyline: 1, radialgradient: 1, rect: 1, stop: 1,
        svg: 1, text: 1, tspan: 1,
      },
      attrs: {
        "alignment-baseline": 1, "aria-hidden": 1, "aria-label": 1,
        "baseline-shift": 1, "clip-path": 1, "clip-rule": 1,
        clippathunits: 1, color: 1, cx: 1, cy: 1, d: 1, direction: 1,
        display: 1, "dominant-baseline": 1, dx: 1, dy: 1, fill: 1,
        "fill-opacity": 1, "fill-rule": 1, "font-family": 1, "font-size": 1,
        "font-style": 1, "font-variant": 1, "font-weight": 1, fr: 1, fx: 1,
        fy: 1, gradienttransform: 1, gradientunits: 1, height: 1, id: 1,
        lengthadjust: 1, "letter-spacing": 1, "marker-end": 1,
        "marker-mid": 1, "marker-start": 1, markerheight: 1, markerunits: 1,
        markerwidth: 1, mask: 1, maskcontentunits: 1, maskunits: 1,
        offset: 1, opacity: 1, orient: 1, overflow: 1, "paint-order": 1,
        pathlength: 1, patterncontentunits: 1, patterntransform: 1,
        patternunits: 1, points: 1, preserveaspectratio: 1, r: 1, refx: 1,
        refy: 1, role: 1, rotate: 1, rx: 1, ry: 1, "shape-rendering": 1,
        spreadmethod: 1, "stop-color": 1, "stop-opacity": 1, stroke: 1,
        "stroke-dasharray": 1, "stroke-dashoffset": 1, "stroke-linecap": 1,
        "stroke-linejoin": 1, "stroke-miterlimit": 1, "stroke-opacity": 1,
        "stroke-width": 1, style: 1, "text-anchor": 1, "text-decoration": 1,
        "text-rendering": 1, textlength: 1, transform: 1,
        "vector-effect": 1, viewbox: 1, visibility: 1, width: 1,
        "word-spacing": 1, "writing-mode": 1, x: 1, x1: 1, x2: 1, y: 1,
        y1: 1, y2: 1,
      },
    },
    math: {
      tags: {
        math: 1, menclose: 1, merror: 1, mfrac: 1, mi: 1, mmultiscripts: 1,
        mn: 1, mo: 1, mover: 1, mpadded: 1, mphantom: 1, mprescripts: 1,
        mroot: 1, mrow: 1, ms: 1, mspace: 1, msqrt: 1, mstyle: 1, msub: 1,
        msubsup: 1, msup: 1, mtable: 1, mtd: 1, mtext: 1, mtr: 1,
        munder: 1, munderover: 1, none: 1, semantics: 1,
      },
      attrs: {
        accent: 1, accentunder: 1, alttext: 1, "aria-label": 1,
        columnalign: 1, columnlines: 1, columnspacing: 1, columnspan: 1,
        depth: 1, dir: 1, display: 1, displaystyle: 1, fence: 1, form: 1,
        frame: 1, framespacing: 1, height: 1, largeop: 1, linethickness: 1,
        lspace: 1, mathbackground: 1, mathcolor: 1, mathsize: 1,
        mathvariant: 1, maxsize: 1, minsize: 1, movablelimits: 1,
        notation: 1, role: 1, rowalign: 1, rowlines: 1, rowspacing: 1,
        rowspan: 1, rspace: 1, scriptlevel: 1, separator: 1, stretchy: 1,
        style: 1, symmetric: 1, voffset: 1, width: 1,
      },
    },
  };
  // Dropped with their content inside a drawing: what the browser parses as
  // HTML there, and what it never shows.
  var DRAWING_DROPPED = { annotation: 1, "annotation-xml": 1, desc: 1, foreignobject: 1, metadata: 1 };
  // MathML's token elements hold text only: in them the browser reads a tag
  // as HTML.
  var MATH_TOKENS = { mi: 1, mn: 1, mo: 1, ms: 1, mtext: 1 };
  // Deeper elements of a drawing are dropped (their text kept), which bounds
  // what an end tag costs to match.
  var MAX_DRAWING_DEPTH = 64;
  var NAMED_REFS = {
    amp: "&", apos: "'", colon: ":", gt: ">", lt: "<", newline: "\n", quot: '"', tab: "\t",
  };
  // A tag is only recognised within this many characters; longer ones stay text.
  var MAX_TAG_LENGTH = 8192;
  // Every name listed above is shorter than this bound and a longer one is
  // skipped anyway: bounding it keeps a run of "<a<a<a..." from being
  // rescanned from each "<".
  var TAG_HEAD_RE = /<(\/?)([a-zA-Z][^\s\/>]{0,31})/y;
  var ATTR_RE = /([^\s"'>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>`]+)))?/g;

  // Own keys only: an inherited one such as "constructor" is on no list.
  function has(table, key) {
    return Object.prototype.hasOwnProperty.call(table, key);
  }

  function escAttr(value) {
    return value.replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Character references decoded: numeric ones and every named one that can
  // spell a scheme. Any other named one is left as written.
  function decodeRefs(value) {
    return value.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);?/gi, function (ref, body) {
      var key = body.toLowerCase();
      if (key.charAt(0) !== "#") return has(NAMED_REFS, key) ? NAMED_REFS[key] : ref;
      var code = key.charAt(1) === "x" ? parseInt(key.slice(2), 16) : parseInt(key.slice(1), 10);
      return code > 0 && code <= 0x10ffff && (code < 0xd800 || code > 0xdfff)
        ? String.fromCodePoint(code)
        : "\uFFFD";
    });
  }

  // The URL the browser would follow: character references decoded, then the
  // whitespace and controls its URL parser ignores removed.
  function effectiveUrl(value) {
    return decodeRefs(value).replace(/[\u0000-\u0020\u007f-\u009f]+/g, "");
  }

  // A scheme that runs script or launches a Windows program is refused, an
  // obfuscated one included. `lenient` (default rendering) accepts any other
  // scheme; otherwise only SAFE_SCHEMES are accepted.
  function isSafeUrl(value, lenient) {
    var scheme = /^([^:\/?#]*):/.exec(effectiveUrl(value));
    if (!scheme) return true;
    return lenient === true
      ? !SCRIPT_SCHEMES.test(scheme[1]) && !PROGRAM_SCHEMES.test(scheme[1])
      : SAFE_SCHEMES.test(scheme[1]);
  }

  // A CSS value as its parser reads it: character references and CSS escapes
  // decoded, whitespace removed, lower-cased. null when it holds a comment or
  // a named reference decodeRefs() leaves alone: either could hide what the
  // parser reads from the patterns matched on the result.
  function cssProbe(value) {
    var css = decodeRefs(value);
    if (/&[a-z]/i.test(css) || css.indexOf("/*") >= 0) return null;
    return css
      .replace(/\\(?:([0-9a-f]{1,6})[ \t\n\r\f]?|([\s\S]))/gi, function (_, hex, ch) {
        if (!hex) return /[\n\r\f]/.test(ch) ? "" : ch;
        var code = parseInt(hex, 16);
        return code > 0 && code <= 0x10ffff && (code < 0xd800 || code > 0xdfff)
          ? String.fromCodePoint(code)
          : "\uFFFD";
      })
      .replace(/[\u0000-\u0020\u007f-\u009f]+/g, "")
      .toLowerCase();
  }

  // No reference to anything but one of the page's own "#id" parts.
  function isSelfContained(css) {
    return !DRAWING_REFS.test(css.replace(/url\((['"]?)#[^'"()]*\1\)/g, ""));
  }

  function isSafeStyle(value) {
    var css = cssProbe(value);
    return css !== null && !SCRIPT_CSS.test(css) && !(DRAWING_INHERITED.test(css) && !isSelfContained(css));
  }

  // An attribute value of a drawing: no script, and no reference outside it.
  function isDrawingValue(value) {
    var css = cssProbe(value);
    return css !== null && !SCRIPT_CSS.test(css) && css.indexOf("inherit") < 0 && isSelfContained(css);
  }

  // Where an image the platform serves is loaded from: an absolute URL under
  // `root` (the platform's origin plus any BASE_PATH), or a path the platform
  // wrote root-relative, which belongs under `root` too. null for any other
  // image: loading it would hand its URL to a third party or the host page.
  function platformImageUrl(value, root) {
    if (!root) return null;
    var url = effectiveUrl(value);
    if (url.toLowerCase().indexOf(root.toLowerCase() + "/") === 0) return url;
    // "//host" and "/\host" name another host.
    if (/^\/(?![\/\\])/.test(url)) return root + url;
    return null;
  }

  // The addresses the platform writes for a file or an image it stores
  // (generate_image, the code interpreter, the screenshot tools, and
  // generate_image's database fallback): routes behind a platform sign-in.
  var CHAT_FILE_RE = /^\/api\/v1\/chat\/files\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:\/download)?$/;
  var CHAT_IMAGE_RE = /^\/api\/v1\/chat\/images\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$/;
  // A Markdown image. Its alt text holds at most one level of brackets and
  // its target no bracket or space: both keep a run of "![" from being
  // rescanned from each one.
  var MD_IMAGE_RE = /!\[((?:[^\[\]\x00]|\[[^\[\]\x00]*\])*)\]\(([^()\s\x00]+)\)/g;

  // Where a platform address (root-relative or under `root`) is loaded from:
  // the same file or image through `session`, the widget session's own route,
  // which serves only what belongs to that session. null for any other
  // address, and for every address without a session.
  function sessionMediaUrl(value, root, session) {
    if (!session) return null;
    var url = effectiveUrl(value);
    if (root && url.toLowerCase().indexOf(root.toLowerCase() + "/") === 0) url = url.slice(root.length);
    var m = CHAT_FILE_RE.exec(url);
    if (m) return session + "/files/" + m[1].toLowerCase();
    m = CHAT_IMAGE_RE.exec(url);
    return m ? session + "/images/" + m[1].toLowerCase() : null;
  }

  // The first value of each attribute, as the browser keeps it.
  function readAttrs(attrs) {
    var values = {};
    var m;
    ATTR_RE.lastIndex = 0;
    while ((m = ATTR_RE.exec(attrs))) {
      var name = m[1].toLowerCase();
      if (has(values, name)) continue;
      values[name] = m[2] != null ? m[2] : m[3] != null ? m[3] : m[4] != null ? m[4] : "";
    }
    return values;
  }

  // `src`, when given, replaces an image's own source (a session image).
  function cleanAttrs(tag, attrs, root, strict, src) {
    var allowed = has(TAG_ATTRS, tag) ? TAG_ATTRS[tag] : {};
    var values = readAttrs(attrs);
    var out = "";
    var blank = false;
    var rel = "";
    for (var name in values) {
      if (!has(values, name)) continue;
      if (name !== "title" && !has(allowed, name) && (strict || !has(LENIENT_ATTRS, name))) continue;
      var value = values[name];
      if (has(URL_ATTRS, name) && !isSafeUrl(value, !strict)) continue;
      if (name === "style" && !isSafeStyle(value)) continue;
      if (tag === "a" && (name === "target" || name === "rel")) {
        if (strict) continue;
        if (name === "rel") {
          rel = value;
          continue;
        }
        if (value.toLowerCase() !== "_blank") continue;
        blank = true;
      }
      if (src && tag === "img" && name === "src") value = src;
      else if (strict && tag === "img" && name === "src") value = platformImageUrl(value, root) || "";
      out += " " + name + '="' + escAttr(value) + '"';
    }
    if (tag !== "a") return out;
    if (strict) return out + NEW_TAB;
    if (blank) rel = "noopener noreferrer";
    return rel ? out + ' rel="' + escAttr(rel) + '"' : out;
  }

  // An image the platform stores for this session is shown in both modes,
  // loaded through the session's own route. Otherwise, in strict rendering an
  // image the platform serves is shown and any other is written as a link to
  // it, labelled by its alt text or its URL, so an answer cannot make the
  // visitor's browser fetch a URL of its choosing.
  function renderImage(attrs, root, strict, session) {
    var values = readAttrs(attrs);
    var src = has(values, "src") ? values.src : "";
    var media = src && isSafeUrl(src) ? sessionMediaUrl(src, root, session) : null;
    if (media) return "<img" + cleanAttrs("img", attrs, root, strict, media) + ' class="fc-img">';
    if (!strict) return "<img" + cleanAttrs("img", attrs, root, false) + ">";
    if (src && isSafeUrl(src) && platformImageUrl(src, root)) {
      return "<img" + cleanAttrs("img", attrs, root, true) + ">";
    }
    var alt = has(values, "alt") ? values.alt.trim() : "";
    var label = (alt || src).replace(/</g, "&lt;").replace(/>/g, "&gt;");
    if (!src || !isSafeUrl(src)) return label;
    return '<a href="' + escAttr(src) + '"' + NEW_TAB + ">" + label + "</a>";
  }

  // Index of the ">" ending the tag whose attributes start at `from`, stepping
  // over quoted values; -1 while it has not arrived (a half-streamed answer).
  // As in the browser's tokenizer, a quote only opens a value right after "=",
  // anywhere else it is part of a name: an apostrophe in the prose after a
  // stray "<b" must not hide the rest of the answer.
  function tagEnd(s, from) {
    var limit = Math.min(s.length, from + MAX_TAG_LENGTH);
    // 0 before an attribute name, 1 in one, 2 after one, 3 after "=",
    // 4 in an unquoted value.
    var state = 0;
    for (var j = from; j < limit; j++) {
      var c = s.charAt(j);
      if (c === ">") return j;
      var space = c === " " || c === "\t" || c === "\n" || c === "\f" || c === "\r";
      if (state === 3) {
        if (c === '"' || c === "'") {
          j = s.indexOf(c, j + 1);
          if (j < 0 || j >= limit) return -1;
          state = 0;
        } else if (!space) {
          state = 4;
        }
      } else if (state === 4) {
        if (space) state = 0;
      } else if (space) {
        if (state === 1) state = 2;
      } else if (c === "/") {
        state = 0;
      } else {
        state = c === "=" && state !== 0 ? 3 : 1;
      }
    }
    return -1;
  }

  // tagEnd(s, j) for every j from `first` to the end of `s` (at j - first),
  // without its MAX_TAG_LENGTH bound: the ">" it stops at, or -1. Read from
  // the end, each character once for all the tags of a frame, however they
  // overlap. tagEnd's states "in a name" and "after a name" read every
  // character alike, so they are one here.
  function tagEnds(s, first) {
    var ends = new Int32Array(s.length - first + 1);
    ends[s.length - first] = -1;
    // Where tagEnd stops from the next character in its other states (in or
    // after a name, after "=", in an unquoted value), and the next quotes.
    var name = -1, equals = -1, value = -1, dq = -1, sq = -1;
    for (var j = s.length - 1; j >= first; j--) {
      var c = s.charAt(j);
      var k = j - first;
      if (c === ">") {
        ends[k] = name = equals = value = j;
      } else {
        var next = ends[k + 1];
        var space = c === " " || c === "\t" || c === "\n" || c === "\f" || c === "\r";
        var close = c === '"' ? dq : sq;
        // After "=", a quoted value runs to its closing quote.
        var afterEquals = c === '"' || c === "'"
          ? (close < 0 ? -1 : ends[close + 1 - first])
          : space ? equals : value;
        ends[k] = space || c === "/" ? next : name;
        name = c === "=" ? equals : c === "/" ? next : name;
        value = space ? next : value;
        equals = afterEquals;
      }
      if (c === '"') dq = j;
      else if (c === "'") sq = j;
    }
    return ends;
  }

  // Index of the "<" from which a streamed frame is written as text, or -1:
  // the frame holds a tag still being written, and all the text after its
  // "<" belongs to it. Every "<" is read from itself, as sanitizeHtml reads a
  // tag (TAG_HEAD_RE, tagEnd), even one inside another tag's attributes:
  // md()'s own markup and the elements the sanitizer drops can move where
  // the sanitizer starts reading a tag. The text starts at the first "<" of
  // the tags overlapping that one, never inside a tag whose ">" has arrived:
  // a "<" of its attributes written as text would change them. The whole
  // frame is read, and a tag without its ">" is still being written however
  // much text follows it: md() drops blank lines and ends every line with
  // its own markup, so that text says nothing of where the sanitizer ends it.
  function unfinishedTag(s) {
    var first = s.indexOf("<");
    if (first < 0) return -1;
    var ends = tagEnds(s, first);
    // The overlapping tags read so far: from `start` to the ">" at `reach`.
    var start = -1;
    var reach = -1;
    for (var i = first; i >= 0; i = s.indexOf("<", i + 1)) {
      TAG_HEAD_RE.lastIndex = i;
      var head = TAG_HEAD_RE.exec(s);
      if (!head) continue;
      var gt = ends[i + head[0].length - first];
      if (i > reach) start = i;
      if (gt < 0) return start;
      if (gt > reach) reach = gt;
    }
    return -1;
  }

  // Skips an element dropped with its content, up to its closing tag (or to
  // the end when there is none).
  function skipElement(s, name, from) {
    var close = new RegExp("</" + name + "(?=[\\s/>])", "gi");
    close.lastIndex = from;
    var m = close.exec(s);
    if (!m) return s.length;
    var end = s.indexOf(">", m.index);
    return end < 0 ? s.length : end + 1;
  }

  function drawingAttrs(attrs, allowed) {
    var values = readAttrs(attrs);
    var out = "";
    for (var name in values) {
      if (!has(values, name) || !has(allowed, name) || !isDrawingValue(values[name])) continue;
      out += " " + name + '="' + escAttr(values[name]) + '"';
    }
    return out;
  }

  // A tag written inside the drawing whose elements `open` holds (outermost
  // first): one of the drawing's own elements, or nothing. Every element is
  // closed explicitly, so the browser's drawing is exactly `open`.
  function drawingTag(open, name, attrs, closing, selfClosed) {
    var out = "";
    if (closing) {
      var at = open.lastIndexOf(name);
      while (at >= 0 && open.length > at) out += "</" + open.pop() + ">";
      return out;
    }
    var drawing = DRAWINGS[open[0]];
    if (!has(drawing.tags, name) || has(MATH_TOKENS, open[open.length - 1]) || open.length >= MAX_DRAWING_DEPTH) {
      return "";
    }
    if (!selfClosed) open.push(name);
    return "<" + name + drawingAttrs(attrs, drawing.attrs) + (selfClosed ? "/>" : ">");
  }

  // `streaming` marks a frame of an answer still arriving, where md() left a
  // NUL at the end of the frame's own text, before the markup closing it. A
  // tag still open there would be ended by that markup, not by the answer:
  // the frame is refused, null.
  function sanitizeHtml(html, root, strict, session, streaming) {
    var s = String(html);
    var textEnd = streaming ? s.indexOf("\x00") : -1;
    s = s.replace(/\x00/g, "");
    var out = "";
    var i = 0;
    // The first ">" at or after the current "<": a tag with none within
    // MAX_TAG_LENGTH cannot end, so it stays text without a scan and the
    // render stays linear (a streamed answer is re-rendered at every chunk).
    var nextGt = -1;
    // The SVG or MathML elements open here (default rendering only).
    var drawing = [];
    while (i < s.length) {
      var lt = s.indexOf("<", i);
      if (lt < 0) lt = s.length;
      var text = s.slice(i, lt);
      out += text.indexOf(">") < 0 ? text : text.replace(/>/g, "&gt;");
      if (lt === s.length) break;
      if (s.substr(lt, 4) === "<!--") {
        var endComment = s.indexOf("-->", lt + 4);
        i = endComment < 0 ? s.length : endComment + 3;
        continue;
      }
      if (nextGt < lt) {
        nextGt = s.indexOf(">", lt);
        if (nextGt < 0) nextGt = Infinity;
      }
      TAG_HEAD_RE.lastIndex = lt;
      var head = TAG_HEAD_RE.exec(s);
      var from = head ? lt + head[0].length : lt;
      var gt = head && nextGt - from < MAX_TAG_LENGTH ? tagEnd(s, from) : -1;
      // A tag still open where the frame's text ends (one the length bound
      // cuts before that is text in the final render too).
      if (head && lt < textEnd && from + MAX_TAG_LENGTH > textEnd && !(gt >= 0 && gt < textEnd)) return null;
      if (gt < 0) {
        out += "&lt;";
        i = lt + 1;
        continue;
      }
      var attrs = s.slice(from, gt);
      i = gt + 1;
      var closing = head[1] === "/";
      var name = head[2].toLowerCase();
      var selfClosed = /\/\s*$/.test(attrs);
      if (drawing.length) {
        // The line break the Markdown pass wrote between two lines of a
        // drawing would end it in the browser: it is the newline again.
        if (name === "br") {
          if (!closing) out += "\n";
          continue;
        }
        if (has(DROPPED_TAGS, name) || has(DRAWING_DROPPED, name)) {
          if (!closing && !selfClosed) i = skipElement(s, name, i);
          continue;
        }
        if (!has(ALLOWED_TAGS, name) && !has(LENIENT_TAGS, name)) {
          out += drawingTag(drawing, name, attrs, closing, selfClosed);
          continue;
        }
        // Any other HTML tag ends the drawing, as the browser would.
        while (drawing.length) out += "</" + drawing.pop() + ">";
      }
      if (has(DROPPED_TAGS, name) || (strict && has(DRAWINGS, name))) {
        // A self-closed <svg/> or <math/> has no content to drop.
        if (!closing && !(selfClosed && has(DRAWINGS, name))) i = skipElement(s, name, i);
        continue;
      }
      if (has(DRAWINGS, name)) {
        if (!closing) {
          out += "<" + name + drawingAttrs(attrs, DRAWINGS[name].attrs) + (selfClosed ? "/>" : ">");
          if (!selfClosed) drawing.push(name);
        }
        continue;
      }
      if (!has(ALLOWED_TAGS, name) && (strict || !has(LENIENT_TAGS, name))) continue;
      if (closing) {
        if (!has(VOID_TAGS, name)) out += "</" + name + ">";
        continue;
      }
      out += name === "img"
        ? renderImage(attrs, root, strict, session)
        : "<" + name + cleanAttrs(name, attrs, root, strict) + ">";
    }
    return out;
  }

  function link(url, label) {
    return '<a href="' + escAttr(url) + '"' + NEW_TAB + ">" + label + "</a>";
  }

  // `streaming` renders a frame of an answer still arriving (see
  // renderAssistant); `asText` also writes every tag of the answer as text.
  function md(text, root, session, streaming, asText) {
    if (!text) return "";
    // NUL delimits the protected code spans below; an answer cannot carry one.
    text = String(text).replace(/\x00/g, "");

    var codeBlocks = [], inlineCodes = [], images = [], imageTexts = [];

    // `end` is written where the code's text ends.
    function codeBlock(c, end) {
      c = c.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n$/, "");
      codeBlocks.push("<pre><code>" + c + end + "</code></pre>");
      return "\x00CB" + (codeBlocks.length - 1) + "\x00";
    }

    // Protect fenced code blocks from further processing
    text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, function (_, _lang, c) { return codeBlock(c, ""); });
    // A streamed frame of a fence not closed yet: code up to the end, as it
    // will be once it closes. Its text is the frame's last (the NUL below).
    if (streaming) text = text.replace(/```(\w*)\n?([\s\S]*)$/, function (_, _lang, c) { return codeBlock(c, "\x00"); });

    // Protect inline code spans
    text = text.replace(/`([^`]+)`/g, function (_, c) {
      c = c.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      inlineCodes.push("<code>" + c + "</code>");
      return "\x00IC" + (inlineCodes.length - 1) + "\x00";
    });

    // A streamed frame of a code span not closed yet: what follows its
    // backtick may still turn out to be code, so none of it is markup.
    var openCode = streaming ? text.lastIndexOf("`") : -1;
    if (openCode >= 0) text = text.slice(0, openCode) + text.slice(openCode).replace(/</g, "&lt;");

    // A Markdown image of a file or an image stored for this session becomes
    // an <img> (sanitizeHtml loads it through the session); any other stays
    // the text and link it has always rendered as.
    if (session) {
      text = text.replace(MD_IMAGE_RE, function (whole, alt, src, at) {
        // Reaching into a code span still open, it may yet be code.
        if (openCode >= 0 && at + whole.length > openCode) return whole;
        if (!sessionMediaUrl(src, root, session)) return whole;
        images.push('<img src="' + escAttr(src) + '" alt="' + escAttr(alt.replace(/\s+/g, " ").trim()) + '">');
        imageTexts.push(whole);
        return "\x00IM" + (images.length - 1) + "\x00";
      });
    }

    // A streamed frame ending inside a tag: from its "<" on, the text
    // stays text until the tag's own ">" arrives, a session image in it too.
    // The code and the session images are set aside first, so only the
    // answer's own markup is read, and only up to a code span still open:
    // the ">" of a tag that span cuts may yet turn out to be code.
    var unfinished = -1;
    if (streaming) {
      var codeAt = openCode >= 0 ? text.lastIndexOf("`") : -1;
      unfinished = asText ? 0 : unfinishedTag(codeAt >= 0 ? text.slice(0, codeAt) : text);
    }
    if (unfinished >= 0) {
      text = text.slice(0, unfinished) + text.slice(unfinished)
        .replace(/\x00IM(\d+)\x00/g, function (_, j) { return imageTexts[+j]; })
        .replace(/</g, "&lt;");
    }

    function inl(s) {
      return s
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/\*(.+?)\*/g, "<em>$1</em>")
        .replace(/~~(.+?)~~/g, "<del>$1</del>")
        // The label holds no bracket and the target never runs into the next
        // "](": both keep a run of brackets from being rescanned from each "[".
        .replace(/\[([^\[\]]+)\]\(((?:(?!\]\()[^)\x00])+)\)/g, function (_, label, url) { return link(url, label); })
        .replace(/(^|[^"'>])(https?:\/\/[^\s<)\x00]+)/g, function (_, pre, url) { return pre + link(url, url); });
    }

    function isBlock(l) {
      return /^#{1,3} /.test(l) || /^[-*_]{3,}$/.test(l.trim()) ||
        /^[\s]*[-*+] /.test(l) || /^\d+[.)]\s/.test(l) ||
        /^>\s?/.test(l) || /^\x00CB\d+\x00$/.test(l);
    }

    var lines = text.split("\n"), out = "", i = 0, n = lines.length;
    // Where the text of the last block written ends, before the markup that
    // closes it: in a streamed frame, markup written because the frame ends
    // there, which moves as it grows.
    var textEnd = -1;
    function closeWith(markup) {
      textEnd = out.length;
      out += markup;
    }

    while (i < n) {
      var L = lines[i];

      if (!L.trim()) { i++; continue; }

      // Code-block placeholder
      if (/^\x00CB\d+\x00$/.test(L)) { out += L; closeWith(""); i++; continue; }

      // Headings
      var hm = L.match(/^(#{1,3}) (.+)/);
      if (hm) {
        var tg = "h" + hm[1].length;
        out += "<" + tg + ">" + inl(hm[2]);
        closeWith("</" + tg + ">");
        i++; continue;
      }

      // Horizontal rule
      if (/^[-*_]{3,}$/.test(L.trim())) { closeWith("<hr>"); i++; continue; }

      // Unordered list
      if (/^[\s]*[-*+] /.test(L)) {
        out += "<ul>";
        while (i < n && /^[\s]*[-*+] /.test(lines[i])) {
          out += "<li>" + inl(lines[i].replace(/^[\s]*[-*+] /, ""));
          closeWith("</li>");
          i++;
        }
        out += "</ul>"; continue;
      }

      // Ordered list
      if (/^\d+[.)]\s/.test(L)) {
        out += "<ol>";
        while (i < n && /^\d+[.)]\s/.test(lines[i])) {
          out += "<li>" + inl(lines[i].replace(/^\d+[.)]\s/, ""));
          closeWith("</li>");
          i++;
        }
        out += "</ol>"; continue;
      }

      // Blockquote
      if (/^>\s?/.test(L)) {
        var q = [];
        while (i < n && /^>\s?/.test(lines[i])) {
          q.push(lines[i].replace(/^>\s?/, ""));
          i++;
        }
        out += "<blockquote>" + q.map(inl).join("<br>");
        closeWith("</blockquote>");
        continue;
      }

      // GFM table (header row + separator row)
      if (/^\|.+\|/.test(L) && i + 1 < n && /^\|[\s:|-]+\|$/.test(lines[i + 1].trim())) {
        var hds = L.split("|").slice(1, -1);
        out += "<table><thead><tr>";
        hds.forEach(function (h) { out += "<th>" + inl(h.trim()); closeWith("</th>"); });
        out += "</tr></thead><tbody>";
        i += 2;
        while (i < n && /^\|.+\|/.test(lines[i])) {
          var cs = lines[i].split("|").slice(1, -1);
          out += "<tr>";
          cs.forEach(function (c) { out += "<td>" + inl(c.trim()); closeWith("</td>"); });
          out += "</tr>"; i++;
        }
        out += "</tbody></table>"; continue;
      }

      // Paragraph – this line and the consecutive non-block lines after it.
      // Always consuming this one keeps a line no block rule took (a bare
      // "# ", which a streamed heading passes through) from stalling the loop.
      var p = [L];
      i++;
      while (i < n && lines[i].trim() && !isBlock(lines[i])) {
        p.push(lines[i]); i++;
      }
      out += "<p>" + p.map(inl).join("<br>");
      closeWith("</p>");
    }

    // A streamed frame: a NUL, which no text holds, marks for sanitizeHtml
    // where the frame's own text ends (the protected spans are restored
    // around it; an open fence's code carries its own, which comes first).
    if (streaming && textEnd >= 0) out = out.slice(0, textEnd) + "\x00" + out.slice(textEnd);

    // Restore protected spans
    out = out.replace(/\x00IM(\d+)\x00/g, function (_, j) { return images[+j]; });
    out = out.replace(/\x00IC(\d+)\x00/g, function (_, j) { return inlineCodes[+j]; });
    out = out.replace(/\x00CB(\d+)\x00/g, function (_, j) { return codeBlocks[+j]; });
    return out;
  }

  // `platformRoot` is the platform the widget belongs to (origin plus any
  // BASE_PATH). With `strict` (PUBLIC_WIDGET_STRICT_RENDERING) it is the only
  // place an answer's images are loaded from, and every link opens a new tab.
  // `session` is the widget session's own route on the platform: in both
  // modes, a file or an image the platform stores for the session, which an
  // answer shows by its platform address, is loaded through it.
  // `streaming` marks a frame of an answer still arriving: what it has not
  // finished writing (a tag whose own ">" has not arrived, code not closed)
  // loads nothing. The final render is without.
  function renderAssistant(text, platformRoot, strict, session, streaming) {
    var root = platformRoot ? String(platformRoot).replace(/\/+$/, "") : "";
    var route = session ? String(session).replace(/\/+$/, "") : "";
    strict = strict === true;
    if (streaming !== true) return sanitizeHtml(md(text, root, route), root, strict, route);
    var html = sanitizeHtml(md(text, root, route, true), root, strict, route, true);
    // md() dropped what ended a tag whose ">" has arrived (the text after a
    // table row's last "|", a quote marker) and the tag runs on to the end
    // of the frame: this frame shows every tag of the answer as text.
    return html !== null ? html : sanitizeHtml(md(text, root, route, true, true), root, strict, route);
  }

  // Paints at most once per frame while an answer streams; `request` and
  // `cancel` are requestAnimationFrame and cancelAnimationFrame in the page.
  // The caller cancels before painting the final text itself.
  function frameScheduler(request, cancel, paint) {
    var pending = 0;
    return {
      schedule: function () {
        if (!pending) pending = request(function () { pending = 0; paint(); });
      },
      cancel: function () {
        if (pending) cancel(pending);
        pending = 0;
      },
    };
  }
  // render-core:end

  // ── Build DOM ─────────────────────────────────────────────────────────
  var widget = document.createElement("div");
  widget.className = "fc-widget";
  shadow.appendChild(widget);

  // Floating button
  var btn = document.createElement("button");
  btn.className = "fc-btn";
  btn.setAttribute("aria-label", "Open chat");
  btn.innerHTML =
    '<img class="fc-btn-logo" src="' + LOGO_URL + '" alt="Chat" />' +
    '<svg class="fc-btn-close" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
  widget.appendChild(btn);

  // Panel
  var panel = document.createElement("div");
  panel.className = "fc-panel";
  panel.innerHTML =
    '<div class="fc-resize"></div>' +
    '<div class="fc-header">' +
      '<div class="fc-header-icon"><img src="' + LOGO_URL + '" alt="" /></div>' +
      '<div class="fc-header-info">' +
        '<div class="fc-header-title">AI Assistant</div>' +
        '<div class="fc-header-status"><span class="fc-header-dot"></span>Online</div>' +
      '</div>' +
      '<div class="fc-header-actions">' +
        '<button class="fc-refresh-btn" title="New conversation">' + ICONS.refresh + '</button>' +
        '<button class="fc-maximize-btn" title="Maximize">' + ICONS.maximize + '</button>' +
        '<button class="fc-restore-btn fc-hidden" title="Restore default size">' + ICONS.minimize + '</button>' +
      '</div>' +
    '</div>' +
    '<div class="fc-messages"></div>' +
    '<div class="fc-input-area">' +
      '<div class="fc-attachments fc-hidden"></div>' +
      '<div class="fc-attach-error fc-hidden" role="alert"></div>' +
      '<div class="fc-input-row">' +
        '<button class="fc-attach-btn fc-hidden" type="button" title="Attach files" aria-label="Attach files">' + ICONS.paperclip + '</button>' +
        '<input class="fc-file-input" type="file" multiple hidden />' +
        '<textarea class="fc-input" placeholder="Ask a question\u2026" rows="1"></textarea>' +
        '<button class="fc-send-btn" disabled>' + ICONS.send + '</button>' +
      '</div>' +
    '</div>' +
    // Static footer brand fallback is intentionally "Filigran" — the
    // corporate parent that hosts both the XTM One and Filigran Copilot
    // products and that the link target points at. The actual product
    // name (mode default OR admin-customised ``PlatformSettings.platform_name``)
    // is set below from ``agentConfig.platform_name`` once the public
    // ``/chat/{token}/config`` request resolves, so the widget never
    // advertises a deployment mode the host instance doesn't run in.
    '<div class="fc-disclaimer">AI can make mistakes. Check important information.</div>' +
    '<div class="fc-footer">Powered by <a href="https://filigran.io" target="_blank" rel="noopener">Filigran</a></div>';
  widget.appendChild(panel);

  // Element refs
  var messagesEl = panel.querySelector(".fc-messages");
  var inputEl = panel.querySelector(".fc-input");
  var sendBtn = panel.querySelector(".fc-send-btn");
  var titleEl = panel.querySelector(".fc-header-title");
  var footerLinkEl = panel.querySelector(".fc-footer a");
  var resizeHandle = panel.querySelector(".fc-resize");
  var maximizeBtn = panel.querySelector(".fc-maximize-btn");
  var restoreBtn = panel.querySelector(".fc-restore-btn");
  var attachBtn = panel.querySelector(".fc-attach-btn");
  var fileInput = panel.querySelector(".fc-file-input");
  var attachTray = panel.querySelector(".fc-attachments");
  var attachError = panel.querySelector(".fc-attach-error");

  // Apply loaded size (non-default) as inline styles
  if (panelW !== DEFAULT_W || panelH !== DEFAULT_H) {
    panel.style.width = panelW + "px";
    panel.style.height = panelH + "px";
  }

  // ── Header button visibility ──────────────────────────────────────────
  function updateHeaderButtons() {
    var atDefault = !panel.style.width && !panel.style.height && !isMaximized;
    // Hide maximize when maximized; hide restore when at default size
    maximizeBtn.classList.toggle("fc-hidden", isMaximized);
    restoreBtn.classList.toggle("fc-hidden", atDefault);
  }

  // Set initial button visibility
  updateHeaderButtons();

  // ── Maximize / restore ────────────────────────────────────────────────
  function maximizePanel() {
    isMaximized = true;
    panel.style.width = "";
    panel.style.height = "";
    panel.classList.add("maximized");
    panelW = panel.offsetWidth;
    panelH = panel.offsetHeight;
    updateHeaderButtons();
  }

  function restorePanel() {
    isMaximized = false;
    panel.classList.remove("maximized");
    panel.style.width = "";
    panel.style.height = "";
    panelW = DEFAULT_W;
    panelH = DEFAULT_H;
    try { localStorage.removeItem(SIZE_KEY); } catch (e) {}
    updateHeaderButtons();
  }

  // ── Resize logic ──────────────────────────────────────────────────────
  var isResizing = false;
  var resizeStartX, resizeStartY, resizeStartW, resizeStartH;

  resizeHandle.addEventListener("mousedown", function (e) {
    e.preventDefault();
    e.stopPropagation();
    isResizing = true;
    resizeStartX = e.clientX;
    resizeStartY = e.clientY;
    resizeStartW = panel.offsetWidth;
    resizeStartH = panel.offsetHeight;
    document.body.style.cursor = "nw-resize";
    document.body.style.userSelect = "none";
    // Exit maximized state on manual resize
    if (isMaximized) {
      isMaximized = false;
      panel.classList.remove("maximized");
    }
  });

  document.addEventListener("mousemove", function (e) {
    if (!isResizing) return;
    var dx = resizeStartX - e.clientX;
    var dy = resizeStartY - e.clientY;
    var newW = Math.max(MIN_W, Math.min(MAX_W, resizeStartW + dx));
    var newH = Math.max(MIN_H, Math.min(MAX_H, resizeStartH + dy));
    panel.style.width = newW + "px";
    panel.style.height = newH + "px";
    panelW = newW;
    panelH = newH;
  });

  document.addEventListener("mouseup", function () {
    if (!isResizing) return;
    isResizing = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    try {
      localStorage.setItem(SIZE_KEY, JSON.stringify({ w: panelW, h: panelH }));
    } catch (e) {}
    updateHeaderButtons();
  });

  // Touch support for resize
  resizeHandle.addEventListener("touchstart", function (e) {
    e.preventDefault();
    var t = e.touches[0];
    isResizing = true;
    resizeStartX = t.clientX;
    resizeStartY = t.clientY;
    resizeStartW = panel.offsetWidth;
    resizeStartH = panel.offsetHeight;
    if (isMaximized) {
      isMaximized = false;
      panel.classList.remove("maximized");
    }
  }, { passive: false });

  document.addEventListener("touchmove", function (e) {
    if (!isResizing) return;
    var t = e.touches[0];
    var dx = resizeStartX - t.clientX;
    var dy = resizeStartY - t.clientY;
    var newW = Math.max(MIN_W, Math.min(MAX_W, resizeStartW + dx));
    var newH = Math.max(MIN_H, Math.min(MAX_H, resizeStartH + dy));
    panel.style.width = newW + "px";
    panel.style.height = newH + "px";
    panelW = newW;
    panelH = newH;
  }, { passive: true });

  document.addEventListener("touchend", function () {
    if (!isResizing) return;
    isResizing = false;
    try {
      localStorage.setItem(SIZE_KEY, JSON.stringify({ w: panelW, h: panelH }));
    } catch (e) {}
    updateHeaderButtons();
  });

  // ── API helpers ───────────────────────────────────────────────────────
  async function apiGet(path) {
    var resp = await fetch(API + path);
    if (!resp.ok) throw new Error("API error: " + resp.status);
    return resp.json();
  }

  async function apiPost(path, body) {
    var resp = await fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!resp.ok) throw new Error("API error: " + resp.status);
    return resp.json();
  }

  // ── Visitor uploads ───────────────────────────────────────────
  // Offered only when the config says the widget takes files: the attach
  // button, paste and drop. The server checks every file again and says why
  // it refuses one; names are written as text, never as markup.
  function uploadsEnabled() {
    return !!UPLOADS;
  }

  function hasFiles(dataTransfer) {
    return !!dataTransfer && Array.prototype.indexOf.call(dataTransfer.types || [], "Files") >= 0;
  }

  function showAttachError(message) {
    attachError.textContent = message || "";
    attachError.classList.toggle("fc-hidden", !message);
  }

  function updateSendState() {
    sendBtn.disabled = isLoading || (!inputEl.value.trim() && pendingFiles.length === 0);
  }

  function renderAttachments() {
    attachTray.replaceChildren();
    var shown = pendingFiles.filter(function (pending) { return !pending.sending; });
    shown.forEach(function (pending) {
      var chip = document.createElement("span");
      chip.className = "fc-attachment";
      var name = document.createElement("span");
      name.className = "fc-attachment-name";
      name.textContent = pending.file.name + " (" + formatFileSize(pending.file.size) + ")";
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "fc-attachment-remove";
      remove.setAttribute("aria-label", "Remove " + pending.file.name);
      remove.textContent = "\u00d7";
      remove.addEventListener("click", function () {
        pendingFiles = pendingFiles.filter(function (other) { return other !== pending; });
        renderAttachments();
        updateSendState();
      });
      chip.appendChild(name);
      chip.appendChild(remove);
      attachTray.appendChild(chip);
    });
    attachTray.classList.toggle("fc-hidden", shown.length === 0);
  }

  // The config's rules checked up front, so a visitor learns at once what the
  // server would refuse.
  function addFiles(files) {
    if (!uploadsEnabled()) return;
    var accepted = String(UPLOADS.accept || "").split(",");
    var problems = [];
    Array.prototype.forEach.call(files || [], function (file) {
      var dot = file.name.lastIndexOf(".");
      var ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
      if (accepted.indexOf(ext) < 0) {
        problems.push(file.name + ": files of this type cannot be attached.");
      } else if (file.size > UPLOADS.max_bytes) {
        problems.push(file.name + ": the file is too large (the limit is " + formatFileSize(UPLOADS.max_bytes) + ").");
      } else if (pendingFiles.length >= UPLOADS.max_files) {
        problems.push(file.name + ": at most " + UPLOADS.max_files + " files can be attached.");
      } else {
        pendingFiles.push({ file: file, uploaded: null });
      }
    });
    showAttachError(problems.join(" "));
    renderAttachments();
    updateSendState();
  }

  // Stores one file in this session; answers { file_id, filename, content_type, size }.
  // A refusal carries its status, so the caller can tell a retry from a no.
  async function uploadFile(file) {
    var form = new FormData();
    form.append("file", file, file.name);
    var resp = await fetch(sessionRoute() + "/files", { method: "POST", body: form });
    var data = null;
    try { data = await resp.json(); } catch (e) {}
    if (!resp.ok) {
      var refusal = new Error((data && typeof data.detail === "string" && data.detail) || "The file could not be attached.");
      refusal.status = resp.status;
      throw refusal;
    }
    return data;
  }

  // Busy, rate-limited, unreachable or out of room for now: worth trying again later.
  function isRetryable(error) {
    var status = error && error.status;
    return !status || status === 429 || status >= 500;
  }

  // ── Visitor & session persistence ─────────────────────────────────────
  function _setCookie(name, value, days) {
    var expires = "";
    if (days) {
      var d = new Date();
      d.setTime(d.getTime() + days * 864e5);
      expires = "; expires=" + d.toUTCString();
    }
    document.cookie = name + "=" + encodeURIComponent(value) + expires + "; path=/; SameSite=Lax";
  }

  function _getCookie(name) {
    var match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[1]) : null;
  }

  function _uuid() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) {
      return crypto.randomUUID();
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  function getVisitorId() {
    var id = null;
    try { id = localStorage.getItem(VISITOR_KEY); } catch (e) {}
    if (!id) id = _getCookie(VISITOR_COOKIE);
    if (!id) id = _uuid();
    try { localStorage.setItem(VISITOR_KEY, id); } catch (e) {}
    _setCookie(VISITOR_COOKIE, id, 365);
    return id;
  }

  var visitorId = getVisitorId();

  function saveSession() {
    try { localStorage.setItem(SESSION_KEY, sessionId || ""); } catch (e) {}
  }

  function loadSession() {
    try { return localStorage.getItem(SESSION_KEY) || null; } catch (e) { return null; }
  }

  function clearSession() {
    sessionId = null;
    messages = [];
    try { localStorage.removeItem(SESSION_KEY); } catch (e) {}
  }

  // ── Thinking indicator management ─────────────────────────────────────
  var THINKING_PHASES = [
    { icon: ICONS.brain, label: "Thinking", pulse: true },
    { icon: ICONS.sparkle, label: "Analyzing", pulse: true },
    { icon: null, label: "Composing answer", dots: true },
  ];

  function startThinking() {
    thinkingPhase = 0;
    thinkingTimer = setInterval(function () {
      if (thinkingPhase < THINKING_PHASES.length - 1) {
        thinkingPhase++;
        updateThinkingIndicator();
      }
    }, 4000);
  }

  function stopThinking() {
    if (thinkingTimer) {
      clearInterval(thinkingTimer);
      thinkingTimer = null;
    }
    thinkingPhase = 0;
  }

  // Written with innerHTML as is: it may only ever interpolate THINKING_PHASES.
  function buildThinkingHTML(phase) {
    var p = THINKING_PHASES[phase] || THINKING_PHASES[0];
    var iconHtml;
    if (p.dots) {
      iconHtml =
        '<div class="fc-thinking-dots">' +
          '<span></span><span></span><span></span>' +
        '</div>';
    } else {
      iconHtml =
        '<span class="fc-thinking-icon' + (p.pulse ? ' fc-pulse' : '') + '">' +
          p.icon +
        '</span>';
    }
    return '<div style="display:flex;align-items:center;gap:10px">' +
      iconHtml + '<span class="fc-thinking-label">' + p.label + '\u2026</span>' +
      '</div><div class="fc-thinking-text"></div>';
  }

  function updateThinkingIndicator() {
    var el = messagesEl.querySelector(".fc-thinking");
    if (!el) return;
    el.innerHTML = buildThinkingHTML(thinkingPhase);
    renderThinkingText();
    requestAnimationFrame(function () {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    });
  }

  // Write the reasoning so far into the indicator. Rebuilding the indicator
  // (phase label, re-render) recreates the box empty, so every rebuild calls
  // this too.
  function renderThinkingText() {
    var textEl = messagesEl.querySelector(".fc-thinking-text");
    if (!textEl) return;
    textEl.textContent = thinkingTextBuffer.trim();
    textEl.scrollTop = textEl.scrollHeight;
    textEl.classList.toggle("fc-overflowing", textEl.scrollHeight > textEl.clientHeight + 1);
  }

  function updateThinkingLabel(label) {
    stopThinking();
    var el = messagesEl.querySelector(".fc-thinking");
    if (!el) return;
    var labelEl = el.querySelector(".fc-thinking-label");
    if (labelEl) {
      labelEl.textContent = label;
    }
  }

  // ── Rendering ─────────────────────────────────────────────────────────
  function renderMessages() {
    messagesEl.innerHTML = "";

    if (messages.length === 0) {
      var greeting = document.createElement("div");
      greeting.className = "fc-greeting";
      var name = agentConfig ? agentConfig.name : "AI Assistant";
      var desc = agentConfig && agentConfig.description
        ? agentConfig.description
        : "Ask me anything. I\u2019m here to help.";
      var greetingHtml =
        '<div class="fc-greeting-icon"><img src="' + LOGO_URL + '" alt="" /></div>' +
        '<h3>' + escHtml(name) + '</h3>' +
        '<p>' + escHtml(desc) + '</p>';

      // Suggested prompts
      var prompts = (agentConfig && agentConfig.suggested_prompts) || [];
      if (prompts.length > 0) {
        greetingHtml += '<div class="fc-suggestions">';
        prompts.forEach(function (prompt) {
          greetingHtml += '<button class="fc-suggestion-btn" type="button">' + escHtml(prompt) + '</button>';
        });
        greetingHtml += '</div>';
      }

      greeting.innerHTML = greetingHtml;

      // Bind click handlers for suggestion buttons
      greeting.querySelectorAll(".fc-suggestion-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var text = btn.textContent;
          if (text && !isLoading) {
            inputEl.value = text;
            sendMessage();
          }
        });
      });

      messagesEl.appendChild(greeting);
      return;
    }

    messages.forEach(function (m) {
      var el = document.createElement("div");
      el.className = "fc-msg " + m.role;
      if (m.role === "assistant") {
        el.innerHTML = renderAssistant(m.content, PLATFORM_ROOT, STRICT_RENDERING, sessionRoute());
        appendFileChips(el, m.files);
      } else {
        el.textContent = m.content;
        // The files the visitor attached.
        appendFileChips(el, m.files);
      }
      messagesEl.appendChild(el);
    });

    if (pollNotice && !isLoading) {
      var notice = document.createElement("div");
      notice.className = "fc-notice";
      notice.setAttribute("role", "status");
      notice.textContent = pollNotice;
      messagesEl.appendChild(notice);
    }

    if (isLoading) {
      var thinking = document.createElement("div");
      thinking.className = "fc-thinking";
      thinking.innerHTML = buildThinkingHTML(thinkingPhase);
      messagesEl.appendChild(thinking);
      renderThinkingText();
    }

    // Use rAF to scroll after the browser has computed layout for the
    // newly-inserted DOM nodes (especially markdown-rendered content).
    requestAnimationFrame(function () {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    });
  }

  function escHtml(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  // Writes a rendered answer into a streamed bubble, keeping the session
  // images it already shows: its answers are "no-store", so a re-created one
  // could be fetched again on every paint, spending the session's download
  // budget. The markup is parsed in a document with no browsing context,
  // where nothing loads, then moved in.
  var inertDocument = document.implementation.createHTMLDocument("");
  function paintAnswer(el, html) {
    var kept = {};
    el.querySelectorAll("img.fc-img").forEach(function (img) {
      var src = img.getAttribute("src");
      (kept[src] = kept[src] || []).push(img);
    });
    var box = inertDocument.createElement("div");
    box.innerHTML = html;
    box.querySelectorAll("img.fc-img").forEach(function (img) {
      var same = kept[img.getAttribute("src")];
      if (same && same.length) img.replaceWith(same.shift());
    });
    el.replaceChildren.apply(el, Array.prototype.slice.call(box.childNodes));
  }

  // This session's own route on the platform: the files an answer produced
  // and the images it shows load through it, on the capability the widget
  // already holds (the web token and the session id), never a sign-in.
  function sessionRoute() {
    return sessionId
      ? API + "/chat/" + encodeURIComponent(TOKEN) + "/sessions/" + encodeURIComponent(sessionId)
      : null;
  }

  // Files an answer produced or the visitor attached: each
  // one downloads through this session's own route on the platform, never
  // through the markdown.
  var FILE_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  function formatFileSize(n) {
    if (typeof n !== "number" || n < 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return Math.round(n / 1024) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function appendFileChips(el, files) {
    if (!sessionId || !Array.isArray(files) || files.length === 0) return;
    var box = document.createElement("div");
    box.className = "fc-files";
    files.forEach(function (f) {
      if (!f || typeof f.file_id !== "string" || !FILE_ID_RE.test(f.file_id)) return;
      var a = document.createElement("a");
      a.className = "fc-file";
      a.href = sessionRoute() + "/files/" + f.file_id;
      a.target = "_blank";
      a.rel = "noopener";
      var size = formatFileSize(f.size);
      a.textContent = (f.filename || "file") + (size ? " (" + size + ")" : "");
      box.appendChild(a);
    });
    if (box.childNodes.length) el.appendChild(box);
  }

  async function ensureSession() {
    if (sessionId) return;
    var data = await apiPost("/chat/" + TOKEN + "/sessions", { visitor_id: visitorId });
    sessionId = data.session_id;
    saveSession();
  }

  // Stores the attached files one by one before the message is sent. A file
  // the server refuses is taken out of the tray with its reason, one it could
  // not take for now (busy, rate-limited, unreachable) stays for a retry; the
  // ones already stored are kept, so sending again does not store them twice.
  async function uploadPending(attaching) {
    var current = null;
    try {
      await ensureSession();
      for (var i = 0; i < attaching.length; i++) {
        current = attaching[i];
        if (!current.uploaded) current.uploaded = await uploadFile(current.file);
      }
    } catch (e) {
      if (current) {
        if (!isRetryable(e)) {
          pendingFiles = pendingFiles.filter(function (other) { return other !== current; });
          renderAttachments();
        }
        showAttachError(current.file.name + ": " + e.message);
      } else {
        showAttachError("The files could not be attached. Please try again.");
      }
      return null;
    }
    return attaching.map(function (pending) { return pending.uploaded; });
  }

  // ── Send message (with SSE streaming) ─────────────────────────────────
  async function sendMessage() {
    var text = inputEl.value.trim();
    var attaching = uploadsEnabled() ? pendingFiles.slice() : [];
    if ((!text && attaching.length === 0) || isLoading) return;

    isLoading = true;
    sendBtn.disabled = true;
    pollNotice = null;
    var sentFiles = [];
    // The attached files stay in the tray, hidden, until the stream says the
    // server took the message ("accepted"): a message it refuses - an error
    // status, or an answer given without storing it - gives them back.
    var accepted = false;
    function giveBackAttaching(error) {
      if (accepted || !attaching.length) return;
      attaching.forEach(function (pending) {
        pending.sending = false;
        // A file the server no longer has is stored again on the retry.
        if (error && error.status === 400) pending.uploaded = null;
      });
      renderAttachments();
      updateSendState();
      // It says why (a file no longer there, a widget that stopped taking files).
      if (error && error.detail) showAttachError(error.detail);
    }
    if (attaching.length) {
      attachBtn.disabled = true;
      showAttachError("");
      var uploaded = await uploadPending(attaching);
      attachBtn.disabled = false;
      if (!uploaded) {
        isLoading = false;
        updateSendState();
        return;
      }
      sentFiles = uploaded;
      attaching.forEach(function (pending) { pending.sending = true; });
      renderAttachments();
    }

    inputEl.value = "";
    inputEl.style.height = "auto";
    var content = text || "See the attached files.";
    messages.push({ role: "user", content: content, files: sentFiles.length ? sentFiles : null });
    renderMessages();
    startThinking();
    var streamPaint = null;

    try {
      await ensureSession();

      var msgBody = { content: content };
      if (CONTEXT) msgBody.context = CONTEXT;
      if (sentFiles.length) msgBody.file_ids = sentFiles.map(function (f) { return f.file_id; });

      // Use streaming endpoint via fetch + ReadableStream
      var streamUrl = API + "/chat/" + TOKEN + "/sessions/" + sessionId + "/messages/stream";
      var resp = await fetch(streamUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(msgBody),
      });

      if (!resp.ok) {
        var failure = new Error("API error: " + resp.status);
        failure.status = resp.status;
        try {
          var refusal = await resp.json();
          if (refusal && typeof refusal.detail === "string") failure.detail = refusal.detail;
        } catch (ignored) {}
        throw failure;
      }

      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var streamBuffer = "";
      var accumulatedContent = "";
      var answerFiles = null;
      var streamFailure = null;
      // A turn was already answering this session: the message is stored and
      // answered with the next turn, which the poll below shows.
      var queuedBehindTurn = false;
      // The stream said its last word ("done"). A stream that ends without it
      // after "accepted" was cut with its turn (a platform restart): the
      // message is stored and the next process answers it, which the poll
      // below shows.
      var streamDone = false;
      var streamingEl = null;
      var streamingCursor = null;
      // The streamed bubble is painted at most once per animation frame, however
      // many chunks arrive in between; the final text is painted once below.
      streamPaint = frameScheduler(
        function (callback) { return requestAnimationFrame(callback); },
        function (id) { cancelAnimationFrame(id); },
        function () {
          if (!streamingEl) return;
          paintAnswer(streamingEl, renderAssistant(accumulatedContent, PLATFORM_ROOT, STRICT_RENDERING, sessionRoute(), true));
          streamingEl.appendChild(streamingCursor);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        }
      );

      while (true) {
        var result = await reader.read();
        if (result.done) break;

        streamBuffer += decoder.decode(result.value, { stream: true });
        var lines = streamBuffer.split("\n");
        streamBuffer = lines.pop() || "";

        for (var i = 0; i < lines.length; i++) {
          var line = lines[i].trim();
          if (!line.startsWith("data: ")) continue;
          try {
            var evt = JSON.parse(line.substring(6));
            if (evt.type === "status") {
              var st = evt.status;
              if (st === "tool_start") {
                var tools = evt.tools || [];
                var label = tools.length > 0 ? tools[0].replace(/_/g, " ") : "Using tools";
                updateThinkingLabel(label + "…");
              } else if (st === "thinking_text" && evt.content) {
                var chunk = evt.content.replace(/\*\*/g, "").replace(/#{1,6}\s+/g, "").replace(/`/g, "").replace(/[*\-•>]+/g, " ").replace(/\s+/g, " ");
                thinkingTextBuffer += chunk;
                if (thinkingTextBuffer.length > 2 * THINKING_TEXT_MAX_CHARS) {
                  // Never start on the low half of a surrogate pair the cut split.
                  thinkingTextBuffer = thinkingTextBuffer.slice(-THINKING_TEXT_MAX_CHARS).replace(/^[\uDC00-\uDFFF]/, "");
                }
                renderThinkingText();
              } else if (st === "stream_retract") {
                // Rare: text streamed as a provisional answer turned out to
                // precede tool calls — remove the provisional bubble (the
                // text re-arrives as thinking_text right after).
                if (streamingEl) {
                  streamPaint.cancel();
                  if (streamingEl.parentNode) streamingEl.parentNode.removeChild(streamingEl);
                  streamingEl = null;
                  streamingCursor = null;
                  accumulatedContent = "";
                  // Restore the thinking indicator that was torn down when
                  // streaming started — without it the re-arriving
                  // thinking_text has no target and the user sees an empty
                  // chat until the real answer streams.
                  if (!messagesEl.querySelector(".fc-thinking")) {
                    var retractThinking = document.createElement("div");
                    retractThinking.className = "fc-thinking";
                    retractThinking.innerHTML = buildThinkingHTML(0);
                    messagesEl.appendChild(retractThinking);
                    startThinking();
                  }
                }
              } else if (st === "thinking") {
                updateThinkingLabel("Thinking…");
              } else if (st === "composing") {
                updateThinkingLabel("Composing answer…");
              }
            } else if (evt.type === "stream" && evt.content) {
              accumulatedContent += evt.content;

              // First chunk: remove thinking indicator and create
              // the streaming element in its place.
              if (!streamingEl) {
                stopThinking();
                thinkingTextBuffer = "";
                var thinkingNode = messagesEl.querySelector(".fc-thinking");
                if (thinkingNode) thinkingNode.parentNode.removeChild(thinkingNode);
                streamingEl = document.createElement("div");
                streamingEl.className = "fc-msg assistant fc-streaming";
                messagesEl.appendChild(streamingEl);
                streamingCursor = document.createElement("span");
                streamingCursor.className = "fc-streaming-cursor";
                streamingCursor.textContent = "\u258C";
              }

              streamPaint.schedule();
            } else if (evt.type === "accepted") {
              accepted = true;
              if (attaching.length) {
                pendingFiles = pendingFiles.filter(function (pending) { return attaching.indexOf(pending) < 0; });
                renderAttachments();
              }
            } else if (evt.type === "queued") {
              queuedBehindTurn = true;
            } else if (evt.type === "done") {
              streamDone = true;
              accumulatedContent = evt.content || accumulatedContent;
              answerFiles = evt.files || null;
            } else if (evt.type === "error") {
              streamFailure = new Error(evt.content || "The message could not be sent.");
              streamFailure.status = evt.status || 0;
              streamFailure.detail = evt.content || "";
            }
          } catch (parseErr) {
            // Skip malformed SSE lines
          }
        }
      }
      // The server answered with a refusal instead of a turn.
      if (streamFailure) throw streamFailure;
      if (queuedBehindTurn) {
        updateThinkingLabel("Waiting for the previous answer…");
        pollForResponse();
        return;
      }
      if (accepted && !streamDone) {
        waitForCutAnswer(streamingEl);
        return;
      }

      // An answer the server gave without storing the message (a refused request).
      giveBackAttaching(null);
      messages.push({ role: "assistant", content: accumulatedContent, files: answerFiles });

      // If streaming produced visible content, finalize the element
      // in-place instead of tearing down and rebuilding the DOM (which
      // causes a visible blink).
      if (streamingEl) {
        streamPaint.cancel();
        streamingEl.className = "fc-msg assistant";
        paintAnswer(streamingEl, renderAssistant(accumulatedContent, PLATFORM_ROOT, STRICT_RENDERING, sessionRoute()));
        appendFileChips(streamingEl, answerFiles);
        isLoading = false;
        updateSendState();
        stopThinking();
        thinkingTextBuffer = "";
        inputEl.focus();
        return;
      }
    } catch (e) {
      if (streamPaint) streamPaint.cancel();
      if (accepted) {
        // The connection dropped after the message was stored: its answer
        // comes from the turn that resumes it.
        waitForCutAnswer(messagesEl.querySelector(".fc-streaming"));
        return;
      }
      console.error("[chat widget] Send error:", e);
      giveBackAttaching(e);
      messages.push({
        role: "assistant",
        content: "I\u2019m sorry, I couldn\u2019t process your request. Please try again.",
      });
    }

    isLoading = false;
    updateSendState();
    stopThinking();
    thinkingTextBuffer = "";
    renderMessages();
    inputEl.focus();
  }

  // ── Poll for in-flight response ──────────────────────────────────────
  var pollTimer = null;
  var POLL_INTERVAL = 3000;
  // A notice under the conversation once the poll gave up.
  var pollNotice = null;

  // poll-copy:start
  // The wait after a cut stream says the answer is being resumed, like the
  // queued wait says what it waits for; a poll that runs out says
  // so instead of dropping the thinking row silently.
  var MAX_POLL_DURATION = 5 * 60 * 1000;
  var CUT_WAIT_LABEL = "Resuming the answer\u2026";
  var POLL_TIMEOUT_NOTICE = "The answer is taking longer than expected. Reload to check again.";
  function pollTimedOut(start, now) {
    return now - start > MAX_POLL_DURATION;
  }
  // poll-copy:end

  // poll-done:start
  // The answer the widget waits for is in once the session holds more
  // messages than when the poll started, ends on an assistant message and no
  // turn runs any more. A message queued behind a turn in flight is answered
  // by the NEXT turn: the in-flight turn's answer lands after it in the list
  // ([U1, U2, A1]) while the session still runs, so an assistant message last
  // is not enough on its own.
  function pollAnswered(data, baselineCount) {
    var msgs = (data && data.messages) || [];
    var lastMsg = msgs[msgs.length - 1];
    return (
      msgs.length > baselineCount &&
      !!lastMsg &&
      lastMsg.role === "assistant" &&
      !(data && data.is_processing)
    );
  }
  // poll-done:end

  // A stream cut after the message was stored: the partial answer it showed
  // is dropped (the resumed turn answers whole) and the widget polls.
  function waitForCutAnswer(partialEl) {
    if (partialEl && partialEl.parentNode) partialEl.parentNode.removeChild(partialEl);
    renderMessages();
    updateThinkingLabel(CUT_WAIT_LABEL);
    pollForResponse();
  }

  function pollForResponse() {
    if (pollTimer) clearInterval(pollTimer);
    var start = Date.now();
    var baselineCount = messages.length;
    pollNotice = null;

    pollTimer = setInterval(async function () {
      if (pollTimedOut(start, Date.now())) {
        clearInterval(pollTimer);
        pollTimer = null;
        isLoading = false;
        sendBtn.disabled = !inputEl.value.trim();
        stopThinking();
        pollNotice = POLL_TIMEOUT_NOTICE;
        renderMessages();
        return;
      }
      try {
        var data = await apiGet("/chat/" + TOKEN + "/sessions/" + sessionId);
        if (pollAnswered(data, baselineCount)) {
          clearInterval(pollTimer);
          pollTimer = null;
          messages = data.messages || [];
          isLoading = false;
          sendBtn.disabled = !inputEl.value.trim();
          stopThinking();
          renderMessages();
        }
      } catch (e) {}
    }, POLL_INTERVAL);
  }

  // ── Restore session ───────────────────────────────────────────────────
  async function restoreSession() {
    var stored = loadSession();
    if (stored) {
      try {
        var data = await apiGet("/chat/" + TOKEN + "/sessions/" + stored);
        sessionId = stored;
        messages = data.messages || [];
        renderMessages();
        if (data.is_processing) {
          isLoading = true;
          sendBtn.disabled = true;
          renderMessages();
          startThinking();
          pollForResponse();
        }
        return;
      } catch (e) {
        try { localStorage.removeItem(SESSION_KEY); } catch (ex) {}
      }
    }

    if (visitorId) {
      try {
        var vdata = await apiGet("/chat/" + TOKEN + "/visitor/" + visitorId + "/latest");
        if (vdata.session_id) {
          sessionId = vdata.session_id;
          messages = vdata.messages || [];
          saveSession();
          renderMessages();
          if (vdata.is_processing) {
            isLoading = true;
            sendBtn.disabled = true;
            renderMessages();
            startThinking();
            pollForResponse();
          }
        }
      } catch (e) {}
    }
  }

  // ── Event handlers ────────────────────────────────────────────────────
  function togglePanel() {
    isOpen = !isOpen;
    panel.classList.toggle("visible", isOpen);
    btn.classList.toggle("open", isOpen);
    if (isOpen) {
      inputEl.focus();
      requestAnimationFrame(function () {
        messagesEl.scrollTop = messagesEl.scrollHeight;
      });
    }
  }

  btn.addEventListener("click", togglePanel);

  maximizeBtn.addEventListener("click", maximizePanel);
  restoreBtn.addEventListener("click", restorePanel);

  panel.querySelector(".fc-refresh-btn").addEventListener("click", function () {
    clearSession();
    // A file stored in the old session is not the new one's: store it again.
    pendingFiles.forEach(function (pending) { pending.uploaded = null; });
    renderMessages();
  });

  sendBtn.addEventListener("click", sendMessage);

  attachBtn.addEventListener("click", function () {
    if (!uploadsEnabled() || isLoading) return;
    fileInput.click();
  });

  fileInput.addEventListener("change", function () {
    addFiles(fileInput.files);
    fileInput.value = "";
  });

  // A pasted screenshot is attached; text copied from an office app carries a
  // picture of itself, so a paste that holds text stays text.
  inputEl.addEventListener("paste", function (e) {
    if (!uploadsEnabled() || !e.clipboardData) return;
    var pasted = e.clipboardData.files;
    if (!pasted || !pasted.length || e.clipboardData.getData("text/plain")) return;
    e.preventDefault();
    addFiles(pasted);
  });

  panel.addEventListener("dragover", function (e) {
    if (!uploadsEnabled() || !hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    panel.classList.add("fc-dragover");
  });

  panel.addEventListener("dragleave", function (e) {
    if (!panel.contains(e.relatedTarget)) panel.classList.remove("fc-dragover");
  });

  panel.addEventListener("drop", function (e) {
    panel.classList.remove("fc-dragover");
    if (!uploadsEnabled() || !hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    addFiles(e.dataTransfer.files);
  });

  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMessage();
    }
  });

  inputEl.addEventListener("input", function () {
    inputEl.style.height = "auto";
    var sh = inputEl.scrollHeight;
    var maxH = 100;
    inputEl.style.height = Math.min(sh, maxH) + "px";
    inputEl.style.overflowY = sh > maxH ? "auto" : "hidden";
    updateSendState();
  });

  // ── Initialize ────────────────────────────────────────────────────────
  function revealButton() {
    if (!btn.classList.contains("fc-ready")) {
      btn.classList.add("fc-ready");
    }
  }

  (async function init() {
    // Preload the logo image so the button doesn't flash an empty/broken
    // state. Reveal after load (or on error/timeout so we never get stuck).
    var logoReady = new Promise(function (resolve) {
      var img = new Image();
      img.onload = resolve;
      img.onerror = resolve;
      img.src = LOGO_URL;
      setTimeout(resolve, 3000);
    });

    try {
      agentConfig = await apiGet("/chat/" + TOKEN + "/config");
      STRICT_RENDERING = agentConfig.strict_rendering === true;
      UPLOADS = agentConfig.uploads || null;
      if (uploadsEnabled()) {
        fileInput.accept = String(UPLOADS.accept || "");
        attachBtn.classList.remove("fc-hidden");
      }
      if (agentConfig.name) titleEl.textContent = agentConfig.name;
      // Promote the resolved platform name into the footer link. We only
      // overwrite when the config request actually returned a non-empty
      // value; on a missing field or a failed request the brand-neutral
      // ``Filigran`` static fallback above remains, so the widget never
      // displays a wrong-mode product name.
      if (agentConfig.platform_name) {
        var footerEl = footerLinkEl || panel.querySelector(".fc-footer a");
        if (footerEl) footerEl.textContent = agentConfig.platform_name;
      }
    } catch (e) {
      console.error("[chat widget] Could not load agent config:", e);
    }

    await logoReady;
    revealButton();

    await restoreSession();
    renderMessages();
  })();
})();
