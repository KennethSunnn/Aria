import json
import logging
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import uuid
from typing import Any

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file

# 加载环境变量（强制覆盖同名系统变量，避免读取到空值）
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

# 添加项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from aria_manager import ARIAManager, TaskCancelledError
from chat_attachments import (
    MAX_FILES_PER_MESSAGE,
    extract_llm_excerpt,
    image_data_urls_from_attachment_records,
    merge_json_attachments,
    save_uploaded_file,
)
from conversation_lib import ConversationLibrary
from llm.volcengine_llm import _normalize_reasoning_effort, resolve_inference_api_key
from method_lib import MethodologyLibrary
from runtime.timing_breakdown import compute_timing_breakdown

logger = logging.getLogger(__name__)

app = Flask(__name__)

# 初始化管理器
manager = ARIAManager()
methodology_manager = MethodologyLibrary()
conversation_manager = ConversationLibrary()
sse_subscribers: dict[str, list[queue.Queue]] = {}
sse_lock = threading.Lock()
pending_action_plans: dict[str, dict] = {}
execution_sessions_by_conversation: dict[str, str] = {}
conversation_task_bookmark: dict[str, str] = {}


def _try_uuid(s: str) -> str | None:
    """验证字符串是否为合法 UUID，是则返回原值，否则返回 None。"""
    try:
        uuid.UUID(s)
        return s
    except ValueError:
        return None


def _reuse_task_id_for_parse(
    conversation_id: str, client_task_id: str, new_task: bool, bookmark: dict[str, str]
) -> str | None:
    """供 parse_task 沿用线程：new_task 则新 UUID；否则优先客户端 task_id，再回落书签。"""
    if new_task:
        return None
    return (
        _try_uuid((client_task_id or "").strip())
        or _try_uuid((bookmark.get(conversation_id) or "").strip())
    )


def _tid_for_response_non_parse(
    conversation_id: str, client_task_id: str, new_task: bool, bookmark: dict[str, str]
) -> str:
    """clarify / action 预览 / small_talk / 待确认消息 等使用的 task_id，并维护书签（parse_task 路径由书签在 parse 后单独写入）。"""
    if not new_task:
        tid = (
            _try_uuid((client_task_id or "").strip())
            or _try_uuid((bookmark.get(conversation_id) or "").strip())
        )
        if tid:
            bookmark[conversation_id] = tid
            return tid
    tid = str(uuid.uuid4())
    bookmark[conversation_id] = tid
    return tid


def publish_workflow_event(event: dict):
    conversation_id = event.get("conversation_id", "")
    if not conversation_id:
        return
    with sse_lock:
        subscribers = list(sse_subscribers.get(conversation_id, []))
    for q in subscribers:
        try:
            q.put_nowait(event)
        except Exception:
            pass


manager.set_event_sink(publish_workflow_event)

# 大模型 API：仅从环境变量 / .env 读取（不在 Web 中保存密钥，便于开源部署）
_DOTENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_REGRESSION_REPORT_PATH = os.path.join(_PROJECT_ROOT, "data", "benchmarks", "latest_regression_report.json")
_EVAL_REPORT_PATH = os.path.join(_PROJECT_ROOT, "data", "benchmarks", "latest_eval_report.json")
_EVAL_CASES_PATH = os.path.join(_PROJECT_ROOT, "data", "benchmarks", "regression_tasks.json")
_EXPERIENCE_METRICS_PATH = os.path.join(_PROJECT_ROOT, "data", "experience_center_metrics.json")
_WORKSPACE_PREVIEW_MAX_BYTES = 256 * 1024
_WORKSPACE_LIST_LIMIT_MAX = 500
_WORKSPACE_PREVIEW_TEXT_EXTS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".py",
    ".log",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".ini",
    ".toml",
    ".env",
    ".gitignore",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
}


def resolve_api_key() -> str:
    """按 OPENAI_BASE_URL 选择 ARK / 百炼密钥；每次从 .env 刷新。"""
    return resolve_inference_api_key(_DOTENV_PATH)


def _empty_token_usage() -> dict:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "llm_calls": 0,
        "llm_wall_ms": 0,
    }


def _safe_read_json(path: str, default: Any):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception:
        return default


def _safe_write_json(path: str, payload: Any):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_regression_snapshot() -> dict:
    data = _safe_read_json(_REGRESSION_REPORT_PATH, {})
    if not isinstance(data, dict):
        return {"available": False, "match_rate": 0.0, "strict_pass_rate": 0.0, "total_cases": 0}
    total = int(data.get("total_cases", 0) or 0)
    matched = int(data.get("matched_cases", 0) or 0)
    strict = int(data.get("strict_passed_cases", matched) or matched)
    match_rate = float(data.get("match_rate", 0.0) or 0.0)
    strict_rate = float(data.get("strict_pass_rate", strict / max(1, total)) or (strict / max(1, total)))
    return {
        "available": total > 0,
        "match_rate": round(match_rate, 4),
        "strict_pass_rate": round(strict_rate, 4),
        "total_cases": total,
        "matched_cases": matched,
        "strict_passed_cases": strict,
        "generated_at": data.get("generated_at") or "",
    }


def _method_health_index(limit: int = 300) -> dict[str, dict]:
    rows = methodology_manager.get_methodology_health_dashboard(limit=limit)
    out: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict):
            mid = str(row.get("method_id") or "").strip()
            if mid:
                out[mid] = row
    return out


def _calc_recommendation_score(method: dict, health: dict, regression: dict) -> float:
    usage = float(method.get("usage_count", 0) or 0)
    success = float(method.get("success_count", 0) or 0)
    score = float(method.get("score", 0.0) or 0.0)
    health_sr = float(health.get("success_rate", 0.0) or 0.0)
    quality = float(health.get("quality_score_avg", 0.0) or 0.0)
    recency_bonus = 0.08 if str(health.get("last_outcome_at") or "").strip() else 0.0
    utilization = min(1.0, usage / 10.0)
    base = 0.40 * score + 0.25 * health_sr + 0.20 * quality + 0.15 * utilization
    if usage > 0 and success <= 0:
        base *= 0.7
    if regression.get("available") and float(regression.get("strict_pass_rate", 0.0) or 0.0) < 0.45:
        base *= 0.9
    return round(max(0.0, min(1.0, base + recency_bonus)), 4)


def _health_label(reco_score: float) -> str:
    if reco_score >= 0.72:
        return "stable"
    if reco_score >= 0.48:
        return "caution"
    return "deprecate"


def _build_skill_card(method: dict, health: dict, regression: dict) -> dict:
    mid = str(method.get("method_id") or "")
    title = str(method.get("title") or method.get("scene") or "未命名经验")
    scene = str(method.get("scene") or method.get("scenario") or "")
    keywords = method.get("keywords") if isinstance(method.get("keywords"), list) else []
    score = _calc_recommendation_score(method, health, regression)
    label = _health_label(score)
    strict_rate = float(regression.get("strict_pass_rate", 0.0) or 0.0)
    return {
        "method_id": mid,
        "title": title,
        "scene": scene,
        "keywords": keywords[:8],
        "category": str(method.get("category") or "通用/其他"),
        "success_rate": round(float(health.get("success_rate", 0.0) or 0.0), 4),
        "quality_score_avg": round(float(health.get("quality_score_avg", 0.0) or 0.0), 4),
        "recent_regression_pass": bool(regression.get("available")) and strict_rate >= 0.6,
        "risk_hint": "可放心复用" if label == "stable" else ("建议人工确认" if label == "caution" else "建议下线/重写"),
        "health_label": label,
        "recommendation_score": score,
        "last_outcome_at": str(health.get("last_outcome_at") or ""),
        "usage_count": int(method.get("usage_count", 0) or 0),
        "success_count": int(method.get("success_count", 0) or 0),
    }


def _extract_keywords_from_text(text: str, limit: int = 6) -> list[str]:
    txt = str(text or "").strip().lower()
    if not txt:
        return []
    parts = re.split(r"[\s,，;；/|、]+", txt)
    dedup: list[str] = []
    for p in parts:
        token = p.strip()
        if not token:
            continue
        if token in dedup:
            continue
        if len(token) <= 1:
            continue
        dedup.append(token)
        if len(dedup) >= limit:
            break
    return dedup


def _recent_success_summaries(limit: int = 8) -> list[dict]:
    rows: list[dict] = []
    conversations = conversation_manager._load()
    conversations.sort(key=lambda c: float(c.get("updated_at", 0) or 0), reverse=True)
    for conv in conversations:
        cid = str(conv.get("conversation_id") or "")
        title = str(conv.get("title") or "新会话")
        messages = conv.get("messages") if isinstance(conv.get("messages"), list) else []
        if not messages:
            continue
        latest_user = ""
        latest_assistant = ""
        session_id = ""
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            content = str(msg.get("content") or "").strip()
            meta = msg.get("meta") if isinstance(msg.get("meta"), dict) else {}
            if role == "assistant" and not latest_assistant:
                latest_assistant = content
                session_id = str(meta.get("execution_session_id") or "")
            if role == "user" and not latest_user:
                latest_user = content
            if latest_user and latest_assistant:
                break
        if not latest_user:
            continue
        rows.append(
            {
                "conversation_id": cid,
                "title": title,
                "latest_user": latest_user,
                "latest_assistant": latest_assistant,
                "execution_session_id": session_id,
                "updated_at": float(conv.get("updated_at", 0) or 0),
            }
        )
        if len(rows) >= max(1, min(30, int(limit))):
            break
    return rows


