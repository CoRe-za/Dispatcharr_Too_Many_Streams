#!/usr/bin/env python3
"""Dispatcharr integration for the Too Many Streams fallback stream."""

import logging
import os
import threading
from contextvars import ContextVar
from functools import wraps

from apps.channels.models import Channel, ChannelStream, Stream

from .exceptions import TMS_CustomStreamNotFound
from .StreamServer import StreamServer
from .TooManyStreamsConfig import TooManyStreamsConfig


logger = logging.getLogger("plugins.too_many_streams.TooManyStreams")
logger.setLevel(
    os.environ.get("TMS_LOG_LEVEL", os.environ.get("DISPATCHARR_LOG_LEVEL", "INFO")).upper()
)


class TooManyStreams:
    """Install a small wrapper around Dispatcharr's native URL resolver.

    Dispatcharr owns connection accounting, credential pools, stale assignment
    recovery, and requester priority. The plugin deliberately delegates all of
    that work to the native method and only substitutes its custom stream when
    the native selector reports exhausted connection capacity.
    """

    STREAM_NAME = "TooManyStreams"
    CAPACITY_ERROR_FRAGMENT = "maximum connection limit"
    REFRESH_SIGNAL = threading.Event()
    _server_instance = None
    _stream_create_lock = threading.Lock()
    _fallback_profile_channel = ContextVar(
        "too_many_streams_fallback_profile_channel", default=None
    )

    @staticmethod
    def check_requirements_met() -> bool:
        try:
            import PIL  # noqa: F401

            return True
        except ImportError:
            return False

    @staticmethod
    def install_requirements() -> None:
        """Best-effort dependency installation for legacy/manual installs."""
        try:
            import subprocess

            subprocess.check_call(
                [
                    "pip",
                    "install",
                    "-r",
                    os.path.join(os.path.dirname(__file__), "..", "requirements.txt"),
                ]
            )
            logger.info("TooManyStreams: installed requirements")
        except Exception:
            logger.exception("TooManyStreams: failed to install requirements")

    @staticmethod
    def get_stream() -> Stream:
        stream = Stream.objects.filter(
            name=TooManyStreams.STREAM_NAME,
            url=TooManyStreamsConfig.get_stream_url(),
        ).first()
        if stream is None:
            raise TMS_CustomStreamNotFound("TooManyStreams: stream not found")
        return stream

    @staticmethod
    def get_or_create_stream() -> Stream:
        """Return the shared custom source, creating it when necessary."""
        with TooManyStreams._stream_create_lock:
            try:
                return TooManyStreams.get_stream()
            except TMS_CustomStreamNotFound:
                return Stream.objects.create(
                    name=TooManyStreams.STREAM_NAME,
                    url=TooManyStreamsConfig.get_stream_url(),
                    is_custom=True,
                    channel_group=None,
                    stream_profile_id=None,
                )

    @staticmethod
    def _is_capacity_error(result) -> bool:
        if not isinstance(result, (tuple, list)) or len(result) < 6:
            return False
        stream_url, error_reason = result[0], result[5]
        return (
            stream_url is None
            and isinstance(error_reason, str)
            and TooManyStreams.CAPACITY_ERROR_FRAGMENT in error_reason.lower()
        )

    @staticmethod
    def install_url_resolver_override() -> None:
        """Wrap v0.29's resolver after native connection selection completes."""
        from apps.proxy.live_proxy import url_utils, views
        from core.models import PROXY_PROFILE_NAME, StreamProfile

        if hasattr(url_utils, "_tms_original_generate_stream_url"):
            return

        native_resolver = url_utils.generate_stream_url
        url_utils._tms_original_generate_stream_url = native_resolver

        native_get_stream_profile = Channel.get_stream_profile
        Channel._tms_original_get_stream_profile = native_get_stream_profile

        @wraps(native_get_stream_profile)
        def _wrapped_get_stream_profile(channel, *args, **kwargs):
            channel_uuid = str(channel.uuid)
            use_fallback_profile = (
                TooManyStreams._fallback_profile_channel.get() == channel_uuid
            )
            if use_fallback_profile:
                TooManyStreams._fallback_profile_channel.set(None)
            if use_fallback_profile:
                proxy_profile = StreamProfile.objects.filter(
                    name=PROXY_PROFILE_NAME,
                    locked=True,
                    is_active=True,
                ).first()
                if proxy_profile is not None:
                    return proxy_profile
                logger.warning(
                    "TooManyStreams: built-in Proxy stream profile was not found"
                )
            return native_get_stream_profile(channel, *args, **kwargs)

        @wraps(native_resolver)
        def _wrapped_generate_stream_url(channel_id, *args, **kwargs):
            native_result = native_resolver(channel_id, *args, **kwargs)
            if not TooManyStreams._is_capacity_error(native_result):
                return native_result

            try:
                channel = Channel.objects.filter(uuid=channel_id).first()
                if channel is None:
                    return native_result
                proxy_profile = StreamProfile.objects.filter(
                    name=PROXY_PROFILE_NAME,
                    locked=True,
                    is_active=True,
                ).first()
                if proxy_profile is None:
                    logger.error(
                        "TooManyStreams: built-in Proxy stream profile is unavailable"
                    )
                    return native_result
                TooManyStreams._fallback_profile_channel.set(str(channel.uuid))
                TooManyStreams.trigger_refresh()
                logger.info(
                    "TooManyStreams: channel %s exhausted provider capacity; using fallback",
                    channel.uuid,
                )
                return (
                    TooManyStreamsConfig.get_stream_url(),
                    None,
                    False,
                    proxy_profile.id,
                    False,
                    None,
                )
            except Exception:
                logger.exception(
                    "TooManyStreams: could not select fallback for channel %s",
                    channel_id,
                )
                return native_result

        url_utils.generate_stream_url = _wrapped_generate_stream_url
        Channel.get_stream_profile = _wrapped_get_stream_profile
        # views imports the callable directly, so update that bound reference too.
        views.generate_stream_url = _wrapped_generate_stream_url
        logger.info("TooManyStreams: installed native URL resolver wrapper")

    @staticmethod
    def uninstall_url_resolver_override() -> None:
        from apps.proxy.live_proxy import url_utils, views

        native_resolver = getattr(
            url_utils, "_tms_original_generate_stream_url", None
        )
        if native_resolver is not None:
            url_utils.generate_stream_url = native_resolver
            views.generate_stream_url = native_resolver
            delattr(url_utils, "_tms_original_generate_stream_url")
            native_get_stream_profile = getattr(
                Channel, "_tms_original_get_stream_profile", None
            )
            if native_get_stream_profile is not None:
                Channel.get_stream_profile = native_get_stream_profile
                delattr(Channel, "_tms_original_get_stream_profile")
            TooManyStreams._fallback_profile_channel.set(None)
            logger.info("TooManyStreams: uninstalled URL resolver wrapper")

    @staticmethod
    def trigger_refresh() -> None:
        TooManyStreams.REFRESH_SIGNAL.set()

    @staticmethod
    def apply_to_all_channels() -> int:
        """Attach the custom source at the bottom of every channel."""
        custom_stream = TooManyStreams.get_or_create_stream()
        existing_channel_ids = set(
            ChannelStream.objects.filter(stream=custom_stream).values_list(
                "channel_id", flat=True
            )
        )
        links = [
            ChannelStream(channel_id=channel_id, stream=custom_stream, order=9999)
            for channel_id in Channel.objects.exclude(
                id__in=existing_channel_ids
            ).values_list("id", flat=True)
        ]
        if links:
            ChannelStream.objects.bulk_create(links, ignore_conflicts=True)
        logger.info(
            "TooManyStreams: attached fallback source to %s channels", len(links)
        )
        return len(links)

    @staticmethod
    def remove_from_all_channels() -> int:
        """Detach the custom source without disabling automatic v4 fallback."""
        legacy_streams = Stream.objects.filter(name=TooManyStreams.STREAM_NAME)
        deleted, _ = ChannelStream.objects.filter(stream__in=legacy_streams).delete()
        logger.info(
            "TooManyStreams: removed %s fallback source assignments", deleted
        )
        return deleted

    @staticmethod
    def clean_legacy_assignments() -> int:
        """Remove channel links created by pre-v4 releases; v4 needs none."""
        return TooManyStreams.remove_from_all_channels()

    @staticmethod
    def stop_server() -> None:
        if TooManyStreams._server_instance:
            TooManyStreams._server_instance.stop()
            TooManyStreams._server_instance = None
            logger.info("TooManyStreams: server stopped")

    @staticmethod
    def stream_still_mpegts_http_thread(
        image_path=None, host="127.0.0.1", port=8081
    ) -> None:
        TooManyStreams._server_instance = StreamServer(
            host=host,
            port=port,
            image_path=image_path,
            refresh_signal=TooManyStreams.REFRESH_SIGNAL,
        )
        TooManyStreams._server_instance.start()
