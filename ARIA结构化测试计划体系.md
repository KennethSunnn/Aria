ARIA 结构化测试计划体系

## 实施状态（与仓库对齐）

| 项 | 状态 |
|----|------|
| `data/test_plans/main_plan.yaml` | 已落地（smoke / ci / full 分层） |
| `data/test_plans/e2e_plan.yaml` | 已添加占位（Layer 3，供真机扩展） |
| `scripts/run_test_plan.py` | 已落地（`--tier` / `--fail-fast` / JSON 报告） |
| `tests/test_automation_browser.py` | 已落地，`@pytest.mark.automation` |
| `tests/test_automation_desktop.py` | 已落地，`@pytest.mark.automation` |
| `tests/test_computer_use_extended.py` | 已落地，`@pytest.mark.automation` |
| 回归基准 browser/desktop mock | 已扩展：`browser_open` js_error 重试成功、`desktop_type` uia_busy 重试成功；并修正 `desktop_hotkey` 双次 uia_busy 的期望末态为 `failed` |
| `.github/workflows/ci.yml` | 已追加 `Test plan (CI tier)` 步骤 |
| `pyproject.toml` | 已注册 `automation` / `smoke` markers；冒烟三件套已标 `smoke` |
| `ARIAManager._resolve_agency_agents_root` | 已增加 `third_party/agency-agents` 候选路径 |
| `tests/test_personality_catalog_coverage.py` | 无 upstream 资产时 skip，避免 CI 裸仓库失败 |

验证命令见文末。

---

Context
当前状态：用户手动随机测试，效率低、无覆盖目标、无历史数据。 现有测试共 15 个测试文件，但自动化工具层（Browser/Desktop/Computer Use）几乎无覆盖：

Browser automation（browser_driver.py 416行）：0 测试
Desktop UIA（desktop_uia.py 156行）：0 测试
Computer Use 扩展动作（run_double_click/drag/scroll等）：0 测试
目标：搭建一套可自动化执行、分层覆盖、CI 可集成的测试计划体系。

方案：三层测试金字塔 + 测试计划 YAML
层次结构
Layer 3: E2E / 集成  ← data/test_plans/e2e_plan.yaml     (少，慢，需真实环境)
Layer 2: 回归基准    ← scripts/run_regression_benchmark.py (已有，扩展 browser/desktop)
Layer 1: 单元测试    ← tests/test_automation_browser.py    (新增 browser mock 测试)
                      tests/test_automation_desktop.py   (新增 desktop mock 测试)
                      tests/test_computer_use_extended.py (扩展现有 computer use)
测试计划 YAML 格式
data/test_plans/ 目录，每个 YAML 文件定义一个有优先级、可调度、可追踪的测试计划：

plan_id: automation_layer_v1
schedule: "0 2 * * *"         # cron (对接 TriggerScheduler)
on_ci: true                    # CI 也触发
min_pass_rate: 0.80
suites:
  - id: smoke
    description: "快速冒烟（< 30s）"
    priority: 1                # 最先跑
    pytest_marks: smoke
    max_failures: 0
  - id: browser_unit
    priority: 2
    pytest_marks: browser
    ...
  - id: regression
    priority: 3
    script: scripts/run_regression_benchmark.py
    args: [--min-match-rate, "0.60"]
新增文件清单
文件	内容
data/test_plans/main_plan.yaml	主测试计划定义（smoke→unit→regression→eval）
tests/test_automation_browser.py	Browser 自动化层 mock 单元测试（不真实启动 Playwright）
tests/test_automation_desktop.py	Desktop UIA mock 单元测试（不真实操作 pywinauto）
tests/test_computer_use_extended.py	Computer Use 扩展测试（drag/scroll/key/type/window）
scripts/run_test_plan.py	测试计划执行器（解析 YAML → 按优先级调度 → 生成报告）
修改文件清单
文件	修改内容
.github/workflows/ci.yml	增加 run_test_plan.py --plan main_plan.yaml --tier ci 步骤
scripts/run_regression_benchmark.py	增加 browser/desktop mock 回归用例（2+2条）
详细实现步骤
步骤 1：测试计划 YAML（data/test_plans/main_plan.yaml）
定义 4 个 suite，按优先级排序：

