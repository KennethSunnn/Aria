# ARIA - Autonomous Recursive Intelligent Agent

ARIA 是一个基于 LLM 的自主智能代理系统，使用 Python + Flask 构建，支持桌面、浏览器、文件、Shell 等多种自动化操作。

## 架构概览

```
aria_manager.py          核心管理器：任务解析、Action 调度、工具执行
web_app.py               Flask Web 应用：API 端点、SSE 事件流
config.py                模型池配置（MODEL_POOL）
runtime/
  taor_loop.py           TAOR 自主执行循环（Think-Act-Observe-Repeat）
  kairos.py              KAIROS 主动执行引擎（定时触发 + AutoDream 协调）
  auto_dream.py          AutoDream 后台记忆整合引擎
  trigger_scheduler.py   Cron 定时任务调度器（持久化到 data/scheduled_tasks.json）
  hybrid_planner.py      混合规划器（HybridPlanner + PlanTracker）
  permissions.py         五级权限模型（PermissionModel）
  shell_danger.py        Shell 高风险命令拦截
  timing_breakdown.py    执行耗时分解
  orchestration.py       端到端管道门面（OrchestrationFacade）
  scheduler.py           依赖感知并行调度器
memory/
  memory_system.py       STM / MTM / LTM 三层记忆
  auto_memory.py         跨会话用户模式学习（AutoMemoryManager）
  mcp_memory_server.py   MCP 内存服务器
llm/
  volcengine_llm.py      主 LLM 客户端（火山引擎 / ARK API）
  providers.py           多 Provider 抽象层（OpenAI / Anthropic / Groq / DeepSeek 等）
  model_config.py        Vision / Action 三模型分离配置
  groq_llm.py            Groq 独立客户端
automation/
  browser_driver.py      Playwright 浏览器自动化
  desktop_uia.py         Windows UIA 桌面自动化
  screen_ocr.py          屏幕 OCR（pytesseract）
  computer_use.py        Computer Use 截图 + 鼠标键盘控制
  osatlas_grounding.py   OS-Atlas 视觉定位（可选）
  interaction_intelligence.py  交互智能层
  app_profiles/          应用专属 Action 合并规则与 Prompt 片段
data/
  scheduled_tasks.json   定时任务持久化
  methodology/           方法论库（LTM 持久化）
```

## 快速开始

### 依赖安装

```bash
pip install -r requirements.txt
# Windows 桌面自动化需额外安装 pywinauto / pyautogui（requirements.txt 已按平台条件声明）
# 浏览器自动化需初始化 Playwright
playwright install
```

### 环境变量

复制 `.env.example` 为 `.env` 并填写：

```
# 主 LLM（火山引擎 ARK API）
ARK_API_KEY=...
MODEL_NAME=doubao-seed-2-0-lite-260215   # 默认值，可覆盖

# 可选：Computer Use 专用 Vision / Action 模型
ARIA_VISION_PROVIDER=groq
ARIA_VISION_MODEL=...
ARIA_ACTION_PROVIDER=openai
ARIA_ACTION_MODEL=...

# 权限级别：plan / default / accept_edits / dont_ask / bypass
ARIA_PERMISSION_LEVEL=default

# TAOR 自主循环
ARIA_TAOR_MODE=0
ARIA_TAOR_MAX_TURNS=20

# KAIROS 主动引擎
ARIA_KAIROS_ENABLED=0
ARIA_AUTODREAM_IDLE_SECONDS=300
ARIA_AUTODREAM_INTERVAL_SECONDS=3600

# 跨会话记忆
ARIA_AUTO_MEMORY_ENABLED=1
```

### 启动

```bash
python web_app.py
```

默认监听 `http://localhost:5000`。

## 核心模块

### TAOR 自主循环

`runtime/taor_loop.py` — Think-Act-Observe-Repeat 循环，每轮输出严格 JSON：

```json
{
  "thought": "...",
  "action": { "type": "...", ... },
  "finish": false,
  "final_result": null,
  "is_success": null
}
```

- 启用：`ARIA_TAOR_MODE=1`
- 最大轮数：`ARIA_TAOR_MAX_TURNS`（上限 60，默认 20）
- 内置上下文压缩（`_rebuild_messages_with_compact`）与任务面检测（`_detect_task_surface`）

