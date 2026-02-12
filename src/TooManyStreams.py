#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import os
import time
import os, shutil, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import queue

from apps.channels.models import Channel, ChannelStream, Stream
from apps.proxy.ts_proxy.server import ProxyServer
from apps.proxy.ts_proxy.services.channel_service import ChannelService
from core.utils import RedisClient

from .TooManyStreamsConfig import TooManyStreamsConfig
from .exceptions import TMS_CustomStreamNotFound
from .PillowImageGen import PillowImageGen


logger = logging.getLogger('plugins.too_many_streams.TooManyStreams')
logger.setLevel(os.environ.get("TMS_LOG_LEVEL", os.environ.get("DISPATCHARR_LOG_LEVEL", "INFO")).upper())

class TooManyStreams:

    STREAM_NAME = 'TooManyStreams'
    TMS_MAXED_TTL_SEC = 30
    TMS_MAXED_COUNTER = 1
    CHUNK = 188 * 7
    
    REFRESH_SIGNAL = threading.Event()

    @staticmethod
    def check_requirements_met() -> bool:
        return True

    @staticmethod
    def install_requirements() -> None:
        try:
            import subprocess
            subprocess.check_call(["pip", "install", "-r", os.path.join(os.path.dirname(__file__), "..", "requirements.txt")])
            logger.info("TooManyStreams: Installed requirements.")
        except Exception as e:
            logger.error(f"TooManyStreams: Failed to install requirements: {e}")

    @staticmethod
    def get_stream() -> Stream:
        stream:dict = Stream.objects.values('id', 'name', 'url').filter(
            name=TooManyStreams.STREAM_NAME, url=TooManyStreamsConfig.get_stream_url())
        if not stream:
            raise TMS_CustomStreamNotFound("TooManyStreams: Stream not found.")
        return Stream.objects.get(id=stream[0]['id'])
    
    @staticmethod
    def get_or_create_stream() -> Stream:
        try:
            return TooManyStreams.get_stream()
        except TMS_CustomStreamNotFound:
            data = {
                'name': TooManyStreams.STREAM_NAME,
                'url': TooManyStreamsConfig.get_stream_url(),
                'is_custom': True,
                'channel_group': None,
                'stream_profile_id': None,
            }
            return Stream.objects.create(**data)

    @staticmethod
    def add_stream_to_channel(channel_id:int) -> None:
        custom_stream = TooManyStreams.get_or_create_stream()
        try:
            channel = Channel.objects.get(id=channel_id)
            if custom_stream not in channel.streams.all():
                ChannelStream.objects.create(channel=channel, stream_id=custom_stream.id, order=9999)
        except Exception: pass

    @staticmethod   
    def remove_stream_from_channel(channel_id:int) -> None:
        custom_stream = TooManyStreams.get_or_create_stream()
        try:
            channel = Channel.objects.get(id=channel_id)
            if custom_stream in channel.streams.all():
                channel.streams.remove(custom_stream.id)
                channel.save()
                proxy_server = ProxyServer.get_instance()
                ChannelService.stop_channel(str(channel.uuid))
                proxy_server.stop_channel(channel.uuid)
        except Exception: pass

    @staticmethod
    def mark_streams_maxed(channel_id) -> None:
        channel_id = str(channel_id)
        redis_client = RedisClient.get_client()
        key = f"tms:maxed_out:{channel_id}"
        redis_client.incr(key)
        redis_client.expire(key, TooManyStreams.TMS_MAXED_TTL_SEC)

    @staticmethod
    def is_streams_maxed(channel_id) -> bool:
        channel_id = str(channel_id)
        redis_client = RedisClient.get_client()
        key = f"tms:maxed_out:{channel_id}"
        try:
            val = int(redis_client.get(key) or 0)
        except: val = 0
        
        is_maxed = val >= TooManyStreams.TMS_MAXED_COUNTER
        if is_maxed: TooManyStreams.add_stream_to_channel(channel_id)
        else: TooManyStreams.remove_stream_from_channel(channel_id)
        return is_maxed

    @staticmethod
    def trigger_refresh():
        TooManyStreams.REFRESH_SIGNAL.set()

    @staticmethod
    def install_get_stream_override():
        from apps.channels.models import Channel 
        if getattr(Channel, "_orig_get_stream", None) is None:
            Channel._orig_get_stream = Channel.get_stream
            
            def _wrapped_get_stream(self, *args, **kwargs):
                redis_client = RedisClient.get_client()
                error_reason = None

                if not self.streams.exists():
                    return None, None, "No streams assigned to channel"

                # 1. Check if a stream is already active for this channel (Restore session logic)
                stream_id_bytes = redis_client.get(f"channel_stream:{self.id}")
                if stream_id_bytes:
                    try:
                        stream_id = int(stream_id_bytes)
                        profile_id_bytes = redis_client.get(f"stream_profile:{stream_id}")
                        if profile_id_bytes:
                            return stream_id, int(profile_id_bytes), None
                    except (ValueError, TypeError): pass

                # 2. Try to find an available stream
                has_streams_but_maxed_out = False
                has_active_profiles = False

                for stream in self.streams.all().order_by("channelstream__order"):
                    m3u_account = stream.m3u_account
                    if not m3u_account: continue

                    profiles = m3u_account.profiles.all()
                    # Ensure default profile is checked first
                    sorted_profiles = sorted(profiles, key=lambda x: not x.is_default)

                    for profile in sorted_profiles:
                        if not profile.is_active: continue
                        has_active_profiles = True

                        profile_connections_key = f"profile_connections:{profile.id}"
                        current_connections = int(redis_client.get(profile_connections_key) or 0)

                        if profile.max_streams == 0 or current_connections < profile.max_streams:
                            redis_client.set(f"channel_stream:{self.id}", stream.id)
                            redis_client.set(f"stream_profile:{stream.id}", profile.id)
                            if profile.max_streams > 0: redis_client.incr(profile_connections_key)
                            
                            TooManyStreams.trigger_refresh()
                            return stream.id, profile.id, None
                        else:
                            has_streams_but_maxed_out = True

                # 3. Handle maxed out scenario
                if has_streams_but_maxed_out:
                    if not TooManyStreams.is_streams_maxed(self.id):
                        TooManyStreams.mark_streams_maxed(self.id)
                        return None, None, "All M3U profiles have reached maximum connection limits"
                    
                    # Return our custom stream
                    try:
                        custom_stream = TooManyStreams.get_stream()
                        return custom_stream.id, None, None
                    except: pass

                error_reason = "No compatible profile found" if has_active_profiles else "No active profiles found"
                return None, None, error_reason

            Channel.get_stream = _wrapped_get_stream

    @staticmethod
    def apply_to_all_channels():
        for c in Channel.objects.all(): TooManyStreams.add_stream_to_channel(c.id)

    @staticmethod
    def remove_from_all_channels():
        for c in Channel.objects.all(): TooManyStreams.remove_stream_from_channel(c.id)

    @staticmethod
    def stream_still_mpegts_http_thread(image_path=None, host="127.0.0.1", port=8081):
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin: return

        class SharedState:
            def __init__(self):
                self.process = None
                self.clients = []
                self.lock = threading.Lock()
                self.current_img = image_path or os.path.join(os.path.dirname(__file__), "too_many_streams2.jpg")

        state = SharedState()

        def make_cmd(img):
            return [
                ffmpeg_bin, "-loop", "1", "-framerate", "1", "-i", img,
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-r", "1", "-g", "1",
                "-b:v", "800k", "-c:a", "aac", "-b:a", "96k", "-f", "mpegts", "pipe:1"
            ]

        def start_ffmpeg():
            if state.process: state.process.terminate()
            state.process = subprocess.Popen(make_cmd(state.current_img), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        start_ffmpeg()

        def image_updater():
            while True:
                signaled = TooManyStreams.REFRESH_SIGNAL.wait(timeout=60)
                TooManyStreams.REFRESH_SIGNAL.clear()
                if signaled: time.sleep(2)
                if not image_path:
                    try:
                        gen = PillowImageGen(out_path=state.current_img)
                        if gen.get_active_streams() or signaled:
                            if gen.generate(): start_ffmpeg()
                    except Exception as e: logger.error(f"Update failed: {e}")

        threading.Thread(target=image_updater, daemon=True).start()

        def broadcaster():
            while True:
                if not state.process or state.process.stdout.closed:
                    time.sleep(0.1); continue
                if not state.clients:
                    time.sleep(1); continue
                buf = state.process.stdout.read(1316 * 16)
                if not buf:
                    if state.process.poll() is not None: start_ffmpeg()
                    time.sleep(0.1); continue
                with state.lock:
                    for q in state.clients[:]:
                        try: q.put_nowait(buf)
                        except queue.Full: pass

        threading.Thread(target=broadcaster, daemon=True).start()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/", "/stream.ts"):
                    self.send_response(404); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = queue.Queue(maxsize=50)
                with state.lock: state.clients.append(q)
                TooManyStreams.trigger_refresh()
                try:
                    while True: self.wfile.write(q.get())
                except: pass
                finally:
                    with state.lock:
                        if q in state.clients: state.clients.remove(q)
            def log_message(self, *args): pass

        httpd = ThreadingHTTPServer((host, port), Handler)
        httpd.serve_forever()