smoke（pytest mark=smoke）：< 30s，阻断式，任何失败立即停
automation_unit（pytest mark=automation）：browser + desktop + computer_use 单元测试
regression（script）：run_regression_benchmark.py 加强版
eval_passk（script，CI 可选）：run_evaluation.py k=3
步骤 2：tests/test_automation_browser.py
用 monkeypatch + MagicMock 测试 browser_driver.py 的逻辑层（不启动真实 Playwright）：

is_playwright_enabled() 环境变量控制
navigate() 参数验证和超时
click() CSS 选择器解析
type() 输入序列构造
find() 元素查询逻辑
Session 生命周期（ensure_session 幂等性）
错误码映射（timeout/not_found/js_error）
标记 @pytest.mark.automation

步骤 3：tests/test_automation_desktop.py
mock pywinauto 测试 desktop_uia.py：

is_uia_enabled() 环境变量控制
send_hotkey() 热键字符串解析
type_text() 输入字符串构造
pywinauto_package_installed() 依赖检测
错误处理：UIA busy / element not found
标记 @pytest.mark.automation

步骤 4：tests/test_computer_use_extended.py
扩展现有 test_computer_use.py，补全缺失测试：

run_double_click() → 坐标解析 + allow_region 检查
run_scroll() → direction 参数（up/down/left/right）
run_key() → key 名称映射
run_type_text() → 禁用时返回失败
run_drag() → 起终点都经过 allow_region 检查
foreground_window_title() + blocked_by_sensitive_title()
capture_jpeg_data_url() 格式校验
标记 @pytest.mark.automation

步骤 5：scripts/run_test_plan.py（测试计划执行器）
功能：
  - 读取 data/test_plans/<plan>.yaml
  - 按 priority 顺序执行各 suite
  - pytest suite：subprocess 调用 pytest -m <mark> -q
  - script suite：subprocess 调用指定脚本
  - 每个 suite 记录 pass/fail/skip 数量和耗时
  - 生成 data/benchmarks/test_plan_report.json
  - 支持 --tier smoke|ci|full 过滤 suite
  - 支持 --fail-fast（任意 suite 失败立即终止）

CLI：
  python scripts/run_test_plan.py --plan main_plan.yaml --tier ci
  python scripts/run_test_plan.py --plan main_plan.yaml --tier smoke --fail-fast
步骤 6：扩展回归基准（run_regression_benchmark.py）
在 _run_autonomy_loop_regression 基础上新增：

browser_session_error_then_retry：browser_open 返回 js_error 后重试成功
desktop_uia_busy_backoff：desktop_type 返回 uia_busy 触发退让
步骤 7：CI 集成（ci.yml）
在现有 Regression benchmark gate 后增加：

- name: Test plan (CI tier)
  run: python scripts/run_test_plan.py --plan main_plan.yaml --tier ci --fail-fast
关键文件路径
主测试计划：data/test_plans/main_plan.yaml
测试计划执行器：scripts/run_test_plan.py
Browser 测试：tests/test_automation_browser.py
Desktop 测试：tests/test_automation_desktop.py
Computer Use 扩展：tests/test_computer_use_extended.py
回归基准：scripts/run_regression_benchmark.py（扩展）
CI 配置：.github/workflows/ci.yml（追加步骤）
报告输出：data/benchmarks/test_plan_report.json
复用现有基础设施
monkeypatch / MagicMock 模式：复用 test_computer_use.py 的写法
_simulate_case 模式：复用 run_regression_benchmark.py 的 action mock 方案
_safe_read_json / _safe_write_json：复用 web_app.py 的 JSON 工具函数
evaluation/transcript_logger.py：可选，为 test plan 报告提供结构化日志
验证方式
# 1. 单元测试（新增部分）
pytest tests/test_automation_browser.py tests/test_automation_desktop.py tests/test_computer_use_extended.py -v

# 2. 全量单元测试（含原有测试）
pytest

# 3. 冒烟计划
python scripts/run_test_plan.py --plan main_plan.yaml --tier smoke

# 4. CI 全量计划
python scripts/run_test_plan.py --plan main_plan.yaml --tier ci --fail-fast

# 5. 查看报告
cat data/benchmarks/test_plan_report.json