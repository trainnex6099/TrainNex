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
import time
from pathlib import Path
import sqlite3
from types import SimpleNamespace
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
            if record['kind']=='ia':
                record['topic']=ticket_channel_name(record)
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


def opening_embed(panel, kind):
    options = panel.get('embed') or {}
    title = options.get('title', '📩 Open a ticket!' if kind == 'support' else '🛡️ Open an IA investigation')
    description = options.get('description', 'Click the button, choose the reason in the pop-up, and submit to open a private ticket.' if kind == 'support' else 'Authorized personnel can open a private Internal Affairs investigation.')
    footer = options.get('footer', 'Summit · Whatcom County Sheriff’s Office Roleplay')
    color = options.get('color', '#143e31')
    if not isinstance(title, str) or not 1 <= len(title) <= 256 or not isinstance(description, str) or not 1 <= len(description) <= 4000 or not isinstance(footer, str) or len(footer) > 1024 or not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise ValueError('Opening embed settings are invalid. Save the panel settings again in the portal.')
    embed = discord.Embed(title=title, description=description, colour=int(color[1:], 16))
    if footer:
        embed.set_footer(text=footer)
    return embed


def ticket_channel_name(record):
    return f"case-{record['number']:03d}" if record['kind']=='ia' else f"ticket-{record['number']}"


class IntakeModal(discord.ui.Modal):
    def __init__(self, service, kind, topics=None):
        super().__init__(title='Open a ticket' if kind == 'support' else 'Open IA investigation', timeout=300)
        self.service, self.kind = service, kind
        self.topics = topics if isinstance(topics, list) and 1 <= len(topics) <= 25 and all(isinstance(t, str) and 0 < len(t) <= 100 for t in topics) else TOPICS
        if kind == 'support':
            self.choice = discord.ui.Select(options=[discord.SelectOption(label=s, value=str(i)) for i, s in enumerate(self.topics)], min_values=1, max_values=1)
            self.add_item(discord.ui.Label(text='What do you need help with?', component=self.choice))

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            topic = self.topics[int(self.choice.values[0])] if self.kind == 'support' else 'Internal Affairs investigation'
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
        if self.kind == 'ia':
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                record=await self.service.open(interaction,'ia','Internal Affairs investigation')
                await interaction.followup.send(f"Your IA investigation is ready: <#{record['channelId']}>",ephemeral=True)
            except (ValueError,PermissionError) as exc:
                await interaction.followup.send(str(exc),ephemeral=True)
            except Exception:
                await interaction.followup.send('Summit could not finish opening this case. Check for an existing case before trying again, or contact the portal owner.',ephemeral=True)
            return
        try:
            config = await self.service.config()
            member = await self.service.member(interaction)
            if not config[self.kind]['enabled']:
                raise ValueError('This ticket panel is currently paused.')
            if not can_open(member, self.kind, config):
                raise PermissionError('An authorized IA opening role is required.')
            await interaction.response.send_modal(IntakeModal(self.service, self.kind, config[self.kind].get('topics')))
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
    def __init__(self, service, ticket_id, closed=False, kind='support', claimed=False):
        super().__init__(timeout=None)
        actions = [('reopen', 'Reopen ticket', '🔓', discord.ButtonStyle.secondary)] if closed else [('close-now', 'Close', '🔒', discord.ButtonStyle.danger), ('close', 'Close With Reason', '🔒', discord.ButtonStyle.danger)]
        if not closed and kind == 'support':
            actions.append(('open-ia', 'Open IA Case', '🛡️', discord.ButtonStyle.secondary) if claimed else ('claim', 'Claim', '🙋', discord.ButtonStyle.primary))
        for action, label, emoji, style in actions:
            if kind == 'ia' and action == 'claim':
                continue
            button = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f'summit:{action}:{ticket_id}')
            async def callback(interaction, selected=action):
                if selected == 'close':
                    await interaction.response.send_modal(ReasonModal(service, ticket_id))
                    return
                await interaction.response.defer(ephemeral=True, thinking=True)
                try:
                    if selected == 'open-ia':
                        case = await service.open_ia_from_ticket(interaction, ticket_id)
                        await interaction.followup.send(f"IA case: <#{case['channelId']}>", ephemeral=True)
                        return
                    if selected == 'close-now':
                        await service.close(interaction, ticket_id, '')
                        await interaction.followup.send('Ticket closed and transcript saved.', ephemeral=True)
                        return
                    await service.manage(interaction, ticket_id, selected)
                    await interaction.followup.send('Ticket claimed.' if selected == 'claim' else 'Ticket reopened.', ephemeral=True)
                except (ValueError, PermissionError) as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                except Exception:
                    await interaction.followup.send('The ticket could not be updated. Please contact the owner.', ephemeral=True)
            button.callback = callback
            self.add_item(button)
        if kind == 'ia' and not closed:
            self.add_item(discord.ui.Button(label='Case File / Final Report', emoji='📁', url=f"{service.api.base_url.rstrip('/')}/?section=ia&caseId={ticket_id}"))


