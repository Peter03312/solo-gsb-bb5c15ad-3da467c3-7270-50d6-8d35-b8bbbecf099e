"""SQLite 存储层：连接管理、建表、写事务。

所有时间统一存为 UTC ISO8601 字符串（如 2026-09-30T08:00:00+00:00），
格式一致时可直接按字符串比较大小。
"""
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS material_batches (
    batch_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL          -- 首次登记时间（UTC），用于“投入前可用”判定
);

-- 原料过敏原声明：每次登记/更正追加一个版本，当前有效版本 = 最大 version
CREATE TABLE IF NOT EXISTS material_declarations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL REFERENCES material_batches(batch_id),
    version     INTEGER NOT NULL,
    allergens   TEXT NOT NULL,         -- JSON 数组，如 ["milk","peanut"]
    pending     INTEGER NOT NULL,      -- 1 = 待确认
    declared_at TEXT NOT NULL,
    UNIQUE (batch_id, version)
);

CREATE TABLE IF NOT EXISTS processings (
    processing_id TEXT PRIMARY KEY,
    equipment_id  TEXT NOT NULL,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_inputs (
    processing_id TEXT NOT NULL REFERENCES processings(processing_id),
    batch_id      TEXT NOT NULL,
    PRIMARY KEY (processing_id, batch_id)
);

-- 一个批次最多只能被加工产出一次（batch_id 唯一）
CREATE TABLE IF NOT EXISTS processing_outputs (
    processing_id TEXT NOT NULL REFERENCES processings(processing_id),
    batch_id      TEXT NOT NULL UNIQUE,
    PRIMARY KEY (processing_id, batch_id)
);

CREATE TABLE IF NOT EXISTS cleanings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id TEXT NOT NULL,
    cleaned_at   TEXT NOT NULL,
    verified     INTEGER NOT NULL,     -- 是否已验证
    revoked      INTEGER NOT NULL DEFAULT 0  -- 1 = 已撤销，撤销后不再清除残留
);

-- 标签签发申请（可审计审批环节）：质控提交批次号、申请人、审批理由、
-- 拟签发标签备注与当前溯源游标；系统冻结申请时刻的实时结论、依据与封存
-- 状态。审核人仅在申请仍对应同一批次结论/依据/封存状态且未封存时批准或
-- 驳回；批准后凭申请签发标签（一个批准只能签发一张标签，label_id 绑定），
-- 驳回或结论/封存变化一律禁止签发、需重新申请。
-- status: pending（未决）/ approved（已批准）/ rejected（已驳回）。
CREATE TABLE IF NOT EXISTS label_applications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    applicant     TEXT NOT NULL,        -- 申请人
    reason        TEXT NOT NULL,        -- 审批理由（必填）
    note          TEXT,                 -- 拟签发标签备注（可空，签发时随标签冻结）
    cursor        INTEGER NOT NULL,     -- 提交时的溯源游标（须等于当前最大事件序号）
    status        TEXT NOT NULL DEFAULT 'pending',
    snapshot      TEXT NOT NULL,        -- 申请时冻结的结论与依据（JSON）
    release       TEXT NOT NULL,        -- 申请时冻结的封存状态（JSON）
    created_at    TEXT NOT NULL,
    reviewer      TEXT,                 -- 审核人
    reviewed_at   TEXT,
    review_note   TEXT,                 -- 审核意见（可空）
    -- 签发后绑定（一个批准一张标签）；由签发事务与标签写入同步维护，
    -- 有意不加外键：labels.application_id 已引用本表，避免循环依赖
    label_id      INTEGER
);

-- 已签发标签：snapshot 为签发时刻冻结的结论与依据（JSON），不可更改。
-- 审批流上线后签发的标签绑定批准记录（application_id）与申请备注（note）；
-- 历史标签两列为 NULL，快照与查询结构保持兼容。
CREATE TABLE IF NOT EXISTS labels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    issued_at     TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES labels(id),
    snapshot      TEXT NOT NULL,
    application_id INTEGER REFERENCES label_applications(id),
    note          TEXT
);

-- 调查封存：QC 以“唯一调查号 + 疑似批次 + 原因”登记；受影响批次沿加工投入链
-- 与同设备相邻加工残留链（有效清洗截断）实时传播，与过敏原声明是否为空无关。
-- status: open（生效中）/ lifted（已解除；解除只移除该调查自身的影响）。
CREATE TABLE IF NOT EXISTS investigations (
    investigation_id TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL,          -- 疑似交叉接触的根批次
    reason      TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    lifted_at   TEXT,                  -- NULL = 仍生效
    status      TEXT NOT NULL DEFAULT 'open'
);