def _build_skill_draft_from_recent(summary: dict) -> dict:
    title = str(summary.get("title") or "新技能草稿").strip()
    latest_user = str(summary.get("latest_user") or "").strip()
    assistant = str(summary.get("latest_assistant") or "").strip()
    scene = latest_user[:80] if latest_user else title
    keywords = _extract_keywords_from_text(scene, limit=6)
    if not keywords:
        keywords = _extract_keywords_from_text(title, limit=6)
    steps = [
        "确认输入目标与边界条件",
        "按已有流程生成可执行动作计划",
        "执行后记录结果并做质量复盘",
    ]
    if assistant:
        steps.append("参考最近一次执行摘要优化步骤细节")
    return {
        "title": f"{title} - Skill 草稿",
        "scene": scene,
        "keywords": keywords,
        "solve_steps": steps,
        "category": "通用/其他",
        "applicable_range": "任务提效",
        "status": "draft",
        "quality_metrics": {"source": "recent_success_bootstrap"},
        "evidence_refs": [{"conversation_id": str(summary.get("conversation_id") or "")}],
    }


def _json_response(data: dict) -> Response:
    """在 JSON 响应中附带当前线程累计的 LLM token 用量（便于估算成本）。"""
    payload = dict(data)
    payload["token_usage"] = manager.get_token_usage_summary()
    return jsonify(payload)


def _elapsed_ms_since(started_at_perf: float) -> int:
    try:
        return max(0, int((time.perf_counter() - float(started_at_perf)) * 1000))
    except Exception:
        return 0


def _is_confirmation_text(text: str, double: bool = False) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if double:
        keywords = ["二次确认", "再次确认", "高风险确认", "最终确认", "double confirm", "second confirm", "final confirm"]
    else:
        keywords = ["确认执行", "确认", "继续执行", "执行吧", "同意执行", "confirm", "yes execute", "execute now", "go ahead"]
    return any(k in t for k in keywords)


def _is_double_confirmation_text(text: str) -> bool:
    return _is_confirmation_text(text, double=True)


def _finalize_action_execution(
    conversation_id: str,
    request_id: str,
    actions: list[dict],
    plan_summary: str = "",
    plan_risk_level: str = "medium",
    thread_task_id: str | None = None,
    *,
    action_screenshots: bool = False,
):
    session_id = manager.create_execution_session(
        conversation_id,
        request_id or "",
        actions or [],
        methodology_manager,
        conversation_manager,
        action_screenshots=bool(action_screenshots),
        plan_summary=str(plan_summary or ""),
        plan_risk_level=str(plan_risk_level or "medium"),
    )
    execution_sessions_by_conversation[conversation_id] = session_id
    manager.start_execution_session(session_id)
    summary = f"执行已开始：共 {len(actions or [])} 个动作。"
    manager.push_event("action_execute_start", "success", "TaskParser", summary, {"session_id": session_id})
    manager.push_log("TaskParser", summary, "running")
    logs = manager.get_execution_log()
    workflow_events = manager.get_workflow_events()
    tu = manager.get_token_usage_summary()
    tid = (thread_task_id or "").strip() or manager.current_task_id or ""
    conversation_manager.append_message(
        conversation_id,
        "assistant",
        summary,
        {
            "logs": logs,
            "workflow_events": workflow_events,
            "execution_session_id": session_id,
            "token_usage": tu,
            "task_id": tid,
        },
    )
    conversation_manager.replace_workflow_events(conversation_id, workflow_events)
    return {
        "result": summary,
        "logs": logs,
        "workflow_events": workflow_events,
        "conversation_id": conversation_id,
        "api_key_configured": True,
        "task_id": tid,
        "request_id": request_id or "",
        "execution_session_id": session_id,
        "execution_status": "running",
        "model_trace": getattr(manager, "last_model_trace", {}),
        "token_usage": tu,
    }


def _finalize_react_execution(
    conversation_id: str,
    request_id: str,
    user_goal: str,
    dialogue_context: str,
    plan_summary: str = "",
    plan_risk_level: str = "medium",
    thread_task_id: str | None = None,
    *,
    action_screenshots: bool = False,
    react_computer_use_vision: bool = False,
):
    """启动 ReAct 异步会话（Thought→Action→Observation 循环）。"""
    session_id = manager.create_react_execution_session(
        conversation_id,
        request_id or "",
        user_goal,
        dialogue_context,
        methodology_manager,
        conversation_manager,
        action_screenshots=bool(action_screenshots),
        plan_summary=str(plan_summary or ""),
        plan_risk_level=str(plan_risk_level or "medium"),
        react_computer_use_vision=bool(react_computer_use_vision),
    )
    execution_sessions_by_conversation[conversation_id] = session_id
    manager.start_execution_session(session_id)
    summary = "ReAct 执行已开始：将按步推理并在每步后读取环境反馈。"
    manager.push_event("react_session_start", "success", "ReActAgent", summary, {"session_id": session_id})
    manager.push_log("ReActAgent", summary, "running")
    logs = manager.get_execution_log()
    workflow_events = manager.get_workflow_events()
    tu = manager.get_token_usage_summary()
    tid = (thread_task_id or "").strip() or manager.current_task_id or ""
    conversation_manager.append_message(
        conversation_id,
        "assistant",
        summary,
        {
            "logs": logs,
            "workflow_events": workflow_events,
            "execution_session_id": session_id,
            "token_usage": tu,
            "task_id": tid,
            "react_mode": True,
        },
    )
    conversation_manager.replace_workflow_events(conversation_id, workflow_events)
    return {
        "result": summary,
        "logs": logs,
        "workflow_events": workflow_events,
        "conversation_id": conversation_id,
        "api_key_configured": True,
        "task_id": tid,
        "request_id": request_id or "",
        "execution_session_id": session_id,
        "execution_status": "running",
        "react_mode": True,
        "model_trace": getattr(manager, "last_model_trace", {}),
        "token_usage": tu,
    }


def _json_response_small_talk_branch(
    manager: ARIAManager,
    conversation_manager: ConversationLibrary,
    *,
    conversation_id: str,
    request_id: str,
    client_task_id: str,
    new_task: bool,
    conversation_task_bookmark: dict[str, str],
    user_plain: str,
    detect_reason: str,
    detect_source: str,
    with_elapsed,
    _json_response,
):
    """闲聊/寒暄：单轮 TextExecAgent（可 SSE 流式），与 process_input 原 small_talk 分支一致。"""
    st_tid = _tid_for_response_non_parse(conversation_id, client_task_id, new_task, conversation_task_bookmark)
    manager.current_task_id = st_tid
    manager.push_event(
        "small_talk_detect",
        "success",
        "TaskParser",
        "识别为日常问候，切换轻量回复",
        {"reason": detect_reason, "source": detect_source},
    )
    manager.push_log("TaskParser", "识别为问候/闲聊，已跳过复杂流程", "completed")
    final_result = manager.generate_small_talk_reply(user_plain or "（用户上传了文件）")
    manager.push_event("small_talk_reply", "success", "TextExecAgent", "已生成简洁回复")
    manager.push_log("TextExecAgent", "简洁回复已发送", "completed")
    logs = manager.get_execution_log()
    workflow_events = manager.get_workflow_events()
    _tu = manager.get_token_usage_summary()
    conversation_manager.append_message(
        conversation_id,
        "assistant",
        final_result,
        {"logs": logs, "workflow_events": workflow_events, "token_usage": _tu, "task_id": st_tid},
    )
    conversation_manager.replace_workflow_events(conversation_id, workflow_events)
    return _json_response(
        with_elapsed(
            {
                "result": final_result,
                "logs": logs,
                "workflow_events": workflow_events,
                "conversation_id": conversation_id,
                "api_key_configured": True,
                "task_id": st_tid,
                "request_id": request_id or "",
                "model_trace": getattr(manager, "last_model_trace", {}),
                "token_usage": _tu,
            }
        )
    )


def _json_response_qa_only_branch(
    manager: ARIAManager,
    conversation_manager: ConversationLibrary,
    *,
    conversation_id: str,
    request_id: str,
    client_task_id: str,
    new_task: bool,
    conversation_task_bookmark: dict[str, str],
    llm_user_input: str,
    dialogue_context: str,
    with_elapsed,
    _json_response,
):
    """规划器判定 qa_only 且无动作：跳过后续路由 LLM 与多 Agent 瀑布流，直接流式作答。"""
    qa_tid = _tid_for_response_non_parse(conversation_id, client_task_id, new_task, conversation_task_bookmark)
    manager.current_task_id = qa_tid
    manager.push_event(
        "qa_direct",
        "success",
        "TextExecAgent",
        "纯问答：已跳过多 Agent 瀑布流",
        {"task_form": "qa_only"},
    )
    manager.push_log("TextExecAgent", "直接对话作答", "completed")
    final_result = manager.generate_direct_qa_reply(llm_user_input or "", dialogue_context)
    manager.push_event("qa_direct_reply", "success", "TextExecAgent", "已生成回答")
    manager.push_log("TextExecAgent", "回答已发送", "completed")
    logs = manager.get_execution_log()
    workflow_events = manager.get_workflow_events()
    _tu = manager.get_token_usage_summary()
    conversation_manager.append_message(
        conversation_id,
        "assistant",
        final_result,
        {"logs": logs, "workflow_events": workflow_events, "token_usage": _tu, "task_id": qa_tid},
    )
    conversation_manager.replace_workflow_events(conversation_id, workflow_events)
    return _json_response(
        with_elapsed(
            {
                "result": final_result,
                "logs": logs,
                "workflow_events": workflow_events,
                "conversation_id": conversation_id,
                "api_key_configured": True,
                "task_id": qa_tid,
                "request_id": request_id or "",
                "model_trace": getattr(manager, "last_model_trace", {}),
                "token_usage": _tu,
            }
        )
    )


