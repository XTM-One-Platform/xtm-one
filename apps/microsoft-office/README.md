# Microsoft Office Add-in

Office Add-in for **Word**, **Excel**, and **PowerPoint** that connects to XTM One agents. Users can chat with agents, analyze document content, get AI-powered suggestions, and apply changes directly to their documents.

## Features

| Feature | Word | Excel | PowerPoint |
|---------|------|-------|------------|
| **Read selection** | Text selection | Cell/range values | Slide text |
| **Smart context** | Selection + surrounding paragraphs + heading outline | Selection + headers + sheet meta | Current slide + slide count |
| **Replace selection** | Replace text | Write to cells | Replace text |
| **Insert at end** | Append text | — | Add slide |
| **Add comment** | Selection comment | Cell comment | — |
| **Insert HTML** | Rich formatted content | — | — |
| **Insert table** | Structured data | — | — |
| **Markdown rendering** | Agent responses rendered with formatting | Same | Same |
| **Apply preview** | Diff dialog before applying changes | Same | Same |

### Quick Actions

- **Interpret** — Analyze and explain document content
- **Summarize** — Create a concise summary
- **Translate** — Translate selected text
- **Modify** — Rewrite/transform selected content
- **Improve** — Enhance writing quality (Word/PowerPoint)
- **Comment** — Add a review comment (Word/Excel)
- **Formula** — Help with formulas (Excel)
- **Insert Table** — Generate a table from content (Word)

## How it works

The add-in is served directly from the XTM One platform at `/office/` — no separate server required.

1. User opens Word/Excel/PowerPoint and clicks **XTM One** on the Home ribbon
2. If already logged into the XTM One web UI, the add-in **detects the session automatically** (shared localStorage on same origin) — no login needed
3. If not logged in, the add-in shows a "Sign in to XTM One" button that opens the platform login page in a new tab. After signing in, returning to the add-in picks up the session.
4. User picks an agent (or the **General Assistant**) from the list
5. Document context (selection + surrounding content, not the full document) is automatically injected into prompts
6. Agent responses are rendered as markdown with **Replace**, **Insert**, or **Comment** apply buttons

## Production Deployment

The add-in is built into the platform Docker image automatically. After deploying the platform, the add-in is available at `https://your-platform-domain/office/`.

### Manifest generation

The XML manifests are generated from templates in `manifest/` — **do not edit the generated files directly**.

