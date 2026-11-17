"""通过 FastAPI TestClient 验证离线合并的 HTTP 契约。"""
import os
import tempfile
import unittest
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='merge-api-'))
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + str(_TMP / 'api.db'))
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('REDIS_URL', 'redis://localhost:1/0')
os.environ.setdefault('CONFIG_PATH', str(Path(__file__).resolve().parents[1] / 'project.json'))

from src.config import database as db_config  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

db_config.engine = create_engine(os.environ['DATABASE_URL'], connect_args={'check_same_thread': False})
db_config.Session = sessionmaker(bind=db_config.engine, expire_on_commit=False)

import src.models.identity  # noqa: E402,F401
import src.models.merge  # noqa: E402,F401
import src.models.run  # noqa: E402,F401
import src.models.source  # noqa: E402,F401
from src.config.database import Base  # noqa: E402
from src.models.identity import User  # noqa: E402
from src.models.merge import MergeBatch  # noqa: E402
from src.models.source import Source  # noqa: E402
from src.utils.security import password_hash, token  # noqa: E402
from src.services.draft_merge import digest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from src.main import app  # noqa: E402


def make_record(**over):
    data = {'period': '2026-11-17', 'department': '试制机A', 'start': 480, 'end': 1020,
            'ideal_seconds': 25, 'total': 900, 'good': 872, 'downtime': [[540, 590]]}
    data.update(over)
    return data


class MergeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(db_config.engine)
        Base.metadata.create_all(db_config.engine)
        with db_config.Session() as db:
            for role in ('admin', 'analyst', 'viewer'):
                db.add(User(email=role + '@demo.local', role=role, password=password_hash('Demo2026!')))
            db.add(Source(natural_key='key-1', data=make_record()))
            db.commit()

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(db_config.engine)

    def client(self, email):
        with db_config.Session() as db:
            user = db.query(User).filter_by(email=email).one()
            tok = token(user)
        return TestClient(app, headers={'Authorization': 'Bearer ' + tok})

    def item(self, **over):
        base = make_record()
        body = {'natural_key': 'key-1', 'op': 'upsert', 'base': base, 'base_digest': digest(base)}
        body.update(over)
        return body

    def test_export_requires_login(self):
        self.assertEqual(TestClient(app).get('/merge/export').status_code, 401)

    def test_export_carries_basis_for_viewer(self):
        r = self.client('viewer@demo.local').get('/merge/export')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['format'], 'offline-draft-v1')
        self.assertTrue(any(i['natural_key'] == 'key-1' and i['base_digest'] for i in body['items']))

    def test_viewer_cannot_submit(self):
        r = self.client('viewer@demo.local').post('/merge/batches', json={'batch_key': 'viewer-1', 'items': [self.item(incoming=make_record(total=901))]})
        self.assertEqual(r.status_code, 403)

    def test_auto_merge_then_land_status_visible(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(good=860)
            db.commit()
        r = self.client('analyst@demo.local').post('/merge/batches', json={'batch_key': 'api-auto', 'items': [self.item(incoming=make_record(total=910))]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'landed')
        self.assertIn('items', r.json())
        self.assertEqual(r.json()['items'][0]['status'], 'applied')
        with db_config.Session() as db:
            self.assertEqual(db.query(Source).filter_by(natural_key='key-1').one().data['total'], 910)

    def test_conflict_resolve_requires_admin_and_reports_pending(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=880)
            db.commit()
        analyst = self.client('analyst@demo.local')
        r = analyst.post('/merge/batches', json={'batch_key': 'api-conflict', 'items': [self.item(incoming=make_record(total=910))]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['status'], 'pending')
        item_id = body['items'][0]['id']
        self.assertEqual(body['items'][0]['conflict']['kind'], 'field')
        # 分析师无裁决权限
        self.assertEqual(analyst.post(f'/merge/batches/{body["id"]}/items/{item_id}/resolve', json={'fields': {'total': 'incoming'}}).status_code, 403)
        admin = self.client('admin@demo.local')
        pending = admin.get('/merge/pending').json()
        self.assertTrue(any(p['id'] == item_id for p in pending))
        r = admin.post(f'/merge/batches/{body["id"]}/items/{item_id}/resolve', json={'fields': {'total': 'incoming'}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'ready')
        r = admin.post(f'/merge/batches/{body["id"]}/land')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['applied'])
        self.assertEqual(r.json()['batch']['status'], 'landed')
        # 重试落地不重复应用
        r = admin.post(f'/merge/batches/{body["id"]}/land')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['applied'])
        with db_config.Session() as db:
            self.assertEqual(db.query(Source).filter_by(natural_key='key-1').one().data['total'], 910)
            self.assertEqual(db.query(Source).count(), 1)

    def test_recheck_endpoint(self):
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=880)
            db.commit()
        r = self.client('analyst@demo.local').post('/merge/batches', json={'batch_key': 'api-recheck', 'items': [self.item(incoming=make_record(total=910))]})
        batch_id = r.json()['id']
        # 源数据再变，重新核验会刷新依据
        with db_config.Session() as db:
            db.query(Source).filter_by(natural_key='key-1').one().data = make_record(total=875)
            db.commit()
        r = self.client('analyst@demo.local').post(f'/merge/batches/{batch_id}/recheck')
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.json()['changed'], 1)
        self.assertEqual(r.json()['batch']['items'][0]['server']['total'], 875)

    def test_duplicate_batch_key_returns_existing(self):
        body = {'batch_key': 'api-dup', 'items': [self.item(incoming=make_record(total=901))]}
        first = self.client('admin@demo.local').post('/merge/batches', json=body).json()
        second = self.client('admin@demo.local').post('/merge/batches', json=body).json()
        self.assertEqual(first['id'], second['id'])
        with db_config.Session() as db:
            self.assertEqual(db.query(MergeBatch).filter_by(batch_key='api-dup').count(), 1)

    def test_invalid_digest_returns_422(self):
        body = {'batch_key': 'api-badgen', 'items': [self.item(incoming=make_record(total=901), base_digest='0' * 64)]}
        r = self.client('analyst@demo.local').post('/merge/batches', json=body)
        self.assertEqual(r.status_code, 422)


if __name__ == '__main__':
    unittest.main()
