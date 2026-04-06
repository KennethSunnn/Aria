# ARIA 项目规范

## 项目定位

ARIA（Autonomous Recursive Intelligent Agent）是一个 LLM 驱动的自主智能代理系统，基于 Python + Flask，支持桌面/浏览器/消息/文件等多种自动化操作。

## 架构概述

```
aria_manager.py          核心管理器：任务解析、Agent 调度、工具执行
web_app.py               Flask Web 应用：API 端点、SSE 事件流
runtime/
  taor_loop.py           TAOR 自主执行循环（Think-Act-Observe-Repeat）
  kairos.py              KAIROS 主动执行引擎（定时触发 + AutoDream 协调）
  auto_dream.py          AutoDream 后台记忆整合引擎
  trigger_scheduler.py   定时触发调度器（cron 持久化）
  permissions.py         五级权限谱与 plan 允许集合（SAFE / 只读 / PLAN_MODE_ALLOWED）
  shell_danger.py        shell 高风险规则（`shell_run` 路径统一校验）
  scheduler.py           依赖感知并行调度器
  orchestration.py       端到端管道门面
memory/
  memory_system.py       STM/MTM/LTM 三层记忆
  auto_memory.py         跨 Session 用户模式学习（默认开启）
  mcp_memory_server.py   MCP 内存服务器
automation/              桌面/浏览器/OCR 自动化层
data/
  methodology/           方法论库（LTM 持久化）
  scheduled_tasks.json   定时触发任务（TriggerScheduler 持久化）
```

## 核心约定

### 行为真源

- 权限与 plan 允许集合以 **`runtime/permissions.py`** 为准；`ARIAManager.SAFE_ACTION_TYPES` 与该模块导出的常量一致。
- 跨会话用户记忆由 **`memory/auto_memory.py`** 维护；TAOR 系统提示通过 **`AutoMemoryManager.get_system_prompt_fragment`** 注入（可用 `ARIA_AUTO_MEMORY_ENABLED=0` 关闭）。

### 新增 Action 类型时必须同步更新以下 4 处

1. `aria_manager.py` → `ALLOWED_ACTION_TYPES`（集合）
2. `aria_manager.py` → `action_registry`（字典，映射到处理函数）
3. `aria_manager.py` → 根据风险级别维护 `HIGH_RISK_ACTION_TYPES` / `USER_GATE_ACTION_TYPES`；若动作属于「只读 / plan 允许」语义，同步核对 `runtime/permissions.py` 中 `_READ_ONLY_TYPES` 与 `SAFE_ACTION_TYPES`（并由此推导的 `PLAN_MODE_ALLOWED_TYPES`）
4. `tests/test_action_registry_consistency.py` → 若有意外例外需显式声明

### 权限模型（五级）

| 级别 | 说明 |
|------|------|
| `plan` | 只读探测，允许 `PLAN_MODE_ALLOWED_TYPES`（`_READ_ONLY_TYPES` ∪ `SAFE_ACTION_TYPES`，见 `runtime/permissions.py`） |
| `default` | 写操作和 shell 需询问用户 |
| `accept_edits` | 文件/浏览器/桌面自动批准，shell/消息仍需询问 |
| `dont_ask` | 白名单内所有操作自动批准（high-risk 仍询问） |
| `bypass` | 所有操作自动批准（CI/CD 模式） |

环境变量：`ARIA_PERMISSION_LEVEL=default`

### TAOR 循环

- 启用：`ARIA_TAOR_MODE=1`
- 最大轮数：`ARIA_TAOR_MAX_TURNS=20`（上限 60）
- 输出格式：严格 JSON，字段 `thought / finish / final_result / is_success / action`
- 每轮只输出一个 action；不声称未执行的结果

### KAIROS 框架

- 启用：`ARIA_KAIROS_ENABLED=1`
- AutoDream 空闲触发：`ARIA_AUTODREAM_IDLE_SECONDS=300`
- AutoDream 最小间隔：`ARIA_AUTODREAM_INTERVAL_SECONDS=3600`
- 定时任务持久化：`data/scheduled_tasks.json`

## 测试要求

- 合并行为变更前必须运行回归基准
- `tests/test_action_registry_consistency.py` 验证 action 注册表一致性
- `tests/test_taor_permissions.py` 验证权限模型
- 新增 action 类型必须扩展一致性测试

## 关键文件索引

| 文件 | 用途 |
|------|------|
| `aria_manager.py:351` | `ARIAManager` 类定义 |
| `aria_manager.py:504` | `__init__` 初始化 |
| `runtime/taor_loop.py:40` | `TAORLoop` 类 |
| `runtime/kairos.py` | `KAIROSEngine` 类 |
| `runtime/auto_dream.py` | `AutoDreamEngine` 类 |
| `runtime/trigger_scheduler.py` | `TriggerScheduler` 类 |
| `runtime/permissions.py` | `PermissionModel`、`SAFE_ACTION_TYPES`、`PLAN_MODE_ALLOWED_TYPES` |
| `runtime/shell_danger.py` | shell 高风险检测（`shell_run` 与 `_sanitize_shell_command`） |
| `memory/auto_memory.py:88` | `AutoMemoryManager` 类 |
| `data/methodology/methodologies.json` | 方法论库 |
| `data/scheduled_tasks.json` | 定时任务持久化 |

## 安全边界

- 永远不声称文件/应用操作成功，除非执行报告明确返回 success
- 破坏性操作（file_delete、shell_run、kb_delete_all）必须经过确认路径
- `runtime/shell_danger.shell_command_blocked_reason` 匹配的命令（rm -rf /、format、DROP TABLE 等）在 `shell_run` 路径上均被拦截