The app name is resolved in this order:
1. `PLATFORM_NAME` env var — overrides everything (e.g. white-labelled deployments)
2. `PLATFORM_MODE=copilot` or `dev` → **Filigran Copilot**
3. Default (`PLATFORM_MODE=xtm_one`, the platform's default) → **XTM One**

Regenerate all manifests:

```bash
# Default (XTM One)
./apps/generate-manifests.sh

# Copilot mode
PLATFORM_MODE=copilot ./apps/generate-manifests.sh

# From your .env file
PLATFORM_MODE=$(grep -E '^PLATFORM_MODE=' .env | head -1 | cut -d= -f2- | tr -d "\"'") \
PLATFORM_NAME=$(grep -E '^PLATFORM_NAME=' .env | head -1 | cut -d= -f2- | tr -d "\"'") \
./apps/generate-manifests.sh
```

| Template | Generated file | Purpose |
|---|---|---|
| `manifest.prod.template.xml` | `manifest.prod.xml` | Production (Word / Excel / PowerPoint) |
| `manifest.outlook.template.xml` | `manifest.outlook.xml` | Outlook |
| `manifest.dev.template.xml` | `manifest.xml` | Dev — Vite on `https://localhost:3100` |

### Upload the manifest to Office (Word / Excel / PowerPoint)

1. Run `./apps/generate-manifests.sh` to regenerate `manifest/manifest.prod.xml`
2. Replace all occurrences of `COPILOT_URL` with your platform instance URL (e.g. `https://xtm-one.example.com`)
3. Upload the manifest using one of these methods:

**Microsoft 365 Admin Center (recommended for organizations):**
- Go to [admin.microsoft.com](https://admin.microsoft.com) → Settings → Integrated apps → Upload custom apps
- Upload `manifest.prod.xml`
- The add-in will be available to all users in your organization

**Word on the Web (individual testing):**
- Open a document at [office.com](https://office.com)
- Go to **Insert → Office Add-ins → Upload My Add-in**
- Upload `manifest.prod.xml`

**Word Desktop via shared folder (individual testing):**
1. Place `manifest.prod.xml` in a shared folder
2. In Word: **File → Options → Trust Center → Trust Center Settings → Trusted Add-in Catalogs**
3. Add the UNC path to the shared folder, check "Show in Menu"
4. Restart Word, then **Insert → My Add-ins → Shared Folder**

### Install the Outlook add-in

The Outlook add-in uses a **separate manifest** (`manifest.outlook.xml`) and a different install path from Word/Excel/PowerPoint.

**Prepare the manifest:**
1. Run `./apps/generate-manifests.sh` to regenerate `manifest/manifest.outlook.xml`
2. Replace all occurrences of `COPILOT_URL` in `manifest.outlook.xml` with your platform URL

**Microsoft 365 Admin Center (org-wide deployment — recommended):**
1. Go to [admin.microsoft.com](https://admin.microsoft.com) → **Settings → Integrated apps → Deploy add-in**
2. Choose **Upload custom app → I have the manifest file (.xml)**
3. Upload `manifest.outlook.xml`
4. Assign to specific users/groups or the entire org
5. The button appears in the Outlook ribbon within minutes (may need an Outlook restart)

**Outlook on the Web (individual):**
1. Open [outlook.office.com](https://outlook.office.com) → click the **Settings gear** (top right)
2. Go to **Add-ins** (search for it if not visible) → **My add-ins**
3. Scroll to **Custom add-ins** → **+ Add a custom add-in → Add from file**
4. Upload `manifest.outlook.xml`

**Outlook Desktop — Windows (individual):**
1. Open Outlook → **File → Manage Add-ins** (opens Outlook on the Web)
2. Follow the Outlook on the Web steps above

**Outlook Desktop — macOS (individual):**
1. Open Outlook → click **...** (More apps) in the top toolbar → **Get Add-ins**
2. Go to **My add-ins → Custom add-ins → + Add a custom add-in → Add from file**
3. Upload `manifest.outlook.xml`

**Where the button appears after installation:**
- **Reading an email** → Home tab ribbon (pinnable as a side panel)
- **Composing / replying** → Message tab ribbon
- **Calendar appointment** → Appointment/Meeting tab ribbon

> If the button is not visible, check the **overflow menu** (`...`) at the right end of the ribbon — Outlook collapses add-in buttons when the window is narrow.

### Authentication

The add-in does **not** have its own login form. Authentication works via the XTM One platform:

- **Same-origin (production):** The add-in at `/office/` shares `localStorage` with the XTM One frontend at `/`. If the user is logged in on the platform, the add-in picks up the JWT token automatically.
- **Not logged in:** The add-in shows a "Sign in to XTM One" link that opens the platform login page (`/login`) in a new tab. After login (local, OIDC, or SAML), the user returns to the add-in which detects the new session on window focus.

No CORS configuration is needed — the add-in and API share the same origin.

## Development

### Prerequisites

- Node.js 24 (22.18 at minimum)
- The XTM One backend running locally (port 8100)

### Serve via the local backend (recommended)

Build the add-in once, then the backend serves it at `/office/` automatically:

```bash
cd apps/microsoft-office
npm install
npm run build
```

Then start (or restart) the backend — it will detect `apps/microsoft-office/dist/` and mount it at `/office/`:

```
Office Add-in mounted at /office/ from .../apps/microsoft-office/dist
```

The add-in is now available at `http://localhost:8100/office/` on the same origin as the API. No CORS issues, session sharing works, and you can sideload the manifest pointing to `http://localhost:8100`.

To create a dev manifest for this setup, copy `manifest.xml` and replace `COPILOT_URL` with `http://localhost:8100`.

### Serve via Vite dev server (hot reload)

For rapid UI iteration with hot-reload:

```bash
npm run dev
```

This starts a Vite dev server at `https://localhost:3100`. Use `manifest/manifest.xml` (generated from `manifest.dev.template.xml`) which points there. CORS for `localhost:3100` is included in the backend's default config.

### Sideload the add-in

**Word on the Web:**
1. Open a document at [office.com](https://office.com)
2. **Insert → Office Add-ins → Upload My Add-in** → upload the manifest

**Word Desktop (Windows):**
```bash
npm run sideload          # Word
npm run sideload:excel    # Excel
npm run sideload:ppt      # PowerPoint
npm run sideload:stop     # Clean up
```

**macOS:**
```bash
cp manifest/manifest.xml ~/Library/Containers/com.microsoft.Word/Data/Documents/wef/
```

### Rebuild after changes

After editing add-in source files, rebuild to update what the backend serves:

```bash
npm run build
```

No backend restart needed — FastAPI serves the updated static files immediately.

## Architecture

```
microsoft-office/
├── manifest/
│   ├── manifest.prod.template.xml     # Template -> manifest.prod.xml
│   ├── manifest.outlook.template.xml  # Template -> manifest.outlook.xml
│   ├── manifest.dev.template.xml      # Template -> manifest.xml
│   ├── manifest.prod.xml     # Generated: production (replace COPILOT_URL)
│   ├── manifest.outlook.xml  # Generated: Outlook (replace COPILOT_URL)
│   └── manifest.xml          # Generated: dev (Vite on https://localhost:3100)
├── src/
│   ├── components/
│   │   ├── App.tsx            # Root: error boundary, theme, auth routing
│   │   ├── LoginScreen.tsx    # "Sign in to XTM One" redirect link
│   │   ├── OfficeChatPanel.tsx # The shared chat panel + document context, quick actions, Replace / Insert / Comment under an answer
│   │   └── ApplyPreview.tsx   # Diff preview dialog before applying changes
│   ├── hooks/
│   │   ├── useAuth.ts         # JWT session detection (shared with frontend)
│   │   ├── useOffice.ts       # Office.js init + the document context, refreshed on selection change
│   │   ├── usePlatformConfig.ts # The platform's public config (name, logo, mode)
│   │   └── useTheme.ts        # Dark/light theme detection
│   ├── lib/
│   │   ├── api.ts             # Session, base URL and REST client (same-origin aware)
│   │   ├── office.ts          # Word/Excel/PowerPoint document operations
│   │   ├── prompts.ts         # Smart three-tier context prompt builder
│   │   ├── actions.ts         # Structured action protocol parser
│   │   ├── panelConfig.ts     # The panel's routes (the chat page's), upload limit, storage prefix
│   │   ├── sends.ts           # Keeps the typed text in the composer until a taken-over send lands
│   │   ├── turns.ts           # Sends one question per turn (a document review)
│   │   └── types.ts           # TypeScript types
│   ├── commands.ts            # Ribbon command handler
│   ├── main.tsx               # React entry point
│   └── index.css              # Filigran design system (dark + light themes)
├── package.json
├── tsconfig.json
└── vite.config.ts             # base: /office/ in production
```

## API Endpoints Used

| Endpoint | Usage |
|----------|-------|
| `GET /api/v1/auth/me` | Detect existing session |
| `/api/v1/web/chat/...` (`src/lib/panelConfig.ts`) | The web chat's routes: agents, conversations and their history, messages (SSE), Stop, steering, approvals, uploads, the conversation's events stream (SSE) |
| `GET /api/v1/chat/files/{id}/download`, `/api/v1/chat/tts` | File downloads and read aloud |

The pane is the shared `@filigran/chatbot` panel (`src/components/OfficeChatPanel.tsx`), which follows turns started elsewhere in the conversation. Each question goes with the document's context while the bubble shows what the user typed (`onBeforeSend`, `displayText`), and a review of the whole document asks one question per section, one turn each (`src/lib/turns.ts`). Replace (through `ApplyPreview`), Insert below, Comment and Discard sit under an answer, and an answer's `action` block is applied, never shown as text.

Unit tests for `src/lib/` run with the web app's suite (`cd frontend && yarn test:unit`: `tests_unit/office-addin-actions.test.ts`, `office-addin-base-url.test.ts`, `office-addin-panel-config.test.ts`, `office-addin-prompts.test.ts`, `office-addin-sends.test.ts`, `office-addin-turns.test.ts`); `npm run check` type-checks the add-in and runs its component tests. CI runs the unit tests in its Frontend unit tests jobs in the merge queue, and `npm run check`, after `npm ci`, in its Office add-in lint job on a pull request; `./scripts/ci-local.sh --only frontend` runs both.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Your add-in manifest is not valid" | Check that `COPILOT_URL` is replaced with a valid HTTPS URL. The `<Id>` must be a valid hex GUID. Icons must be PNG files. |
| Blank taskpane | Verify the platform is running and `/office/` returns the add-in. Check browser console for errors. |
| "Sign in to XTM One" always shows | Open the XTM One web UI in a browser tab and log in first. The add-in shares the session via localStorage. |
| Session not detected after login | Return to Word and click inside the taskpane — it reloads on window focus. |
| Document operations fail | Ensure ReadWriteDocument permission in the manifest. Some features need WordApi 1.4+. |
| Chat stream disconnects | The panel reopens the events stream automatically (backoff up to 30 s). Check network settings, and that no proxy buffers `text/event-stream` responses. |