def _process_input_classic_waterfall(
    manager: ARIAManager,
    conversation_manager: ConversationLibrary,
    conversation_id: str,
    llm_user_input: str,
    dialogue_context: str,
    reuse_for_parse: str | None,
    conversation_task_bookmark: dict[str, str],
    request_id: str,
    with_elapsed,
    _json_response,
):
    """parse_task → 方法论匹配/学习 → 多 Agent 编排 → 落库与响应（原 process_input 主瀑布流）。"""
    task_info = manager.parse_task(llm_user_input or "", dialogue_context, reuse_for_parse)
    current_task_id = task_info.get("task_id", "")
    conversation_task_bookmark[conversation_id] = current_task_id
    manager.current_task_id = current_task_id

    score, method = manager.match_methodology(task_info)

    if score < 0.7:
        if manager.should_skip_external_methodology_learning(task_info, llm_user_input or ""):
            method = manager.local_automation_methodology_stub(task_info)
            manager.push_event(
                "method_learn",
                "success",
                "SolutionLearner",
                "本机执行类任务，跳过外网方法论学习",
                {
                    "score": score,
                    "execution_surface": task_info.get("execution_surface"),
                },
            )
            manager.push_log(
                "SolutionLearner",
                "已跳过 learn_from_external（execution_surface=本机/微信自动化）",
                "completed",
            )
        elif manager.should_reuse_methodology_without_learn(task_info, score, method):
            manager.push_event(
                "method_learn",
                "success",
                "SolutionLearner",
                "强时效任务沿用已匹配流程模板，跳过整段外网学习",
                {"score": score, "temporal_risk": task_info.get("temporal_risk")},
            )
            manager.push_log(
                "SolutionLearner",
                f"已跳过 learn_from_external（temporal_risk=high, score={score:.2f}）",
                "completed",
            )
        else:
            method = manager.learn_from_external(task_info)

    orchestration_payload = manager.orchestration.execute_pipeline(task_info, method, dialogue_context)
    agents = orchestration_payload.get("agents", {})
    check_payload = orchestration_payload.get("check_payload", {})
    final_result = check_payload.get("final_result") if isinstance(check_payload, dict) else check_payload

    manager.save_methodology(task_info, method, check_payload)
    manager.destroy_agents(agents)

    logs = manager.get_execution_log()
    workflow_events = manager.get_workflow_events()
    _tu = manager.get_token_usage_summary()
    conversation_manager.append_message(
        conversation_id,
        "assistant",
        final_result,
        {"logs": logs, "workflow_events": workflow_events, "token_usage": _tu, "task_id": current_task_id},
    )
    conversation_manager.replace_workflow_events(conversation_id, workflow_events)

    return (
        _json_response(
            with_elapsed(
                {
                    "result": final_result,
                    "logs": logs,
                    "workflow_events": workflow_events,
                    "conversation_id": conversation_id,
                    "api_key_configured": True,
                    "task_id": current_task_id,
                    "request_id": request_id or "",
                    "model_trace": getattr(manager, "last_model_trace", {}),
                    "token_usage": _tu,
                }
            )
        ),
        agents,
    )


@app.route("/api/check_api_key")
def check_api_key():
    # 仅返回是否已配置，不返回任何密钥片段
    return jsonify({"has_api_key": bool(resolve_api_key())})


@app.route("/api/workspace_file", methods=["GET"])
def workspace_file():
    """只读下载 ARIA 工作区内的相对路径文件（供 file_write 落盘后在浏览器拉取）。"""
    raw = (request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"success": False, "message": "invalid_path"}), 400
    p, err = _resolve_workspace_path(raw)
    if err:
        return err
    try:
        if not p.is_file():
            return jsonify({"success": False, "message": "not_found"}), 404
        return send_file(str(p), as_attachment=True, download_name=p.name, mimetype="application/octet-stream")
    except Exception:
        return jsonify({"success": False, "message": "read_error"}), 500


@app.route("/api/workspace_asset", methods=["GET"])
def workspace_asset():
    """预览 ARIA 工作区内附件（用于聊天消息中的图片缩略图/文件直链）。"""
    raw = (request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"success": False, "message": "invalid_path"}), 400
    p, err = _resolve_workspace_path(raw)
    if err:
        return err
    try:
        if not p.is_file():
            return jsonify({"success": False, "message": "not_found"}), 404
        guessed, _ = mimetypes.guess_type(str(p))
        return send_file(str(p), as_attachment=False, mimetype=guessed or "application/octet-stream", conditional=True)
    except Exception:
        return jsonify({"success": False, "message": "read_error"}), 500


def _resolve_workspace_path(raw_path: str):
    raw = (raw_path or "").strip().replace("\\", "/")
    if not raw:
        raw = "."
    if ".." in raw or raw.startswith("/"):
        return None, (jsonify({"success": False, "message": "invalid_path"}), 400)
    try:
        return manager._ensure_safe_path(raw), None
    except ValueError:
        return None, (jsonify({"success": False, "message": "path_not_allowed"}), 403)


def _workspace_path_rel_display(path_obj) -> str:
    try:
        rel = path_obj.relative_to(manager.workspace_root)
        out = str(rel).replace("\\", "/")
        return out if out else "."
    except Exception:
        return "."


def _workspace_entry_payload(path_obj):
    stat = path_obj.stat()
    is_dir = path_obj.is_dir()
    rel = _workspace_path_rel_display(path_obj)
    guessed, _ = mimetypes.guess_type(str(path_obj))
    return {
        "name": path_obj.name or ".",
        "path": rel,
        "is_dir": is_dir,
        "size": 0 if is_dir else int(stat.st_size or 0),
        "modified_at": float(stat.st_mtime or 0.0),
        "mime_type": guessed or ("inode/directory" if is_dir else "application/octet-stream"),
    }


