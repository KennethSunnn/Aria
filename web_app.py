from flask import Flask, Response, jsonify, render_template, request, send_file
import os
import sys
import time
import json
import queue
import threading
import uuid
from dotenv import load_dotenv

# 加载环境变量（强制覆盖同名系统变量，避免读取到空值）
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

# 添加项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from aria_manager import ARIAManager, TaskCancelledError
from llm.volcengine_llm import _normalize_reasoning_effort, resolve_inference_api_key
from conversation_lib import ConversationLibrary
from method_lib import MethodologyLibrary
from chat_attachments import (
    MAX_FILES_PER_MESSAGE,
    extract_llm_excerpt,
    image_data_urls_from_attachment_records,
    merge_json_attachments,
    save_uploaded_file,
)
from typing import Any

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


def _reuse_task_id_for_parse(conversation_id: str, client_task_id: str, new_task: bool, bookmark: dict[str, str]) -> str | None:
    """供 parse_task 沿用线程：new_task 则新 UUID；否则优先客户端 task_id，再回落书签。"""
    if new_task:
        return None
    ct = (client_task_id or "").strip()
    if ct:
        try:
            uuid.UUID(ct)
            return ct
        except ValueError:
            pass
    ex = (bookmark.get(conversation_id) or "").strip()
    if ex:
        try:
            uuid.UUID(ex)
            return ex
        except ValueError:
            pass
    return None


def _tid_for_response_non_parse(conversation_id: str, client_task_id: str, new_task: bool, bookmark: dict[str, str]) -> str:
    """clarify / action 预览 / small_talk / 待确认消息 等使用的 task_id，并维护书签（parse_task 路径由书签在 parse 后单独写入）。"""
    if new_task:
        tid = str(uuid.uuid4())
        bookmark[conversation_id] = tid
        return tid
    ct = (client_task_id or "").strip()
    if ct:
        try:
            uuid.UUID(ct)
            bookmark[conversation_id] = ct
            return ct
        except ValueError:
            pass
    ex = (bookmark.get(conversation_id) or "").strip()
    if ex:
        try:
            uuid.UUID(ex)
            return ex
        except ValueError:
            pass
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


def resolve_api_key() -> str:
    """按 OPENAI_BASE_URL 选择 ARK / 百炼密钥；每次从 .env 刷新。"""
    return resolve_inference_api_key(_DOTENV_PATH)


def _empty_token_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0}


def _json_response(data: dict) -> Response:
    """在 JSON 响应中附带当前线程累计的 LLM token 用量（便于估算成本）。"""
    payload = dict(data)
    payload["token_usage"] = manager.get_token_usage_summary()
    return jsonify(payload)


