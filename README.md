# ARIA Assistant

Autonomous Recursive Intelligent Agent (Aria) is a multi-agent assistant with memory layers, method management, and a web interface.

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

LLM 默认使用 **OpenAI 官方 Python SDK**（`openai`）访问 **火山引擎方舟** OpenAI 兼容接口（`chat.completions`）。将 `OPENAI_BASE_URL` 改为百炼兼容地址并配置 `DASHSCOPE_API_KEY` 即可切回阿里云，见[百炼兼容说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)。

## Environment Variables

Copy `.env.example` to `.env` and set:

```env
# 方舟（默认）
ARK_API_KEY=your-ark-api-key
OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
MODEL_NAME=doubao-seed-2-0-lite-260215

# 百炼（可选，需同步修改 OPENAI_BASE_URL）
# DASHSCOPE_API_KEY=your-dashscope-key
# OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

### Pushing to Git (privacy)

- **Never commit `.env`** — it is listed in `.gitignore`; only `.env.example` belongs in the repo.
- **Local data** under `data/artifacts/`, `data/conversations/conversations.json`, `data/midterm_memory.json`, and **`data/methodology/methodologies.json`** / **`category_feedback.json`** are ignored so screenshots, chats, and your experience library stay off GitHub.
- If those JSON files were **already tracked** in an older commit, run once:  
  `git rm --cached data/methodology/methodologies.json data/methodology/category_feedback.json`  
  then commit (files stay on your disk). History may still contain old blobs; use [git filter-repo](https://github.com/newren/git-filter-repo) if you need to purge secrets from past commits.

密钥也可通过系统环境变量提供。全链路单模型：设置 `MODEL_NAME`（或 `config.py` 的 `DEFAULT_MODEL`）。

Security note:
- Never commit `.env` or real API keys.
- If a key was exposed previously, rotate/revoke it in your provider console.

## Run

Option 1 (Windows): double-click `launch_aria.bat` — starts the backend and opens the app in your browser.

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

## Action Confirmation Flow

- All executable operations are planned first, then require confirmation before execution.
- **In the chat UI you must click “Confirm and Execute”** (typing “confirm” only works when the backend is already in the pending-actions branch; the button is the reliable path). High-risk actions need a **second click** as the UI explains.
- Low/medium-risk actions require one confirmation.
- High-risk actions (for example full knowledge-base deletion) require double confirmation.
- The web UI provides confirmation buttons; API clients can call `POST /api/confirm_actions`.

## Collaboration panel (middle column)

- While a single `POST /api/process_input` request is in flight, the UI avoids aggressive polling that fully rebuilds the collaboration log, so the panel should not “flicker” every few hundred milliseconds.
- If **Server-Sent Events** disconnect (common with some dev servers or proxies), the app falls back to slower polling. Use a production WSGI server if you need stable live updates.

## Computer Control (Trusted Mode)

ARIA supports computer-control actions across:

- Terminal commands (`shell_run`)
- File operations (`file_write`, `file_move`, `file_delete`)

### Office files (.docx / .xlsx / .pptx)

With dependencies installed (`python-docx`, `openpyxl`, `python-pptx` in `requirements.txt`), `file_write` can generate **real** Office files when `params.path` uses those extensions:

- **`.docx`**: `params.content` (body, newline-separated paragraphs), optional `params.title`.
- **`.xlsx`** (and macro-free **`.xlsm`** filename): prefer `params.rows` as a 2D JSON array; otherwise `params.content` as line-oriented TSV or comma-separated cells.
- **`.pptx`**: `params.title`, plus `params.bullets` (string array) or `params.content` lines (first line can serve as title when `title` is omitted).

Use paths under `data/artifacts/…`, confirm execution in the UI, then open the download link from the execution summary (`/api/workspace_file?path=…`). First version: no images, charts, macros, or multi-sheet layouts beyond a single `Sheet1`.
- Browser operations (`browser_open`, `browser_click`, `browser_type`)
- Web research (`web_fetch`, `web_understand`: server-side HTTP fetch + optional LLM summary; see below)
- Desktop operations (`desktop_open_app`, `desktop_hotkey`)

### Web research limits

- **`web_understand`** fetches HTML over HTTP and strips visible text; it cannot run JavaScript, log into sites, or solve captchas. For search-style tasks the server prefers **DuckDuckGo HTML** (and may fall back to Bing) because many SERPs are JS-heavy; the local browser may still open **Baidu** for Chinese users when applicable.
- **No automatic “click the second search result”**: to analyze a specific article, paste its **direct URL** into the task or ask for a plan that uses that URL with `web_understand` / `web_fetch`. Full browser automation would require something like Playwright (not included here).

Trusted mode behavior:

- Low/medium-risk plans start automatically.
- High-risk plans require explicit confirmation (double-confirm).

Execution control APIs:

- `POST /api/execution/start`
- `POST /api/execution/pause`
- `POST /api/execution/resume`
- `POST /api/execution/abort`
- `POST /api/execution/takeover`
- `GET /api/execution/status?session_id=...`

Safety defaults:

- Path allowlist rooted at project workspace.
- Command blocklist for destructive shell commands.
- Max action steps and step timeout enforcement.
- Per-step audit report (`step_id`, `status`, `stdout`, `stderr`, `artifacts`, `screenshots`).
- **Action screenshots** (full-screen capture after `browser_*` / `desktop_*` steps) are **off by default**. Turn on the **chat panel checkbox** (“Capture full-screen…”) or set `ARIA_ACTION_SCREENSHOT=1` in `.env`; PNGs go under `data/artifacts/screenshots/`.

## Acceptance Checks

Use these prompts in the app to verify the full loop:

1. Enter a low-risk intent (example: "clean low-quality methods in knowledge base").
2. Confirm the generated plan once and verify an execution summary appears.
3. Enter a high-risk intent (example: "clear all methods in knowledge base").
4. Confirm once and verify the app asks for a second confirmation.
5. Confirm again and verify execution completes and logs are updated.

Expected behavior:
- No operation runs before confirmation.
- Unsupported action types are filtered out by allowlist.
- High-risk actions cannot execute without second confirmation.

Computer-control acceptance (three scenarios):

1. Browser flow: open page and perform one interaction, then verify execution session status.
2. File flow: write/move/delete within workspace and verify artifacts are created.
3. Terminal/high-risk flow: issue a risky command intent and verify double-confirm gate triggers.

## Troubleshooting

- If the batch file starts the server but the browser is blank, open `http://127.0.0.1:5000/app` directly.
- If backend fails to start, install dependencies again:
  `pip install -r requirements.txt`
- If model calls fail, verify `DASHSCOPE_API_KEY`, `OPENAI_BASE_URL`, and provider-side permissions.
