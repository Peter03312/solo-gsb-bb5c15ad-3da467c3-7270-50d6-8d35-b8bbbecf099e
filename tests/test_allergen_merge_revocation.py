"""过敏原代码并码撤销端到端测试。

质控发现并码登记有误时，可凭并码记录号 + 必填原因撤销单条关系；并码列表可查
原登记及撤销原因、时间，代码查询仅按生效链给出规范码与沿革。串接链按仍生效的
关系解析：A→B→C 中撤销 B→C 后 A 归 B，撤销 A→B 后 B 仍归 C；同一旧代码可
重新并入当前非别名目标，原有同向重试仅对生效关系幂等。原料声明各版本与已签发
标签快照保持原始代码；撤销后重算受影响批次及设备残留下游，初始化快照反映新
结论，仅为实际变化的批次在同一事务写入有序事件，历史事件不可改写。撤销使未
提交的预演凭据失效；未知记录、重复撤销、空白原因或非法再并码拒绝且不留记录
或事件。既有查询字段与来源路径结构保持兼容。
"""
import os
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="allergen-merge-revoke-test-")
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


def revoke(c, merge_id, reason="质控复核：原登记有误"):
    return c.post(f"/allergen-merges/{merge_id}/revoke",
                  json={"reason": reason})


# ---------------------------------------------------------------- 撤销校验

def test_revoke_unknown_record_404_no_event(client):
    latest = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    r = revoke(client, 999)
    assert r.status_code == 404
    # 不留记录、不产事件
    assert client.get("/allergen-merges").json()["merges"] == []
    assert client.get("/changes/events?cursor=0&limit=1").json()["latest"] == latest


