import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from maplebot.db import Database
from maplebot.job_icons import BUILTIN_ROOT,JobIconStore,catalog,placeholder_icon,seed_icon_keys
from maplebot.service import Service,Actor
from maplebot.validation import key
from maplebot.thumbnails import decode
from tests.test_thumbnails import png


class JobIconTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.store=JobIconStore(self.root/'overrides')

    def test_every_bundled_job_has_valid_face_and_verified_source(self):
        data=catalog()
        self.assertEqual(len(data['icons']),48)
        self.assertEqual(len(data['legacy_seed_order']),47)
        keys=seed_icon_keys()
        self.assertIn(key('렌'),keys)
        self.assertIn(key('레테'),keys)
        self.assertIn(key('비숍'),keys)
        for icon_key,info in data['icons'].items():
            with self.subTest(job=info['job']):
                face=self.store.face(1,{'job_id':1,'job_icon_key':icon_key})
                self.assertNotEqual(face,placeholder_icon())
                image=decode(face)
                self.assertEqual(image.size,(96,96))
                self.assertIsNotNone(image.getbbox())

    def test_unknown_missing_and_corrupt_use_identical_placeholder(self):
        for icon_key in (None,'not-registered','../../escape','job-999'):
            self.assertEqual(self.store.face(1,{'job_id':1,'job_icon_key':icon_key}),placeholder_icon())
        broken=self.root/'broken';broken.mkdir()
        manifest={'icons':{'job-001':{'file':'broken.webp','sha256':'not-matching','crop':None}}}
        (broken/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        (broken/'broken.webp').write_bytes(b'broken')
        store=JobIconStore(self.root/'empty',broken)
        self.assertEqual(store.face(1,{'job_id':1,'job_icon_key':'job-001'}),placeholder_icon())

    def test_custom_file_binds_to_id_not_mutable_name(self):
        folder=self.store.root/'1';folder.mkdir(parents=True)
        member={'job_id':500,'job_icon_key':None,'job_name':'사용자직업'}
        self.assertEqual(self.store.face(1,member),placeholder_icon())
        (folder/'500.png').write_bytes(png('red'))
        first=self.store.face(1,member)
        self.assertNotEqual(first,placeholder_icon())
        self.assertEqual(first,self.store.face(1,{**member,'job_name':'새이름'}))
        self.assertEqual(self.store.face(2,member),placeholder_icon())
        (folder/'500.png').write_bytes(b'broken')
        self.assertEqual(self.store.face(1,member),placeholder_icon())

    def test_symlink_cannot_escape_image_root(self):
        folder=self.store.root/'1';folder.mkdir(parents=True)
        outside=self.root/'outside.png';outside.write_bytes(png('red'))
        (folder/'500.png').symlink_to(outside)
        self.assertEqual(self.store.face(1,{'job_id':500,'job_icon_key':None}),placeholder_icon())

    def test_seed_rename_and_custom_reused_builtin_name(self):
        db=Database(str(self.root/'db.sqlite3'));service=Service(db);actor=Actor(1,10,True)
        jobs=service.rows(1,'jobs',actor=actor)
        hero=next(j for j in jobs if j['name']=='히어로')
        self.assertEqual(hero['icon_key'],seed_icon_keys()[key('히어로')])
        p={'id':hero['id'],'name':'이름변경직업'}
        pending=service.execute(actor,'job.rename',p)
        service.execute(actor,'job.rename',p,pending['token'])
        service.execute(actor,'job.add',{'name':'히어로'})
        rows=service.rows(1,'jobs',actor=actor)
        renamed=next(j for j in rows if j['id']==hero['id'])
        added=next(j for j in rows if j['name']=='히어로')
        self.assertEqual(renamed['icon_key'],hero['icon_key'])
        self.assertIsNone(added['icon_key'])
        self.assertEqual(self.store.face(1,{'job_id':added['id'],'job_icon_key':added['icon_key']}),placeholder_icon())
        self.assertNotEqual(self.store.face(1,{'job_id':renamed['id'],'job_icon_key':renamed['icon_key']}),placeholder_icon())

    def make_legacy(self,path):
        schema=(Path(__file__).parents[1]/'maplebot'/'schema.sql').read_text(encoding='utf-8').replace(', icon_key TEXT','')
        names=json.loads((Path(__file__).parents[1]/'maplebot'/'jobs.json').read_text(encoding='utf-8'))['jobs'][:47]
        c=sqlite3.connect(path)
        c.executescript(schema)
        c.execute('INSERT INTO guild_settings(guild_id,initialized) VALUES(1,1)')
        c.executemany('INSERT INTO jobs(guild_id,name,name_key) VALUES(1,?,?)',[(n,key(n)) for n in names])
        c.execute("UPDATE jobs SET name='이름변경직업',name_key='이름변경직업' WHERE id=1")
        c.execute("INSERT INTO jobs(guild_id,name,name_key) VALUES(1,'히어로','히어로')")
        c.execute("INSERT INTO discord_users VALUES(1,10,'사용자','present',0,0)")
        c.execute("INSERT INTO characters(guild_id,owner_id,registered_by,name,name_key,job_id,created_at,updated_at) VALUES(1,10,10,'기존캐릭터','기존캐릭터',1,0,0)")
        c.commit();c.close()

    def test_v1_migration_preserves_identity_custom_jobs_and_characters(self):
        path=str(self.root/'legacy.sqlite3');self.make_legacy(path)
        db=Database(path)
        with db.transaction(False) as c:
            hero=c.execute('SELECT * FROM jobs WHERE id=1').fetchone()
            custom=c.execute("SELECT * FROM jobs WHERE name='히어로'").fetchone()
            self.assertEqual(hero['name'],'이름변경직업')
            self.assertEqual(hero['icon_key'],'job-001')
            self.assertIsNone(custom['icon_key'])
            self.assertEqual(c.execute('SELECT job_id FROM characters').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT MAX(version) FROM schema_versions').fetchone()[0],3)
        # Do not reassign an administrator's bindings on every restart.
        with db.transaction() as c:c.execute('UPDATE jobs SET icon_key=NULL WHERE id=1')
        restarted=Database(path)
        with restarted.transaction(False) as c:self.assertIsNone(c.execute('SELECT icon_key FROM jobs WHERE id=1').fetchone()[0])

    def test_failed_migration_rolls_back_and_can_retry(self):
        path=str(self.root/'legacy.sqlite3');self.make_legacy(path)
        with sqlite3.connect(path) as c:
            c.executescript("CREATE TRIGGER reject_icon BEFORE UPDATE ON jobs BEGIN SELECT RAISE(ABORT,'blocked'); END;")
        with self.assertRaises(sqlite3.IntegrityError):Database(path)
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute('SELECT MAX(version) FROM schema_versions').fetchone()[0],1)
            self.assertNotIn('icon_key',[r[1] for r in c.execute('PRAGMA table_info(jobs)')])
            c.execute('DROP TRIGGER reject_icon')
        db=Database(path)
        with db.transaction(False) as c:self.assertEqual(c.execute('SELECT icon_key FROM jobs WHERE id=1').fetchone()[0],'job-001')

    def test_new_guild_seeds_lethe_with_sd(self):
        db=Database(str(self.root/'new.sqlite3'))
        with db.transaction() as c:db.initialize(c,1)
        with db.transaction(False) as c:
            rows=c.execute("SELECT * FROM jobs WHERE guild_id=1 AND name='레테'").fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['icon_key'],'job-048')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],48)
        self.assertNotEqual(self.store.face(1,{'job_id':rows[0]['id'],'job_icon_key':rows[0]['icon_key']}),placeholder_icon())

    def make_v2(self,path,existing=False):
        db=Database(path)
        with db.transaction() as c:
            db.initialize(c,1)
            c.execute("DELETE FROM jobs WHERE name='레테'")
            if existing:
                c.execute("INSERT INTO jobs(guild_id,name,name_key,active) VALUES(1,'레테','레테',0)")
            c.execute('DELETE FROM schema_versions WHERE version>=3')

    def test_v2_adds_lethe_once_and_preserves_later_rename(self):
        path=str(self.root/'v2.sqlite3');self.make_v2(path)
        db=Database(path)
        with db.transaction() as c:
            row=c.execute("SELECT * FROM jobs WHERE name='레테'").fetchone()
            self.assertEqual(row['icon_key'],'job-048')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],48)
            c.execute("UPDATE jobs SET name='사용자변경',name_key='사용자변경' WHERE id=?",(row['id'],))
        db=Database(path)
        with db.transaction(False) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],48)
            self.assertEqual(c.execute('SELECT icon_key FROM jobs WHERE id=?',(row['id'],)).fetchone()[0],'job-048')
            self.assertEqual(c.execute("SELECT COUNT(*) FROM jobs WHERE name='레테'").fetchone()[0],0)

    def test_v2_existing_lethe_reuses_id_and_preserves_inactive_state(self):
        path=str(self.root/'existing.sqlite3');self.make_v2(path,True)
        with sqlite3.connect(path) as c:
            before=c.execute("SELECT id FROM jobs WHERE name='레테'").fetchone()[0]
        db=Database(path)
        with db.transaction(False) as c:
            rows=c.execute("SELECT * FROM jobs WHERE name='레테'").fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['id'],before)
            self.assertEqual(rows[0]['active'],0)
            self.assertEqual(rows[0]['icon_key'],'job-048')
