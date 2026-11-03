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


JOB_LABELS={
    '아크메이지(불,독)':'불독','아크메이지(썬,콜)':'썬콜',
    '다크나이트':'닼나','패스파인더':'패파','나이트로드':'나로',
    '듀얼블레이더':'듀블','캐논슈터':'캐슈','소울마스터':'소마',
    '플레임위자드':'플위','윈드브레이커':'윈브','나이트워커':'나워',
    '스트라이커':'스커','루미너스':'루미','메르세데스':'메르',
    '배틀메이지':'배메','와일드헌터':'와헌','데몬슬레이어':'데슬',
    '데몬어벤져':'데벤','엔젤릭버스터':'엔버','키네시스':'키네',
}


def cell_label(draw,text,width,size,minimum=24):
    text=''.join(c for c in str(text) if c.isprintable())
    while size>minimum and draw.textlength(text,font=font(size))>width:
        size-=1
    return fit(draw,text,width,size),size


def draw_cell(draw,position,text,width,size,fill,*,minimum=24,bold=False):
    label,size=cell_label(draw,text,width,size,minimum)
    draw.text(position,label,font=font(size),fill=fill,anchor='lm',
              stroke_width=1 if bold else 0)


def render_party(group,maximum,icons,guild_id):
    members=group['members']
    rows=max(1,(len(members)+1)//2)
    image=Image.new('RGB',(1120,112+rows*170),(27,29,33))
    draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((1,1,1118,image.height-2),radius=20,outline=(90,100,110),width=2)
    draw.line((24,8,1096,8),fill=(101,173,134),width=6)
    draw.text((28,28),fit(draw,group['name'],860,36),font=font(36),fill='white')
    draw.text((950,36),f'{len(members)}/{maximum}명',font=font(28),fill=(200,210,220))
    for index,m in enumerate(members):
        x=24+(index%2)*548;y=100+(index//2)*170
        draw.rounded_rectangle((x,y,x+524,y+150),radius=12,fill=(38,41,46),outline=(70,75,85),width=2)
        icon=decode(icons.face(guild_id,m)).resize((124,124),Image.Resampling.LANCZOS)
        image.paste(icon,(x+12,y+13),icon)
        # Keep the 524x150 tile, using two text rows beside the larger icon.
        # Column 1: nickname / hexa; column 2: job / owner.
        draw_cell(draw,(x+160,y+46),m['name'],224,36,'white',bold=True)
        job=JOB_LABELS.get(m['job_name'],m['job_name'])
        if not m['job_active']:job+='·중지'
        draw_cell(draw,(x+396,y+46),job,112,32,(220,225,232))
        draw_cell(draw,(x+160,y+106),show_hexa(m['hexa_value']),224,34,'white')
        owner=m.get('display_name') or '사용자'
        if m.get('membership')=='left':owner='탈퇴·'+owner
        elif m.get('membership')!='present':owner='미확인·'+owner
        draw_cell(draw,(x+396,y+106),owner,112,32,(185,200,215))
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
