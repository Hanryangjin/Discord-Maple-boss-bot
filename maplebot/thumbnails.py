"""Square job SD icon attachments, without Discord avatar composition."""
from collections import OrderedDict
from dataclasses import dataclass, field
import asyncio
import hashlib
import io
import logging
import os
from pathlib import Path

import discord
from PIL import Image, ImageOps

log = logging.getLogger(__name__)
MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 2048 * 2048


def decode(data: bytes) -> Image.Image:
    if len(data) > MAX_BYTES:
        raise ValueError('image byte limit')
    with Image.open(io.BytesIO(data)) as source:
        if source.width <= 0 or source.height <= 0 or source.width * source.height > MAX_PIXELS:
            raise ValueError('image dimension limit')
        source.seek(0)  # Animated inputs intentionally use their first frame.
        return ImageOps.exif_transpose(source).convert('RGBA')


def compose(icon_data: bytes) -> bytes:
    # Discord thumbnails may crop non-square inputs. Contain the complete icon
    # in a square canvas so narrow/wide overrides keep all their contents.
    icon = ImageOps.contain(decode(icon_data), (96,96), method=Image.Resampling.LANCZOS)
    square = Image.new('RGBA', (96,96), (0,0,0,0))
    square.alpha_composite(icon, ((96-icon.width)//2,(96-icon.height)//2))
    output = io.BytesIO()
    square.save(output, format='PNG')
    return output.getvalue()


@dataclass
class PreparedThumbnails:
    urls: dict[int,str] = field(default_factory=dict)
    files: list[discord.File] = field(default_factory=list)

    def close(self):
        for file in self.files:
            file.close()


class ThumbnailComposer:
    def __init__(self, bot, icon_dir=None):
        self.bot = bot
        self.root = Path(icon_dir or os.getenv('MAPLEBOT_JOB_ICONS','assets/jobs')).resolve()
        from .job_icons import JobIconStore
        self.icons = JobIconStore(self.root)
        self.image_cache = OrderedDict()
        self.semaphore = asyncio.Semaphore(4)

    async def one(self, guild_id, member):
        try:
            async with self.semaphore:
                icon = await asyncio.to_thread(self.icons.face, guild_id, member)
            digest = hashlib.sha256(b'job-square-v2\0'+icon).hexdigest()
            image = self.image_cache.get(digest)
            if image is None:
                async with self.semaphore:
                    image = await asyncio.to_thread(compose, icon)
                self.image_cache[digest] = image
            self.image_cache.move_to_end(digest)
            while len(self.image_cache) > 128 or sum(map(len,self.image_cache.values())) > 16*1024*1024:
                self.image_cache.popitem(last=False)
            return member['id'], digest, image
        except Exception as error:
            log.warning('character thumbnail fallback: %s',type(error).__name__)
            return None

    async def prepare(self, guild_id, members):
        unique = {int(member['id']):member for member in members}
        if len(unique) > 10:
            raise ValueError('Discord attachment count exceeded')
        results = await asyncio.gather(*(self.one(guild_id,member) for member in unique.values()))
        prepared = PreparedThumbnails()
        for result in results:
            if result is None:
                continue
            char_id, digest, data = result
            filename = f'character-{char_id}-{digest[:16]}.png'
            prepared.urls[char_id] = 'attachment://'+filename
            prepared.files.append(discord.File(io.BytesIO(data),filename=filename))
        return prepared

    async def close(self):
        self.image_cache.clear()


async def prepare_thumbnails(bot, guild_id, members):
    composer = getattr(bot,'thumbnails',None)
    return await composer.prepare(guild_id,members) if composer else PreparedThumbnails()
