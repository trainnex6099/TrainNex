import unittest
from unittest.mock import AsyncMock,patch
from summit_api import SummitAPI
import summit_bridge as bridge

class DirectorySyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_and_repeated_directory_sync_do_not_rewrite_unchanged_roles(self):
        api=SummitAPI();role={'id':'1','name':'IA','position':3,'color':5,'managed':False}
        api.get=AsyncMock(return_value={'roles':[{**role,'managed':0,'updatedAt':'old'}]})
        api.put=AsyncMock(return_value={'ok':True})
        await api.sync_roles(9,[role]);await api.sync_roles(9,[role])
        api.get.assert_awaited_once();api.put.assert_not_awaited()

    async def test_changed_directory_is_sent_once(self):
        api=SummitAPI();api.get=AsyncMock(return_value={'channels':[]});api.put=AsyncMock(return_value={'ok':True})
        channel={'id':'2','name':'support','position':1,'type':0,'categoryId':None}
        await api.sync_channels(9,[channel]);await api.sync_channels(9,[channel])
        api.put.assert_awaited_once()

    async def test_failed_directory_write_can_be_retried(self):
        api=SummitAPI();api.get=AsyncMock(return_value={'roles':[]});api.put=AsyncMock(side_effect=[RuntimeError('offline'),{'ok':True}])
        role={'id':'1','name':'IA','position':3,'color':5,'managed':False}
        with self.assertRaises(RuntimeError):await api.sync_roles(9,[role])
        await api.sync_roles(9,[role]);self.assertEqual(api.put.await_count,2)

    async def test_unchanged_snapshots_use_one_write_per_heartbeat(self):
        api=SummitAPI();api.put=AsyncMock(return_value={'ok':True})
        state={'guildId':'9','shifts':[],'loas':[],'updatedAt':10}
        with patch.object(bridge,'snapshot',return_value=state),patch.object(bridge.time,'monotonic',return_value=1000):
            await bridge.publish(api,9);state['updatedAt']=11;await bridge.publish(api,9)
        self.assertEqual(api.put.await_count,1)
        with patch.object(bridge,'snapshot',return_value=state),patch.object(bridge.time,'monotonic',return_value=1601):await bridge.publish(api,9)
        self.assertEqual(api.put.await_count,2)

    async def test_changed_shift_state_publishes_immediately(self):
        api=SummitAPI();api.put=AsyncMock(return_value={'ok':True})
        state={'guildId':'9','shifts':[],'loas':[],'updatedAt':10}
        with patch.object(bridge,'snapshot',return_value=state):
            await bridge.publish(api,9);state['shifts']=[{'shift_id':'S-1','status':'active'}];await bridge.publish(api,9)
        self.assertEqual(api.put.await_count,2)

if __name__=='__main__':unittest.main()
