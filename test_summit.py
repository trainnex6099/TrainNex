import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import summit_store as store
import summit_bridge as bridge
from summit_wire import shift_payload, loa_payload


class FakeAPI:
    def __init__(self, commands=None, state=None):
        self.commands = commands or []
        self.state = state
        self.results = []
        self.fail_ack = False
    async def get(self, path, guild_id=None):
        return {'state': self.state} if guild_id else {'commands': self.commands}
    async def put(self, path, payload, guild_id=None):
        self.state = payload
        return {'ok': True}
    async def request(self, method, path, payload):
        if payload['status'] == 'processing':
            item = next(c for c in self.commands if c['id'] == payload['id'])
            if item.get('status') == 'processing':
                return {'claimed': False}
            item['status'] = 'processing'
            return {'claimed': True}
        if self.fail_ack:
            self.fail_ack = False
            raise OSError('Lost acknowledgement')
        self.results.append(payload)
        return {'ok': True}


class SummitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / 'summit.db'
        store.init_shift_db(); store.init_loa_db()
        self.type_id = store.save_shift_type('1516305981078900756', {'name':'Patrol'})
    def tearDown(self):
        store.DB_PATH = self.original
        self.tmp.cleanup()
    def start(self):
        return store.start_shift('1516305981078900756','123456789012345678','Deputy','Deputy',self.type_id)
    async def test_shift_cross_surface_and_break_exclusion(self):
        with patch('summit_store.time.time', return_value=1000):
            s = self.start()
        with patch('summit_store.time.time', return_value=1100):
            s = store.set_shift_break(s['shift_id'], True)
        wire = shift_payload(s, now=1200)
        self.assertEqual(wire['workedSeconds'], 100)
        self.assertEqual(wire['breakSeconds'], 100)
        self.assertEqual(wire['employeeId'],'123456789012345678')
        # Discord sees the exact shift started by the portal action.
        self.assertEqual(store.get_active_shift(s['guild_id'],s['user_id'])['shift_id'],s['shift_id'])
        with patch('summit_store.time.time', return_value=1300):
            ended = store.end_shift(s['shift_id'])
        self.assertEqual(shift_payload(ended,1300)['workedSeconds'],100)
        self.assertIsNone(store.get_active_shift(s['guild_id'],s['user_id']))
    async def test_duplicate_active_shift_rejected(self):
        self.start()
        with self.assertRaises(ValueError): self.start()
    async def test_loa_review_and_end_cross_surface(self):
        r=store.create_loa_request('1516305981078900756','123','Deputy','1D',86400,'Holiday')
        r=store.approve_loa(r['loa_id'],'456')
        self.assertEqual(loa_payload(r)['reviewedBy'],'456')
        with self.assertRaises(ValueError): store.deny_loa(r['loa_id'],'789','No')
        r=store.end_loa(r['loa_id'],'123')
        self.assertEqual(loa_payload(r)['status'],'ended')
    async def test_receipt_prevents_replay_after_ack_failure(self):
        api=FakeAPI([{'id':'request-1','command':json.dumps({'action':'shift.start'}),'requestedBy':'123'}])
        api.fail_ack=True
        calls=[]
        async def execute(action,actor): calls.append(actor); return {'shiftId':'S-1'}
        with self.assertRaises(OSError): await bridge.process(api,execute)
        await bridge.process(api,execute)
        self.assertEqual(calls,['123'])
        self.assertEqual(api.results[-1]['status'],'completed')
    async def test_unknown_inflight_command_never_replayed(self):
        api=FakeAPI([{'id':'lost','status':'processing','command':'{}','requestedBy':'123'}])
        async def execute(*args): self.fail('Must not replay interrupted operation')
        await bridge.process(api,execute)
        self.assertEqual(api.results[-1]['status'],'failed')
    async def test_permission_failure_is_recorded(self):
        api=FakeAPI([{'id':'request-2','command':'{}','requestedBy':'123'}])
        async def execute(*args): raise PermissionError('FTO role required')
        await bridge.process(api,execute)
        self.assertEqual(api.results[-1]['result']['error'],'FTO role required')
    async def test_backup_retains_full_state_and_snowflakes(self):
        s=self.start();api=FakeAPI()
        uid=1445916050427216002
        training={'queue':[uid], 'offers':{'offer':{'trainer_id':uid}},'pending':{}}
        await bridge.publish(api,s['guild_id'],training)
        self.assertEqual(api.state['training']['queue'],[str(uid)])
        self.assertEqual(api.state['shifts'][0]['shift_type_id'],self.type_id)
        self.assertEqual(bridge.training_local(api.state['training']),training)
    async def test_restore_rejects_foreign_guild(self):
        s=self.start()
        with self.assertRaises(ValueError):
            await bridge.restore(FakeAPI(state={'shifts':[s]}),'999')
    async def test_restore_does_not_rollback_local_work(self):
        s=self.start();api=FakeAPI();await bridge.publish(api,s['guild_id'])
        store.end_shift(s['shift_id'])
        await bridge.restore(api,s['guild_id'])
        self.assertEqual(store.get_shift(s['shift_id'])['status'],'ended')


if __name__=='__main__': unittest.main()
