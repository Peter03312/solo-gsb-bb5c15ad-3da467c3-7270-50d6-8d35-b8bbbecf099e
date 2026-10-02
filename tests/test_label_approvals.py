"""标签签发审批（可审计）端到端测试。

流程：质控提交签发申请（批次号、申请人、审批理由、拟签发标签备注、当前
溯源游标）-> 系统冻结并返回申请时刻的实时结论/依据/封存状态 -> 审核人
批准或驳回（仅当申请仍对应同一批次结论且未封存）-> 批准后凭申请签发标签
（绑定批准记录、冻结申请时结论，一个批准一张标签）。驳回或结论/封存变化
禁止签发、需重新申请；同一未决申请重复提交幂等；已处理申请不可重复审批。
"""
import os
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="allergen-label-approval-test-")
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


def build_line(c):
    """EQ-1: P1(RM-MILK -> OUT-1) 08-09, P2(RM-OAT -> OUT-2) 10-11（无清洗）。"""
    material(c, "RM-MILK", allergens=["milk"])
    material(c, "RM-OAT", allergens=["gluten"])
    assert processing(c, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-MILK"], ["OUT-1"]).status_code == 201
    assert processing(c, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OAT"], ["OUT-2"]).status_code == 201


def cursor(c):
    return c.get("/changes/snapshot").json()["cursor"]


def apply(c, batch_id, applicant="qc-zhang", reason="例行放行审核",
          note=None, cur=None):
    body = {"batch_id": batch_id, "applicant": applicant, "reason": reason,
            "cursor": cursor(c) if cur is None else cur}
    if note is not None:
        body["note"] = note
    return c.post("/label-applications", json=body)


def approve(c, aid, reviewer="qa-li"):
    return c.post(f"/label-applications/{aid}/approve",
                  json={"reviewer": reviewer})


def reject(c, aid, reviewer="qa-li", note=None):
    body = {"reviewer": reviewer}
    if note is not None:
        body["note"] = note
    return c.post(f"/label-applications/{aid}/reject", json=body)


def issue(c, batch_id, aid, supersedes_id=None):
    body = {"application_id": aid}
    if supersedes_id is not None:
        body["supersedes_id"] = supersedes_id
    return c.post(f"/batches/{batch_id}/labels", json=body)


def approved_application(c, batch_id, note=None):
    aid = apply(c, batch_id, note=note).json()["application_id"]
    assert approve(c, aid).status_code == 200
    return aid


# ---------------------------------------------------------------- 申请提交

def test_application_returns_frozen_live_view(client):
    build_line(client)
    r = apply(client, "OUT-2", note="2026 年第 3 批燕麦奶标签")
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["replayed"] is False
    assert d["status"] == "pending"
    assert d["applicant"] == "qc-zhang"
    assert d["reason"] == "例行放行审核"
    assert d["note"] == "2026 年第 3 批燕麦奶标签"
    assert d["cursor"] == cursor(client)
    # 实时过敏原（含设备残留 milk）、待确认、封存状态与依据摘要
    assert d["conclusion"]["allergens"] == ["gluten", "milk"]
    assert d["conclusion"]["pending"] is False
    assert d["conclusion"]["allergen_paths"]["milk"][0][-1]["via"] == \
        "equipment_residue"
    assert d["release"] == {"held": False, "investigations": []}
    versions = {m["batch_id"]: m["version"]
                for m in d["basis"]["material_declarations"]}
    assert versions == {"RM-MILK": 1, "RM-OAT": 1}
    assert d["stale"] is False
    assert d["reviewer"] is None and d["label_id"] is None


def test_application_pending_state_in_response(client):
    material(client, "RM-P", pending=True)
    d = apply(client, "RM-P").json()
    assert d["conclusion"]["pending"] is True
    assert d["conclusion"]["pending_paths"][0][0]["batch_id"] == "RM-P"


def test_application_validation_errors(client):
    build_line(client)
    # 未知批次 -> 400
    r = apply(client, "NOPE")
    assert r.status_code == 400 and "未知批次" in r.json()["detail"]
    # 缺少理由 / 申请人 -> 422
    assert apply(client, "OUT-2", reason="").status_code == 422
    assert apply(client, "OUT-2", applicant="  ").status_code == 422
    r = client.post("/label-applications",
                    json={"batch_id": "OUT-2", "applicant": "qc",
                          "cursor": cursor(client)})
    assert r.status_code == 422  # 缺少 reason 字段
    # 无效游标：负数 / 超过当前最大序号 -> 400
    assert apply(client, "OUT-2", cur=-1).status_code == 400
    assert apply(client, "OUT-2", cur=cursor(client) + 1).status_code == 400
    # 失败不留申请
    assert client.get("/label-applications").json()["applications"] == []


def test_application_stale_cursor_rejected(client):
    build_line(client)
    stale = cursor(client)
    # 游标之后发生新变更（与目标批次无关的登记也算）
    material(client, "RM-NEW", allergens=["soy"])
    r = apply(client, "OUT-2", cur=stale)
    assert r.status_code == 409 and "过期" in r.json()["detail"]
    # 用最新游标重新提交成功
    assert apply(client, "OUT-2").status_code == 201


def test_duplicate_pending_application_idempotent(client):
    build_line(client)
    cur = cursor(client)
    r1 = apply(client, "OUT-2", note="备注", cur=cur)
    assert r1.status_code == 201
    r2 = apply(client, "OUT-2", note="备注", cur=cur)
    assert r2.status_code == 200 and r2.json()["replayed"] is True
    assert r2.json()["application_id"] == r1.json()["application_id"]
    # 不重复建行
    apps = client.get("/label-applications").json()["applications"]
    assert len(apps) == 1
    # 任一字段不同则是新申请
    r3 = apply(client, "OUT-2", note="另一条备注", cur=cur)
    assert r3.status_code == 201
    assert r3.json()["application_id"] != r1.json()["application_id"]
    # 已处理的申请不在幂等范围：批准后同样请求创建新申请
    approve(client, r1.json()["application_id"])
    r4 = apply(client, "OUT-2", note="备注", cur=cur)
    assert r4.status_code == 201
    assert r4.json()["application_id"] != r1.json()["application_id"]


# ---------------------------------------------------------------- 审批

def test_approve_then_issue_freezes_application_conclusion(client):
    build_line(client)
    aid = apply(client, "OUT-2", note="放行标签").json()["application_id"]
    r = approve(client, aid)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "approved"
    assert d["reviewer"] == "qa-li" and d["reviewed_at"]
    # 批准后才能签发：绑定批准记录并冻结申请时结论
    r = issue(client, "OUT-2", aid)
    assert r.status_code == 201, r.text
    label = r.json()
    assert label["application_id"] == aid
    assert label["note"] == "放行标签"
    assert label["conclusion"]["allergens"] == ["gluten", "milk"]
    # 申请与标签一一绑定
    app = client.get(f"/label-applications/{aid}").json()
    assert app["label_id"] == label["label_id"]
    # 后续变更不改标签快照（冻结的是申请时结论）
    client.post("/materials/RM-OAT/declarations",
                json={"allergens": ["gluten", "sesame"]})
    frozen = client.get(f"/labels/{label['label_id']}").json()
    assert frozen["conclusion"]["allergens"] == ["gluten", "milk"]
    assert frozen["application_id"] == aid and frozen["note"] == "放行标签"


def test_issue_requires_approval(client):
    build_line(client)
    aid = apply(client, "OUT-2").json()["application_id"]
    # 未批准（未决）-> 409
    r = issue(client, "OUT-2", aid)
    assert r.status_code == 409 and "尚未批准" in r.json()["detail"]
    # 驳回后 -> 409，需重新申请
    assert reject(client, aid, note="理由不充分").status_code == 200
    r = issue(client, "OUT-2", aid)
    assert r.status_code == 409 and "已驳回" in r.json()["detail"]
    # 驳回的申请保留审批轨迹
    app = client.get(f"/label-applications/{aid}").json()
    assert app["status"] == "rejected"
    assert app["reviewer"] == "qa-li" and app["review_note"] == "理由不充分"


def test_processed_application_cannot_be_reviewed_again(client):
    build_line(client)
    aid = apply(client, "OUT-2").json()["application_id"]
    assert approve(client, aid).status_code == 200
    # 已批准：重复批准 / 驳回均 409
    assert approve(client, aid).status_code == 409
    assert reject(client, aid).status_code == 409
    # 已驳回：再批准 / 再驳回均 409
    aid2 = apply(client, "OUT-2").json()["application_id"]
    assert reject(client, aid2).status_code == 200
    assert approve(client, aid2).status_code == 409
    assert reject(client, aid2).status_code == 409
    # 未知申请 404
    assert approve(client, 999).status_code == 404
    assert reject(client, 999).status_code == 404
    # 审核人必填
    r = client.post(f"/label-applications/{aid2}/approve",
                    json={"reviewer": " "})
    assert r.status_code == 422


def test_review_rejected_when_conclusion_changed(client):
    build_line(client)
    aid = apply(client, "OUT-2").json()["application_id"]
    # 申请后结论变化（声明更正）-> 申请失效，批准/驳回均 409
    client.post("/materials/RM-OAT/declarations",
                json={"allergens": ["gluten", "sesame"]})
    assert approve(client, aid).status_code == 409
    assert reject(client, aid).status_code == 409
    # 申请标记为失效（stale），需重新申请
    assert client.get(f"/label-applications/{aid}").json()["stale"] is True
    # 重新申请 -> 批准 -> 签发，冻结新结论
    aid2 = approved_application(client, "OUT-2")
    label = issue(client, "OUT-2", aid2).json()
    assert label["conclusion"]["allergens"] == ["gluten", "milk", "sesame"]


def test_issue_rejected_when_conclusion_changed_after_approval(client):
    build_line(client)
    aid = approved_application(client, "OUT-2")
    # 批准后结论变化 -> 禁止签发，需重新申请
    client.post("/materials/RM-OAT/declarations",
                json={"allergens": ["gluten", "sesame"]})
    r = issue(client, "OUT-2", aid)
    assert r.status_code == 409 and "重新申请" in r.json()["detail"]
    assert client.get("/batches/OUT-2/labels").json() == []


def test_one_approval_issues_exactly_one_label(client):
    build_line(client)
    aid = approved_application(client, "OUT-2")
    label = issue(client, "OUT-2", aid).json()
    # 同一批准再次签发 -> 409（一个批准只能签发一张标签）
    r = issue(client, "OUT-2", aid)
    assert r.status_code == 409 and str(label["label_id"]) in \
        r.json()["detail"]
    labels = client.get("/batches/OUT-2/labels").json()
    assert [l["label_id"] for l in labels] == [label["label_id"]]


def test_issue_application_mismatch_and_unknown(client):
    build_line(client)
    material(client, "RM-X")
    aid = approved_application(client, "OUT-2")
    # 申请属于别的批次 -> 400
    r = issue(client, "RM-X", aid)
    assert r.status_code == 400 and "OUT-2" in r.json()["detail"]
    # 未知申请 -> 404
    assert issue(client, "OUT-2", 999).status_code == 404
    # 缺少 application_id -> 422（批准后才能签发）
    assert client.post("/batches/OUT-2/labels", json={}).status_code == 422


# ---------------------------------------------------------------- 封存约束

def test_held_batch_application_cannot_be_approved(client):
    build_line(client)
    # 封存批次仍可提交申请（响应如实返回封存状态）
    client.post("/investigations", json={
        "investigation_id": "INV-1", "batch_id": "RM-MILK",
        "reason": "疑似交叉接触"})
    r = apply(client, "OUT-2")
    assert r.status_code == 201
    d = r.json()
    assert d["release"]["held"] is True
    assert d["release"]["investigations"][0]["investigation_id"] == "INV-1"
    aid = d["application_id"]
    # 封存中：批准/驳回均 409
    assert approve(client, aid).status_code == 409
    assert reject(client, aid).status_code == 409
    # 解除封存后：封存状态已变化，原申请失效，仍不可审批
    client.post("/investigations/INV-1/lift")
    assert client.get(f"/label-applications/{aid}").json()["stale"] is True
    assert approve(client, aid).status_code == 409
    # 需重新申请：新申请 -> 批准 -> 签发
    aid2 = approved_application(client, "OUT-2")
    assert issue(client, "OUT-2", aid2).status_code == 201


def test_issue_rejected_when_held_after_approval(client):
    build_line(client)
    aid = approved_application(client, "OUT-2")
    # 批准后批次被新封存 -> 禁止签发
    client.post("/investigations", json={
        "investigation_id": "INV-1", "batch_id": "RM-MILK",
        "reason": "疑似交叉接触"})
    r = issue(client, "OUT-2", aid)
    assert r.status_code == 409
    assert client.get("/batches/OUT-2/labels").json() == []


# ---------------------------------------------------------------- 查询与兼容

def test_application_queries_and_filters(client):
    build_line(client)
    material(client, "RM-X")
    a1 = apply(client, "OUT-2").json()["application_id"]
    a2 = apply(client, "RM-X").json()["application_id"]
    a3 = apply(client, "OUT-2", note="第二条").json()["application_id"]
    approve(client, a1)
    reject(client, a2)
    all_apps = client.get("/label-applications").json()["applications"]
    assert [a["application_id"] for a in all_apps] == [a1, a2, a3]
    by_status = client.get("/label-applications?status=pending").json()
    assert [a["application_id"] for a in by_status["applications"]] == [a3]
    by_batch = client.get(
        "/label-applications?batch_id=RM-X").json()["applications"]
    assert [a["application_id"] for a in by_batch] == [a2]
    assert client.get("/label-applications?status=weird").status_code == 400
    assert client.get("/label-applications/999").status_code == 404


def test_supersedes_still_supported_with_approval(client):
    build_line(client)
    l1 = issue(client, "OUT-2", approved_application(client, "OUT-2")).json()
    client.post("/materials/RM-OAT/declarations",
                json={"allergens": ["gluten", "sesame"]})
    # 变更后签发关联旧版的新标签（新申请 + 新批准）
    l2 = issue(client, "OUT-2", approved_application(client, "OUT-2"),
               supersedes_id=l1["label_id"]).json()
    assert l2["supersedes_id"] == l1["label_id"]
    assert l2["application_id"] != l1["application_id"]
    assert l2["conclusion"]["allergens"] == ["gluten", "milk", "sesame"]
    # 旧标签快照不变
    assert client.get(f"/labels/{l1['label_id']}").json()[
        "conclusion"]["allergens"] == ["gluten", "milk"]


def test_approval_flow_produces_no_events(client):
    build_line(client)
    cur = cursor(client)
    aid = apply(client, "OUT-2").json()["application_id"]
    approve(client, aid)
    assert issue(client, "OUT-2", aid).status_code == 201
    ev = client.get(f"/changes/events?cursor={cur}&limit=100").json()
    assert ev["events"] == []


def test_legacy_label_without_application_stays_compatible(client):
    """审批流上线前签发的历史标签（无 application_id/note）查询兼容。"""
    build_line(client)
    conn = db.connect()
    conn.execute(
        "INSERT INTO labels (batch_id, issued_at, supersedes_id, snapshot)"
        " VALUES ('OUT-2', '2026-01-02T12:00:00+00:00', NULL,"
        " '{\"batch_id\": \"OUT-2\", \"conclusion\": {\"pending\": false,"
        " \"allergens\": [\"gluten\"], \"allergen_paths\": {},"
        " \"pending_paths\": []}, \"basis\": {\"material_declarations\": [],"
        " \"cleanings\": []}}')")
    conn.commit()
    conn.close()
    labels = client.get("/batches/OUT-2/labels").json()
    assert len(labels) == 1
    assert labels[0]["application_id"] is None
    assert labels[0]["note"] is None
    assert labels[0]["conclusion"]["allergens"] == ["gluten"]
    # 新签发的标签与历史标签同列返回
    aid = approved_application(client, "OUT-2")
    assert issue(client, "OUT-2", aid).status_code == 201
    labels = client.get("/batches/OUT-2/labels").json()
    assert len(labels) == 2
    assert labels[1]["application_id"] == aid


def test_migration_adds_approval_columns_to_legacy_labels(tmp_path,
                                                          monkeypatch):
    """旧版库（labels 无 application_id/note 列）启动时自动幂等迁移，
    历史标签数据保留、新列默认为 NULL。"""
    legacy = tmp_path / "legacy-labels.db"
    raw = sqlite3.connect(legacy)
    snapshot = (
        '{"batch_id": "RM-1", "conclusion": {"pending": false,'
        ' "allergens": ["milk"], "allergen_paths": {}, "pending_paths": []},'
        ' "basis": {"material_declarations": [], "cleanings": []}}')
    raw.executescript(
        """
        CREATE TABLE material_batches (batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE material_declarations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
            version INTEGER NOT NULL, allergens TEXT NOT NULL,
            pending INTEGER NOT NULL, declared_at TEXT NOT NULL,
            UNIQUE (batch_id, version));
        CREATE TABLE labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            supersedes_id INTEGER REFERENCES labels(id),
            snapshot TEXT NOT NULL);
        INSERT INTO material_batches VALUES ('RM-1', '2026-01-01T00:00:00+00:00');
        INSERT INTO material_declarations
            (batch_id, version, allergens, pending, declared_at)
            VALUES ('RM-1', 1, '["milk"]', 0, '2026-01-01T00:00:00+00:00');
        """)
    raw.execute(
        "INSERT INTO labels (id, batch_id, issued_at, supersedes_id, snapshot)"
        " VALUES (3, 'RM-1', '2026-01-02T00:00:00+00:00', NULL, ?)",
        (snapshot,))
    raw.commit()
    raw.close()
    monkeypatch.setenv("DB_PATH", str(legacy))
    db.init_db()  # 启动迁移（幂等，再跑一次也不应报错）
    db.init_db()
    conn = db.connect()
    row = conn.execute("SELECT * FROM labels WHERE id = 3").fetchone()
    assert row["application_id"] is None and row["note"] is None
    assert row["batch_id"] == "RM-1"
    # 新表 label_applications 已建，可正常走审批流
    conn.close()
    with TestClient(app) as c:
        assert c.get("/labels/3").json()["application_id"] is None
        aid = apply(c, "RM-1").json()["application_id"]
        assert approve(c, aid).status_code == 200
        label = issue(c, "RM-1", aid).json()
        assert label["application_id"] == aid
