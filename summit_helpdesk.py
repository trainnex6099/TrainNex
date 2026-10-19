"""Summit's private tickets, IA cases, transcript logging, and report delivery.

Ticket state lives on the existing persistent Railway volume. A protected copy
is published to the WCSO portal; ticket messages are never made publicly readable.
"""
import asyncio
from datetime import datetime, timezone
import html
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import discord

GUILD_ID = 1516305981078900756
EMPLOYEE_ROLE_ID = 1516647746025558017
TOPICS = ['General questions', 'Reports or complaints', 'Termination appeal',
          'Application assistance', 'Server partnerships', 'Leaving / retiring from WCSO', 'Other']
API_PATH = '/api/summit/helpdesk'


def now():
    return datetime.now(timezone.utc).isoformat()


def staff(member, ticket, owner_id):
    return str(member.id) == str(owner_id) or any(
        str(role.id) in ticket.get('staffRoleIds', []) for role in member.roles)


def can_open(member, kind, config):
    return kind == 'support' or any(
        str(role.id) in config[kind].get('openerRoleIds', []) for role in member.roles)


def serialize_message(message):
    return {'id':str(message.id),'authorId':str(message.author.id),'authorName':message.author.display_name,
            'avatarUrl':str(message.author.display_avatar.url),'bot':message.author.bot,
            'createdAt':message.created_at.isoformat(),'content':getattr(message,'clean_content',message.content),
            'attachments':[a.url for a in message.attachments],
            'embeds':[e.to_dict() for e in message.embeds],
            'controls':[c.label for row in message.components for c in getattr(row,'children',[]) if getattr(c,'label',None)]}


