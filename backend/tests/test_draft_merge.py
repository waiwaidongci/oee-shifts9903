"""离线底稿三方合并的纯函数验证。运行：python -m unittest tests.test_draft_merge"""
import unittest

from src.services.draft_merge import (
    digest,
    merge_downtime,
    merge_record,
    normalize,
    subtract,
    union,
)


def record(**over):
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


class IntervalTests(unittest.TestCase):
    def test_normalize_and_subtract(self):
        self.assertEqual(normalize([[560, 590], [540, 570], [540, 570]]), [[540, 590]])
        self.assertEqual(subtract([[480, 1020]], [[540, 590], [950, 970]]), [[480, 540], [590, 950], [970, 1020]])
        self.assertEqual(union([[480, 520]], [[500, 560]]), [[480, 560]])

    def test_disjoint_downtime_edits_merge(self):
        base = [[540, 590], [700, 720]]
        server = [[540, 590], [700, 730]]              # 服务端延长 700-730
        incoming = [[530, 590], [700, 720], [900, 950]]  # 离线侧新增 900-950 并提前 530
        merged, conflict = merge_downtime(base, server, incoming)
        self.assertIsNone(conflict)
        self.assertEqual(merged, [[530, 590], [700, 730], [900, 950]])

    def test_overlapping_identical_downtime_edit_merges(self):
        base = [[540, 590], [700, 720]]
        server = [[540, 590], [680, 760]]     # 服务端把 700-760 并成一段
        incoming = [[540, 590], [680, 760]]   # 离线侧给出相同更正
        merged, conflict = merge_downtime(base, server, incoming)
        self.assertIsNone(conflict)
        self.assertEqual(merged, [[540, 590], [680, 760]])

    def test_identical_downtime_edits_merge(self):
        base = [[540, 560], [590, 610]]
        server = [[540, 610]]   # 服务端补记 560-590
        incoming = [[540, 610]]  # 离线侧补记同一段
        merged, conflict = merge_downtime(base, server, incoming)
        self.assertIsNone(conflict)
        self.assertEqual(merged, [[540, 610]])

    def test_adjacent_downtime_edits_merge(self):
        # 服务端改 570-600，离线侧改 540-560，修改范围相邻但不相交。
        base = [[540, 600]]
        server = [[540, 570]]
        incoming = [[560, 600]]
        merged, conflict = merge_downtime(base, server, incoming)
        self.assertIsNone(conflict)
        self.assertEqual(merged, [[560, 570]])

    def test_overlapping_conflicting_edit_keeps_both(self):
        # 服务端删除 540-590，离线侧只把终点从 600 改到 590；修改范围交叠，
        # 在交叠段内双方对停机状态的判断不一致。
        base = [[540, 600]]
        server = []
        incoming = [[540, 590]]
        merged, conflict = merge_downtime(base, server, incoming)
        self.assertIsNone(merged)
        self.assertEqual(conflict['overlap'], [[590, 600]])
        self.assertEqual(conflict['server_edits'], [[540, 600]])
        self.assertEqual(conflict['incoming_edits'], [[590, 600]])


class RecordTests(unittest.TestCase):
    def test_independent_fields_auto_merge(self):
        base = record()
        server = record(good=860)
        incoming = record(total=910)
        out = merge_record(base, server, incoming)
        self.assertEqual(out['action'], 'update')
        self.assertIsNone(out['conflict'])
        self.assertEqual(out['data']['good'], 860)
        self.assertEqual(out['data']['total'], 910)
        self.assertEqual(out['changed_fields'], ['total'])

    def test_same_field_different_edits_keep_both(self):
        base = record()
        server = record(total=880)
        incoming = record(total=910)
        out = merge_record(base, server, incoming)
        self.assertEqual(out['action'], 'conflict')
        self.assertEqual(out['conflict']['kind'], 'field')
        self.assertEqual(out['conflict']['fields'][0]['field'], 'total')
        self.assertEqual(out['conflict']['fields'][0]['server'], 880)
        self.assertEqual(out['conflict']['fields'][0]['incoming'], 910)

    def test_incoming_unchanged_is_noop(self):
        out = merge_record(record(), record(good=860), record())
        self.assertEqual(out['action'], 'noop')

    def test_offline_delete_against_unchanged_server_deletes(self):
        out = merge_record(record(), record(), None)
        self.assertEqual(out['action'], 'delete')

    def test_offline_delete_against_modified_server_conflicts(self):
        out = merge_record(record(), record(good=860), None)
        self.assertEqual(out['action'], 'conflict')
        self.assertEqual(out['conflict']['kind'], 'delete-modify')

    def test_offline_modify_against_server_delete_conflicts(self):
        out = merge_record(record(), None, record(total=910))
        self.assertEqual(out['action'], 'conflict')
        self.assertEqual(out['conflict']['kind'], 'delete-modify')

    def test_offline_untouched_against_server_delete_is_noop(self):
        out = merge_record(record(), None, record())
        self.assertEqual(out['action'], 'noop')

    def test_new_record_created(self):
        out = merge_record(None, None, record())
        self.assertEqual(out['action'], 'create')

    def test_new_record_collision_conflicts(self):
        out = merge_record(None, record(total=910), record())
        self.assertEqual(out['action'], 'conflict')
        self.assertEqual(out['conflict']['kind'], 'add-collision')

    def test_digest_is_stable(self):
        self.assertEqual(digest(record()), digest(record()))
        self.assertNotEqual(digest(record()), digest(record(total=901)))


if __name__ == '__main__':
    unittest.main()
