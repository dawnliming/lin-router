from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    DEFAULT_AUTO_MODEL_NAME,
    DEFAULT_BASE_URL,
    DEFAULT_PUBLIC_API_KEY,
    PROVIDER_ARK,
    PROVIDER_RELAY,
    new_aggregate_route_key,
    new_route_key,
)
from .models import AggregateMember, AggregateModel, ConnectionGroup, ModelConfig


class ConfigStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.groups: List[ConnectionGroup] = []
        self.models: List[ModelConfig] = []
        self.aggregate_models: List[AggregateModel] = []
        self.aggregate_members: List[AggregateMember] = []
        self.aggregate_member_revisions: Dict[str, int] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.groups = []
            self.models = []
            self.aggregate_models = []
            self.aggregate_members = []
            self.save()
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            self.groups = []
            self.models = []
            self.aggregate_models = []
            self.aggregate_members = []
            return

        if not isinstance(raw, dict):
            self.groups = []
            self.models = []
            self.aggregate_models = []
            self.aggregate_members = []
            return

        groups_raw = raw.get("groups", [])
        models_raw = raw.get("models", [])
        aggregates_raw = raw.get("aggregate_models", [])
        members_raw = raw.get("aggregate_members", [])
        revisions_raw = raw.get("aggregate_member_revisions", {})
        if isinstance(groups_raw, list):
            self.groups = [ConnectionGroup.from_dict(x) for x in groups_raw if isinstance(x, dict)]
        else:
            self.groups = []
        changed = False
        for group in self.groups:
            if not group.route_key:
                group.route_key = new_route_key()
                changed = True
            if not group.provider_type:
                group.provider_type = PROVIDER_ARK
                changed = True

        if not isinstance(models_raw, list):
            if changed:
                self.save()
            self.models = []
            return

        legacy_models = [ModelConfig.from_dict(x) for x in models_raw if isinstance(x, dict)]
        if not self.groups and legacy_models:
            group_map: Dict[tuple[str, str], ConnectionGroup] = {}
            migrated_models: List[ModelConfig] = []
            for model in legacy_models:
                legacy = next((x for x in models_raw if isinstance(x, dict) and x.get("id") == model.id), {})
                base_url = str(legacy.get("base_url") or DEFAULT_BASE_URL)
                api_key = str(legacy.get("ark_api_key") or "")
                key = (base_url, api_key)
                group = group_map.get(key)
                if group is None:
                    group = ConnectionGroup(
                        id=uuid.uuid4().hex,
                        name=f"{base_url} · {len(group_map) + 1}",
                        base_url=base_url,
                        provider_type=PROVIDER_ARK,
                        ark_api_key=api_key,
                        route_key=new_route_key(),
                    )
                    group_map[key] = group
                model.group_id = group.id
                migrated_models.append(model)
            self.groups = list(group_map.values())
            self.models = migrated_models
            self.save()
            return

        self.models = legacy_models
        if self.groups:
            group_ids = {g.id for g in self.groups}
            for model in self.models:
                if not model.group_id or model.group_id not in group_ids:
                    model.group_id = self.groups[0].id
                    changed = True

        self.aggregate_models = [AggregateModel.from_dict(x) for x in aggregates_raw if isinstance(x, dict)] if isinstance(aggregates_raw, list) else []
        self.aggregate_members = [AggregateMember.from_dict(x) for x in members_raw if isinstance(x, dict)] if isinstance(members_raw, list) else []
        self.aggregate_member_revisions = {
            str(aggregate_id): max(0, int(revision or 0))
            for aggregate_id, revision in revisions_raw.items()
        } if isinstance(revisions_raw, dict) else {}
        for aggregate in self.aggregate_models:
            self.aggregate_member_revisions.setdefault(aggregate.id, 0)
        # 旧配置升级：为没有 route_key 的聚合模型自动生成
        for agg in self.aggregate_models:
            if not str(agg.route_key or "").strip():
                agg.route_key = new_aggregate_route_key()
                changed = True
        # 清理 orphan 成员
        if self._cleanup_orphan_members():
            changed = True
        if changed:
            self.save()

    def save(self) -> None:
        with self._lock:
            # 所有配置保存入口都收口历史策略，避免其他字段保存时把旧值重新写回。
            for aggregate in self.aggregate_models:
                aggregate.strategy = AggregateModel.normalize_strategy(aggregate.strategy)
            payload = {
                "groups": [asdict(g) for g in self.groups],
                "models": [asdict(m) for m in self.models],
                "aggregate_models": [asdict(m) for m in self.aggregate_models],
                "aggregate_members": [asdict(m) for m in self.aggregate_members],
                "aggregate_member_revisions": self.aggregate_member_revisions,
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)

    def _cleanup_orphan_members(self) -> bool:
        """删除引用不存在 group/model 的聚合成员。"""
        with self._lock:
            group_ids = {g.id for g in self.groups}
            model_ids = {m.id for m in self.models}
            before = len(self.aggregate_members)
            self.aggregate_members = [
                member for member in self.aggregate_members
                if member.aggregate_id in {a.id for a in self.aggregate_models}
                and member.group_id in group_ids
                and member.model_id in model_ids
            ]
            return len(self.aggregate_members) != before

    def find_aggregate(self, aggregate_id: str) -> Optional[AggregateModel]:
        return next((a for a in self.aggregate_models if a.id == aggregate_id), None)

    def find_aggregate_by_name(self, name: str) -> Optional[AggregateModel]:
        return next((a for a in self.aggregate_models if a.name == name), None)

    def find_aggregate_by_route_key(self, route_key: str) -> Optional[AggregateModel]:
        return next((a for a in self.aggregate_models if a.route_key == route_key), None)

    def get_aggregate_members(self, aggregate_id: str) -> List[AggregateMember]:
        return [m for m in self.aggregate_members if m.aggregate_id == aggregate_id]

    def aggregate_member_revision(self, aggregate_id: str) -> int:
        with self._lock:
            return int(self.aggregate_member_revisions.get(aggregate_id, 0))

    def _touch_aggregate_member_revision(self, aggregate_id: str) -> int:
        revision = self.aggregate_member_revision(aggregate_id) + 1
        self.aggregate_member_revisions[aggregate_id] = revision
        return revision

    def find_aggregate_member(self, member_id: str) -> Optional[AggregateMember]:
        return next((m for m in self.aggregate_members if m.id == member_id), None)

    def _validate_aggregate_name(self, name: str, exclude_id: Optional[str] = None) -> Tuple[bool, str]:
        name = str(name or "").strip()
        if not name:
            return False, "聚合模型名不能为空"
        if name in {DEFAULT_AUTO_MODEL_NAME, "all-router-auto"}:
            return False, f"聚合模型名不能为保留名 {name}"
        for group in self.groups:
            if name == self._group_auto_model_name_static(group):
                return False, f"聚合模型名与连接组自动路由模型名冲突: {name}"
        for model in self.models:
            if name in {model.id, model.name, model.ep_id}:
                return False, f"聚合模型名与真实模型冲突: {name}"
        for agg in self.aggregate_models:
            if agg.id != exclude_id and agg.name == name:
                return False, f"聚合模型名已存在: {name}"
        return True, ""

    def _validate_aggregate_route_key(
        self, route_key: str, exclude_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        route_key = str(route_key or "").strip()
        if not route_key:
            return False, "聚合模型 Key 不能为空"
        if route_key == DEFAULT_PUBLIC_API_KEY:
            return False, f"聚合模型 Key 不能为保留 Key {DEFAULT_PUBLIC_API_KEY}"
        for group in self.groups:
            if group.route_key == route_key:
                return False, "聚合模型 Key 与连接组 Key 冲突"
        for agg in self.aggregate_models:
            if agg.id != exclude_id and agg.route_key == route_key:
                return False, "聚合模型 Key 已存在"
        return True, ""

    def _validate_aggregate_client_aliases(self, aliases: List[str]) -> Tuple[bool, str]:
        reserved = {DEFAULT_AUTO_MODEL_NAME, "all-router-auto", DEFAULT_PUBLIC_API_KEY}
        for alias in aliases:
            if not alias:
                return False, "客户端公开模型别名不能为空"
            if alias in reserved:
                return False, f"客户端公开模型别名不能为保留名 {alias}"
        return True, ""

    @staticmethod
    def _group_auto_model_name_static(group: ConnectionGroup) -> str:
        if group and group.auto_model_name and group.auto_model_name.strip():
            return group.auto_model_name.strip()
        return DEFAULT_AUTO_MODEL_NAME

    def upsert_aggregate(self, aggregate: AggregateModel) -> Tuple[bool, str]:
        with self._lock:
            aggregate.strategy = AggregateModel.normalize_strategy(aggregate.strategy)
            aggregate.client_model_aliases = AggregateModel._normalize_client_model_aliases(aggregate.client_model_aliases)
            ok, msg = self._validate_aggregate_name(aggregate.name, aggregate.id)
            if not ok:
                return False, msg
            ok, msg = self._validate_aggregate_client_aliases(aggregate.client_model_aliases)
            if not ok:
                return False, msg
            # 新建或更新时若 route_key 为空，自动生成 lr-ag- 前缀 Key
            if not str(aggregate.route_key or "").strip():
                for _ in range(100):
                    aggregate.route_key = new_aggregate_route_key()
                    ok, _ = self._validate_aggregate_route_key(aggregate.route_key, aggregate.id)
                    if ok:
                        break
            ok, msg = self._validate_aggregate_route_key(aggregate.route_key, aggregate.id)
            if not ok:
                return False, msg
            now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            aggregate.updated_at = now
            existing = self.find_aggregate(aggregate.id)
            if existing:
                aggregate.created_at = existing.created_at or now
                for idx, item in enumerate(self.aggregate_models):
                    if item.id == aggregate.id:
                        self.aggregate_models[idx] = aggregate
                        break
            else:
                aggregate.created_at = now
                self.aggregate_models.append(aggregate)
                self.aggregate_member_revisions.setdefault(aggregate.id, 0)
            self.save()
            return True, ""

    def remove_aggregate(self, aggregate_id: str) -> Tuple[bool, int]:
        with self._lock:
            before = len(self.aggregate_models)
            self.aggregate_models = [a for a in self.aggregate_models if a.id != aggregate_id]
            removed_models = len(self.aggregate_models) != before
            before_members = len(self.aggregate_members)
            self.aggregate_members = [m for m in self.aggregate_members if m.aggregate_id != aggregate_id]
            removed_members_count = before_members - len(self.aggregate_members)
            if removed_models or removed_members_count:
                self.aggregate_member_revisions.pop(aggregate_id, None)
                self.save()
            return removed_models, removed_members_count

    def upsert_aggregate_member(self, member: AggregateMember) -> Tuple[bool, str]:
        with self._lock:
            if not self.find_aggregate(member.aggregate_id):
                return False, "聚合模型不存在"
            group = self.find_group(member.group_id)
            if not group:
                return False, "连接组不存在"
            if group.provider_type != PROVIDER_RELAY:
                return False, "聚合成员只能来自 relay 连接组"
            if not self.find_model(member.model_id):
                return False, "模型不存在"
            existing = next((m for m in self.aggregate_members if m.id == member.id), None)
            # 手动启用聚合成员时自动清冷却：避免 checkbox 启用后仍被 _aggregate_member_usable 跳过
            if existing and member.enabled:
                now_ts = int(time.time())
                if not existing.enabled or (existing.cooldown_until and existing.cooldown_until > now_ts):
                    member.cooldown_until = 0
                    member.cooldown_reason = ""
                    member.last_error = ""
                    member.last_checked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            # 同一聚合模型内 (group_id, model_id) 不重复
            duplicate = next(
                (m for m in self.aggregate_members
                 if m.aggregate_id == member.aggregate_id
                 and m.group_id == member.group_id
                 and m.model_id == member.model_id
                 and m.id != member.id),
                None,
            )
            if duplicate:
                return False, "该连接组/模型组合已存在于当前聚合模型"
            if existing:
                for idx, item in enumerate(self.aggregate_members):
                    if item.id == member.id:
                        self.aggregate_members[idx] = member
                        break
            else:
                # 新成员默认放在末尾
                if member.priority == 0:
                    siblings = self.get_aggregate_members(member.aggregate_id)
                    max_priority = max((m.priority for m in siblings), default=0)
                    member.priority = max_priority + 1
                self.aggregate_members.append(member)
            self._touch_aggregate_member_revision(member.aggregate_id)
            self.save()
            return True, ""

    def batch_add_aggregate_members(
        self,
        aggregate_id: str,
        group_id: str,
        model_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """按连接组一次性添加聚合成员，并在一次保存中完成变更。

        这里不接受客户端提交的完整模型配置，而是在锁内重新读取当前 Store
        中的聚合、连接组和模型。这样批量操作不会绕过 relay、可用状态或重复
        成员校验，也不会因前端并发发起多个请求而留下半截顺序。
        """

        def detail(
            model_id: str,
            *,
            model_name: str = "",
            code: str,
            reason: str,
            member_id: str = "",
            priority: Optional[int] = None,
        ) -> Dict[str, Any]:
            item: Dict[str, Any] = {
                "model_id": model_id,
                "model_name": model_name,
                "code": code,
                "reason": reason,
            }
            if member_id:
                item["member_id"] = member_id
            if priority is not None:
                item["priority"] = priority
            return item

        def result(
            *,
            ok: bool,
            message: str,
            code: str = "",
            added: Optional[List[Dict[str, Any]]] = None,
            skipped: Optional[List[Dict[str, Any]]] = None,
            failed: Optional[List[Dict[str, Any]]] = None,
            revision: int = 0,
            members: Optional[List[AggregateMember]] = None,
        ) -> Dict[str, Any]:
            added_items = added or []
            skipped_items = skipped or []
            failed_items = failed or []
            payload: Dict[str, Any] = {
                "ok": ok,
                "message": message,
                "added_count": len(added_items),
                "skipped_count": len(skipped_items),
                "failed_count": len(failed_items),
                "counts": {
                    "added": len(added_items),
                    "skipped": len(skipped_items),
                    "failed": len(failed_items),
                },
                "summary": {
                    "added": len(added_items),
                    "skipped": len(skipped_items),
                    "failed": len(failed_items),
                },
                "added": added_items,
                "skipped": skipped_items,
                "failed": failed_items,
                "revision": revision,
                "members": [asdict(member) for member in (members or [])],
            }
            if code:
                payload["code"] = code
            return payload

        with self._lock:
            # 批量请求必须以当前内存中的配置为准，避免客户端传入过期或伪造模型字段。
            aggregate = self.find_aggregate(str(aggregate_id or "").strip())
            if not aggregate:
                return result(ok=False, message="聚合模型不存在", code="aggregate_not_found")
            group = self.find_group(str(group_id or "").strip())
            if not group:
                return result(ok=False, message="连接组不存在", code="group_not_found")
            if group.provider_type != PROVIDER_RELAY:
                return result(
                    ok=False,
                    message="聚合成员只能来自 relay 连接组",
                    code="aggregate_group_not_relay",
                )

            # 显式选择时保留请求顺序的唯一项；重复选择本身作为 skipped 返回，便于前端解释结果。
            requested_ids: Optional[List[str]] = None
            duplicate_requested: List[str] = []
            if model_ids is not None:
                requested_ids = []
                seen_requested: set[str] = set()
                for raw_model_id in model_ids:
                    model_id = str(raw_model_id or "").strip()
                    if not model_id:
                        continue
                    if model_id in seen_requested:
                        duplicate_requested.append(model_id)
                        continue
                    seen_requested.add(model_id)
                    requested_ids.append(model_id)

            all_models = [model for model in self.models if model.group_id == group.id]
            models_by_id = {model.id: model for model in self.models}
            existing_keys = {
                (member.group_id, member.model_id)
                for member in self.aggregate_members
                if member.aggregate_id == aggregate.id
            }
            added: List[Dict[str, Any]] = []
            skipped: List[Dict[str, Any]] = []
            failed: List[Dict[str, Any]] = []
            candidates: List[ModelConfig] = []

            if requested_ids is None:
                selected_models = all_models
            else:
                requested_set = set(requested_ids)
                # 以配置中的模型顺序为准，确保 priority 追加顺序稳定。
                selected_models = [model for model in all_models if model.id in requested_set]
                selected_ids = {model.id for model in selected_models}
                for model_id in requested_ids:
                    if model_id not in models_by_id:
                        failed.append(detail(model_id, code="model_not_found", reason="模型不存在"))
                    elif model_id not in selected_ids:
                        model = models_by_id[model_id]
                        failed.append(
                            detail(
                                model_id,
                                model_name=model.name,
                                code="model_not_in_group",
                                reason="模型不属于所选连接组",
                            )
                        )
                for model_id in duplicate_requested:
                    model = models_by_id.get(model_id)
                    skipped.append(
                        detail(
                            model_id,
                            model_name=model.name if model else "",
                            code="duplicate_request",
                            reason="请求中重复选择，已跳过重复项",
                        )
                    )

            for model in selected_models:
                key = (group.id, model.id)
                if key in existing_keys:
                    skipped.append(
                        detail(
                            model.id,
                            model_name=model.name,
                            code="member_exists",
                            reason="该连接组/模型组合已存在于当前聚合模型",
                        )
                    )
                    continue
                if model.usable is not True:
                    failed.append(
                        detail(
                            model.id,
                            model_name=model.name,
                            code="model_unusable",
                            reason="模型当前不可用，未加入聚合模型",
                        )
                    )
                    continue
                candidates.append(model)

            previous_members = list(self.aggregate_members)
            previous_revision_present = aggregate.id in self.aggregate_member_revisions
            previous_revision = self.aggregate_member_revision(aggregate.id)
            max_priority = max(
                (member.priority for member in self.aggregate_members if member.aggregate_id == aggregate.id),
                default=0,
            )
            new_members: List[AggregateMember] = []
            for model in candidates:
                max_priority += 1
                member = AggregateMember(
                    id=uuid.uuid4().hex,
                    aggregate_id=aggregate.id,
                    group_id=group.id,
                    model_id=model.id,
                    priority=max_priority,
                )
                new_members.append(member)
                added.append(
                    detail(
                        model.id,
                        model_name=model.name,
                        code="added",
                        reason="已添加到聚合模型",
                        member_id=member.id,
                        priority=member.priority,
                    )
                )

            revision = previous_revision
            if new_members:
                # 先更新内存，再一次性落盘；落盘失败时恢复两个状态，避免半截批量结果。
                self.aggregate_members.extend(new_members)
                revision = self._touch_aggregate_member_revision(aggregate.id)
                try:
                    self.save()
                except Exception:
                    self.aggregate_members = previous_members
                    if previous_revision_present:
                        self.aggregate_member_revisions[aggregate.id] = previous_revision
                    else:
                        self.aggregate_member_revisions.pop(aggregate.id, None)
                    failed.extend(
                        detail(
                            model.id,
                            model_name=model.name,
                            code="config_save_failed",
                            reason="保存批量成员失败，已回滚本次变更",
                        )
                        for model in candidates
                    )
                    added = []
                    revision = previous_revision
                    members = sorted(previous_members, key=lambda member: member.priority)
                    return result(
                        ok=False,
                        message="保存批量成员失败，本次变更已回滚",
                        code="config_save_failed",
                        added=added,
                        skipped=skipped,
                        failed=failed,
                        revision=revision,
                        members=members,
                    )

            members = sorted(self.get_aggregate_members(aggregate.id), key=lambda member: member.priority)
            if added:
                message = f"已添加 {len(added)} 个模型"
                if skipped:
                    message += f"，跳过 {len(skipped)} 个重复项"
                if failed:
                    message += f"，{len(failed)} 个模型不可添加"
            elif skipped and not failed:
                message = "所选模型已全部存在，未新增成员"
            elif failed and not skipped:
                message = "没有可添加的模型"
            elif not all_models:
                message = "该连接组没有可添加的模型"
            else:
                message = "没有可添加的模型"
            return result(
                ok=True,
                message=message,
                added=added,
                skipped=skipped,
                failed=failed,
                revision=revision,
                members=members,
            )

    def remove_aggregate_member(self, member_id: str) -> bool:
        with self._lock:
            member = self.find_aggregate_member(member_id)
            before = len(self.aggregate_members)
            self.aggregate_members = [m for m in self.aggregate_members if m.id != member_id]
            changed = len(self.aggregate_members) != before
            if changed:
                if member:
                    self._touch_aggregate_member_revision(member.aggregate_id)
                self.save()
            return changed

    def move_aggregate_member(self, member_id: str, direction: str) -> bool:
        with self._lock:
            member = next((m for m in self.aggregate_members if m.id == member_id), None)
            if not member:
                return False
            siblings = sorted(
                [m for m in self.aggregate_members if m.aggregate_id == member.aggregate_id],
                key=lambda m: m.priority,
            )
            idx = next((i for i, m in enumerate(siblings) if m.id == member_id), -1)
            if idx < 0:
                return False
            if direction == "up":
                new_idx = idx - 1
            elif direction == "down":
                new_idx = idx + 1
            elif direction == "top":
                new_idx = 0
            elif direction == "bottom":
                new_idx = len(siblings) - 1
            else:
                return False
            if new_idx < 0 or new_idx >= len(siblings) or new_idx == idx:
                return True
            siblings[idx], siblings[new_idx] = siblings[new_idx], siblings[idx]
            for i, m in enumerate(siblings):
                m.priority = i + 1
            self._touch_aggregate_member_revision(member.aggregate_id)
            self.save()
            return True

    def reorder_aggregate_members(self, aggregate_id: str, member_ids: List[str], expected_revision: Optional[int] = None) -> Tuple[bool, str, str, int]:
        """Atomically replace one aggregate's complete member order when its revision matches."""
        with self._lock:
            if not self.find_aggregate(aggregate_id):
                return False, "聚合模型不存在", "aggregate_not_found", 0
            current_revision = self.aggregate_member_revision(aggregate_id)
            if expected_revision is not None and expected_revision != current_revision:
                return False, "成员顺序已被其他操作更新，请刷新后重试", "aggregate_member_revision_conflict", current_revision
            siblings = sorted(self.get_aggregate_members(aggregate_id), key=lambda member: member.priority)
            expected_ids = [member.id for member in siblings]
            if len(member_ids) != len(expected_ids) or len(set(member_ids)) != len(member_ids):
                return False, "成员排序无效，必须包含当前聚合模型的全部且不重复成员", "invalid_member_order", current_revision
            if set(member_ids) != set(expected_ids):
                return False, "成员排序包含缺失或不属于当前聚合模型的成员", "invalid_member_order", current_revision
            by_id = {member.id: member for member in siblings}
            for priority, member_id in enumerate(member_ids, start=1):
                by_id[member_id].priority = priority
            revision = self._touch_aggregate_member_revision(aggregate_id)
            self.save()
            return True, "", "", revision

    def clear_aggregate_member_cooldown(self, member_id: str, now_str: Optional[str] = None) -> bool:
        with self._lock:
            member = next((m for m in self.aggregate_members if m.id == member_id), None)
            if not member:
                return False
            member.enabled = True
            member.cooldown_until = 0
            member.cooldown_reason = ""
            member.last_error = ""
            member.last_checked_at = now_str or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            self.save()
            return True

    def remove_members_for_group(self, group_id: str) -> int:
        with self._lock:
            before = len(self.aggregate_members)
            self.aggregate_members = [m for m in self.aggregate_members if m.group_id != group_id]
            removed = before - len(self.aggregate_members)
            if removed:
                self.save()
            return removed

    def remove_members_for_model(self, model_id: str) -> int:
        with self._lock:
            before = len(self.aggregate_members)
            self.aggregate_members = [m for m in self.aggregate_members if m.model_id != model_id]
            removed = before - len(self.aggregate_members)
            if removed:
                self.save()
            return removed

    def refresh_expired_cooldowns(self) -> bool:
        with self._lock:
            now = int(time.time())
            changed = False
            for model in self.models:
                if model.cooldown_until and model.cooldown_until <= now:
                    model.cooldown_until = 0
                    model.cooldown_reason = ""
                    if not model.disabled_by_user:
                        model.usable = True
                    model.last_error = ""
                    model.last_checked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                    changed = True
            for member in self.aggregate_members:
                if member.cooldown_until and member.cooldown_until <= now:
                    member.cooldown_until = 0
                    member.cooldown_reason = ""
                    member.last_error = ""
                    member.last_checked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                    changed = True
            if changed:
                self.save()
            return changed

    def upsert_group(self, group: ConnectionGroup) -> None:
        with self._lock:
            if not group.route_key:
                existing = self.find_group(group.id)
                group.route_key = existing.route_key if existing and existing.route_key else new_route_key()
            if not group.provider_type:
                group.provider_type = PROVIDER_ARK
            for idx, item in enumerate(self.groups):
                if item.id == group.id:
                    self.groups[idx] = group
                    self.save()
                    return
            self.groups.append(group)
            self.save()

    def upsert_model(self, model: ModelConfig) -> None:
        with self._lock:
            for idx, item in enumerate(self.models):
                if item.id == model.id:
                    self.models[idx] = model
                    self.save()
                    return
            self.models.append(model)
            self.save()

    def invalidate_group_verification(self, group_id: str) -> bool:
        """连接性配置变更后清除该组及聚合成员的历史成功证据。"""
        with self._lock:
            changed = False
            for model in self.models:
                if model.group_id != group_id:
                    continue
                if model.last_success_at or model.last_checked_at:
                    model.last_success_at = ""
                    model.last_checked_at = ""
                    model.last_error = ""
                    changed = True
            for member in self.aggregate_members:
                if member.group_id != group_id:
                    continue
                if member.last_success_at or member.last_checked_at:
                    member.last_success_at = ""
                    member.last_checked_at = ""
                    member.last_error = ""
                    changed = True
            if changed:
                self.save()
            return changed

    def invalidate_model_member_verification(self, model_id: str) -> bool:
        """模型上游字段变更时清除引用它的聚合成员验证证据。"""
        with self._lock:
            changed = False
            for member in self.aggregate_members:
                if member.model_id != model_id:
                    continue
                if member.last_success_at or member.last_checked_at:
                    member.last_success_at = ""
                    member.last_checked_at = ""
                    member.last_error = ""
                    changed = True
            if changed:
                self.save()
            return changed

    def remove_model(self, model_id: str) -> bool:
        with self._lock:
            before = len(self.models)
            self.models = [m for m in self.models if m.id != model_id]
            changed = len(self.models) != before
            if self.remove_members_for_model(model_id):
                changed = True
            if changed:
                self.save()
            return changed

    def move_model(self, model_id: str, direction: str) -> bool:
        with self._lock:
            idx = next((i for i, m in enumerate(self.models) if m.id == model_id), -1)
            if idx < 0:
                return False
            group_id = self.models[idx].group_id
            group_positions = [i for i, model in enumerate(self.models) if model.group_id == group_id]
            local_idx = next((i for i, pos in enumerate(group_positions) if pos == idx), -1)
            if local_idx < 0:
                return False
            group_models = [self.models[pos] for pos in group_positions]
            if direction == "up":
                new_local_idx = local_idx - 1
            elif direction == "down":
                new_local_idx = local_idx + 1
            elif direction == "bottom":
                new_local_idx = len(group_models) - 1
            elif direction == "top":
                new_local_idx = 0
            else:
                return False
            if new_local_idx < 0 or new_local_idx >= len(group_positions):
                return False
            if new_local_idx == local_idx:
                return True
            model = group_models.pop(local_idx)
            group_models.insert(new_local_idx, model)
            for pos, model in zip(group_positions, group_models):
                self.models[pos] = model
            self.save()
            return True

    def reset_usable(self) -> None:
        with self._lock:
            changed = False
            for model in self.models:
                if not model.usable or model.last_error or model.disabled_by_user:
                    model.usable = True
                    model.disabled_by_user = False
                    model.last_error = ""
                    model.cooldown_until = 0
                    model.cooldown_reason = ""
                    changed = True
            if changed:
                self.save()

    def toggle_group(self, group_id: str) -> bool:
        """切换指定连接组下所有模型的可用状态（组内全可用则全部禁用，否则全部启用）。"""
        with self._lock:
            group_models = [m for m in self.models if m.group_id == group_id]
            if not group_models:
                return False
            all_usable = all(m.usable and not m.disabled_by_user for m in group_models)
            changed = False
            for model in group_models:
                target = not all_usable
                if model.usable != target or model.disabled_by_user == target:
                    model.usable = target
                    model.disabled_by_user = not target
                    if target:
                        model.cooldown_until = 0
                        model.cooldown_reason = ""
                        model.last_error = ""
                    changed = True
            if changed:
                self.save()
            return changed

    def find_group(self, group_id: str) -> Optional[ConnectionGroup]:
        return next((g for g in self.groups if g.id == group_id), None)

    def find_group_by_route_key(self, route_key: str) -> Optional[ConnectionGroup]:
        return next((g for g in self.groups if g.route_key == route_key), None)

    def find_model(self, model_id: str) -> Optional[ModelConfig]:
        return next((m for m in self.models if m.id == model_id), None)

    def find_model_by_group_ep(self, group_id: str, ep_id: str) -> Optional[ModelConfig]:
        return next((m for m in self.models if m.group_id == group_id and m.ep_id == ep_id), None)

    def remove_group(self, group_id: str) -> Tuple[bool, int, int]:
        """删除连接组，级联删除组下模型和聚合成员。"""
        with self._lock:
            before_groups = len(self.groups)
            before_models = len(self.models)
            self.models = [m for m in self.models if m.group_id != group_id]
            self.groups = [g for g in self.groups if g.id != group_id]
            removed_models = before_models - len(self.models)
            removed_members = self.remove_members_for_group(group_id)
            group_removed = len(self.groups) != before_groups
            if group_removed or removed_models or removed_members:
                self.save()
            return group_removed, removed_models, removed_members
