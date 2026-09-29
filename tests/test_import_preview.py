"""POST /import-preview 的端到端 / 单元测试。运行：python -m pytest -q"""

import json

from fastapi.testclient import TestClient

from app.main import app
from app.minimizer import build_validator
from app.repairer import (
    REASON_BUDGET,
    REASON_NO_FIX,
    STATUS_REPAIRED,
    STATUS_UNRESOLVED,
    STATUS_VALID,
    Repairer,
)

client = TestClient(app)


def post(schema, instance, **extra):
    body = {"schema": schema, "instance": instance}
    body.update(extra)
    return client.post("/import-preview", json=body)


def run_repair(schema, instance, max_evaluations=1000):
    validator = build_validator(schema)
    return Repairer(schema, validator, instance, max_evaluations).run()


# ---- HTTP 层 ---------------------------------------------------------------


def test_invalid_schema_returns_422():
    resp = post({"type": "not-a-type"}, {})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_schema"


def test_unusable_schema_bad_ref_returns_422():
    resp = post({"$ref": "#/definitions/missing"}, {"x": 1})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_schema"


def test_bad_max_evaluations_returns_422():
    resp = post({"type": "integer"}, "x", max_evaluations=0)
    assert resp.status_code == 422
    resp = post({"type": "integer"}, "x", max_evaluations=10**9)
    assert resp.status_code == 422


# ---- 原实例合法 ------------------------------------------------------------


def test_valid_instance_untouched():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
    data = post(schema, {"a": 1}).json()
    assert data["status"] == STATUS_VALID
    assert data["candidate_instance"] == {"a": 1}
    assert data["changes"] == []
    assert data["remaining_errors"] == []
    assert data["unresolved_reason"] is None
    # 首次校验即确认合法，只消耗 1 次评估。
    assert data["evaluations"] == 1
    assert data["stopped_by_budget"] is False


def test_boolean_true_schema_is_valid():
    data = post(True, "anything").json()
    assert data["status"] == STATUS_VALID


# ---- 单点修复 --------------------------------------------------------------


def test_required_missing_without_default_is_not_fabricated():
    schema = {"type": "object", "required": ["id"]}
    data = post(schema, {}).json()
    assert data["status"] == STATUS_UNRESOLVED
    assert data["unresolved_reason"] == REASON_NO_FIX
    assert data["candidate_instance"] == {}
    assert data["changes"] == []
    errs = data["remaining_errors"]
    assert [e["keyword"] for e in errs] == ["required"]
    assert errs[0]["instance_path"] == ""


def test_required_uses_declared_default():
    schema = {
        "type": "object",
        "required": ["role"],
        "properties": {"role": {"type": "string", "default": "guest"}},
    }
    data = post(schema, {}).json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == {"role": "guest"}
    (change,) = data["changes"]
    assert change["op"] == "add"
    assert change["pointer"] == "/role"
    assert change["keyword"] == "required"
    assert change["old"] is None
    assert change["new"] == "guest"
    assert data["remaining_errors"] == []


def test_required_default_through_ref():
    schema = {
        "type": "object",
        "required": ["meta"],
        "properties": {"meta": {"$ref": "#/$defs/Meta"}},
        "$defs": {
            "Meta": {
                "type": "object",
                "required": ["kind"],
                "properties": {"kind": {"type": "string", "default": "basic"}},
            }
        },
    }
    data = post(schema, {"meta": {}}).json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == {"meta": {"kind": "basic"}}
    assert data["changes"][0]["pointer"] == "/meta/kind"


def test_required_default_inside_array_items():
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "default": 0}},
        },
    }
    data = post(schema, [{"id": 3}, {}]).json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == [{"id": 3}, {"id": 0}]
    assert data["changes"][0]["pointer"] == "/1/id"


def test_enum_prefers_same_type_member():
    schema = {"enum": ["admin", 0, None]}
    data = post(schema, "wrong").json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == "admin"
    change = data["changes"][0]
    assert change["keyword"] == "enum"
    assert change["pointer"] == ""
    assert change["old"] == "wrong"
    assert change["new"] == "admin"


def test_const_fix():
    data = post({"const": 42}, 7).json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == 42
    assert data["changes"][0]["keyword"] == "const"


def test_minimum_and_maximum_integer_boundaries():
    data = post({"type": "integer", "minimum": 18}, 5).json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == 18

    data = post({"type": "integer", "maximum": 100}, 999).json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == 100


def test_integer_value_uses_nearest_int_for_fractional_bound():
    data = post({"type": "integer", "minimum": 2.5}, 1).json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == 3  # ceil(2.5)，保持整型

    data = post({"type": "integer", "maximum": 2.5}, 9).json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == 2  # floor(2.5)


def test_min_length_and_max_length():
    data = post({"type": "string", "minLength": 3}, "a").json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == "aaa"

    data = post({"type": "string", "maxLength": 3}, "abcdef").json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == "abc"


def test_min_length_empty_string_padded():
    data = post({"type": "string", "minLength": 2}, "").json()
    assert data["status"] == STATUS_REPAIRED
    assert len(data["candidate_instance"]) == 2


# ---- 多错误有界搜索 --------------------------------------------------------


