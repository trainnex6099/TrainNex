"""Exercise actual bot dispatch with Discord I/O mocked, never a real token."""
import os
os.environ.setdefault('DISCORD_TOKEN','test-only-not-a-real-token')
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
import time
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import main
import summit_store as store

GUILD = 1516305981078900756
USER = 123456789012345678


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.original=store.DB_PATH
        store.DB_PATH=Path(self.tmp.name)/'test.db';store.init_shift_db();store.init_loa_db()
        store.save_shift_type(GUILD,{'name':'Patrol'})
        self.guild=SimpleNamespace(id=GUILD,fetch_member=AsyncMock(),get_member=MagicMock(return_value=None))
        self.member=MagicMock(spec=discord.Member)
        self.member.id=USER;self.member.guild=self.guild;self.member.display_name='Deputy'
        self.member.top_role=SimpleNamespace(name='Deputy')
        self.member.roles=[SimpleNamespace(id=1516647746025558017)]
        self.member.guild_permissions=SimpleNamespace(administrator=False,manage_guild=False)
        self.guild.fetch_member.return_value=self.member
        self.patches=[patch.object(main.bot,'get_guild',return_value=self.guild)]
        for name in ['publish_portal_state','sync_shift_record_to_portal','sync_loa_record_to_portal','_apply_shift_roles','_shift_log','_apply_loa_role','_loa_log','_send_loa_dm','_edit_loa_request_message']:
            self.patches.append(patch.object(main,name,AsyncMock()))
        for p in self.patches:p.start()
        main.HYDRATED_GUILDS.add(GUILD)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        main.HYDRATED_GUILDS.discard(GUILD);store.DB_PATH=self.original;self.tmp.cleanup()
    async def test_portal_start_discord_end_same_shift(self):
        result=await main.execute_portal_action({'action':'shift.start','guildId':str(GUILD),'type':'Patrol'},str(USER))
        interaction=SimpleNamespace(guild=self.guild,user=self.member,response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock(),followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(main,'_shift_manage_embed',return_value=discord.Embed()):
            view=main.ShiftManageView(USER,'Patrol')
            await view.end_btn.callback(interaction)
        self.assertEqual(store.get_shift(result['shiftId'])['status'],'ended')
        self.guild.fetch_member.assert_awaited_once_with(USER)
    async def test_stale_portal_employee_role_rejected(self):
        self.member.roles=[]
        with self.assertRaises(PermissionError):
            await main.execute_portal_action({'action':'shift.start','guildId':str(GUILD)},str(USER))
        self.assertEqual(store.list_active_shifts(GUILD),[])

    async def test_reset_requires_owner_even_for_employee(self):
        with patch.object(main.HELPDESK,'config',AsyncMock(return_value={'ownerId':'987'})):
            with self.assertRaises(PermissionError):
                await main.execute_portal_action({'action':'system.reset','guildId':str(GUILD),'target':'shifts','confirmation':'RESET SHIFTS'},str(USER))

    async def test_reset_retains_shift_history_and_saves_backup(self):
        shift_type=store.get_shift_type_by_name(GUILD,'Patrol')
        record=store.start_shift(GUILD,USER,'Deputy','Deputy',shift_type['id'])
        with patch.object(main.HELPDESK,'config',AsyncMock(return_value={'ownerId':str(USER)})),patch.object(store,'DATA_DIR',Path(self.tmp.name)),patch.object(main.HELPDESK.store,'path',Path(self.tmp.name)/'helpdesk.db'):
            result=await main.execute_portal_action({'action':'system.reset','guildId':str(GUILD),'target':'shifts','confirmation':'RESET SHIFTS'},str(USER))
        self.assertEqual(store.get_shift(record['shift_id'])['status'],'ended')
        self.assertTrue(result['historyRetained'])
        self.assertTrue(list((Path(self.tmp.name)/'backups').glob('*/test.db')))

    async def test_reset_requires_exact_confirmation(self):
        with patch.object(main.HELPDESK,'config',AsyncMock(return_value={'ownerId':str(USER)})):
            with self.assertRaises(ValueError):
                await main.execute_portal_action({'action':'system.reset','guildId':str(GUILD),'target':'shifts','confirmation':'yes'},str(USER))
    async def test_foreign_loa_cannot_be_reviewed(self):
        r=store.create_loa_request('999','123','Other','1D',86400,'Leave')
        with self.assertRaises(ValueError):
            await main.execute_portal_action({'action':'loa.approve','guildId':str(GUILD),'recordId':r['loa_id']},str(USER))
        self.assertEqual(store.get_loa(r['loa_id'])['status'],'pending')
    async def test_nonreviewer_cannot_approve_loa(self):
        r=store.create_loa_request(GUILD,'123','Other','1D',86400,'Leave')
        with self.assertRaises(PermissionError):
            await main.execute_portal_action({'action':'loa.approve','guildId':str(GUILD),'recordId':r['loa_id']},str(USER))
    async def test_disabled_shift_system_rejected(self):
        store.save_shift_settings(GUILD,{'enabled':False})
        with self.assertRaises(ValueError):
            await main.execute_portal_action({'action':'shift.start','guildId':str(GUILD),'type':'Patrol'},str(USER))
    async def test_stale_shift_id_cannot_end_new_shift(self):
        await main.execute_portal_action({'action':'shift.start','guildId':str(GUILD),'type':'Patrol'},str(USER))
        with self.assertRaises(ValueError):
            await main.execute_portal_action({'action':'shift.end','guildId':str(GUILD),'recordId':'S-OLD'},str(USER))
        self.assertIsNotNone(store.get_active_shift(GUILD,USER))

    async def test_portal_training_claim_cannot_be_claimed_again_in_discord(self):
        self.member.roles.append(SimpleNamespace(id=12345))
        data={'queue':[USER],'offers':{'offer-1':{'state':'pending','trainer_id':USER,'trainee_id':987,
              'approval_url':'https://discord.com/channels/1/2/3','expires_at':time.time()+60}},
              'pending':{},'processed_approval_messages':[]}
        with patch.object(main,'TRAINING_SERVER_ID',GUILD),patch.object(main,'FTO_ROLE_ID',12345),patch.object(main,'DATA',data),patch.object(main,'save_data'),patch.object(main,'resolve_user',AsyncMock(return_value=None)),patch.object(main,'send_claim_notification',AsyncMock()) as notify:
            await main.execute_portal_action({'action':'training.claim','guildId':str(GUILD),'offerId':'offer-1'},str(USER))
            interaction=SimpleNamespace(user=self.member,response=SimpleNamespace(send_message=AsyncMock()))
            await main.handle_offer_claim(interaction,'offer-1')
            self.assertEqual(data['offers']['offer-1']['state'],'claimed')
            self.assertEqual(data['pending']['987']['claimed_by'],USER)
            notify.assert_awaited_once()

    async def test_training_queue_requires_manager_role(self):
        with patch.object(main,'TRAINING_SERVER_ID',GUILD):
            with self.assertRaises(PermissionError):
                await main.execute_portal_action({'action':'training.add','guildId':str(GUILD),'trainerId':str(USER)},str(USER))


if __name__=='__main__':unittest.main()
