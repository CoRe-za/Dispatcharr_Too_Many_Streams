import json
import logging
import os
import re
import requests
import textwrap
import time
import uuid as uuid_mod
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from hashlib import md5

from apps.channels.models import Channel
from core.utils import RedisClient

from .TooManyStreamsConfig import TooManyStreamsConfig

try:
    from apps.epg.models import ProgramData
    from django.utils import timezone
except ImportError:
    ProgramData = None
    timezone = None

DEFAULT_OUT_FILE = "/tmp/too_many_streams2.jpg"
CACHE_DIR = "/tmp/tms_logos"

class PillowImageGen:
    """
    Generates a 1920x1080 JPG image of active streams using Pillow.
    Optimized for low CPU usage with reliable state detection.
    """
    
    _last_active_uuids = None

    def __init__(
        self,
        out_path: str = DEFAULT_OUT_FILE,
    ):
        config = TooManyStreamsConfig.get_config()
        self.title = config.stream_title
        self.description = config.stream_description
        self.html_cols = max(1, int(config.stream_channel_cols))
        self.out_path = out_path
        self.active_streams: list[dict] = []
        self._current_uuids = []

        self.logger = logging.getLogger("plugins.too_many_streams.PillowImageGen")
        self.logger.setLevel(config.tms_log_level)
        
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _get_cached_logo(self, url: str) -> Image.Image:
        if not url: return None
        hashed_url = md5(url.encode()).hexdigest()
        cache_path = os.path.join(CACHE_DIR, hashed_url)
        
        if os.path.exists(cache_path) and (time.time() - os.path.getmtime(cache_path) < 3600):
            try:
                return Image.open(cache_path).convert("RGBA")
            except Exception: pass

        try:
            resp = requests.get(url, timeout=3)
            if resp.status_code == 200:
                with open(cache_path, "wb") as f:
                    f.write(resp.content)
                return Image.open(BytesIO(resp.content)).convert("RGBA")
        except Exception: pass
        return None

    def get_active_streams(self) -> bool:
        """
        Fetches active streams and populates self.active_streams.
        Returns: True if the list of streams has changed since last generation.
        """
        try:
            redis_client = RedisClient.get_client()
            keys = redis_client.keys("live:channel:*:metadata")
            self.logger.info(f"KEYS live:channel:*:metadata returned {len(keys)} keys: {keys}")
            active_uuids = []

            for key in keys:
                try:
                    key_str = key.decode("utf-8") if isinstance(key, bytes) else str(key)
                    m = re.search(r"live:channel:(.*):metadata", key_str)
                    if m:
                        raw = m.group(1)
                        uuid_mod.UUID(hex=raw)
                        active_uuids.append(raw)
                except: continue

            active_uuids.sort()
            self._current_uuids = active_uuids

            if not active_uuids:
                self.active_streams = []
            else:
                channels = Channel.objects.filter(uuid__in=active_uuids).select_related('epg_data').only('id', 'name', 'logo', 'uuid', 'epg_data')
                self.logger.info(f"DB query returned {channels.count()} channels for {len(active_uuids)} UUIDs: {active_uuids}")
                active_list = []
                tms_url = TooManyStreamsConfig.get_stream_url()

                for ch in channels:
                    self.logger.info(f"Processing ch uuid={ch.uuid} id={ch.id} name={ch.name}")
                    try:
                        meta = redis_client.hgetall(f"live:channel:{ch.uuid}:metadata")
                        raw_url = meta.get(b"url", "") or meta.get("url", "")
                        ch_url = raw_url.decode("utf-8") if isinstance(raw_url, bytes) else raw_url
                        self.logger.info(f"  meta url={ch_url} tms_url={tms_url} skip={ch_url == tms_url}")
                        if ch_url == tms_url:
                            continue
                    except Exception as e:
                        self.logger.info(f"  metadata error: {e}")

                    current_show = None
                    next_show = None
                    if ProgramData is not None and getattr(ch, 'epg_data', None) is not None:
                        try:
                            now = timezone.now()
                            current_show = ProgramData.objects.filter(
                                epg=ch.epg_data, start_time__lte=now, end_time__gte=now
                            ).order_by('-start_time').first()
                            next_show = ProgramData.objects.filter(
                                epg=ch.epg_data, start_time__gt=now
                            ).order_by('start_time').first()
                        except Exception:
                            pass
                    else:
                        current_show = None
                        next_show = None

                    active_list.append({
                        'num': f"#{ch.id}",
                        'logo_url': ch.logo.url if ch.logo else "",
                        'name': ch.name,
                        'current_title': current_show.title if current_show else None,
                        'current_start': current_show.start_time if current_show else None,
                        'current_stop': current_show.end_time if current_show else None,
                        'next_title': next_show.title if next_show else None,
                    })
                
                def channel_sort_key(item):
                    num_str = item['num'].lstrip("#")
                    return int(num_str) if num_str.isdigit() else 999999
                
                active_list.sort(key=channel_sort_key)
                self.active_streams = active_list[:15] 

            self.logger.info(f"current_uuids={self._current_uuids} last_uuids={PillowImageGen._last_active_uuids} changed={self._current_uuids != PillowImageGen._last_active_uuids}")
            has_changed = self._current_uuids != PillowImageGen._last_active_uuids
            return has_changed
            
        except Exception as e:
            self.logger.error("Error in get_active_streams", exc_info=True)
            return True # Force generation on error to be safe

    def _hex_to_rgb(self, hex_color: str, default: tuple) -> tuple:
        try:
            hex_color = hex_color.lstrip('#')
            return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        except Exception:
            return default

    def generate(self, force=False) -> bool:
        """Generates the image. Force=True bypasses the change check."""
        if not force and self._current_uuids == PillowImageGen._last_active_uuids and os.path.exists(self.out_path):
            return False

        width, height = 1920, 1080
        config = TooManyStreamsConfig.get_config()
        
        bg_color = self._hex_to_rgb(config.theme_bg_color, (15, 23, 42))
        title_color = self._hex_to_rgb(config.theme_text_color, (248, 250, 252))
        desc_color = (148, 163, 184) # Keep secondary text static or derive? Let's keep it static for now or add config later.
        
        card_bg = self._hex_to_rgb(config.theme_card_bg_color, (30, 41, 59))
        card_border = self._hex_to_rgb(config.theme_card_border_color, (51, 65, 85))
        
        pill_bg_color = self._hex_to_rgb(config.theme_accent_color, (56, 189, 248))
        pill_text_color = self._hex_to_rgb(config.theme_accent_text_color, (15, 23, 42))
        
        name_color = title_color # Use main text color for channel names
        unavailable_color = (239, 68, 68)
        
        try:
            img = Image.new('RGBA', (width, height), color=bg_color + (255,))
            draw = ImageDraw.Draw(img)

            def load_font(size, bold=False):
                fonts = ["arialbd.ttf", "arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
                for f in fonts:
                    try: return ImageFont.truetype(f, size)
                    except: continue
                return ImageFont.load_default()

            title_font, desc_font = load_font(48, True), load_font(20)
            name_font, pill_font = load_font(22, True), load_font(14, True)

            if not self.active_streams:
                unavailable_text = "This Channel is Unavailable"
                bbox = draw.textbbox((0, 0), unavailable_text, font=title_font)
                draw.text(((width - (bbox[2] - bbox[0])) / 2, (height - (bbox[3] - bbox[1])) / 2), 
                          unavailable_text, font=title_font, fill=unavailable_color)
            else:
                bbox = draw.textbbox((0, 0), self.title, font=title_font)
                draw.text(((width - (bbox[2] - bbox[0])) / 2, 100), self.title, font=title_font, fill=title_color)

                content_width = 1440
                grid_margin = (width - content_width) / 2
                wrapper = textwrap.TextWrapper(width=100)
                desc_lines = wrapper.wrap(text=self.description)
                current_y = 180
                for line in desc_lines:
                    bbox = draw.textbbox((0, 0), line, font=desc_font)
                    draw.text(((width - (bbox[2] - bbox[0])) / 2, current_y), line, font=desc_font, fill=desc_color)
                    current_y += 32

                cols, card_spacing = self.html_cols, 24
                card_w = (content_width - (card_spacing * (cols - 1))) / cols
                card_h, grid_y_start = 220, current_y + 16
                epg_font = load_font(17)
                epg_small_font = load_font(15)
                epg_color = (148, 163, 184)
                epg_dim_color = (100, 115, 136)
                progress_bar_color = pill_bg_color

                for i, ch_data in enumerate(self.active_streams):
                    col, row = i % cols, i // cols
                    x = grid_margin + col * (card_w + card_spacing)
                    y = grid_y_start + row * (card_h + card_spacing)

                    channel_num = ch_data['num']
                    icon_url = ch_data['logo_url']
                    channel_name = ch_data['name']
                    current_title = ch_data.get('current_title')
                    current_start = ch_data.get('current_start')
                    current_stop = ch_data.get('current_stop')
                    next_title = ch_data.get('next_title')

                    draw.rounded_rectangle([x, y, x + card_w, y + card_h], radius=12, fill=card_bg + (255,), outline=card_border + (255,), width=2)

                    px, py = 24, 24
                    pill_text = f"CH {channel_num.replace('#', '')}"
                    p_bbox = draw.textbbox((0, 0), pill_text, font=pill_font)
                    p_w, p_h = (p_bbox[2] - p_bbox[0]) + 24, (p_bbox[3] - p_bbox[1]) + 12
                    draw.rounded_rectangle([x + px, y + py, x + px + p_w, y + py + p_h], radius=6, fill=pill_bg_color + (255,))
                    draw.text((x + px + 12, y + py + 6), pill_text, font=pill_font, fill=pill_text_color)

                    icon_w, icon_h = 100, 64
                    icon_x, icon_y = x + px, y + py + p_h + 14
                    icon = self._get_cached_logo(icon_url)
                    if icon:
                        icon.thumbnail((icon_w, icon_h), Image.Resampling.LANCZOS)
                        draw.rounded_rectangle([icon_x, icon_y, icon_x + icon_w, icon_y + icon_h], radius=8, fill=bg_color + (255,), outline=card_border + (255,), width=1)
                        paste_x = int(icon_x + (icon_w - icon.width) // 2)
                        paste_y = int(icon_y + (icon_h - icon.height) // 2)
                        img.paste(icon, (paste_x, paste_y), icon)
                    else:
                        draw.rectangle([icon_x, icon_y, icon_x + icon_w, icon_y + icon_h], fill=(bg_color + (255,)))

                    name_x, name_y = icon_x + icon_w + 14, icon_y + 4
                    max_name_w = card_w - (px * 2) - icon_w - 18
                    avg_char_w = draw.textbbox((0, 0), "A", font=name_font)[2]
                    chars_per_line = max(1, int(max_name_w / avg_char_w))
                    name_lines = textwrap.wrap(channel_name, width=chars_per_line)
                    for line_idx, line in enumerate(name_lines[:2]):
                        draw.text((name_x, name_y + (line_idx * 28)), line, font=name_font, fill=name_color)
                    name_bottom = name_y + min(len(name_lines), 2) * 28

                    icon_bottom = icon_y + icon_h
                    epg_y = max(icon_bottom, name_bottom) + 10

                    if current_title:
                        now_text = f"Now: {current_title}"
                        draw.text((x + px, epg_y), now_text, font=epg_font, fill=epg_color)

                        if current_start and current_stop:
                            bar_y = epg_y + 22
                            bar_h = 5
                            bar_w = card_w - (px * 2)
                            try:
                                total_sec = (current_stop - current_start).total_seconds()
                                elapsed = (timezone.now() - current_start).total_seconds()
                                fill_ratio = max(0.0, min(1.0, elapsed / total_sec)) if total_sec > 0 else 0.0
                            except Exception:
                                fill_ratio = 0.0
                            draw.rounded_rectangle([x + px, bar_y, x + px + bar_w, bar_y + bar_h], radius=2, fill=card_border + (255,))
                            if fill_ratio > 0.01:
                                fill_w = int(bar_w * fill_ratio)
                                draw.rounded_rectangle([x + px, bar_y, x + px + fill_w, bar_y + bar_h], radius=2, fill=progress_bar_color + (255,))
                            next_y = bar_y + bar_h + 4
                        else:
                            next_y = epg_y + 22
                    else:
                        next_y = epg_y

                    if next_title:
                        next_text = f"Up Next: {next_title}"
                        draw.text((x + px, next_y), next_text, font=epg_small_font, fill=epg_dim_color)

            final_img = img.convert("RGB")
            os.makedirs(os.path.dirname(os.path.abspath(self.out_path)) or ".", exist_ok=True)
            final_img.save(self.out_path, "JPEG", quality=92)
            PillowImageGen._last_active_uuids = self._current_uuids
            return True
        except Exception as e:
            self.logger.error("Generation failed", exc_info=True)
            return False
