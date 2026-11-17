import time
from collections import Counter

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.models.merge import MergeBatch, MergeItem, MergeLock
from src.models.source import Period, Source
from src.services.audit import log
from src.services.draft_merge import (
    DOWNTIME,
    digest,
    downtime_union,
    merge_record,
    normalize,
    normalize_record,
    records_equal,
)
from src.services.validation import validate_source

MAX_ITEMS = 500
LOCK_ID = 1


# ---------------------------------------------------------------- 基础工具

def _batch_lock(db):
    """获取批次落地/裁决用的单行锁，使落地在核算看来整批可见或整批不可见。"""
    if not db.get(MergeLock, LOCK_ID):
        db.add(MergeLock(id=LOCK_ID))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
    return db.query(MergeLock).filter_by(id=LOCK_ID).with_for_update().first()

def _source_by_key(db, key, for_update=False):
    query = db.query(Source).filter_by(natural_key=key)
    if for_update:
        # 加锁读总是读取最新已提交版本，避免 REPEATABLE READ 快照看不到
        # 刚落地批次写入的底稿，从而用旧值覆盖对方更正。
        query = query.with_for_update()
    return query.first()


def _alternative_key(db, item):
    # 另立底稿的自然键带批次条目编号，避免与真实键或既有补录键冲突。
    suffix = '~merge-' + str(item.batch_id) + '-' + str(item.id)
    base = item.natural_key[:max(1, 120 - len(suffix))] + suffix
    key, attempt = base, 1
    while _source_by_key(db, key):
        attempt += 1
        extra = '-' + str(attempt)
        key = base[:120 - len(extra)] + extra
    return key[:120]


def _check_constraints(data):
    """合并/裁决后的业务校验：字段合法、班次正时长、产量与停机约束。"""
    validate_source(data)
    operating = data['end'] - data['start']
    kept = 0
    for a, b in normalize(data['downtime']):
        a, b = max(a, data['start']), min(b, data['end'])
        if b > a:
            kept += b - a
    operating -= kept
    if data['total'] > 0 and operating <= 0:
        raise ValueError('合并后班次全部停机却仍有产量，请修正停机区间或产量')
    return data


def _differing_fields(base, server, incoming):
    rows = []
    for field in sorted(set(base or {}) | set(server or {}) | set(incoming or {})):
        if field == DOWNTIME:
            continue
        b = (base or {}).get(field)
        s = (server or {}).get(field)
        i = (incoming or {}).get(field)
        if s != i:
            rows.append({'field': field, 'base': b, 'server': s, 'incoming': i})
    return rows


def _downtime_conflict(base, server, incoming, edits_s, edits_i, overlap):
    return {
        'base': normalize((base or {}).get(DOWNTIME, [])),
        'server': normalize((server or {}).get(DOWNTIME, [])),
        'incoming': normalize((incoming or {}).get(DOWNTIME, [])),
        'server_edits': edits_s,
        'incoming_edits': edits_i,
        'overlap': overlap,
    }


def _constraint_conflict(base, server, incoming, candidate, message):
    return {
        'kind': 'constraint',
        'base': base,
        'server': server,
        'incoming': incoming,
        'candidate': candidate,
        'fields': _differing_fields(base, server, incoming),
        'downtime': _downtime_conflict(
            base, server, incoming, [], [], []
        ) if (server or {}).get(DOWNTIME, []) != (incoming or {}).get(DOWNTIME, []) else None,
        'message': message,
    }


def _guard_result(base, server, incoming, outcome):
    """对自动合并出的新值仍做产量与停机约束校验。"""
    if outcome['action'] not in ('create', 'update') or outcome['data'] is None:
        return outcome
    try:
        _check_constraints(outcome['data'])
        return outcome
    except ValueError as exc:
        conflict = _constraint_conflict(base, server, incoming, outcome['data'], str(exc))
        return {
            'action': 'conflict',
            'data': outcome['data'],
            'changed_fields': [],
            'conflict': conflict,
        }


# ---------------------------------------------------------------- 批次读取

def snapshot_item(item):
    return {
        'id': item.id,
        'batch_id': item.batch_id,
        'ordinal': item.ordinal,
        'natural_key': item.natural_key,
        'op': item.op,
        'base': item.base,
        'base_digest': item.base_digest,
        'incoming': item.incoming,
        'server': item.server,
        'server_digest': item.server_digest,
        'result': item.result,
        'status': item.status,
        'conflict': item.conflict,
        'message': item.message,
        'applied_id': item.applied_id,
        'checked_at': item.checked_at,
    }


