import tempfile
from pathlib import Path
import unittest
from maplebot.db import Database
from maplebot.discord_app import MapleBot,MapleCommands,BoardView
from maplebot.panel import PanelSession


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_tree_serializes_and_ui_fits_discord(self):
        with tempfile.TemporaryDirectory() as temp:
            bot=MapleBot(Database(str(Path(temp)/'db.sqlite3')))
            await bot.add_cog(MapleCommands(bot))
            payload=[c.to_dict(bot.tree) for c in bot.tree.get_commands()]
            groups={c['name']:c for c in payload}
            self.assertEqual(len(groups),8)
            for command in payload:
                self.assertFalse(command['dm_permission'])
                self.assertEqual(command['contexts'],[0])
            self.assertEqual(sum(len(c.get('options',[])) if c['name']!='내보스' else 1 for c in payload),36)
            chars={o['name']:o for o in groups['캐릭터']['options']}
            edit={o['name']:o for o in chars['수정']['options']}
            self.assertIn('헥환초기화',edit)
            self.assertEqual(edit['헥환']['type'],3)  # STRING, not binary floating point
            self.assertTrue(edit['직업']['autocomplete'])
            with bot.service.db.transaction() as c:
                bot.service.db.initialize(c,1)
            for view in (PanelSession(bot,1,1).build(),BoardView(bot)):
                self.assertLessEqual(len(view.children),25)
                self.assertTrue(view.to_components())
            self.assertTrue(BoardView(bot).is_persistent())
            await bot.close()
