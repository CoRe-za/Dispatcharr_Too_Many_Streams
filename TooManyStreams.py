#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import os
import threading

from apps.channels.models import Channel, ChannelStream, Stream
from apps.proxy.live_proxy.server import ProxyServer
from apps.proxy.live_proxy.services.channel_service import ChannelService
from core.utils import RedisClient

from .TooManyStreamsConfig import TooManyStreamsConfig
from .exceptions import TMS_CustomStreamNotFound
from .StreamServer import StreamServer

logger = logging.getLogger('plugins.too_many_streams.TooManyStreams')
logger.setLevel(os.environ.get("TMS_LOG_LEVEL", os.environ.get("DISPATCHARR_LOG_LEVEL", "INFO")).upper())

class TooManyStreams:

    STREAM_NAME = 'TooManyStreams'
    TMS_MAXED_TTL_SEC = 30

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
            if not ChannelStream.objects.filter(channel_id=channel_id, stream_id=custom_stream.id).exists():
                ChannelStream.objects.create(channel_id=channel_id, stream_id=custom_stream.id, order=9999)
                logger.info(f"Added TMS stream to channel {channel_id}")
        except Exception as e:
            logger.error(f"Failed to add TMS stream to channel {channel_id}: {e}")

    @staticmethod
    def remove_stream_from_channel(channel_id:int) -> None:
        custom_stream = TooManyStreams.get_or_create_stream()
        try:
            deleted, _ = ChannelStream.objects.filter(channel_id=channel_id, stream_id=custom_stream.id).delete()
            if deleted:
                logger.info(f"Removed TMS stream from channel {channel_id}")
                channel = Channel.objects.get(id=channel_id)
                proxy_server = ProxyServer.get_instance()
                ChannelService.stop_channel(str(channel.uuid))
                proxy_server.stop_channel(channel.uuid)
        except Exception as e:
            logger.error(f"Failed to remove TMS stream from channel {channel_id}: {e}")

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
        except:
            val = 0
        return val >= 1

    @staticmethod
    def trigger_refresh():
        TooManyStreams.REFRESH_SIGNAL.set()

    @staticmethod
    def cleanup_channel_streams():
        try:
            stream = TooManyStreams.get_stream()
        except TMS_CustomStreamNotFound:
            return
        deleted, _ = ChannelStream.objects.filter(stream_id=stream.id).delete()
        if deleted:
            logger.info(f"Cleaned up {deleted} ChannelStream entries for TMS stream {stream.id}")

    @staticmethod
    def _extract_channel_uuid(args):
        if not args:
            return None
        a = args[0]
        if isinstance(a, str) and len(a) > 30:
            return a
        if hasattr(a, 'uuid'):
            return str(a.uuid)
        if hasattr(a, 'id'):
            try:
                ch = Channel.objects.get(id=int(a))
                return str(ch.uuid)
            except Exception:
                pass
        return None

    @staticmethod
    def _are_channel_profiles_maxed(channel_uuid):
        try:
            channel = Channel.objects.get(uuid=channel_uuid)
        except Exception:
            return False
        if not channel.streams.exists():
            return False
        redis_client = RedisClient.get_client()
        has_maxed = False
        for stream in channel.streams.all().order_by("channelstream__order"):
            m3u_account = stream.m3u_account
            if not m3u_account:
                continue
            profiles = m3u_account.profiles.all()
            sorted_profiles = sorted(profiles, key=lambda x: not x.is_default)
            for profile in sorted_profiles:
                if not profile.is_active:
                    continue
                key = f"profile_connections:{profile.id}"
                curr = int(redis_client.get(key) or 0)
                if profile.max_streams == 0 or curr < profile.max_streams:
                    return False
                else:
                    has_maxed = True
        return has_maxed

    @staticmethod
    def install_url_utils_override():
        """Patch generate_stream_url across all live_proxy modules to redirect maxed channels."""
        try:
            from apps.proxy.live_proxy import url_utils as uu
            import sys
            import functools

            tms_url = TooManyStreamsConfig.get_stream_url()
            original_fn = uu.generate_stream_url

            @functools.wraps(original_fn)
            def wrapped_generate_stream_url(*args, **kwargs):
                cid = TooManyStreams._extract_channel_uuid(args)
                if cid and TooManyStreams._are_channel_profiles_maxed(cid):
                    TooManyStreams.mark_streams_maxed(cid)
                    logger.info(f"generate_stream_url: channel {cid} all profiles maxed -> TMS")
                    return (tms_url, "TooManyStreams", False, 3)
                return original_fn(*args, **kwargs)

            uu.generate_stream_url = wrapped_generate_stream_url

            patched_count = 0
            for mod_name, mod in list(sys.modules.items()):
                if not mod_name.startswith("apps.proxy.live_proxy"):
                    continue
                if getattr(mod, "generate_stream_url", None) is original_fn:
                    setattr(mod, "generate_stream_url", wrapped_generate_stream_url)
                    patched_count += 1

            logger.info(f"Patched generate_stream_url in {patched_count} live_proxy module(s) -> TMS redirect")
        except Exception as e:
            logger.error(f"install_url_utils_override failed: {e}", exc_info=True)

    @staticmethod
    def install_channel_get_stream_override():
        """Patch Channel.get_stream to redirect to TMS stream when all profiles are maxed."""
        try:
            from apps.channels.models import Channel
            if getattr(Channel, "_tms_override", False):
                return
            if not hasattr(Channel, "get_stream"):
                return
            Channel._tms_override = True

            original_get_stream = Channel.get_stream

            def wrapped_get_stream(self, *args, **kwargs):
                redis_client = RedisClient.get_client()
                has_streams_but_maxed = False

                if not self.streams.exists():
                    return original_get_stream(self, *args, **kwargs)

                for stream in self.streams.all().order_by("channelstream__order"):
                    m3u_account = stream.m3u_account
                    if not m3u_account:
                        continue
                    profiles = m3u_account.profiles.all()
                    sorted_profiles = sorted(profiles, key=lambda x: not x.is_default)
                    for profile in sorted_profiles:
                        if not profile.is_active:
                            continue
                        key = f"profile_connections:{profile.id}"
                        curr = int(redis_client.get(key) or 0)
                        if profile.max_streams == 0 or curr < profile.max_streams:
                            return original_get_stream(self, *args, **kwargs)
                        else:
                            has_streams_but_maxed = True

                if has_streams_but_maxed:
                    TooManyStreams.mark_streams_maxed(str(self.uuid))
                    logger.info(f"Channel {self.uuid} all profiles maxed - returning TMS stream")
                    try:
                        tms_stream = TooManyStreams.get_or_create_stream()
                        return (tms_stream.id, None, None)
                    except Exception as e:
                        logger.error(f"Failed to get/create TMS stream for maxed channel: {e}")

                return original_get_stream(self, *args, **kwargs)

            Channel.get_stream = wrapped_get_stream
            logger.info("Patched Channel.get_stream -> TMS redirect when maxed")
        except Exception as e:
            logger.error(f"install_channel_get_stream_override failed: {e}")

    @staticmethod
    def apply_to_all_channels():
        custom_stream = TooManyStreams.get_or_create_stream()
        existing = set(ChannelStream.objects.filter(stream_id=custom_stream.id).values_list('channel_id', flat=True))
        all_ids = set(Channel.objects.values_list('id', flat=True))
        to_create = [ChannelStream(channel_id=cid, stream_id=custom_stream.id, order=9999) for cid in all_ids - existing]
        if to_create:
            ChannelStream.objects.bulk_create(to_create)
            logger.info(f"Applied TMS stream to {len(to_create)} channels")
        else:
            logger.info("TMS stream already applied to all channels")

    @staticmethod
    def remove_from_all_channels():
        custom_stream = TooManyStreams.get_or_create_stream()
        affected = list(ChannelStream.objects.filter(stream_id=custom_stream.id).values_list('channel_id', flat=True))
        if not affected:
            logger.info("No channels have TMS stream applied")
            return
        ChannelStream.objects.filter(stream_id=custom_stream.id).delete()
        logger.info(f"Removed TMS stream from {len(affected)} channels")
        for cid in affected:
            try:
                channel = Channel.objects.get(id=cid)
                proxy_server = ProxyServer.get_instance()
                ChannelService.stop_channel(str(channel.uuid))
                proxy_server.stop_channel(channel.uuid)
            except Exception as e:
                logger.error(f"Failed to stop channel {cid}: {e}")

    @staticmethod
    def stream_still_mpegts_http_thread(image_path=None, host="127.0.0.1", port=8081):
        server = StreamServer(host=host, port=port, image_path=image_path, refresh_signal=TooManyStreams.REFRESH_SIGNAL)
        server.start()