def batch_dict(batch, items=None):
    body = {
        'id': batch.id,
        'batch_key': batch.batch_key,
        'actor': batch.actor,
        'created': batch.created,
        'status': batch.status,
        'payload_digest': batch.payload_digest,
        'request': batch.request,
        'summary': batch.summary,
        'note': batch.note,
        'landed_at': batch.landed_at,
        'revalidated_at': batch.revalidated_at,
    }
    if items is not None:
        body['items'] = items
    return body


def _summary(status, items):
    counts = Counter(i.status if not isinstance(i, dict) else i['status'] for i in items)
    counts['total'] = len(items)
    counts['status'] = status
    return dict(counts)


# ---------------------------------------------------------------- 采集与暂存

def export_bundle(db):
    """带出原始依据：每条底稿的完整内容与摘要，离线侧据此做三方合并。"""
    sources = db.query(Source).order_by(Source.id).all()
    return {
        'format': 'offline-draft-v1',
        'generated_at': time.time(),
        'digest_rule': 'sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False))',
        'contract': {
            'items': 'natural_key / op(upsert|delete) / base / base_digest / incoming',
            'conflict': '同一字段异改、撤销与修改、交叠停机异改均保留双方，整批暂存等待裁决',
        },
        'items': [
            {
                'natural_key': s.natural_key,
                'id': s.id,
                'base': s.data,
                'base_digest': digest(s.data),
            }
            for s in sources
        ],
    }


def _merge_items(db, request):
    items, seen = [], set()
    for ordinal, raw in enumerate(request['items']):
        key = (raw.get('natural_key') or '').strip()
        if not key:
            raise HTTPException(422, '自然键不可为空')
        if len(key) > 120:
            raise HTTPException(422, '自然键过长')
        if key in seen:
            raise HTTPException(422, '同一批次内自然键重复：' + key)
        seen.add(key)
        op = raw.get('op') or 'upsert'
        if op not in ('upsert', 'delete'):
            raise HTTPException(422, '操作必须是 upsert 或 delete')
        raw_base = raw.get('base')
        if raw_base is not None and not isinstance(raw_base, dict):
            raise HTTPException(422, '原始依据必须是对象或留空：' + key)
        base_digest = raw.get('base_digest') or (digest(raw_base) if raw_base is not None else None)
        if raw_base is not None and base_digest != digest(raw_base):
            # 摘要按回传原文校验，规范化（如停机合并）不得改变摘要口径。
            raise HTTPException(422, '原始依据与其摘要不一致，请重新采集：' + key)
        base = raw_base
        incoming = normalize_record(raw.get('incoming'))
        if op == 'delete':
            if base is None:
                # 没有原始依据的撤销无法判断是否与库内修改相撞，要求先带出依据。
                raise HTTPException(422, '撤销必须附带原始依据：' + key)
            incoming = None
        elif not isinstance(incoming, dict):
            raise HTTPException(422, '回传修订必须提供 incoming 底稿：' + key)
        if incoming is not None:
            _check_constraints(incoming)
        source = _source_by_key(db, key, for_update=True)
        server = source.data if source else None
        outcome = _guard_result(base, server, incoming, merge_record(base, server, incoming))
        conflict = outcome['conflict']
        status = 'conflict' if conflict else ('noop' if outcome['action'] == 'noop' else 'auto')
        items.append(
            {
                'ordinal': ordinal,
                'natural_key': key,
                'op': op,
                'base': base,
                'base_digest': base_digest,
                'incoming': incoming,
                'server': server,
                'server_digest': digest(server) if server is not None else None,
                'result': outcome,
                'status': status,
                'conflict': conflict,
                'message': conflict['message'] if conflict else '',
                'checked_at': time.time(),
            }
        )
    return items