def _is_confirmation_text(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    keywords = [
        "确认执行", "确认", "继续执行", "执行吧", "同意执行",
        "confirm", "yes execute", "execute now", "go ahead"
    ]
    return any(k in t for k in keywords)


def _is_double_confirmation_text(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    keywords = [
        "二次确认", "再次确认", "高风险确认", "最终确认",
        "double confirm", "second confirm", "final confirm",
    ]
    return any(k in t for k in keywords)


def _finalize_action_execution(
    conversation_id: str,
    request_id: str,
    actions: list[dict],
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
        'result': summary,
        'logs': logs,
        'workflow_events': workflow_events,
        'conversation_id': conversation_id,
        'api_key_configured': True,
        'task_id': tid,
        'request_id': request_id or "",
        'execution_session_id': session_id,
        'execution_status': 'running',
        'model_trace': getattr(manager, "last_model_trace", {}),
        'token_usage': tu,
    }


@app.route('/api/check_api_key')
def check_api_key():
    # 仅返回是否已配置，不返回任何密钥片段
    return jsonify({'has_api_key': bool(resolve_api_key())})


@app.route('/api/workspace_file', methods=['GET'])
def workspace_file():
    """只读下载 ARIA 工作区内的相对路径文件（供 file_write 落盘后在浏览器拉取）。"""
    raw = (request.args.get('path') or '').strip().replace('\\', '/')
    if not raw or '..' in raw or raw.startswith('/'):
        return jsonify({'success': False, 'message': 'invalid_path'}), 400
    try:
        p = manager._ensure_safe_path(raw)
        if not p.is_file():
            return jsonify({'success': False, 'message': 'not_found'}), 404
        return send_file(
            str(p),
            as_attachment=True,
            download_name=p.name,
            mimetype='application/octet-stream',
        )
    except ValueError:
        return jsonify({'success': False, 'message': 'path_not_allowed'}), 403
    except Exception:
        return jsonify({'success': False, 'message': 'read_error'}), 500


# 首页
@app.route('/')
def index():
    # 首页展示落地页；进入应用后才进入主交互界面
    return render_template('landing.html')

# 主交互页面
@app.route('/app')
def app_ui():
    return render_template('simple_index.html')

# 处理用户输入
@app.route('/api/process_input', methods=['POST'])
def process_input():
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
        payload_early = {
            "new_task": new_task_mp,
            "action_screenshots": action_screenshots_mp,
            "task_id": client_tid_mp,
            "reasoning_effort": (form.get("reasoning_effort") or "").strip() or None,
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

    attachment_records: list[dict] = []
    try:
        for f in upload_files[:MAX_FILES_PER_MESSAGE]:
            attachment_records.append(save_uploaded_file(manager, str(conversation_id), f))
        if attachments_json_payload:
            attachment_records.extend(
                merge_json_attachments(manager, str(conversation_id), attachments_json_payload)
            )
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
    if attachment_records:
        names = ", ".join(str(r.get("name") or "") for r in attachment_records)
        display_user_content = (
            (display_user_content + "\n\n" if display_user_content else "") + f"[附件 {len(attachment_records)} 个: {names}]"
        )
    llm_user_input = user_plain
    if attachment_excerpt:
        llm_user_input = (
            (llm_user_input + "\n\n" if llm_user_input else "")
            + "【用户上传文件的抽取摘要】\n"
            + attachment_excerpt
        ).strip()
    elif attachment_records:
        llm_user_input = (
            (llm_user_input + "\n\n" if llm_user_input else "")
            + "【用户已上传以下文件（路径相对 ARIA 工作区）】\n"
            + "\n".join(f"- {r.get('path')}" for r in attachment_records)
        ).strip()

    manager.set_conversation_context(conversation_id)
    manager.current_request_id = request_id or ""
    
    # 检查是否已配置 API Key（方舟：ARK_API_KEY；百炼：DASHSCOPE_API_KEY，见 resolve_inference_api_key）
    api_key = resolve_api_key()
    if not api_key:
        # 生成模拟日志
        logs = [
            {
                "agent": "系统",
                "content": "未检测到 API Key（方舟请设 ARK_API_KEY；百炼请设 DASHSCOPE_API_KEY），使用模拟模式",
                "status": "warning",
                "timestamp": time.time()
            },
            {
                "agent": "TaskParser",
                "content": "正在分析你的问题",
                "status": "running",
                "timestamp": time.time() + 0.5
            },
            {
                "agent": "TaskParser",
                "content": "问题分析完成",
                "status": "completed",
                "timestamp": time.time() + 1
            },
            {
                "agent": "系统",
                "content": "请在 .env 配置 ARK_API_KEY（火山方舟）或 DASHSCOPE_API_KEY（百炼），与 OPENAI_BASE_URL 一致",
                "status": "warning",
                "timestamp": time.time() + 1.5
            }
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
        return jsonify({
            'result': mock_reply,
            'logs': logs,
            'workflow_events': [],
            'conversation_id': conversation_id,
            'api_key_configured': False,
            'task_id': "",
            'request_id': request_id or "",
            'token_usage': _empty_token_usage(),
        })
    
    agents = {}
    try:
        payload = payload_early if isinstance(payload_early, dict) else {}
        action_screenshots = bool(payload.get("action_screenshots"))
        new_task = bool(payload.get("new_task"))
        client_task_id = str(payload.get("task_id") or "").strip()
        dialogue_context = conversation_manager.format_dialogue_context_for_prompt(conversation_id)
        reuse_for_parse = _reuse_task_id_for_parse(conversation_id, client_task_id, new_task, conversation_task_bookmark)

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
                if manager.requires_double_confirmation(actions):
                    pending["double_confirm_ready"] = True
                    pending_action_plans[conversation_id] = pending
                    msg = "检测到高风险动作。请回复“二次确认”后执行。"
                    manager.push_event("action_double_confirm_required", "warning", "TaskParser", msg)
                    manager.push_log("TaskParser", msg, "warning")
                    logs = manager.get_execution_log()
                    workflow_events = manager.get_workflow_events()
                    _tu = manager.get_token_usage_summary()
                    conversation_manager.append_message(
                        conversation_id,
                        "assistant",
                        msg,
                        {
                            "logs": logs,
                            "workflow_events": workflow_events,
                            "pending_actions": {"actions": actions, "requires_double_confirmation": True},
                            "token_usage": _tu,
                            "task_id": tid,
                        },
                    )
                    conversation_manager.replace_workflow_events(conversation_id, workflow_events)
                    return _json_response({
                        'result': msg,
                        'logs': logs,
                        'workflow_events': workflow_events,
                        'conversation_id': conversation_id,
                        'api_key_configured': True,
                        'task_id': tid,
                        'request_id': request_id or "",
                        'pending_actions': {"actions": actions, "requires_double_confirmation": True},
                        'needs_confirmation': True,
                        'needs_double_confirmation': True,
                        'model_trace': getattr(manager, "last_model_trace", {}),
                        'token_usage': _tu,
                    })
                pending_action_plans.pop(conversation_id, None)
                return jsonify(
                    _finalize_action_execution(
                        conversation_id,
                        request_id or "",
                        actions,
                        thread_task_id=tid,
                        action_screenshots=action_screenshots,
                    )
                )

            if _is_double_confirmation_text(user_input or "") and pending.get("double_confirm_ready"):
                pending_action_plans.pop(conversation_id, None)
                return jsonify(
                    _finalize_action_execution(
                        conversation_id,
                        request_id or "",
                        actions,
                        thread_task_id=tid,
                        action_screenshots=action_screenshots,
                    )
                )

        plan = manager.plan_actions(llm_user_input or "", dialogue_context)
        if plan.get("mode") == "clarify":
            tid = _tid_for_response_non_parse(conversation_id, client_task_id, new_task, conversation_task_bookmark)
            manager.current_task_id = tid
            clarify_text = manager.format_clarify_plan_for_user(plan)
            manager.push_event("plan_clarify", "success", "TaskParser", "需用户补充信息后再继续", {"summary": plan.get("summary", "")})
            manager.push_log("TaskParser", "已列出待确认项，等待用户回复", "completed")
            logs = manager.get_execution_log()
            workflow_events = manager.get_workflow_events()
            _tu = manager.get_token_usage_summary()
            conversation_manager.append_message(
                conversation_id,
                "assistant",
                clarify_text,
                {"logs": logs, "workflow_events": workflow_events, "token_usage": _tu, "clarify_plan": plan, "task_id": tid},
            )
            conversation_manager.replace_workflow_events(conversation_id, workflow_events)
            return _json_response({
                "result": clarify_text,
                "logs": logs,
                "workflow_events": workflow_events,
                "conversation_id": conversation_id,
                "api_key_configured": True,
                "task_id": tid,
                "request_id": request_id or "",
                "needs_confirmation": False,
                "needs_clarify": True,
                "model_trace": getattr(manager, "last_model_trace", {}),
                "token_usage": _tu,
            })
        if plan.get("mode") == "action" and plan.get("actions"):
            tid = _tid_for_response_non_parse(conversation_id, client_task_id, new_task, conversation_task_bookmark)
            manager.current_task_id = tid
            plan["requires_double_confirmation"] = manager.requires_double_confirmation(plan.get("actions") or [])
            needs_user_gate = manager.actions_require_user_gate(plan.get("actions") or [])
            auto_ok = not plan.get("requires_double_confirmation") and not needs_user_gate
            if auto_ok:
                started_payload = _finalize_action_execution(
                    conversation_id,
                    request_id or "",
                    plan.get("actions") or [],
                    thread_task_id=tid,
                    action_screenshots=action_screenshots,
                )
                started_payload["needs_confirmation"] = False
                started_payload["auto_executed"] = True
                return jsonify(started_payload)
            pending_action_plans[conversation_id] = {
                "actions": plan.get("actions"),
                "summary": plan.get("summary", ""),
                "created_at": time.time(),
                "double_confirm_ready": False,
            }
            preview_text = manager.format_action_plan_for_user(plan)
            manager.push_event("action_plan", "success", "TaskParser", "已生成执行计划，等待确认", {"plan": plan})
            manager.push_log("TaskParser", "已生成执行计划，等待确认", "warning")
            logs = manager.get_execution_log()
            workflow_events = manager.get_workflow_events()
            _tu = manager.get_token_usage_summary()
            conversation_manager.append_message(
                conversation_id,
                "assistant",
                preview_text,
                {"logs": logs, "workflow_events": workflow_events, "pending_actions": plan, "token_usage": _tu, "task_id": tid},
            )
            conversation_manager.replace_workflow_events(conversation_id, workflow_events)
            return _json_response({
                'result': preview_text,
                'logs': logs,
                'workflow_events': workflow_events,
                'conversation_id': conversation_id,
                'api_key_configured': True,
                'task_id': tid,
                'request_id': request_id or "",
                'pending_actions': plan,
                'needs_confirmation': True,
                'needs_double_confirmation': bool(plan.get("requires_double_confirmation")),
                'model_trace': getattr(manager, "last_model_trace", {}),
                'token_usage': _tu,
            })

        route = manager.classify_interaction_mode(llm_user_input or "")
        if route.get("mode") == "small_talk":
            st_tid = _tid_for_response_non_parse(conversation_id, client_task_id, new_task, conversation_task_bookmark)
            manager.current_task_id = st_tid
            manager.push_event(
                "small_talk_detect",
                "success",
                "TaskParser",
                "识别为日常问候，切换轻量回复",
                {"reason": route.get("reason", ""), "source": route.get("source", "heuristic")},
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
            return _json_response({
                'result': final_result,
                'logs': logs,
                'workflow_events': workflow_events,
                'conversation_id': conversation_id,
                'api_key_configured': True,
                'task_id': st_tid,
                'request_id': request_id or "",
                'model_trace': getattr(manager, "last_model_trace", {}),
                'token_usage': _tu,
            })

        task_info = manager.parse_task(llm_user_input or "", dialogue_context, reuse_for_parse)
        current_task_id = task_info.get("task_id", "")
        conversation_task_bookmark[conversation_id] = current_task_id
        manager.current_task_id = current_task_id
        
        # 匹配方法论
        score, method = manager.match_methodology(task_info)
        
        # 低于 0.7 通常重新学习；强时效任务若已有可用流程模板（分数 ≥ ARIA_TEMPORAL_METHOD_MATCH_FLOOR）则跳过以省 Token
        if score < 0.7:
            if manager.should_reuse_methodology_without_learn(task_info, score, method):
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
        
        # 拆分子任务
        sub_tasks = manager.split_sub_tasks(task_info, method)
        
        # 生成Agent
        agents = manager.create_agents(sub_tasks)
        
        # 执行Agent
        results = manager.run_agents(agents, method, dialogue_context)
        
        # 校验结果
        check_payload = manager.check_result(results)
        final_result = check_payload.get("final_result") if isinstance(check_payload, dict) else check_payload
        
        # 保存方法论
        manager.save_methodology(task_info, method, check_payload)
        
        # 销毁Agent
        manager.destroy_agents(agents)
        
        # 获取执行日志
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

        return _json_response({
            'result': final_result,
            'logs': logs,
            'workflow_events': workflow_events,
            'conversation_id': conversation_id,
            'api_key_configured': True,
            'task_id': current_task_id,
            'request_id': request_id or "",
            'model_trace': getattr(manager, "last_model_trace", {}),
            'token_usage': _tu,
        })
    except TaskCancelledError:
        cancelled_text = "任务已中止。你可以调整问题后重新发起。"
        logs = manager.get_execution_log()
        workflow_events = manager.get_workflow_events()
        _tu = manager.get_token_usage_summary()
        cx_tid = (conversation_task_bookmark.get(conversation_id) or getattr(manager, "current_task_id", "") or "").strip()
        conversation_manager.append_message(
            conversation_id,
            "assistant",
            cancelled_text,
            {"logs": logs, "workflow_events": workflow_events, "token_usage": _tu, "task_id": cx_tid},
        )
        conversation_manager.replace_workflow_events(conversation_id, workflow_events)
        return _json_response({
            'result': cancelled_text,
            'logs': logs,
            'workflow_events': workflow_events,
            'conversation_id': conversation_id,
            'api_key_configured': True,
            'task_id': cx_tid,
            'request_id': request_id or "",
            'cancelled': True,
            'token_usage': _tu,
        })
    except Exception as e:
        err = f'执行错误: {str(e)}'
        _tu = manager.get_token_usage_summary()
        ex_tid = (conversation_task_bookmark.get(conversation_id) or getattr(manager, "current_task_id", "") or "").strip()
        conversation_manager.append_message(
            conversation_id,
            "assistant",
            err,
            {"logs": [], "workflow_events": [], "token_usage": _tu, "task_id": ex_tid},
        )
        return _json_response({
            'result': err,
            'logs': [],
            'workflow_events': [],
            'conversation_id': conversation_id,
            'api_key_configured': True,
            'task_id': ex_tid,
            'request_id': request_id or "",
            'token_usage': _tu,
        })
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


@app.route('/api/confirm_actions', methods=['POST'])
def confirm_actions():
    data = request.json or {}
    action_screenshots = bool(data.get("action_screenshots"))
    conversation_id = (data.get('conversation_id') or '').strip()
    request_id = (data.get('request_id') or '').strip()
    if not conversation_id:
        return jsonify({'success': False, 'message': '缺少 conversation_id'}), 400
    pending = pending_action_plans.get(conversation_id)
    if not pending:
        return jsonify({'success': False, 'message': '当前没有待确认动作'}), 404

    force = bool(data.get('force'))
    actions = pending.get("actions") or []
    if manager.requires_double_confirmation(actions) and not force:
        pending["double_confirm_ready"] = True
        pending_action_plans[conversation_id] = pending
        return jsonify({
            'success': False,
            'message': '检测到高风险动作，请二次确认后执行',
            'requires_double_confirmation': True,
            'pending_actions': {"actions": actions, "requires_double_confirmation": True},
        }), 409

    pending_action_plans.pop(conversation_id, None)
    btid = (conversation_task_bookmark.get(conversation_id) or "").strip()
    payload = _finalize_action_execution(
        conversation_id, request_id, actions, thread_task_id=btid or None, action_screenshots=action_screenshots
    )
    payload['success'] = True
    return jsonify(payload)


@app.route('/api/execution/start', methods=['POST'])
def start_execution():
    data = request.json or {}
    action_screenshots = bool(data.get("action_screenshots"))
    conversation_id = (data.get('conversation_id') or '').strip()
    request_id = (data.get('request_id') or '').strip()
    actions = data.get('actions') if isinstance(data.get('actions'), list) else []
    if not conversation_id:
        return jsonify({'success': False, 'message': '缺少 conversation_id'}), 400
    if not actions:
        return jsonify({'success': False, 'message': '缺少 actions'}), 400
    btid = (conversation_task_bookmark.get(conversation_id) or "").strip()
    payload = _finalize_action_execution(
        conversation_id, request_id, actions, thread_task_id=btid or None, action_screenshots=action_screenshots
    )
    payload['success'] = True
    return jsonify(payload)


@app.route('/api/execution/pause', methods=['POST'])
def pause_execution():
    data = request.json or {}
    session_id = (data.get('session_id') or '').strip()
    if not session_id:
        return jsonify({'success': False, 'message': '缺少 session_id'}), 400
    return jsonify(manager.pause_execution_session(session_id))


@app.route('/api/execution/resume', methods=['POST'])
def resume_execution():
    data = request.json or {}
    session_id = (data.get('session_id') or '').strip()
    if not session_id:
        return jsonify({'success': False, 'message': '缺少 session_id'}), 400
    return jsonify(manager.resume_execution_session(session_id))


@app.route('/api/execution/abort', methods=['POST'])
def abort_execution():
    data = request.json or {}
    session_id = (data.get('session_id') or '').strip()
    if not session_id:
        return jsonify({'success': False, 'message': '缺少 session_id'}), 400
    return jsonify(manager.abort_execution_session(session_id))


@app.route('/api/execution/status')
def execution_status():
    session_id = (request.args.get('session_id') or '').strip()
    conversation_id = (request.args.get('conversation_id') or '').strip()
    if not session_id and conversation_id:
        session_id = execution_sessions_by_conversation.get(conversation_id, '')
    if not session_id:
        return jsonify({'success': False, 'message': '缺少 session_id'}), 400
    return jsonify(manager.get_execution_session(session_id))


@app.route('/api/cancel_task', methods=['POST'])
def cancel_task():
    data = request.json or {}
    request_id = (data.get('request_id') or '').strip()
    conversation_id = (data.get('conversation_id') or '').strip()
    if not request_id:
        return jsonify({'success': False, 'message': '缺少 request_id'}), 400

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
    return jsonify({'success': True, 'request_id': request_id})

# 获取执行日志
@app.route('/api/get_logs')
def get_logs():
    logs = manager.get_execution_log()
    return jsonify({'logs': logs})

# 获取结构化工作流事件
@app.route('/api/workflow_events')
def get_workflow_events():
    conversation_id = request.args.get('conversation_id', '')
    if conversation_id:
        convo = conversation_manager.get_conversation(conversation_id) or {}
        return jsonify({'workflow_events': convo.get('workflow_events', [])})
    return jsonify({'workflow_events': manager.get_workflow_events()})


@app.route('/api/workflow_stream')
def workflow_stream():
    conversation_id = request.args.get('conversation_id', '')
    if not conversation_id:
        return jsonify({'success': False, 'message': 'missing conversation_id'}), 400

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
    return Response(stream(), mimetype='text/event-stream', headers=headers)

# 新建会话
@app.route('/api/conversations', methods=['POST'])
def create_conversation():
    data = request.json or {}
    title = data.get('title', '新会话')
    conversation = conversation_manager.create_conversation(title)
    return jsonify({'conversation': conversation})

# 会话列表：默认与 ?archived=false 仅返回未归档；?archived=true 仅已归档
@app.route('/api/conversations')
def list_conversations():
    archived = request.args.get('archived')
    if archived == 'true':
        archived_bool = True
    elif archived == 'false':
        archived_bool = False
    else:
        archived_bool = None
    conversations = conversation_manager.list_conversations(archived_bool)
    return jsonify({'conversations': conversations})

# 会话详情
@app.route('/api/conversations/<conversation_id>')
def get_conversation(conversation_id):
    conversation = conversation_manager.get_conversation(conversation_id)
    if not conversation:
        return jsonify({'conversation': None, 'success': False}), 404
    return jsonify({'conversation': conversation, 'success': True})

# 删除会话
@app.route('/api/conversations/<conversation_id>', methods=['DELETE'])
def delete_conversation(conversation_id):
    success = conversation_manager.delete_conversation(conversation_id)
    return jsonify({'success': success})

# 搜索方法论
@app.route('/api/search_methodology', methods=['POST'])
def search_methodology():
    data = request.json
    keyword = data.get('keyword', '')
    results = methodology_manager.search_methodologies(keyword)
    return jsonify({'results': results})

# 获取方法论列表
@app.route('/api/get_methodologies')
def get_methodologies():
    methodologies = methodology_manager.get_all_methodologies()
    return jsonify({'methodologies': methodologies})

# 删除方法论
@app.route('/api/delete_methodology', methods=['POST'])
def delete_methodology():
    data = request.json
    methodology_id = data.get('methodology_id')
    success = methodology_manager.delete_methodology(methodology_id)
    return jsonify({'success': success})

@app.route('/api/delete_methodologies_batch', methods=['POST'])
def delete_methodologies_batch():
    data = request.json or {}
    methodology_ids = data.get('methodology_ids', [])
    result = methodology_manager.delete_methodologies_batch(methodology_ids if isinstance(methodology_ids, list) else [])
    return jsonify(result)

# 更新方法论分类（同时记录人工分类反馈）
@app.route('/api/update_methodology_category', methods=['POST'])
def update_methodology_category():
    data = request.json or {}
    methodology_id = data.get('methodology_id', '')
    category = data.get('category', '')
    if not methodology_id:
        return jsonify({'success': False, 'message': '缺少methodology_id'}), 400
    updated = methodology_manager.update_methodology_category(methodology_id, category)
    return jsonify({'success': bool(updated), 'methodology': updated})

# 创建方法论
@app.route('/api/create_methodology', methods=['POST'])
def create_methodology():
    data = request.json
    scene = data.get('scene')
    keywords = data.get('keywords', [])
    solve_steps = data.get('solve_steps', [])
    methodology = methodology_manager.add_methodology(scene, keywords, solve_steps)
    return jsonify({'methodology_id': methodology['method_id']})

# 获取方法论详情
@app.route('/api/get_methodology', methods=['POST'])
def get_methodology():
    data = request.json
    methodology_id = data.get('methodology_id')
    methodology = methodology_manager.get_methodology_by_id(methodology_id)
    return jsonify({'methodology': methodology})

# 获取记忆状态
@app.route('/api/get_memory_status')
def get_memory_status():
    memory_status = {
        'short_term': {
            'task_id': manager.stm.task_id,
            'user_input': manager.stm.user_input,
            'current_step': manager.stm.current_step,
            'sub_tasks_count': len(manager.stm.sub_tasks),
            'agents_count': len(manager.stm.agent_status),
            'results_count': len(manager.stm.results),
            'logs_count': len(manager.stm.logs)
        },
        'mid_term': {
            'task_templates_count': len(manager.mtm.task_templates),
            'agent_combinations_count': len(manager.mtm.agent_combinations),
            'last_task_flow_count': len(manager.mtm.last_task_flow),
            'common_prompts_count': len(manager.mtm.common_prompts)
        },
        'long_term': {
            'methodologies_count': len(manager.ltm.methodologies),
            'best_cases_count': len(manager.ltm.best_cases),
            'knowledge_base_count': len(manager.ltm.knowledge_base)
        }
    }
    return jsonify({'memory_status': memory_status})

if __name__ == '__main__':
    # 创建templates目录
    if not os.path.exists('templates'):
        os.makedirs('templates')
    
    # 启动应用
    app.run(debug=True, host='0.0.0.0', port=5000)