MULTI_SCHEMA = {
    "type": "object",
    "required": ["role", "age", "name"],
    "properties": {
        "role": {"type": "string", "default": "guest"},
        "age": {"type": "integer", "minimum": 18},
        "name": {"type": "string", "maxLength": 3},
        "grade": {"enum": ["A", "B", "C"]},
    },
}
MULTI_INSTANCE = {"age": 5, "name": "alice", "grade": "Z"}


def test_multiple_errors_all_fixable():
    data = post(MULTI_SCHEMA, MULTI_INSTANCE).json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == {
        "role": "guest",
        "age": 18,
        "name": "ali",
        "grade": "A",
    }
    pointers = [ch["pointer"] for ch in data["changes"]]
    # 同一候选的改动按 JSON Pointer 固定顺序输出。
    assert pointers == ["/age", "/grade", "/name", "/role"]
    assert data["remaining_errors"] == []
    # 首次校验原实例后，每个候选各一次完整校验；总数确定（enum 错误最多产生
    # 两个候选，故根层 5 个候选、随后逐层减少）。
    assert data["evaluations"] == 13
    assert data["stopped_by_budget"] is False


def test_unfixable_error_blocks_success_but_keeps_best_candidate():
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {
            "a": {"type": "integer", "default": 1},
            "b": {"type": "string"},
        },
    }
    data = post(schema, {"b": 5}).json()
    assert data["status"] == STATUS_UNRESOLVED
    assert data["unresolved_reason"] == REASON_NO_FIX
    # 可修复的 required 仍然应用到最佳候选；type 错误保留为剩余错误。
    assert data["candidate_instance"] == {"a": 1, "b": 5}
    assert [e["keyword"] for e in data["remaining_errors"]] == ["type"]
    assert data["changes"][0]["pointer"] == "/a"


def test_enum_candidate_that_satisfies_other_constraint_is_found():
    # 短枚举成员会触发 minLength；搜索必须试到长成员才能成功。
    schema = {"type": "string", "enum": ["foo", "barbaz"], "minLength": 5}
    data = post(schema, "x").json()
    assert data["status"] == STATUS_REPAIRED
    assert data["candidate_instance"] == "barbaz"


def test_candidates_are_deduplicated_and_deterministic():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "integer", "minimum": 1},
            "b": {"type": "integer", "minimum": 1},
        },
    }
    body = {"schema": schema, "instance": {"a": 0, "b": 0}}
    d1 = client.post("/import-preview", json=body).json()
    d2 = client.post("/import-preview", json=body).json()
    assert d1 == d2
    assert d1["status"] == STATUS_REPAIRED
    assert d1["candidate_instance"] == {"a": 1, "b": 1}


def test_best_candidate_ranked_by_error_count_then_locations():
    # 两处独立错误；预算只允许探索部分候选时，最佳候选仍应是错误最少者。
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "integer", "minimum": 1},
            "b": {"type": "integer", "minimum": 1},
        },
    }
    # 预算 3：首次校验 + 恰好两个单点候选（各修一处）。
    data = post(schema, {"a": 0, "b": 0}, max_evaluations=3).json()
    assert data["status"] == STATUS_UNRESOLVED
    assert data["unresolved_reason"] == REASON_BUDGET
    assert data["stopped_by_budget"] is True
    # 单点候选只剩 1 个错误，优于原实例的 2 个。
    assert len(data["remaining_errors"]) == 1
    assert len(data["changes"]) == 1


def test_budget_one_only_validates_original():
    schema = {"type": "integer", "minimum": 1}
    data = post(schema, 0, max_evaluations=1).json()
    assert data["status"] == STATUS_UNRESOLVED
    assert data["unresolved_reason"] == REASON_BUDGET
    assert data["evaluations"] == 1
    assert data["candidate_instance"] == 0
    assert data["changes"] == []


def test_every_candidate_is_fully_revalidated():
    # minimum 修复值恰好破坏 multipleOf：multipleOf 不提供修复算子，
    # 因此修复 minimum 后仍有错误，不能标记成功。
    schema = {"type": "integer", "minimum": 5, "multipleOf": 3}
    data = post(schema, 1).json()
    assert data["status"] == STATUS_UNRESOLVED
    assert data["unresolved_reason"] == REASON_NO_FIX
    keywords = {e["keyword"] for e in data["remaining_errors"]}
    assert keywords == {"multipleOf"}


# ---- 其他语义 --------------------------------------------------------------


def test_false_schema_unfixable():
    data = post(False, 1).json()
    assert data["status"] == STATUS_UNRESOLVED
    assert data["remaining_errors"][0]["keyword"] is None


def test_response_shape_contains_required_fields():
    data = post({"type": "integer", "minimum": 1}, 0).json()
    for field_ in (
        "status",
        "candidate_instance",
        "original_instance",
        "changes",
        "remaining_errors",
        "evaluations",
        "stopped_by_budget",
        "unresolved_reason",
    ):
        assert field_ in data
    assert data["original_instance"] == 0


def test_remaining_error_fields():
    data = post({"type": "object", "required": ["x"]}, {}).json()
    err = data["remaining_errors"][0]
    assert set(err) == {"keyword", "instance_path", "schema_path", "message"}
    assert isinstance(err["message"], str) and err["message"]