def create_batch(db, user, body):
    existing = db.query(MergeBatch).filter_by(batch_key=body.batch_key).first()
    if existing:
        # 重试不重复应用：相同批次键直接返回既有批次与既有落地结果。
        return existing, False
    if not body.items:
        raise HTTPException(422, '离线底稿为空')
    if len(body.items) > MAX_ITEMS:
        raise HTTPException(422, f'单批最多 {MAX_ITEMS} 条')
    # 先取锁再读底稿：与落地/裁决串行，且加锁读能看到刚提交的落地结果。
    _batch_lock(db)
    existing = db.query(MergeBatch).filter_by(batch_key=body.batch_key).first()
    if existing:
        return existing, False
    request = {
        'batch_key': body.batch_key,
        'note': body.note or '',
        'items': [
            {
                'natural_key': i.natural_key,
                'op': i.op,
                'base': i.base,
                'base_digest': i.base_digest,
                'incoming': i.incoming,
            }
            for i in body.items
        ],
    }
    items = _merge_items(db, request)
    pending = any(i['status'] == 'conflict' for i in items)
    batch = MergeBatch(
        batch_key=body.batch_key,
        actor=user.email,
        created=time.time(),
        status='pending' if pending else 'landing',
        payload_digest=digest({'batch_key': body.batch_key, 'items': request['items']}),
        request=request,
        note=body.note or '',
        summary=_summary('pending' if pending else 'landing', items),
    )
    db.add(batch)
    try:
        db.flush()
    except IntegrityError:
        # 并发同批次键：唯一约束触发，返回先落地的那一批，不重复应用。
        db.rollback()
        existing = db.query(MergeBatch).filter_by(batch_key=body.batch_key).first()
        if existing:
            return existing, False
        raise
    stored = []
    for data in items:
        item = MergeItem(
            batch_id=batch.id,
            ordinal=data['ordinal'],
            natural_key=data['natural_key'],
            op=data['op'],
            base=data['base'],
            base_digest=data['base_digest'],
            incoming=data['incoming'],
            server=data['server'],
            server_digest=data['server_digest'],
            result=data['result'],
            status=data['status'],
            conflict=data['conflict'],
            message=data['message'],
            checked_at=data['checked_at'],
        )
        db.add(item)
        stored.append(item)
    db.flush()

    if pending:
        # 冲突批次整批暂存：不写底稿、不影响核算。
        log(db, user.email, 'merge.staged', f'batch={batch.id} items={len(items)} conflicts={batch.summary.get("conflict", 0)}')
        db.commit()
        return batch, True

    _apply_all(db, batch, stored)
    log(db, user.email, 'merge.landed', f'batch={batch.id} items={len(items)} auto')
    db.commit()
    return batch, True

# ---------------------------------------------------------------- 落地

def _apply_all(db, batch, items):
    for item in items:
        if item.status in ('noop', 'applied', 'skipped'):
            continue
        item.applied_id = _apply_item(db, item)
        item.status = 'applied'
    batch.status = 'landed'
    batch.landed_at = time.time()
    batch.summary = _summary('landed', items)
    db.add(batch)
    db.flush()


def _apply_item(db, item):
    result = dict(item.result or {})
    action = result.get('action')
    if action == 'noop':
        source = _source_by_key(db, item.natural_key)
        return source.id if source else None
    if action == 'delete':
        source = _source_by_key(db, item.natural_key)
        if source:
            db.delete(source)
            db.flush()
        return None
    if action in ('create', 'update'):
        key = result.get('alternate_key') or item.natural_key
        if key != item.natural_key:
            # 另立新键保存回传值；若已被占用则顺延，避免覆盖他人底稿。
            key = _alternative_key(db, item)
            result['alternate_key'] = key
            item.result = result
            db.add(item)
        return _write(db, key, result['data'])
    if action == 'both':
        # 保留双方：库内主体保持服务端结论，另以独立自然键补录回传内容。
        alternate_key = _alternative_key(db, item)
        result['alternate_key'] = alternate_key
        item.result = result
        db.add(item)
        _write(db, alternate_key, result['alternate'])
        if result.get('data') is None:
            return None
        return _write(db, item.natural_key, result['data'])
    raise HTTPException(409, '条目尚未裁决，整批不能落地：' + item.natural_key)


def _write(db, key, data):
    _check_constraints(data)
    source = _source_by_key(db, key)
    if not source:
        source = Source(natural_key=key, data=data)
        db.add(source)
    else:
        source.data = data
        db.add(source)
    db.flush()
    if not db.query(Period).filter_by(name=data['period']).first():
        db.add(Period(name=data['period'], description='业务来源自动登记期间'))
    return source.id


# ---------------------------------------------------------------- 重新核验

def _stale(item, current):
    return (digest(current) if current is not None else None) != item.server_digest


def _refresh_item(db, item, current):
    outcome = _guard_result(item.base, current, item.incoming, merge_record(item.base, current, item.incoming))
    conflict = outcome['conflict']
    item.server = current
    item.server_digest = digest(current) if current is not None else None
    item.result = outcome
    item.conflict = conflict
    item.status = 'conflict' if conflict else ('noop' if outcome['action'] == 'noop' else 'auto')
    item.message = conflict['message'] if conflict else ''
    item.checked_at = time.time()
    db.add(item)
    return item