@app.route("/api/workspace_fs/list", methods=["GET"])
def workspace_fs_list():
    path_obj, err = _resolve_workspace_path(request.args.get("path", "."))
    if err:
        return err
    if not path_obj.exists():
        return jsonify({"success": False, "message": "not_found"}), 404
    if not path_obj.is_dir():
        return jsonify({"success": False, "message": "not_directory"}), 400
    try:
        limit = int(request.args.get("limit", "200") or "200")
    except ValueError:
        limit = 200
    limit = max(1, min(_WORKSPACE_LIST_LIMIT_MAX, limit))
    try:
        entries = sorted(path_obj.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        items = [_workspace_entry_payload(p) for p in entries[:limit]]
        return jsonify(
            {
                "success": True,
                "path": _workspace_path_rel_display(path_obj),
                "items": items,
                "truncated": len(entries) > limit,
                "limit": limit,
            }
        )
    except Exception:
        return jsonify({"success": False, "message": "read_error"}), 500


@app.route("/api/workspace_fs/meta", methods=["GET"])
def workspace_fs_meta():
    path_obj, err = _resolve_workspace_path(request.args.get("path", "."))
    if err:
        return err
    if not path_obj.exists():
        return jsonify({"success": False, "message": "not_found"}), 404
    try:
        return jsonify(
            {
                "success": True,
                "entry": _workspace_entry_payload(path_obj),
            }
        )
    except Exception:
        return jsonify({"success": False, "message": "read_error"}), 500


@app.route("/api/workspace_fs/preview", methods=["GET"])
def workspace_fs_preview():
    path_obj, err = _resolve_workspace_path(request.args.get("path", ""))
    if err:
        return err
    if not path_obj.exists():
        return jsonify({"success": False, "message": "not_found"}), 404
    if not path_obj.is_file():
        return jsonify({"success": False, "message": "not_file"}), 400
    ext = path_obj.suffix.lower()
    if ext not in _WORKSPACE_PREVIEW_TEXT_EXTS:
        return jsonify({"success": False, "message": "preview_type_not_allowed"}), 415
    try:
        with open(path_obj, "rb") as f:
            raw = f.read(_WORKSPACE_PREVIEW_MAX_BYTES + 1)
        truncated = len(raw) > _WORKSPACE_PREVIEW_MAX_BYTES
        raw = raw[:_WORKSPACE_PREVIEW_MAX_BYTES]
        content = raw.decode("utf-8", errors="replace")
        return jsonify(
            {
                "success": True,
                "path": _workspace_path_rel_display(path_obj),
                "content": content,
                "encoding": "utf-8",
                "truncated": truncated,
                "max_bytes": _WORKSPACE_PREVIEW_MAX_BYTES,
                "entry": _workspace_entry_payload(path_obj),
            }
        )
    except Exception:
        return jsonify({"success": False, "message": "read_error"}), 500


# 首页
@app.route("/")
def index():
    # 首页展示落地页；进入应用后才进入主交互界面
    return render_template("landing.html")


# 主交互页面
@app.route("/app")
def app_ui():
    return render_template("simple_index.html")


# 处理用户输入
@app.route("/api/process_input", methods=["POST"])
def process_input():
    request_started_at = time.perf_counter()

    def with_elapsed(payload: dict) -> dict:
        out = dict(payload or {})
        out["elapsed_ms"] = _elapsed_ms_since(request_started_at)
        _tu = manager.get_token_usage_summary()
        out["timing_breakdown"] = compute_timing_breakdown(
            elapsed_ms=out["elapsed_ms"],
            llm_ms=int(_tu.get("llm_wall_ms", 0) or 0),
            local_action_ms=0,
        )
        return out

    is_multipart = bool(request.content_type and "multipart/form-data" in request.content_type.lower())
    attachments_json_payload: Any = None
    upload_files: list = []
    if is_multipart:
        form = request.form
        user_input = form.get("user_input")
        conversation_id = (form.get("conversation_id") or "").strip() or None
        request_id = form.get("request_id") or ""
        action_screenshots_mp = str(form.get("action_screenshots") or "").lower() in ("1", "true", "yes", "on")
        new_task_mp = str(form.get("new_task") or "").lower() in ("1", "true", "yes", "on")
        client_tid_mp = str(form.get("task_id") or "").strip()
        aj = (form.get("attachments_json") or "").strip()
        if aj:
            try:
                attachments_json_payload = json.loads(aj)
            except Exception:
                attachments_json_payload = None
        upload_files = [f for f in request.files.getlist("files") if f and getattr(f, "filename", None)]
        react_mode_mp = str(form.get("react_mode") or "").lower() in ("1", "true", "yes", "on")
        payload_early = {
            "new_task": new_task_mp,
            "action_screenshots": action_screenshots_mp,
            "task_id": client_tid_mp,
            "reasoning_effort": (form.get("reasoning_effort") or "").strip() or None,
            "react_mode": react_mode_mp,
            "react_computer_use_vision": str(form.get("react_computer_use_vision") or "").lower() in ("1", "true", "yes", "on"),
        }
    else:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            data = {}
        payload_early = data
        user_input = data.get("user_input")
        conversation_id = (data.get("conversation_id") or "").strip() or None
        request_id = data.get("request_id", "")
        attachments_json_payload = data.get("attachments")

    if not conversation_id:
        conversation = conversation_manager.create_conversation("新会话")
        conversation_id = conversation.get("conversation_id")
    elif not conversation_manager.get_conversation(conversation_id):
        conversation = conversation_manager.create_conversation("新会话")
        conversation_id = conversation.get("conversation_id")

    # 通知 KAIROS 记录用户活动（重置 AutoDream 空闲计时器）
    manager.record_kairos_activity()

    attachment_records: list[dict] = []
    try:
        for f in upload_files[:MAX_FILES_PER_MESSAGE]:
            attachment_records.append(save_uploaded_file(manager, str(conversation_id), f))
        if attachments_json_payload:
            attachment_records.extend(merge_json_attachments(manager, str(conversation_id), attachments_json_payload))
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    by_path: dict[str, dict] = {}
    for r in attachment_records:
        p = str(r.get("path") or "")
        if p:
            by_path[p] = r
    attachment_records = list(by_path.values())

    attachment_excerpt = extract_llm_excerpt(manager, attachment_records) if attachment_records else ""
    user_plain = (user_input or "").strip()
    display_user_content = user_plain
    llm_user_input = user_plain
    if attachment_excerpt:
        llm_user_input = (
            (llm_user_input + "\n\n" if llm_user_input else "") + "【用户上传文件的抽取摘要】\n" + attachment_excerpt
        ).strip()
    elif attachment_records:
        llm_user_input = (
            (llm_user_input + "\n\n" if llm_user_input else "")
            + "【用户已上传以下文件（路径相对 ARIA 工作区）】\n"
            + "\n".join(f"- {r.get('path')}" for r in attachment_records)
        ).strip()

    manager.set_conversation_context(conversation_id)
    manager.current_request_id = request_id or ""
    manager.set_workspace_mode("aria")

    # 检查是否已配置 API Key（方舟：ARK_API_KEY；百炼：DASHSCOPE_API_KEY，见 resolve_inference_api_key）
    api_key = resolve_api_key()
    if not api_key:
        # 生成模拟日志
        logs = [
            {
                "agent": "系统",
                "content": "未检测到 API Key（方舟请设 ARK_API_KEY；百炼请设 DASHSCOPE_API_KEY），使用模拟模式",
                "status": "warning",
                "timestamp": time.time(),
            },
            {"agent": "TaskParser", "content": "正在分析你的问题", "status": "running", "timestamp": time.time() + 0.5},
            {"agent": "TaskParser", "content": "问题分析完成", "status": "completed", "timestamp": time.time() + 1},
            {
                "agent": "系统",
                "content": "请在 .env 配置 ARK_API_KEY（火山方舟）或 DASHSCOPE_API_KEY（百炼），与 OPENAI_BASE_URL 一致",
                "status": "warning",
                "timestamp": time.time() + 1.5,
            },
        ]
        conversation_manager.append_message(
            conversation_id,
            "user",
            display_user_content,
            {"attachments": attachment_records} if attachment_records else None,
        )
        mock_reply = (
            "未配置 API Key。默认使用火山方舟：在 .env 设置 ARK_API_KEY 与 "
            "OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3；若用百炼则改 base_url 并设 DASHSCOPE_API_KEY。"
        )
        conversation_manager.append_message(
            conversation_id,
            "assistant",
            mock_reply,
            {"logs": logs, "workflow_events": []},
        )
        manager.set_conversation_context("")
        return jsonify(
            with_elapsed(
                {
                    "result": mock_reply,
                    "logs": logs,
                    "workflow_events": [],
                    "conversation_id": conversation_id,
                    "api_key_configured": False,
                    "task_id": "",
                    "request_id": request_id or "",
                    "token_usage": _empty_token_usage(),
                }
            )
        )

    agents = {}
    try:
        payload = payload_early if isinstance(payload_early, dict) else {}
        action_screenshots = bool(payload.get("action_screenshots"))
        new_task = bool(payload.get("new_task"))
        client_task_id = str(payload.get("task_id") or "").strip()
        _rm = payload.get("react_mode") if isinstance(payload, dict) else None
        react_mode_user = _rm is True or (isinstance(_rm, str) and _rm.strip().lower() in ("1", "true", "yes", "on"))
        manager.set_workspace_mode("aria")
        dialogue_context = conversation_manager.format_dialogue_context_for_prompt(conversation_id)
        reuse_for_parse = _reuse_task_id_for_parse(
            conversation_id, client_task_id, new_task, conversation_task_bookmark
        )

        manager.set_api_key(api_key)
        manager.set_conversation_context(conversation_id)
        manager.clear_execution_log()
        manager.clear_workflow_events()
        manager.reset_token_usage()
        manager.set_turn_vision_images(image_data_urls_from_attachment_records(manager, attachment_records))
        attachment_exts = [str(r.get("ext") or "").lower() for r in attachment_records]
        client_re = payload.get("reasoning_effort") if isinstance(payload, dict) else None
        if client_re is not None and not isinstance(client_re, str):
            client_re = str(client_re).strip() or None
        elif isinstance(client_re, str):
            client_re = client_re.strip() or None
        eff_override = _normalize_reasoning_effort(client_re)
        if eff_override:
            manager.set_turn_reasoning_effort(eff_override)
        else:
            manager.set_turn_reasoning_effort(
                manager.resolve_reasoning_effort_for_turn(
                    llm_user_input or "",
                    dialogue_context,
                    has_attachments=bool(attachment_records),
                    attachment_exts=attachment_exts,
                )
            )
        conversation_manager.append_message(
            conversation_id,
            "user",
            display_user_content,
            {"attachments": attachment_records} if attachment_records else None,
        )

        if conversation_id in pending_action_plans:
            tid = _tid_for_response_non_parse(conversation_id, client_task_id, new_task, conversation_task_bookmark)
            manager.current_task_id = tid
            pending = pending_action_plans.get(conversation_id, {})
            actions = pending.get("actions") or []
            if _is_confirmation_text(user_input or ""):
                pending_action_plans.pop(conversation_id, None)
                if pending.get("react_mode"):
                    return jsonify(
                        with_elapsed(
                            _finalize_react_execution(
                                conversation_id,
                                request_id or "",
                                str(pending.get("user_goal") or ""),
                                str(pending.get("dialogue_context") or ""),
                                plan_summary=str(pending.get("summary") or ""),
                                plan_risk_level=str(pending.get("risk_level") or "medium"),
                                thread_task_id=tid,
                                action_screenshots=action_screenshots,
                                react_computer_use_vision=bool(pending.get("react_computer_use_vision")),
                            )
                        )
                    )
                return jsonify(
                    with_elapsed(
                        _finalize_action_execution(
                        conversation_id,
                        request_id or "",
                        actions,
                        plan_summary=str(pending.get("summary") or ""),
                        plan_risk_level=str(pending.get("risk_level") or "medium"),
                        thread_task_id=tid,
                        action_screenshots=action_screenshots,
                        )
                    )
                )

        if conversation_id in pending_action_plans:
            if not _is_confirmation_text(user_input or "") and not (
                _is_double_confirmation_text(user_input or "")
                and pending_action_plans.get(conversation_id, {}).get("double_confirm_ready")
            ):
                pending_action_plans.pop(conversation_id, None)

        # 短问候/感谢等：跳过 plan_actions（TaskParser 大块 JSON 不流式），首包即可走 TextExecAgent 流式
        if not attachment_records:
            fast = manager.classify_interaction_mode_heuristic(llm_user_input or "")
            if isinstance(fast, dict) and fast.get("mode") == "small_talk":
                return _json_response_small_talk_branch(
                    manager,
                    conversation_manager,
                    conversation_id=conversation_id,
                    request_id=request_id or "",
                    client_task_id=client_task_id,
                    new_task=new_task,
                    conversation_task_bookmark=conversation_task_bookmark,
                    user_plain=user_plain,
                    detect_reason=str(fast.get("reason") or ""),
                    detect_source=str(fast.get("source") or "heuristic"),
                    with_elapsed=with_elapsed,
                    _json_response=_json_response,
                )

        # TAOR 模式下跳过 plan_actions（节省 1 次 LLM 调用），直接进入 TAOR 循环
        if os.getenv("ARIA_TAOR_MODE", "0").strip().lower() in ("1", "true", "yes"):
            taor_result = manager.run_taor_pipeline(
                user_input=llm_user_input or "",
                dialogue_context=dialogue_context,
                conversation_id=conversation_id,
            )
            final_result = taor_result.get("final_result", "")
            logs = manager.get_execution_log()
            workflow_events = manager.get_workflow_events()
            _tu = manager.get_token_usage_summary()
            conversation_manager.append_message(
                conversation_id,
                "assistant",
                final_result,
                {"logs": logs, "workflow_events": workflow_events, "token_usage": _tu},
            )
            conversation_manager.replace_workflow_events(conversation_id, workflow_events)
            tid = _tid_for_response_non_parse(conversation_id, client_task_id, new_task, conversation_task_bookmark)
            manager.current_task_id = tid
            return _json_response(
                with_elapsed(
                    {
                        "result": final_result,
                        "logs": logs,
                        "workflow_events": workflow_events,
                        "conversation_id": conversation_id,
                        "api_key_configured": True,
                        "task_id": tid,
                        "request_id": request_id or "",
                        "needs_confirmation": False,
                        "model_trace": getattr(manager, "last_model_trace", {}),
                        "token_usage": _tu,
                        "is_success": taor_result.get("is_success", True),
                    }
                )
            )

        manager.push_event(
            "task_parse", "running", "TaskParser", "正在解析任务并规划执行步骤…", {}
        )
        plan = manager.plan_actions(llm_user_input or "", dialogue_context)
        react_mode_aria = bool(plan.get("react_recommended")) if isinstance(plan, dict) else False
        if plan.get("mode") == "clarify":
            tid = _tid_for_response_non_parse(conversation_id, client_task_id, new_task, conversation_task_bookmark)
            manager.current_task_id = tid
            clarify_text = manager.format_clarify_plan_for_user(plan)
            manager.push_event(
                "plan_clarify", "success", "TaskParser", "需用户补充信息后再继续", {"summary": plan.get("summary", "")}
            )
            manager.push_log("TaskParser", "已列出待确认项，等待用户回复", "completed")
            logs = manager.get_execution_log()
            workflow_events = manager.get_workflow_events()
            _tu = manager.get_token_usage_summary()
            conversation_manager.append_message(
                conversation_id,
                "assistant",
                clarify_text,
                {
                    "logs": logs,
                    "workflow_events": workflow_events,
                    "token_usage": _tu,
                    "clarify_plan": plan,
                    "task_id": tid,
                },
            )
            conversation_manager.replace_workflow_events(conversation_id, workflow_events)
            cc = plan.get("choices") if isinstance(plan.get("choices"), list) else []
            clarify_choices = [c for c in cc if isinstance(c, dict) and str(c.get("id") or "").strip()]
            return _json_response(
                with_elapsed(
                    {
                        "result": clarify_text,
                        "logs": logs,
                        "workflow_events": workflow_events,
                        "conversation_id": conversation_id,
                        "api_key_configured": True,
                        "task_id": tid,
                        "request_id": request_id or "",
                        "needs_confirmation": False,
                        "needs_clarify": True,
                        "clarify_choices": clarify_choices,
                        "model_trace": getattr(manager, "last_model_trace", {}),
                        "token_usage": _tu,
                    }
                )
            )
        if plan.get("mode") == "action" and (plan.get("actions") or react_mode_user or react_mode_aria):
            tid = _tid_for_response_non_parse(conversation_id, client_task_id, new_task, conversation_task_bookmark)
            manager.current_task_id = tid
            react_mode = bool(react_mode_user or react_mode_aria)
            # 若计划包含 computer_*，自动升级为 ReAct（需逐步观察）；桌面截图推理建议开视觉
            _plan_actions = plan.get("actions") or []
            _computer_use_types = {"computer_screenshot", "computer_click", "computer_double_click",
                                   "computer_type", "computer_key", "computer_drag", "computer_scroll"}
            _has_computer_use = any(
                isinstance(a, dict) and a.get("type") in _computer_use_types for a in _plan_actions
            )
            _computer_use_count = sum(
                1 for a in _plan_actions
                if isinstance(a, dict) and a.get("type") in _computer_use_types
            )
            # 仅当 computer_use 动作 ≥2 步（多步状态依赖）或 LLM 明确建议 ReAct 时才自动升级；
            # 单步 computer_use（如一次截图确认）不强制走 ReAct 循环
            if not react_mode and _has_computer_use and (_computer_use_count >= 2 or react_mode_aria):
                react_mode = True
            _rcv_payload = False
            if isinstance(payload, dict):
                _rcv_payload = (payload.get("react_computer_use_vision") is True) or (
                    isinstance(payload.get("react_computer_use_vision"), str)
                    and payload.get("react_computer_use_vision", "").strip().lower() in ("1", "true", "yes", "on")
                )
            if react_mode:
                if _has_computer_use:
                    react_computer_use_vision = True
                else:
                    react_computer_use_vision = _rcv_payload or bool(
                        plan.get("react_computer_use_vision_recommended")
                    )
            else:
                react_computer_use_vision = _rcv_payload
            if isinstance(plan, dict):
                plan["react_user_forced"] = bool(react_mode_user)
                if react_mode_user:
                    plan["react_mode_source"] = "user"
                elif react_mode_aria:
                    plan["react_mode_source"] = "aria"
                elif _has_computer_use:
                    plan["react_mode_source"] = "computer"
                else:
                    plan["react_mode_source"] = "aria"
            plan_risk_level = manager.evaluate_action_risk_level(plan.get("actions") or [])
            if react_mode and plan_risk_level == "safe":
                plan_risk_level = "medium"
            plan["risk_level"] = plan_risk_level
            plan["requires_double_confirmation"] = plan_risk_level == "high"
            auto_ok = (not react_mode) and plan_risk_level == "safe"
            if auto_ok:
                started_payload = _finalize_action_execution(
                    conversation_id,
                    request_id or "",
                    plan.get("actions") or [],
                    plan_summary=str(plan.get("summary") or ""),
                    plan_risk_level=plan_risk_level,
                    thread_task_id=tid,
                    action_screenshots=action_screenshots,
                )
                started_payload["needs_confirmation"] = False
                started_payload["auto_executed"] = True
                return jsonify(with_elapsed(started_payload))
            pending_action_plans[conversation_id] = {
                "actions": plan.get("actions") or [],
                "summary": plan.get("summary", ""),
                "risk_level": plan_risk_level,
                "created_at": time.time(),
                "double_confirm_ready": False,
                "react_mode": bool(react_mode),
                "react_computer_use_vision": bool(react_computer_use_vision),
                "user_goal": llm_user_input or "",
                "dialogue_context": dialogue_context,
            }
            plan["react_mode"] = bool(react_mode)
            preview_text = (
                manager.format_react_plan_for_user(plan, llm_user_input or "")
                if react_mode
                else manager.format_action_plan_for_user(plan)
            )
            manager.push_event(
                "action_plan",
                "success",
                "TaskParser",
                "已生成 ReAct 执行说明，等待确认" if react_mode else "已生成执行计划，等待确认",
                {"plan": plan, "react_mode": react_mode},
            )
            manager.push_log("TaskParser", "已生成执行计划，等待确认", "warning")
            logs = manager.get_execution_log()
            workflow_events = manager.get_workflow_events()
            _tu = manager.get_token_usage_summary()
            conversation_manager.append_message(
                conversation_id,
                "assistant",
                preview_text,
                {
                    "logs": logs,
                    "workflow_events": workflow_events,
                    "pending_actions": plan,
                    "token_usage": _tu,
                    "task_id": tid,
                    "react_mode": bool(react_mode),
                },
            )
            conversation_manager.replace_workflow_events(conversation_id, workflow_events)
            return _json_response(
                with_elapsed(
                    {
                        "result": preview_text,
                        "logs": logs,
                        "workflow_events": workflow_events,
                        "conversation_id": conversation_id,
                        "api_key_configured": True,
                        "task_id": tid,
                        "request_id": request_id or "",
                        "pending_actions": plan,
                        "needs_confirmation": True,
                        "needs_double_confirmation": bool(plan.get("requires_double_confirmation")),
                        "risk_level": plan_risk_level,
                        "react_mode": bool(react_mode),
                        "model_trace": getattr(manager, "last_model_trace", {}),
                        "token_usage": _tu,
                    }
                )
            )

        if plan.get("mode") == "small_talk" and not (plan.get("actions") or []):
            return _json_response_small_talk_branch(
                manager,
                conversation_manager,
                conversation_id=conversation_id,
                request_id=request_id or "",
                client_task_id=client_task_id,
                new_task=new_task,
                conversation_task_bookmark=conversation_task_bookmark,
                user_plain=user_plain,
                detect_reason=str(plan.get("summary") or "planner_small_talk"),
                detect_source="planner",
                with_elapsed=with_elapsed,
                _json_response=_json_response,
            )
        if (
            plan.get("mode") == "qa"
            and not (plan.get("actions") or [])
            and str(plan.get("task_form") or "").strip().lower() == "qa_only"
        ):
            return _json_response_qa_only_branch(
                manager,
                conversation_manager,
                conversation_id=conversation_id,
                request_id=request_id or "",
                client_task_id=client_task_id,
                new_task=new_task,
                conversation_task_bookmark=conversation_task_bookmark,
                llm_user_input=llm_user_input or "",
                dialogue_context=dialogue_context,
                with_elapsed=with_elapsed,
                _json_response=_json_response,
            )

        resp, agents = _process_input_classic_waterfall(
            manager,
            conversation_manager,
            conversation_id,
            llm_user_input or "",
            dialogue_context,
            reuse_for_parse,
            conversation_task_bookmark,
            request_id or "",
            with_elapsed,
            _json_response,
        )
        return resp
    except TaskCancelledError:
        cancelled_text = "任务已中止。你可以调整问题后重新发起。"
        logs = manager.get_execution_log()
        workflow_events = manager.get_workflow_events()
        _tu = manager.get_token_usage_summary()
        cx_tid = (
            conversation_task_bookmark.get(conversation_id) or getattr(manager, "current_task_id", "") or ""
        ).strip()
        conversation_manager.append_message(
            conversation_id,
            "assistant",
            cancelled_text,
            {"logs": logs, "workflow_events": workflow_events, "token_usage": _tu, "task_id": cx_tid},
        )
        conversation_manager.replace_workflow_events(conversation_id, workflow_events)
        return _json_response(
            with_elapsed(
                {
                    "result": cancelled_text,
                    "logs": logs,
                    "workflow_events": workflow_events,
                    "conversation_id": conversation_id,
                    "api_key_configured": True,
                    "task_id": cx_tid,
                    "request_id": request_id or "",
                    "cancelled": True,
                    "token_usage": _tu,
                }
            )
        )
    except Exception as e:
        err = f"执行错误: {str(e)}"
        _tu = manager.get_token_usage_summary()
        ex_tid = (
            conversation_task_bookmark.get(conversation_id) or getattr(manager, "current_task_id", "") or ""
        ).strip()
        logger.exception("process_input failed conversation_id=%s error_type=%s", conversation_id, type(e).__name__)
        snap_logs: list = []
        snap_events: list = []
        try:
            snap_logs = list(manager.get_execution_log() or [])
        except Exception:
            pass
        try:
            snap_events = list(manager.get_workflow_events() or [])
        except Exception:
            pass
        conversation_manager.append_message(
            conversation_id,
            "assistant",
            err,
            {
                "logs": snap_logs,
                "workflow_events": snap_events,
                "token_usage": _tu,
                "task_id": ex_tid,
                "error_type": type(e).__name__,
            },
        )
        try:
            conversation_manager.replace_workflow_events(conversation_id, snap_events)
        except Exception:
            pass
        return _json_response(
            with_elapsed(
                {
                    "result": err,
                    "logs": snap_logs,
                    "workflow_events": snap_events,
                    "conversation_id": conversation_id,
                    "api_key_configured": True,
                    "task_id": ex_tid,
                    "request_id": request_id or "",
                    "token_usage": _tu,
                    "error_type": type(e).__name__,
                }
            )
        )
    finally:
        if agents:
            try:
                manager.destroy_agents(agents)
            except TaskCancelledError:
                pass
            except Exception:
                pass
        manager.set_conversation_context("")
        manager.current_task_id = ""
        manager.current_request_id = ""
        manager.clear_turn_vision_images()
        manager.clear_turn_reasoning_effort()
        manager.clear_cancel(request_id)


@app.route("/api/confirm_actions", methods=["POST"])
def confirm_actions():
    data = request.json or {}
    action_type = data.get("action_type", "confirm")  # confirm | reject
    action_screenshots = bool(data.get("action_screenshots"))
    conversation_id = (data.get("conversation_id") or "").strip()
    request_id = (data.get("request_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "message": "缺少 conversation_id"}), 400
    pending = pending_action_plans.get(conversation_id)
    if not pending:
        return jsonify({"success": False, "message": "当前没有待确认动作"}), 404

    # 处理拒绝操作
    if action_type == "reject":
        pending_action_plans.pop(conversation_id, None)
        return jsonify({"success": True, "message": "已取消执行", "rejected": True})

    actions = pending.get("actions") or []
    risk_level = str(pending.get("risk_level") or manager.evaluate_action_risk_level(actions))

    pending_action_plans.pop(conversation_id, None)
    btid = (conversation_task_bookmark.get(conversation_id) or "").strip()
    if pending.get("react_mode"):
        payload = _finalize_react_execution(
            conversation_id,
            request_id,
            str(pending.get("user_goal") or ""),
            str(pending.get("dialogue_context") or ""),
            plan_summary=str(pending.get("summary") or ""),
            plan_risk_level=risk_level,
            thread_task_id=btid or None,
            action_screenshots=action_screenshots,
            react_computer_use_vision=bool(pending.get("react_computer_use_vision")),
        )
    else:
        payload = _finalize_action_execution(
            conversation_id,
            request_id,
            actions,
            plan_summary=str(pending.get("summary") or ""),
            plan_risk_level=risk_level,
            thread_task_id=btid or None,
            action_screenshots=action_screenshots,
        )
    payload["success"] = True
    return jsonify(payload)


@app.route("/api/execution/start", methods=["POST"])
def start_execution():
    data = request.json or {}
    action_screenshots = bool(data.get("action_screenshots"))
    conversation_id = (data.get("conversation_id") or "").strip()
    request_id = (data.get("request_id") or "").strip()
    actions = data.get("actions") if isinstance(data.get("actions"), list) else []
    if not conversation_id:
        return jsonify({"success": False, "message": "缺少 conversation_id"}), 400
    if not actions:
        return jsonify({"success": False, "message": "缺少 actions"}), 400
    btid = (conversation_task_bookmark.get(conversation_id) or "").strip()
    payload = _finalize_action_execution(
        conversation_id,
        request_id,
        actions,
        plan_summary="manual_start_execution",
        plan_risk_level=manager.evaluate_action_risk_level(actions),
        thread_task_id=btid or None,
        action_screenshots=action_screenshots,
    )
    payload["success"] = True
    return jsonify(payload)


@app.route("/api/execution/pause", methods=["POST"])
def pause_execution():
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "message": "缺少 session_id"}), 400
    return jsonify(manager.pause_execution_session(session_id))


@app.route("/api/execution/resume", methods=["POST"])
def resume_execution():
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "message": "缺少 session_id"}), 400
    return jsonify(manager.resume_execution_session(session_id))


