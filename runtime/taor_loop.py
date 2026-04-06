"""
TAOR 自主执行循环 (Think-Act-Observe-Repeat)

核心思想：Orchestrator 只负责驱动循环、执行工具、传递结果；
让模型决定下一步，约 50 行核心循环逻辑，给模型无限操作空间。

对比现有 7 步瀑布流：
  瀑布流  = 框架决定 agent 类型、任务拆分、方法论应用
  TAOR   = 模型看到工具清单后自主决定每一步

模型输出格式（JSON-in-response，复用 react_infer_next_step 约定）：
  {
    "thought": "本轮推理",
    "finish": false,
    "final_result": "",        // finish=true 时填写
    "is_success": true,        // finish=true 时填写
    "action": {                // 需要调用工具时填写（字段名复用 react_infer_next_step）
      "type": "browser_open",
      "target": "...",
      "params": {},
      "risk": "low",
      "reason": "..."
    }
  }

配置项：
  ARIA_TAOR_MODE=1          在 web_app.py 调用入口启用
  ARIA_TAOR_MAX_TURNS=20    最大循环轮数（默认 20，上限 60）
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from .compaction import ContextCompactor


class TAORLoop:
    """
    Think-Act-Observe-Repeat 自主执行循环。

    当 ARIA_TAOR_MODE=1 时，由 aria_manager.run_taor_pipeline() 调用，
    替代现有 7 步瀑布流。瀑布流原始代码保持不变，通过特性标志切换。
    """

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    # ------------------------------------------------------------------ #
    # 公开入口                                                               #
    # ------------------------------------------------------------------ #

    def run(
        self,
        user_input: str,
        dialogue_context: str = "",
        method: dict[str, Any] | None = None,
        plan_context: str = "",
        plan_tracker: Any | None = None,
    ) -> dict[str, Any]:
        """
        执行 TAOR 循环直到任务完成或达到最大轮数。

        返回与瀑布流结果兼容的字典：
          {
            "final_result": str,
            "is_success": bool,
            "tool_trace": list[dict],   # 每轮的 thought/action/observation
          }
        """
        max_turns = self._max_turns()
        reminder_interval = self._reminder_interval()
        compactor = ContextCompactor(self._manager)
        messages = self._build_initial_messages(user_input, dialogue_context, method, plan_context)
        tool_trace: list[dict[str, Any]] = []

        # 卡死检测状态
        _last_action_fingerprints: list[str] = []  # 最近 N 轮指纹
        _consecutive_fail_count = 0
        _stall_warned = False        # 是否已注入警告（给模型一次机会换策略）
        _STALL_REPEAT_LIMIT = 3   # 相同指纹连续 N 轮 → 终止
        _STALL_FAIL_LIMIT = 4     # 连续失败 N 轮 → 终止

        for turn in range(1, max_turns + 1):
            self._manager.check_cancelled("taor_turn_start")
            self._manager.push_event(
                "taor_loop",
                "running",
                "TAORLoop",
                f"TAOR 第 {turn} 轮推理",
                {"turn": turn, "max_turns": max_turns, "event_subtype": "taor_think"},
            )

            # ---- Think ----
            token_before = self._manager.get_token_usage_summary().get("total_tokens", 0)
            llm_text = self._manager._call_llm(
                messages,
                fallback_text="",
                agent_code="TAORLoop",
            )
            token_after = self._manager.get_token_usage_summary().get("total_tokens", 0)
            compactor.record_usage(token_after - token_before)

            step = self._parse_model_response(llm_text)
            messages.append({"role": "assistant", "content": llm_text or ""})

            if step["finish"]:
                final_result = step["final_result"] or step["thought"] or ""
                is_success = step["is_success"]
                self._manager.push_event(
                    "taor_loop",
                    "success",
                    "TAORLoop",
                    f"TAOR 完成（{turn} 轮）",
                    {"turn": turn, "is_success": is_success, "event_subtype": "taor_finish"},
                )
                return {
                    "final_result": final_result,
                    "is_success": is_success,
                    "tool_trace": tool_trace,
                    "plan_completed_steps": plan_tracker.completed_steps if plan_tracker else [],
                    "plan_total_steps": len(plan_tracker.plan.get("sub_goals", [])) if plan_tracker else 0,
                }

            raw_action = step["action"]
            if not isinstance(raw_action, dict) or not raw_action.get("type"):
                # 模型给出文字回复但未指定动作且未 finish → 隐式完成
                return {
                    "final_result": step["thought"] or llm_text or "",
                    "is_success": True,
                    "tool_trace": tool_trace,
                    "plan_completed_steps": plan_tracker.completed_steps if plan_tracker else [],
                    "plan_total_steps": len(plan_tracker.plan.get("sub_goals", [])) if plan_tracker else 0,
                }

            # ---- Act ----
            action_type = str(raw_action.get("type") or "")

            # 卡死检测：相同 action 指纹连续重复
            _action_fp = self._action_fingerprint(raw_action)
            _last_action_fingerprints.append(_action_fp)
            if len(_last_action_fingerprints) > _STALL_REPEAT_LIMIT:
                _last_action_fingerprints.pop(0)
            if (
                len(_last_action_fingerprints) == _STALL_REPEAT_LIMIT
                and len(set(_last_action_fingerprints)) == 1
            ):
                if not _stall_warned:
                    # 先注入警告，给模型一次机会换策略
                    _stall_warned = True
                    _last_action_fingerprints.clear()
                    _warn_hint = (
                        f"【系统警告】你已连续 {_STALL_REPEAT_LIMIT} 轮执行相同操作（{action_type}）且无进展。"
                        "可能原因：①坐标错误 ②截图中出现的是 ARIA 自身 UI 而非目标应用。"
                        "请先截图重新观察当前屏幕，确认目标应用窗口可见后再操作。"
                        "禁止继续重试相同坐标。"
                    )
                    messages.append({"role": "user", "content": _warn_hint})
                    self._manager.push_event(
                        "taor_loop", "warning", "TAORLoop",
                        f"检测到重复操作，已向模型注入换策略提示（第 {turn} 轮）",
                        {"turn": turn, "event_subtype": "taor_stall_warn"},
                    )
                    # 跳过本轮 Act，让模型重新 Think
                    continue
                else:
                    # 已经警告过还在重复 → 强制终止
                    _stall_msg = (
                        f"ARIA 连续重复执行相同操作（{action_type}）且换策略提示后仍未改变，"
                        "已自动终止循环，请换一种方式操作或手动介入。"
                    )
                    self._manager.push_event(
                        "taor_loop", "warning", "TAORLoop", _stall_msg,
                        {"turn": turn, "event_subtype": "taor_stall"},
                    )
                    return {
                        "final_result": _stall_msg,
                        "is_success": False,
                        "tool_trace": tool_trace,
                        "plan_completed_steps": plan_tracker.completed_steps if plan_tracker else [],
                        "plan_total_steps": len(plan_tracker.plan.get("sub_goals", [])) if plan_tracker else 0,
                    }

            _action_reason = str(raw_action.get("reason") or "").strip()[:120]
            _action_target = str(raw_action.get("target") or "").strip()[:80]
            _act_summary = self._friendly_act_summary(action_type, raw_action)
            self._manager.push_event(
                "taor_loop",
                "running",
                "TAORLoop",
                _act_summary,
                {
                    "action_type": action_type,
                    "turn": turn,
                    "action_target": _action_target,
                    "action_reason": _action_reason,
                    "event_subtype": "taor_act",
                },
            )

            # ---- Observe ----
            observation = self._dispatch_action(raw_action)
            tool_trace.append(
                {
                    "turn": turn,
                    "thought": step["thought"],
                    "action": raw_action,
                    "observation": observation,
                }
            )

            # 观察结果推 SSE + 连续失败检测
            obs_success = observation.get("success", True)
            obs_err = str(observation.get("error") or observation.get("stderr") or "")[:80]
            _obs_summary = self._friendly_obs_summary(action_type, raw_action, observation, obs_success, obs_err)
            self._manager.push_event(
                "taor_loop",
                "success" if obs_success else "warning",
                "TAORLoop",
                _obs_summary,
                {
                    "turn": turn,
                    "obs_success": obs_success,
                    "action_type": action_type,
                    "event_subtype": "taor_observe",
                },
            )

            if obs_success:
                _consecutive_fail_count = 0
            else:
                _consecutive_fail_count += 1
                if _consecutive_fail_count >= _STALL_FAIL_LIMIT:
                    _fail_msg = (
                        f"ARIA 连续 {_STALL_FAIL_LIMIT} 轮工具调用失败，已自动终止。"
                        f"最近错误：{obs_err}"
                    )
                    self._manager.push_event(
                        "taor_loop", "warning", "TAORLoop", _fail_msg,
                        {"turn": turn, "event_subtype": "taor_stall"},
                    )
                    return {
                        "final_result": _fail_msg,
                        "is_success": False,
                        "tool_trace": tool_trace,
                        "plan_completed_steps": plan_tracker.completed_steps if plan_tracker else [],
                        "plan_total_steps": len(plan_tracker.plan.get("sub_goals", [])) if plan_tracker else 0,
                    }

                # GUI 操作失败后自动截图，让模型在下一轮看到当前屏幕状态
                _GUI_ACTION_PREFIXES = ("computer_", "screen_", "desktop_", "window_", "browser_")
                _auto_screenshot = os.getenv("ARIA_TAOR_AUTO_SCREENSHOT_ON_FAIL", "1").strip().lower() not in ("0", "false", "off", "no")
                if _auto_screenshot and any(action_type.startswith(p) for p in _GUI_ACTION_PREFIXES):
                    try:
                        _ss_result = self._dispatch_action({"type": "computer_screenshot", "params": {}})
                        if _ss_result.get("success") and _ss_result.get("screenshot"):
                            observation = dict(observation)
                            observation["auto_screenshot_after_fail"] = _ss_result["screenshot"]
                    except Exception:
                        pass

            obs_text = json.dumps(observation, ensure_ascii=False, default=str)
            messages.append({"role": "user", "content": f"TOOL_RESULT:\n{obs_text}"})

            # 步骤完成检测（regex 扫描 thought + observation 信号）
            if plan_tracker is not None:
                if step.get("thought"):
                    plan_tracker.scan_thought(step["thought"])
                # 动态：根据 observation 结果推进当前子目标
                _cur_sid = plan_tracker._current_step_id
                if _cur_sid is not None:
                    plan_tracker.scan_observation(_cur_sid, observation)
                # 轮数计数（阻塞检测）
                plan_tracker.tick(turn)

            # 漂移修正提醒（每 N 轮注入一次，或阻塞时立即注入）
            if plan_tracker is not None:
                _inject_reminder = (turn % reminder_interval == 0) or plan_tracker.is_stalled()
                if _inject_reminder:
                    messages.append(
                        {"role": "user", "content": f"【计划进度提醒】\n{plan_tracker.reminder_text()}"}
                    )

            # 对 context_window 文本做压缩（用已完成的 tool_trace 作为历史）
            if len(tool_trace) >= 3:
                history_text = self._format_trace_for_compact(tool_trace[:-1])
                compacted = compactor.maybe_compact(history_text, task_goal=user_input)
                if compacted != history_text:
                    # 重建 messages：保留 system + 首轮 user，替换中间历史
                    messages = self._rebuild_messages_with_compact(
                        messages, compacted, user_input
                    )

        # 达到最大轮数
        self._manager.push_event(
            "taor_loop",
            "warning",
            "TAORLoop",
            f"TAOR 已达最大轮数 {max_turns}，强制结束",
            {"max_turns": max_turns},
        )
        return {
            "final_result": f"任务已执行 {max_turns} 轮，请查看工具执行记录获取中间结果。",
            "is_success": False,
            "tool_trace": tool_trace,
            "plan_completed_steps": plan_tracker.completed_steps if plan_tracker else [],
            "plan_total_steps": len(plan_tracker.plan.get("sub_goals", [])) if plan_tracker else 0,
        }

    # ------------------------------------------------------------------ #
    # 私有：消息构建                                                          #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # 私有：任务表面检测（用于裁剪系统提示）                                        #
    # ------------------------------------------------------------------ #

    _COMPUTER_KEYWORDS = frozenset([
        "点击", "截图", "屏幕", "鼠标", "键盘", "输入", "打开", "窗口", "桌面",
        "click", "screenshot", "screen", "mouse", "keyboard", "type", "open", "window",
        "desktop", "drag", "scroll", "右键", "双击",
    ])
    _BROWSER_KEYWORDS = frozenset([
        "浏览器", "网页", "网站", "搜索", "chrome", "firefox", "edge", "url",
        "browser", "web", "http", "https", "打开网址", "访问",
    ])
    _FILE_KEYWORDS = frozenset([
        "文件", "文档", "保存", "读取", "写入", "目录", "路径", "folder",
        "file", "read", "write", "save", "directory", "path", "创建文件",
    ])

    def _detect_task_surface(self, user_input: str) -> set[str]:
        """从用户输入关键词推断任务表面，返回集合：computer / browser / file / general。"""
        text = user_input.lower()
        surfaces: set[str] = set()
        if any(k in text for k in self._COMPUTER_KEYWORDS):
            surfaces.add("computer")
        if any(k in text for k in self._BROWSER_KEYWORDS):
            surfaces.add("browser")
        if any(k in text for k in self._FILE_KEYWORDS):
            surfaces.add("file")
        if not surfaces:
            surfaces.add("general")
        return surfaces

    def _build_initial_messages(
        self,
        user_input: str,
        dialogue_context: str,
        method: dict[str, Any] | None,
        plan_context: str = "",
    ) -> list[dict[str, Any]]:
        method_ctx = self._manager._methodology_summary_text(method) if method else ""
        surfaces = self._detect_task_surface(user_input)

        # 按任务表面裁剪工具列表，减少无关工具对模型注意力的干扰
        all_types = self._manager.ALLOWED_ACTION_TYPES
        if "general" not in surfaces:
            _computer_types = {t for t in all_types if t.startswith("computer_") or t.startswith("screen_")}
            _browser_types = {t for t in all_types if t.startswith("browser_")}
            _file_types = {t for t in all_types if t.startswith("file_")}
            _always_types = {t for t in all_types if not (
                t.startswith("computer_") or t.startswith("screen_") or
                t.startswith("browser_") or t.startswith("file_")
            )}
            filtered: set[str] = set(_always_types)
            if "computer" in surfaces:
                filtered |= _computer_types
            if "browser" in surfaces:
                filtered |= _browser_types
            if "file" in surfaces:
                filtered |= _file_types
            allowed_types = ", ".join(sorted(filtered))
        else:
            allowed_types = ", ".join(sorted(all_types))

        # 按表面选择性注入 capability fragment
        from automation import browser_driver, desktop_uia, screen_ocr
        cap_parts: list[str] = []
        if "computer" in surfaces or "general" in surfaces:
            cap_parts.append(self._manager._computer_use_capability_summary())
            cap_parts.append(desktop_uia.capability_summary_for_planner())
            cap_parts.append(screen_ocr.get_capability_summary())
        if "browser" in surfaces or "general" in surfaces:
            cap_parts.append(browser_driver.capability_summary_for_planner())
        capability_fragment = "".join(cap_parts)

        coord_fragment = self._manager._react_coordinate_contract_prompt_fragment()
        current_time_str = time.strftime("%Y年%m月%d日 %H:%M，%A")

        # 记忆懒加载：仅当任务不是纯 computer_use 时注入（computer_use 任务不依赖跨会话记忆）
        if "computer" in surfaces and "general" not in surfaces and "browser" not in surfaces:
            memory_fragment = ""
        else:
            memory_fragment = self._manager._memory_system_prompt_fragment(user_input)

        sys_content = (
            f"【当前时间】{current_time_str}\n\n"
            "你是 ARIA 的 TAOR 自主执行引擎。每轮先推理（Think），再决定是否调用一个工具（Act），"
            "观察结果（Observe）后进入下一轮（Repeat），直到任务完成。\n\n"
            "⚠️ 重要：【本会话近期对话】仅供理解用户指代（如「它」、「上面那个」），"
            "绝对不是当前任务。当前唯一任务由用户消息末尾的【当前任务】字段指定。"
            "不得将历史对话中出现的任何操作目标、步骤或意图带入本次执行。\n\n"
            "输出格式：严格 JSON 对象，禁止 markdown 围栏，禁止前后缀文字。字段：\n"
            '  "thought"      : 本步推理\n'
            '  "finish"       : bool，任务完成时为 true\n'
            '  "final_result" : 当 finish=true 时给用户的最终回复\n'
            '  "is_success"   : 当 finish=true 时填写，bool\n'
            '  "action"       : 需要调用工具时填写，字段：type, target, params, risk, reason\n\n'
            "规则：\n"
            "- 若上一步工具调用失败，在 thought 中分析原因并调整策略。\n"
            "- 不要编造未执行的结果；不要声称已保存文件但未执行 file_write。\n"
            "- 每轮只输出一个 action。\n"
            "- 若工具返回结果中包含 taor_hint 字段，必须立即 finish=true，"
            "在 final_result 中将 taor_hint 内容转述给用户，不得重试该操作。\n"
            "- 若工具返回 message 字段包含 app_already_running，说明目标应用已在运行，"
            "不得再次调用 desktop_open_app 打开同一应用，应直接操作该应用窗口。\n"
            "- 【严禁】操作 ARIA 自身的界面元素（确认按钮、执行计划卡片、对话框等）。"
            "截图中若出现 ARIA 的 Web UI、确认弹窗或「执行计划」卡片，忽略它们，"
            "直接操作目标应用（如微信、文件管理器等）。\n"
            "- 若连续多轮点击同一区域坐标均无进展，说明坐标有误或界面已变化，"
            "必须先截图重新定位，切勿继续重试相同坐标。\n\n"
            + (
                "【执行路线图（Advisory）】\n"
                + plan_context
                + "\n每完成一个子目标，在 thought 中标注「✓ 子目标N完成」。"
                "路线图仅供参考，若观察结果要求偏离，可调整策略。\n\n"
                if plan_context
                else ""
            )
            + f"可用工具类型：{allowed_types}\n\n"
            + f"{capability_fragment}\n"
            + f"{coord_fragment}\n"
            + (f"{method_ctx}\n\n" if method_ctx else "")
            + memory_fragment
        )

        user_parts: list[str] = []
        if (dialogue_context or "").strip():
            user_parts.append(f"【本会话近期对话】\n{dialogue_context.strip()}")
        user_parts.append(f"【当前任务】\n{user_input}")

        return [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]

    def _rebuild_messages_with_compact(
        self,
        messages: list[dict[str, Any]],
        compact_text: str,
        user_input: str,
    ) -> list[dict[str, Any]]:
        """
        压缩触发后，重建 messages 列表：
        保留 system 提示和任务说明，中间历史替换为压缩摘要。
        """
        if not messages:
            return messages
        system_msg = messages[0]
        # 第一条 user 消息（任务目标）
        first_user = next((m for m in messages[1:] if m.get("role") == "user"), None)
        rebuilt = [system_msg]
        if first_user:
            rebuilt.append(first_user)
        rebuilt.append(
            {"role": "user", "content": f"【执行历史摘要（已压缩）】\n{compact_text}"}
        )
        # 最后两条消息（最新 assistant + tool_result）保留
        if len(messages) >= 2:
            rebuilt.extend(messages[-2:])
        return rebuilt

    # ------------------------------------------------------------------ #
    # 私有：模型输出解析                                                       #
    # ------------------------------------------------------------------ #

    def _parse_model_response(self, llm_text: str) -> dict[str, Any]:
        data = self._manager._extract_json_object(llm_text or "")
        if not isinstance(data, dict):
            # 非 JSON 回复 → 隐式完成
            return {
                "thought": llm_text or "",
                "finish": True,
                "final_result": llm_text or "",
                "is_success": True,
                "action": None,
            }
        finish_raw = data.get("finish")
        finish = finish_raw is True or str(finish_raw).lower() in ("1", "true", "yes")
        return {
            "thought": str(data.get("thought") or "").strip(),
            "finish": finish,
            "final_result": str(data.get("final_result") or "").strip(),
            "is_success": bool(data.get("is_success", True)),
            "action": data.get("action") or data.get("tool_call"),  # 兼容两种字段名
        }

    # ------------------------------------------------------------------ #
    # 私有：工具分发                                                          #
    # ------------------------------------------------------------------ #

    def _dispatch_action(self, action: dict[str, Any]) -> dict[str, Any]:
        """
        通过现有的 app_registry → action_registry 链路分发单个工具调用。
        优先级与 execute_actions() 一致。
        """
        raw_type = str(action.get("type") or "")
        action_type = self._manager._normalize_action_type_alias(raw_type)

        if not action_type or action_type not in self._manager.ALLOWED_ACTION_TYPES:
            return {
                "success": False,
                "error_code": "unsupported_action",
                "error": f"不支持的工具类型：{raw_type!r}",
            }

        blocked, gate_code, gate_msg = self._manager.taor_action_blocked_for_dispatch(action)
        if blocked:
            return {
                "success": False,
                "error_code": gate_code or "user_gate_blocked",
                "stderr": gate_msg,
                "message": gate_msg,
                "taor_hint": (
                    "此操作在 TAOR 自主模式下无法执行（需要用户确认或权限不足）。"
                    "请立即设置 finish=true，在 final_result 中告知用户需要手动操作或切换到主流程执行。"
                    "不要重试相同操作。"
                ),
            }

        # 优先通过 app_registry 处理
        app_registry = getattr(self._manager, "app_registry", None)
        if app_registry is not None:
            cap_result = app_registry.get_capability(action_type)
            if cap_result:
                app, _ = cap_result
                try:
                    execute_fn = getattr(app, "execute", None)
                    if callable(execute_fn):
                        result = execute_fn(
                            action_type,
                            action,
                            cancel_checker=getattr(self._manager, "check_cancelled", None),
                        )
                        return result if isinstance(result, dict) else {"success": True, "output": str(result)}
                except Exception as exc:
                    return {"success": False, "error": str(exc)}

        # 回退到 action_registry
        handler = self._manager.action_registry.get(action_type)
        if not handler:
            return {"success": False, "error_code": "unsupported_action", "error": action_type}

        action_ctx = dict(action)
        action_ctx["_request_id"] = getattr(self._manager, "current_request_id", "")
        try:
            result = handler(
                action_ctx,
                getattr(self._manager, "current_conversation_id", ""),
                None,  # methodology_manager
                None,  # conversation_manager
            )
            return result if isinstance(result, dict) else {"success": True, "output": str(result)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------ #
    # 私有：辅助                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _action_fingerprint(action: dict[str, Any]) -> str:
        """
        生成 action 的去重指纹。
        对 computer_click / computer_screenshot 类，坐标量化到 20px 格，
        避免 (22,830) vs (24,832) 被视为不同操作。
        """
        atype = str(action.get("type") or "")
        target = str(action.get("target") or "")
        params = action.get("params") or {}

        if atype in ("computer_click", "computer_move"):
            x = params.get("x") or 0
            y = params.get("y") or 0
            # 量化到 20px 格
            x_q = int(x) // 20
            y_q = int(y) // 20
            return f"{atype}::{x_q},{y_q}"
        if atype == "computer_screenshot":
            return f"{atype}::screenshot"
        return f"{atype}::{target}"

    def _max_turns(self) -> int:
        try:
            return max(1, min(60, int(os.getenv("ARIA_TAOR_MAX_TURNS", "20") or "20")))
        except (TypeError, ValueError):
            return 20

    def _reminder_interval(self) -> int:
        try:
            return max(1, int(os.getenv("ARIA_HYBRID_REMINDER_INTERVAL", "5") or "5"))
        except (TypeError, ValueError):
            return 5

    @staticmethod
    def _format_trace_for_compact(tool_trace: list[dict[str, Any]]) -> str:
        """将 tool_trace 转为适合压缩器处理的文本。"""
        if not tool_trace:
            return ""
        lines: list[str] = []
        for row in tool_trace:
            turn = row.get("turn", "?")
            thought = str(row.get("thought") or "").strip()[:200]
            act = row.get("action") or {}
            obs = row.get("observation") or {}
            success = obs.get("success", "?")
            atype = act.get("type", "?")
            lines.append(
                f"[Turn {turn}] thought: {thought}\n"
                f"  action: {atype}  success: {success}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 私有：用户友好的事件摘要                                                   #
    # ------------------------------------------------------------------ #

    _ACT_LABELS: dict[str, str] = {
        "computer_screenshot": "截取屏幕",
        "computer_click": "点击屏幕",
        "computer_move": "移动鼠标",
        "computer_type": "输入文字",
        "computer_scroll": "滚动页面",
        "computer_drag": "拖拽元素",
        "screen_ocr": "识别屏幕文字",
        "screen_find_text": "查找屏幕文字",
        "screen_click_text": "点击屏幕文字",
        "desktop_open_app": "打开应用",
        "desktop_hotkey": "按下快捷键",
        "desktop_type": "键盘输入",
        "desktop_sequence": "执行操作序列",
        "window_activate": "切换窗口",
        "window_list": "列出窗口",
        "browser_open": "打开网页",
        "browser_click": "点击网页元素",
        "browser_type": "网页输入",
        "browser_find": "查找网页元素",
        "browser_screenshot": "截取网页截图",
        "browser_scroll": "滚动网页",
        "web_fetch": "抓取网页",
        "web_understand": "理解网页内容",
        "file_read": "读取文件",
        "file_write": "写入文件",
        "file_delete": "删除文件",
        "file_list": "列出文件",
        "shell_run": "执行命令",
        "wechat_send_message": "发送微信消息",
        "kb_read": "读取知识库",
        "kb_write": "写入知识库",
        "kb_delete_all": "清空知识库",
    }

    @classmethod
    def _friendly_act_summary(cls, action_type: str, action: dict[str, Any]) -> str:
        """生成面向用户的操作摘要，隐藏技术细节。"""
        label = cls._ACT_LABELS.get(action_type, action_type)
        target = str(action.get("target") or "").strip()
        params = action.get("params") or {}

        if action_type == "computer_click":
            x = params.get("x") or 0
            y = params.get("y") or 0
            if x or y:
                return f"{label}（{x}, {y}）"
        elif action_type == "computer_type":
            text = str(params.get("text") or target or "").strip()
            if text:
                return f"{label}：{text[:30]}{'…' if len(text) > 30 else ''}"
        elif action_type == "desktop_open_app":
            app = str(params.get("app") or target or "").strip()
            if app:
                return f"{label}：{app}"
        elif action_type == "desktop_hotkey":
            keys = str(params.get("keys") or target or "").strip()
            if keys:
                return f"{label}：{keys}"
        elif action_type == "window_activate":
            title = str(params.get("title") or target or "").strip()
            if title:
                return f"{label}：{title}"
        elif action_type in ("browser_open", "web_fetch", "web_understand"):
            url = str(params.get("url") or target or "").strip()
            if url:
                # 只显示域名部分
                import re as _re
                m = _re.search(r"https?://([^/?\s]{1,40})", url)
                domain = m.group(1) if m else url[:40]
                return f"{label}：{domain}"
        elif action_type == "wechat_send_message":
            to = str(params.get("to") or target or "").strip()
            if to:
                return f"{label}：发给 {to}"
        elif action_type == "shell_run":
            cmd = str(params.get("command") or target or "").strip()
            if cmd:
                return f"{label}：{cmd[:40]}{'…' if len(cmd) > 40 else ''}"

        if target:
            return f"{label}：{target[:40]}{'…' if len(target) > 40 else ''}"
        return label

    @staticmethod
    def _friendly_obs_summary(
        action_type: str,
        action: dict[str, Any],
        observation: dict[str, Any],
        obs_success: bool,
        obs_err: str,
    ) -> str:
        """生成面向用户的观察结果摘要。"""
        if not obs_success:
            if obs_err:
                return f"操作失败：{obs_err}"
            return "操作失败"

        label = TAORLoop._ACT_LABELS.get(action_type, action_type)

        if action_type == "computer_screenshot":
            return "屏幕截图已获取"
        if action_type == "screen_ocr":
            text = str(observation.get("stdout") or "").strip()
            if text:
                snippet = text[:30].replace("\n", " ")
                return f"识别到文字：{snippet}{'…' if len(text) > 30 else ''}"
            return "屏幕文字识别完成"
        if action_type == "desktop_open_app":
            return f"应用已打开"
        if action_type == "window_activate":
            title = str(observation.get("stdout") or "").strip()
            if title:
                return f"已切换到窗口"
            return "窗口已激活"
        if action_type == "computer_click":
            return "点击完成"
        if action_type == "computer_type":
            return "文字已输入"
        if action_type == "desktop_hotkey":
            return "快捷键已执行"
        if action_type in ("web_fetch", "web_understand"):
            return "网页内容已获取"
        if action_type == "wechat_send_message":
            return "微信消息已发送"

        msg = str(observation.get("message") or observation.get("stdout") or "").strip()
        if msg:
            return f"{label}：{msg[:50]}{'…' if len(msg) > 50 else ''}"
        return f"{label}完成"