def recheck_batch(db, user, batch_id):
    _batch_lock(db)
    batch = db.get(MergeBatch, batch_id)
    if not batch:
        raise HTTPException(404, '合并批次不存在')
    if batch.status == 'landed':
        raise HTTPException(409, '批次已落地；旧报告不变，不再回溯')
    items = db.query(MergeItem).filter_by(batch_id=batch.id).order_by(MergeItem.ordinal).all()
    changed = 0
    for item in items:
        if item.status == 'applied':
            continue
        source = _source_by_key(db, item.natural_key, for_update=True)
        current = source.data if source else None
        if not _stale(item, current):
            # 源数据未变：已裁决条目保持结论，其余条目依据仍有效。
            continue
        changed += 1
        # 源数据变化：丢弃任何旧裁决，按原始依据 + 最新库内值 + 回传值重算。
        # 若有冲突则回到待裁决，否则按重新合并的结果归类，绝不沿用旧结论。
        _refresh_item(db, item, current)
    batch.status = 'pending' if any(i.status == 'conflict' for i in items) else 'ready'
    batch.summary = _summary(batch.status, items)
    batch.revalidated_at = time.time()
    db.add(batch)
    log(db, user.email, 'merge.recheck', f'batch={batch.id} changed={changed}')
    db.commit()
    return batch, changed


# ---------------------------------------------------------------- 裁决

def _record_choice(db, item, choice):
    current = item.server
    incoming = item.incoming
    if choice == 'server':
        return {'action': 'noop', 'data': current, 'changed_fields': []}
    if choice == 'incoming':
        if incoming is None:
            return {'action': 'delete', 'data': None, 'changed_fields': sorted(item.base or {})}
        return {
            'action': 'create' if current is None else 'update',
            'data': incoming,
            'changed_fields': sorted(incoming),
        }
    if choice == 'both':
        if incoming is None:
            # 回传撤销、库内修改：保留库内修改。
            return {'action': 'noop', 'data': current, 'changed_fields': []}
        if current is None:
            # 库内撤销、回传修改：原键保持撤销，另立新键保存修改。
            return {
                'action': 'create',
                'data': incoming,
                'changed_fields': sorted(incoming),
                'alternate_key': _alternative_key(db, item),
            }
        return {
            'action': 'both',
            'data': current,
            'alternate': incoming,
            'alternate_key': _alternative_key(db, item),
            'changed_fields': ['alternate'],
        }
    raise HTTPException(422, '该冲突只能选择库内、回传或保留双方')


def _field_choice(db, item, body):
    conflict = item.conflict or {}
    merged = dict(item.result.get('data') or item.server or {})
    chosen = body.fields or {}
    for entry in conflict.get('fields') or []:
        name = entry['field']
        pick = chosen.get(name, 'server')
        if pick not in ('server', 'incoming'):
            raise HTTPException(422, '字段裁决只能选择库内或回传：' + name)
        merged[name] = entry['incoming'] if pick == 'incoming' else entry['server']
    downtime = conflict.get('downtime')
    if downtime:
        pick = body.downtime or 'union'
        if pick == 'server':
            merged[DOWNTIME] = normalize(downtime['server'])
        elif pick == 'incoming':
            merged[DOWNTIME] = normalize(downtime['incoming'])
        elif pick == 'union':
            merged[DOWNTIME] = downtime_union(downtime['server'], downtime['incoming'])
        else:
            raise HTTPException(422, '停机裁决只能选择库内、回传或保留双方')
    if body.record == 'both':
        # 同一字段无法并存两个标量时，保留双方的方式是原键保存库内值，
        # 回传值另立底稿，两份依据都可复核。另立的底稿同时带上双方独立合入的字段。
        alternate = dict(merged)
        for entry in conflict.get('fields') or []:
            alternate[entry['field']] = entry['incoming']
        if item.server is None:
            return {'action': 'create', 'data': item.incoming, 'changed_fields': sorted(item.incoming or {})}
        try:
            _check_constraints(item.server)
            _check_constraints(alternate)
        except ValueError as exc:
            raise HTTPException(422, '保留双方时有依据不满足业务约束：' + str(exc))
        return {
            'action': 'both',
            'data': item.server,
            'alternate': alternate,
            'alternate_key': _alternative_key(db, item),
            'changed_fields': ['alternate'],
        }
    try:
        _check_constraints(merged)
    except ValueError as exc:
        raise HTTPException(422, '裁决结果不满足产量或停机约束：' + str(exc))
    action = 'update' if item.server is not None else 'create'
    if records_equal(merged, item.server):
        action = 'noop'
    return {'action': action, 'data': merged, 'changed_fields': sorted(merged)}


