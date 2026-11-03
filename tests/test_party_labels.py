"""Compact party labels stay within unchanged tiles, including long custom data."""
import copy
import unittest
from unittest.mock import patch
from PIL import Image,ImageDraw
from maplebot.party_image import cell_label,font,render_party
from maplebot.job_icons import JobIconStore
from maplebot.thumbnails import decode
from tests.test_pagination import group


class CompactPartyTests(unittest.TestCase):
    def test_long_names_jobs_and_owners_fit_their_columns(self):
        draw=ImageDraw.Draw(Image.new('RGB',(1120,282)))
        for text,width,size in [('모꼬고랄곡기',224,36),('서버관리자가추가한매우긴직업',112,32),
                                ('아주긴디스코드소유자이름',112,32),('미확인·소유자',112,32),
                                ('99999.99만',224,34)]:
            label,actual=cell_label(draw,text,width,size)
            self.assertLessEqual(draw.textlength(label,font=font(actual)),width)
            self.assertGreaterEqual(actual,24)
        self.assertEqual(cell_label(draw,'치릿차',224,36),('치릿차',36))
        self.assertEqual(cell_label(draw,'서공민',112,32),('서공민',32))

    def test_two_rows_enlarged_icon_and_alias_without_mutating_data(self):
        g=group(1,2);g['members'][0]['name']='설메한량';g['members'][0]['job_name']='아크메이지(썬,콜)'
        before=copy.deepcopy(g)
        icons=JobIconStore('/tmp/empty-party-icons')
        from maplebot.party_image import draw_cell
        with patch('maplebot.party_image.draw_cell',wraps=draw_cell) as cell:
            result=render_party(g,2,icons,1)
        self.assertEqual(decode(result).size,(1120,282))
        self.assertEqual(g,before)
        calls=cell.call_args_list
        self.assertEqual(len(calls),8)
        self.assertEqual(calls[0].args[1],(184,146))
        self.assertEqual(calls[1].args[2],'썬콜')
        self.assertEqual(calls[2].args[2],'7.20만')
        self.assertEqual(calls[3].args[2],'소유자')
        self.assertTrue(all(c.args[1][1] in (146,206) for c in calls))
        self.assertEqual(decode(icons.face(1,g['members'][0])).size,(96,96))

    def test_status_remains_in_compact_labels(self):
        g=group(1,1);g['members'][0].update(job_active=False,membership='left')
        from maplebot.party_image import draw_cell
        with patch('maplebot.party_image.draw_cell',wraps=draw_cell) as cell:
            render_party(g,6,JobIconStore('/tmp/empty-party-icons'),1)
        self.assertIn('중지',cell.call_args_list[1].args[2])
        self.assertTrue(cell.call_args_list[3].args[2].startswith('탈퇴·'))