### KAIROS 主动执行引擎

`runtime/kairos.py` — 后台守护线程，协调两个子系统：

- **TriggerScheduler**：Cron 表达式定时触发，任务持久化到 `data/scheduled_tasks.json`
- **AutoDreamEngine**：空闲时自动整合记忆、衰减旧条目

启用：`ARIA_KAIROS_ENABLED=1`

### 权限模型（五级）

`runtime/permissions.py`

| 级别 | 说明 |
|------|------|
| `plan` | 只读探测，仅允许 `PLAN_MODE_ALLOWED_TYPES` |
| `default` | 写操作和 Shell 需询问用户 |
| `accept_edits` | 文件 / 浏览器 / 桌面自动批准，Shell / 消息仍需询问 |
| `dont_ask` | 白名单内所有操作自动批准（高风险仍询问） |
| `bypass` | 所有操作自动批准（CI/CD 模式） |

高风险类型（`HIGH_RISK_ACTION_TYPES`）：`kb_delete_all` / `file_delete` / `shell_run` / `desktop_hotkey` / `desktop_type` / `desktop_sequence`

### Action 类型

共 41 个已注册 Action，分为以下类别：

| 类别 | Action |
|------|--------|
| 文件 | `file_write` `file_move` `file_delete` |
| Shell | `shell_run` |
| 浏览器 | `browser_open` `browser_click` `browser_type` `browser_find` `browser_hover` `browser_select` `browser_upload` `browser_scroll` `browser_wait` `browser_js` `browser_press` |
| 桌面 | `desktop_open_app` `desktop_hotkey` `desktop_type` `desktop_sequence` |
| Computer Use | `computer_screenshot` `computer_click` `computer_click_element` `computer_double_click` `computer_move` `computer_drag` `computer_scroll` `computer_key` `computer_type` `computer_wait` |
| 屏幕 | `screen_ocr` `screen_find_text` `screen_click_text` |
| Web | `web_fetch` `web_understand` |
| 媒体 | `media_summarize` |
| 知识库 | `kb_delete_all` `kb_delete_by_keyword` `kb_delete_low_quality` |
| 会话 | `conversation_new` `window_activate` |

### LLM 多 Provider

`llm/providers.py` 提供统一抽象，支持：OpenAI、Anthropic、Groq、DeepSeek、OpenRouter、Gemini、Fireworks、Llama、Mistral、Moonshot。

`llm/model_config.py` 支持为 Vision / Action 角色独立配置 Provider，通过 `ARIA_VISION_PROVIDER` / `ARIA_ACTION_PROVIDER` 等环境变量控制。

主 LLM 默认使用火山引擎 ARK API（`llm/volcengine_llm.py`），模型由 `MODEL_NAME` 指定，默认 `doubao-seed-2-0-lite-260215`。

### 记忆系统

- **三层记忆**（`memory/memory_system.py`）：STM（短期）/ MTM（中期）/ LTM（长期）
- **AutoMemory**（`memory/auto_memory.py`）：跨会话用户模式学习，通过 `get_system_prompt_fragment` 注入 TAOR 系统提示；`ARIA_AUTO_MEMORY_ENABLED=0` 可关闭
- **AutoDream**（`runtime/auto_dream.py`）：空闲触发的后台记忆整合，自动衰减 90 天以上旧条目

## 安全边界

- `runtime/shell_danger.py` 拦截所有高危 Shell 命令（`rm -rf /`、`format`、`DROP TABLE` 等）
- 破坏性操作（`file_delete`、`shell_run`、`kb_delete_all`）必须经过确认路径
- 永远不声称操作成功，除非执行返回明确的 success 结果

## 测试

```bash
python -m pytest tests/
```

关键测试：

- `tests/test_action_registry_consistency.py` — Action 注册表一致性
- `tests/test_taor_permissions.py` — 权限模型
- `tests/test_shell_danger.py` — Shell 危险命令拦截
- `tests/test_hybrid_planner.py` — 混合规划器
- `tests/test_timing_breakdown.py` — 耗时分解
- `tests/test_auto_memory_prompt_fragment.py` — AutoMemory 系统提示注入
