import json
import os
import time
from datetime import date

import psycopg
from psycopg.rows import dict_row

from build import clean_scan
from partition import (
    AXIS_NAMES,
    BATCH_CUTS,
    FACETS,
    FACET_SHORTS,
    OUTCOME_NAMES,
    candidate_keys,
    facet_axis,
    facet_name,
    is_mixed,
    normalize_method,
    partition_key,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ods_scan (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    terminal_code TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    landed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dwd_move (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    terminal_code TEXT NOT NULL,
    container_no TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    event_at TIMESTAMP NOT NULL,
    partition_key TEXT,
    facet_method TEXT
);

CREATE TABLE IF NOT EXISTS build_runs (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    built_by TEXT NOT NULL,
    method TEXT,
    carried_facet TEXT,
    deviation BOOLEAN NOT NULL DEFAULT FALSE,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS build_rejects (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES build_runs(id),
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    reason TEXT NOT NULL
);

-- 分区设置：全湖只此一份。切法一经封存就全湖沿用，不许有的业务日按事件码切、有的按通道切。
CREATE TABLE IF NOT EXISTS partition_policy (
    id SMALLINT PRIMARY KEY,
    method TEXT NOT NULL,
    pinned_by TEXT NOT NULL,
    pinned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pinned_on DATE NOT NULL,
    CONSTRAINT partition_policy_single CHECK (id = 1),
    CONSTRAINT partition_policy_method CHECK (method IN ('event', 'channel'))
);

-- 混切队列：切面名不为空、又跟封存切法对不上的扫描逐条排队。切面名为空的不进队列。
CREATE TABLE IF NOT EXISTS mixed_queue (
    id BIGSERIAL PRIMARY KEY,
    ods_id BIGINT NOT NULL REFERENCES ods_scan(id),
    biz_date DATE NOT NULL,
    terminal_code TEXT NOT NULL,
    facet_name TEXT NOT NULL,
    facet_axis TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'pending',
    run_id BIGINT REFERENCES build_runs(id),
    settled_by TEXT,
    settled_at TIMESTAMPTZ,
    settle_note TEXT,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT mixed_queue_outcome CHECK (
        outcome IN ('pending', 'adopted', 'deviation', 'contention')
    ),
    CONSTRAINT mixed_queue_one_per_scan UNIQUE (ods_id)
);

-- 混切台账：清洗请求带的切面偏了、两路同时下手争用、采纳了哪一面，都落在这里。
CREATE TABLE IF NOT EXISTS mixed_attempts (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    mixed_id BIGINT REFERENCES mixed_queue(id),
    ods_id BIGINT,
    carried_facet TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    note TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT mixed_attempts_outcome CHECK (
        outcome IN ('adopted', 'deviation', 'contention')
    )
);

CREATE INDEX IF NOT EXISTS mixed_queue_day_idx ON mixed_queue (biz_date);
CREATE INDEX IF NOT EXISTS dwd_move_partition_idx ON dwd_move (biz_date, partition_key);
"""

MIGRATE = """
ALTER TABLE dwd_move ADD COLUMN IF NOT EXISTS partition_key TEXT;
ALTER TABLE dwd_move ADD COLUMN IF NOT EXISTS facet_method TEXT;
ALTER TABLE build_runs ADD COLUMN IF NOT EXISTS method TEXT;
ALTER TABLE build_runs ADD COLUMN IF NOT EXISTS carried_facet TEXT;
ALTER TABLE build_runs ADD COLUMN IF NOT EXISTS deviation BOOLEAN NOT NULL DEFAULT FALSE;
"""

# 全湖一把锁：钉切面按它，跨日加锁保证不会今天一个切法、明天另一个切法。
POLICY_LOCK_KEY = 820214
# 混切处置也走同一把跨日锁：不分业务日，同一刻只留一路下手。


class BadMethod(Exception):
    pass


class PolicySealed(Exception):
    pass


class FacetNotPinned(Exception):
    pass


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                conn.execute(MIGRATE)
                count = conn.execute("SELECT COUNT(*) AS n FROM ods_scan").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                else:
                    sync_mixed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _stamp(value):
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="minutes")


def _payload(row):
    payload = row["raw_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


def _land(conn, biz_date, terminal, payload):
    row = conn.execute(
        """
        INSERT INTO ods_scan (biz_date, terminal_code, raw_payload)
        VALUES (%s, %s, %s::jsonb)
        RETURNING id
        """,
        (biz_date, terminal, json.dumps(payload, ensure_ascii=False)),
    ).fetchone()
    return row["id"]


def _seed(conn):
    built = [
        {"scan": "IN", "box": "CSNU1029384", "at": "2026-11-11T07:40:00", "gate": "T1", "facet": "IN"},
        {"scan": "OUT", "box": "CSNU1029384", "at": "2026-11-11T16:05:00", "gate": "T1", "facet": "OUT"},
    ]
    waiting = [
        {"scan": "IN", "box": "TCLU5566778", "at": "2026-11-12T09:15:00", "gate": "T2", "facet": "IN"},
        {"scan": "XX", "box": "TCLU5566778", "at": "2026-11-12T11:00:00", "gate": "T2", "facet": "XX"},
        {"scan": "RS", "box": "BAD", "at": "2026-11-12T12:00:00", "gate": "T2", "facet": "RS"},
        # 切面名带的是通道 T2：切法封存为事件码时就成了混切。
        {"scan": "OUT", "box": "TCLU5566778", "at": "2026-11-12T17:20:00", "gate": "T2", "facet": "T2"},
        # 切面名为空的扫描不进混切队列。
        {"scan": "IN", "box": "MSCU7788990", "at": "2026-11-12T19:40:00", "gate": "T2", "facet": "  "},
    ]
    for payload in built:
        _land(conn, "2026-11-11", payload["gate"], payload)
    for payload in waiting:
        _land(conn, "2026-11-12", payload["gate"], payload)
    # 2026-11-11 的明细是分区切面引入之前收的：留着旧明细，不带分区键。
    _seed_legacy_build(conn, "2026-11-11", "admin")


def _seed_legacy_build(conn, biz_date, username):
    run = conn.execute(
        """
        INSERT INTO build_runs (biz_date, accepted_count, rejected_count, built_by)
        VALUES (%s, 0, 0, %s)
        RETURNING id
        """,
        (biz_date, username),
    ).fetchone()
    accepted = 0
    rejected = 0
    rows = conn.execute(
        "SELECT id, terminal_code, raw_payload FROM ods_scan WHERE biz_date = %s ORDER BY id",
        (biz_date,),
    ).fetchall()
    for row in rows:
        cleaned, reason = clean_scan(_payload(row))
        if reason:
            conn.execute(
                "INSERT INTO build_rejects (run_id, source_id, reason) VALUES (%s, %s, %s)",
                (run["id"], row["id"], reason),
            )
            rejected += 1
            continue
        conn.execute(
            """
            INSERT INTO dwd_move
                (biz_date, source_id, terminal_code, container_no, event_kind, event_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                biz_date,
                row["id"],
                row["terminal_code"],
                cleaned["container_no"],
                cleaned["event_kind"],
                cleaned["event_at"],
            ),
        )
        accepted += 1
    conn.execute(
        "UPDATE build_runs SET accepted_count = %s, rejected_count = %s WHERE id = %s",
        (accepted, rejected, run["id"]),
    )


# ---------------- 分区设置 ----------------


def policy_of(conn):
    return conn.execute(
        "SELECT method, pinned_by, pinned_at, pinned_on FROM partition_policy WHERE id = 1"
    ).fetchone()


def settings(conn):
    row = policy_of(conn)
    if row is None:
        return {
            "status": "unset",
            "method": None,
            "method_name": None,
            "short_name": None,
            "pinned_by": None,
            "pinned_at": None,
            "pinned_on": None,
            "locked": False,
            "sealed": False,
            "can_change": True,
            "choices": [
                {"value": code, "label": label, "selected": False}
                for code, label in FACETS.items()
            ],
        }
    return {
        "status": "sealed",
        "method": row["method"],
        "method_name": FACETS[row["method"]],
        "short_name": FACET_SHORTS[row["method"]],
        "pinned_by": row["pinned_by"],
        "pinned_at": _stamp(row["pinned_at"]),
        "pinned_on": row["pinned_on"].isoformat(),
        "locked": True,
        "sealed": True,
        "can_change": False,
        "choices": [
            {"value": code, "label": label, "selected": code == row["method"]}
            for code, label in FACETS.items()
        ],
    }


def pin_policy(conn, method, username):
    normalized = normalize_method(method)
    if normalized is None:
        raise BadMethod("分区办法只能选按事件码切开后落库，或按通道切开后落库")
    # 跨日加锁：全湖一把锁，任何一天的建层员都串在这把锁上。
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (POLICY_LOCK_KEY,))
    row = policy_of(conn)
    if row is None:
        conn.execute(
            """
            INSERT INTO partition_policy (id, method, pinned_by, pinned_at, pinned_on)
            VALUES (1, %s, %s, NOW(), %s)
            """,
            (normalized, username, date.today()),
        )
        return settings(conn)
    if row["method"] == normalized:
        return settings(conn)
    raise PolicySealed("分区切法已封存，本日禁止改口，全湖沿用同一套切法")


# ---------------- 混切队列 ----------------


def _mixed_lock(conn, biz_date):
    """同一批混切跨日加锁：锁按业务日取，整天串在这把锁上，先到先办。"""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"mixed:{biz_date}",),
    )


