import asyncio
from pathlib import Path
import tempfile
import unittest
from maplebot.db import Database
from maplebot.service import Actor, Service
from maplebot.board import BoardWorker


class FakeDiscord:
    def __init__(self):
        self.messages={}
        self.next_id=1
        self.fail_send=None
        self.fail_delete=False
        self.hook=None
        self.hook_channel=None

    async def send(self,channel,body,marker):
        if self.fail_send:raise self.fail_send
        mid=self.next_id;self.next_id+=1
        self.messages[(channel,mid)]={'body':dict(body),'marker':marker}
        if self.hook and (self.hook_channel is None or self.hook_channel==channel):
            hook,self.hook=self.hook,None
            await hook()
        return mid

    async def edit(self,channel,mid,body,marker):
        if (channel,mid) not in self.messages:return False
        self.messages[(channel,mid)]={'body':dict(body),'marker':marker}
        if self.hook and (self.hook_channel is None or self.hook_channel==channel):
            hook,self.hook=self.hook,None
            await hook()
        return True

    async def delete(self,channel,mid):
        if self.fail_delete:raise PermissionError('delete')
        self.messages.pop((channel,mid),None)

    async def scan(self,channel,guild):
        result=[]
        for (ch,mid),v in self.messages.items():
            p=v['marker'].split(':')
            if ch==channel and len(p)==5 and p[0]=='maple' and int(p[1])==guild:
                result.append((int(p[2]),int(p[3]),int(p[4]),mid))
        return result

    async def find(self,channel,marker):
        return next((mid for (ch,mid),v in self.messages.items() if ch==channel and v['marker']==marker),None)


class BoardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(str(Path(self.tmp.name)/'db.sqlite3'));self.s=Service(self.db);self.a=Actor(1,10,True)
        self.s.observe_member(1,10,'user','present')
        self.do('settings.channel',{'value':100})
        self.do('boss.add',{'name':'보스A','difficulty':'하드'})
        self.bid=self.s.rows(1,'bosses',actor=self.a)[0]['id']
        self.transport=FakeDiscord();self.worker=BoardWorker(self.s,self.transport)

    def do(self,action,p):
        r=self.s.execute(self.a,action,p)
        return self.s.execute(self.a,action,p,r['token']) if r['status']=='confirm' else r

    def row(self,sql,args=()):
        with self.db.transaction(False) as c:return [dict(r) for r in c.execute(sql,args)]

    def retries_now(self):
        with self.db.transaction() as c:
            c.execute('UPDATE board_sync_state SET retry_at=0')
            c.execute('UPDATE board_cleanup_messages SET retry_at=0')

    async def publish(self,channel=None):
        self.do('board.publish',{'channel_id':channel} if channel else {})
        await self.worker.tick()

    async def test_publish_update_deleted_message_and_stop(self):
        await self.publish()
        self.assertEqual(len(self.transport.messages),1)
        self.assertEqual(self.s.settings(self.a)['active_generation'],1)
        self.do('group.add',{'boss_id':self.bid,'name':'파티신규'})
        await self.worker.tick()
        self.assertIn('파티신규',str(self.transport.messages))
        self.transport.messages.clear()
        self.do('board.refresh',{})
        await self.worker.tick()
        self.assertEqual(len(self.transport.messages),1)
        self.do('board.stop',{})
        await self.worker.tick()
        self.assertFalse(self.transport.messages)
        self.assertIsNone(self.s.settings(self.a)['active_generation'])

    async def test_retry_and_restart(self):
        self.transport.fail_send=RuntimeError('temporary')
        await self.publish()
        self.assertIsNone(self.s.settings(self.a)['active_generation'])
        self.assertIsNotNone(self.s.settings(self.a)['preparing_generation'])
        self.transport.fail_send=None;self.retries_now()
        self.worker=BoardWorker(Service(Database(self.db.path)),self.transport)
        await self.worker.tick()
        self.assertEqual(self.s.settings(self.a)['active_generation'],1)
        state=self.row('SELECT * FROM board_sync_state')[0]
        self.assertEqual(state['completed'],state['requested'])

    async def test_permission_failure_preserves_old_channel(self):
        await self.publish()
        self.transport.fail_send=PermissionError('forbidden')
        await self.publish(200)
        self.assertEqual(self.s.settings(self.a)['active_generation'],1)
        self.assertIsNone(self.s.settings(self.a)['preparing_generation'])
        self.assertTrue(all(ch==100 for ch,mid in self.transport.messages))

    async def test_move_channel_and_late_old_cleanup(self):
        await self.publish()
        self.transport.fail_delete=True
        await self.publish(200)
        self.assertEqual(self.s.settings(self.a)['last_channel_id'],200)
        self.assertEqual(len(self.transport.messages),2)
        self.assertIn('운영 종료',str(self.transport.messages[(100,1)]))
        self.do('board.stop',{})
        await self.worker.tick()
        await self.publish(200)
        self.transport.fail_delete=False;self.retries_now()
        await self.worker.tick()
        current=self.s.settings(self.a)['active_generation']
        self.assertTrue(self.transport.messages)
        self.assertTrue(all(f':{current}:' in value['marker'] for value in self.transport.messages.values()))

    async def test_stop_during_send_does_not_resurrect(self):
        async def stop():self.do('board.stop',{})
        self.transport.hook=stop
        await self.publish()
        await self.worker.tick()
        self.assertFalse(self.transport.messages)
        self.assertIsNone(self.s.settings(self.a)['active_generation'])

    async def test_change_during_render_keeps_new_revision_pending(self):
        await self.publish()
        async def change():self.do('group.add',{'boss_id':self.bid,'name':'도중추가'})
        self.transport.hook=change
        self.do('board.refresh',{})
        await self.worker.tick()
        state=self.row('SELECT * FROM board_sync_state WHERE generation=1')[0]
        self.assertLess(state['completed'],state['requested'])
        await self.worker.tick()
        self.assertIn('도중추가',str(self.transport.messages))
        state=self.row('SELECT * FROM board_sync_state WHERE generation=1')[0]
        self.assertEqual(state['completed'],state['requested'])

    async def test_inactive_during_preparation_must_be_cleaned_before_promote(self):
        await self.publish()
        async def deactivate():self.do('boss.deactivate',{'id':self.bid})
        self.transport.hook=deactivate
        self.transport.hook_channel=200
        await self.publish(200)
        self.assertEqual(self.s.settings(self.a)['active_generation'],1)
        await self.worker.tick()
        self.assertEqual(self.s.settings(self.a)['active_generation'],2)
        self.assertIn('운영 중단',str(self.transport.messages))
        self.assertNotIn('파티 0개',str(self.transport.messages))

    async def test_default_channel_does_not_move_existing(self):
        await self.publish()
        self.do('settings.channel',{'value':200})
        await self.publish()
        self.assertEqual(self.s.settings(self.a)['last_channel_id'],100)
        self.assertEqual(self.s.settings(self.a)['active_generation'],1)

    async def test_zero_active_bosses_then_new_boss_is_published(self):
        self.do('boss.deactivate',{'id':self.bid})
        await self.publish()
        self.assertEqual(self.s.settings(self.a)['active_generation'],1)
        self.assertFalse(self.transport.messages)
        self.do('boss.activate',{'id':self.bid})
        await self.worker.tick()
        self.assertEqual(len(self.transport.messages),1)

    async def test_page_shrink_deletes_surplus(self):
        for n in range(90):self.do('group.add',{'boss_id':self.bid,'name':'가나다라마바사아자차카타파하'*3+str(n)})
        await self.publish()
        self.assertGreater(len(self.transport.messages),1)
        with self.db.transaction() as c:
            c.execute("UPDATE boss_groups SET status='deleted' WHERE guild_id=1")
            self.s.touch(c,1)
        await self.worker.tick()
        self.assertEqual(len(self.transport.messages),1)

    async def test_message_adoption_after_uncertain_send(self):
        self.do('board.publish',{})
        marker=f'maple:1:1:{self.bid}:0'
        await self.transport.send(100,{'title':'old','description':'old'},marker)
        await self.worker.tick()
        self.assertEqual(len(self.transport.messages),1)
        self.assertEqual(len(self.row('SELECT * FROM board_messages')),1)

    async def test_retired_generation_orphan_recovered_after_restart(self):
        await self.publish()
        await self.transport.send(100,{'title':'orphan','description':'orphan'},f'maple:1:1:{self.bid}:9')
        self.do('board.stop',{})
        self.worker=BoardWorker(Service(Database(self.db.path)),self.transport)
        await self.worker.tick()
        self.assertFalse(self.transport.messages)

    async def test_refresh_specific_boss_does_not_dirty_others(self):
        self.do('boss.add',{'name':'보스B','difficulty':'노말'})
        await self.publish()
        before={r['boss_id']:r['requested'] for r in self.row('SELECT * FROM board_sync_state WHERE generation=1')}
        self.do('board.refresh',{'boss_id':self.bid})
        after={r['boss_id']:r['requested'] for r in self.row('SELECT * FROM board_sync_state WHERE generation=1')}
        self.assertEqual(after[self.bid],before[self.bid]+1)
        for bid in before:
            if bid!=self.bid:self.assertEqual(after[bid],before[bid])
