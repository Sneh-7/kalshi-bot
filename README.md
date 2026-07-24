# kalshi-bot

A trading bot for [Kalshi](https://kalshi.com), a CFTC-regulated prediction market
exchange for event contracts.

> **Project status: scaffold.**
> This repository currently contains a project skeleton only. `main.py` prints
> `Hello, World!` and there are no dependencies. No exchange integration, trading
> logic, or strategy code has been written yet.
>
> The intended design is documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
> Everything described there is a **plan, not an implementation**.

---

## What exists today

| Path               | Lines | Contents                                  |
| ------------------ | ----: | ----------------------------------------- |
| `main.py`          |     9 | Entry point stub; prints `Hello, World!`  |
| `requirements.txt` |     1 | Placeholder comment, no packages declared |
| `README.md`        |     — | This file                                 |
| `.gitignore`       |     — | Python artifacts, virtualenvs, secrets    |

That is the complete tracked codebase.

## Requirements

- Python 3.9 or newer (3.11+ recommended)
- macOS, Linux, or WSL

Verify your interpreter:

```bash
python3 --version
```

## Setup

Clone and enter the repository:

```bash
git clone https://github.com/Sneh-7/kalshi-bot.git
cd kalshi-bot
```

Create and activate a virtual environment. This keeps project packages isolated
from your system Python:

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

> `requirements.txt` is currently empty, so this is a no-op today. It will
> matter once real dependencies land.

## Running

```bash
python3 main.py
```

Expected output:

```
Hello, World!
```

If you see that, your environment is working. That is the full extent of current
functionality.

## Project layout

```
kalshi-bot/
├── main.py              # Entry point (stub)
├── requirements.txt     # Dependencies (empty)
├── README.md            # This file
├── .gitignore           # Ignored paths
└── docs/
    ├── ARCHITECTURE.md  # Planned design — NOT implemented
    └── SETUP.md         # Dev environment & GitHub configuration notes
```

## Configuration

No configuration is required yet. When exchange integration is added, credentials
will be read from a `.env` file, which is already listed in `.gitignore`.

**Never commit credentials.** This repository is public. Anything committed here is
world-readable, and secrets pushed to a public repo should be treated as
compromised even after deletion — git retains history, and automated scrapers
index public pushes quickly.

A future `.env` will look roughly like:

```bash
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=./secrets/kalshi_key.pem
KALSHI_ENV=demo          # demo | prod
```

## A note on `polymarket-sentiment-agent/`

You may see a `polymarket-sentiment-agent/` directory in your working copy. It is
**deliberately excluded** from this repository via `.gitignore`.

It is a separate upstream project
([`priyanshshahh/polymarket-sentiment-agent`](https://github.com/priyanshshahh/polymarket-sentiment-agent))
that happens to be checked out inside this folder. It has its own git history and
its own remote. It was previously committed here by accident as a broken gitlink —
a submodule pointer with no `.gitmodules` file — which would have produced an empty,
uninitializable directory for anyone cloning this repo. That entry has been removed.

Do not `git add` it.

## Roadmap

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full plan. In brief:

1. Kalshi REST client with authentication
2. Configuration and secrets loading
3. Market data ingestion
4. Strategy interface and a first baseline strategy
5. Paper-trading execution against Kalshi's demo environment
6. Risk limits and position sizing
7. Live execution (only after sustained paper-trading validation)

## License

MIT