-- 过敏原代码并码（同义代码合并）：质控登记 old_code -> target_code。
-- 并码登记有误可凭登记记录号 + 必填原因撤销**单条关系**：撤销行保留在表内
-- （并码列表可查原登记及撤销原因、时间），但不再参与生效链解析；同一旧代码
-- 撤销后可重新并入当前非别名目标（新登记为新的一行）。
-- 库层约束：每个旧代码至多存在一条**生效中**的并码（部分唯一索引）——
-- 生效中的同一旧代码改指其他目标在库层即被拒绝，撤销行不占名额。
-- 原料声明版本与标签快照中的原始代码永不改写出库，只在实时计算时按本表中
-- **仍生效**的行规范化。
CREATE TABLE IF NOT EXISTS allergen_code_merges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    old_code      TEXT NOT NULL,
    target_code   TEXT NOT NULL,
    reason        TEXT NOT NULL,          -- 必填并码原因
    merged_at     TEXT NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0,  -- 1 = 已撤销（仅该行关系失效）
    revoked_reason TEXT,                      -- 必填撤销原因
    revoked_at    TEXT
);

-- 变更事件流：与业务写入同一事务落库，下游按游标（seq）增量消费。
-- seq 全局递增；同一次操作产生的多条事件按批次号排序（插入序即批次号序）。
CREATE TABLE IF NOT EXISTS trace_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 全局递增序号
    op          TEXT NOT NULL,       -- 触发操作类型（登记/更正/撤销/预演提交等）
    op_ref      TEXT NOT NULL,       -- JSON：操作定位信息（加工号/清洗 id/声明版本/凭据等）
    batch_id    TEXT NOT NULL,       -- 批次号
    kind        TEXT NOT NULL,       -- material / product
    before_view TEXT,                -- JSON：变更前完整结论与依据；新批次为 NULL
    after_view  TEXT NOT NULL,       -- JSON：变更后完整结论与依据
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trace_events_batch ON trace_events(batch_id);

-- 每个旧代码至多一条生效中的并码（撤销行保留但不占名额，撤销后可重新并码）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_allergen_merges_active_old
    ON allergen_code_merges(old_code) WHERE revoked = 0;
"""


def _migrate_merges(conn: sqlite3.Connection) -> None:
    """幂等迁移（建表之前执行）：旧版 allergen_code_merges（old_code 列级
    UNIQUE、无撤销列）重建为新表结构并搬移既有行（id 保持不变，事件
    op_ref.merge_id 不失效）。新结构库直接跳过。"""
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(allergen_code_merges)")}
    if cols and "revoked" not in cols:
        conn.executescript(
            """
            ALTER TABLE allergen_code_merges RENAME TO allergen_code_merges_old;
            CREATE TABLE allergen_code_merges (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                old_code      TEXT NOT NULL,
                target_code   TEXT NOT NULL,
                reason        TEXT NOT NULL,
                merged_at     TEXT NOT NULL,
                revoked       INTEGER NOT NULL DEFAULT 0,
                revoked_reason TEXT,
                revoked_at    TEXT
            );
            INSERT INTO allergen_code_merges
                (id, old_code, target_code, reason, merged_at)
                SELECT id, old_code, target_code, reason, merged_at
                FROM allergen_code_merges_old;
            DROP TABLE allergen_code_merges_old;
            """)


def _migrate_labels(conn: sqlite3.Connection) -> None:
    """幂等迁移（建表之后执行，外键目标 label_applications 需已存在）：
    旧版 labels（无 application_id / note 列）补加两列，历史标签两列为
    NULL（审批流上线前签发，快照与查询保持兼容）。新结构库直接跳过。"""
    label_cols = {r["name"] for r in conn.execute("PRAGMA table_info(labels)")}
    if label_cols and "application_id" not in label_cols:
        conn.execute(
            "ALTER TABLE labels ADD COLUMN application_id"
            " INTEGER REFERENCES label_applications(id)")
        conn.execute("ALTER TABLE labels ADD COLUMN note TEXT")


def db_path() -> str:
    return os.environ.get("DB_PATH", "app.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        # 1) 旧版并码表重建（SCHEMA 中的部分唯一索引依赖新结构的 revoked 列）；
        # 2) 幂等建表/建索引（含新表 label_applications）；
        # 3) 旧版 labels 补列（外键目标 label_applications 需已存在）。
        _migrate_merges(conn)
        conn.executescript(SCHEMA)
        _migrate_labels(conn)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """写事务：BEGIN IMMEDIATE 保证“校验+写入”原子，失败回滚保持原状态。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    conn.commit()


@contextmanager
def read_tx(conn: sqlite3.Connection):
    """读事务：WAL 下为多个查询提供一致快照（初始化读取/分页读取看到同一状态），
    不阻塞并发写入。"""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()  # 只读事务，结束即释放（等效提交）