@app.route("/api/execution/abort", methods=["POST"])
def abort_execution():
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "message": "缺少 session_id"}), 400
    return jsonify(manager.abort_execution_session(session_id))


@app.route("/api/execution/status")
def execution_status():
    session_id = (request.args.get("session_id") or "").strip()
    conversation_id = (request.args.get("conversation_id") or "").strip()
    if not session_id and conversation_id:
        session_id = execution_sessions_by_conversation.get(conversation_id, "")
    if not session_id:
        return jsonify({"success": False, "message": "缺少 session_id"}), 400
    return jsonify(manager.get_execution_session(session_id))


@app.route("/api/cancel_task", methods=["POST"])
def cancel_task():
    data = request.json or {}
    request_id = (data.get("request_id") or "").strip()
    conversation_id = (data.get("conversation_id") or "").strip()
    if not request_id:
        return jsonify({"success": False, "message": "缺少 request_id"}), 400

    manager.request_cancel(request_id)
    if conversation_id:
        old_conv = manager.current_conversation_id
        old_req = manager.current_request_id
        manager.set_conversation_context(conversation_id)
        manager.current_request_id = request_id
        manager.push_event(
            "task_cancelled",
            "success",
            "TaskParser",
            "用户主动中止当前任务",
            {"request_id": request_id},
        )
        manager.push_log("系统", "已接收中止指令", "warning")
        manager.set_conversation_context(old_conv)
        manager.current_request_id = old_req
    return jsonify({"success": True, "request_id": request_id})


