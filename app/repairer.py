"""数据导入预览：在严格受限的修复算子下做有界搜索，产出最佳候选实例。

调用方提交 JSON Schema、待导入实例与校验次数上限，本模块：

1. 对原实例做一次完整校验；原实例合法时不作任何修改，直接返回 ``valid``；
2. 仅针对以下约束构造 *单点改动*（每个改动只触及一个 JSON Pointer）：

   - ``required``：缺失的必填属性在对应子 schema 上声明了 ``default`` 时，
     补入该默认值；没有默认值一律不补（不凭空捏造必填值）；
   - ``enum`` / ``const``：替换为枚举/常量取值（优先与现值同 JSON 类型者）；
   - ``minimum`` / ``maximum``：取边界本身（整型现值遇非整型边界时取最近的
     可通过整数 ``ceil``/``floor``）；
   - ``minLength`` / ``maxLength``：填充 / 截断到边界长度；

   其他约束（``type`` / ``pattern`` / ``format`` / ``oneOf`` …）**不构造改动**，
   但仍参与每次候选的完整校验——无法消除它们就不能标记成功。

3. 对多处错误做确定性的最佳优先有界搜索：每个候选都重新全量校验，按
   「剩余错误数 → 改动位置数 → 规范 JSON」固定顺序择优，候选按规范序列化
   去重；仅当某个候选 *全部* 错误清零才标记 ``repaired``。
4. 预算耗尽或不存在可行改动时，保留搜索过程中见到的最佳候选并明确标记
   ``unresolved``（``unresolved_reason`` 取 ``budget_exhausted`` /
   ``no_fix_available``）。

全程无随机源，相同输入必然得到相同结果。
"""

from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass
from typing import Any

from .minimizer import BudgetExhausted, get_at, pointer_from_path, set_at
from .mutator import (
    _brief,
    _canonical,
    _is_int,
    _is_num,
    _json_equal,
    _resolve_local_ref,
    _types_of,
)

# 修复状态。
STATUS_VALID = "valid"
STATUS_REPAIRED = "repaired"
STATUS_UNRESOLVED = "unresolved"

# 未修复原因。
REASON_BUDGET = "budget_exhausted"
REASON_NO_FIX = "no_fix_available"

# 找不到 default 时的哨兵（default 本身允许是 null，不能用 None 表示缺失）。
_NO_DEFAULT = object()


@dataclass
class Change:
    """单个 JSON Pointer 上的最终净改动。"""

    op: str  # "add"（补必填属性）/ "replace"（替换既有值）
    pointer: str
    keyword: str
    detail: str
    old: Any
    new: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "pointer": self.pointer,
            "keyword": self.keyword,
            "detail": self.detail,
            # add 没有旧值，统一以 null 占位（op 已区分语义）。
            "old": self.old,
            "new": self.new,
        }


@dataclass
class ErrorInfo:
    """剩余校验错误的可序列化视图。"""

    keyword: str
    instance_path: str
    schema_path: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "instance_path": self.instance_path,
            "schema_path": self.schema_path,
            "message": self.message,
        }


@dataclass
class RepairResult:
    status: str
    candidate_instance: Any
    original_instance: Any
    changes: list[Change]
    remaining_errors: list[ErrorInfo]
    evaluations: int
    stopped_by_budget: bool
    unresolved_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidate_instance": self.candidate_instance,
            "original_instance": self.original_instance,
            "changes": [c.to_dict() for c in self.changes],
            "remaining_errors": [e.to_dict() for e in self.remaining_errors],
            "evaluations": self.evaluations,
            "stopped_by_budget": self.stopped_by_budget,
            "unresolved_reason": self.unresolved_reason,
        }


@dataclass
class _Fix:
    """一次可应用的单点改动。"""

    pointer: str
    path: tuple
    op: str  # "add" / "replace"
    keyword: str
    detail: str
    new_value: Any
    old_value: Any = None  # add 时不使用

    def apply(self, root: Any) -> Any:
        if self.op == "add":
            parent_path = tuple(self.path[:-1])
            key = self.path[-1]
            parent = root if not parent_path else get_at(root, parent_path)
            return set_at(root, parent_path, {**parent, key: self.new_value})
        return set_at(root, self.path, self.new_value)


