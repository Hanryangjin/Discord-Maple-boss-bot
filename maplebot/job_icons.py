"""Stable job-to-SD artwork bindings and a universal placeholder."""
from functools import lru_cache
from pathlib import Path
import hashlib
import io
import json
import logging
import re
from .validation import key

log=logging.getLogger(__name__)
BUILTIN_ROOT=Path(__file__).with_name('resources')/'job_icons'
EXTENSIONS=('.png','.webp','.jpg','.jpeg','.gif')


def catalog(root=BUILTIN_ROOT):
    try:
        return json.loads((Path(root)/'manifest.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return {'icons':{},'legacy_seed_order':[]}


def seed_icon_keys():
    return {key(info['job']):icon_key for icon_key,info in catalog()['icons'].items()}


@lru_cache(maxsize=1)
def placeholder_icon():
    from PIL import Image,ImageDraw,ImageFont
    image=Image.new('RGBA',(96,96),(0,0,0,0))
    draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((1,1,94,94),radius=16,fill=(55,59,69,255),outline=(135,145,160,255),width=2)
    draw.text((48,48),'?',font=ImageFont.load_default(size=52),anchor='mm',fill=(220,225,235,255))
    output=io.BytesIO();image.save(output,format='PNG')
    return output.getvalue()


class JobIconStore:
    def __init__(self,root,builtin_root=BUILTIN_ROOT):
        self.root=Path(root).resolve()
        self.builtin_root=Path(builtin_root).resolve()
        self.catalog=catalog(self.builtin_root)['icons']

    def _read(self,path,root):
        from .thumbnails import MAX_BYTES
        path=path.resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size>MAX_BYTES:
            return None
        data=path.read_bytes()
        return data if len(data)<=MAX_BYTES else None

    def resolve(self,guild_id,job_id,icon_key):
        # Never guess the job image from mutable job/character names.
        if isinstance(job_id,int) and job_id>0:
            for extension in EXTENSIONS:
                try:
                    data=self._read(self.root/str(guild_id)/(str(job_id)+extension),self.root)
                    if data is not None:return data,None
                except OSError:continue
        if isinstance(icon_key,str) and re.fullmatch(r'job-[0-9]{3}',icon_key):
            info=self.catalog.get(icon_key)
            if info and isinstance(info.get('file'),str):
                try:
                    data=self._read(self.builtin_root/info['file'],self.builtin_root)
                    if data is not None and hashlib.sha256(data).hexdigest()==info['sha256']:
                        return data,info.get('crop')
                except (OSError,KeyError,ValueError):pass
        return None,None

    def face(self,guild_id,member):
        from PIL import Image,ImageDraw,ImageOps
        from .thumbnails import decode
        try:
            data,crop=self.resolve(guild_id,member.get('job_id'),member.get('job_icon_key'))
            if data is None:return placeholder_icon()
            image=decode(data)
            if crop is not None:
                if (not isinstance(crop,list) or len(crop)!=4 or not all(isinstance(v,int) for v in crop)
                    or not (0<=crop[0]<crop[2]<=image.width and 0<=crop[1]<crop[3]<=image.height)):
                    return placeholder_icon()
                image=image.crop(tuple(crop))
            face=ImageOps.contain(image,(88,88),method=Image.Resampling.LANCZOS)
            result=Image.new('RGBA',(96,96),(0,0,0,0))
            draw=ImageDraw.Draw(result)
            draw.rounded_rectangle((1,1,94,94),radius=16,fill=(43,45,49,255),outline=(105,115,130,255),width=2)
            result.alpha_composite(face,((96-face.width)//2,(96-face.height)//2))
            output=io.BytesIO();result.save(output,format='PNG')
            return output.getvalue()
        except Exception as error:
            log.warning('job icon placeholder: %s',type(error).__name__)
            return placeholder_icon()