class TranscriptActions(discord.ui.View):
    def __init__(self, service, ticket_id):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label='View Online Transcript', emoji='📄', url=f'{service.api.base_url}/tickets/{ticket_id}'))
        button = discord.ui.Button(label='Edit Reason', emoji='✏️', style=discord.ButtonStyle.secondary, custom_id=f'summit:reason:{ticket_id}')
        async def edit(interaction):
            await interaction.response.send_modal(ReasonModal(service, ticket_id, edit=True, log_message_id=str(interaction.message.id)))
        button.callback = edit
        self.add_item(button)
        reopen = discord.ui.Button(label='Reopen ticket', emoji='🔓', style=discord.ButtonStyle.secondary, custom_id=f'summit:reopen:{ticket_id}')
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
            self.bot.add_view(TicketActions(self, record['id'], record['status'] == 'closed', record['kind'], record['status'] == 'claimed'))
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

    async def synchronize_members(self):
        # A separate member sync must not interrupt ticket or signing processing.
        if not hasattr(self, '_members_synced') or time.monotonic()-self._members_synced>300:
            guild = self.bot.get_guild(GUILD_ID)
            if guild:
                try:
                    if not guild.chunked:
                        await guild.chunk(cache=True)
                    members = sorted([{'id':str(m.id), 'username':m.name,
                                       'displayName':m.display_name,
                                       'avatarUrl':str(m.display_avatar.replace(size=128).url)}
                                      for m in guild.members if not m.bot], key=lambda m:m['id'])
                    result = await self.api.put('/api/internal-affairs/members',
                                                {'guildId':str(GUILD_ID), 'members':members})
                    if result.get('ok'):
                        self._members_synced = time.monotonic()
                except Exception:
                    # Retry on the next interval without logging member data or credentials.
                    self._members_synced = time.monotonic()-240

    async def synchronize(self):
        await self.restore()
        if not hasattr(self,'_investigators_synced') or time.monotonic()-self._investigators_synced>300:
            guild=self.bot.get_guild(GUILD_ID)
            if guild:
                investigators=[{'id':str(m.id),'name':m.display_name} for m in guild.members if not m.bot and any(str(role.id)=='1516461975239004340' for role in m.roles)]
                await self.api.put('/api/internal-affairs/signing',{'investigators':investigators})
                self._investigators_synced=time.monotonic()
        await self.synchronize_members()
        config = None
        for candidate in self.store.all():
            async with self.lock(candidate['id']):
                record = self.store.get(candidate['id'])
                refresh_controls = record['status'] in ('open','claimed') and record.get('controlVersion') != (5 if record['kind']=='ia' else 4)
                if not refresh_controls and not record.get('pendingBackup') and not (record['status'] == 'closed' and not record.get('channelDeleted')):
                    continue
                if config is None:
                    config = await self.config()
                if record['status'] == 'closed' and not record.get('logMessageId'):
                    await self.log_ticket(record, config, True)
                if refresh_controls:
                    await self.update_controls(record)
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

    async def open_ia_from_ticket(self, interaction, ticket_id):
        async with self.lock(ticket_id):
            source = self.store.get(ticket_id)
            member = await self.member(interaction)
            config = await self.config()
            if not source or not staff(member, source, config['ownerId']) or not can_open(member, 'ia', config):
                raise PermissionError('An authorized ticket handling role and IA opening role are required.')
            if source['kind'] != 'support' or source['status'] != 'claimed' or not source.get('claimedBy'):
                raise ValueError('Claim this support ticket before opening an IA case.')
            # A stable ID and the existing open lock prevent duplicate cases on retries.
            request = SimpleNamespace(id=f"ia-from-{source['id']}", guild=interaction.guild, user=interaction.user)
            case = await self.open(request, 'ia', 'Internal Affairs investigation', source_ticket=source)
            source.update(linkedIaCaseId=case['id'], linkedIaChannelId=case['channelId'])
            self.store.save(source)
            await self.publish_record(source)
            return case

    async def open(self, interaction, kind, topic, source_ticket=None):
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
            if source_ticket:
                record.update(sourceTicketId=source_ticket['id'], sourceTicketNumber=source_ticket['number'], sourceChannelId=source_ticket['channelId'], sourceTopic=source_ticket['topic'], sourceOpenerId=source_ticket['openerId'])
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
                channel = await guild.create_text_channel(ticket_channel_name(record),category=category,overwrites=overwrites,topic=f"summit-ticket:{record['id']}",reason='Summit ticket requested by member')
            record.update(channelId=str(channel.id),categoryId=str(category.id),status='open')
            self.store.save(record)
            message = await channel.send(**self.intake_notice(record),embeds=self.intake_embeds(record),view=TicketActions(self,record['id'],kind=record['kind']))
            record['controlMessageId'] = str(message.id)
            record['controlVersion'] = 5 if kind=='ia' else 4
            self.store.save(record)
            await self.publish_record(record)
            return record

    def intake_notice(self, record):
        if record['kind']=='ia':
            return {'content':None,'allowed_mentions':discord.AllowedMentions.none()}
        return {'content':' '.join(f'<@&{role}>' for role in record['staffRoleIds']) or None,'allowed_mentions':discord.AllowedMentions(everyone=False,users=False,roles=[discord.Object(id=int(role)) for role in record['staffRoleIds']])}

    def intake_embeds(self, record):
        greeting=discord.Embed(description='Thank you for contacting support.\nPlease describe your issue and wait for a response.' if record['kind']=='support' else 'Internal Affairs intake is ready.\nPlease provide the details of this investigation below.',colour=0x143E31)
        greeting.set_footer(text='Summit · Whatcom County Sheriff’s Office Roleplay')
        topic=discord.Embed(title='Support' if record['kind']=='support' else 'Internal Affairs',description=record['topic'],colour=0x143E31)
        topic.set_footer(text=f"{ticket_channel_name(record)} · Internal Affairs" if record['kind']=='ia' else f"Ticket #{record['number']} · " + (f"Claimed by {record['claimedByName']}" if record.get('claimedByName') else 'Awaiting staff'))
        if record['kind']=='ia' and record.get('sourceTicketId'):
            topic.add_field(name='Source ticket', value=f"[Ticket #{record['sourceTicketNumber']}](https://discord.com/channels/{record['guildId']}/{record['sourceChannelId']})", inline=False)
            topic.add_field(name='Ticket topic', value=record['sourceTopic'][:1024], inline=False)
            topic.add_field(name='Ticket opened by', value=f"<@{record['sourceOpenerId']}>", inline=False)
        return [greeting,topic]

    def log_embed(self, record, closed, public=False):
        embed=discord.Embed(title='🔒 Ticket Closed' if closed else 'Ticket Opened',colour=0x2ECC71,timestamp=datetime.now(timezone.utc))
        if public:
            embed.add_field(name='🌐 Ticket ID',value=str(record['number']))
            embed.description='Ticket transcripts are available only to WCSO Highranks through the protected portal link.'
            embed.set_footer(text='Summit · WCSO Roleplay Support')
            return embed
        fields=[('🌐 Ticket ID',str(record['number'])),('👤 Opened By',f"<@{record['openerId']}>"),('🔒 Closed By',f"<@{record['closedBy']}>" if record.get('closedBy') else record.get('closedByName','Unknown')),('🕒 Open Time',record['openedAt']),('🙋 Claimed By',f"<@{record['claimedBy']}>" if record.get('claimedBy') else 'Unclaimed'),('📝 Reason',record.get('closeReason') or 'No reason specified')]
        for name,value in fields:
            if record['kind']=='ia' and name.endswith('Claimed By'):
                continue
            embed.add_field(name=name,value=value[:1024],inline=not name.endswith('Reason'))
        embed.set_footer(text=f"Summit · {'Internal Affairs' if record['kind']=='ia' else record['topic']}")
        return embed

    async def log_ticket(self, record, config, closed):
        if not closed or record.get('status') != 'closed':
            return
        if not record.get('transcriptChannelId'):
            return
        guild=self.bot.get_guild(GUILD_ID)
        public=record['kind']=='support'
        channel=guild.get_channel(int(record['transcriptChannelId'])) if public else self.private_destination(guild,record['transcriptChannelId'],record['staffRoleIds'],config['ownerId'])
        if not isinstance(channel,discord.TextChannel):raise ValueError('Configure a text channel for transcript notices.')
        kwargs={'embed':self.log_embed(record,closed,public),'allowed_mentions':discord.AllowedMentions.none()}
        if closed:
            # All transcripts use the portal role check; never attach a reusable HTML copy.
            kwargs['view']=TranscriptActions(self,record['id'])
        message=await channel.send(**kwargs)
        if closed:
            record.update(logMessageId=str(message.id),archiveUrl=message.jump_url,fullArchiveLogged=False)
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
                if record['kind']=='ia':raise ValueError('IA investigations do not use ticket claims.')
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
                    channel=await guild.create_text_channel(ticket_channel_name(record),category=category,overwrites=overwrites,topic=f"summit-ticket:{record['id']}",reason='Summit case reopened by staff')
                    controls=await channel.send('Previous closed conversation is retained and will be included when this ticket closes again.',view=TicketActions(self,record['id'],kind=record['kind']),allowed_mentions=discord.AllowedMentions.none())
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
                kwargs={'embeds':self.intake_embeds(record),'view':TicketActions(self,record['id'],record['status']=='closed',record['kind'],record['status']=='claimed'),'allowed_mentions':discord.AllowedMentions.none()}
                if record['kind']=='ia':kwargs['content']=None
                await message.edit(**kwargs)
                record['controlVersion'] = 5 if record['kind']=='ia' else 4
                self.store.save(record)
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
                        await message.edit(embed=self.log_embed(record,True))
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
        if action['action']=='helpdesk.signature-request':
            request_id=str(action.get('requestId',''))
            if not re.fullmatch(r'[0-9a-f-]{36}',request_id):raise ValueError('Invalid signing request.')
            data=await self.api.get('/api/internal-affairs/signing?requestId='+request_id)
            record=data['request']
            if record.get('dm_status')=='sent':return {'status':'signing request already delivered'}
            guild=self.bot.get_guild(GUILD_ID)
            if not guild:raise ValueError('Summit is not connected to WCSO.')
            member=await guild.fetch_member(int(record['signer_id']))
            if str(member.id)!=str(data.get('ownerId')) and not any(str(r.id) in ('1516461975239004340','1526692828946763886') for r in member.roles):
                await self.api.put('/api/internal-affairs/signing',{'requestId':request_id,'status':'failed','error':'The assigned signer no longer has Internal Affairs access.'})
                raise PermissionError('The assigned signer no longer has Internal Affairs access.')
            embed=discord.Embed(title='🖊️ IA documents ready for your signature',description='An Internal Affairs case file and final report are ready. Open the private portal link and sign in with your own Discord account to review and sign both documents.',colour=0x143E31)
            case_label=str(data.get('caseLabel',''))
            if re.fullmatch(r'case-\d{3,12}',case_label):
                embed.add_field(name='Case',value=f'`{case_label}`',inline=False)
            view=discord.ui.View();view.add_item(discord.ui.Button(label='Review & sign documents',url=self.api.base_url.rstrip('/')+data['signingPath']))
            try:
                message=await member.send(embed=embed,view=view,allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                await self.api.put('/api/internal-affairs/signing',{'requestId':request_id,'status':'failed','error':'Discord blocked the DM. Enable direct messages from this server; the assigned signer can also sign in the IA portal.'})
                raise ValueError('Discord could not deliver the signing DM.')
            await self.api.put('/api/internal-affairs/signing',{'requestId':request_id,'status':'sent','messageId':str(message.id)})
            return {'status':'signing request delivered'}
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
            embed=opening_embed(panel, kind)
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
