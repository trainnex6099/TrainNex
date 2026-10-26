"""Private helpdesk flows with temporary storage and mocked Discord I/O."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock
import discord
from summit_helpdesk import serialize_message, TicketStore, SummitHelpdesk, IntakeModal, TOPICS, staff, can_open, transcript_html, opening_embed, TicketActions, OpeningPanel, ticket_channel_name

OWNER = '123456789012345678'
USER = '123456789012345679'
TEAM = '123456789012345680'


class HelpdeskTests(unittest.IsolatedAsyncioTestCase):
    async def test_ia_escalation_button_only_on_claimed_support(self):
        for closed,kind,claimed in [(False,'support',False),(True,'support',True),(False,'ia',True)]:
            self.assertNotIn('Open IA Case',[b.label for b in TicketActions(self.service,'case-1',closed,kind,claimed).children])
        view=TicketActions(self.service,'case-1',claimed=True)
        self.assertEqual([b.label for b in view.children],['Close','Close With Reason','Open IA Case'])
        self.service.open_ia_from_ticket=AsyncMock(return_value={'channelId':'999'})
        interaction=SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        await view.children[2].callback(interaction)
        self.service.open_ia_from_ticket.assert_awaited_once_with(interaction,'case-1')

    async def test_escalation_rechecks_claim_and_both_permissions(self):
        self.service.config=AsyncMock(return_value={'ownerId':OWNER,'ia':{'openerRoleIds':[TEAM]}})
        self.service.open=AsyncMock(return_value={'id':'linked','channelId':'999'})
        with self.assertRaises(PermissionError):
            await self.service.open_ia_from_ticket(self.interaction,'case-1')
        self.member.roles=[SimpleNamespace(id=int(TEAM))]
        with self.assertRaises(ValueError):
            await self.service.open_ia_from_ticket(self.interaction,'case-1')
        self.record.update(status='claimed',claimedBy=USER)
        self.service.store.save(self.record)
        self.service.config.return_value['ia']['openerRoleIds']=['987654321012345678']
        with self.assertRaises(PermissionError):
            await self.service.open_ia_from_ticket(self.interaction,'case-1')
        self.service.open.assert_not_awaited()

    async def test_escalation_uses_stable_case_id_and_links_source_without_transcript(self):
        self.service.config=AsyncMock(return_value={'ownerId':OWNER,'ia':{'openerRoleIds':[TEAM]}})
        self.member.roles=[SimpleNamespace(id=int(TEAM))]
        self.record.update(status='claimed',claimedBy=USER)
        self.service.store.save(self.record)
        self.service.open=AsyncMock(return_value={'id':'ia-from-case-1','channelId':'999'})
        await self.service.open_ia_from_ticket(self.interaction,'case-1')
        await self.service.open_ia_from_ticket(self.interaction,'case-1')
        for call in self.service.open.await_args_list:
            self.assertEqual(call.args[0].id,'ia-from-case-1')
            self.assertEqual(call.kwargs['source_ticket']['id'],'case-1')
        saved=self.service.store.get('case-1')
        self.assertEqual(saved['linkedIaCaseId'],'ia-from-case-1')
        self.assertEqual(saved['messages'],[])
        case={**self.record,'kind':'ia','sourceTicketId':'case-1','sourceTicketNumber':1,'sourceChannelId':'123','sourceTopic':'Other','sourceOpenerId':USER}
        self.assertEqual(self.service.intake_embeds(case)[1].fields[0].value,'[Ticket #1](https://discord.com/channels/1516305981078900756/123)')

    async def test_ia_has_no_ping_or_claim_while_support_keeps_both(self):
        ia={**self.record,'kind':'ia','number':5}
        self.assertEqual([b.label for b in TicketActions(self.service,'ia-test',kind='ia').children],['Close','Close With Reason'])
        self.assertIn('Claim',[b.label for b in TicketActions(self.service,'support-test').children])
        notice=self.service.intake_notice(ia)
        self.assertIsNone(notice['content'])
        self.assertEqual(notice['allowed_mentions'].to_dict()['parse'],[])
        support=self.service.intake_notice(self.record)
        self.assertIn(TEAM,support['content'])
        self.assertNotIn('Claimed',str(self.service.intake_embeds(ia)[1].to_dict()))
        self.assertFalse(any('Claimed By' in field.name for field in self.service.log_embed(ia,True).fields))

    async def test_existing_ia_controls_remove_ping_and_claim(self):
        record={**self.record,'kind':'ia','number':5,'controlMessageId':'77'}
        message=SimpleNamespace(edit=AsyncMock())
        self.bot.get_channel.return_value=SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        await self.service.update_controls(record)
        args=message.edit.await_args.kwargs
        self.assertIsNone(args['content'])
        self.assertNotIn('Claim',[b.label for b in args['view'].children])
        self.assertEqual(record['controlVersion'],3)

    async def test_ia_opens_directly_without_a_name_modal(self):
        self.service.open=AsyncMock(return_value={'channelId':'987'})
        interaction=SimpleNamespace(response=SimpleNamespace(defer=AsyncMock(),send_modal=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        await OpeningPanel(self.service,'ia').open(interaction)
        interaction.response.defer.assert_awaited_once()
        interaction.response.send_modal.assert_not_awaited()
        self.service.open.assert_awaited_once_with(interaction,'ia','Internal Affairs investigation')

    async def test_ia_uses_case_number_as_topic_and_channel_name(self):
        record=self.service.store.create({**self.record,'id':'numbered-case','kind':'ia','topic':'Custom title must not be used'})
        self.assertEqual(record['number'],5)
        self.assertEqual(record['topic'],'case-005')
        self.assertEqual(ticket_channel_name(record),'case-005')
        self.service.store.observe_channels([SimpleNamespace(name='case-007')])
        later=self.service.store.create({**self.record,'id':'later-case','kind':'ia'})
        self.assertEqual(ticket_channel_name(later),'case-008')

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
