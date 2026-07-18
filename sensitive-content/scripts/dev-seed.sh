#!/usr/bin/env bash
# 本地联调造数脚本：创建租户敏感类型/规则，并模拟命中事件（含 session_id）。
# 幂等：类型/规则按 code/pattern 查重后创建；命中事件使用确定性 UUID，
# 重复执行时服务端 ON CONFLICT DO NOTHING 自动去重。
#
# 用法：
#   ./dev-seed.sh                # 默认连 http://localhost:8091，租户 1
#   BASE_URL=... TENANT=... ADMIN_TOKEN=... RUNTIME_TOKEN=... ./dev-seed.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-$SCRIPT_DIR/../.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

BASE_URL="${BASE_URL:-http://localhost:8091}" \
TENANT="${TENANT:-1}" \
ADMIN_TOKEN="${ADMIN_TOKEN:-dev-admin}" \
RUNTIME_TOKEN="${RUNTIME_TOKEN:-dev-runtime}" \
"$PYTHON" - <<'PYEOF'
"""通过管理/内部 API 造联调数据（仅用标准库，无额外依赖）。"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = os.environ["BASE_URL"].rstrip("/")
TENANT = os.environ["TENANT"]
ADMIN = {"Authorization": f"Bearer {os.environ['ADMIN_TOKEN']}"}
RUNTIME = {"Authorization": f"Bearer {os.environ['RUNTIME_TOKEN']}"}
# 命中事件 event_id 的确定性命名空间：保证脚本重复执行不产生重复事件
NS = uuid.uuid5(uuid.NAMESPACE_URL, "hiclaw/sensitive-content/dev-seed")


def call(method: str, path: str, headers: dict, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(f"{BASE}{path}", data=data, method=method,
                  headers={**headers, "Content-Type": "application/json"})
    try:
        with urlopen(req) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def fail(msg, detail):
    print(f"FAILED: {msg}: {detail}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 1. 类型：全局种子类型直接查表；租户 1 补两个自有类型
# ---------------------------------------------------------------------------
status, data = call("GET", f"/api/v1/tenants/{TENANT}/sensitive-types?page_size=200", ADMIN)
if status != 200:
    fail("list types", data)
types_by_code = {t["code"]: t for t in data["items"]}

tenant_types = [
    {"code": "medical-ad", "name": "医疗违规宣传", "action": "FIXED_REPLY",
     "action_config": {"reply_text": "该内容涉及违规医疗宣传，无法回答。"},
     "priority": 70, "description": "夸大疗效/特效药类宣传话术"},
    {"code": "competitor", "name": "竞品信息", "action": "LOG_ONLY",
     "action_config": {}, "priority": 20, "description": "提及竞品仅记录不拦截"},
]
for t in tenant_types:
    if t["code"] in types_by_code:
        print(f"type exists: {t['code']} (id={types_by_code[t['code']]['id']})")
        continue
    status, created = call("POST", f"/api/v1/tenants/{TENANT}/sensitive-types", ADMIN, t)
    if status != 201:
        fail(f"create type {t['code']}", created)
    types_by_code[t["code"]] = created
    print(f"type created: {t['code']} (id={created['id']})")

tid = lambda code: types_by_code[code]["id"]

# ---------------------------------------------------------------------------
# 2. 规则：text 与 regex 各若干条，挂在全局类型与租户类型上
# ---------------------------------------------------------------------------
status, data = call("GET", f"/api/v1/tenants/{TENANT}/sensitive-rules?page_size=500", ADMIN)
if status != 200:
    fail("list rules", data)
rules_by_pattern = {r["pattern"]: r for r in data["items"]}

rules = [
    {"type_id": tid("politics"), "pattern": "违禁词A", "match_mode": "text",
     "description": "演示：涉政文本词"},
    {"type_id": tid("violence"), "pattern": "爆炸装置", "match_mode": "text",
     "description": "演示：暴恐文本词"},
    {"type_id": tid("privacy"), "pattern": r"1[3-9]\d{9}", "match_mode": "regex",
     "description": "演示：手机号正则（脱敏放行）"},
    {"type_id": tid("privacy"), "pattern": r"\d{17}[\dXx]", "match_mode": "regex",
     "description": "演示：身份证号正则（脱敏放行）"},
    {"type_id": tid("medical-ad"), "pattern": "包治百病", "match_mode": "text",
     "description": "演示：违规医疗宣传词（固定回复）"},
    {"type_id": tid("medical-ad"), "pattern": "特效药", "match_mode": "text",
     "description": "演示：违规医疗宣传词（固定回复）"},
    {"type_id": tid("competitor"), "pattern": "友商产品X", "match_mode": "text",
     "description": "演示：竞品词（仅记录）"},
]
for r in rules:
    if r["pattern"] in rules_by_pattern:
        print(f"rule exists: {r['pattern']} (id={rules_by_pattern[r['pattern']]['id']})")
        continue
    status, created = call("POST", f"/api/v1/tenants/{TENANT}/sensitive-rules", ADMIN, r)
    if status != 201:
        fail(f"create rule {r['pattern']}", created)
    rules_by_pattern[r["pattern"]] = created
    print(f"rule created: {r['pattern']} (id={created['id']})")

rule = lambda p: rules_by_pattern[p]

# ---------------------------------------------------------------------------
# 3. 命中事件：4 个会话、分散在最近 7 天，覆盖不同规则/行为组合
# ---------------------------------------------------------------------------
now = datetime.now(timezone.utc)
action_of = {r["pattern"]: types_by_code[[c for c in types_by_code
              if types_by_code[c]["id"] == r["type_id"]][0]]["action"]
             for r in rules_by_pattern.values() if r["pattern"] in
             {x["pattern"] for x in rules}}

# (会话, 规则 pattern, 距今天数, 当次请求内命中数)
plan = [
    ("sess-demo-001", "违禁词A", 0.1, 1),
    ("sess-demo-001", "特效药", 0.2, 2),
    ("sess-demo-001", "1[3-9]\\d{9}", 0.5, 1),
    ("sess-demo-002", "包治百病", 1.0, 1),
    ("sess-demo-002", "特效药", 1.2, 1),
    ("sess-demo-002", "友商产品X", 1.5, 3),
    ("sess-demo-003", "爆炸装置", 2.0, 1),
    ("sess-demo-003", "违禁词A", 2.5, 1),
    ("sess-demo-003", "\\d{17}[\\dXx]", 3.0, 1),
    ("sess-demo-004", "1[3-9]\\d{9}", 4.0, 2),
    ("sess-demo-004", "友商产品X", 5.0, 1),
    ("sess-demo-004", "违禁词A", 6.5, 1),
]

events = []
for i, (sess, pattern, days_ago, hits) in enumerate(plan):
    r = rule(pattern)
    act = action_of.get(pattern, "LOG_ONLY")
    events.append({
        # uuid5 确定性生成：同一条计划永远同一个 event_id，实现幂等
        "event_id": str(uuid.uuid5(NS, f"{TENANT}/{i}/{sess}/{pattern}")),
        "rule_id": r["id"],
        "type_id": r["type_id"],
        "rule_action": act,
        "final_action": act,
        "selected": True,
        "final_rule_id": r["id"],
        "tenant_id": TENANT,
        "request_fingerprint": f"req-fp-{i:04d}",
        "session_fingerprint": f"sess-fp-{sess}",
        "session_id": sess,
        "policy_version": "global-1:tenant-1",
        "hit_count": hits,
        "hit_at": (now - timedelta(days=days_ago)).isoformat(),
    })

status, result = call("POST", "/internal/v1/hit-events:batch", RUNTIME, {"events": events})
if status != 200:
    fail("post hit events", result)
print(f"hit events: accepted={result['accepted']} duplicates={result['duplicates']}")
print("seed done.")
PYEOF