def _resolve_result(db, item, body):
    conflict = item.conflict or {}
    if conflict.get('kind') in ('delete-modify', 'add-collision', 'constraint'):
        # 约束冲突源于两侧独立修改叠加后不合法，按整条依据裁决最稳妥。
        if body.record not in ('server', 'incoming', 'both'):
            raise HTTPException(422, '该冲突需明确选择库内、回传或保留双方')
        return _record_choice(db, item, body.record)
    return _field_choice(db, item, body)


def resolve_item(db, user, batch_id, item_id, body):
    _batch_lock(db)
    batch = db.get(MergeBatch, batch_id)
    if not batch:
        raise HTTPException(404, '合并批次不存在')
    if batch.status == 'landed':
        raise HTTPException(409, '批次已落地；旧报告与底稿不再回溯，可另建批次回传')
    item = db.get(MergeItem, item_id)
    if not item or item.batch_id != batch.id:
        raise HTTPException(404, '合并条目不存在')
    if not item.conflict:
        raise HTTPException(409, '该条没有待裁决冲突')
    source = _source_by_key(db, item.natural_key, for_update=True)
    current = source.data if source else None
    if _stale(item, current):
        # 源数据自暂存后变化：刷新条目并要求重新核验，不直接应用旧裁决。
        _refresh_item(db, item, current)
        items = db.query(MergeItem).filter_by(batch_id=batch.id).order_by(MergeItem.ordinal).all()
        batch.status = 'pending' if any(i.status == 'conflict' for i in items) else 'ready'
        batch.summary = _summary(batch.status, items)
        db.add(batch)
        log(db, user.email, 'merge.stale', f'batch={batch.id} item={item.id}')
        db.commit()
        raise HTTPException(409, '源数据在暂存后已变化，条目已刷新，请重新核验后再裁决')
    result = _resolve_result(db, item, body)
    item.result = result
    item.status = 'resolved'
    item.conflict = None
    item.message = ''
    item.checked_at = time.time()
    db.add(item)
    items = db.query(MergeItem).filter_by(batch_id=batch.id).order_by(MergeItem.ordinal).all()
    batch.status = 'pending' if any(i.status == 'conflict' for i in items) else 'ready'
    batch.summary = _summary(batch.status, items)
    db.add(batch)
    log(db, user.email, 'merge.resolve', f'batch={batch.id} item={item.id} choose={body.record or body.downtime or "fields"}')
    db.commit()
    return item


def land_batch(db, user, batch_id):
    _batch_lock(db)
    batch = db.get(MergeBatch, batch_id)
    if not batch:
        raise HTTPException(404, '合并批次不存在')
    if batch.status == 'landed':
        # 重试不重复应用。
        return batch, False
    items = db.query(MergeItem).filter_by(batch_id=batch.id).order_by(MergeItem.ordinal).all()
    if any(i.status == 'conflict' for i in items):
        raise HTTPException(409, '仍有冲突未裁决，整批不会落地')
    stale = []
    for item in items:
        source = _source_by_key(db, item.natural_key, for_update=True)
        if _stale(item, source.data if source else None):
            stale.append(item.natural_key)
    if stale:
        raise HTTPException(409, '源数据已变化，请重新核验后再落地：' + '、'.join(stale[:5]))
    try:
        _apply_all(db, batch, items)
    except Exception:
        db.rollback()
        raise
    log(db, user.email, 'merge.land', f'batch={batch.id} items={len(items)}')
    db.commit()
    return batch, True


def list_batches(db, limit=50):
    return db.query(MergeBatch).order_by(MergeBatch.id.desc()).limit(limit).all()


def detail_batch(db, batch_id):
    batch = db.get(MergeBatch, batch_id)
    if not batch:
        raise HTTPException(404, '合并批次不存在')
    items = db.query(MergeItem).filter_by(batch_id=batch.id).order_by(MergeItem.ordinal).all()
    return batch, [snapshot_item(i) for i in items]


def pending_items(db):
    """仍需裁决的条目，供页面显示待处理及依据。"""
    return (
        db.query(MergeItem)
        .join(MergeBatch, MergeBatch.id == MergeItem.batch_id)
        .filter(MergeBatch.status.in_(('pending', 'ready')), MergeItem.status == 'conflict')
        .order_by(MergeItem.batch_id.desc(), MergeItem.ordinal)
        .limit(200)
        .all()
    )
