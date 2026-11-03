"""Deterministic 2-column party pictures; all rendering runs off the event loop."""
from functools import lru_cache
from pathlib import Path
import asyncio
import hashlib
import io
import discord
from PIL import Image, ImageDraw, ImageFont
from .job_icons import JobIconStore
from .thumbnails import PreparedThumbnails, decode
from .validation import show_hexa

FONT=Path(__file__).with_name('resources')/'fonts'/'MaplePartyUI.otf'

@lru_cache(maxsize=12)
def font(size):
    return ImageFont.truetype(str(FONT),size)


def fit(draw,text,width,size=23):
    text=''.join(c for c in str(text) if c.isprintable())
    if draw.textlength(text,font=font(size))<=width:return text
    while text and draw.textlength(text+'…',font=font(size))>width:
        text=text[:-1]
    return text+'…'


def render_party(group,maximum,icons,guild_id):
    members=group['members']
    rows=max(1,(len(members)+1)//2)
    image=Image.new('RGB',(1120,112+rows*170),(27,29,33))
    draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((1,1,1118,image.height-2),radius=20,outline=(90,100,110),width=2)
    draw.line((24,8,1096,8),fill=(101,173,134),width=6)
    draw.text((28,28),fit(draw,group['name'],860,32),font=font(32),fill='white')
    draw.text((950,36),f'{len(members)}/{maximum}명',font=font(24),fill=(200,210,220))
    for index,m in enumerate(members):
        x=24+(index%2)*548;y=100+(index//2)*170
        draw.rounded_rectangle((x,y,x+524,y+150),radius=12,fill=(38,41,46),outline=(70,75,85),width=2)
        icon=decode(icons.face(guild_id,m));image.paste(icon,(x+12,y+27),icon)
        draw.text((x+120,y+12),fit(draw,m['name'],386,27),font=font(27),fill='white')
        job=m['job_name']+(' (비활성)' if not m['job_active'] else '')
        draw.text((x+120,y+53),fit(draw,job,386),font=font(23),fill=(220,225,232))
        draw.text((x+120,y+84),'헥환 '+show_hexa(m['hexa_value']),font=font(22),fill=(220,225,232))
        owner=m.get('display_name') or '사용자'
        owner+= ' (탈퇴)' if m.get('membership')=='left' else ' (확인 불가)' if m.get('membership')!='present' else ''
        draw.text((x+120,y+115),fit(draw,'소유자: '+owner,386,20),font=font(20),fill=(170,185,200))
    if not members:draw.text((30,140),'빈 파티 · 편집에서 캐릭터를 추가하세요.',font=font(26),fill=(200,210,220))
    out=io.BytesIO();image.save(out,format='PNG');return out.getvalue()


async def prepare_parties(bot,guild_id,groups,maximum):
    if len(groups)>10:raise ValueError('Discord attachment count exceeded')
    composer=getattr(bot,'thumbnails',None)
    icons=composer.icons if composer else JobIconStore('assets/jobs')
    result=PreparedThumbnails()
    try:
        for group in groups:
            data=await asyncio.to_thread(render_party,group,maximum,icons,guild_id)
            digest=hashlib.sha256(data).hexdigest()[:16]
            name=f'party-{group["id"]}-{digest}.png'
            result.urls[group['id']]='attachment://'+name
            result.files.append(discord.File(io.BytesIO(data),filename=name))
        return result
    except BaseException:
        result.close();raise
