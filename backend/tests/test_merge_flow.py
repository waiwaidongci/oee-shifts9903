"""端到端验证离线合并：暂存、落地、裁决、重试与核算可见性。

使用临时 SQLite 与内存假 Redis，运行：
  PYTHONPATH=backend python -m unittest tests.test_merge_flow
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='merge-flow-'))
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + str(_TMP / 'test.db'))
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('CONFIG_PATH', str(Path(__file__).resolve().parents[1] / 'project.json'))

import src.config.settings as settings  # noqa: E402

settings.CONFIG = json.loads((Path(__file__).resolve().parents[1] / 'project.json').read_text())

from src.config import database as db_config  # noqa: E402

db_config.engine.dispose()
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

db_config.engine = create_engine(os.environ['DATABASE_URL'], connect_args={'check_same_thread': False})
db_config.Session = sessionmaker(bind=db_config.engine, expire_on_commit=False)

import src.models.identity as identity  # noqa: E402
import src.models.merge as merge_models  # noqa: E402
import src.models.run as run_models  # noqa: E402
import src.models.source as source_models  # noqa: E402
from src.config.database import Base  # noqa: E402
from src.models.identity import User  # noqa: E402
from src.models.merge import MergeBatch, MergeItem  # noqa: E402
from src.models.run import Run  # noqa: E402
from src.models.source import Policy, Source  # noqa: E402
from src.schemas.requests import MergeInput, ResolveInput, RunInput  # noqa: E402
from src.services import merge as merge_service  # noqa: E402
import src.services.report as report_service  # noqa: E402
from src.services.draft_merge import digest  # noqa: E402

Base.metadata.create_all(db_config.engine)


class _FakeRedis:
    def rpush(self, *args, **kwargs):
        return 1


class _FakeRedisFactory:
    @staticmethod
    def from_url(url):
        return _FakeRedis()


report_service.redis = type('R', (), {'Redis': _FakeRedisFactory})


def seed_user(email, role='admin'):
    with db_config.Session() as db:
        if not db.query(User).filter_by(email=email).first():
            db.add(User(email=email, role=role, password='x'))
            db.commit()
        return db.query(User).filter_by(email=email).first()


def make_record(**over):
    data = {
        'period': '2026-11-17',
        'department': '试制机A',
        'start': 480,
        'end': 1020,
        'ideal_seconds': 25,
        'total': 900,
        'good': 872,
        'downtime': [[540, 590]],
    }
    data.update(over)
    return data


class MergeFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = db_config.engine
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.admin = seed_user('admin@demo.local', 'admin')
        self.analyst = seed_user('analyst@demo.local', 'analyst')
        with db_config.Session() as db:
            db.add(Policy(version='OEE-v1', data={'version': 'OEE-v1', 'definition': '演示口径'}))
            db.add(Source(natural_key='key-1', data=make_record()))
            db.commit()

    def batch(self, items, key='batch-a', note=''):
        with db_config.Session() as db:
            user = db.query(User).filter_by(email=self.admin.email).first()
            body = MergeInput(batch_key=key, note=note, items=items)
            obj, _ = merge_service.create_batch(db, user, body)
            return obj.id

    def current(self, key='key-1'):
        with db_config.Session() as db:
            source = db.query(Source).filter_by(natural_key=key).first()
            return source.data if source else None

    def item_payload(self, base=None, **over):
        base = base if base is not None else make_record()
        payload = {'natural_key': 'key-1', 'op': 'upsert', 'base': base, 'base_digest': digest(base)}
        payload.update(over)
        return payload

    def test_independent_changes_land_atomically(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(good=860)
            db.commit()
        batch_id = self.batch([self.item_payload(incoming=make_record(total=910))])
        with db_config.Session() as db:
            batch = db.get(MergeBatch, batch_id)
            self.assertEqual(batch.status, 'landed')
            data = db.query(Source).filter_by(natural_key='key-1').one().data
            self.assertEqual(data['good'], 860)
            self.assertEqual(data['total'], 910)
            self.assertEqual(db.query(Source).count(), 1)

    def test_conflict_stages_without_landing(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=880)
            db.commit()
        batch_id = self.batch([self.item_payload(incoming=make_record(total=910))])
        with db_config.Session() as db:
            batch = db.get(MergeBatch, batch_id)
            self.assertEqual(batch.status, 'pending')
            # 暂存不落地：底稿保持服务端值，核算看不到回传修订。
            self.assertEqual(db.query(Source).filter_by(natural_key='key-1').one().data['total'], 880)
            self.assertEqual(db.query(MergeItem).filter_by(batch_id=batch_id, status='conflict').count(), 1)
            # 冲突批次不影响核算：提交核算仍读取 880。
            run = report_service.submit(db, db.query(User).filter_by(email=self.admin.email).one(), RunInput(name='冲突期间核算', policy_id=1, period='全部'))
            self.assertEqual(run.snapshot['sources'][0]['total'], 880)

    def test_resolve_keeps_both_then_lands(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=880)
            db.commit()
        batch_id = self.batch([self.item_payload(incoming=make_record(total=910))])
        with db_config.Session() as db:
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            user = db.query(User).filter_by(email=self.admin.email).one()
            merge_service.resolve_item(db, user, batch_id, item.id, ResolveInput(record='both'))
        with db_config.Session() as db:
            batch = db.get(MergeBatch, batch_id)
            self.assertEqual(batch.status, 'ready')
            user = db.query(User).filter_by(email=self.admin.email).one()
            merge_service.land_batch(db, user, batch_id)
        with db_config.Session() as db:
            totals = sorted(s.data['total'] for s in db.query(Source).all())
            self.assertEqual(totals, [880, 910])

    def test_field_resolution_uses_incoming_value(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(good=860)
            db.commit()
        batch_id = self.batch([self.item_payload(incoming=make_record(good=850))])
        with db_config.Session() as db:
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            user = db.query(User).filter_by(email=self.admin.email).one()
            merge_service.resolve_item(db, user, batch_id, item.id, ResolveInput(fields={'good': 'incoming'}))
            merge_service.land_batch(db, user, batch_id)
        self.assertEqual(self.current()['good'], 850)

    def test_retry_same_batch_is_idempotent(self):
        batch_id = self.batch([self.item_payload(incoming=make_record(total=910))], key='retry-1')
        with db_config.Session() as db:
            user = db.query(User).filter_by(email=self.admin.email).one()
            body = MergeInput(batch_key='retry-1', note='', items=[self.item_payload(incoming=make_record(total=910))])
            again, created = merge_service.create_batch(db, user, body)
            self.assertEqual(again.id, batch_id)
            self.assertFalse(created)
            merge_service.land_batch(db, user, batch_id)
            merge_service.land_batch(db, user, batch_id)
        with db_config.Session() as db:
            self.assertEqual(db.query(Source).count(), 1)
            self.assertEqual(db.query(Source).filter_by(natural_key='key-1').one().data['total'], 910)

    def test_stale_source_blocks_resolution(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=880)
            db.commit()
        batch_id = self.batch([self.item_payload(incoming=make_record(total=910))])
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=875)
            db.commit()
        with db_config.Session() as db:
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            user = db.query(User).filter_by(email=self.admin.email).one()
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as ctx:
                merge_service.resolve_item(db, user, batch_id, item.id, ResolveInput(fields={'total': 'incoming'}))
            self.assertEqual(ctx.exception.status_code, 409)
        with db_config.Session() as db:
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            self.assertEqual(item.server['total'], 875)
            self.assertEqual(item.status, 'conflict')

    def test_delete_vs_modify_conflict_and_both(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(good=860)
            db.commit()
        batch_id = self.batch([self.item_payload(op='delete', incoming=None)], key='del-1')
        with db_config.Session() as db:
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            self.assertEqual(item.conflict['kind'], 'delete-modify')
            user = db.query(User).filter_by(email=self.admin.email).one()
            merge_service.resolve_item(db, user, batch_id, item.id, ResolveInput(record='both'))
            merge_service.land_batch(db, user, batch_id)
        with db_config.Session() as db:
            self.assertEqual(db.query(Source).count(), 1)
            self.assertEqual(db.query(Source).filter_by(natural_key='key-1').one().data['good'], 860)

    def test_recheck_keeps_resolution_when_source_unchanged(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=880)
            db.commit()
        batch_id = self.batch([self.item_payload(incoming=make_record(total=910))])
        with db_config.Session() as db:
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            user = db.query(User).filter_by(email=self.admin.email).one()
            merge_service.resolve_item(db, user, batch_id, item.id, ResolveInput(record='both'))
        with db_config.Session() as db:
            user = db.query(User).filter_by(email=self.admin.email).one()
            batch, changed = merge_service.recheck_batch(db, user, batch_id)
            self.assertEqual(changed, 0)
            self.assertEqual(batch.status, 'ready')
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            self.assertEqual(item.status, 'resolved')

    def test_recheck_invalidates_resolution_when_source_changes(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=880)
            db.commit()
        batch_id = self.batch([self.item_payload(incoming=make_record(total=910))])
        with db_config.Session() as db:
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            user = db.query(User).filter_by(email=self.admin.email).one()
            merge_service.resolve_item(db, user, batch_id, item.id, ResolveInput(record='both'))
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=870)
            db.commit()
        with db_config.Session() as db:
            user = db.query(User).filter_by(email=self.admin.email).one()
            batch, changed = merge_service.recheck_batch(db, user, batch_id)
            self.assertEqual(changed, 1)
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            self.assertEqual(item.server['total'], 870)
            self.assertNotEqual(item.status, 'resolved')
            # 旧裁决依据失效：保存的裁决结果须被重新合并结果覆盖。
            self.assertEqual(item.result['action'], 'conflict')

    def test_pending_blocks_whole_batch(self):
        # key-1 与 key-2 在库内和离线两侧都被改成了不同的 total，两条均冲突。
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=880)
            db.add(Source(natural_key='key-2', data=make_record(total=808, good=760)))
            db.commit()
        items = [
            self.item_payload(incoming=make_record(total=910)),
            {'natural_key': 'key-2', 'op': 'upsert', 'base': make_record(total=800, good=760),
             'base_digest': digest(make_record(total=800, good=760)),
             'incoming': make_record(total=805, good=760)},
        ]
        batch_id = self.batch(items, key='mixed-1')
        with db_config.Session() as db:
            self.assertEqual(db.get(MergeBatch, batch_id).status, 'pending')
            before = {s.natural_key: s.data['total'] for s in db.query(Source).all()}
            # 整批暂存：底稿都保持服务端值。
            self.assertEqual(before, {'key-1': 880, 'key-2': 808})
            user = db.query(User).filter_by(email=self.admin.email).one()
            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                merge_service.land_batch(db, user, batch_id)
        # 只裁决了第一条，第二条仍冲突，整批仍不落地。
        with db_config.Session() as db:
            first = db.query(MergeItem).filter_by(batch_id=batch_id, status='conflict').order_by(MergeItem.id).first()
            user = db.query(User).filter_by(email=self.admin.email).one()
            merge_service.resolve_item(db, user, batch_id, first.id, ResolveInput(fields={'total': 'incoming'}))
        with db_config.Session() as db:
            self.assertEqual(db.get(MergeBatch, batch_id).status, 'pending')
            self.assertEqual(db.query(Source).filter_by(natural_key='key-2').one().data['total'], 808)
            # 两条都裁决后才可整批落地。
            second = db.query(MergeItem).filter_by(batch_id=batch_id, status='conflict').order_by(MergeItem.id).first()
            user = db.query(User).filter_by(email=self.admin.email).one()
            merge_service.resolve_item(db, user, batch_id, second.id, ResolveInput(fields={'total': 'incoming'}))
            merge_service.land_batch(db, user, batch_id)
        with db_config.Session() as db:
            self.assertEqual(db.get(MergeBatch, batch_id).status, 'landed')
            totals = {s.natural_key: s.data['total'] for s in db.query(Source).all()}
            self.assertEqual(totals, {'key-1': 910, 'key-2': 805})

    def test_export_digest_accepts_nonnormalized_base(self):
        # seed 数据的停机区间可能重叠；导出摘要按原文计算，回传原文必须通过校验。
        raw = make_record(downtime=[[540, 570], [560, 590], [950, 970]])
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = raw
            db.commit()
            bundle = merge_service.export_bundle(db)
        exported = next(i for i in bundle['items'] if i['natural_key'] == 'key-1')
        self.assertEqual(exported['base_digest'], digest(raw))
        batch_id = self.batch([{'natural_key': 'key-1', 'op': 'upsert', 'base': raw,
                                'base_digest': exported['base_digest'],
                                'incoming': {**raw, 'total': 910}}], key='raw-digest')
        with db_config.Session() as db:
            self.assertEqual(db.get(MergeBatch, batch_id).status, 'landed')
            self.assertEqual(db.query(Source).filter_by(natural_key='key-1').one().data['total'], 910)

    def test_downtime_overlap_requires_resolution_and_union(self):
        base = make_record(downtime=[[540, 600]])
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = {**base, 'downtime': []}
            db.commit()
        incoming = {**base, 'downtime': [[540, 590]]}
        batch_id = self.batch([self.item_payload(base=base, incoming=incoming)], key='down-conflict')
        with db_config.Session() as db:
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            self.assertEqual(item.conflict['kind'], 'field')
            self.assertIsNotNone(item.conflict['downtime'])
            self.assertEqual(item.conflict['downtime']['overlap'], [[590, 600]])
            user = db.query(User).filter_by(email=self.admin.email).one()
            merge_service.resolve_item(db, user, batch_id, item.id, ResolveInput(downtime='union'))
            merge_service.land_batch(db, user, batch_id)
        self.assertEqual(self.current()['downtime'], [[540, 590]])

    def test_add_collision_keeps_both(self):
        record = make_record(total=777, good=700)
        batch_id = self.batch([{'natural_key': 'key-1', 'op': 'upsert', 'base': None, 'base_digest': None,
                                'incoming': record}], key='add-collision')
        with db_config.Session() as db:
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            self.assertEqual(item.conflict['kind'], 'add-collision')
            user = db.query(User).filter_by(email=self.admin.email).one()
            merge_service.resolve_item(db, user, batch_id, item.id, ResolveInput(record='both'))
            merge_service.land_batch(db, user, batch_id)
        with db_config.Session() as db:
            totals = sorted(s.data['total'] for s in db.query(Source).all())
            self.assertEqual(totals, [777, 900])

    def test_new_record_auto_created(self):
        record = make_record(department='试制机D', total=640, good=600)
        batch_id = self.batch([{'natural_key': 'new-key', 'op': 'upsert', 'base': None, 'base_digest': None,
                                'incoming': record}], key='create-new')
        with db_config.Session() as db:
            self.assertEqual(db.get(MergeBatch, batch_id).status, 'landed')
            created = db.query(Source).filter_by(natural_key='new-key').one()
            self.assertEqual(created.data['total'], 640)
            self.assertEqual(db.query(Source).filter_by(natural_key='key-1').one().data['total'], 900)

    def test_report_unchanged_after_later_merge(self):
        batch_id = self.batch([self.item_payload(incoming=make_record(total=910))], key='after-run')
        with db_config.Session() as db:
            run = report_service.submit(db, db.query(User).filter_by(email=self.admin.email).one(), RunInput(name='合并后核算', policy_id=1, period='全部'))
            checksum = run.checksum
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=999)
            db.commit()
        with db_config.Session() as db:
            self.assertEqual(db.query(Run).first().checksum, checksum)
            self.assertEqual(db.query(Run).first().snapshot['sources'][0]['total'], 910)

    def test_export_bundle_carries_basis(self):
        with db_config.Session() as db:
            bundle = merge_service.export_bundle(db)
            self.assertEqual(bundle['format'], 'offline-draft-v1')
            item = bundle['items'][0]
            self.assertEqual(item['natural_key'], 'key-1')
            self.assertEqual(item['base_digest'], digest(item['base']))

    def test_disjoint_downtime_edits_auto_merge(self):
        base = make_record(downtime=[[540, 590], [700, 720]])
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = {**base, 'downtime': [[540, 590], [700, 730]]}
            db.commit()
        incoming = {**base, 'downtime': [[530, 590], [700, 720], [900, 950]]}
        batch_id = self.batch([self.item_payload(base=base, incoming=incoming)])
        with db_config.Session() as db:
            self.assertEqual(db.get(MergeBatch, batch_id).status, 'landed')
            self.assertEqual(db.query(Source).filter_by(natural_key='key-1').one().data['downtime'],
                             [[530, 590], [700, 730], [900, 950]])

    def test_constraint_violation_is_conflict(self):
        # 两侧修改各自合法，但合并后 start(560) 与 end(570) 之间全被停机覆盖，
        # 却仍有产量：自动合入须转为待裁决，而不是静默落地。
        base = make_record(start=480, end=1020, downtime=[[540, 590]])
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = {
                **base, 'end': 570, 'total': 900, 'good': 872,
            }
            db.commit()
        incoming = {**base, 'start': 560}
        # 校验原始依据与回传各自合法。
        self.assertTrue(incoming['start'] < incoming['end'])
        batch_id = self.batch([self.item_payload(base=base, incoming=incoming)], key='constraint-1')
        with db_config.Session() as db:
            batch = db.get(MergeBatch, batch_id)
            self.assertEqual(batch.status, 'pending')
            item = db.query(MergeItem).filter_by(batch_id=batch_id).one()
            self.assertEqual(item.conflict['kind'], 'constraint')
            self.assertEqual(db.query(Source).filter_by(natural_key='key-1').one().data['end'], 570)

    def test_invalid_incoming_rejected_at_intake(self):
        # 中间件把 ValueError 映射为 422；服务层直接抛出。
        incoming = make_record(downtime=[[480, 1020]])
        with self.assertRaises(ValueError):
            self.batch([self.item_payload(incoming=incoming)], key='bad-1')
        with db_config.Session() as db:
            self.assertEqual(db.query(Source).filter_by(natural_key='key-1').one().data['downtime'], [[540, 590]])


if __name__ == '__main__':
    unittest.main()