# 获取执行日志
@app.route("/api/get_logs")
def get_logs():
    logs = manager.get_execution_log()
    return jsonify({"logs": logs})


# 获取结构化工作流事件
@app.route("/api/workflow_events")
def get_workflow_events():
    conversation_id = request.args.get("conversation_id", "")
    if conversation_id:
        convo = conversation_manager.get_conversation(conversation_id) or {}
        return jsonify({"workflow_events": convo.get("workflow_events", [])})
    return jsonify({"workflow_events": manager.get_workflow_events()})


@app.route("/api/audit_export")
def audit_export():
    """导出本会话的结构化 workflow_events（JSON），供审计；不含密钥。"""
    conversation_id = (request.args.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "message": "missing conversation_id"}), 400
    convo = conversation_manager.get_conversation(conversation_id) or {}
    events = convo.get("workflow_events") or []
    return jsonify(
        {
            "success": True,
            "conversation_id": conversation_id,
            "exported_at": time.time(),
            "workflow_events": events,
        }
    )


@app.route("/api/workflow_stream")
def workflow_stream():
    conversation_id = request.args.get("conversation_id", "")
    if not conversation_id:
        return jsonify({"success": False, "message": "missing conversation_id"}), 400

    q = queue.Queue(maxsize=200)
    with sse_lock:
        sse_subscribers.setdefault(conversation_id, []).append(q)

    def stream():
        try:
            # 连接建立后先发一条握手事件
            hello = {"type": "connected", "conversation_id": conversation_id, "timestamp": time.time()}
            yield f"data: {json.dumps(hello, ensure_ascii=False)}\n\n"
            while True:
                try:
                    evt = q.get(timeout=15)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    keepalive = {"type": "keepalive", "timestamp": time.time()}
                    yield f"data: {json.dumps(keepalive, ensure_ascii=False)}\n\n"
        finally:
            with sse_lock:
                subscribers = sse_subscribers.get(conversation_id, [])
                if q in subscribers:
                    subscribers.remove(q)
                if not subscribers and conversation_id in sse_subscribers:
                    sse_subscribers.pop(conversation_id, None)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(stream(), mimetype="text/event-stream", headers=headers)


