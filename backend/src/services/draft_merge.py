"""离线底稿三方合并的纯函数实现。

不依赖数据库与业务配置，便于单元验证：
- 标量字段按 base/server/incoming 三方比较，单边修改自动合入；
- 同一字段双方异改保留双方，交由裁决；
- 停机区间按“相对原始依据被修改的时间范围”合并，修改范围不相交则可合入，
  交叠处若两侧对停机状态的判断不同则保留双方待裁决。
"""
import hashlib
import json

DOWNTIME = "downtime"


def digest(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def normalize(intervals):
    """合并重叠或相接的停机区间，返回按起点排序的 [start, end] 列表。"""
    pieces = sorted((int(a), int(b)) for a, b in intervals)
    merged = []
    for a, b in pieces:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def subtract(left, right):
    """返回 left 中不被 right 覆盖的时间片段（两者均应为已规范化区间）。"""
    out = []
    for start, end in left:
        cursor = start
        for a, b in right:
            if b <= cursor:
                continue
            if a >= end:
                break
            if a > cursor:
                out.append([cursor, min(a, end)])
            cursor = max(cursor, b)
            if cursor >= end:
                break
        if cursor < end:
            out.append([cursor, end])
    return out


def union(*sets):
    return normalize([piece for group in sets for piece in group])


def intersect(left, right):
    out = []
    for a, b in left:
        for c, d in right:
            start, end = max(a, c), min(b, d)
            if start < end:
                out.append([start, end])
    return normalize(out)


def _restricted(intervals, region):
    return intersect(intervals, region)


def merge_downtime(base, server, incoming):
    """三方合并停机区间。

    返回 (merged, conflict)。merged 为 None 表示需要裁决；conflict 给出
    原始依据、服务端、回传三方区间以及双方修改范围和交叠范围。
    """
    b, s, i = normalize(base), normalize(server), normalize(incoming)
    if i == b:
        return s, None
    if s == b or s == i:
        return i, None
    added_s, removed_s = subtract(s, b), subtract(b, s)
    added_i, removed_i = subtract(i, b), subtract(b, i)
    edits_s, edits_i = union(added_s, removed_s), union(added_i, removed_i)
    overlap = intersect(edits_s, edits_i)
    span = union(edits_s, edits_i)
    # 修改范围交叠，且双方在合并后的修改范围内停机状态不一致 -> 交叠异改。
    disputed = bool(overlap) and _restricted(s, span) != _restricted(i, span)
    if disputed:
        return None, {
            "base": b,
            "server": s,
            "incoming": i,
            "server_edits": edits_s,
            "incoming_edits": edits_i,
            "overlap": overlap,
            "span": span,
        }
    merged = union(
        subtract(b, union(removed_s, removed_i)),
        added_s,
        added_i,
    )
    return merged, None


def normalize_record(data):
    if not isinstance(data, dict):
        return data
    copy = dict(data)
    if isinstance(copy.get(DOWNTIME), list):
        copy[DOWNTIME] = normalize(copy[DOWNTIME])
    return copy


def records_equal(left, right):
    return normalize_record(left) == normalize_record(right)


def _conflict(kind, base, server, incoming, candidate, message, fields=None, downtime=None):
    return {
        "kind": kind,
        "base": base,
        "server": server,
        "incoming": incoming,
        "candidate": candidate,
        "fields": fields or [],
        "downtime": downtime,
        "message": message,
    }


def _result(action, data=None, changed=None, conflict=None):
    return {
        "action": action,
        "data": data,
        "changed_fields": changed or [],
        "conflict": conflict,
    }


def merge_record(base, server, incoming):
    """三方合并一条底稿。

    base: 离线时取回的原始依据；None 表示新增依据。
    server: 当前库内底稿；None 表示已被删除。
    incoming: 回传结果；None 表示回传撤销/删除。
    """
    if base is None:
        if incoming is None:
            return _result("noop")
        if server is None:
            return _result("create", dict(incoming), sorted(incoming))
        if records_equal(server, incoming):
            return _result("noop")
        return _result(
            "conflict",
            None,
            conflict=_conflict(
                "add-collision",
                None,
                server,
                incoming,
                None,
                "同一自然键已在库内存在且内容不同，双方底稿均保留。",
            ),
        )

    if incoming is None:
        if server is None:
            return _result("noop")
        if records_equal(server, base):
            return _result("delete", None, sorted(base))
        return _result(
            "conflict",
            None,
            conflict=_conflict(
                "delete-modify",
                base,
                server,
                None,
                None,
                "回传撤销，但库内该底稿已被修改；撤销与修改双方均保留。",
            ),
        )

    if server is None:
        if records_equal(incoming, base):
            # 离线期间未改动，库内撤销生效，不复活底稿。
            return _result("noop")
        return _result(
            "conflict",
            None,
            conflict=_conflict(
                "delete-modify",
                base,
                None,
                incoming,
                dict(incoming),
                "回传修改，但库内该底稿已被撤销；撤销与修改双方均保留。",
            ),
        )

    if records_equal(server, incoming):
        return _result("noop")

    merged = dict(server)
    field_conflicts = []
    for field in sorted(set(base) | set(server) | set(incoming)):
        if field == DOWNTIME:
            continue
        b, s, i = base.get(field), server.get(field), incoming.get(field)
        if i == b:
            continue
        if s == b or s == i:
            merged[field] = i
        else:
            field_conflicts.append({"field": field, "base": b, "server": s, "incoming": i})
            merged[field] = s

    downtime_conflict = None
    if DOWNTIME in (set(base) | set(server) | set(incoming)):
        b = base.get(DOWNTIME, [])
        s = server.get(DOWNTIME, [])
        i = incoming.get(DOWNTIME, [])
        merged_downtime, downtime_conflict = merge_downtime(b, s, i)
        merged[DOWNTIME] = s if downtime_conflict else merged_downtime

    if field_conflicts or downtime_conflict:
        return _result(
            "conflict",
            merged,
            conflict=_conflict(
                "field",
                base,
                server,
                incoming,
                merged,
                "同一字段双方给出不同更正，或停机修改范围交叠且结果不一致，双方取值均保留。",
                fields=field_conflicts,
                downtime=downtime_conflict,
            ),
        )

    changed = sorted(k for k in set(merged) | set(server) if merged.get(k) != server.get(k))
    if merged == server:
        return _result("noop")
    return _result("update", merged, changed)


def downtime_union(server, incoming):
    """裁决“保留双方”时把两侧停机区间并集作为落地值。"""
    return union(normalize(server or []), normalize(incoming or []))
