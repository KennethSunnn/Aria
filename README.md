# ARIA Assistant

Adaptive Resilient Intelligence Architecture (ARIA) is a multi-agent assistant with memory layers, method management, and a web interface.

## Features

- Three-layer memory system: STM, MTM, and LTM.
- Method/experience center with search, detail view, and update flows.
- Multi-agent execution with collaboration logs and workflow visualization.
- Flask-based web UI for conversations, method management, and task execution.
- Bilingual UI support (English default, switchable to Chinese).

## Setup

1. Clone the repository.
2. Create a virtual environment.
3. Install dependencies.

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate

pip install -r requirements.txt
```

## Environment Variables

Copy `.env.example` to `.env` and set:

```env
VOLCANO_API_KEY=your-api-key-here
```

You can also set `VOLCANO_API_KEY` in system environment variables.

Security note:
- Never commit `.env` or real API keys.
- If a key was exposed previously, rotate/revoke it in your provider console.

## Run

Option 1 (launcher files):
- Double-click `launch_aria.html` to open the launcher page.
- Double-click `launch_aria.bat` to auto-start backend and open the app.

Option 2 (direct Python):

```bash
python web_app.py
```

Then open:
- [http://127.0.0.1:5000/app](http://127.0.0.1:5000/app)

## Core Files

- `web_app.py`: Flask routes and API endpoints.
- `aria_manager.py`: task flow and multi-agent orchestration.
- `method_lib.py`: method library management.
- `memory/memory_system.py`: memory data structures and operations.
- `templates/simple_index.html`: main web app template.
- `templates/landing.html`: landing page.
- `static/js/i18n.js`: i18n runtime.
- `static/locales/en.json`, `static/locales/zh.json`: locale dictionaries.

## Troubleshooting

- If launcher opens but app is blank, open `http://127.0.0.1:5000/app` directly.
- If backend fails to start, install dependencies again:
  `pip install -r requirements.txt`
- If model calls fail, verify `VOLCANO_API_KEY` and provider-side permissions.
