# Podcast Digest (Mindmonk — GLM-5.2 build)

Watches your favorite YouTube channels for new **long-form** uploads, pulls their
transcripts, and sends you a structured AI brief on Telegram.

Each processed podcast produces a four-section brief:

1. **💡 Key Insights** — the core ideas, condensed.
2. **🔁 Patterns & Anti-Patterns** — recurring good thinking vs. flawed reasoning.
3. **🔍 Unbiased Grading** — a critical, even-handed evaluation + letter grade,
   produced by your configured LLM acting as an independent grader.
4. **🎯 Tailored Learnings** — ideas matched against *your* profile → concrete,
   personalized action items.

No YouTube Data API key required — channel polling uses `yt-dlp`. You only need
a **Telegram bot token** and an **LLM API key** (OpenAI or Anthropic).

---

## Quick start

### 1. Clone & install

```bash
cd /opt
git clone <your-repo> podcast-digest
cd podcast-digest

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

Copy the example files and fill in your details:

```bash
cp .env.example .env
cp config.example.yaml config.yaml
cp profile.example.yaml profile.yaml
```

**`.env`** — secrets & provider:
| Var | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_CHAT_ID` | Your chat id — message [@userinfobot](https://t.me/userinfobot). |
| `LLM_PROVIDER` | `openai` or `anthropic`. |
| `LLM_API_KEY` | Your API key for the chosen provider. |
| `LLM_MODEL` | e.g. `gpt-4o-mini` or `claude-3-5-haiku-latest`. |
| `GRADER_MODEL` | *(optional)* Route section 3 to a different model. |

**`config.yaml`** — channels & polling:
- `channels:` — list of `{name, url}`. Use the channel's `/videos` URL.
- `min_duration_seconds` — long-form threshold (default 1200 = 20 min).
- `poll_interval_minutes` — how often to check (default 30).

**`profile.yaml`** — *you*. This drives section 4. The richer it is, the sharper
your tailored learnings:

```yaml
profession: "Senior Backend Engineer"
skill_level: "Mid-Senior"
goals:
  - "Transition into ML engineering"
interests:
  - "Distributed systems"
  - "Investing"
current_focus: "Building a side project in Go."
```

### 3. Test once

```bash
python main.py --once
```

This runs a single poll+process cycle. Check `logs/podcast-digest.log` and your
Telegram chat.

### 4. Run continuously

```bash
python main.py        # daemon: polls every poll_interval_minutes
```

---

## Deploy on a VPS (systemd)

```bash
# Install as above, then:
sudo cp systemd/podcast-digest.service /etc/systemd/system/
# Edit the file: set User and the python path to your venv.
sudo nano /etc/systemd/system/podcast-digest.service
sudo systemctl daemon-reload
sudo systemctl enable --now podcast-digest

# Check it:
sudo systemctl status podcast-digest
journalctl -u podcast-digest -f
```

The service auto-restarts on failure (`Restart=on-failure`).

---

## How it works

```
┌─────────┐   ┌──────────┐   ┌────────────┐   ┌─────────────┐   ┌──────────┐
│ Channels│──▶│ yt-dlp   │──▶│ long-form  │──▶│ transcript  │──▶│ LLM brief│
│ (YAML)  │   │ poll     │   │ filter     │   │ fetch       │   │ (4 sect.)│
└─────────┘   └──────────┘   └────────────┘   └─────────────┘   └─────┬────┘
                                                                      │
                                          ┌──────────────┐   ┌────────▼────────┐
                                          │  Telegram    │◀──│ SQLite dedup    │
                                          │  delivery    │   │ (processed once)│
                                          └──────────────┘   └─────────────────┘
```

- **Polling** (`src/youtube.py`): `yt-dlp` flat-playlist lists recent uploads
  with metadata; no downloads, no API key. Live/premieres/upcoming are skipped.
- **Dedup** (`src/store.py`): SQLite tracks every seen video — each is processed
  exactly once across restarts.
- **Transcripts** (`src/transcripts.py`): prefers manual captions, falls back to
  auto-generated, then translation. Skips videos with no captions.
- **Brief** (`src/prompts.py`, `src/summarizer.py`): two LLM calls — main
  (sections 1,2,4) and grading (section 3, optionally a different model).
  Retries with backoff.
- **Delivery** (`src/telegram.py`): Markdown messages, auto-split on section
  boundaries if over Telegram's 4096-char limit.

## Project layout

```
podcast-digest/
├── main.py                # entrypoint: --once or scheduled daemon
├── requirements.txt
├── .env.example           # secrets + provider config (placeholders)
├── config.example.yaml    # channels, poll interval, duration filter
├── profile.example.yaml   # your profile (drives section 4)
├── config/settings.py     # typed config loading
├── src/
│   ├── models.py          # Channel, Video, Transcript dataclasses
│   ├── store.py           # SQLite processed-video state
│   ├── youtube.py         # yt-dlp polling + long-form filter
│   ├── transcripts.py     # youtube-transcript-api fetch
│   ├── prompts.py         # the 4-section prompt templates
│   ├── summarizer.py      # OpenAI/Anthropic abstraction + assembly
│   ├── telegram.py        # Bot API delivery + message splitting
│   └── pipeline.py        # orchestration
└── systemd/
    └── podcast-digest.service
```

## Notes & limitations (v1)

- **Channel management is config-file only.** A Telegram `/add` command is a
  natural future addition.
- **Captions required.** If a video has no transcript, it's skipped (with an
  optional notice). Audio-based transcription (e.g. Whisper) is out of scope.
- **Delivery is a single message per video** (split if needed). Transcript-file
  attachments aren't included in v1.
- **YouTube's frontend can change**, which occasionally breaks `yt-dlp`. If
  polling fails, `pip install -U yt-dlp` usually fixes it.

## Troubleshooting

- **No messages arriving?** Run `--once`, check logs, and verify the bot can
  message you: the user must have started the conversation with the bot first.
- **`LLM_API_KEY is not set`?** You left the placeholder in `.env`.
- **yt-dlp errors?** Upgrade it: `pip install -U yt-dlp`.
