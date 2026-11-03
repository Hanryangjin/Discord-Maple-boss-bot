"""Transactional application service. Discord adapters never write domain tables."""
from dataclasses import dataclass
import json
import sqlite3
import time
import uuid
from .db import Database
from .validation import UserError, character_name, hexa, key, label


@dataclass(frozen=True)
class Actor:
    guild_id: int
    user_id: int
    admin: bool = False
    role_ids: tuple[int, ...] = ()


CONFIRM = {'character.add', 'character.owner', 'boss.delete', 'group.delete', 'group.merge',
           'job.rename', 'job.deactivate', 'board.publish', 'board.stop', 'boss.order'}
ADMIN = {'settings.role', 'settings.channel', 'boss.delete', 'boss.restore',
         'job.add', 'job.rename', 'job.activate', 'job.deactivate'}
OWN = {'character.add', 'character.edit', 'character.activate', 'character.deactivate'}
TABLES = ('jobs', 'characters', 'bosses', 'boss_groups', 'group_members')


class Service:
    def __init__(self, db: Database):
        self.db = db

    def manager(self, conn, actor):
        row = conn.execute('SELECT manager_role_id FROM guild_settings WHERE guild_id=?', (actor.guild_id,)).fetchone()
        return actor.admin or bool(row and row[0] and row[0] in actor.role_ids)

    def authorize(self, conn, actor, action, payload):
        manager = self.manager(conn, actor)
        if action in ADMIN and not actor.admin:
            raise UserError('서버 관리자만 실행할 수 있습니다.')
        if action not in ADMIN and action not in OWN and not manager:
            raise UserError('파티 배분 관리자 권한이 필요합니다.')
        if action in OWN and not manager:
            owner = payload.get('owner_id', actor.user_id)
            if action != 'character.add':
                owner = self.get(conn, actor.guild_id, 'characters', payload['id'])['owner_id']
            if owner != actor.user_id:
                raise UserError('본인 캐릭터만 관리할 수 있습니다.')
        if action == 'board.publish':
            settings = conn.execute('SELECT * FROM guild_settings WHERE guild_id=?', (actor.guild_id,)).fetchone()
            existing = settings['last_channel_id'] or settings['default_channel_id']
            if payload.get('channel_id', existing) != existing and not actor.admin:
                raise UserError('게시 채널 지정·이전은 서버 관리자 전용입니다.')

    def observe_member(self, guild_id, user_id, name, state):
        if state not in ('present', 'left', 'unknown'):
            raise ValueError(state)
        with self.db.transaction() as c:
            self.db.initialize(c, guild_id)
            old = c.execute('SELECT * FROM discord_users WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
            now = time.time()
            c.execute('''INSERT INTO discord_users VALUES(?,?,?,?,?,?)
                         ON CONFLICT(guild_id,user_id) DO UPDATE SET display_name=excluded.display_name,
                         membership=excluded.membership, observed_at=excluded.observed_at,
                         last_present_at=COALESCE(excluded.last_present_at,discord_users.last_present_at)''',
                      (guild_id, user_id, name, state, now, now if state == 'present' else None))
            if not old or old['membership'] != state or old['display_name'] != name:
                self.touch(c, guild_id)

    @staticmethod
    def get(c, guild, table, entity_id):
        if table not in TABLES:
            raise ValueError(table)
        row = c.execute(f'SELECT * FROM {table} WHERE guild_id=? AND id=?', (guild, entity_id)).fetchone()
        if not row:
            raise UserError('대상을 찾을 수 없습니다. 현재 서버의 항목을 선택해 주세요.')
        return dict(row)

    def snapshot(self, c, guild):
        return {t: [dict(r) for r in c.execute(f'SELECT * FROM {t} WHERE guild_id=?', (guild,))] for t in TABLES}

    def touch(self, c, guild, boss_ids=None):
        # Conservative guild revision fences every confirmation, including external state changes.
        c.execute('UPDATE guild_settings SET revision=revision+1 WHERE guild_id=?', (guild,))
        settings = c.execute('SELECT * FROM guild_settings WHERE guild_id=?', (guild,)).fetchone()
        c.execute('UPDATE bosses SET composition_version=composition_version+1 WHERE guild_id=?', (guild,))
        ids = [r[0] for r in c.execute('SELECT id FROM bosses WHERE guild_id=?', (guild,))]
        if boss_ids is not None:
            ids = [bid for bid in ids if bid in boss_ids]
        for generation in (settings['active_generation'], settings['preparing_generation']):
            if generation is None:
                continue
            for bid in ids:
                c.execute("""INSERT INTO board_sync_state(guild_id,generation,boss_id,requested) VALUES(?,?,?,?)
                    ON CONFLICT(guild_id,generation,boss_id) DO UPDATE SET requested=board_sync_state.requested+1,retry_at=0""",
                    (guild,generation,bid,settings['revision']))

    def execute(self, actor: Actor, action: str, payload: dict, confirmation: str | None = None):
        try:
            with self.db.transaction() as c:
                self.db.initialize(c, actor.guild_id)
                self.authorize(c, actor, action, payload)
                settings = dict(c.execute('SELECT * FROM guild_settings WHERE guild_id=?', (actor.guild_id,)).fetchone())
                request = None
                if confirmation:
                    request = c.execute('SELECT * FROM confirmations WHERE id=? AND guild_id=? AND actor_id=?',
                                        (confirmation, actor.guild_id, actor.user_id)).fetchone()
                    if not request or request['action'] != action or json.loads(request['payload']) != payload:
                        raise UserError('유효하지 않은 확인 요청입니다.')
                    if request['consumed']:
                        return {'status': 'done', 'message': request['result'], 'duplicate': True}
                    if request['expires_at'] < time.time():
                        raise UserError('확인 요청이 만료되었습니다. 다시 실행해 주세요.')
                    if request['revision'] != settings['revision']:
                        raise UserError('확인 후 데이터가 변경되었습니다. 명령을 다시 실행하여 최신 내용을 확인해 주세요.')
                before = self.snapshot(c, actor.guild_id)
                c.execute('SAVEPOINT preview')
                result = self.apply(c, actor, action, dict(payload))
                after = self.snapshot(c, actor.guild_id)
                changed = before != after or action.startswith(('settings.', 'board.'))
                if (action in CONFIRM or (action=='boss.edit' and 'sort_order' in payload)) and not confirmation and changed:
                    c.execute('ROLLBACK TO preview')
                    c.execute('RELEASE preview')
                    token = uuid.uuid4().hex
                    c.execute('INSERT INTO confirmations(id,guild_id,actor_id,action,payload,revision,expires_at) VALUES(?,?,?,?,?,?,?)',
                              (token, actor.guild_id, actor.user_id, action, json.dumps(payload), settings['revision'], time.time()+300))
                    return {'status': 'confirm', 'token': token, 'message': self.preview(action, payload, before, result)}
                c.execute('RELEASE preview')
                if changed:
                    self.touch(c, actor.guild_id, {payload['boss_id']} if action == 'board.refresh' and 'boss_id' in payload else None)
                    c.execute('INSERT INTO audit_logs(guild_id,actor_id,action,payload,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)',
                              (actor.guild_id, actor.user_id, action, json.dumps(payload, ensure_ascii=False),
                               json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), time.time()))
                message = result if changed else '변경 사항이 없습니다.'
                if request:
                    c.execute('UPDATE confirmations SET consumed=1,result=? WHERE id=?', (message, confirmation))
                return {'status': 'done', 'message': message}
        except sqlite3.IntegrityError as exc:
            raise UserError('이름 또는 배정이 중복되거나 연결 정보가 유효하지 않습니다. 기존 항목을 확인해 주세요.') from exc

    def preview(self, action, payload, before, result):
        summary = result + '\n\n확인하면 위 작업을 실행합니다. 아직 저장하지 않았습니다.'
        if action == 'boss.delete':
            bid = payload['id']
            groups = [r for r in before['boss_groups'] if r['boss_id'] == bid and r['status'] == 'active']
            members = [r for r in before['group_members'] if r['boss_id'] == bid]
            summary += f'\n영향: 파티 {len(groups)}개, 배정 {len(members)}개. 복구해도 배정은 돌아오지 않습니다.'
        if action in ('group.delete', 'group.merge'):
            gid = payload['id']
            ids = {r['character_id'] for r in before['group_members'] if r['group_id'] == gid}
            names = [r['name'] for r in before['characters'] if r['id'] in ids]
            summary += '\n원본 구성원: ' + (', '.join(names) or '없음')
        if action.startswith('job.'):
            count = sum(r['job_id'] == payload.get('id') for r in before['characters'])
            summary += f'\n연결 캐릭터: {count}개'
        return summary

    def present(self, c, guild, user):
        row = c.execute('SELECT membership FROM discord_users WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
        if not row or row[0] == 'unknown':
            raise UserError('서버 소속을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.')
        if row[0] != 'present':
            raise UserError('현재 서버에 소속된 사용자의 캐릭터만 배정·연결할 수 있습니다.')

    def active_boss(self, c, guild, boss_id):
        boss = self.get(c, guild, 'bosses', boss_id)
        if boss['status'] != 'active':
            raise UserError('활성 보스에서만 실행할 수 있습니다.')
        return boss

    def assign(self, c, guild, group_id, char_id):
        group = self.get(c, guild, 'boss_groups', group_id)
        if group['status'] != 'active':
            raise UserError('삭제된 파티입니다.')
        boss = self.active_boss(c, guild, group['boss_id'])
        char = self.get(c, guild, 'characters', char_id)
        if not char['active']:
            raise UserError('비활성 캐릭터는 배정할 수 없습니다.')
        self.present(c, guild, char['owner_id'])
        members = c.execute('''SELECT ch.* FROM group_members m JOIN characters ch ON ch.id=m.character_id
                               WHERE m.guild_id=? AND m.group_id=?''', (guild, group_id)).fetchall()
        if len(members) >= boss['max_members']:
            raise UserError('대상 파티의 정원이 가득 찼습니다.')
        if any(r['owner_id'] == char['owner_id'] for r in members):
            raise UserError('같은 소유자의 캐릭터를 한 파티에 배정할 수 없습니다.')
        c.execute('INSERT INTO group_members VALUES(?,?,?,?,?,?)',
                  (guild, boss['id'], group['cycle_id'], group_id, char_id, time.time()))
        c.execute('UPDATE boss_groups SET version=version+1 WHERE id=?', (group_id,))

    def normalize_boss_order(self,c,guild,ids=None):
        if ids is None:
            ids=[r[0] for r in c.execute("SELECT id FROM bosses WHERE guild_id=? AND status='active' ORDER BY sort_order,id",(guild,))]
        for position,bid in enumerate(ids,1):
            c.execute('UPDATE bosses SET sort_order=?,version=version+1 WHERE guild_id=? AND id=? AND sort_order<>?',
                      (position,guild,bid,position))

    def begin_publication(self,c,guild,channel,kind='publish'):
        settings=c.execute('SELECT * FROM guild_settings WHERE guild_id=?',(guild,)).fetchone()
        if settings['preparing_generation'] is not None:
            raise UserError('게시 준비·이전 작업이 진행 중입니다. 완료 후 다시 시도해 주세요.')
        generation=settings['generation_counter']+1
        c.execute('INSERT INTO board_generations VALUES(?,?,?,?,?,NULL)',(guild,generation,channel,'preparing',time.time()))
        c.execute('UPDATE guild_settings SET generation_counter=?,preparing_generation=? WHERE guild_id=?',(generation,generation,guild))
        c.execute("INSERT INTO board_jobs(guild_id,generation,kind,state,created_at) VALUES(?,?,?,'pending',?)",(guild,generation,kind,time.time()))
        return generation

    def reorder_boss(self,c,guild,boss_id,position):
        self.active_boss(c,guild,boss_id)
        rows=c.execute("SELECT id,name,difficulty FROM bosses WHERE guild_id=? AND status='active' ORDER BY sort_order,id",(guild,)).fetchall()
        try:
            text=str(position).strip()
            if isinstance(position,bool) or not text.isascii() or not text.isdecimal():raise ValueError
            value=int(text)
        except (TypeError,ValueError):
            raise UserError('보스 위치는 1부터 시작하는 정수로 입력해 주세요.')
        if not 1<=value<=len(rows):
            raise UserError(f'보스 위치는 1~{len(rows)} 사이여야 합니다.')
        original=[r['id'] for r in rows]
        desired=[bid for bid in original if bid!=boss_id]
        desired.insert(value-1,boss_id)
        if desired==original:
            return '보스 정보를 저장합니다. 현재 표시 순서는 유지합니다.'
        if desired!=original:
            settings=c.execute('SELECT * FROM guild_settings WHERE guild_id=?',(guild,)).fetchone()
            if settings['preparing_generation'] is not None:
                raise UserError('게시 준비·이전 작업이 진행 중입니다. 완료 후 순서를 변경해 주세요.')
            if settings['active_generation'] is not None:
                channel=c.execute('SELECT channel_id FROM board_generations WHERE guild_id=? AND generation=?',(guild,settings['active_generation'])).fetchone()[0]
                if not c.execute('SELECT 1 FROM board_forums WHERE channel_id=?',(channel,)).fetchone():
                    self.begin_publication(c,guild,channel,'reorder')
        self.normalize_boss_order(c,guild,desired)
        names={r['id']:f'{r["name"]} ({r["difficulty"]})' for r in rows}
        listing='\n'.join(f'{index}. {names[bid]}' for index,bid in enumerate(desired,1))
        return ('보스 표시 순서를 변경합니다.\n'+listing[:1400]+
                '\n텍스트 채널은 새 순서로 재게시되어 기존 메시지 링크가 바뀝니다. 포럼 목록은 Discord 정렬을 따르며 게시글을 유지합니다.')

    def apply(self, c, a, action, p):
        g = a.guild_id
        kind, op = action.split('.')
        if kind == 'settings':
            col = {'role': 'manager_role_id', 'channel': 'default_channel_id'}[op]
            c.execute(f'UPDATE guild_settings SET {col}=? WHERE guild_id=?', (p['value'], g))
            return '서버 설정을 업데이트했습니다.'
        if kind == 'job':
            if op == 'add':
                name = label(p['name'])
                c.execute('INSERT INTO jobs(guild_id,name,name_key) VALUES(?,?,?)', (g, name, key(name)))
                return f'직업 {name}을 추가합니다.'
            job = self.get(c, g, 'jobs', p['id'])
            if op == 'rename':
                name = label(p['name'])
                c.execute('UPDATE jobs SET name=?,name_key=?,version=version+1 WHERE id=? AND name<>?', (name, key(name), job['id'], name))
            elif op in ('activate', 'deactivate'):
                active = int(op == 'activate')
                c.execute('UPDATE jobs SET active=?,version=version+1 WHERE id=? AND active<>?', (active, job['id'], active))
            else:
                raise UserError('지원하지 않는 직업 작업입니다.')
            return f'직업 {job["name"]} 정보를 변경합니다. 기존 캐릭터·배정은 유지합니다.'
        if kind == 'character':
            if op == 'add':
                name = character_name(p['name'])
                owner = p.get('owner_id', a.user_id)
                self.present(c, g, owner)
                job = self.get(c, g, 'jobs', p['job_id'])
                if not job['active']:
                    raise UserError('활성 직업을 선택해 주세요.')
                power = hexa(p['hexa']) if p.get('hexa') is not None else None
                now = time.time()
                c.execute('''INSERT INTO characters(guild_id,owner_id,registered_by,name,name_key,job_id,hexa_value,created_at,updated_at)
                             VALUES(?,?,?,?,?,?,?,?,?)''', (g, owner, a.user_id, name, key(name), job['id'], power, now, now))
                c.execute('UPDATE jobs SET links_version=links_version+1 WHERE id=?', (job['id'],))
                return f'{name} · {job["name"]} · 소유자 <@{owner}> 등록'
            ch = self.get(c, g, 'characters', p['id'])
            if op == 'edit':
                changes = {}
                if 'name' in p:
                    changes['name'] = character_name(p['name'])
                    changes['name_key'] = key(changes['name'])
                if 'job_id' in p:
                    job = self.get(c, g, 'jobs', p['job_id'])
                    if not job['active']:
                        raise UserError('활성 직업을 선택해 주세요.')
                    changes['job_id'] = job['id']
                if p.get('reset_hexa') and p.get('hexa') is not None:
                    raise UserError('헥환 입력과 초기화는 동시에 요청할 수 없습니다.')
                if p.get('reset_hexa'):
                    changes['hexa_value'] = None
                elif p.get('hexa') is not None:
                    changes['hexa_value'] = hexa(p['hexa'])
                if not changes:
                    raise UserError('수정할 항목을 지정해 주세요.')
            elif op == 'owner':
                self.present(c, g, p['owner_id'])
                clashes = c.execute('''SELECT 1 FROM group_members m JOIN group_members other ON other.group_id=m.group_id
                    JOIN characters ch ON ch.id=other.character_id WHERE m.guild_id=? AND m.character_id=?
                    AND ch.owner_id=? AND ch.id<>?''', (g, ch['id'], p['owner_id'], ch['id'])).fetchone()
                if clashes:
                    raise UserError('소유자 변경으로 같은 파티의 소유자 중복이 발생합니다.')
                changes = {'owner_id': p['owner_id']}
            elif op in ('activate', 'deactivate'):
                if op == 'activate':
                    self.present(c, g, ch['owner_id'])
                elif c.execute('SELECT 1 FROM group_members WHERE guild_id=? AND character_id=?', (g, ch['id'])).fetchone():
                    raise UserError('현재 배정이 남아 있습니다. 관리자가 먼저 배정을 제외해야 합니다.')
                changes = {'active': int(op == 'activate')}
            else:
                raise UserError('지원하지 않는 캐릭터 작업입니다.')
            changes = {k: v for k, v in changes.items() if ch[k] != v}
            if changes:
                c.execute('UPDATE characters SET '+','.join(f'{k}=?' for k in changes)+',version=version+1,updated_at=? WHERE id=?',
                          (*changes.values(), time.time(), ch['id']))
                if 'job_id' in changes:
                    c.execute('UPDATE jobs SET links_version=links_version+1 WHERE id IN (?,?)', (ch['job_id'], changes['job_id']))
                c.execute('UPDATE boss_groups SET version=version+1 WHERE id IN (SELECT group_id FROM group_members WHERE character_id=?)', (ch['id'],))
            return f'{ch["name"]} 정보를 변경합니다.'
        if kind == 'boss':
            if op == 'add':
                name, diff = label(p['name']), label(p['difficulty'], 20)
                maximum = int(p.get('max_members', 6))
                if not 1 <= maximum <= 6:
                    raise UserError('정원은 1~6명입니다.')
                c.execute('INSERT INTO bosses(guild_id,name,name_key,difficulty,difficulty_key,max_members) VALUES(?,?,?,?,?,?)',
                          (g, name, key(name), diff, key(diff), maximum))
                new_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                rank=c.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM bosses WHERE guild_id=? AND status='active' AND id<>?",(g,new_id)).fetchone()[0]
                c.execute('UPDATE bosses SET sort_order=? WHERE id=?',(rank,new_id))
                self.normalize_boss_order(c,g)
                return f'{name} ({diff}) 보스를 파티 0개로 등록했습니다.'
            boss = self.get(c, g, 'bosses', p['id'])
            if op == 'order':
                return self.reorder_boss(c,g,boss['id'],p['position'])
            if op == 'edit':
                self.active_boss(c, g, boss['id'])
                changes = {}
                for field, limit in [('name', 50), ('difficulty', 20), ('display_name', 50)]:
                    if field in p:
                        changes[field] = label(p[field], limit)
                        if field != 'display_name':
                            changes[field+'_key'] = key(changes[field])
                for field in ('max_members',):
                    if field in p:
                        changes[field] = int(p[field])
                maximum = changes.get('max_members', boss['max_members'])
                if not 1 <= maximum <= 6:
                    raise UserError('정원은 1~6명입니다.')
                full = c.execute('''SELECT gr.name,COUNT(m.character_id) AS n FROM boss_groups gr
                    JOIN group_members m ON m.group_id=gr.id WHERE gr.guild_id=? AND gr.boss_id=? AND gr.status='active'
                    GROUP BY gr.id HAVING COUNT(m.character_id)>?''', (g, boss['id'], maximum)).fetchall()
                if full:
                    raise UserError('정원 변경 불가: '+', '.join(f'{r["name"]} {r["n"]}명' for r in full)+' — 먼저 수동 정리해 주세요.')
                if not changes and 'sort_order' not in p:
                    raise UserError('수정할 항목을 지정해 주세요.')
                changes = {k: v for k, v in changes.items() if boss[k] != v}
                if changes:
                    c.execute('UPDATE bosses SET '+','.join(f'{k}=?' for k in changes)+',version=version+1 WHERE id=?', (*changes.values(), boss['id']))
                if 'sort_order' in p:
                    return self.reorder_boss(c,g,boss['id'],p['sort_order'])
            elif op in ('activate', 'deactivate', 'delete', 'restore'):
                if op == 'restore' and boss['status'] != 'deleted':
                    raise UserError('삭제된 보스만 복구할 수 있습니다.')
                if op != 'restore' and boss['status'] == 'deleted':
                    raise UserError('삭제된 보스는 보스 복구를 사용해 주세요.')
                status = {'activate': 'active', 'deactivate': 'inactive', 'delete': 'deleted', 'restore': 'inactive'}[op]
                if status != boss['status']:
                    if status=='active':
                        rank=c.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM bosses WHERE guild_id=? AND status='active'",(g,)).fetchone()[0]
                        c.execute('UPDATE bosses SET sort_order=? WHERE id=?',(rank,boss['id']))
                    c.execute('UPDATE bosses SET status=?,version=version+1 WHERE id=?', (status, boss['id']))
                    self.normalize_boss_order(c,g)
                    if op == 'delete':
                        c.execute('DELETE FROM group_members WHERE guild_id=? AND boss_id=?', (g, boss['id']))
                        c.execute("UPDATE boss_groups SET status='deleted',version=version+1 WHERE guild_id=? AND boss_id=? AND status='active'", (g, boss['id']))
            else:
                raise UserError('지원하지 않는 보스 작업입니다.')
            return f'{boss["name"]} 보스 정보를 변경합니다. 복구 시 이전 파티·배정은 돌아오지 않습니다.'
        if kind == 'group':
            if op == 'add':
                boss = self.active_boss(c, g, p['boss_id'])
                cycle = c.execute('SELECT id FROM cycles WHERE guild_id=?', (g,)).fetchone()[0]
                count = c.execute('SELECT COUNT(*) FROM boss_groups WHERE guild_id=? AND boss_id=?', (g, boss['id'])).fetchone()[0]
                name = label(p['name']) if p.get('name') is not None else f'파티 {count+1}'
                c.execute('INSERT INTO boss_groups(guild_id,boss_id,cycle_id,name,sort_order) VALUES(?,?,?,?,?)', (g, boss['id'], cycle, name, count))
                return f'{boss["name"]} / {name} 빈 파티 생성'
            group = self.get(c, g, 'boss_groups', p['id'])
            if group['status'] != 'active':
                raise UserError('삭제된 파티입니다.')
            boss = self.get(c, g, 'bosses', group['boss_id'])
            if op != 'remove' or boss['status'] != 'inactive':
                self.active_boss(c, g, boss['id'])
            if op == 'rename':
                name = label(p['name'])
                c.execute('UPDATE boss_groups SET name=?,version=version+1 WHERE id=? AND name<>?', (name, group['id'], name))
            elif op == 'delete':
                c.execute('DELETE FROM group_members WHERE guild_id=? AND group_id=?', (g, group['id']))
                c.execute("UPDATE boss_groups SET status='deleted',version=version+1 WHERE id=?", (group['id'],))
            elif op == 'member':
                self.assign(c, g, group['id'], p['character_id'])
            elif op == 'remove':
                self.get(c, g, 'characters', p['character_id'])
                cur = c.execute('DELETE FROM group_members WHERE guild_id=? AND group_id=? AND character_id=?', (g, group['id'], p['character_id']))
                if cur.rowcount:
                    c.execute('UPDATE boss_groups SET version=version+1 WHERE id=?', (group['id'],))
            elif op in ('move', 'merge'):
                target = self.get(c, g, 'boss_groups', p['target_id'])
                if target['id'] == group['id']:
                    if op == 'move':
                        return '변경 사항이 없습니다.'
                    raise UserError('서로 다른 파티를 선택해 주세요.')
                if target['status'] != 'active':
                    raise UserError('삭제된 대상 파티입니다.')
                if target['boss_id'] != group['boss_id'] or target['cycle_id'] != group['cycle_id']:
                    raise UserError('같은 보스·회차의 파티를 선택해 주세요.')
                members = [r[0] for r in c.execute('SELECT character_id FROM group_members WHERE group_id=?', (group['id'],))]
                if op == 'move':
                    if p['character_id'] not in members:
                        raise UserError('선택 캐릭터가 원본 파티에 없습니다.')
                    members = [p['character_id']]
                for char_id in members:
                    c.execute('DELETE FROM group_members WHERE group_id=? AND character_id=?', (group['id'], char_id))
                    self.assign(c, g, target['id'], char_id)
                c.execute('UPDATE boss_groups SET version=version+1 WHERE id=?', (group['id'],))
                if op == 'merge':
                    c.execute("UPDATE boss_groups SET status='deleted' WHERE id=?", (group['id'],))
            else:
                raise UserError('지원하지 않는 파티 작업입니다.')
            return f'{boss["name"]} / {group["name"]} 파티 작업 완료'
        if kind == 'board':
            settings = dict(c.execute('SELECT * FROM guild_settings WHERE guild_id=?', (g,)).fetchone())
            if op == 'publish':
                channel = p.get('channel_id') or settings['last_channel_id'] or settings['default_channel_id']
                if not channel:
                    raise UserError('서버 관리자가 먼저 게시 채널을 설정해야 합니다.')
                if settings['preparing_generation'] is not None:
                    raise UserError('이미 게시 준비 중입니다. 설정 조회에서 상태를 확인해 주세요.')
                active = settings['active_generation']
                if active is not None:
                    current = c.execute('SELECT channel_id FROM board_generations WHERE guild_id=? AND generation=?', (g, active)).fetchone()[0]
                    if current == channel:
                        return '현재 파티 배분표 갱신을 요청했습니다.'
                self.begin_publication(c,g,channel)
                return f'<#{channel}>에 파티 배분표 게시·이전을 준비합니다.'
            if op == 'stop':
                for generation in (settings['active_generation'], settings['preparing_generation']):
                    if generation is not None:
                        c.execute("UPDATE board_generations SET status='retired' WHERE guild_id=? AND generation=?", (g, generation))
                        c.execute("UPDATE board_jobs SET state='cancelled' WHERE guild_id=? AND generation=? AND state='pending'", (g, generation))
                c.execute('UPDATE guild_settings SET active_generation=NULL,preparing_generation=NULL WHERE guild_id=?', (g,))
                return '게시를 중단합니다. 기존 게시물은 정리하며 파티 데이터는 유지합니다.'
            if op == 'refresh':
                if 'boss_id' in p:
                    self.get(c, g, 'bosses', p['boss_id'])
                if settings['active_generation'] is None:
                    raise UserError('게시 중이 아닙니다. /파티배분표 게시를 사용해 주세요.')
                return '최신 파티 배분표 갱신을 요청했습니다.'
        raise UserError('지원하지 않는 작업입니다.')

    def rows(self, guild, table, *, actor=None, state='active', owner_id=None, boss_id=None):
        with self.db.transaction(False) as c:
            self.db.initialize(c, guild)
            manager = actor is not None and self.manager(c, actor)
            result = self.snapshot(c, guild)[table] if table in TABLES else []
            if table == 'characters':
                result = [r for r in result if owner_id is None or r['owner_id'] == owner_id]
                if state != 'all':
                    result = [r for r in result if r['active'] == int(state == 'active')]
            elif table == 'jobs':
                if state != 'active' and not manager:
                    raise UserError('비활성 목록은 관리자만 조회할 수 있습니다.')
                if state != 'all':
                    result = [r for r in result if r['active'] == int(state == 'active')]
            elif table == 'bosses':
                if state != 'active' and not manager:
                    raise UserError('비활성·삭제 보스는 관리자만 조회할 수 있습니다.')
                if state != 'all':
                    result = [r for r in result if r['status'] == state]
            elif table == 'boss_groups':
                result = [r for r in result if r['status'] == 'active' and (boss_id is None or r['boss_id'] == boss_id)]
                bosses = {r['id']: r for r in self.snapshot(c, guild)['bosses']}
                result = [r for r in result if manager or bosses[r['boss_id']]['status'] == 'active']
            if table in ('bosses','boss_groups'):
                result.sort(key=lambda row:(row['sort_order'],row['id']))
            return result

    def settings(self, actor):
        with self.db.transaction() as c:
            self.db.initialize(c, actor.guild_id)
            if not self.manager(c, actor):
                raise UserError('관리자만 설정을 조회할 수 있습니다.')
            result = dict(c.execute('SELECT * FROM guild_settings WHERE guild_id=?', (actor.guild_id,)).fetchone())
            result['generations'] = [dict(r) for r in c.execute('SELECT * FROM board_generations WHERE guild_id=? ORDER BY generation DESC LIMIT 5', (actor.guild_id,))]
            result['errors'] = [dict(r) for r in c.execute('SELECT boss_id,last_error FROM board_sync_state WHERE guild_id=? AND last_error IS NOT NULL LIMIT 10', (actor.guild_id,))]
            result['cleanup_errors'] = [dict(r) for r in c.execute('SELECT channel_id,message_id,last_error FROM board_cleanup_messages WHERE guild_id=? AND last_error IS NOT NULL LIMIT 10', (actor.guild_id,))]
            return result

    def member_ids(self, guild):
        with self.db.transaction(False) as c:
            return [r[0] for r in c.execute('SELECT DISTINCT owner_id FROM characters WHERE guild_id=?', (guild,))]
