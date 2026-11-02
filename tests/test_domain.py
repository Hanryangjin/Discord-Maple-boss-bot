import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path
from maplebot.db import Database
from maplebot.service import Actor, Service
from maplebot.validation import UserError, character_name, hexa, show_hexa
from maplebot.render import boss_pages, character_pages


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name)/'db.sqlite3'))
        self.s = Service(self.db)
        self.admin = Actor(1, 10, True)
        self.other = Actor(2, 20, True)
        for uid in range(10, 31):
            self.s.observe_member(1, uid, f'user{uid}', 'present')
        self.s.observe_member(2, 20, 'other', 'present')
        self.job = self.s.rows(1, 'jobs', actor=self.admin)[0]['id']
        self.do('settings.role', {'value': 99})
        self.manager = Actor(1, 11, False, (99,))
        self.do('boss.add', {'name':'보스A','difficulty':'하드','max_members':6})
        self.bid = self.s.rows(1, 'bosses', actor=self.admin)[0]['id']
        self.do('group.add', {'boss_id':self.bid,'name':'파티1'})
        self.gid = self.s.rows(1, 'boss_groups', actor=self.admin)[0]['id']

    def do(self, action, payload, actor=None):
        actor = actor or self.admin
        result = self.s.execute(actor, action, payload)
        if result['status'] == 'confirm':
            result = self.s.execute(actor, action, payload, result['token'])
        return result

    def char(self, name='캐릭터A', owner=10):
        self.do('character.add', {'name':name,'owner_id':owner,'job_id':self.job,'hexa':'5.25'})
        return next(r['id'] for r in self.s.rows(1,'characters',actor=self.admin,state='all') if r['name']==name)

    def sql(self, query, args=()):
        with self.db.transaction(False) as c:
            return [dict(r) for r in c.execute(query,args)]