def _record_attempt(conn, biz_date, carried_facet, outcome, note, actor,
                    mixed_id=None, ods_id=None):
    conn.execute(
        """
        INSERT INTO mixed_attempts
            (biz_date, mixed_id, ods_id, carried_facet, outcome, note, actor)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (biz_date, mixed_id, ods_id, carried_facet, outcome, note, actor),
    )


def _queue_entry(conn, ods_id):
    return conn.execute(
        "SELECT * FROM mixed_queue WHERE ods_id = %s", (ods_id,)
    ).fetchone()


def _enqueue(conn, ods_id, biz_date, terminal, payload):
    """切面名不为空、又与封存切法对不上，才进混切队列。"""
    policy = policy_of(conn)
    if policy is None:
        return
    name = facet_name(payload)
    if not name:
        return
    axis = facet_axis(payload, terminal)
    if not is_mixed(axis, policy["method"]):
        return
    conn.execute(
        """
        INSERT INTO mixed_queue
            (ods_id, biz_date, terminal_code, facet_name, facet_axis)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (ods_id) DO NOTHING
        """,
        (ods_id, biz_date, terminal, name, axis),
    )


def sync_mixed(conn):
    """贴源里已有、队列里还没有的混切扫描补排进队列；切面名为空的不进。"""
    policy = policy_of(conn)
    if policy is None:
        return
    rows = conn.execute(
        """
        SELECT o.id, o.biz_date, o.terminal_code, o.raw_payload
        FROM ods_scan o
        WHERE NOT EXISTS (SELECT 1 FROM mixed_queue q WHERE q.ods_id = o.id)
        ORDER BY o.id
        """
    ).fetchall()
    for row in rows:
        _enqueue(conn, row["id"], row["biz_date"], row["terminal_code"], _payload(row))


def land_scan(conn, biz_date, terminal, payload):
    ods_id = _land(conn, biz_date, terminal, payload)
    _enqueue(conn, ods_id, biz_date, terminal, payload)
    return ods_id


def _axis_name(axis):
    return AXIS_NAMES.get(axis, axis)


def mixed_rows(conn, biz_date=None):
    sync_mixed(conn)
    sql = """
        SELECT q.id, q.ods_id, q.biz_date, q.terminal_code, q.facet_name, q.facet_axis,
               q.outcome, q.settled_by, q.settled_at, q.settle_note, q.queued_at,
               o.raw_payload
        FROM mixed_queue q
        JOIN ods_scan o ON o.id = q.ods_id
    """
    params = ()
    if biz_date:
        sql += " WHERE q.biz_date = %s"
        params = (biz_date,)
    sql += " ORDER BY q.biz_date, q.id"
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        payload = _payload({"raw_payload": row.pop("raw_payload")})
        row["biz_date"] = row["biz_date"].isoformat()
        row["facet_axis_name"] = _axis_name(row["facet_axis"])
        row["outcome_name"] = OUTCOME_NAMES.get(row["outcome"], row["outcome"])
        row["queued_at"] = _stamp(row["queued_at"])
        row["settled_at"] = _stamp(row["settled_at"])
        keys = candidate_keys(payload, row["terminal_code"])
        row["event_key"] = keys["event_key"]
        row["channel_key"] = keys["channel_key"]
    return rows


def attempts(conn, biz_date=None):
    sql = """
        SELECT a.id, a.biz_date, a.mixed_id, a.ods_id, a.carried_facet, a.outcome,
               a.note, a.actor, a.created_at
        FROM mixed_attempts a
    """
    params = ()
    if biz_date:
        sql += " WHERE a.biz_date = %s"
        params = (biz_date,)
    sql += " ORDER BY a.id DESC LIMIT 100"
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        row["biz_date"] = row["biz_date"].isoformat()
        row["created_at"] = _stamp(row["created_at"])
        row["outcome_name"] = {
            "adopted": "已采纳",
            "deviation": "分区切面偏了",
            "contention": "混切争用",
        }.get(row["outcome"], row["outcome"])
    return rows


def adopt_mixed(conn, biz_date, carried_facet, username):
    """对同一批混切下手：跨日加锁后，只采纳与分区设置相符的切面。

    返回 (payload, problem)；problem 为 None 表示采纳成功，否则是 (status, message)。
    先到且有锁的一路落账，后到的记混切争用；切面与设置不符记分区切面偏了。
    """
    policy = policy_of(conn)
    if policy is None:
        return None, (409, "分区切面尚未锁死，混切队列不能采纳")
    if not str(carried_facet or "").strip():
        return None, (422, "切面名不能为空，空切面不进混切队列")
    _mixed_lock(conn, biz_date)

    rows = conn.execute(
        "SELECT * FROM mixed_queue WHERE biz_date = %s ORDER BY id FOR UPDATE",
        (biz_date,),
    ).fetchall()
    if not rows:
        return None, (404, "这一日没有混切扫描")

    if any(row["outcome"] == "adopted" for row in rows):
        note = "混切争用：这一批混切已被先到的建层员采纳，后到的记争用"
        _record_attempt(
            conn, biz_date, str(carried_facet or ""), "contention", note, username
        )
        return None, (409, note)

    method = normalize_method(carried_facet)
    if method is None:
        return None, (422, "切面名认不出；只能带按事件码切或按通道切的切面")

    if method != policy["method"]:
        note = (
            f"分区切面偏了：清洗请求带着的切面「{carried_facet}」跟分区设置"
            f"「{FACETS[policy['method']]}」对不上，混切扫描不落库"
        )
        conn.execute(
            """
            UPDATE mixed_queue
            SET outcome = 'deviation', settled_by = %s, settled_at = NOW(), settle_note = %s
            WHERE biz_date = %s
            """,
            (username, note, biz_date),
        )
        _record_attempt(
            conn, biz_date, str(carried_facet), "deviation", note, username
        )
        return None, (409, note)

    conn.execute(
        """
        UPDATE mixed_queue
        SET outcome = 'adopted', settled_by = %s, settled_at = NOW(),
            settle_note = %s
        WHERE biz_date = %s
        """,
        (username, f"按「{FACETS[method]}」采纳，与分区设置相符", biz_date),
    )
    note = f"混切已采纳：按「{FACETS[method]}」落库，与分区设置相符"
    _record_attempt(conn, biz_date, str(carried_facet), "adopted", note, username)
    return {
        "biz_date": biz_date,
        "carried_facet": str(carried_facet),
        "method": method,
        "method_name": FACETS[method],
        "adopted": len(rows),
        "outcome": "adopted",
        "note": note,
    }, None


# ---------------- 收成明细层 ----------------


def rebuild_day(conn, biz_date, username, carried_method=None):
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (f"build:{biz_date}",)
    )
    carried = None
    if carried_method not in (None, ""):
        carried = normalize_method(carried_method)
        if carried is None:
            raise BadMethod("分区办法只能选按事件码切开后落库，或按通道切开后落库")

    policy = policy_of(conn)
    if policy is None:
        raise FacetNotPinned("分区切面尚未锁死，这一批不准落库")
    method = policy["method"]
    deviation = carried is not None and carried != method

    sync_mixed(conn)

    rows = conn.execute(
        "SELECT id, terminal_code, raw_payload FROM ods_scan WHERE biz_date = %s ORDER BY id",
        (biz_date,),
    ).fetchall()
    queued = {
        row["ods_id"]: row
        for row in conn.execute(
            "SELECT * FROM mixed_queue WHERE biz_date = %s", (biz_date,)
        ).fetchall()
    }

    run = conn.execute(
        """
        INSERT INTO build_runs
            (biz_date, accepted_count, rejected_count, built_by, method, carried_facet, deviation)
        VALUES (%s, 0, 0, %s, %s, %s, %s)
        RETURNING id
        """,
        (biz_date, username, method, carried, deviation),
    ).fetchone()
    if deviation:
        note = (
            f"分区切面偏了：清洗请求带着的切面「{carried_method}」跟分区设置"
            f"「{FACETS[method]}」对不上，混切扫描不落库"
        )
        _record_attempt(conn, biz_date, str(carried_method), "deviation", note, username)

    conn.execute("DELETE FROM dwd_move WHERE biz_date = %s", (biz_date,))
    accepted = 0
    rejected = 0
    mixed_kept = 0
    for row in rows:
        payload = _payload(row)
        axis = facet_axis(payload, row["terminal_code"])
        queue_row = queued.get(row["id"])
        adopted = queue_row is not None and queue_row["outcome"] == "adopted"
        if is_mixed(axis, method) and not adopted:
            # 混切扫描不落库：记进混切队列，等按分区设置采纳后再落。
            if queue_row is None:
                _enqueue(conn, row["id"], row["biz_date"], row["terminal_code"], payload)
                queue_row = _queue_entry(conn, row["id"])
            note = "切面名与分区设置不符，混切扫描不落库，等混切队列采纳后再落"
            conn.execute(
                "INSERT INTO build_rejects (run_id, source_id, reason) VALUES (%s, %s, %s)",
                (run["id"], row["id"], note),
            )
            rejected += 1
            mixed_kept += 1
            continue

        cleaned, reason = clean_scan(payload)
        if reason:
            conn.execute(
                "INSERT INTO build_rejects (run_id, source_id, reason) VALUES (%s, %s, %s)",
                (run["id"], row["id"], reason),
            )
            rejected += 1
            continue

        key = partition_key(method, payload, row["terminal_code"])
        if adopted and queue_row is not None:
            conn.execute(
                "UPDATE mixed_queue SET run_id = %s WHERE id = %s",
                (run["id"], queue_row["id"]),
            )
        conn.execute(
            """
            INSERT INTO dwd_move
                (biz_date, source_id, terminal_code, container_no, event_kind, event_at,
                 partition_key, facet_method)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                biz_date,
                row["id"],
                row["terminal_code"],
                cleaned["container_no"],
                cleaned["event_kind"],
                cleaned["event_at"],
                key,
                method,
            ),
        )
        accepted += 1

    conn.execute(
        "UPDATE build_runs SET accepted_count = %s, rejected_count = %s WHERE id = %s",
        (accepted, rejected, run["id"]),
    )
    return {
        "run_id": run["id"],
        "method": method,
        "method_name": FACETS[method],
        "batch_cut": BATCH_CUTS[method],
        "carried_facet": carried,
        "deviation": deviation,
        "accepted": accepted,
        "rejected": rejected,
        "mixed_kept": mixed_kept,
    }


