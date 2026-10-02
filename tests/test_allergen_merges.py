"""过敏原代码并码端到端测试。

质控把旧过敏原代码并入目标代码（必填原因）：并码可串接；目标须非空且当前
不是别名；自映射、成环、同一旧代码改指其他目标一律拒绝且不留记录；同向重试
幂等返回原结果。原料声明版本与已签发标签快照保留原值；实时溯源按最终规范
代码合并同义项并去重，同时保留每条原始来源路径、声明版本，待确认状态不变；
并码后重算受影响批次（含设备残留下游），沿用既有事件字段与全局顺序，仅为
实际变化记录事件；并码与事件同事务；新原料登记/声明更正/预演按当前并码关系
解释代码；既有接口字段与路径结构保持兼容。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="allergen-merge-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    for t in ["material_declarations", "material_batches", "processing_inputs",
              "processing_outputs", "processings", "cleanings", "labels",
              "trace_events", "investigations", "label_applications",
              "allergen_code_merges"]:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'trace_events'")
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'allergen_code_merges'")
    conn.close()
    with TestClient(app) as c:
        yield c


def material(c, batch_id, allergens=None, pending=False,
             declared_at="2026-01-01T00:00:00Z"):
    return c.post("/materials", json={
        "batch_id": batch_id, "allergens": allergens or [],
        "pending": pending, "declared_at": declared_at})


def processing(c, pid, eq, start, end, inputs, outputs):
    return c.post("/processings", json={
        "processing_id": pid, "equipment_id": eq,
        "start_utc": start, "end_utc": end,
        "inputs": inputs, "outputs": outputs})

def issue_label(c, batch_id):
    """走完审批流签发标签：申请（当前游标）-> 批准 -> 签发。"""
    cursor = c.get("/changes/snapshot").json()["cursor"]
    app = c.post("/label-applications", json={
        "batch_id": batch_id, "applicant": "qc-1",
        "reason": "例行放行审核", "cursor": cursor})
    assert app.status_code == 201, app.text
    aid = app.json()["application_id"]
    r = c.post(f"/label-applications/{aid}/approve", json={"reviewer": "qa-1"})
    assert r.status_code == 200, r.text
    return c.post(f"/batches/{batch_id}/labels", json={"application_id": aid})



def merge(c, old, target, reason="同义词并码"):
    return c.post("/allergen-merges", json={
        "old_code": old, "target_code": target, "reason": reason})


# ---------------------------------------------------------------- 登记校验

def test_merge_requires_declared_old_code(client):
    material(client, "RM-1", allergens=["milk"])
    r = merge(client, "casein", "milk")
    assert r.status_code == 400  # 旧代码未在任何现存声明中出现
    assert client.get("/allergen-merges").json()["merges"] == []


def test_merge_self_map_rejected_without_record(client):
    material(client, "RM-1", allergens=["milk"])
    r = merge(client, "milk", "milk")
    assert r.status_code == 409
    assert "自映射" in r.json()["detail"]
    assert client.get("/allergen-merges").json()["merges"] == []


def test_merge_target_cannot_be_alias(client):
    material(client, "RM-1", allergens=["milk", "dairy", "casein"])
    assert merge(client, "casein", "milk").status_code == 201
    # milk 当前是规范代码；casein 已是别名，不能再作目标
    r = merge(client, "milk", "casein")
    assert r.status_code == 409
    assert "别名" in r.json()["detail"]
    # 库内仍只有第一条并码
    merges = client.get("/allergen-merges").json()["merges"]
    assert [(m["old_code"], m["target_code"]) for m in merges] == [
        ("casein", "milk")]


def test_same_old_code_cannot_repoint_elsewhere(client):
    material(client, "RM-1", allergens=["milk", "casein", "dairy"])
    assert merge(client, "casein", "milk").status_code == 201
    r = merge(client, "casein", "dairy")
    assert r.status_code == 409
    merges = client.get("/allergen-merges").json()["merges"]
    assert [m["target_code"] for m in merges] == ["milk"]


def test_merge_requires_reason(client):
    material(client, "RM-1", allergens=["milk", "casein"])
    r = client.post("/allergen-merges",
                    json={"old_code": "casein", "target_code": "milk",
                          "reason": "   "})
    assert r.status_code == 422
    assert client.get("/allergen-merges").json()["merges"] == []


def test_idempotent_same_direction_retry_returns_original(client):
    material(client, "RM-1", allergens=["milk", "casein"])
    first = merge(client, "casein", "milk", reason="首次原因")
    assert first.status_code == 201
    merge_id = first.json()["merge_id"]
    second = merge(client, "casein", "milk", reason="再次请求原因不同")
    assert second.status_code == 200  # 同向重试
    body = second.json()
    assert body["merge_id"] == merge_id       # 返回原登记
    assert body["reason"] == "首次原因"        # 原因不被覆盖
    assert body["replayed"] is True
    assert first.json().get("replayed") is False
    merges = client.get("/allergen-merges").json()["merges"]
    assert len(merges) == 1


# ---------------------------------------------------------------- 串接与代码查询

def test_merge_chains_and_code_lookup(client):
    material(client, "RM-1", allergens=["milk", "casein", "dairy"])
    assert merge(client, "casein", "dairy").status_code == 201
    assert merge(client, "dairy", "milk").status_code == 201  # 串接
    info = client.get("/allergen-codes/casein").json()
    assert info["canonical_code"] == "milk"
    assert info["is_alias"] is True
    assert [(h["old_code"], h["target_code"]) for h in info["history"]] == [
        ("casein", "dairy"), ("dairy", "milk")]
    canon = client.get("/allergen-codes/milk").json()
    assert canon["canonical_code"] == "milk"
    assert canon["history"] == []
    unknown = client.get("/allergen-codes/soy").json()
    assert unknown["canonical_code"] == "soy"
    assert unknown["is_alias"] is False


# ---------------------------------------------------------------- 实时溯源规范化

def test_trace_merges_synonyms_dedupes_and_keeps_raw_paths(client):
    # 两条原料链最终汇入同一加工产出：旧代码 casein 与规范代码 milk 合并
    material(client, "RM-A", allergens=["casein"])
    material(client, "RM-B", allergens=["milk"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-A", "RM-B"], ["OUT-1"])
    assert merge(client, "casein", "milk").status_code == 201

    t = client.get("/batches/OUT-1/trace").json()
    assert t["allergens"] == ["milk"]              # 同义项合并
    paths = t["allergen_paths"]["milk"]
    # 每条原始来源路径保留（RM-A 上的原始代码 casein、RM-B 上的 milk）
    root_steps = sorted(paths, key=lambda p: p[0]["batch_id"])
    step_a = next(p[0] for p in root_steps if p[0]["batch_id"] == "RM-A")
    step_b = next(p[0] for p in root_steps if p[0]["batch_id"] == "RM-B")
    assert (step_a["allergen_code"], step_a["canonical_code"]) == (
        "casein", "milk")
    # 规范代码自身的声明步骤保持原结构（不附加代码字段）
    assert "allergen_code" not in step_b and "canonical_code" not in step_b
    versions = {d["batch_id"]: d["version"]
                for d in t["basis"]["material_declarations"]}
    assert versions == {"RM-A": 1, "RM-B": 1}     # 声明版本依据保留
    # 原料端：原始 allergens 保留，附加规范化结果
    ma = client.get("/materials/RM-A").json()
    assert ma["allergens"] == ["casein"]
    assert ma["allergens_canonical"] == ["milk"]


def test_trace_chain_resolution_and_product_dedup(client):
    # 同一原料声明里两个旧代码经串接并到同一规范代码，产品端按完整路径去重
    material(client, "RM-1", allergens=["casein", "dairy"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    merge(client, "casein", "dairy")
    merge(client, "dairy", "milk")
    t = client.get("/batches/OUT-1/trace").json()
    assert t["allergens"] == ["milk"]
    paths = t["allergen_paths"]["milk"]
    # 声明步骤不同（原始代码不同）-> 两条路径都保留，不去掉证据
    assert sorted(p[0]["allergen_code"] for p in paths) == ["casein", "dairy"]


def test_merge_keeps_pending_state(client):
    material(client, "RM-1", allergens=["casein"], pending=True)
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    merge(client, "casein", "milk")
    t = client.get("/batches/OUT-1/trace").json()
    assert t["pending"] is True
    assert t["pending_paths"][0][0]["batch_id"] == "RM-1"
    assert t["allergens"] == ["milk"]


def test_merge_recalculates_equipment_residue_downstream(client):
    material(client, "RM-MILK", allergens=["casein"],
             declared_at="2026-01-02T06:00:00Z")
    material(client, "RM-OAT", allergens=["gluten"],
             declared_at="2026-01-02T06:00:00Z")
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-MILK"], ["OUT-MILK-1"])
    processing(client, "P2", "EQ-1",
               "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
               ["RM-OAT"], ["OUT-OAT-1"])  # 残留继承 casein
    assert merge(client, "casein", "milk").status_code == 201
    t = client.get("/batches/OUT-OAT-1/trace").json()
    assert "milk" in t["allergens"] and "casein" not in t["allergens"]
    # 残留路径保留原始来源信息
    milk_paths = t["allergen_paths"]["milk"]
    residue = [s for p in milk_paths for s in p
               if s.get("via") == "equipment_residue"]
    assert residue and residue[0]["equipment_id"] == "EQ-1"


# ---------------------------------------------------------------- 历史声明与标签快照

def test_merge_covers_allergens_only_in_old_declaration_versions(client):
    material(client, "RM-1", allergens=["casein"])
    # 更正后现行声明不再含 casein，但旧版本里有 -> 仍允许登记并码
    client.post("/materials/RM-1/declarations", json={"allergens": ["milk"]})
    assert merge(client, "casein", "milk").status_code == 201


def test_declaration_versions_and_issued_labels_keep_raw_values(client):
    material(client, "RM-1", allergens=["casein"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    issued = issue_label(client, "OUT-1").json()
    assert issued["conclusion"]["allergens"] == ["casein"]  # 签发时快照
    merge(client, "casein", "milk")
    label = client.get(f"/labels/{issued['label_id']}").json()
    assert label["conclusion"]["allergens"] == ["casein"]   # 旧标签不变
    frozen_step = label["conclusion"]["allergen_paths"]["casein"][0][0]
    assert "allergen_code" not in frozen_step  # 旧快照结构原样保留
    # 声明历史仍保留原始代码
    decls = client.post(
        "/materials/RM-1/declarations", json={"allergens": ["casein"]})
    assert decls.status_code == 201
    live = client.get("/batches/OUT-1/trace").json()
    assert live["allergens"] == ["milk"]
    # 并码后新签发标签按当前规范代码冻结
    label2 = issue_label(client, "OUT-1").json()
    assert label2["conclusion"]["allergens"] == ["milk"]


# ---------------------------------------------------------------- 事件流

def test_merge_records_events_only_for_actual_changes(client):
    material(client, "RM-A", allergens=["casein"])
    material(client, "RM-B", allergens=["gluten"])  # 不受影响
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-A"], ["OUT-1"])
    snap = client.get("/changes/snapshot").json()
    merge(client, "casein", "milk")
    ev = client.get(f"/changes/events?cursor={snap['cursor']}&limit=100").json()
    assert {e["batch_id"] for e in ev["events"]} == {"RM-A", "OUT-1"}
    assert all(e["op"] == "allergen_code_merge" for e in ev["events"])
    op_refs = {e["batch_id"]: e["op_ref"] for e in ev["events"]}
    assert op_refs["RM-A"]["old_code"] == "casein"
    assert op_refs["RM-A"]["target_code"] == "milk"
    # 全局顺序：同次操作按批次号排序
    assert [e["batch_id"] for e in ev["events"]] == sorted(
        e["batch_id"] for e in ev["events"])
    # 仅实际变化：事件 after 为 milk，RM-B 无事件
    out_ev = next(e for e in ev["events"] if e["batch_id"] == "OUT-1")
    assert out_ev["after"]["conclusion"]["allergens"] == ["milk"]
    assert out_ev["before"]["conclusion"]["allergens"] == ["casein"]


def test_failed_merge_records_no_event(client):
    material(client, "RM-1", allergens=["milk"])
    before_max = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    merge(client, "milk", "milk")
    after_max = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    assert before_max == after_max


# ---------------------------------------------------------------- 新登记/更正/预演按当前并码解释

def test_new_registration_and_correction_follow_current_merges(client):
    material(client, "RM-1", allergens=["milk"])
    material(client, "RM-2", allergens=["casein"])
    merge(client, "casein", "milk")
    # 并码后新原料用旧代码登记：实时结论按规范代码解释
    material(client, "RM-3", allergens=["casein"])
    t = client.get("/batches/RM-3/trace").json()
    assert t["allergens"] == ["milk"]
    step = t["allergen_paths"]["milk"][0][0]
    assert step["allergen_code"] == "casein"
    # 声明更正同样按当前并码关系解释
    client.post("/materials/RM-1/declarations", json={"allergens": ["casein"]})
    t1 = client.get("/batches/RM-1/trace").json()
    assert t1["allergens"] == ["milk"]
    assert t1["basis"]["material_declarations"][0]["version"] == 2


def test_declaration_preview_follows_current_merges(client):
    material(client, "RM-1", allergens=["milk", "casein"])
    merge(client, "casein", "milk")
    # 现行声明 [milk, casein] 规范口径只有 milk；预演更正为 [casein, milk]
    # （同集合、不同书写顺序），按当前并码关系解释后结论的过敏原/待确认无差异
    # （追加新版本本身仍体现为依据版本变化，与既有更正语义一致）
    r = client.post("/materials/RM-1/declarations/preview",
                    json={"allergens": ["casein", "milk"]}).json()
    assert r["affected_batches"][0]["diff"]["conclusion"] is None
    # 但用原始代码 [casein] 更正（去掉 milk 同义书写项，版本/依据变化）仍有差异，
    # 差异以规范口径呈现；且声明中记录的原始代码不变（存储层不规范化）
    r2 = client.post("/materials/RM-1/declarations/preview",
                     json={"allergens": ["casein"]}).json()
    assert r2["affected_batches"][0]["batch_id"] == "RM-1"
    assert r2["change"]["allergens"] == ["casein"]


def test_concurrent_merge_invalidates_declaration_preview(client):
    material(client, "RM-1", allergens=["milk"])
    prev = client.post("/materials/RM-1/declarations/preview",
                       json={"allergens": ["soy"]}).json()
    token = prev["preview_id"]
    # 预演期间发生并码（即使与 RM-1 无关）：解释口径已变，凭据失效
    material(client, "RM-2", allergens=["nut", "tree_nut"])
    merge(client, "tree_nut", "nut")
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 409


# ---------------------------------------------------------------- 兼容性

def test_existing_trace_fields_and_paths_remain_compatible(client):
    material(client, "RM-1", allergens=["milk", "peanut"], pending=True)
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    t = client.get("/batches/OUT-1/trace").json()
    # 既有字段结构不变
    assert set(t) >= {"batch_id", "kind", "pending", "allergens",
                      "allergen_paths", "pending_paths", "basis", "release"}
    assert t["allergens"] == ["milk", "peanut"]
    step = t["allergen_paths"]["milk"][0][0]
    assert step["batch_id"] == "RM-1" and step["via"] == "declaration"
    # 无并码时步骤与原结构一致（不额外带代码字段）
    assert "allergen_code" not in step