def test_revoke_duplicate_409(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    assert revoke(client, mid).status_code == 200
    r = revoke(client, mid)
    assert r.status_code == 409
    # 表内只有一行（撤销行保留），撤销原因不被第二次请求覆盖
    merges = client.get("/allergen-merges").json()["merges"]
    assert len(merges) == 1
    assert merges[0]["status"] == "revoked"
    assert merges[0]["revoked_reason"] == "质控复核：原登记有误"


def test_revoke_blank_reason_422(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    r = revoke(client, mid, reason="   ")
    assert r.status_code == 422
    # 关系仍生效
    assert client.get(f"/allergen-merges/{mid}").json()["status"] == "active"
    info = client.get("/allergen-codes/casein").json()
    assert info["canonical_code"] == "milk"


def test_revoke_response_and_list_keep_original_with_revocation_fields(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    m = merge(client, "casein", "milk", reason="首次并码原因").json()
    r = revoke(client, m["merge_id"], reason="复核撤销原因").json()
    assert r["merge_id"] == m["merge_id"]
    assert r["old_code"] == "casein" and r["target_code"] == "milk"
    assert r["reason"] == "首次并码原因"  # 原登记保留
    assert r["status"] == "revoked" and r["revoked"] is True
    assert r["revoked_reason"] == "复核撤销原因"
    assert r["revoked_at"] and r["merged_at"]
    # 单条查询
    got = client.get(f"/allergen-merges/{m['merge_id']}").json()
    assert got["revoked_reason"] == "复核撤销原因"
    # 列表含撤销行
    listed = client.get("/allergen-merges").json()["merges"]
    assert [(x["merge_id"], x["status"]) for x in listed] == [
        (m["merge_id"], "revoked")]


# ---------------------------------------------------------------- 生效链解析

def test_revoke_chain_tail_A_goes_to_B(client):
    # A(casein) -> B(dairy) -> C(milk)，撤销 B->C 后 A 归 B，B 归 B 自身
    material(client, "RM-1", allergens=["casein", "dairy", "milk"])
    ab = merge(client, "casein", "dairy").json()["merge_id"]
    bc = merge(client, "dairy", "milk").json()["merge_id"]
    revoke(client, bc)
    assert client.get("/allergen-codes/casein").json()["canonical_code"] == "dairy"
    assert client.get("/allergen-codes/dairy").json()["canonical_code"] == "dairy"
    assert client.get("/allergen-codes/milk").json()["canonical_code"] == "milk"
    # 代码查询沿革仅含仍生效的跳（B->C 已撤销，不出现在 casein/dairy 沿革中）
    hist = client.get("/allergen-codes/casein").json()["history"]
    assert [(h["merge_id"], h["old_code"], h["target_code"])
            for h in hist] == [(ab, "casein", "dairy")]
    # 撤销行在沿革中不可见，但仍在并码列表里
    listed = client.get("/allergen-merges").json()["merges"]
    assert {x["merge_id"] for x in listed} == {ab, bc}


def test_revoke_chain_head_B_still_goes_to_C(client):
    # 撤销 A->B 后 B 仍归 C
    material(client, "RM-1", allergens=["casein", "dairy", "milk"])
    ab = merge(client, "casein", "dairy").json()["merge_id"]
    merge(client, "dairy", "milk")
    revoke(client, ab)
    assert client.get("/allergen-codes/casein").json()["canonical_code"] == "casein"
    assert client.get("/allergen-codes/dairy").json()["canonical_code"] == "milk"
    hist_casein = client.get("/allergen-codes/casein").json()["history"]
    assert hist_casein == []
    hist_dairy = client.get("/allergen-codes/dairy").json()["history"]
    assert [(h["old_code"], h["target_code"]) for h in hist_dairy] == [
        ("dairy", "milk")]


def test_revoke_restores_trace_canonical_and_path_structure(client):
    material(client, "RM-A", allergens=["casein"])
    material(client, "RM-B", allergens=["milk"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-A", "RM-B"], ["OUT-1"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    t1 = client.get("/batches/OUT-1/trace").json()
    assert t1["allergens"] == ["milk"]
    revoke(client, mid)
    t2 = client.get("/batches/OUT-1/trace").json()
    assert t2["allergens"] == ["casein", "milk"]
    # 撤销后 casein 声明步骤回到无附加代码字段的原结构
    step = t2["allergen_paths"]["casein"][0][0]
    assert step == {"batch_id": "RM-A", "via": "declaration"}


# ---------------------------------------------------------------- 撤销后重新并码

def test_remerge_same_old_code_creates_new_record(client):
    material(client, "RM-1", allergens=["casein", "milk", "dairy"])
    first = merge(client, "casein", "milk").json()
    revoke(client, first["merge_id"])
    # 撤销后重新并入另一个当前非别名目标 -> 新记录、201
    second = merge(client, "casein", "dairy")
    assert second.status_code == 201
    assert second.json()["merge_id"] != first["merge_id"]
    assert client.get("/allergen-codes/casein").json()["canonical_code"] == "dairy"
    listed = client.get("/allergen-merges").json()["merges"]
    assert [(x["old_code"], x["target_code"], x["status"]) for x in listed] == [
        ("casein", "milk", "revoked"), ("casein", "dairy", "active")]


def test_remerge_back_to_same_target_is_new_registration(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    first = merge(client, "casein", "milk").json()
    revoke(client, first["merge_id"])
    # 撤销后以相同 old/target 重新登记：不是“同向重试”，而是新登记
    again = merge(client, "casein", "milk")
    assert again.status_code == 201
    assert again.json()["replayed"] is False
    assert again.json()["merge_id"] != first["merge_id"]
    assert client.get("/allergen-codes/casein").json()["canonical_code"] == "milk"


def test_same_direction_replay_idempotent_only_while_active(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    first = merge(client, "casein", "milk", reason="原原因").json()
    # 生效中同向重试：幂等
    replay = merge(client, "casein", "milk", reason="另一个原因")
    assert replay.status_code == 200
    assert replay.json()["merge_id"] == first["merge_id"]
    assert replay.json()["reason"] == "原原因"
    revoke(client, first["merge_id"])
    # 撤销后再次相同请求：不再是幂等重试
    after = merge(client, "casein", "milk", reason="重新并码")
    assert after.status_code == 201
    assert after.json()["merge_id"] != first["merge_id"]


def test_remerge_into_current_alias_target_rejected(client):
    material(client, "RM-1", allergens=["casein", "dairy", "milk"])
    first = merge(client, "casein", "milk").json()
    merge(client, "dairy", "milk")
    revoke(client, first["merge_id"])
    # dairy 当前是别名（dairy->milk 生效）：casein 不能再并入 dairy
    r = merge(client, "casein", "dairy")
    assert r.status_code == 409
    assert "别名" in r.json()["detail"]
    # 非法再并码不留新记录、不产事件
    listed = client.get("/allergen-merges").json()["merges"]
    assert [x for x in listed if x["status"] == "active" and x["old_code"] == "casein"] == []
    # 撤销行之后撤销 dairy->milk，则 dairy 不再是别名，再并码成功
    dairy_id = next(x["merge_id"] for x in listed
                    if x["old_code"] == "dairy" and x["status"] == "active")
    revoke(client, dairy_id)
    ok = merge(client, "casein", "dairy")
    assert ok.status_code == 201


def test_repoint_active_merge_still_rejected_and_note_revoke_first(client):
    material(client, "RM-1", allergens=["casein", "milk", "dairy"])
    merge(client, "casein", "milk")
    r = merge(client, "casein", "dairy")  # 生效中改指 -> 409
    assert r.status_code == 409
    assert "撤销" in r.json()["detail"]
    assert len(client.get("/allergen-merges").json()["merges"]) == 1


# ---------------------------------------------------------------- 历史不改写

def test_revoke_keeps_declaration_versions_and_label_snapshots(client):
    material(client, "RM-1", allergens=["casein"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    lab = issue_label(client, "OUT-1").json()
    mid = merge(client, "casein", "milk").json()["merge_id"]
    lab2 = issue_label(client, "OUT-1").json()
    assert lab2["conclusion"]["allergens"] == ["milk"]
    revoke(client, mid)
    # 两张已签发标签各自保持签发时快照
    assert client.get(f"/labels/{lab['label_id']}").json()[
        "conclusion"]["allergens"] == ["casein"]
    assert client.get(f"/labels/{lab2['label_id']}").json()[
        "conclusion"]["allergens"] == ["milk"]
    # 原料声明原始代码不变
    assert client.get("/materials/RM-1").json()["allergens"] == ["casein"]
    # 撤销后新签发标签按撤销后的口径
    lab3 = issue_label(client, "OUT-1").json()
    assert lab3["conclusion"]["allergens"] == ["casein"]


# ---------------------------------------------------------------- 重算/事件/快照

def test_revoke_recalculates_equipment_residue_downstream(client):
    material(client, "RM-MILK", allergens=["casein"],
             declared_at="2026-01-02T06:00:00Z")
    material(client, "RM-OAT", allergens=["gluten"],
             declared_at="2026-01-02T06:00:00Z")
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-MILK"], ["OUT-MILK-1"])
    processing(client, "P2", "EQ-1",
               "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
               ["RM-OAT"], ["OUT-OAT-1"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    assert "milk" in client.get("/batches/OUT-OAT-1/trace").json()["allergens"]
    snap = client.get("/changes/snapshot").json()
    revoke(client, mid)
    # 残留下游重算：milk 消失，casein 恢复
    t = client.get("/batches/OUT-OAT-1/trace").json()
    assert t["allergens"] == ["casein", "gluten"]
    # 初始化快照反映新结论
    batches = {b["batch_id"]: b for b in
               client.get("/changes/snapshot").json()["batches"]}
    assert batches["OUT-OAT-1"]["conclusion"]["allergens"] == [
        "casein", "gluten"]
    # 事件：撤销重算（含残留下游），仅实际变化批次、按批次号有序
    ev = client.get(
        f"/changes/events?cursor={snap['cursor']}&limit=100").json()["events"]
    assert {e["batch_id"] for e in ev} == {"RM-MILK", "OUT-MILK-1", "OUT-OAT-1"}
    assert all(e["op"] == "allergen_code_merge_revocation" for e in ev)
    assert [e["batch_id"] for e in ev] == sorted(e["batch_id"] for e in ev)
    out_ev = next(e for e in ev if e["batch_id"] == "OUT-OAT-1")
    assert out_ev["before"]["conclusion"]["allergens"] == ["gluten", "milk"]
    assert out_ev["after"]["conclusion"]["allergens"] == ["casein", "gluten"]
    assert out_ev["op_ref"]["merge_id"] == mid
    assert out_ev["op_ref"]["old_code"] == "casein"


def test_revoke_with_no_conclusion_change_writes_no_event(client):
    # 旧代码只存在于历史声明版本、当前结论不引用 -> 撤销不产生批次变化事件
    material(client, "RM-1", allergens=["casein"])
    client.post("/materials/RM-1/declarations", json={"allergens": ["milk"]})
    mid = merge(client, "casein", "milk").json()["merge_id"]
    latest = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    revoke(client, mid)
    after = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    assert latest == after


def test_revoke_then_remerge_events_are_append_only(client):
    material(client, "RM-1", allergens=["casein"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    revoke(client, mid)
    second = merge(client, "casein", "milk").json()["merge_id"]
    events = client.get("/changes/events?cursor=0&limit=100").json()["events"]
    ops = [(e["op"], e["op_ref"].get("merge_id")) for e in events
           if e["batch_id"] == "RM-1"]
    assert ops == [
        ("material_register", None),
        ("allergen_code_merge", mid),
        ("allergen_code_merge_revocation", mid),
        ("allergen_code_merge", second),
    ]
    # 历史事件行不可改写：撤销事件的 before 仍是撤销前视图
    rev_ev = next(e for e in events
                  if e["op"] == "allergen_code_merge_revocation")
    assert rev_ev["before"]["conclusion"]["allergens"] == ["milk"]
    assert rev_ev["after"]["conclusion"]["allergens"] == ["casein"]


def test_failed_revoke_rolls_back_event_and_record(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    revoke(client, mid)  # 第一次成功
    latest_after_first = client.get(
        "/changes/events?cursor=0&limit=1").json()["latest"]
    revoke(client, mid)  # 第二次 409，无新增事件
    assert client.get(
        "/changes/events?cursor=0&limit=1").json()["latest"] == latest_after_first
    # 撤销时间/原因未被重复请求改写
    got = client.get(f"/allergen-merges/{mid}").json()
    assert got["revoked_reason"] == "质控复核：原登记有误"


# ---------------------------------------------------------------- 预演凭据失效

def test_merge_revocation_invalidates_pending_preview(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    # 预演一项与并码无直接关系的声明更正
    prev = client.post("/materials/RM-1/declarations/preview",
                       json={"allergens": ["casein", "milk", "soy"]}).json()
    token = prev["preview_id"]
    # 预演未提交期间撤销并码 -> 解释口径变化，凭据失效
    revoke(client, mid)
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 409
    # 重新预演后可正常提交
    prev2 = client.post("/materials/RM-1/declarations/preview",
                        json={"allergens": ["casein", "milk", "soy"]}).json()
    ok = client.post(f"/previews/{prev2['preview_id']}/commit")
    assert ok.status_code == 200
    assert ok.json()["applied"] is True


def test_remerge_registration_invalidates_pending_preview_after_revoke(client):
    material(client, "RM-1", allergens=["casein", "milk", "dairy"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    revoke(client, mid)
    prev = client.post(
        "/materials/RM-1/declarations/preview",
        json={"allergens": ["milk"]}).json()
    token = prev["preview_id"]
    # 撤销后重新并码同样改变解释口径
    merge(client, "casein", "dairy")
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 409


# ---------------------------------------------------------------- 兼容性

def test_active_merge_view_fields_compatible(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    merge(client, "casein", "milk")
    # 生效行：撤销相关字段为空值，原有字段全部保留
    m = client.get("/allergen-merges").json()["merges"][0]
    assert set(m) >= {"merge_id", "old_code", "target_code", "reason",
                      "merged_at"}
    assert m["status"] == "active" and m["revoked"] is False
    assert m["revoked_reason"] is None and m["revoked_at"] is None


def test_migration_from_legacy_schema(tmp_path, monkeypatch):
    """旧版库（old_code 列级 UNIQUE、无撤销列）启动时自动迁移，数据与 id 保留。"""
    legacy = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy)
    raw.executescript(
        """
        CREATE TABLE material_batches (batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE material_declarations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
            version INTEGER NOT NULL, allergens TEXT NOT NULL,
            pending INTEGER NOT NULL, declared_at TEXT NOT NULL,
            UNIQUE (batch_id, version));
        CREATE TABLE allergen_code_merges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            old_code TEXT NOT NULL UNIQUE,
            target_code TEXT NOT NULL, reason TEXT NOT NULL, merged_at TEXT NOT NULL);
        INSERT INTO material_batches VALUES ('RM-1', '2026-01-01T00:00:00+00:00');
        INSERT INTO material_declarations
            (batch_id, version, allergens, pending, declared_at)
            VALUES ('RM-1', 1, '["casein","milk"]', 0, '2026-01-01T00:00:00+00:00');
        INSERT INTO allergen_code_merges
            (id, old_code, target_code, reason, merged_at)
            VALUES (7, 'casein', 'milk', '历史原因', '2026-02-01T00:00:00+00:00');
        """)
    raw.commit()
    raw.close()
    monkeypatch.setenv("DB_PATH", str(legacy))
    db.init_db()  # 启动迁移（幂等，再跑一次也不应报错）
    db.init_db()
    conn = db.connect()
    rows = conn.execute("SELECT * FROM allergen_code_merges ORDER BY id").fetchall()
    assert len(rows) == 1
    assert (rows[0]["id"], rows[0]["old_code"], rows[0]["target_code"],
            rows[0]["revoked"]) == (7, "casein", "milk", 0)
    # 迁移后撤销 + 重新并码可用（同一旧代码两条行：一撤销一生效）
    from app import service
    from app.schemas import AllergenCodeMergeCreate, AllergenCodeMergeRevokeCreate

    class _R:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    service.revoke_allergen_merge(
        conn, 7, _R(reason="旧库迁移后撤销"))
    view, replayed = service.register_allergen_merge(
        conn, AllergenCodeMergeCreate(
            old_code="casein", target_code="milk", reason="重新并码"))
    assert replayed is False and view["merge_id"] != 7
    assert service.allergen_code_info(conn, "casein")["canonical_code"] == "milk"
    conn.close()