# ---------------- 视图 ----------------


def _partition_counts(conn):
    rows = conn.execute(
        """
        SELECT biz_date,
               COUNT(DISTINCT partition_key) FILTER (WHERE left(partition_key, 6) = 'event=') AS event_facets,
               COUNT(DISTINCT partition_key) FILTER (WHERE left(partition_key, 8) = 'channel=') AS channel_facets
        FROM dwd_move
        WHERE partition_key IS NOT NULL
        GROUP BY biz_date
        """
    ).fetchall()
    return {
        row["biz_date"].isoformat(): {
            "event_facets": int(row["event_facets"] or 0),
            "channel_facets": int(row["channel_facets"] or 0),
        }
        for row in rows
    }


def _partitions_of(conn, biz_date):
    rows = conn.execute(
        """
        SELECT partition_key, COUNT(*) AS row_count
        FROM dwd_move
        WHERE biz_date = %s AND partition_key IS NOT NULL
        GROUP BY partition_key
        ORDER BY partition_key
        """,
        (biz_date,),
    ).fetchall()
    return [
        {"partition_key": row["partition_key"], "rows": int(row["row_count"])}
        for row in rows
    ]


def _latest_run(conn, biz_date):
    return conn.execute(
        """
        SELECT id, accepted_count, rejected_count, built_by, built_at,
               method, carried_facet, deviation
        FROM build_runs WHERE biz_date = %s ORDER BY id DESC LIMIT 1
        """,
        (biz_date,),
    ).fetchone()


