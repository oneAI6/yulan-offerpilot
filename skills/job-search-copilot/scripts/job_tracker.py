#!/usr/bin/env python3
"""Private local evidence ledger. This program never contacts recruiting sites."""
import argparse
import csv
import hashlib
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

INTENTS = ("greet", "inquire", "apply", "followup")
SCHEMA = """
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, platform TEXT NOT NULL,
 url TEXT NOT NULL, external_id TEXT, company TEXT NOT NULL, title TEXT NOT NULL,
 stage TEXT NOT NULL DEFAULT 'discovered', created_at TEXT NOT NULL
);
CREATE TABLE criteria (
 job_id TEXT NOT NULL REFERENCES jobs(id), name TEXT NOT NULL, status TEXT NOT NULL,
 note TEXT NOT NULL, source TEXT, evidence TEXT, checked_at TEXT NOT NULL, rule_hash TEXT NOT NULL,
 PRIMARY KEY (job_id,name)
);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), intent TEXT NOT NULL,
 outcome TEXT NOT NULL, note TEXT NOT NULL, result_note TEXT, evidence TEXT,
 resume TEXT, profile_hash TEXT NOT NULL, authorization TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE events (
 id INTEGER PRIMARY KEY, job_id TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL,
 created_at TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def canonical_url(url):
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("https", "http") or not parts.netloc:
        raise ValueError("职位来源必须是有效 http/https URL")
    if parts.username or parts.password:
        raise ValueError("职位URL不能包含认证凭据")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() != "trackingid"]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                       urlencode(sorted(query)), parts.fragment))


def receipt(path):
    p = Path(path).expanduser().resolve()
    if not p.is_file() or not p.stat().st_size:
        raise ValueError("凭证/简历必须是存在的非空文件")
    return {"path": str(p), "sha256": digest(p.read_bytes())}


def intact(value):
    if not value:
        return False
    try:
        data = json.loads(value)
        return receipt(data["path"])["sha256"] == data["sha256"]
    except (ValueError, OSError, KeyError, TypeError):
        return False


def validate_profile(profile):
    auth = profile.get("authorization", {})
    if auth.get("mode") not in ("draft", "inquire", "apply"):
        raise ValueError("authorization.mode 必须为 draft / inquire / apply")
    platforms = auth.get("platforms")
    if not isinstance(platforms, list) or any(not isinstance(p, str) or not p.strip() for p in platforms):
        raise ValueError("authorization.platforms 必须为具体平台字符串列表")
    if auth["mode"] != "draft" and (not platforms or not auth.get("scope_note", "").strip()):
        raise ValueError("外发模式需要平台范围和用户授权依据 scope_note")
    if not isinstance(auth.get("allow_unverified_application", False), bool):
        raise ValueError("allow_unverified_application 必须为布尔值")
    rules = profile.get("criteria")
    if not isinstance(rules, dict) or not rules:
        raise ValueError("至少配置一项筛选条件")
    for name, rule in rules.items():
        if not isinstance(rule, dict) or not isinstance(rule.get("required"), bool) or not rule.get("rule"):
            raise ValueError("条件需要 required 布尔值与 rule 说明：" + name)
    followup = profile.get("followup", {})
    for name, default in (("minimum_hours", 48), ("max_confirmed", 1)):
        value = followup.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError("跟进设置必须为非负数：" + name)
    return profile


def load(workspace):
    profile_path = workspace / "profile.json"
    if not profile_path.exists() or not (workspace / "ledger.sqlite3").exists():
        raise ValueError("请先使用 init 建立私有工作区")
    raw = profile_path.read_bytes()
    profile = validate_profile(json.loads(raw))
    db = sqlite3.connect(workspace / "ledger.sqlite3")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db, profile, digest(raw)


def job(db, job_id):
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("职位不存在：" + job_id)
    return row


def event(db, job_id, kind, detail):
    db.execute("INSERT INTO events(job_id,kind,detail,created_at) VALUES(?,?,?,?)",
               (job_id, kind, serialized(detail), now()))


def condition_states(db, profile, job_id):
    rows = {r["name"]: r for r in db.execute("SELECT * FROM criteria WHERE job_id=?", (job_id,))}
    states = {}
    for name in profile["criteria"]:
        row = rows.get(name)
        status = row["status"] if row else "unknown"
        rule_hash = digest(serialized(profile["criteria"][name]).encode())
        if row and row["rule_hash"] != rule_hash:
            status = "unknown"
        elif status == "pass" and not intact(row["evidence"]):
            status = "unknown"
        states[name] = status
    return states


def evaluate(db, profile, job_id, intent, contains_resume=False):
    row = job(db, job_id)
    states = condition_states(db, profile, job_id)
    required = [n for n, r in profile["criteria"].items() if r["required"]]
    failed = [n for n in required if states[n] == "fail"]
    unknown = [n for n in required if states[n] == "unknown"]
    reasons = []
    if row["stage"] == "closed":
        reasons.append("职位已关闭，停止外发")
    auth = profile["authorization"]
    applying = intent == "apply" or contains_resume
    if auth["mode"] == "draft" or (applying and auth["mode"] != "apply"):
        reasons.append("当前档案没有对应外发授权")
    if row["platform"] not in [p.casefold().strip() for p in auth["platforms"]]:
        reasons.append("平台不在授权范围")
    if failed:
        reasons.append("硬条件明确不满足：" + ", ".join(failed))
    if applying and unknown and not auth.get("allow_unverified_application", False):
        reasons.append("申请前需核实硬条件：" + ", ".join(unknown))
    attempts = list(db.execute("SELECT * FROM attempts WHERE job_id=?", (job_id,)))
    if any(a["outcome"] in ("pending", "uncertain") for a in attempts):
        reasons.append("有未解决的 pending/uncertain 动作，先查历史")
    if intent != "followup" and any(a["intent"] == intent and a["outcome"] == "confirmed" for a in attempts):
        reasons.append("相同初次外发已确认成功，不重复发送")
    if intent == "followup":
        confirmed = [a for a in attempts if a["outcome"] == "confirmed"]
        settings = profile.get("followup", {})
        if not confirmed:
            reasons.append("没有已确认的首次触达")
        else:
            count = sum(a["intent"] == "followup" for a in confirmed)
            if count >= settings.get("max_confirmed", 1):
                reasons.append("已达到跟进次数上限")
            last = max(datetime.fromisoformat(a["updated_at"]) for a in confirmed)
            age = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            if age < settings.get("minimum_hours", 48):
                reasons.append("尚未达到跟进最短间隔")
    return {"allowed": not reasons, "job_id": job_id, "intent": intent,
            "criteria": states, "failed_required": failed, "unknown_required": unknown,
            "reasons": reasons, "fully_matched": bool(required) and not failed and not unknown}


def run(args):
    workspace = Path(args.workspace).expanduser().resolve()
    if args.command == "init":
        if (workspace / "profile.json").exists() or (workspace / "ledger.sqlite3").exists():
            raise ValueError("已有私有工作区，不会覆盖")
        profile = validate_profile(json.loads(Path(args.profile).read_text()))
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            workspace.chmod(0o700)
        except OSError:
            pass
        (workspace / "profile.json").write_text(serialized(profile) + "\n", encoding="utf-8")
        for folder in ("inputs", "resumes", "evidence", "exports"):
            (workspace / folder).mkdir(exist_ok=True)
        with sqlite3.connect(workspace / "ledger.sqlite3") as db:
            db.executescript(SCHEMA)
        return {"workspace": str(workspace), "mode": profile["authorization"]["mode"]}
    db, profile, profile_hash = load(workspace)
    try:
        with db:
            if args.command == "add":
                platform = args.platform.strip().casefold()
                if not platform:
                    raise ValueError("平台不能为空")
                url = canonical_url(args.url)
                identity = args.external_id.strip() if args.external_id else url
                key = digest(serialized([platform, identity]).encode())
                existing = db.execute("SELECT id FROM jobs WHERE fingerprint=?", (key,)).fetchone()
                if not existing:
                    same_url = list(db.execute("SELECT id,external_id FROM jobs WHERE platform=? AND url=?", (platform, url)))
                    if args.external_id:
                        existing = next((r for r in same_url if not r["external_id"]), None)
                        if existing:
                            db.execute("UPDATE jobs SET external_id=?,fingerprint=? WHERE id=?", (args.external_id, key, existing["id"]))
                    elif len(same_url) == 1:
                        existing = same_url[0]
                    elif len(same_url) > 1:
                        raise ValueError("同一URL已有不同职位ID，请使用明确 external-id")
                if existing:
                    return {"job_id": existing["id"], "duplicate": True}
                job_id = "j_" + key[:16]
                db.execute("INSERT INTO jobs(id,fingerprint,platform,url,external_id,company,title,created_at) VALUES(?,?,?,?,?,?,?,?)",
                           (job_id, key, platform, url, args.external_id, args.company.strip(), args.title.strip(), now()))
                similar = [r["id"] for r in db.execute("SELECT id FROM jobs WHERE lower(company)=lower(?) AND lower(title)=lower(?) AND id!=?",
                                                       (args.company.strip(), args.title.strip(), job_id))]
                event(db, job_id, "discovered", {"url": url})
                return {"job_id": job_id, "duplicate": False, "possible_duplicates": similar}
            if args.command == "criterion":
                job(db, args.job_id)
                if args.name not in profile["criteria"]:
                    raise ValueError("条件名未在档案中定义")
                proof = receipt(args.evidence) if args.evidence else None
                if args.status != "unknown" and proof is None:
                    raise ValueError("pass/fail 需要非空证据文件")
                rule = profile["criteria"][args.name]
                db.execute("INSERT OR REPLACE INTO criteria VALUES(?,?,?,?,?,?,?,?)",
                           (args.job_id, args.name, args.status, args.note, args.source,
                            serialized(proof) if proof else None, now(), digest(serialized(rule).encode())))
                event(db, args.job_id, "criterion", {"name": args.name, "status": args.status,
                      "note": args.note, "source": args.source, "evidence": proof, "rule": rule})
                return {"job_id": args.job_id, "name": args.name, "status": args.status}
            if args.command == "check":
                return evaluate(db, profile, args.job_id, args.intent)
            if args.command == "prepare":
                db.execute("BEGIN IMMEDIATE")
                has_resume = bool(args.resume or args.resume_version)
                if has_resume and args.intent in ("greet", "inquire"):
                    raise ValueError("附简历应登记 apply 或带附件的 followup")
                if args.intent == "apply" and not has_resume:
                    raise ValueError("申请需要 --resume 或已核对的 --resume-version")
                result = evaluate(db, profile, args.job_id, args.intent, has_resume)
                if not result["allowed"]:
                    return {**result, "prepared": False}
                resume = receipt(args.resume) if args.resume else ({"version": args.resume_version} if args.resume_version else None)
                attempt_id = "a_" + uuid.uuid4().hex[:16]
                timestamp = now()
                db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (attempt_id, args.job_id, args.intent, "pending", args.note, None, None,
                            serialized(resume) if resume else None, profile_hash,
                            serialized(profile["authorization"]), timestamp, timestamp))
                event(db, args.job_id, "prepared", {"attempt_id": attempt_id, "intent": args.intent})
                return {**result, "prepared": True, "attempt_id": attempt_id, "resume": resume}
            if args.command == "finish":
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM attempts WHERE id=?", (args.attempt_id,)).fetchone()
                if row is None or row["outcome"] not in ("pending", "uncertain"):
                    raise ValueError("只允许核实 pending/uncertain 动作")
                proof = receipt(args.evidence) if args.evidence else None
                if args.outcome != "uncertain" and proof is None:
                    raise ValueError("成功或确定失败需要非空凭证；单纯超时应标 uncertain")
                db.execute("UPDATE attempts SET outcome=?,result_note=?,evidence=?,updated_at=? WHERE id=?",
                           (args.outcome, args.note, serialized(proof) if proof else None, now(), args.attempt_id))
                event(db, row["job_id"], "result", {"attempt_id": args.attempt_id, "outcome": args.outcome, "note": args.note})
                return {"attempt_id": args.attempt_id, "outcome": args.outcome}
            if args.command == "stage":
                job(db, args.job_id)
                proof = receipt(args.evidence)
                db.execute("UPDATE jobs SET stage=? WHERE id=?", (args.value, args.job_id))
                event(db, args.job_id, "stage", {"value": args.value, "note": args.note, "evidence": proof})
                return {"job_id": args.job_id, "stage": args.value}
            rows = [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created_at,id")]
            for row in rows:
                row.pop("fingerprint")
                row["criteria"] = condition_states(db, profile, row["id"])
                row["attempts"] = [dict(a) for a in db.execute("SELECT id,intent,outcome,resume,created_at,updated_at FROM attempts WHERE job_id=?", (row["id"],))]
            if args.command == "list":
                return rows
            if args.command == "summary":
                counts = {i: 0 for i in INTENTS}
                pending = uncertain = matched = 0
                required = [n for n, r in profile["criteria"].items() if r["required"]]
                for row in rows:
                    matched += bool(required) and all(row["criteria"][n] == "pass" for n in required)
                    for a in row["attempts"]:
                        pending += a["outcome"] == "pending"
                        uncertain += a["outcome"] == "uncertain"
                        if a["outcome"] == "confirmed":
                            counts[a["intent"]] += 1
                return {"jobs": len(rows), "fully_matched": matched, "confirmed_actions": counts,
                        "pending": pending, "uncertain": uncertain,
                        "stages": {s: sum(r["stage"] == s for r in rows) for s in ("replied", "interviewing", "offer", "closed")}}
            if args.command == "export":
                path = Path(args.output).expanduser().resolve()
                if path.exists():
                    raise ValueError("导出目标已存在，请换一个文件名以保留旧记录")
                path.parent.mkdir(parents=True, exist_ok=True)
                if args.format == "json":
                    path.write_text(serialized(rows) + "\n", encoding="utf-8")
                else:
                    def safe(value):
                        text = str(value)
                        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
                    with path.open("w", encoding="utf-8-sig", newline="") as stream:
                        writer = csv.writer(stream)
                        writer.writerow(("job_id", "platform", "company", "title", "url", "stage", "criteria", "attempts"))
                        for row in rows:
                            writer.writerow([safe(row[k]) for k in ("id", "platform", "company", "title", "url", "stage")] +
                                            [safe(serialized(row["criteria"])), safe(serialized(row["attempts"]))])
                return {"output": str(path), "jobs": len(rows)}
    finally:
        db.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", default=".private/job-search")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("init"); q.add_argument("--profile", required=True)
    q = sub.add_parser("add")
    for flag in ("platform", "url", "company", "title"):
        q.add_argument("--" + flag, required=True)
    q.add_argument("--external-id")
    q = sub.add_parser("criterion"); q.add_argument("job_id")
    q.add_argument("--name", required=True); q.add_argument("--status", choices=("pass", "fail", "unknown"), required=True)
    q.add_argument("--note", required=True); q.add_argument("--source"); q.add_argument("--evidence")
    for name in ("check", "prepare"):
        q = sub.add_parser(name); q.add_argument("job_id"); q.add_argument("--intent", choices=INTENTS, required=True)
        if name == "prepare":
            q.add_argument("--note", required=True)
            group = q.add_mutually_exclusive_group(); group.add_argument("--resume"); group.add_argument("--resume-version")
    q = sub.add_parser("finish"); q.add_argument("attempt_id")
    q.add_argument("--outcome", choices=("confirmed", "failed", "uncertain"), required=True)
    q.add_argument("--note", required=True); q.add_argument("--evidence")
    q = sub.add_parser("stage"); q.add_argument("job_id")
    q.add_argument("--value", choices=("replied", "interviewing", "offer", "closed"), required=True)
    q.add_argument("--note", required=True); q.add_argument("--evidence", required=True)
    sub.add_parser("list"); sub.add_parser("summary")
    q = sub.add_parser("export"); q.add_argument("--format", choices=("json", "csv"), required=True)
    q.add_argument("--output", required=True)
    return p


def main(argv=None):
    try:
        result = run(parser().parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get("allowed") is False else 0
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