@dataclass
class _Node:
    """搜索节点：候选实例、累计净改动与本次完整校验的去重错误。"""

    instance: Any
    # pointer -> 迄今为止该位置上的净改动（同一位置被多次修复时合并）。
    changes: dict[str, Change]
    infos: list[ErrorInfo]
    raw_errors: list
    canonical: str = ""

    def __post_init__(self) -> None:
        if not self.canonical:
            self.canonical = _canonical(self.instance)

    @property
    def location_count(self) -> int:
        return len(self.changes)

    def key(self) -> tuple:
        # 固定择优顺序：剩余错误数升序 → 改动位置数升序 → 规范 JSON 升序。
        return (len(self.infos), len(self.changes), self.canonical)


class Repairer:
    """对单个 (schema, instance) 对执行有界修复搜索。"""

    def __init__(self, schema: Any, validator, instance: Any, max_evaluations: int):
        self.schema = schema
        self.validator = validator
        self.original = instance
        self.max_evaluations = max_evaluations
        self.evaluations = 0
        self.stopped_by_budget = False

    # ---- 校验 / 预算 -----------------------------------------------------

    def _validate(self, instance: Any) -> tuple[list[ErrorInfo], list]:
        """执行一次计预算的完整校验，返回 (去重错误视图, 去重原始错误)。

        预算耗尽时把 ``stopped_by_budget`` 置位并抛 :class:`BudgetExhausted`；
        校验器自身无法运行（坏 ``$ref``、非法正则等）时透传底层异常。
        """
        if self.evaluations >= self.max_evaluations:
            self.stopped_by_budget = True
            raise BudgetExhausted()
        self.evaluations += 1
        infos: list[ErrorInfo] = []
        raw: list = []
        seen: set = set()
        for err in self.validator.iter_errors(instance):
            # allOf 等通道可能逐字重复同一错误：按 (关键字, 实例位置, 信息) 去重。
            dedup_key = (err.validator, tuple(err.absolute_path), err.message)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            raw.append(err)
            infos.append(
                ErrorInfo(
                    keyword=err.validator,
                    instance_path=pointer_from_path(tuple(err.absolute_path)),
                    schema_path=pointer_from_path(tuple(err.absolute_schema_path)),
                    message=err.message,
                )
            )
        return infos, raw

    # ---- 主流程 -----------------------------------------------------------

    def run(self) -> RepairResult:
        # 第 1 次评估：确认原实例状态。合法实例不作任何修改（异常透传给接口层）。
        infos, raw = self._validate(self.original)
        if not infos:
            return self._result(STATUS_VALID, self.original, [], [], None)

        root = _Node(self.original, {}, infos, raw)
        best = root
        visited: set[str] = {root.canonical}
        # 堆元素：(择优键, 确定性序号, 节点)。
        tie = 0
        heap: list = [(root.key(), tie, root)]

        while heap:
            _, _, node = heapq.heappop(heap)

            # 已有全通过候选时的剪枝（位置数沿任意搜索路径单调不减）：
            # - 当前节点改动位置数已多于最佳解：其任何后代只会更多，不可能更优；
            #   按堆序（错误数 → 位置数）它后面的开放节点同样不优于它，停止；
            # - 当前节点本身也是全通过候选（并列）：它的后代只会引入更多改动
            #   位置，故不展开，继续处理堆中其他并列节点即可。
            if not best.infos and node.location_count > best.location_count:
                break
            if not best.infos and node is not best:
                continue

            try:
                children = self._expand(node, visited)
            except BudgetExhausted:
                break
            for child in children:
                if child.key() < best.key():
                    best = child
                tie += 1
                heapq.heappush(heap, (child.key(), tie, child))

        if not best.infos:
            return self._result(
                STATUS_REPAIRED,
                best.instance,
                self._sorted_changes(best.changes),
                [],
                None,
            )
        reason = REASON_BUDGET if self.stopped_by_budget else REASON_NO_FIX
        return self._result(
            STATUS_UNRESOLVED,
            best.instance,
            self._sorted_changes(best.changes),
            best.infos,
            reason,
        )

    def _result(
        self,
        status: str,
        instance: Any,
        changes: list[Change],
        errors: list[ErrorInfo],
        reason: str | None,
    ) -> RepairResult:
        return RepairResult(
            status=status,
            candidate_instance=instance,
            original_instance=self.original,
            changes=changes,
            remaining_errors=errors,
            evaluations=self.evaluations,
            stopped_by_budget=self.stopped_by_budget,
            unresolved_reason=reason,
        )

    @staticmethod
    def _sorted_changes(changes: dict[str, Change]) -> list[Change]:
        # 输出按 JSON Pointer 固定排序，保证响应确定性。
        return [changes[p] for p in sorted(changes)]

    # ---- 候选扩展 ---------------------------------------------------------

    def _expand(self, node: _Node, visited: set[str]) -> list[_Node]:
        """基于节点当前错误构造全部可行单点改动，逐个重新全量校验。"""
        # default 映射随当前候选实例的结构收集（不额外消耗校验预算）。
        defaults = self._collect_defaults(node.instance)

        fixes: list[_Fix] = []
        seen_fix_keys: set = set()
        for err in node.raw_errors:
            for fix in self._fixes_for(err, defaults):
                # 改动值与现值相同（如 draft4 独占边界被误报）：不可能有进展，跳过。
                if fix.op == "replace" and _json_equal(
                    get_at(node.instance, fix.path), fix.new_value
                ):
                    continue
                dedup_key = (fix.pointer, fix.op, _canonical(fix.new_value))
                if dedup_key in seen_fix_keys:
                    continue
                seen_fix_keys.add(dedup_key)
                fixes.append(fix)

        children: list[_Node] = []
        for fix in fixes:
            try:
                candidate = fix.apply(node.instance)
            except (KeyError, IndexError, TypeError):
                continue  # 结构与实例不再匹配：防御性跳过。
            canon = _canonical(candidate)
            if canon in visited:
                continue
            try:
                infos, raw = self._validate(candidate)
            except BudgetExhausted:
                # 预算在本候选处耗尽：不再展开/入队任何后续候选。
                return children
            except Exception:
                # 校验器对该候选无法运行（如改动后触达坏 $ref）：跳过该候选。
                continue
            visited.add(canon)
            changes = self._merge_change(node.changes, fix)
            child = _Node(candidate, changes, infos, raw, canon)
            children.append(child)
        return children

    @staticmethod
    def _merge_change(changes: dict[str, Change], fix: _Fix) -> dict[str, Change]:
        merged = dict(changes)
        previous = merged.get(fix.pointer)
        if previous is None:
            merged[fix.pointer] = Change(
                op=fix.op,
                pointer=fix.pointer,
                keyword=fix.keyword,
                detail=fix.detail,
                old=None if fix.op == "add" else fix.old_value,
                new=fix.new_value,
            )
        else:
            # 同一位置被后续修复再次触及：保留相对原实例的净效果
            # （首次若是 add，最终仍是 add；old 保留最初的旧值/null）。
            merged[fix.pointer] = Change(
                op=previous.op,
                pointer=fix.pointer,
                keyword=fix.keyword,
                detail=fix.detail,
                old=previous.old,
                new=fix.new_value,
            )
        return merged

    # ---- 各关键字的单点改动构造 ------------------------------------------

    def _fixes_for(self, err, defaults: dict) -> list[_Fix]:
        """为一条校验错误构造候选单点改动（通常 1 个；enum 最多 2 个）。

        返回多个候选时按确定性优先级排列：与现值同 JSON 类型的枚举成员优先，
        其后补一个声明顺序上的首个成员；其余约束各自只给“最安全”的一个值。
        """
        keyword = err.validator
        path = tuple(err.absolute_path)
        pointer = pointer_from_path(path)

        if keyword == "required":
            fix = self._fix_required(err, path, defaults)
            return [fix] if fix is not None else []
        if keyword == "enum":
            return self._fixes_enum(err, path, pointer)
        if keyword == "const":
            return [self._fix_const(err, path, pointer)]
        if keyword == "minimum":
            fix = self._fix_bound(err, path, pointer, "minimum", math.ceil, ">=")
            return [fix] if fix is not None else []
        if keyword == "maximum":
            fix = self._fix_bound(err, path, pointer, "maximum", math.floor, "<=")
            return [fix] if fix is not None else []
        if keyword == "minLength":
            fix = self._fix_min_length(err, path, pointer)
            return [fix] if fix is not None else []
        if keyword == "maxLength":
            fix = self._fix_max_length(err, path, pointer)
            return [fix] if fix is not None else []
        # 其他约束不构造改动，但仍参与完整校验（错误保留在剩余列表中）。
        return []

    def _fix_required(self, err, parent_path: tuple, defaults: dict) -> _Fix | None:
        required = err.validator_value
        parent = err.instance
        if not isinstance(required, list) or not isinstance(parent, dict):
            return None
        missing = [k for k in required if isinstance(k, str) and k not in parent]
        if not missing:
            return None
        name = missing[0]
        key = (parent_path, name)
        if key not in defaults:
            return None  # 无 default：不凭空补必填值。
        default = defaults[key]
        path = parent_path + (name,)
        return _Fix(
            pointer=pointer_from_path(path),
            path=path,
            op="add",
            keyword="required",
            detail=f"add missing required property {name!r} with default {_brief(default)}",
            new_value=default,
        )

    def _fixes_enum(self, err, path: tuple, pointer: str) -> list[_Fix]:
        allowed = err.validator_value
        current = err.instance
        if not isinstance(allowed, list) or not allowed:
            return []

        def make(member: Any, note: str) -> _Fix:
            return _Fix(
                pointer=pointer,
                path=path,
                op="replace",
                keyword="enum",
                detail=f"replace {_brief(current)} with {_brief(member)} ({note})",
                new_value=member,
                old_value=current,
            )

        current_types = _types_of(current)
        same_type = [m for m in allowed if _types_of(m) & current_types]
        ordered: list[Any] = []
        for member in same_type:
            if not any(_json_equal(member, m) for m in ordered):
                ordered.append(member)
        # 至多再补一个非同型成员（声明顺序的第一个），保持分支数有界。
        for member in allowed:
            if len(ordered) >= 2:
                break
            if not any(_json_equal(member, m) for m in ordered):
                ordered.append(member)

        fixes: list[_Fix] = []
        for i, member in enumerate(ordered[:2]):
            note = "enum member, same type" if _types_of(member) & current_types else "enum member"
            fixes.append(make(member, note))
        return fixes

    def _fix_const(self, err, path: tuple, pointer: str) -> _Fix | None:
        value = err.validator_value
        current = err.instance
        return _Fix(
            pointer=pointer,
            path=path,
            op="replace",
            keyword="const",
            detail=f"set value to const {_brief(value)}",
            new_value=value,
            old_value=current,
        )

    def _fix_bound(
        self, err, path: tuple, pointer: str, keyword: str, nearest_int, relation: str
    ) -> _Fix | None:
        bound = err.validator_value
        current = err.instance
        if not _is_num(bound) or not _is_num(current):
            return None
        value: Any = bound
        # 整型现值遇非整型边界：取边界外（安全）一侧最近的可通过整数。
        if _is_int(current) and not float(bound).is_integer():
            value = nearest_int(bound)
        return _Fix(
            pointer=pointer,
            path=path,
            op="replace",
            keyword=keyword,
            detail=f"set to {value} ({relation} {keyword} {bound})",
            new_value=value,
            old_value=current,
        )

    def _fix_min_length(self, err, path: tuple, pointer: str) -> _Fix | None:
        bound = err.validator_value
        current = err.instance
        if not _is_int(bound) or not isinstance(current, str):
            return None
        pad = bound - len(current)  # 报错保证 pad > 0
        if pad <= 0:
            return None
        if current:
            # 重复末字符更可能保住 pattern 等其他约束；失败时相关约束仍会
            # 留在剩余错误里，不会凭空成功。
            value = current + current[-1] * pad
        else:
            value = "x" * bound
        return _Fix(
            pointer=pointer,
            path=path,
            op="replace",
            keyword="minLength",
            detail=f"pad string {len(current)} -> {len(value)} chars",
            new_value=value,
            old_value=current,
        )

    def _fix_max_length(self, err, path: tuple, pointer: str) -> _Fix | None:
        bound = err.validator_value
        current = err.instance
        if not _is_int(bound) or not isinstance(current, str) or len(current) <= bound:
            return None
        value = current[:bound]
        return _Fix(
            pointer=pointer,
            path=path,
            op="replace",
            keyword="maxLength",
            detail=f"truncate string {len(current)} -> {bound} chars",
            new_value=value,
            old_value=current,
        )

    # ---- default 收集（沿 schema applicator 与实例同步遍历）---------------

    def _collect_defaults(self, instance: Any) -> dict[tuple, Any]:
        """返回 ``{(对象路径元组, 属性名): default 值}``。

        只遍历 ``properties`` / ``patternProperties`` / ``additionalProperties``
        / ``items`` / ``prefixItems`` / ``allOf`` 与本地 ``$ref``（带环保护）；
        default 的查找跟随属性子 schema 的本地 ``$ref`` 与 ``allOf``。
        """
        mapping: dict[tuple, Any] = {}

        def walk(node: Any, inst: Any, path: tuple, visited: set) -> None:
            if not isinstance(node, dict):
                return
            ident = (id(node), path)
            if ident in visited:
                return
            visited.add(ident)

            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#"):
                resolved = _resolve_local_ref(self.schema, ref)
                if isinstance(resolved, dict):
                    walk(resolved, inst, path, visited)

            if isinstance(inst, dict):
                props = node.get("properties")
                if isinstance(props, dict):
                    for key, sub in props.items():
                        map_key = (path, key)
                        if map_key not in mapping:
                            default = _find_default(sub, self.schema)
                            if default is not _NO_DEFAULT:
                                mapping[map_key] = default

            self._walk_children(node, inst, path, visited, walk)

        walk(self.schema, instance, (), set())
        return mapping

    def _walk_children(self, node, inst, path, visited, walk) -> None:
        if isinstance(inst, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for key, sub in props.items():
                    if key in inst:
                        walk(sub, inst[key], path + (key,), visited)

            compiled_patterns: list[tuple[str, re.Pattern]] = []
            patterns = node.get("patternProperties")
            if isinstance(patterns, dict):
                for pattern, sub in patterns.items():
                    try:
                        rx = re.compile(pattern)
                    except re.error:
                        continue  # 非法正则：校验阶段同样无法运行，遍历跳过。
                    compiled_patterns.append((sub, rx))
                    for key in inst:
                        if rx.search(key):
                            walk(sub, inst[key], path + (key,), visited)

            additional = node.get("additionalProperties")
            if isinstance(additional, dict):
                declared = set(props) if isinstance(props, dict) else set()
                for key in inst:
                    if key in declared:
                        continue
                    if any(rx.search(key) for _, rx in compiled_patterns):
                        continue
                    walk(additional, inst[key], path + (key,), visited)

        if isinstance(inst, list):
            prefix = node.get("prefixItems")
            if isinstance(prefix, list):
                for i, sub in enumerate(prefix[: len(inst)]):
                    walk(sub, inst[i], path + (i,), visited)

            items = node.get("items")
            if isinstance(items, dict):
                # 2020-12：items 作用于 prefixItems 之后；更早 draft 作用于全部元素。
                start = len(prefix) if isinstance(prefix, list) else 0
                for i in range(start, len(inst)):
                    walk(items, inst[i], path + (i,), visited)
            elif isinstance(items, list):
                # 旧版 tuple form（draft 2019-09 及以前）。
                for i, sub in enumerate(items[: len(inst)]):
                    walk(sub, inst[i], path + (i,), visited)

        all_of = node.get("allOf")
        if isinstance(all_of, list):
            for sub in all_of:
                walk(sub, inst, path, visited)


def _find_default(sub: Any, root: Any, seen: set | None = None) -> Any:
    """在属性子 schema 中（跟随本地 ``$ref`` 与 ``allOf``，带环保护）找首个 default。"""
    if seen is None:
        seen = set()
    if not isinstance(sub, dict):
        return _NO_DEFAULT
    if id(sub) in seen:
        return _NO_DEFAULT
    seen.add(id(sub))
    if "default" in sub:
        return sub["default"]
    ref = sub.get("$ref")
    if isinstance(ref, str) and ref.startswith("#"):
        default = _find_default(_resolve_local_ref(root, ref), root, seen)
        if default is not _NO_DEFAULT:
            return default
    all_of = sub.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            default = _find_default(branch, root, seen)
            if default is not _NO_DEFAULT:
                return default
    return _NO_DEFAULT
