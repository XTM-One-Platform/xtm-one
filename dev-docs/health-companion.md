# The Concierge companion and its pets (#8261)

A floating, animated pet in the web app that shows what the Concierge, and the user's own work, is doing. Per user, off by default, behind `CONCIERGE`. Code: `backend/app/services/health/pets.py` (the package format and the library), `companion.py` (the preference and the activity), `api/v1/platform_health_companion.py`, model `models/health_pet.py`, migrations `c8cnc8254083` (the library) and `c8cnc8254084` (`users.concierge_companion`); the built-in Alfred under `app/agents/seed_data/health_pets/alfred/`, drawn by `backend/scripts/generate_alfred_pet.py`.

## The pet package (Codex pet format)

`pet.json` (`id`, `displayName`, `description`, `spritesheetPath`, optional `spriteVersionNumber: 2`) and a spritesheet of 8 columns of 192x208 cells: version 1 has 9 rows (1536x1872), version 2 adds rows 9-10, the look directions (1536x2288). One row per state, with the frame durations of the format's `scripts/generate-pet-previews.py` (`pets.STATES`): idle 6, running-right 8, running-left 8, waving 4, jumping 5, failed 8, waiting 6, running 6, review 6.

An upload (`POST /concierge/pets`, administrators) is one `.zip`, or `pet.json` and the image. It is hostile input:

| Check | Refusal code |
|---|---|
| Each file at most 8 MB, read bounded; a zip entry read bounded whatever its header declares; at most 32 entries | `too_large`, `not_a_package` |
| Exactly one `pet.json`, at the root or in one folder, at most 16 KB, a JSON object | `manifest` |
| `id` a lowercase slug (64), `displayName` 1-64 and `description` 280 characters, trimmed, no control, format or line-separator character; `spriteVersionNumber` 1 or 2 (strict types) | `manifest` |
| `spritesheetPath` a relative `.webp` or `.png` path that stays in the package (no `..`, no leading `/`, no `\`) | `manifest`, `spritesheet_missing` |
| PNG or WebP magic bytes (never SVG), Pillow reads the same format, one frame | `image_type` |
| The exact atlas size of the version, read from the header before any pixel is decoded | `image_size` |
| The pixels decode | `image_unreadable` |
| The id is free (a built-in's included) | `slug_taken` (409) |

A refusal answers its code and the rule (`pets.ERROR_MESSAGES`), never the input; the detail is logged with `%r`. The platform stores the spritesheet it re-encoded (lossless WebP, RGBA): no uploaded byte, metadata chunk or second frame reaches a browser. `GET /pets/{slug}/spritesheet` serves it as `image/webp` with `nosniff`, `default-src 'none'` and an ETag. The library is a platform one, not a shareable entity: every signed-in user lists and uses every pet; administrators add and remove them (audited). A built-in is never removed (409); a removed pet's users see the default.

## Alfred

The built-in pet and the default one: an original elderly butler in a tailcoat, bow tie and white gloves, with a silver tray and a pocket watch. No gallery asset (the awesome-codex-pet pets are CC BY-NC 4.0) and no likeness of any existing character is used: every pixel is drawn by `generate_alfred_pet.py` on a 48x52 grid scaled 4x. `tests/test_health_pets.py` regenerates the atlas in memory and fails when the committed one differs; rerun the script after a change.

## The preference and the activity

`users.concierge_companion` holds `{"enabled", "pet"}`; unset or invalid reads as off with the default pet. `GET` / `PUT /me/companion` are the signed-in user's own; an unknown pet is a 422.

`GET /me/activity` is what the pet shows, from what the user may already read and without any model call (`companion.companion_activity`), in this order:

| State | When |
|---|---|
| `failed` | A cycle in their scope, or one of their own runs, failed in the last 30 minutes |
| `waiting` | One of their own runs is `awaiting_human` |
| `review` | A proposal waits for a decision they may take (`approval_error` is `None`: owner, `config` share or group grant; not an administrator on a private target) |
| `running` | A cycle in their scope is open, or one of their own runs is running |
| `idle` | Otherwise |

Their scope is every cycle for an administrator and the cycles they asked for otherwise. The review count reads every pending proposal's target, so it is recomputed at most once a minute per user and process (`REVIEW_TTL`); a failed run whose `completed_at` is unset counts from its start. The web app adds the chat turn on screen (running while it streams), which never reaches the server.