# 新建会话
@app.route("/api/conversations", methods=["POST"])
def create_conversation():
    data = request.json or {}
    title = data.get("title", "新会话")
    conversation = conversation_manager.create_conversation(title)
    return jsonify({"conversation": conversation})


# 会话列表：默认与 ?archived=false 仅返回未归档；?archived=true 仅已归档
@app.route("/api/conversations")
def list_conversations():
    archived = request.args.get("archived")
    if archived == "true":
        archived_bool = True
    elif archived == "false":
        archived_bool = False
    else:
        archived_bool = None
    conversations = conversation_manager.list_conversations(archived_bool)
    return jsonify({"conversations": conversations})


# 会话详情
@app.route("/api/conversations/<conversation_id>")
def get_conversation(conversation_id):
    conversation = conversation_manager.get_conversation(conversation_id)
    if not conversation:
        return jsonify({"conversation": None, "success": False}), 404
    return jsonify({"conversation": conversation, "success": True})


# 删除会话
@app.route("/api/conversations/<conversation_id>", methods=["DELETE"])
def delete_conversation(conversation_id):
    success = conversation_manager.delete_conversation(conversation_id)
    return jsonify({"success": success})


# 搜索方法论
@app.route("/api/search_methodology", methods=["POST"])
def search_methodology():
    data = request.json
    keyword = data.get("keyword", "")
    results = methodology_manager.search_methodologies(keyword)
    return jsonify({"results": results})


# 获取方法论列表
@app.route("/api/get_methodologies")
def get_methodologies():
    methodologies = methodology_manager.get_all_methodologies()
    return jsonify({"methodologies": methodologies})


# 删除方法论
@app.route("/api/delete_methodology", methods=["POST"])
def delete_methodology():
    data = request.json
    methodology_id = data.get("methodology_id")
    success = methodology_manager.delete_methodology(methodology_id)
    return jsonify({"success": success})


@app.route("/api/delete_methodologies_batch", methods=["POST"])
def delete_methodologies_batch():
    data = request.json or {}
    methodology_ids = data.get("methodology_ids", [])
    result = methodology_manager.delete_methodologies_batch(
        methodology_ids if isinstance(methodology_ids, list) else []
    )
    return jsonify(result)


# 更新方法论分类（同时记录人工分类反馈）
@app.route("/api/update_methodology_category", methods=["POST"])
def update_methodology_category():
    data = request.json or {}
    methodology_id = data.get("methodology_id", "")
    category = data.get("category", "")
    if not methodology_id:
        return jsonify({"success": False, "message": "缺少methodology_id"}), 400
    updated = methodology_manager.update_methodology_category(methodology_id, category)
    return jsonify({"success": bool(updated), "methodology": updated})


# 创建方法论
@app.route("/api/create_methodology", methods=["POST"])
def create_methodology():
    data = request.json or {}
    if isinstance(data.get("methodology"), dict):
        payload = dict(data.get("methodology") or {})
    else:
        payload = {
            "scene": data.get("scene"),
            "keywords": data.get("keywords", []),
            "solve_steps": data.get("solve_steps", []),
            "title": data.get("title", ""),
            "category": data.get("category", ""),
            "applicable_range": data.get("applicable_range", "通用"),
            "status": data.get("status", "published"),
            "quality_metrics": data.get("quality_metrics", {}),
            "evidence_refs": data.get("evidence_refs", []),
        }
    methodology = methodology_manager.add_methodology(payload)
    return jsonify({"methodology_id": methodology["method_id"], "methodology": methodology, "success": True})


# 获取方法论详情
@app.route("/api/get_methodology", methods=["POST"])
def get_methodology():
    data = request.json
    methodology_id = data.get("methodology_id")
    methodology = methodology_manager.get_methodology_by_id(methodology_id)
    return jsonify({"methodology": methodology})


@app.route("/api/rollback_methodology", methods=["POST"])
def rollback_methodology():
    data = request.json or {}
    methodology_id = str(data.get("methodology_id") or "").strip()
    to_version = int(data.get("to_version") or 0)
    if not methodology_id or to_version <= 0:
        return jsonify({"success": False, "message": "缺少 methodology_id 或 to_version"}), 400
    rolled = methodology_manager.rollback_methodology(methodology_id, to_version)
    return jsonify({"success": bool(rolled), "methodology": rolled})


@app.route("/api/methodology_health", methods=["GET"])
def methodology_health():
    try:
        limit = int(request.args.get("limit", "100") or "100")
    except ValueError:
        limit = 100
    dashboard = methodology_manager.get_methodology_health_dashboard(limit=limit)
    summary = methodology_manager.get_ab_stats_summary()
    return jsonify({"rows": dashboard, "summary": summary, "success": True})