class DomainTests(Fixture):
    def test_register_delegate_and_owner_edit(self):
        cid=self.char(owner=12)
        row=self.sql('SELECT * FROM characters WHERE id=?',(cid,))[0]
        self.assertEqual((row['owner_id'],row['registered_by'],row['hexa_value']),(12,10,52500))
        self.do('character.edit',{'id':cid,'hexa':'6.00'},Actor(1,12))
        with self.assertRaises(UserError): self.do('character.edit',{'id':cid,'hexa':'7.00'},Actor(1,13))
        with self.assertRaises(UserError): self.do('character.add',{'name':'타인캐릭','owner_id':12,'job_id':self.job},Actor(1,13))

    def test_casefold_duplicates_and_same_character_case_change(self):
        cid=self.char('Maple01')
        with self.assertRaises(UserError):self.char('MAPLE01')
        self.do('character.edit',{'id':cid,'name':'maple01'})
        self.assertEqual(self.sql('SELECT name FROM characters WHERE id=?',(cid,))[0]['name'],'maple01')
        self.do('character.deactivate',{'id':cid})
        with self.assertRaises(UserError):self.char('Maple01')

    def test_other_guild_cannot_access(self):
        cid=self.char()
        with self.assertRaises(UserError):self.do('character.edit',{'id':cid,'name':'변경이름'},self.other)
        with self.assertRaises(UserError):self.do('group.member',{'id':self.gid,'character_id':cid},self.other)

    def test_capacity_and_duplicate_and_owner(self):
        ids=[self.char(f'캐릭터{n}',10+n) for n in range(7)]
        for cid in ids[:6]:self.do('group.member',{'id':self.gid,'character_id':cid})
        with self.assertRaises(UserError):self.do('group.member',{'id':self.gid,'character_id':ids[6]})
        self.do('group.add',{'boss_id':self.bid,'name':'파티2'})
        target=self.s.rows(1,'boss_groups',actor=self.admin)[1]['id']
        with self.assertRaises(UserError):self.do('group.member',{'id':target,'character_id':ids[0]})
        self.assertEqual(len(self.sql('SELECT * FROM group_members')),6)

    def test_same_owner_and_reassignment(self):
        a=self.char('캐릭터1',10);b=self.char('캐릭터2',11);c=self.char('캐릭터3',10)
        self.do('group.member',{'id':self.gid,'character_id':a})
        with self.assertRaises(UserError):self.do('group.member',{'id':self.gid,'character_id':c})
        self.do('group.member',{'id':self.gid,'character_id':b})
        with self.assertRaises(UserError):self.do('character.owner',{'id':b,'owner_id':10})
        self.assertEqual(self.sql('SELECT owner_id FROM characters WHERE id=?',(b,))[0]['owner_id'],11)

    def test_failed_move_rolls_back(self):
        cid=self.char()
        self.do('group.member',{'id':self.gid,'character_id':cid})
        self.do('group.add',{'boss_id':self.bid,'name':'파티2'})
        target=self.s.rows(1,'boss_groups',actor=self.admin)[1]['id']
        other=self.char('다른캐릭',10)
        self.do('group.member',{'id':target,'character_id':other})
        with self.assertRaises(UserError):self.do('group.move',{'id':self.gid,'target_id':target,'character_id':cid})
        self.assertEqual(self.sql('SELECT group_id FROM group_members WHERE character_id=?',(cid,))[0]['group_id'],self.gid)

    def test_merge_partial_failure_rolls_back_all(self):
        ids=[self.char(f'캐릭터{n}',10+n) for n in range(4)]
        self.do('boss.edit',{'id':self.bid,'max_members':3})
        self.do('group.add',{'boss_id':self.bid,'name':'파티2'})
        target=self.s.rows(1,'boss_groups',actor=self.admin)[1]['id']
        for cid in ids[:2]:self.do('group.member',{'id':self.gid,'character_id':cid})
        for cid in ids[2:]:self.do('group.member',{'id':target,'character_id':cid})
        with self.assertRaises(UserError):self.do('group.merge',{'id':self.gid,'target_id':target})
        self.assertEqual(len(self.sql('SELECT * FROM group_members WHERE group_id=?',(self.gid,))),2)

    def test_capacity_shrink_manual_cleanup(self):
        ids=[self.char(f'캐릭터{n}',10+n) for n in range(3)]
        for cid in ids:self.do('group.member',{'id':self.gid,'character_id':cid})
        with self.assertRaisesRegex(UserError,'파티1 3명'):self.do('boss.edit',{'id':self.bid,'max_members':2})
        self.do('group.remove',{'id':self.gid,'character_id':ids[-1]})
        self.do('boss.edit',{'id':self.bid,'max_members':2})

    def test_inactive_boss_exclusion_and_deleted_restore(self):
        cid=self.char();self.do('group.member',{'id':self.gid,'character_id':cid})
        self.do('boss.deactivate',{'id':self.bid})
        with self.assertRaises(UserError):self.do('character.deactivate',{'id':cid})
        self.do('group.remove',{'id':self.gid,'character_id':cid})
        self.do('boss.delete',{'id':self.bid})
        with self.assertRaises(UserError):self.do('boss.activate',{'id':self.bid})
        self.do('boss.restore',{'id':self.bid})
        self.assertEqual(self.sql('SELECT status FROM bosses WHERE id=?',(self.bid,))[0]['status'],'inactive')
        self.assertEqual(self.sql('SELECT status FROM boss_groups WHERE id=?',(self.gid,))[0]['status'],'deleted')
        self.assertEqual(len(self.sql('SELECT * FROM characters')),1)
        with self.assertRaises(UserError):self.do('boss.add',{'name':'보스A','difficulty':'하드'})

    def test_confirmation_double_and_stale_and_revoked(self):
        p={'id':self.gid}
        first=self.s.execute(self.manager,'group.delete',p)
        self.assertEqual(first['status'],'confirm')
        self.s.execute(self.manager,'group.delete',p,first['token'])
        repeated=self.s.execute(self.manager,'group.delete',p,first['token'])
        self.assertTrue(repeated['duplicate'])
        self.do('group.add',{'boss_id':self.bid})
        other=self.s.rows(1,'boss_groups',actor=self.admin)[0]['id']
        first=self.s.execute(self.manager,'group.delete',{'id':other})
        self.char()
        with self.assertRaises(UserError):self.s.execute(self.manager,'group.delete',{'id':other},first['token'])
        with self.assertRaises(UserError):self.s.execute(Actor(1,11),'group.delete',{'id':other},first['token'])

    def test_confirmation_expiry_and_atomic_audit_rollback(self):
        p={'id':self.gid}
        pending=self.s.execute(self.admin,'group.delete',p)
        with self.db.transaction() as c:
            c.execute('UPDATE confirmations SET expires_at=0 WHERE id=?',(pending['token'],))
        before=len(self.sql('SELECT * FROM audit_logs'))
        with self.assertRaisesRegex(UserError,'만료'):
            self.s.execute(self.admin,'group.delete',p,pending['token'])
        self.assertEqual(len(self.sql('SELECT * FROM audit_logs')),before)
        self.assertEqual(self.sql('SELECT status FROM boss_groups WHERE id=?',(self.gid,))[0]['status'],'active')

    def test_same_name_other_guild_and_job_scope(self):
        self.char('Same01')
        foreign=self.s.rows(2,'jobs',actor=self.other)[0]['id']
        self.do('character.add',{'name':'Same01','job_id':foreign,'owner_id':20},self.other)
        with self.assertRaises(UserError):self.do('character.add',{'name':'New01','job_id':foreign,'owner_id':10})

    def test_job_inactive_preserves_char_and_assignment(self):
        cid=self.char()
        self.do('job.deactivate',{'id':self.job})
        with self.assertRaises(UserError):self.char('캐릭터B',11)
        self.do('character.edit',{'id':cid,'hexa':'6.00'})
        self.do('group.member',{'id':self.gid,'character_id':cid})
        self.do('job.rename',{'id':self.job,'name':'직업새이름'})
        with self.db.transaction(False) as c:
            rendered=str(boss_pages(c,1,self.bid))
        self.assertIn('직업새이름',rendered)
        self.assertIn('비활성 직업',rendered)
        with self.assertRaises(UserError):self.do('job.add',{'name':'직업새이름'})

    def test_job_initialization_preserves_admin_changes(self):
        self.do('job.rename',{'id':self.job,'name':'변경직업'})
        self.do('job.deactivate',{'id':self.job})
        s2=Service(Database(self.db.path))
        s2.rows(1,'jobs',actor=self.admin,state='all')
        row=self.sql('SELECT * FROM jobs WHERE id=?',(self.job,))[0]
        self.assertEqual((row['name'],row['active']),('변경직업',0))

    def test_left_unknown_and_return(self):
        cid=self.char()
        for state in ('left','unknown'):
            self.s.observe_member(1,10,'old',state)
            with self.assertRaises(UserError):self.do('group.member',{'id':self.gid,'character_id':cid})
        self.s.observe_member(1,10,'new','present')
        self.do('group.member',{'id':self.gid,'character_id':cid})
        self.s.observe_member(1,10,'new','left')
        self.assertEqual(len(self.sql('SELECT * FROM group_members')),1)
        with self.db.transaction(False) as c:
            self.assertIn('서버 탈퇴',str(boss_pages(c,1,self.bid)))
        self.do('group.remove',{'id':self.gid,'character_id':cid})

    def test_render_hide_inactive_and_single_character_reverse_lookup(self):
        cid=self.char();other=self.char('다른캐릭',11)
        for c in (cid,other):self.do('group.member',{'id':self.gid,'character_id':c})
        with self.db.transaction(False) as c:
            self.assertIn('다른캐릭',str(character_pages(c,1,cid)))
            self.assertIn('5.25만',str(boss_pages(c,1,self.bid)))
        self.do('boss.deactivate',{'id':self.bid})
        with self.db.transaction(False) as c:
            self.assertNotIn('다른캐릭',str(character_pages(c,1,cid)))
            self.assertNotIn('캐릭터A',str(boss_pages(c,1,self.bid)))
            self.assertIn('캐릭터A',str(boss_pages(c,1,self.bid,public=False)))

    def test_concurrent_capacity(self):
        self.do('boss.edit',{'id':self.bid,'max_members':1})
        ids=[self.char('캐릭터1',10),self.char('캐릭터2',11)]
        def add(cid):
            try:self.do('group.member',{'id':self.gid,'character_id':cid});return True
            except UserError:return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            result=list(pool.map(add,ids))
        self.assertEqual(sum(result),1)
        self.assertEqual(len(self.sql('SELECT * FROM group_members')),1)

    def test_backup_restores(self):
        self.char()
        path=self.db.backup(str(Path(self.tmp.name)/'backups'))
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM characters').fetchone()[0],1)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')


class ValidationTests(unittest.TestCase):
    def test_name_boundaries(self):
        for value in ('a1','가a','가나','가나다라마바','abcdefghijkl','가나다abc123'):
            self.assertEqual(character_name(value),value)
        for value in ('a','가','가나다라마바사','abcdefghijklm','ㄱㅏ','가ㄱ','a b','a!','가나다abc1234','ＡＢ'):
            with self.subTest(value=value),self.assertRaises(UserError):character_name(value)

    def test_hexa(self):
        for text,expected in [('5.25',52500),('5',50000),('0.01',100),('5.2500',52500)]:
            self.assertEqual(hexa(text),expected)
        for text in ('0','-1','1e2','1.001','NaN','5.25만','1,000','5.250000000000000000000000000000001','999999999999999999999999'):
            with self.subTest(text=text),self.assertRaises(UserError):hexa(text)
        self.assertEqual(show_hexa(52500),'5.25만')
        self.assertEqual(show_hexa(None),'미등록')
