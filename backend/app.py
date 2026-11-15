import os

from flask import Flask, jsonify, request

from auth import authenticate, issue_token, read_user
from db import (
    BadMethod,
    FacetNotPinned,
    PolicySealed,
    adopt_mixed,
    connect,
    day_list,
    facet_detail,
    init_db,
    land_scan,
    mixed_rows,
    partition_desk,
    pin_policy,
    rebuild_day,
    settings,
)
from partition import FACETS

app = Flask(__name__)
app.json.ensure_ascii = False


def _stamp(value):
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="minutes")


def _user():
    return read_user(request.headers.get("Authorization"))


def _writer():
    user = _user()
    if not user:
        return None, (jsonify({"detail": "未登录"}), 401)
    if user["role"] != "writer":
        return None, (jsonify({"detail": "核对员不能改分区切面，也不能构建明细层"}), 403)
    return user, None


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return jsonify({"status": "ok", "service": "gate-event-lake"})


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not user:
        return jsonify({"detail": "用户名或密码错误"}), 401
    return jsonify(
        {
            "access_token": issue_token(user),
            "username": user["username"],
            "role": user["role"],
            "title": user["title"],
        }
    )


@app.get("/api/dates")
def list_dates():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        return jsonify(day_list(conn))


@app.get("/api/dates/<biz_date>")
def date_detail(biz_date):
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        ods = conn.execute(
            """
            SELECT id, terminal_code, raw_payload
            FROM ods_scan WHERE biz_date = %s ORDER BY id
            """,
            (biz_date,),
        ).fetchall()
        if not ods:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        moves = conn.execute(
            """
            SELECT source_id, terminal_code, container_no, event_kind, event_at,
                   partition_key, facet_method
            FROM dwd_move WHERE biz_date = %s ORDER BY event_at, id
            """,
            (biz_date,),
        ).fetchall()
        run = conn.execute(
            """
            SELECT id, accepted_count, rejected_count, built_by, built_at,
                   method, carried_facet, deviation
            FROM build_runs WHERE biz_date = %s ORDER BY id DESC LIMIT 1
            """,
            (biz_date,),
        ).fetchone()
        rejects = []
        if run:
            rejects = conn.execute(
                """
                SELECT source_id, reason FROM build_rejects
                WHERE run_id = %s ORDER BY source_id
                """,
                (run["id"],),
            ).fetchall()
        detail = facet_detail(conn, biz_date)
    for move in moves:
        move["event_at"] = _stamp(move["event_at"])
    if run:
        run["built_at"] = _stamp(run["built_at"])
        run["method_name"] = FACETS.get(run["method"]) if run["method"] else None
    return jsonify(
        {
            "biz_date": biz_date,
            "ods": ods,
            "moves": moves,
            "run": run,
            "rejects": rejects,
            "facets": detail,
        }
    )


@app.get("/api/partitions")
def partitions():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        return jsonify(partition_desk(conn))


@app.put("/api/partitions/settings")
def set_partition_settings():
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    with connect() as conn:
        try:
            view = pin_policy(conn, body.get("method"), user["username"])
        except BadMethod as exc:
            return jsonify({"detail": str(exc)}), 422
        except PolicySealed as exc:
            return jsonify({"detail": str(exc), "settings": settings(conn)}), 409
        conn.commit()
        return jsonify(view)


@app.get("/api/mixed")
def mixed():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    biz_date = request.args.get("biz_date") or None
    with connect() as conn:
        return jsonify({"queue": mixed_rows(conn, biz_date)})


@app.post("/api/mixed/adopt")
def adopt():
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    biz_date = str(body.get("biz_date") or "").strip()
    carried = body.get("facet", body.get("method"))
    if not biz_date:
        return jsonify({"detail": "没指定业务日"}), 422
    with connect() as conn:
        result, problem = adopt_mixed(conn, biz_date, carried, user["username"])
        conn.commit()
    if problem:
        status, message = problem
        return jsonify({"detail": message}), status
    return jsonify(result)


@app.post("/api/scans")
def land():
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    biz_date = str(body.get("biz_date") or "").strip()
    payload = body.get("payload")
    if not biz_date or not isinstance(payload, dict):
        return jsonify({"detail": "业务日和扫描原文都要带"}), 422
    terminal = str(payload.get("gate") or body.get("terminal_code") or "").strip()
    if not terminal:
        return jsonify({"detail": "扫描原文缺闸口通道"}), 422
    with connect() as conn:
        ods_id = land_scan(conn, biz_date, terminal, payload)
        conn.commit()
        detail = facet_detail(conn, biz_date)
    row = next(item for item in detail["rows"] if item["source_id"] == ods_id)
    return jsonify({"source_id": ods_id, "facet": row, "detail": detail})


@app.post("/api/dates/<biz_date>/build")
def build_date(biz_date):
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    carried = body.get("method", body.get("facet"))
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ods_scan WHERE biz_date = %s LIMIT 1",
            (biz_date,),
        ).fetchone()
        if not exists:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        try:
            summary = rebuild_day(conn, biz_date, user["username"], carried_method=carried)
        except FacetNotPinned as exc:
            return jsonify({"detail": str(exc)}), 409
        except BadMethod as exc:
            return jsonify({"detail": str(exc)}), 422
        conn.commit()
    payload = date_detail(biz_date)
    body = payload.get_json()
    body["summary"] = summary
    return jsonify(body)


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
