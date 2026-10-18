"""Portal command transport. Discord remains the executor for both entry points.

Only the signed-in Discord ID from the trusted portal is accepted as actor.
The bot resolves current Discord membership and checks roles again at execution.
"""
import json
import time
import summit_store as store

PATH = '/api/summit/operations'


def init():
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS portal_receipts (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT NOT NULL,
            updated_at INTEGER NOT NULL)''')


def receipt(command_id):
    init()
    with store.connect() as db:
        row = db.execute('SELECT * FROM portal_receipts WHERE id=?', (command_id,)).fetchone()
    return dict(row) if row else None


def remember(command_id, status, result):
    init()
    with store.connect() as db:
        db.execute('''INSERT INTO portal_receipts VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET status=excluded.status,
            result=excluded.result,updated_at=excluded.updated_at''',
            (command_id, status, json.dumps(result), int(time.time())))


async def process(api, execute):
    payload = await api.get(PATH)
    for item in payload.get('commands', []):
        command_id = item['id']
        old = receipt(command_id)
        if not old:
            claim = await api.request('PATCH', PATH, payload={'id': command_id, 'status': 'processing'})
            if not claim.get('claimed'):
                # A prior executor lost its local receipt: never replay blindly.
                if item.get('status') == 'processing':
                    await api.request('PATCH', PATH, payload={'id': command_id, 'status': 'failed',
                        'result': {'error': 'Bot restarted during execution. Check current state before retrying.'}})
                continue
        if old:
            # A restart during execution must never silently perform an action twice.
            status = old['status'] if old['status'] != 'processing' else 'failed'
            result = json.loads(old['result']) if old['status'] != 'processing' else {
                'error': 'Execution was interrupted. Check current state before submitting a new action.'}
        else:
            remember(command_id, 'processing', {})
            try:
                command = item['command']
                action = ({'action':'directory.sync','guildId':command.split(':')[-1]}
                          if command.startswith('sync:discord:') else json.loads(command))
                result = await execute(action, str(item['requestedBy']))
                status = 'completed'
            except (ValueError, PermissionError) as exc:
                status, result = 'failed', {'error': str(exc)}
            except Exception:
                # Do not send exception details or credentials to portal users.
                status, result = 'failed', {'error': 'Execution interrupted. Check current state before retrying.'}
            remember(command_id, status, result)
        await api.request('PATCH', PATH, payload={'id': command_id, 'status': status, 'result': result})


def snapshot(guild_id):
    store.init_shift_db()
    store.init_loa_db()
    with store.connect() as db:
        shifts = [dict(r) for r in db.execute(
            'SELECT * FROM shifts WHERE guild_id=? ORDER BY started_at DESC', (str(guild_id),))]
        loas = [dict(r) for r in db.execute(
            'SELECT * FROM loa_requests WHERE guild_id=? ORDER BY requested_at DESC', (str(guild_id),))]
    return {'guildId': str(guild_id), 'shifts': shifts, 'loas': loas, 'updatedAt': int(time.time())}


async def publish(api, guild_id, training=None):
    state = snapshot(guild_id)
    if training is not None:
        state['training'] = training_wire(training)
    result = await api.put(PATH, state, guild_id)
    if not result.get('ok'):
        raise RuntimeError('Portal did not confirm the state backup')


async def restore(api, guild_id):
    data = await api.get(PATH, guild_id)
    state = data.get('state')
    if not state:
        return None
    # Never import records belonging to a different Discord guild.
    for kind in ('shifts', 'loas'):
        if any(str(r.get('guild_id')) != str(guild_id) for r in state.get(kind, [])):
            raise ValueError('Portal backup contains an invalid guild')
    shifts = state.get('shifts', [])
    types = {r['name']: r['id'] for r in store.list_shift_types(guild_id)}
    for record in shifts:
        if record.get('shift_type_name') in types:
            record['shift_type_id'] = types[record['shift_type_name']]
    # A persistent local volume may contain newer work from an API outage.
    # Discord is the executor; only hydrate empty local guild storage.
    local = snapshot(guild_id)
    if not local['shifts'] and not local['loas']:
        store.upsert_shift_records(shifts)
        store.upsert_loa_records(state.get('loas', []))
    return training_local(state.get('training'))


def training_wire(value):
    # Discord snowflakes exceed JavaScript's safe integer range.
    if isinstance(value, int) and not isinstance(value, bool) and value > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {k: training_wire(v) for k, v in value.items()}
    if isinstance(value, list):
        return [training_wire(v) for v in value]
    return value


def training_local(value, key=''):
    if isinstance(value, str) and value.isdigit() and (key in ('queue', 'attempted_trainers', 'processed_approval_messages') or key.endswith('_id') or key == 'claimed_by'):
        return int(value)
    if isinstance(value, dict):
        return {k: training_local(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [training_local(v, key) for v in value]
    return value