@app.route("/api/experience_hub_data", methods=["GET"])
def experience_hub_data():
    try:
        limit = int(request.args.get("limit", "60") or "60")
    except ValueError:
        limit = 60
    methods = methodology_manager.get_all_methodologies()
    health_map = _method_health_index(limit=max(limit, 300))
    regression = _load_regression_snapshot()
    cards = []
    for method in methods:
        if not isinstance(method, dict):
            continue
        mid = str(method.get("method_id") or "")
        health = health_map.get(mid, {})
        cards.append(_build_skill_card(method, health, regression))
    cards.sort(
        key=lambda x: (float(x.get("recommendation_score", 0.0) or 0.0), int(x.get("usage_count", 0) or 0)),
        reverse=True,
    )
    top_limit = max(4, min(30, limit // 2))
    alerts: list[dict] = []
    if regression.get("available"):
        strict = float(regression.get("strict_pass_rate", 0.0) or 0.0)
        if strict < 0.6:
            alerts.append(
                {
                    "type": "regression",
                    "level": "warning",
                    "message": f"回归严格通过率偏低：{strict:.0%}，建议谨慎复用低分技能",
                }
            )
        elif strict >= 0.85:
            alerts.append({"type": "regression", "level": "success", "message": f"回归严格通过率稳定：{strict:.0%}"})
    deprecate_count = sum(1 for c in cards if c.get("health_label") == "deprecate")
    if deprecate_count > 0:
        alerts.append(
            {"type": "quality", "level": "warning", "message": f"发现 {deprecate_count} 条低健康经验，建议回滚或重写"}
        )
    payload = {
        "success": True,
        "recommended_skills": cards[:top_limit],
        "all_skill_cards": cards[: max(1, min(300, limit))],
        "recent_successes": _recent_success_summaries(limit=8),
        "alerts": alerts,
        "regression": regression,
    }
    return jsonify(payload)


@app.route("/api/experience_recent_successes", methods=["GET"])
def experience_recent_successes():
    try:
        limit = int(request.args.get("limit", "8") or "8")
    except ValueError:
        limit = 8
    return jsonify({"success": True, "rows": _recent_success_summaries(limit=limit)})


@app.route("/api/create_skill_draft_from_recent", methods=["POST"])
def create_skill_draft_from_recent():
    data = request.json or {}
    conversation_id = str(data.get("conversation_id") or "").strip()
    rows = _recent_success_summaries(limit=30)
    picked = None
    if conversation_id:
        picked = next((r for r in rows if str(r.get("conversation_id") or "") == conversation_id), None)
    if picked is None and rows:
        picked = rows[0]
    if not picked:
        return jsonify({"success": False, "message": "没有可用于生成草稿的近期成功任务"}), 404
    draft = _build_skill_draft_from_recent(picked)
    return jsonify({"success": True, "draft": draft, "source": picked})


@app.route("/api/import_methodologies", methods=["POST"])
def import_methodologies():
    data = request.json or {}
    items = data.get("items")
    if not isinstance(items, list):
        return jsonify({"success": False, "message": "items 必须为数组"}), 400
    existing = methodology_manager.get_all_methodologies()
    existing_keys = {str(m.get("event_key") or "").strip() for m in existing if isinstance(m, dict)}
    imported = 0
    skipped = 0
    errors: list[dict] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            skipped += 1
            errors.append({"index": idx, "message": "非对象条目，已跳过"})
            continue
        scene = str(item.get("scene") or "").strip()
        steps = item.get("solve_steps") if isinstance(item.get("solve_steps"), list) else []
        if not scene and not steps:
            skipped += 1
            errors.append({"index": idx, "message": "缺少 scene/solve_steps"})
            continue
        normalized_preview = methodology_manager.normalize_methodology(item)
        event_key = str(normalized_preview.get("event_key") or "").strip()
        if event_key and event_key in existing_keys:
            skipped += 1
            errors.append({"index": idx, "message": f"重复 event_key: {event_key}"})
            continue
        created = methodology_manager.add_methodology(item)
        if created and isinstance(created, dict):
            imported += 1
            ek = str(created.get("event_key") or "").strip()
            if ek:
                existing_keys.add(ek)
        else:
            skipped += 1
            errors.append({"index": idx, "message": "创建失败"})
    return jsonify({"success": True, "imported": imported, "skipped": skipped, "errors": errors})


@app.route("/api/experience_metrics/event", methods=["POST"])
def experience_metrics_event():
    data = request.json or {}
    event_name = str(data.get("event") or "").strip()
    if not event_name:
        return jsonify({"success": False, "message": "missing event"}), 400
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    stats = _safe_read_json(_EXPERIENCE_METRICS_PATH, {})
    if not isinstance(stats, dict):
        stats = {}
    counters = stats.get("counters") if isinstance(stats.get("counters"), dict) else {}
    counters[event_name] = int(counters.get(event_name, 0) or 0) + 1
    history = stats.get("recent_events") if isinstance(stats.get("recent_events"), list) else []
    history.append(
        {
            "event": event_name,
            "method_id": str(data.get("method_id") or ""),
            "conversation_id": str(data.get("conversation_id") or ""),
            "at": now,
        }
    )
    history = history[-200:]
    stats["counters"] = counters
    stats["recent_events"] = history
    stats["updated_at"] = now
    _safe_write_json(_EXPERIENCE_METRICS_PATH, stats)
    return jsonify({"success": True, "counters": counters, "updated_at": now})


# 获取记忆状态
@app.route("/api/get_memory_status")
def get_memory_status():
    memory_status = {
        "short_term": {
            "task_id": manager.stm.task_id,
            "user_input": manager.stm.user_input,
            "current_step": manager.stm.current_step,
            "sub_tasks_count": len(manager.stm.sub_tasks),
            "agents_count": len(manager.stm.agent_status),
            "results_count": len(manager.stm.results),
            "logs_count": len(manager.stm.logs),
        },
        "mid_term": {
            "task_templates_count": len(manager.mtm.task_templates),
            "agent_combinations_count": len(manager.mtm.agent_combinations),
            "last_task_flow_count": len(manager.mtm.last_task_flow),
            "common_prompts_count": len(manager.mtm.common_prompts),
        },
        "long_term": {
            "methodologies_count": len(manager.ltm.methodologies),
            "best_cases_count": len(manager.ltm.best_cases),
            "knowledge_base_count": len(manager.ltm.knowledge_base),
        },
    }
    return jsonify({"memory_status": memory_status})


# 应用管理 API（新框架）
@app.route("/api/applications")
def list_applications():
    """列出所有已注册的应用插件。"""
    apps = manager.app_registry.list_apps()
    result = []
    for app in apps:
        result.append({
            "app_id": app.app_id,
            "app_name": app.app_name,
            "capabilities_count": len(app.capabilities),
        })
    return jsonify({"success": True, "applications": result})


@app.route("/api/applications/<app_id>/capabilities")
def get_application_capabilities(app_id):
    """获取指定应用的能力列表。"""
    app = manager.app_registry.get_app(app_id)
    if not app:
        return jsonify({"success": False, "message": f"应用 {app_id} 不存在"}), 404

    capabilities = []
    for cap in app.capabilities:
        capabilities.append({
            "action_type": cap.action_type,
            "display_name": cap.display_name,
            "description": cap.description,
            "risk_level": cap.risk_level,
            "requires_confirmation": cap.requires_confirmation,
            "parameters": [
                {
                    "name": p.name,
                    "display_name": p.display_name,
                    "param_type": p.param_type,
                    "required": p.required,
                    "description": p.description,
                }
                for p in cap.parameters
            ],
        })

    return jsonify({
        "success": True,
        "app_id": app.app_id,
        "app_name": app.app_name,
        "capabilities": capabilities,
    })


# ------------------------------------------------------------------ #
# KAIROS API — 定时触发 / AutoDream / 状态查询                          #
# ------------------------------------------------------------------ #

@app.route("/api/kairos/status", methods=["GET"])
def kairos_status():
    """返回 KAIROS 引擎运行状态。"""
    if manager.kairos is None:
        return jsonify({"running": False, "message": "KAIROS 未启用（设置 ARIA_KAIROS_ENABLED=1）"})
    return jsonify({"success": True, **manager.kairos.get_status()})


@app.route("/api/triggers", methods=["GET"])
def list_triggers():
    """列出所有定时触发器。"""
    if manager.kairos is None:
        return jsonify({"success": False, "message": "KAIROS 未启用"}), 400
    tasks = manager.kairos.scheduler.list_tasks()
    return jsonify({"success": True, "triggers": tasks, "count": len(tasks)})


@app.route("/api/triggers/create", methods=["POST"])
def create_trigger():
    """
    创建定时触发器。

    Body: {"cron": "0 9 * * 1-5", "prompt": "检查今日待办", "recurring": true, "durable": true}
    """
    if manager.kairos is None:
        return jsonify({"success": False, "message": "KAIROS 未启用（设置 ARIA_KAIROS_ENABLED=1）"}), 400
    data = request.get_json(silent=True) or {}
    cron = str(data.get("cron") or "").strip()
    prompt = str(data.get("prompt") or "").strip()
    if not cron or not prompt:
        return jsonify({"success": False, "message": "cron 和 prompt 为必填项"}), 400
    recurring = bool(data.get("recurring", True))
    durable = bool(data.get("durable", True))
    try:
        task_id = manager.kairos.scheduler.create(cron, prompt, recurring=recurring, durable=durable)
        return jsonify({"success": True, "task_id": task_id, "message": f"触发器 {task_id} 已创建"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/triggers/<task_id>", methods=["DELETE"])
def delete_trigger(task_id: str):
    """删除定时触发器。"""
    if manager.kairos is None:
        return jsonify({"success": False, "message": "KAIROS 未启用"}), 400
    deleted = manager.kairos.scheduler.delete(task_id)
    if deleted:
        return jsonify({"success": True, "message": f"触发器 {task_id} 已删除"})
    return jsonify({"success": False, "message": f"触发器 {task_id} 不存在"}), 404


@app.route("/api/dream/run", methods=["POST"])
def run_dream():
    """手动触发一次 AutoDream 周期。"""
    if manager.kairos is None:
        return jsonify({"success": False, "message": "KAIROS 未启用（设置 ARIA_KAIROS_ENABLED=1）"}), 400
    result = manager.kairos.dream_engine.run_dream_cycle()
    return jsonify(result)


@app.route("/api/eval/report", methods=["GET"])
def eval_report():
    """返回最新 Pass@k 评估报告快照。"""
    data = _safe_read_json(_EVAL_REPORT_PATH, {})
    if not data:
        return jsonify({"available": False, "message": "尚未运行评估，请先调用 /api/eval/run"}), 404
    suite = data.get("suite") or {}
    return jsonify({
        "available": True,
        "generated_at": data.get("generated_at", ""),
        "k": data.get("k", 1),
        "avg_pass_at_k": suite.get("avg_pass_at_k", 0.0),
        "avg_pass_rate": suite.get("avg_pass_rate", 0.0),
        "avg_score": suite.get("avg_score", 0.0),
        "avg_latency_ms": suite.get("avg_latency_ms", 0.0),
        "total_cases": suite.get("total_cases", 0),
        "perfect_consistency_cases": suite.get("perfect_consistency_cases", 0),
        "zero_pass_cases": suite.get("zero_pass_cases", 0),
        "by_category": suite.get("by_category", {}),
        "by_difficulty": suite.get("by_difficulty", {}),
        "transcript_summary": data.get("transcript_summary", {}),
        "cases": data.get("cases", []),
    })


@app.route("/api/eval/run", methods=["POST"])
def eval_run():
    """触发一次 Pass@k 评估（在后台线程执行）。

    Body (JSON, all optional):
        k           : int   trials per case (default 3 for web, max 10)
        categories  : list  filter by category
        save_transcripts: bool (default true)
    """
    try:
        body = request.get_json(silent=True) or {}
        k = max(1, min(10, int(body.get("k", 3))))
        categories = body.get("categories") or None
        save_transcripts = bool(body.get("save_transcripts", True))

        cases_raw = _safe_read_json(_EVAL_CASES_PATH, [])
        if not isinstance(cases_raw, list) or not cases_raw:
            return jsonify({"success": False, "message": "benchmark cases 文件不存在或为空"}), 400

        # Run in background thread to avoid blocking SSE/Flask workers
        def _run():
            try:
                from scripts.run_evaluation import run_evaluation
                run_evaluation(
                    manager=manager,
                    cases=cases_raw,
                    k=k,
                    categories=list(categories) if categories else None,
                    save_transcripts=save_transcripts,
                )
            except Exception as exc:
                logger.warning("eval_run background error: %s", exc)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return jsonify({
            "success": True,
            "message": f"评估已启动（k={k}, categories={categories or 'all'}），完成后可通过 /api/eval/report 查看结果",
            "k": k,
            "categories": categories,
            "total_cases": len(cases_raw),
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/eval/cases", methods=["GET"])
def eval_cases():
    """返回当前 benchmark cases 列表（不执行）。"""
    cases = _safe_read_json(_EVAL_CASES_PATH, [])
    if not isinstance(cases, list):
        return jsonify({"cases": [], "total": 0})
    return jsonify({"cases": cases, "total": len(cases)})


if __name__ == "__main__":
    # 创建templates目录
    if not os.path.exists("templates"):
        os.makedirs("templates")

    # 启动应用
    app.run(debug=True, host="0.0.0.0", port=5000)
