# DiscordLLM Control Room

A Windows desktop control room for a Discord bot backed by a local Ollama chat endpoint. The single-file Tkinter application manages bot lifecycle, prompts, recent conversation context, greetings, voice playback, optional speech generation, and an optional external long-term-memory/heartbeat service.

## Core capabilities

- Respond when mentioned and expose explicit chat commands.
- Send recent channel context and bounded per-user/channel history to Ollama.
- Configure a primary system prompt and lower-priority community prompt.
- Run from the Windows notification area.
- Configure per-server online greetings and user overrides.
- Join a selected Discord voice channel and play audio resolved by `yt-dlp` through FFmpeg.
- Generate an optional `!jitd` voice reply through a separately installed Qwen TTS project.
- Integrate with a separately installed cognitive-memory service for recall, goals, personhood prompts, and reviewed heartbeat drafts.

The public repository does not bundle a model, Ollama, FFmpeg, Qwen TTS, a memory service/database, generated audio, or third-party source trees.

## Architecture

```text
Tkinter operator UI -> BotRunner thread -> Discord gateway/API
          |                  |-> local Ollama HTTP endpoint
          |                  |-> optional memory service/Postgres
          |                  |-> optional Qwen TTS subprocess
          +------------------+-> yt-dlp + FFmpeg voice playback
```

## Install

Requirements: Windows, Python 3.10+, a Discord bot application/token, and an Ollama-compatible local endpoint. Voice playback additionally requires FFmpeg.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python discordllm_gui.py
```

`run.bat` prefers `.venv\Scripts\pythonw.exe` and falls back to `pythonw.exe`.

Set at minimum:

```env
DISCORD_TOKEN=replace_with_your_bot_token
OLLAMA_URL=http://127.0.0.1:11434/api/chat
OLLAMA_MODEL=replace_with_an_installed_model
```

The GUI no longer writes the Discord token or memory DSN to its settings JSON. Credentials are runtime environment variables. If an older private settings file ever contained a token, rotate that token before reuse; removing the field does not revoke it.

## Discord setup and permissions

Create and configure the bot in Discord's developer portal. Enable only the gateway intents and server permissions required by the features you use. Presence-based greetings require presence/member visibility; reading messages and voice features require their corresponding intents/permissions.

Test in a private server first. The bot can read message context, post messages, mention users, and join voice channels; these are meaningful actions affecting other people.

## Commands and integrations

The source defines chat, history, greeting, recall/memory, goal, and voice-related commands inside `BotRunner._main()`. Inspect that command section before deployment so the deployed behavior matches the permissions and moderation expectations of each server.

Optional integrations:

- `MEMORY_DSN` enables the external memory adapter only when its Python package/project is separately present.
- `QWEN_TTS_PROJECT_DIR` points to a separate TTS checkout containing its own environment and `qwen_voice.py`.
- `YT_DLP_PATH` and `FFMPEG_PATH` can override executable discovery.

YouTube/media playback must comply with the media source's terms and copyright. This project does not grant permission to download or rebroadcast content.

## Heartbeat safety

Heartbeat can draft or send unsolicited messages. Keep it disabled or in shadow/manual-approval mode until all of the following are verified:

- exactly intended servers/channels are enabled;
- quiet hours, hourly/daily quotas, and reply-context policy are correct;
- drafts are grounded in appropriate recent context;
- external memory/outbox records are treated as untrusted data;
- operators can stop the bot and revoke its permissions quickly.

Autonomous posting should be an explicit informed choice, not a default. The optional memory worker and its migrations are not included in this repository.

## Privacy and retention

The app can process Discord message text, author/channel identifiers, presence changes, voice-channel metadata, prompts, conversation history, and optional memory records. Inform server members as required, minimize permissions and retention, and do not enable memory ingestion for communities that have not agreed to it.

Local settings may still contain prompts, guild/channel IDs, greeting targets, and window state, so `discordllm_gui.settings.json` is ignored. Ollama is local by default, but any changed model URL can transmit prompts elsewhere.

## Limitations

- Large single-file application with optional paths that require separate services and more integration tests.
- Discord API behavior, intents, voice transport, and rate limits can change.
- Model output can be incorrect, unsafe, or socially inappropriate; server moderation still applies.
- Tray, audio, FFmpeg, and path behavior are Windows-oriented.

## License

Original source is MIT licensed. Discord, Ollama, model weights, Qwen projects, memory services, FFmpeg, yt-dlp, and Python packages retain their own terms.
