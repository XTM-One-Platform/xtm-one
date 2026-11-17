# Video Studio - Feature Flag

The Video Studio (design in `video-studio.md`) ships behind the
`VIDEO_STUDIO` feature flag, off by default. A render needs a worker
sized for Chromium and ffmpeg (a 4K render peaks at several GiB), which
a default deployment's worker is not, so a deployment turns the Studio on when
it sizes its worker for it. Like `DEEP_INVESTIGATION`, the flag is **enforced
by the backend**: with the flag off the feature is dark, not merely hidden.

## Enabling it

```
ENABLED_FEATURES=VIDEO_STUDIO
```

The flag is read once at startup, so set it on both the platform (it serves the
Studio and seeds its skill and templates) and the worker (it builds the tool in
assignment runs and runs the render jobs), and restart them. The resolved id
list reaches the frontend through `GET /auth/config`.

**`ENABLED_FEATURES=*` enables it too** - `*` means every known flag. List the
ids explicitly on a deployment whose worker is not sized for renders.

Size the worker before turning it on: requests of 6 GiB of memory and limits
of 12 GiB and 4 CPUs cover a 4K render (the charts' worker values).

## What the flag gates

| Surface | With the flag off |
| ------- | ----------------- |
| `/api/v1/video-studio*` | Every route answers 403 naming the flag (router-level `require_feature`, evaluated before authentication), and the routes are absent from `/api/openapi.json` |
| `/api/v1/public/videos*` | Every public link answers like a token nobody issued (`public_links.public_links_enabled`), and the routes are absent from `/api/openapi.json` |
| `video_studio` built-in tool | Never built, in agent chats (`agent_chat.py`), General Assistant chats (`general_chat.py`) and assignment runs (`assignment_engine.py`), even for an agent that carries the id |
| A conversation bound to a video | No studio context is read (`studio.load_video_studio_context` returns `None`) |
| `GET /settings/builtin-tools/catalog` | The `video_studio` key is omitted - filtered in the endpoint and in `_scannable_tool_groups`, *not* in `build_builtin_tool_catalog`, which doubles as the registry skill-package validation reads |
| Agent builder tool | `video_studio` is not named in the ids it advertises to the model (`_advertised_builtin_tool_ids`); its validation still accepts it, so an agent that carries it stays editable |
| Video Production skill (built-in) | Not seeded (`FEATURE_GATED_BUILTIN_SKILLS` in `builtin_agents.py`, read by `seed.seed_builtin_skills`) |
| Built-in video templates | Not seeded (`template_container_seed.builtin_containers_for`): the template seed, the every-boot `remove_unshipped_builtin_containers` and the template asset seed all read it |
| Sidebar, command palette | `Work > Videos` is not listed (`NavFlags.videoStudio`) |
| `/videos`, `/videos/<id>` | Redirect home (`FeatureFlagGate` in `App.tsx`) |
| Work hub | No Videos card, and no request for the videos |
| Built-in tool pickers | The suite is not offered (`BUILTIN_TOOLS` `gate: "video_studio"`, resolved by `useBuiltinTools`); an agent that carries it shows it disabled |
| Persisted `[[VIDEO:...]]` cards | Not rendered; the markers are still stripped from the message text |
| Settings > AI models | The Video Generation and Music Generation sections are not rendered; Speech Generation stays, the chat reads answers aloud with it |
| Model catalog announcements | The video and music models the environment pins are left out of the announced catalog (`CapabilityRole.feature_flag`, `env_capability_defaults.capability_offered`), so users are never emailed about them; an unsent change that carried them is folded into one without them at the next boot, and turning the flag on announces them as new. The speech model's email says what it does without the Studio (`model_catalog_announcements._CAPABILITIES`) |
| Templates | `Video` is not offered as a new template's kind, and a video template offers no `New video with this template` |

## What is kept

- Stored data: videos, renders, recordings, AI shots, music, browser profiles
  and public links. Nothing is migrated - turning the flag back on picks
  everything up again, public links included (they also follow the
  `video_public_links_enabled` security option).
- An agent's or the General Assistant's `builtin_tool_ids` may still list
  `video_studio`; the runtime withholds the tool.
- A person's own video templates stay listed in `Templates`: they can be read,
  duplicated and exported, not used.
- The two video security options stay in Settings > Security: they are part of
  the security audit, and they govern the Studio again when it comes back.
- Video studio jobs already queued run to their end; nothing new can enqueue
  one while the API and the tool are dark.

## Turning the flag OFF on an instance that already seeded the Studio

The built-in seeds are full syncs, so the next boot **deletes** what the flag
withholds:

- **The Video Production skill.** Agents that listed it keep a dangling id in
  `skill_ids`, which every skill reader skips. Turning the flag back on seeds it
  again under a new id, and phase 3 of the skill seed binds it to its default
  assistants again.
- **The built-in video templates**, with their assets. Videos made with one
  keep their content and lose the link (`ON DELETE SET NULL`), like decks and
  reports. Turning the flag back on seeds the
  templates again under new ids, so those videos stay free style until a
  template is picked again.

## Removing the flag

Once every deployment sizes its worker for renders, drop `VIDEO_STUDIO` from
`KNOWN_FEATURE_FLAGS` / `FeatureFlag` (`backend/app/core/feature_flags.py`) and
from `KNOWN_FEATURE_FLAGS` in `frontend/src/lib/utils.ts`, delete
`FEATURE_GATED_BUILTIN_SKILLS` and `_FEATURE_GATED_BUILTIN_TOOL_IDS` (and their
readers, if nothing else uses them), and remove the gates listed in the first
table.

`backend/tests/test_video_studio_feature_flag.py` pins all of it.
