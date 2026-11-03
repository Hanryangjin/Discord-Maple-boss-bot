import tempfile
from pathlib import Path
import unittest
from maplebot.db import Database
from maplebot.service import Service,Actor
from maplebot.board import BoardWorker
from maplebot.validation import UserError
from tests.test_domain import Fixture
from tests.test_board import FakeDiscord


class OrderServiceTests(Fixture):
    def add_boss(self,name):
        self.do('boss.add',{'name':name,'difficulty':'하드'})
        return next(r['id'] for r in self.s.rows(1,'bosses',actor=self.admin) if r['name']==name)

    def order(self):return [r['id'] for r in self.s.rows(1,'bosses',actor=self.admin)]

    def test_position_moves_others_without_duplicate_ranks(self):
        b=self.add_boss('보스B');c=self.add_boss('보스C')
        self.do('boss.order',{'id':c,'position':1},self.manager)
        self.assertEqual(self.order(),[c,self.bid,b])
        self.assertEqual([r['sort_order'] for r in self.s.rows(1,'bosses',actor=self.admin)],[1,2,3])
        self.do('boss.edit',{'id':c,'sort_order':3})
        self.assertEqual(self.order(),[self.bid,b,c])

    def test_bounds_permissions_and_cross_guild(self):
        self.add_boss('보스B')
        for position in (0,-1,3,'1.5',True):
            with self.assertRaises(UserError):self.do('boss.order',{'id':self.bid,'position':position})
        with self.assertRaises(UserError):self.do('boss.order',{'id':self.bid,'position':2},Actor(1,12))
        with self.assertRaises(UserError):self.do('boss.order',{'id':self.bid,'position':1},self.other)
        self.assertEqual(self.order()[0],self.bid)

    def test_confirmation_is_atomic_and_noop_does_not_republish(self):
        b=self.add_boss('보스B')
        p={'id':b,'position':1}
        pending=self.s.execute(self.admin,'boss.order',p)
        self.assertEqual(pending['status'],'confirm')
        self.assertEqual(self.order(),[self.bid,b])
        self.s.execute(self.admin,'boss.order',p,pending['token'])
        repeated=self.s.execute(self.admin,'boss.order',p,pending['token'])
        self.assertTrue(repeated['duplicate'])
        before=len(self.sql('SELECT * FROM audit_logs'))
        r=self.s.execute(self.admin,'boss.order',p)
        self.assertEqual(r['status'],'done')
        self.assertEqual(len(self.sql('SELECT * FROM audit_logs')),before)

    def test_inactive_omitted_and_reactivation_appends(self):
        b=self.add_boss('보스B');c=self.add_boss('보스C')
        self.do('boss.deactivate',{'id':b})
        with self.assertRaises(UserError):self.do('boss.order',{'id':b,'position':1})
        self.do('boss.order',{'id':c,'position':1})
        self.do('boss.activate',{'id':b})
        self.assertEqual(self.order(),[c,self.bid,b])
        d=self.add_boss('보스D')
        self.assertEqual(self.order(),[c,self.bid,b,d])

    def test_pending_publication_blocks_reorder_and_rolls_back_other_edits(self):
        b=self.add_boss('보스B')
        self.do('settings.channel',{'value':100})
        self.do('board.publish',{})
        with self.assertRaises(UserError):self.do('boss.edit',{'id':b,'sort_order':1,'name':'바뀌면안됨'})
        self.assertEqual(self.order(),[self.bid,b])
        self.assertEqual(self.sql('SELECT name FROM bosses WHERE id=?',(b,))[0]['name'],'보스B')

    def test_rank_change_survives_database_restart(self):
        b=self.add_boss('보스B')
        self.do('boss.order',{'id':b,'position':1})
        restarted=Service(Database(self.db.path))
        self.assertEqual([r['id'] for r in restarted.rows(1,'bosses',actor=self.admin)],[b,self.bid])


class OrderBoardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(str(Path(self.tmp.name)/'db.sqlite3'));self.s=Service(self.db);self.a=Actor(1,10,True)
        self.s.observe_member(1,10,'user','present')
        self.ids=[]
        for name in ('보스A','보스B','보스C'):
            self.do('boss.add',{'name':name,'difficulty':'하드'})
            self.ids.append(next(r['id'] for r in self.s.rows(1,'bosses',actor=self.a) if r['name']==name))
        self.do('settings.channel',{'value':100})
        self.transport=FakeDiscord();self.worker=BoardWorker(self.s,self.transport)

    def do(self,action,payload):
        r=self.s.execute(self.a,action,payload)
        return self.s.execute(self.a,action,payload,r['token']) if r['status']=='confirm' else r

    def displayed(self):
        current=self.s.settings(self.a)['active_generation']
        result=[]
        for (channel,mid),message in sorted(self.transport.messages.items(),key=lambda item:item[0][1]):
            parts=message['marker'].split(':')
            if len(parts)==5 and parts[2]==str(current):result.append((int(parts[3]),int(parts[4])))
        return result

    async def settle(self,n=4):
        for _ in range(n):await self.worker.tick()

    async def test_initial_publication_follows_order_not_boss_id(self):
        self.do('boss.order',{'id':self.ids[2],'position':1})
        self.do('board.publish',{})
        await self.settle()
        self.assertEqual(self.displayed(),[(self.ids[2],0),(self.ids[0],0),(self.ids[1],0)])

    async def test_existing_messages_rebuilt_only_after_confirmation_and_preparation(self):
        self.do('board.publish',{});await self.settle()
        originals=set(self.transport.messages)
        payload={'id':self.ids[2],'position':1}
        pending=self.s.execute(self.a,'boss.order',payload)
        self.assertIsNone(self.s.settings(self.a)['preparing_generation'])
        self.s.execute(self.a,'boss.order',payload,pending['token'])
        self.assertEqual(self.s.settings(self.a)['active_generation'],1)
        self.assertEqual(set(self.transport.messages),originals)
        await self.settle()
        self.assertEqual(self.s.settings(self.a)['active_generation'],2)
        self.assertEqual(self.displayed(),[(self.ids[2],0),(self.ids[0],0),(self.ids[1],0)])
        self.assertFalse(originals & set(self.transport.messages))

    async def test_permission_failure_preserves_old_board_then_recovers(self):
        self.do('board.publish',{});await self.settle()
        originals=set(self.transport.messages)
        self.do('boss.order',{'id':self.ids[2],'position':1})
        self.transport.fail_send=PermissionError('forbidden')
        await self.worker.tick()
        self.assertEqual(self.s.settings(self.a)['active_generation'],1)
        self.assertIsNone(self.s.settings(self.a)['preparing_generation'])
        self.assertEqual(set(self.transport.messages),originals)
        counter=self.s.settings(self.a)['generation_counter']
        await self.worker.tick()
        self.assertEqual(self.s.settings(self.a)['generation_counter'],counter)
        self.transport.fail_send=None
        with self.db.transaction() as c:c.execute('UPDATE board_sync_state SET retry_at=0')
        await self.settle()
        self.assertEqual(self.displayed(),[(self.ids[2],0),(self.ids[0],0),(self.ids[1],0)])

    async def test_missing_first_boss_message_repaired_without_permanent_reordering(self):
        self.do('board.publish',{});await self.settle()
        first=min(self.transport.messages,key=lambda item:item[1])
        del self.transport.messages[first]
        self.do('board.refresh',{'boss_id':self.ids[0]})
        await self.settle()
        self.assertEqual(self.displayed(),[(self.ids[0],0),(self.ids[1],0),(self.ids[2],0)])
        self.assertGreater(self.s.settings(self.a)['active_generation'],1)

    async def test_page_growth_keeps_first_boss_pages_together(self):
        self.do('board.publish',{});await self.settle()
        for n in range(6):self.do('group.add',{'boss_id':self.ids[0],'name':f'파티{n}'})
        await self.settle()
        self.assertEqual(self.displayed(),[(self.ids[0],0),(self.ids[0],1),(self.ids[1],0),(self.ids[2],0)])

    async def test_preparation_growth_restarts_ordered_generation(self):
        self.do('board.publish',{});await self.settle()
        async def grow():
            for n in range(6):self.do('group.add',{'boss_id':self.ids[0],'name':f'파티{n}'})
        self.transport.hook=grow;self.transport.hook_channel=200
        self.do('board.publish',{'channel_id':200})
        await self.settle(5)
        self.assertEqual(self.s.settings(self.a)['last_channel_id'],200)
        self.assertEqual(self.displayed(),[(self.ids[0],0),(self.ids[0],1),(self.ids[1],0),(self.ids[2],0)])
        self.assertTrue(all(ch==200 for ch,mid in self.transport.messages))