def transcript_html(record):
    esc = lambda text: html.escape(str(text), quote=True)
    def rich(message):
        return ''.join('<section style="border-left:4px solid #2f946b;padding:1rem;margin-top:1rem"><b>' + esc(e.get('title','')) + '</b><pre>' + esc(e.get('description','')) + '</pre>' + ''.join('<b>'+esc(f.get('name',''))+'</b><pre>'+esc(f.get('value',''))+'</pre>' for f in e.get('fields',[])) + '</section>' for e in message.get('embeds',[]))
    rows = ''.join('<article><b>' + esc(m['authorName']) + '</b> <small>' + esc(m['createdAt']) +
                   '</small><pre>' + esc(m['content']) + '</pre>' +
                   rich(m) + ''.join('<p>' + esc(a) + '</p>' for a in m.get('attachments', [])) + '</article>'
                   for m in record.get('messages', []))
    return ('<!doctype html><meta charset="utf-8"><title>Summit ticket transcript</title>'
            '<style>body{font:16px Arial;margin:3rem;max-width:75rem;background:#f2f0e9;color:#17231d}'
            'article{padding:1rem;background:white;border:1px solid #d9d4c6;margin:1rem 0}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}small{color:#69736c}</style>'
            '<h1>WCSO Roleplay - ' + esc(record['kind']) + ' #' + esc(record['number']) + '</h1>'
            '<p>Opened by ' + esc(record['openerName']) + ' | Topic: ' + esc(record['topic']) + '</p>'
            '<p>Close reason: ' + esc(record.get('closeReason', 'No reason specified')) + '</p>' + rows).encode()


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class TicketStore:
    def __init__(self, path=None):
        self.path = Path(path or Path(os.getenv('SUMMIT_DATA_DIR', str(Path(__file__).parent))) / 'summit-helpdesk.db')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            # Support and IA have independent sequences; IA follows four previous cases.
            columns = db.execute('PRAGMA table_info(tickets)').fetchall()
            if any(row[1] == 'number' and row[5] for row in columns):
                db.execute('ALTER TABLE tickets RENAME TO tickets_legacy')
                db.execute('CREATE TABLE tickets (id TEXT PRIMARY KEY, number INTEGER NOT NULL, record TEXT NOT NULL)')
                db.execute('INSERT INTO tickets SELECT id,number,record FROM tickets_legacy')
                db.execute('DROP TABLE tickets_legacy')
            db.execute('CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, number INTEGER NOT NULL, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS counters (kind TEXT PRIMARY KEY, next_number INTEGER NOT NULL)')
            for kind, first in [('support', 1), ('ia', 5)]:
                records = [json.loads(r[0]) for r in db.execute('SELECT record FROM tickets').fetchall()]
                next_number = max([first] + [r['number'] + 1 for r in records if r['kind'] == kind])
                db.execute('INSERT INTO counters VALUES (?,?) ON CONFLICT(kind) DO UPDATE SET next_number=MAX(next_number,excluded.next_number)', (kind,next_number))
            db.execute('CREATE TABLE IF NOT EXISTS panels (kind TEXT PRIMARY KEY, channel_id TEXT NOT NULL, message_id TEXT NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.path, factory=ManagedConnection)

    def get(self, ticket_id):
        with self.connect() as db:
            row = db.execute('SELECT record FROM tickets WHERE id=?', (str(ticket_id),)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self):
        with self.connect() as db:
            rows = db.execute('SELECT record FROM tickets ORDER BY number').fetchall()
        return [json.loads(row[0]) for row in rows]

    def create(self, record):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT record FROM tickets WHERE id=?', (record['id'],)).fetchone()
            if existing:
                return json.loads(existing[0])
            record['number'] = db.execute('SELECT next_number FROM counters WHERE kind=?', (record['kind'],)).fetchone()[0]
            record['pendingBackup'] = True
            db.execute('UPDATE counters SET next_number=next_number+1 WHERE kind=?', (record['kind'],))
            db.execute('INSERT INTO tickets (id,number,record) VALUES (?,?,?)', (record['id'],record['number'],json.dumps(record)))
        return record

    def save(self, record):
        record['pendingBackup'] = True
        with self.connect() as db:
            db.execute('UPDATE tickets SET record=? WHERE id=?', (json.dumps(record), record['id']))

    def mark_clean(self, record):
        record['pendingBackup'] = False
        with self.connect() as db:
            db.execute('UPDATE tickets SET record=? WHERE id=?', (json.dumps(record), record['id']))

    def restore(self, record):
        if self.get(record['id']):
            return
        with self.connect() as db:
            # Preserve each sequence and prevent reused numbers after volume recovery.
            db.execute('INSERT OR IGNORE INTO tickets (number,id,record) VALUES (?,?,?)',
                       (record['number'], record['id'], json.dumps(record)))
            db.execute('UPDATE counters SET next_number=MAX(next_number,?) WHERE kind=?', (record['number']+1,record['kind']))

    def advance(self, kind, number):
        with self.connect() as db:
            db.execute('UPDATE counters SET next_number=MAX(next_number,?) WHERE kind=?', (number,kind))

    def observe_channels(self, channels):
        for channel in channels:
            match = re.search(r'(ticket|case)-(\d+)',channel.name)
            if match:
                self.advance('support' if match[1]=='ticket' else 'ia',int(match[2])+1)


class IntakeModal(discord.ui.Modal):
    def __init__(self, service, kind):
        super().__init__(title='Open a ticket' if kind == 'support' else 'Open IA investigation', timeout=300)
        self.service, self.kind = service, kind
        if kind == 'support':
            self.choice = discord.ui.Select(options=[discord.SelectOption(label=s, value=str(i)) for i, s in enumerate(TOPICS)], min_values=1, max_values=1)
            self.add_item(discord.ui.Label(text='What do you need help with?', component=self.choice))
        else:
            self.subject = discord.ui.TextInput(max_length=100, required=True)
            self.add_item(discord.ui.Label(text='Investigation title', component=self.subject))

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            topic = TOPICS[int(self.choice.values[0])] if self.kind == 'support' else str(self.subject.value)
            record = await self.service.open(interaction, self.kind, topic)
            await interaction.followup.send(f"Your {'IA investigation' if self.kind == 'ia' else 'ticket'} is ready: <#{record['channelId']}>", ephemeral=True)
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            await interaction.followup.send('Summit could not finish opening this ticket. Check for an existing ticket before trying again, or contact the portal owner.', ephemeral=True)


class OpeningPanel(discord.ui.View):
    def __init__(self, service, kind):
        super().__init__(timeout=None)
        self.service, self.kind = service, kind
        button = discord.ui.Button(label='Open a ticket!' if kind == 'support' else 'Open IA investigation', emoji='📩', style=discord.ButtonStyle.primary, custom_id=f'summit:open:{kind}')
        button.callback = self.open
        self.add_item(button)

    async def open(self, interaction):
        try:
            config = await self.service.config()
            member = await self.service.member(interaction)
            if not config[self.kind]['enabled']:
                raise ValueError('This ticket panel is currently paused.')
            if not can_open(member, self.kind, config):
                raise PermissionError('An authorized IA opening role is required.')
            await interaction.response.send_modal(IntakeModal(self.service, self.kind))
        except (ValueError, PermissionError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
        except Exception:
            await interaction.response.send_message('Ticket settings could not be loaded. Please contact the portal owner.', ephemeral=True)


class ReasonModal(discord.ui.Modal):
    def __init__(self, service, ticket_id, edit=False, log_message_id=None):
        super().__init__(title='Edit close reason' if edit else 'Close ticket', timeout=300)
        self.service, self.ticket_id, self.edit, self.log_message_id = service, ticket_id, edit, log_message_id
        self.reason = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=1000, required=False)
        self.add_item(discord.ui.Label(text='Reason', description='Leave blank to record no reason specified.', component=self.reason))

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.service.close(interaction, self.ticket_id, str(self.reason.value).strip(), self.edit, self.log_message_id)
            await interaction.followup.send('Close reason updated.' if self.edit else 'Ticket closed and transcript saved.', ephemeral=True)
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            await interaction.followup.send('Summit could not complete the transcript update. The ticket and its messages have been retained; please contact the owner.', ephemeral=True)


class TicketActions(discord.ui.View):
    def __init__(self, service, ticket_id, closed=False):
        super().__init__(timeout=None)
        for action, label, style in [('claim', 'Claim ticket', discord.ButtonStyle.primary), ('close', 'Close ticket', discord.ButtonStyle.danger), ('reopen', 'Reopen ticket', discord.ButtonStyle.secondary)]:
            button = discord.ui.Button(label=label, style=style, custom_id=f'summit:{action}:{ticket_id}', disabled=(action != 'reopen' if closed else action == 'reopen'))
            async def callback(interaction, selected=action):
                if selected == 'close':
                    await interaction.response.send_modal(ReasonModal(service, ticket_id))
                    return
                await interaction.response.defer(ephemeral=True, thinking=True)
                try:
                    await service.manage(interaction, ticket_id, selected)
                    await interaction.followup.send('Ticket claimed.' if selected == 'claim' else 'Ticket reopened.', ephemeral=True)
                except (ValueError, PermissionError) as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                except Exception:
                    await interaction.followup.send('The ticket could not be updated. Please contact the owner.', ephemeral=True)
            button.callback = callback
            self.add_item(button)


class TranscriptActions(discord.ui.View):
    def __init__(self, service, ticket_id):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label='View Online Transcript', emoji='📄', url=f'{service.api.base_url}/tickets/{ticket_id}'))
        button = discord.ui.Button(label='Edit Reason', emoji='✏️', style=discord.ButtonStyle.secondary, custom_id=f'summit:reason:{ticket_id}')
        async def edit(interaction):
            await interaction.response.send_modal(ReasonModal(service, ticket_id, edit=True, log_message_id=str(interaction.message.id)))
        button.callback = edit
        self.add_item(button)
        reopen = discord.ui.Button(label='Reopen ticket', style=discord.ButtonStyle.secondary, custom_id=f'summit:reopen:{ticket_id}')
        async def reopen_ticket(interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await service.manage(interaction, ticket_id, 'reopen')
                record = service.store.get(ticket_id)
                await interaction.followup.send(f"Ticket reopened: <#{record['channelId']}>", ephemeral=True)
            except (ValueError, PermissionError) as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
            except Exception:
                await interaction.followup.send('The case could not be reopened. Its archived transcript has been retained.', ephemeral=True)
        reopen.callback = reopen_ticket
        self.add_item(reopen)


class SummitHelpdesk:
    def __init__(self, bot, api, path=None):
        self.bot, self.api, self.store = bot, api, TicketStore(path)
        self.locks = {}
        self.restored = False

    def setup(self):
        for kind in ('support', 'ia'):
            self.bot.add_view(OpeningPanel(self, kind))
        for record in self.store.all():
            self.bot.add_view(TicketActions(self, record['id'], record['status'] == 'closed'))
            if record.get('logMessageId'):
                self.bot.add_view(TranscriptActions(self, record['id']), message_id=int(record['logMessageId']))

    async def config(self):
        data = await self.api.get(API_PATH + '?configuration=1')
        return {**data['config'], 'ownerId': str(data.get('ownerId', ''))}

    async def restore(self):
        if self.restored or not self.api.configured:
            return
        cursor = 0
        while True:
            data = await self.api.get(API_PATH + '?cursor=' + str(cursor))
            for record in data.get('tickets', []):
                if str(record.get('guildId')) == str(GUILD_ID):
                    self.store.restore(record)
            cursor = data.get('nextCursor')
            if cursor is None:
                break
        self.setup()
        self.restored = True

    async def member(self, interaction):
        if not interaction.guild or interaction.guild.id != GUILD_ID:
            raise PermissionError('Use this panel in the WCSO Discord server.')
        return await interaction.guild.fetch_member(interaction.user.id)

    def lock(self, key):
        return self.locks.setdefault(str(key), asyncio.Lock())

    async def publish_record(self, record):
        remote = dict(record)
        if len(remote.get('messages', [])) > 5000 or len(json.dumps({'ticket': remote}).encode('utf-8')) > 950000:
            remote['messages'] = []
            remote['transcriptTruncated'] = True
        response = await self.api.put(API_PATH, {'ticket': remote})
        if not response.get('ok'):
            raise RuntimeError('Ticket backup not confirmed')
        self.store.mark_clean(record)

    async def synchronize(self):
        await self.restore()
        config = None
        for candidate in self.store.all():
            async with self.lock(candidate['id']):
                record = self.store.get(candidate['id'])
                if not record.get('pendingBackup') and not (record['status'] == 'closed' and not record.get('channelDeleted')):
                    continue
                if config is None:
                    config = await self.config()
                if record['status'] == 'closed' and not record.get('logMessageId'):
                    await self.log_ticket(record, config, True)
                elif record['status'] in ('open', 'claimed') and not record.get('openLogMessageId'):
                    await self.log_ticket(record, config, False)
                await self.publish_record(record)
                if record['status'] == 'closed':
                    await self.archive_channel(record)

    def private_destination(self, guild, channel_id, allowed_roles, owner_id):
        channel = guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
        if not isinstance(channel, discord.TextChannel):
            raise ValueError('Configure a text channel for this inbox.')
        allowed = set(allowed_roles)
        if channel.permissions_for(guild.default_role).view_channel:
            raise ValueError('The transcript/report channel must be private to the authorized team.')
        for role in guild.roles:
            if role.is_default() or role.permissions.administrator or str(role.id) in allowed:
                continue
            if channel.permissions_for(role).view_channel:
                raise ValueError('This inbox is visible to roles outside its authorized team. Restrict its permissions before continuing.')
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Member) and target.id not in (guild.me.id, int(owner_id or 0)) and not staff(target, {'staffRoleIds': allowed_roles}, owner_id) and overwrite.view_channel:
                raise ValueError('This inbox grants access to a member outside its authorized team.')
        return channel

    async def open(self, interaction, kind, topic):
        await self.restore()
        member = await self.member(interaction)
        config = await self.config()
        panel = config[kind]
        if not panel['enabled'] or not can_open(member, kind, config):
            raise PermissionError('This panel is paused or you do not have permission to open this case.')
        async with self.lock(f'open:{kind}:{member.id}'):
            existing = self.store.get(str(interaction.id))
            if existing and existing.get('channelId'):
                return existing
            if sum(r['openerId'] == str(member.id) and r['status'] != 'closed' for r in self.store.all()) >= 3:
                raise ValueError('You already have three open tickets. Please use an existing ticket.')
            guild = interaction.guild
            # Continue the server's existing ticket sequence without reusing IDs.
            self.store.observe_channels(guild.text_channels)
            category = guild.get_channel(int(panel['categoryId'])) if panel['categoryId'] else None
            if not isinstance(category, discord.CategoryChannel):
                raise ValueError('The active-ticket category has not been configured correctly.')
            if kind == 'ia' and panel.get('transcriptChannelId'):
                self.private_destination(guild, panel['transcriptChannelId'], panel['staffRoleIds'], config['ownerId'])
            record = existing or self.store.create({'id':str(interaction.id), 'guildId':str(GUILD_ID), 'kind':kind, 'topic':topic[:100], 'openerId':str(member.id), 'openerName':member.display_name, 'staffRoleIds':panel['staffRoleIds'], 'transcriptChannelId':panel['transcriptChannelId'], 'status':'opening', 'openedAt':now(), 'messages':[]})
            overwrites = {guild.default_role:discord.PermissionOverwrite(view_channel=False), guild.me:discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True), member:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,attach_files=True)}
            for role_id in panel['staffRoleIds']:
                role = guild.get_role(int(role_id))
                if role and role != guild.default_role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,attach_files=True)
            owner = guild.get_member(int(config['ownerId'])) if config['ownerId'].isdigit() else None
            if owner:
                overwrites[owner] = discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
            channel = next((ch for ch in guild.text_channels if ch.topic == f"summit-ticket:{record['id']}"), None)
            if channel is None:
                channel = await guild.create_text_channel(f"{'case' if kind=='ia' else 'ticket'}-{record['number']}",category=category,overwrites=overwrites,topic=f"summit-ticket:{record['id']}",reason='Summit ticket requested by member')
            record.update(channelId=str(channel.id),categoryId=str(category.id),status='open')
            self.store.save(record)
            embed = discord.Embed(title=f"{'IA investigation' if kind=='ia' else 'Support ticket'} #{record['number']}",description=f"**Topic:** {record['topic']}\nExplain what you need below. Do not share passwords or account credentials.",colour=0x143E31)
            message = await channel.send(embed=embed,view=TicketActions(self,record['id']),allowed_mentions=discord.AllowedMentions.none())
            record['controlMessageId'] = str(message.id)
            self.store.save(record)
            await self.log_ticket(record, config, closed=False)
            await self.publish_record(record)
            return record

    def log_embed(self, record, closed, public=False):
        embed=discord.Embed(title='Ticket Closed' if closed else 'Ticket Opened',colour=0x2ECC71,timestamp=datetime.now(timezone.utc))
        if public:
            embed.add_field(name='Ticket ID',value=str(record['number']))
            embed.description='The full conversation and case details are available to the ticket opener and authorized staff through the portal.'
            embed.set_footer(text='Summit · WCSO Roleplay Support')
            return embed
        fields=[('Ticket ID',str(record['number'])),('Opened By',f"{record['openerName']} ({record['openerId']})"),('Closed By',record.get('closedByName','Not closed')),('Open Time',record['openedAt']),('Claimed By',record.get('claimedByName','Unclaimed')),('Reason',record.get('closeReason') or 'No reason specified')]
        for name,value in fields:embed.add_field(name=name,value=value[:1024],inline=name!='Reason')
        embed.set_footer(text=f"Summit · {'Internal Affairs' if record['kind']=='ia' else record['topic']}")
        return embed

    async def log_ticket(self, record, config, closed):
        if not record.get('transcriptChannelId'):
            return
        guild=self.bot.get_guild(GUILD_ID)
        public=record['kind']=='support'
        channel=guild.get_channel(int(record['transcriptChannelId'])) if public else self.private_destination(guild,record['transcriptChannelId'],record['staffRoleIds'],config['ownerId'])
        if not isinstance(channel,discord.TextChannel):raise ValueError('Configure a text channel for transcript notices.')
        kwargs={'embed':self.log_embed(record,closed,public),'allowed_mentions':discord.AllowedMentions.none()}
        if closed:
            if not public:kwargs['file']=discord.File(io.BytesIO(transcript_html(record)),filename=f"summit-{record['kind']}-{record['number']}.html")
            kwargs['view']=TranscriptActions(self,record['id'])
        message=await channel.send(**kwargs)
        if closed:
            record.update(logMessageId=str(message.id),archiveUrl=message.jump_url,fullArchiveLogged=not public)
            self.store.save(record)
            await self.publish_record(record)
        else:
            record['openLogMessageId']=str(message.id)
            self.store.save(record)

    async def manage(self, interaction, ticket_id, action):
        async with self.lock(ticket_id):
            record=self.store.get(ticket_id)
            member=await self.member(interaction)
            config=await self.config()
            if not record or not staff(member,record,config['ownerId']):
                raise PermissionError('An authorized handling staff role is required.')
            if action=='claim':
                if record['status']=='closed':raise ValueError('This ticket is closed.')
                if record.get('claimedBy') and record['claimedBy']!=str(member.id):raise ValueError('Another staff member has already claimed this ticket.')
                record.update(claimedBy=str(member.id),claimedByName=member.display_name,status='claimed')
            elif action=='reopen':
                if record['status']!='closed':raise ValueError('This ticket is already open.')
                channel=interaction.guild.get_channel(int(record['channelId']))
                if not isinstance(channel,discord.TextChannel):
                    guild=interaction.guild
                    category_id=record.get('categoryId') or config[record['kind']]['categoryId']
                    category=guild.get_channel(int(category_id)) if category_id else None
                    if not isinstance(category,discord.CategoryChannel):raise ValueError('Configure the case category before reopening.')
                    opener=await guild.fetch_member(int(record['openerId']))
                    overwrites={guild.default_role:discord.PermissionOverwrite(view_channel=False),guild.me:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,manage_channels=True),opener:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,attach_files=True),member:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)}
                    for role_id in record['staffRoleIds']:
                        role=guild.get_role(int(role_id))
                        if role and role!=guild.default_role:overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,attach_files=True)
                    channel=await guild.create_text_channel(f"{'case' if record['kind']=='ia' else 'ticket'}-{record['number']}",category=category,overwrites=overwrites,topic=f"summit-ticket:{record['id']}",reason='Summit case reopened by staff')
                    controls=await channel.send('Previous conversation is preserved in the online transcript.',view=TicketActions(self,record['id']),allowed_mentions=discord.AllowedMentions.none())
                    record.update(channelId=str(channel.id),controlMessageId=str(controls.id))
                await self.permissions(channel,record,True)
                record.setdefault('closures',[]).append({key:record.get(key) for key in ('closedAt','closedBy','closedByName','closeReason','logMessageId','archiveUrl')})
                record.update(status='open',closedAt=None,closedBy=None,closedByName=None,closeReason=None,channelDeleted=False,logMessageId=None)
                await channel.send('This ticket was reopened by authorized staff.',allowed_mentions=discord.AllowedMentions.none())
            self.store.save(record)
            await self.publish_record(record)
            await self.update_controls(record)

    async def permissions(self, channel, record, enabled):
        opener=channel.guild.get_member(int(record['openerId']))
        if opener:await channel.set_permissions(opener,view_channel=True,read_message_history=True,send_messages=enabled,attach_files=enabled)
        for role_id in record['staffRoleIds']:
            role=channel.guild.get_role(int(role_id))
            if role:await channel.set_permissions(role,view_channel=True,read_message_history=True,send_messages=enabled,attach_files=enabled)

    async def update_controls(self, record):
        channel=self.bot.get_channel(int(record['channelId']))
        if channel and record.get('controlMessageId'):
            try:
                message=await channel.fetch_message(int(record['controlMessageId']))
                await message.edit(view=TicketActions(self,record['id'],record['status']=='closed'))
            except discord.NotFound:
                pass

    async def archive_channel(self, record):
        """Delete only after the portal backup and any configured transcript log succeed."""
        if record.get('channelDeleted'):
            return
        if record.get('transcriptChannelId') and not record.get('logMessageId'):
            raise ValueError('Save the transcript log before removing the ticket channel.')
        oversized = len(record.get('messages', [])) > 5000 or len(json.dumps({'ticket':record}).encode('utf-8')) > 950000
        if oversized and not record.get('fullArchiveLogged'):
            raise ValueError('A full transcript channel is required for this large case. Its channel has been retained.')
        channel = self.bot.get_channel(int(record['channelId']))
        try:
            if channel is None:
                channel = await self.bot.fetch_channel(int(record['channelId']))
            if str(channel.guild.id) != str(record['guildId']):
                raise ValueError('Ticket channel server does not match.')
            await channel.delete(reason='Summit ticket closed; transcript archived')
        except discord.NotFound:
            pass
        record['channelDeleted'] = True
        self.store.save(record)
        await self.publish_record(record)

    async def close(self, interaction, ticket_id, reason, edit=False, log_message_id=None):
        async with self.lock(ticket_id):
            record=self.store.get(ticket_id)
            member=await self.member(interaction)
            config=await self.config()
            authorized=record and staff(member,record,config['ownerId'])
            if not record or (not authorized and (edit or str(member.id)!=record['openerId'])):
                raise PermissionError('Only the opener or authorized staff may close this ticket. Editing reasons is staff-only.')
            if edit:
                if record['status']!='closed' or log_message_id!=record.get('logMessageId'):
                    raise ValueError('This closure record has been superseded.')
                record['closeReason']=reason or 'No reason specified'
                self.store.save(record)
                await self.publish_record(record)
                channel=self.bot.get_channel(int(record['transcriptChannelId']))
                if channel:
                    message=await channel.fetch_message(int(record['logMessageId']))
                    if record['kind']=='support':
                        await message.edit(embed=self.log_embed(record,True,True))
                    else:
                        await message.edit(embed=self.log_embed(record,True), attachments=[discord.File(io.BytesIO(transcript_html(record)), filename=f"summit-{record['kind']}-{record['number']}.html")])
                return
            if record['status']=='closed':raise ValueError('This ticket is already closed.')
            channel=interaction.guild.get_channel(int(record['channelId']))
            if not isinstance(channel,discord.TextChannel):raise ValueError('The ticket channel is unavailable.')
            messages={m['id']:m for m in record.get('messages',[])}
            async for m in channel.history(limit=None,oldest_first=True):
                messages[str(m.id)]=serialize_message(m)
            record.update(messages=list(messages.values()),closeReason=reason or 'No reason specified',closedBy=str(member.id),closedByName=member.display_name,closedAt=now(),status='closed')
            await self.permissions(channel,record,False)
            self.store.save(record)
            await self.log_ticket(record,config,True)
            await self.publish_record(record)
            await self.archive_channel(record)

    async def execute(self, action, actor_id):
        config=await self.config()
        guild=self.bot.get_guild(GUILD_ID)
        if not guild:raise ValueError('Summit is not connected to WCSO.')
        member=await guild.fetch_member(int(actor_id))
        if action['action']=='helpdesk.publish':
            if str(actor_id)!=config['ownerId']:raise PermissionError('Only the configured portal owner can publish ticket panels.')
            kind=action.get('kind')
            if kind not in ('support','ia') or not config[kind]['enabled']:raise ValueError('Configure and enable this panel first.')
            panel=config[kind]
            if not panel['categoryId'] or not isinstance(guild.get_channel(int(panel['categoryId'])),discord.CategoryChannel):raise ValueError('Choose an active-ticket category.')
            if kind=='ia' and panel['transcriptChannelId']:self.private_destination(guild,panel['transcriptChannelId'],panel['staffRoleIds'],config['ownerId'])
            if kind=='support' and panel['transcriptChannelId'] and not isinstance(guild.get_channel(int(panel['transcriptChannelId'])),discord.TextChannel):raise ValueError('Configure the support transcript notice channel.')
            channel=guild.get_channel(int(panel['panelChannelId']))
            if not isinstance(channel,discord.TextChannel):raise ValueError('Opening panel channel is unavailable.')
            embed=discord.Embed(title='Open a ticket!' if kind=='support' else 'Open an IA investigation',description='Click the button, choose the reason in the pop-up, and submit to open a private ticket.' if kind=='support' else 'Authorized personnel can open a private Internal Affairs investigation.',colour=0x143E31)
            embed.set_footer(text='Summit · Whatcom County Sheriff’s Office Roleplay')
            with self.store.connect() as db:old=db.execute('SELECT channel_id,message_id FROM panels WHERE kind=?',(kind,)).fetchone()
            message=None
            if old and old[0]==str(channel.id):
                try:
                    message=await channel.fetch_message(int(old[1]));await message.edit(embed=embed,view=OpeningPanel(self,kind))
                except discord.NotFound:message=None
            if message is None:message=await channel.send(embed=embed,view=OpeningPanel(self,kind),allowed_mentions=discord.AllowedMentions.none())
            with self.store.connect() as db:db.execute('INSERT INTO panels VALUES (?,?,?) ON CONFLICT(kind) DO UPDATE SET channel_id=excluded.channel_id,message_id=excluded.message_id',(kind,str(channel.id),str(message.id)))
            return {'status':'panel published','channelId':str(channel.id)}
        raise ValueError('Unsupported helpdesk action.')
