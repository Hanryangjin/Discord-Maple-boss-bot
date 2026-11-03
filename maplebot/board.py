"""Restartable board reconciler with generation fencing and per-guild ordering."""
import asyncio
import logging
import time
from .render import boss_pages

log = logging.getLogger(__name__)


class BoardWorker:
    def __init__(self, service, transport):
        self.service = service
        self.db = service.db
        self.transport = transport
        self.locks = {}
        self.adopted = set()

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('board worker tick failed')
            await asyncio.sleep(2)

    def guilds(self):
        with self.db.transaction(False) as c:
            return [r[0] for r in c.execute('SELECT guild_id FROM guild_settings')]

    async def tick(self):
        for guild in await asyncio.to_thread(self.guilds):
            async with self.locks.setdefault(guild, asyncio.Lock()):
                await self.guild_tick(guild)

    def targets(self, guild):
        with self.db.transaction(False) as c:
            return [dict(r) for r in c.execute("SELECT * FROM board_generations WHERE guild_id=? AND status IN('active','preparing') ORDER BY generation", (guild,))]

    def snapshot(self, guild, generation, boss):
        with self.db.transaction(False) as c:
            gen = c.execute('SELECT * FROM board_generations WHERE guild_id=? AND generation=?', (guild, generation)).fetchone()
            state = c.execute('SELECT * FROM board_sync_state WHERE guild_id=? AND generation=? AND boss_id=?', (guild, generation, boss)).fetchone()
            if not gen or gen['status'] == 'retired' or not state or state['retry_at'] > time.time():
                return None
            if state['completed'] >= state['requested']:
                return None
            info = c.execute('SELECT status FROM bosses WHERE guild_id=? AND id=?', (guild, boss)).fetchone()
            # Never generate a fresh message for a boss that was already inactive before publication.
            known = c.execute('SELECT 1 FROM board_messages WHERE guild_id=? AND generation=? AND boss_id=?', (guild, generation, boss)).fetchone()
            content = boss_pages(c, guild, boss) if info['status'] == 'active' or known else []
            return dict(gen), dict(state), content

    def valid(self, guild, generation):
        with self.db.transaction(False) as c:
            row = c.execute("SELECT status FROM board_generations WHERE guild_id=? AND generation=?", (guild, generation)).fetchone()
            return bool(row and row[0] in ('preparing', 'active'))

    def message_rows(self, guild, generation, boss=None):
        with self.db.transaction(False) as c:
            sql = 'SELECT * FROM board_messages WHERE guild_id=? AND generation=?'
            args = [guild, generation]
            if boss is not None:
                sql += ' AND boss_id=?'
                args.append(boss)
            return [dict(r) for r in c.execute(sql, args)]

    def save_message(self, guild, gen, boss, page, channel, mid, version):
        with self.db.transaction() as c:
            valid = c.execute("SELECT 1 FROM board_generations WHERE guild_id=? AND generation=? AND status<>'retired'", (guild, gen)).fetchone()
            if not valid:
                c.execute('INSERT OR IGNORE INTO board_cleanup_messages(channel_id,message_id,guild_id,generation) VALUES(?,?,?,?)', (channel, mid, guild, gen))
                return False
            c.execute('''INSERT INTO board_messages VALUES(?,?,?,?,?,?,?) ON CONFLICT(guild_id,generation,boss_id,page)
                         DO UPDATE SET channel_id=excluded.channel_id,message_id=excluded.message_id,render_version=excluded.render_version''',
                      (guild, gen, boss, page, channel, mid, version))
            return True

    def remove_message(self, channel, mid):
        with self.db.transaction() as c:
            c.execute('DELETE FROM board_messages WHERE channel_id=? AND message_id=?', (channel, mid))
            c.execute('DELETE FROM board_cleanup_messages WHERE channel_id=? AND message_id=?', (channel, mid))

    def failure(self, guild, generation, boss, exc):
        # Persist error classes only: API exception strings can contain request details.
        message = type(exc).__name__
        with self.db.transaction() as c:
            state = c.execute('SELECT retries FROM board_sync_state WHERE guild_id=? AND generation=? AND boss_id=?', (guild, generation, boss)).fetchone()
            retries = state[0] + 1 if state else 1
            c.execute('UPDATE board_sync_state SET retries=?,retry_at=?,last_error=? WHERE guild_id=? AND generation=? AND boss_id=?',
                      (retries, time.time()+min(300, 2**min(retries,8)), message, guild, generation, boss))
            gen = c.execute('SELECT status FROM board_generations WHERE guild_id=? AND generation=?', (guild, generation)).fetchone()
            c.execute('UPDATE board_generations SET last_error=? WHERE guild_id=? AND generation=?', (message, guild, generation))
            # Definitive permission/channel failure aborts preparation; transient failures resume.
            if gen and gen[0] == 'preparing' and isinstance(exc, (PermissionError, LookupError)):
                c.execute("UPDATE board_generations SET status='retired' WHERE guild_id=? AND generation=?", (guild, generation))
                c.execute('UPDATE board_sync_state SET retry_at=? WHERE guild_id=? AND generation=?',(time.time()+60,guild,generation))
                c.execute('UPDATE guild_settings SET preparing_generation=NULL WHERE guild_id=? AND preparing_generation=?', (guild, generation))
                c.execute("UPDATE board_jobs SET state='failed',last_error=? WHERE guild_id=? AND generation=?", (message, guild, generation))

    async def reconcile(self, guild, generation, boss):
        snap = await asyncio.to_thread(self.snapshot, guild, generation, boss)
        if not snap:
            return
        gen, state, content = snap
        known = {r['page']: r for r in await asyncio.to_thread(self.message_rows, guild, generation, boss)}
        try:
            for page, body in enumerate(content):
                if not await asyncio.to_thread(self.valid, guild, generation):
                    return
                marker = f'maple:{guild}:{generation}:{boss}:{page}'
                old = known.get(page)
                mid = old['message_id'] if old else None
                # On restart/uncertain send, adopt only messages from our bot with an exact marker.
                if mid is None:
                    mid = await self.transport.find(gen['channel_id'], marker)
                if mid is not None:
                    exists = await self.transport.edit(gen['channel_id'], mid, body, marker)
                    if not exists:
                        mid = None
                if mid is None:
                    mid = await self.transport.send(gen['channel_id'], body, marker)
                if not await asyncio.to_thread(self.save_message, guild, generation, boss, page, gen['channel_id'], mid, state['requested']):
                    return
            for page, old in known.items():
                if page >= len(content):
                    await self.transport.delete(old['channel_id'], old['message_id'])
                    await asyncio.to_thread(self.remove_message, old['channel_id'], old['message_id'])
            with self.db.transaction() as c:
                # Fenced: never claim a newer version than the snapshot actually rendered.
                c.execute('UPDATE board_sync_state SET completed=MAX(completed,?),retries=0,retry_at=0,last_error=NULL WHERE guild_id=? AND generation=? AND boss_id=?',
                          (state['requested'], guild, generation, boss))
        except Exception as exc:
            await asyncio.to_thread(self.failure, guild, generation, boss, exc)
            log.warning('board sync failed guild=%s generation=%s boss=%s error=%s', guild, generation, boss, type(exc).__name__)

    @staticmethod
    def physically_ordered(c,guild,generation):
        # Forum lists are sorted by Discord activity, not message snowflakes.
        forum=c.execute('SELECT 1 FROM board_forums f JOIN board_generations g ON g.channel_id=f.channel_id WHERE g.guild_id=? AND g.generation=?',(guild,generation)).fetchone()
        if forum:return True
        ids=[row[0] for row in c.execute("""SELECT m.message_id FROM board_messages m
            JOIN bosses b ON b.guild_id=m.guild_id AND b.id=m.boss_id
            WHERE m.guild_id=? AND m.generation=? AND b.status='active'
            ORDER BY b.sort_order,b.id,m.page""",(guild,generation))]
        return ids==sorted(ids)

    def ensure_order(self,guild):
        # Discord cannot move existing messages. If a deleted/page-grown board lands
        # below another boss, rebuild a generation rather than claiming it was moved.
        with self.db.transaction() as c:
            settings=c.execute('SELECT * FROM guild_settings WHERE guild_id=?',(guild,)).fetchone()
            for generation in (settings['preparing_generation'],settings['active_generation']):
                if generation is None or self.physically_ordered(c,guild,generation):
                    continue
                gen=c.execute('SELECT * FROM board_generations WHERE guild_id=? AND generation=?',(guild,generation)).fetchone()
                if gen['status']=='active' and settings['preparing_generation'] is not None:
                    continue
                cooldown=c.execute("""SELECT 1 FROM board_generations old JOIN board_sync_state state
                    ON state.guild_id=old.guild_id AND state.generation=old.generation
                    WHERE old.guild_id=? AND old.channel_id=? AND old.status='retired'
                    AND old.last_error IS NOT NULL AND state.retry_at>? LIMIT 1""",(guild,gen['channel_id'],time.time())).fetchone()
                if cooldown:
                    continue
                if gen['status']=='preparing':
                    c.execute("UPDATE board_generations SET status='retired' WHERE guild_id=? AND generation=?",(guild,generation))
                    c.execute("UPDATE board_jobs SET state='cancelled' WHERE guild_id=? AND generation=? AND state='pending'",(guild,generation))
                    c.execute('UPDATE guild_settings SET preparing_generation=NULL WHERE guild_id=?',(guild,))
                self.service.begin_publication(c,guild,gen['channel_id'],'order-repair')
                self.service.touch(c,guild)
                return

    def promote(self, guild):
        with self.db.transaction() as c:
            settings = c.execute('SELECT * FROM guild_settings WHERE guild_id=?', (guild,)).fetchone()
            gen = settings['preparing_generation']
            if gen is None:
                return
            pending = c.execute('SELECT 1 FROM board_sync_state WHERE guild_id=? AND generation=? AND completed<requested', (guild, gen)).fetchone()
            missing = c.execute("SELECT 1 FROM bosses b WHERE guild_id=? AND status='active' AND NOT EXISTS(SELECT 1 FROM board_sync_state s WHERE s.guild_id=b.guild_id AND s.generation=? AND s.boss_id=b.id)", (guild, gen)).fetchone()
            if pending or missing or not self.physically_ordered(c,guild,gen):
                return
            target = c.execute('SELECT * FROM board_generations WHERE guild_id=? AND generation=?', (guild, gen)).fetchone()
            if target['status'] != 'preparing':
                return
            if settings['active_generation'] is not None:
                c.execute("UPDATE board_generations SET status='retired' WHERE guild_id=? AND generation=?", (guild, settings['active_generation']))
            c.execute("UPDATE board_generations SET status='active',last_error=NULL WHERE guild_id=? AND generation=?", (guild, gen))
            c.execute('UPDATE guild_settings SET active_generation=?,preparing_generation=NULL,last_channel_id=?,revision=revision+1 WHERE guild_id=?', (gen, target['channel_id'], guild))
            c.execute("UPDATE board_jobs SET state='done' WHERE guild_id=? AND generation=?", (guild, gen))

    def cleanup_rows(self, guild):
        with self.db.transaction() as c:
            c.execute('''INSERT OR IGNORE INTO board_cleanup_messages(channel_id,message_id,guild_id,generation)
                SELECT m.channel_id,m.message_id,m.guild_id,m.generation FROM board_messages m JOIN board_generations g
                ON g.guild_id=m.guild_id AND g.generation=m.generation WHERE m.guild_id=? AND g.status='retired' ''', (guild,))
            return [dict(r) for r in c.execute('SELECT * FROM board_cleanup_messages WHERE guild_id=? AND retry_at<=?', (guild, time.time()))]

    async def cleanup(self, guild):
        for row in await asyncio.to_thread(self.cleanup_rows, guild):
            try:
                await self.transport.delete(row['channel_id'], row['message_id'])
                await asyncio.to_thread(self.remove_message, row['channel_id'], row['message_id'])
            except Exception as exc:
                try:
                    await self.transport.edit(row['channel_id'], row['message_id'], {'title': '운영 종료', 'description': '이 파티 배분표는 중단되거나 다른 채널로 이전되었습니다.'}, 'maple:retired')
                except Exception:
                    pass
                with self.db.transaction() as c:
                    c.execute('UPDATE board_cleanup_messages SET last_error=?,retries=retries+1,retry_at=? WHERE channel_id=? AND message_id=?',
                              (type(exc).__name__, time.time()+60, row['channel_id'], row['message_id']))

    async def recover_orphans(self, guild):
        with self.db.transaction(False) as c:
            generations = [dict(r) for r in c.execute('SELECT * FROM board_generations WHERE guild_id=?',(guild,))]
        for gen in generations:
            stamp=(guild,gen['generation'],gen['status'])
            if stamp in self.adopted:
                continue
            try:
                found = await self.transport.scan(gen['channel_id'],guild)
            except (PermissionError, LookupError):
                continue
            with self.db.transaction() as c:
                for generation,boss,page,mid in found:
                    if generation!=gen['generation']:
                        continue
                    known=c.execute('SELECT message_id FROM board_messages WHERE guild_id=? AND generation=? AND boss_id=? AND page=?',
                                    (guild,generation,boss,page)).fetchone()
                    current=c.execute('SELECT status FROM board_generations WHERE guild_id=? AND generation=?',(guild,generation)).fetchone()
                    boss_exists=c.execute('SELECT 1 FROM bosses WHERE guild_id=? AND id=?',(guild,boss)).fetchone()
                    if current['status']=='retired' or not boss_exists or (known and known[0]!=mid):
                        c.execute('INSERT OR IGNORE INTO board_cleanup_messages(channel_id,message_id,guild_id,generation) VALUES(?,?,?,?)',
                                  (gen['channel_id'],mid,guild,generation))
                    elif not known:
                        c.execute('INSERT INTO board_messages VALUES(?,?,?,?,?,?,-1)',(guild,generation,boss,page,gen['channel_id'],mid))
                        c.execute('''INSERT INTO board_sync_state(guild_id,generation,boss_id,requested) VALUES(?,?,?,1)
                                     ON CONFLICT(guild_id,generation,boss_id) DO UPDATE SET requested=requested+1,retry_at=0''',
                                  (guild,generation,boss))
            self.adopted.add(stamp)

    async def guild_tick(self, guild):
        await self.recover_orphans(guild)
        for gen in await asyncio.to_thread(self.targets, guild):
            with self.db.transaction(False) as c:
                ids = [r[0] for r in c.execute('SELECT s.boss_id FROM board_sync_state s JOIN bosses b ON b.guild_id=s.guild_id AND b.id=s.boss_id WHERE s.guild_id=? AND s.generation=? AND s.requested>s.completed ORDER BY b.sort_order,b.id', (guild, gen['generation']))]
            for boss in ids:
                await self.reconcile(guild, gen['generation'], boss)
        await asyncio.to_thread(self.ensure_order, guild)
        await asyncio.to_thread(self.promote, guild)
        await self.cleanup(guild)