def _day_cut(run):
    if run is None:
        return None, "这一批尚未落库"
    if run["method"] is None:
        return None, "这一批早于分区切面，还没按分区落库"
    method = run["method"]
    return method, BATCH_CUTS.get(method, method)


def _mixed_counts(conn):
    rows = conn.execute(
        """
        SELECT biz_date, COUNT(*) AS n
        FROM mixed_queue GROUP BY biz_date
        """
    ).fetchall()
    return {row["biz_date"].isoformat(): int(row["n"]) for row in rows}


def day_list(conn):
    sync_mixed(conn)
    policy = policy_of(conn)
    method_name = FACETS[policy["method"]] if policy else "未封存"
    counts = _partition_counts(conn)
    mixed = _mixed_counts(conn)
    rows = conn.execute(
        """
        SELECT d.biz_date,
               (SELECT COUNT(*) FROM ods_scan o WHERE o.biz_date = d.biz_date) AS ods_count,
               (SELECT COUNT(*) FROM dwd_move m WHERE m.biz_date = d.biz_date) AS dwd_count,
               EXISTS(SELECT 1 FROM build_runs r WHERE r.biz_date = d.biz_date) AS built
        FROM (SELECT DISTINCT biz_date FROM ods_scan) d
        ORDER BY d.biz_date
        """
    ).fetchall()
    out = []
    for row in rows:
        key = row["biz_date"].isoformat()
        run = _latest_run(conn, key)
        cut_method, cut_label = _day_cut(run)
        facet = counts.get(key, {"event_facets": 0, "channel_facets": 0})
        row["biz_date"] = key
        row["event_facets"] = facet["event_facets"]
        row["channel_facets"] = facet["channel_facets"]
        row["mixed_count"] = mixed.get(key, 0)
        row["method_name"] = FACETS[run["method"]] if run and run["method"] else method_name
        row["batch_cut"] = cut_label
        row["batch_cut_method"] = cut_method
        out.append(row)
    return out


