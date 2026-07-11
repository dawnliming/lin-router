from __future__ import annotations

from typing import Any, Callable, Dict, List

from .contracts import RequestLog


def diagnose_logs(logs: List[RequestLog], sanitize_detail: Callable[[str], str]) -> Dict[str, Any]:
    """Compute an explanation from completed records without changing runtime state."""
    text = "\n".join(f"{log.status} {log.event} {log.failure_scope} {log.detail}" for log in logs).lower()
    final = next(
        (log for log in reversed(logs) if str(log.status).startswith("2") or log.event in {"error", "network", "stream_idle_timeout", "waf_lock_timeout"}),
        logs[-1],
    )
    title, severity, root_cause = "请求已完成", "success", "request_completed"
    scope, suggestion = final.failure_scope or "request", "无需处理。"
    actions: List[Dict[str, str]] = []
    cooldown_applied = any(bool(log.cooldown_applied) for log in logs)
    if "waf_lock_wait_timeout" in text or "candidate_busy" in text or "large_task_in_progress" in text:
        title, severity, root_cause, scope = "候选忙 / 等待 WAF 锁超时", "warning", "candidate_busy", "local_lock"
        suggestion = "候选正在处理大上下文请求，系统已尝试切换到下一个候选；通常无需清冷却。"
    elif "stream_idle_timeout" in text:
        title, severity, root_cause, scope = "上游流式响应空闲超时", "error", "stream_idle_timeout", "upstream"
        suggestion = "建议稍后重试，或对冷却中的单个模型/成员执行“重试恢复”。"
        actions.append({"type": "recover", "label": "重试恢复冷却对象"})
    elif "read_timeout" in text or "timed out" in text or "timeout" in text and "waf_lock" not in text:
        title, severity, root_cause, scope = "上游请求超时", "error", "upstream_timeout", "upstream"
        suggestion = "如果该候选已进入冷却，可单点重试恢复；如果频繁出现，建议降低优先级或检查中转站。"
        actions.append({"type": "recover", "label": "重试恢复冷却对象"})
    elif "waf_blocked" in text or "request_level" in text or "upstream_request_rejected" in text:
        title, severity, root_cause, scope = "请求级错误 / 上游拒绝请求", "warning", "request_level_error", "request"
        cooldown_applied = False
        suggestion = "请检查请求参数、内容策略或 WAF 兼容设置；这类错误不会判定为模型健康失败。"
    elif "auth_error" in text or "401" in text or "403" in text:
        title, severity, root_cause, scope = "鉴权失败", "error", "auth_error", "candidate"
        suggestion = "请检查该连接组或模型的 API Key / Route Key 是否正确。"
    elif "rate_limit" in text or "429" in text:
        title, severity, root_cause, scope = "上游限流", "warning", "rate_limit", "upstream"
        suggestion = "建议稍后重试，或临时切换到其他候选。"
    elif "server_error" in text or " 5" in text or "network" in text:
        title, severity, root_cause, scope = "上游健康失败", "error", "upstream_error", "upstream"
        suggestion = "系统会对真实上游故障写入冷却；可在确认恢复后单点重试。"
        actions.append({"type": "recover", "label": "重试恢复冷却对象"})
    return {
        "title": title, "severity": severity, "root_cause": root_cause,
        "failure_scope": scope, "cooldown_applied": cooldown_applied,
        "suggestion": suggestion, "request_id": logs[0].request_id if logs else "",
        "related_events": len(logs), "actions": actions,
        "technical_summary": sanitize_detail(final.detail)[:500],
    }
