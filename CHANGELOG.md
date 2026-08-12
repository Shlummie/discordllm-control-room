# Changelog

## 2026-03-12

### Added

- heartbeat operator controls in the GUI, including:
  - a dedicated `Heartbeat` tab
  - status refresh
  - pause / resume / run-if-due controls
  - draft review, editing, approval, and rejection
- passive Discord activity ingestion so normal guild traffic can inform heartbeat decisions
- per-server heartbeat routing:
  - enable or disable heartbeat per server
  - select exactly one heartbeat channel per enabled server
  - optionally show typing before heartbeat sends per server
- Discord-aware heartbeat draft metadata in AGI-memory outbox payloads, including:
  - `guild_id`
  - `channel_id`
  - `reply_to_message_id`
  - `jump_url`
  - `reason`
  - `confidence`
  - `community_signal`

### Changed

- heartbeat decisions now use recent summarized Discord community activity instead of only generic heartbeat context
- the top-level README and `agi-memory/README.md` now document heartbeat modes, routing, and the required patch migration for existing DB volumes
- `migrations/000_patch_personhood.sql` now serves as the re-runnable upgrade path for the heartbeat routing and source-normalization function changes

### Fixed

- Discord activity memory flush failures caused by passing raw Python dicts into the working-memory `source_attribution` JSONB argument
- loss of Discord-specific source metadata during `normalize_source_reference(...)`, which previously stripped fields like `guild_id`, `channel_id`, and `reply_to_message_id`
- heartbeat drafts missing Discord routing metadata on existing DB volumes until the patch migration is applied

### Notes

- For safest rollout, start with `shadow mode`, then move to manual approval, then autonomous sending
- If you want heartbeat contained to one place, enable exactly one server route and one channel in the GUI

## 2026-03-11

### Added

- Discord voice channel support in the GUI
- YouTube URL / search playback through the `Voice` tab
- voice controls for:
  - join
  - play
  - stop
  - leave
- GUI voice status updates
- voice diagnostic logging from `discord.py`

### Changed

- `run.bat` now launches the app through the project-local `.venv` when available
- voice playback uses the local dependency stack instead of relying on system Python packages

### Fixed

- Discord voice handshake failures caused by older global `discord.py`
- missing runtime support for new Discord voice transport by installing:
  - `discord.py 2.7.1`
  - `PyNaCl`
  - `davey`
  - `yt-dlp`
- missing root project documentation by adding:
  - `README.md`
  - `CHANGELOG.md`

### Notes

- The app should be launched with `run.bat`
- If voice breaks again, first confirm the app is running from `.venv`
- `agi-memory` remains optional and was not changed as part of these fixes