def partition_desk(conn):
    days = day_list(conn)
    policy = policy_of(conn)
    method_name = FACETS[policy["method"]] if policy else "未封存"
    return {
        "settings": settings(conn),
        "days": days,
        "queue": mixed_rows(conn),
        "attempts": attempts(conn),
        "summary": {
            "event_facets": sum(day["event_facets"] for day in days),
            "channel_facets": sum(day["channel_facets"] for day in days),
            "mixed_count": sum(day["mixed_count"] for day in days),
            "method_name": method_name,
        },
    }


def facet_detail(conn, biz_date):
    """切面详情：每条扫描按事件码切落在哪、按通道切落在哪、实际落在哪。"""
    sync_mixed(conn)
    policy = policy_of(conn)
    method = policy["method"] if policy else None
    rows = conn.execute(
        """
        SELECT id, terminal_code, raw_payload
        FROM ods_scan WHERE biz_date = %s ORDER BY id
        """,
        (biz_date,),
    ).fetchall()
    landed = {
        row["source_id"]: row
        for row in conn.execute(
            """
            SELECT source_id, partition_key, facet_method
            FROM dwd_move WHERE biz_date = %s
            """,
            (biz_date,),
        ).fetchall()
    }
    queued = {
        row["ods_id"]: row
        for row in conn.execute(
            "SELECT * FROM mixed_queue WHERE biz_date = %s", (biz_date,)
        ).fetchall()
    }
    run = _latest_run(conn, biz_date)
    cut_method, cut_label = _day_cut(run)

    items = []
    for row in rows:
        payload = _payload(row)
        axis = facet_axis(payload, row["terminal_code"])
        keys = candidate_keys(payload, row["terminal_code"])
        move = landed.get(row["id"])
        queue_row = queued.get(row["id"])
        if axis == "empty":
            status = "empty"
            status_name = "切面名为空，不进混切队列"
        elif method is None:
            status = "unpinned"
            status_name = "分区切面尚未锁死"
        elif axis != method:
            if queue_row and queue_row["outcome"] == "adopted":
                status = "adopted"
                status_name = "混切已按分区设置采纳"
            else:
                status = "mixed"
                status_name = "混切：切面名与分区设置不符，未落库"
        else:
            status = "aligned"
            status_name = f"与分区设置相符（{FACET_SHORTS[method]}）"
        items.append(
            {
                "source_id": row["id"],
                "terminal_code": row["terminal_code"],
                "facet_name": facet_name(payload),
                "facet_axis": axis,
                "facet_axis_name": _axis_name(axis),
                "event_key": keys["event_key"],
                "channel_key": keys["channel_key"],
                "landed_key": move["partition_key"] if move else None,
                "landed_method": move["facet_method"] if move else None,
                "status": status,
                "status_name": status_name,
                "queue_outcome": queue_row["outcome"] if queue_row else None,
            }
        )

    partitions = _partitions_of(conn, biz_date)
    return {
        "biz_date": biz_date,
        "settings": settings(conn),
        "batch_cut": cut_label,
        "batch_cut_method": cut_method,
        "method_name": FACETS[method] if method else "未封存",
        "summary": {
            "event_facets": sum(
                1 for item in partitions if item["partition_key"].startswith("event=")
            ),
            "channel_facets": sum(
                1 for item in partitions if item["partition_key"].startswith("channel=")
            ),
            "mixed_count": len(queued),
            "method_name": FACETS[method] if method else "未封存",
        },
        "partitions": partitions,
        "rows": items,
    }
