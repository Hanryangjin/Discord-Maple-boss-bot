"""Stable job SD face (or common placeholder) + Discord avatar attachment."""
from collections import OrderedDict
from dataclasses import dataclass, field
import asyncio
import hashlib
import io
import logging
import os
from pathlib import Path
import time

import discord
from PIL import Image, ImageDraw, ImageOps
from .cards import avatar_url

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


def placeholder_avatar():
    image = Image.new('RGBA', (112,112), (80,100,130,255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((36,20,76,60), fill=(230,235,242,255))
    draw.ellipse((16,66,96,142), fill=(230,235,242,255))
    return image


def compose(icon_data: bytes, avatar_data: bytes | None) -> bytes:
    character = ImageOps.contain(decode(icon_data), (64,64), method=Image.Resampling.LANCZOS)
    try:
        avatar = decode(avatar_data) if avatar_data else placeholder_avatar()
    except (OSError, ValueError, Image.DecompressionBombError):
        avatar = placeholder_avatar()
    avatar = ImageOps.fit(avatar, (112,112), method=Image.Resampling.LANCZOS)
    mask = Image.new('L', (448,448), 0)
    ImageDraw.Draw(mask).ellipse((0,0,447,447), fill=255)
    mask = mask.resize((112,112), Image.Resampling.LANCZOS)
    # Multiplication preserves transparency already present in the avatar.
    from PIL import ImageChops
    avatar.putalpha(ImageChops.multiply(avatar.getchannel('A'), mask))
    combined = Image.new('RGBA', (200,128), (0,0,0,0))
    combined.alpha_composite(character, (8+(64-character.width)//2,120-character.height))
    combined.alpha_composite(avatar, (80,8))
    output = io.BytesIO()
    combined.save(output, format='PNG')
    return output.getvalue()


@dataclass
class PreparedThumbnails:
    urls: dict[int,str] = field(default_factory=dict)
    files: list[discord.File] = field(default_factory=list)

    def close(self):
        for file in self.files:
            file.close()


class ThumbnailComposer:
    def __init__(self, bot, icon_dir=None, *, reader=None):
        self.bot = bot
        self.root = Path(icon_dir or os.getenv('MAPLEBOT_JOB_ICONS','assets/jobs')).resolve()
        from .job_icons import JobIconStore
        self.icons = JobIconStore(self.root)
        self.reader = reader or bot.http.get_from_cdn
        self.avatar_cache = OrderedDict()
        self.image_cache = OrderedDict()
        self.pending_avatars = {}
        self.semaphore = asyncio.Semaphore(4)

    async def avatar(self, url):
        cached = self.avatar_cache.get(url)
        if cached and cached[0] > time.monotonic():
            self.avatar_cache.move_to_end(url)
            return cached[1]
        task = self.pending_avatars.get(url)
        if task is None:
            task = asyncio.create_task(self.fetch_avatar(url))
            self.pending_avatars[url] = task
            task.add_done_callback(lambda completed: self.pending_avatars.pop(url, None))
        return await asyncio.shield(task)

    async def fetch_avatar(self, url):
        try:
            async with self.semaphore:
                data = await asyncio.wait_for(self.reader(url), timeout=4)
                if not data or len(data) > MAX_BYTES:
                    raise ValueError('avatar byte limit')
                def verify():
                    image=decode(data)
                    image.close()
                await asyncio.to_thread(verify)
            lifetime = 3600
        except Exception as error:
            log.warning('avatar download fallback: %s', type(error).__name__)
            data, lifetime = None, 30
        self.avatar_cache[url] = (time.monotonic()+lifetime, data)
        # Bound both cache item count and memory; keys are avatar URLs, never tokens.
        while len(self.avatar_cache) > 64 or sum(len(v[1] or b'') for v in self.avatar_cache.values()) > 8*1024*1024:
            self.avatar_cache.popitem(last=False)
        return data

    async def one(self, guild_id, member):
        try:
            icon = await asyncio.to_thread(self.icons.face, guild_id, member)
            url = avatar_url(self.bot, guild_id, member['owner_id'])
            avatar = await self.avatar(url)
            digest = hashlib.sha256(b'composition-v1\0'+icon+b'\0'+(avatar or b'fallback-avatar')).hexdigest()
            image = self.image_cache.get(digest)
            if image is None:
                async with self.semaphore:
                    image = await asyncio.to_thread(compose, icon, avatar)
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
        pending=list(self.pending_avatars.values())
        for task in pending:task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        self.pending_avatars.clear()
        self.avatar_cache.clear()
        self.image_cache.clear()


async def prepare_thumbnails(bot, guild_id, members):
    composer = getattr(bot,'thumbnails',None)
    return await composer.prepare(guild_id,members) if composer else PreparedThumbnails()
