"""Private helpdesk flows with temporary storage and mocked Discord I/O."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock
import discord
from summit_helpdesk import serialize_message, TicketStore, SummitHelpdesk, IntakeModal, TOPICS, staff, can_open, transcript_html, opening_embed, TicketActions

OWNER = '123456789012345678'
USER = '123456789012345679'
TEAM = '123456789012345680'


class HelpdeskTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_and_claimed_tickets_never_post_transcript_notices(self):
        channel=MagicMock(spec=discord.TextChannel)
        channel.send=AsyncMock()
        self.bot.get_guild.return_value=SimpleNamespace(get_channel=lambda _:channel)
        for status in ('open','claimed'):
            self.record.update(status=status,transcriptChannelId='987')
            await self.service.log_ticket(self.record,{'ownerId':OWNER},False)
            await self.service.log_ticket(self.record,{'ownerId':OWNER},True)
        channel.send.assert_not_awaited()
        self.api.put.assert_not_awaited()

    async def test_open_ticket_sync_only_backs_up_metadata(self):
        self.service.restore=AsyncMock()
        self.service.log_ticket=AsyncMock()
        self.service.archive_channel=AsyncMock()
        self.service.update_controls=AsyncMock()
        await self.service.synchronize()
        self.service.log_ticket.assert_not_awaited()
        self.service.archive_channel.assert_not_awaited()
        self.assertEqual(self.api.put.await_args.args[1]['ticket']['messages'],[])

    async def test_ticket_controls_and_two_embed_layout(self):
        view=TicketActions(self.service,'case-1')
        self.assertEqual([button.label for button in view.children],['Close','Close With Reason','Claim'])
        self.service.close=AsyncMock()
        interaction=SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        await view.children[0].callback(interaction)
        self.service.close.assert_awaited_once_with(interaction,'case-1','')
        embeds=self.service.intake_embeds({**self.record,'topic':'Partnership assistance'})
        self.assertEqual(len(embeds),2)
        self.assertIn('Thank you for contacting support',embeds[0].description)
        self.assertEqual(embeds[1].description,'Partnership assistance')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = SimpleNamespace(configured=True, base_url='https://portal.example', put=AsyncMock(return_value={'ok':True}))
        self.bot = MagicMock()
        self.service = SummitHelpdesk(self.bot, self.api, Path(self.tmp.name)/'tickets.db')
        self.service.config = AsyncMock(return_value={'ownerId':OWNER})
        self.member = SimpleNamespace(id=int(USER), roles=[], display_name='Member', display_avatar=SimpleNamespace(url='https://cdn.discordapp.com/embed/avatars/0.png'), bot=False)
        self.service.member = AsyncMock(return_value=self.member)
        self.record = self.service.store.create({'id':'case-1','guildId':'1516305981078900756','kind':'support','topic':'Other','openerId':USER,'openerName':'Member','staffRoleIds':[TEAM],'status':'open','openedAt':'today','channelId':'123','messages':[]})
        self.interaction = SimpleNamespace(guild=MagicMock(), user=self.member)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_modal_contains_seven_required_topics(self):
        modal = IntakeModal(self.service, 'support')
        self.assertEqual([o.label for o in modal.choice.options], TOPICS)
        self.assertEqual(modal.choice.min_values, 1)
        self.assertEqual(modal.choice.max_values, 1)

    async def test_ia_does_not_inherit_general_support_access(self):
        config={'ownerId':OWNER,'ia':{'openerRoleIds':[TEAM]}}
        self.assertTrue(can_open(self.member,'support',config))
        self.assertFalse(can_open(self.member,'ia',config))
        self.member.roles=[SimpleNamespace(id=int(TEAM))]
        self.assertTrue(can_open(self.member,'ia',config))

    async def test_custom_ticket_types_and_invalid_fallback(self):
        modal = IntakeModal(self.service, 'support', ['Technical assistance', 'Partnership inquiry'])
        self.assertEqual([o.label for o in modal.choice.options], ['Technical assistance', 'Partnership inquiry'])
        self.assertEqual(IntakeModal(self.service, 'support', []).topics, TOPICS)
        self.assertEqual(IntakeModal(self.service, 'support', ['x'] * 26).topics, TOPICS)

    async def test_configured_opening_embed_and_limits(self):
        embed = opening_embed({'embed': {'title': '📩 Contact WCSO', 'description': '**Welcome**', 'color': '#abcdef', 'footer': 'Summit'}}, 'support')
        self.assertEqual(embed.title, '📩 Contact WCSO')
        self.assertEqual(embed.description, '**Welcome**')
        self.assertEqual(embed.colour.value, 0xabcdef)
        self.assertEqual(embed.footer.text, 'Summit')
        self.assertTrue(opening_embed({}, 'ia').title.startswith('🛡'))
        with self.assertRaises(ValueError):
            opening_embed({'embed': {'title': 'x' * 257}}, 'support')

    async def test_opener_cannot_claim_staff_ticket(self):
        with self.assertRaises(PermissionError):
            await self.service.manage(self.interaction,'case-1','claim')
        self.assertEqual(self.service.store.get('case-1')['status'],'open')

    async def test_claim_cannot_be_taken_from_another_staff_member(self):
        self.member.roles=[SimpleNamespace(id=int(TEAM))]
        self.record.update(claimedBy=OWNER,status='claimed')
        self.service.store.save(self.record)
        with self.assertRaises(ValueError):
            await self.service.manage(self.interaction,'case-1','claim')
        self.assertEqual(self.service.store.get('case-1')['claimedBy'],OWNER)

    async def test_unrelated_member_cannot_close(self):
        self.member.id=987
        with self.assertRaises(PermissionError):
            await self.service.close(self.interaction,'case-1','')
        self.api.put.assert_not_awaited()

    async def test_close_preserves_messages_before_deleting_channel(self):
        channel=MagicMock(spec=discord.TextChannel)
        message=SimpleNamespace(id=88,author=self.member,created_at=SimpleNamespace(isoformat=lambda:'today'),content='Private conversation',attachments=[],embeds=[],components=[])
        async def history(**kwargs):
            yield message
        channel.history=history
        self.interaction.guild.get_channel.return_value=channel
        self.service.permissions=AsyncMock()
        self.service.log_ticket=AsyncMock()
        self.service.update_controls=AsyncMock()
        self.bot.get_channel.return_value=channel
        channel.guild.id=1516305981078900756
        await self.service.close(self.interaction,'case-1','Resolved')
        stored=self.service.store.get('case-1')
        self.assertEqual(stored['messages'][0]['content'],'Private conversation')
        self.assertEqual(stored['status'],'closed')
        self.service.permissions.assert_awaited_once()
        self.assertFalse(self.service.permissions.await_args.args[2])
        channel.delete.assert_awaited_once()
        self.assertTrue(stored['channelDeleted'])

    async def test_transcript_backup_failure_prevents_channel_deletion(self):
        channel=MagicMock(spec=discord.TextChannel)
        async def history(**kwargs):
            if False:yield None
        channel.history=history
        self.interaction.guild.get_channel.return_value=channel
        self.bot.get_channel.return_value=channel
        self.service.permissions=AsyncMock()
        self.service.log_ticket=AsyncMock()
        self.api.put.side_effect=RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            await self.service.close(self.interaction,'case-1','Resolved')
        channel.delete.assert_not_awaited()
        self.assertTrue(self.service.store.get('case-1')['pendingBackup'])

    async def test_numbering_is_independent_and_ia_starts_after_four(self):
        ia=self.service.store.create({**self.record,'id':'ia-1','kind':'ia'})
        second=self.service.store.create({**self.record,'id':'support-2'})
        next_ia=self.service.store.create({**self.record,'id':'ia-2','kind':'ia'})
        self.assertEqual((self.record['number'],second['number']),(1,2))
        self.assertEqual((ia['number'],next_ia['number']),(5,6))
        reopened=TicketStore(self.service.store.path)
        self.assertEqual(reopened.create({**self.record,'id':'ia-3','kind':'ia'})['number'],7)

    async def test_large_portal_copy_retains_full_local_archive(self):
        self.record['messages']=[{'content':'a'*1000000}]
        self.service.store.save(self.record)
        await self.service.publish_record(self.record)
        remote=self.api.put.await_args.args[1]['ticket']
        self.assertTrue(remote['transcriptTruncated'])
        self.assertEqual(remote['messages'],[])
        self.assertEqual(len(self.service.store.get('case-1')['messages'][0]['content']),1000000)

    async def test_failed_backup_remains_pending(self):
        self.api.put.return_value={'ok':False}
        with self.assertRaises(RuntimeError):
            await self.service.publish_record(self.record)
        self.assertTrue(self.service.store.get('case-1')['pendingBackup'])

    async def test_restore_is_idempotent_and_preserves_number(self):
        other=TicketStore(Path(self.tmp.name)/'restore.db')
        other.restore(self.record);other.restore(self.record)
        self.assertEqual(len(other.all()),1)
        self.assertEqual(other.get('case-1')['number'],self.record['number'])

    async def test_html_transcript_escapes_message_content(self):
        self.record['messages']=[{'authorName':'<script>','createdAt':'today','content':'<img src=x onerror=alert(1)>','attachments':[]}]
        result=transcript_html(self.record).decode()
        self.assertNotIn('<script>',result)
        self.assertNotIn('<img',result)
        self.assertIn('&lt;img',result)

    async def test_owner_and_handling_roles_only(self):
        self.assertFalse(staff(self.member,self.record,OWNER))
        self.member.roles=[SimpleNamespace(id=int(TEAM))]
        self.assertTrue(staff(self.member,self.record,OWNER))

    async def test_public_support_log_contains_no_conversation_attachment(self):
        channel=MagicMock(spec=discord.TextChannel)
        channel.send=AsyncMock(return_value=SimpleNamespace(id=99,jump_url='https://discord.com/channels/1/2/99'))
        self.bot.get_guild.return_value=SimpleNamespace(get_channel=lambda _:channel)
        self.record.update(transcriptChannelId='987',messages=[{'content':'Private conversation'}],closeReason='Private reason',status='closed')
        await self.service.log_ticket(self.record,{'ownerId':OWNER},True)
        args=channel.send.await_args.kwargs
        self.assertNotIn('file',args)
        self.assertNotIn('Private reason',str(args['embed'].to_dict()))
        self.assertNotIn('Private conversation',str(args['embed'].to_dict()))
        self.assertFalse(self.service.store.get('case-1')['fullArchiveLogged'])

    async def test_ia_log_uses_protected_link_without_downloadable_transcript(self):
        channel=MagicMock(spec=discord.TextChannel)
        channel.send=AsyncMock(return_value=SimpleNamespace(id=99,jump_url='https://discord.com/channels/1/2/99'))
        self.bot.get_guild.return_value=SimpleNamespace()
        self.service.private_destination=MagicMock(return_value=channel)
        self.record.update(kind='ia',transcriptChannelId='987',messages=[{'content':'Private conversation'}],status='closed')
        await self.service.log_ticket(self.record,{'ownerId':OWNER},True)
        args=channel.send.await_args.kwargs
        self.assertNotIn('file',args)
        self.assertEqual(args['view'].children[0].url,'https://portal.example/tickets/case-1')
        self.assertFalse(self.service.store.get('case-1')['fullArchiveLogged'])

    async def test_ia_opening_requires_ia_role_even_for_owner(self):
        self.member.id=int(OWNER)
        self.assertFalse(can_open(self.member,'ia',{'ownerId':OWNER,'ia':{'openerRoleIds':[TEAM]}}))

    async def test_existing_discord_numbers_are_not_reused(self):
        self.service.store.observe_channels([SimpleNamespace(name='ticket-21'),SimpleNamespace(name='🔴case-000004')])
        next_support=self.service.store.create({**self.record,'id':'new-support'})
        next_ia=self.service.store.create({**self.record,'id':'new-ia','kind':'ia'})
        self.assertEqual((next_support['number'],next_ia['number']),(22,5))

    async def test_bot_embeds_and_archived_controls_are_saved(self):
        msg=SimpleNamespace(id=77,author=self.member,created_at=SimpleNamespace(isoformat=lambda:'today'),content='',attachments=[],embeds=[discord.Embed(title='Claimed ticket',description='Your ticket will be handled by IA')],components=[SimpleNamespace(children=[SimpleNamespace(label='Close')])])
        saved=serialize_message(msg)
        self.assertEqual(saved['embeds'][0]['title'],'Claimed ticket')
        self.assertEqual(saved['controls'],['Close'])
        self.assertIn('Claimed ticket',transcript_html({**self.record,'messages':[saved]}).decode())


if __name__ == '__main__':
    unittest.main()
