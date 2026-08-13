"""Contract tests for the Dispatcharr v0.29 URL-resolver integration."""

import importlib
import sys
import types
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class _Query:
    def __init__(self, value=None):
        self.value = value

    def first(self):
        return self.value

    def delete(self):
        return 0, {}


class _Manager:
    def __init__(self, value=None):
        self.value = value

    def filter(self, **_kwargs):
        return _Query(self.value)


class DispatcharrV029ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native_profile = object()
        cls.proxy_profile = types.SimpleNamespace(id=7)

        class Channel:
            def get_stream_profile(self):
                return cls.native_profile

        cls.channel = Channel()
        cls.channel.uuid = "channel-uuid"
        cls.channel.pk = 1
        Channel.objects = _Manager(cls.channel)
        cls.Channel = Channel

        class Stream:
            objects = _Manager()

        class ChannelStream:
            objects = _Manager()

        class StreamProfile:
            objects = _Manager(cls.proxy_profile)

        modules = {
            "too_many_streams": types.ModuleType("too_many_streams"),
            "too_many_streams.src": types.ModuleType("too_many_streams.src"),
            "apps": types.ModuleType("apps"),
            "apps.channels": types.ModuleType("apps.channels"),
            "apps.channels.models": types.ModuleType("apps.channels.models"),
            "apps.proxy": types.ModuleType("apps.proxy"),
            "apps.proxy.live_proxy": types.ModuleType("apps.proxy.live_proxy"),
            "apps.proxy.live_proxy.url_utils": types.ModuleType(
                "apps.proxy.live_proxy.url_utils"
            ),
            "apps.proxy.live_proxy.views": types.ModuleType(
                "apps.proxy.live_proxy.views"
            ),
            "core": types.ModuleType("core"),
            "core.models": types.ModuleType("core.models"),
            "too_many_streams.src.StreamServer": types.ModuleType(
                "too_many_streams.src.StreamServer"
            ),
            "too_many_streams.src.TooManyStreamsConfig": types.ModuleType(
                "too_many_streams.src.TooManyStreamsConfig"
            ),
        }
        modules["apps"].__path__ = []
        modules["apps.channels"].__path__ = []
        modules["apps.proxy"].__path__ = []
        modules["apps.proxy.live_proxy"].__path__ = []
        modules["core"].__path__ = []
        modules["too_many_streams"].__path__ = [str(PLUGIN_ROOT)]
        modules["too_many_streams.src"].__path__ = [str(PLUGIN_ROOT / "src")]

        channel_models = modules["apps.channels.models"]
        channel_models.Channel = Channel
        channel_models.ChannelStream = ChannelStream
        channel_models.Stream = Stream

        core_models = modules["core.models"]
        core_models.PROXY_PROFILE_NAME = "Proxy"
        core_models.StreamProfile = StreamProfile

        def native_resolver(_channel_id):
            return cls.native_result

        modules["apps.proxy.live_proxy.url_utils"].generate_stream_url = native_resolver
        modules["apps.proxy.live_proxy.views"].generate_stream_url = native_resolver
        modules["apps.proxy.live_proxy"].url_utils = modules[
            "apps.proxy.live_proxy.url_utils"
        ]
        modules["apps.proxy.live_proxy"].views = modules[
            "apps.proxy.live_proxy.views"
        ]

        modules["too_many_streams.src.StreamServer"].StreamServer = object

        class Config:
            @staticmethod
            def get_stream_url():
                return "http://127.0.0.1:1337/stream.ts"

        modules[
            "too_many_streams.src.TooManyStreamsConfig"
        ].TooManyStreamsConfig = Config

        cls.saved_modules = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)
        target = "too_many_streams.src.TooManyStreams"
        sys.modules.pop(target, None)
        cls.integration = importlib.import_module(target).TooManyStreams
        cls.url_utils = modules["apps.proxy.live_proxy.url_utils"]
        cls.views = modules["apps.proxy.live_proxy.views"]

    @classmethod
    def tearDownClass(cls):
        cls.integration.uninstall_url_resolver_override()
        for name, original in cls.saved_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    def tearDown(self):
        self.integration.uninstall_url_resolver_override()

    def test_non_capacity_result_is_unchanged(self):
        type(self).native_result = (
            "https://provider/stream",
            "ua",
            False,
            2,
            True,
            None,
        )
        self.integration.install_url_resolver_override()

        self.assertEqual(
            self.views.generate_stream_url("channel-uuid"), self.native_result
        )

    def test_capacity_error_becomes_proxy_fallback(self):
        type(self).native_result = (
            None,
            None,
            False,
            None,
            False,
            "All active M3U profiles have reached maximum connection limits",
        )
        self.integration.install_url_resolver_override()

        result = self.views.generate_stream_url("channel-uuid")

        self.assertEqual(result[0], "http://127.0.0.1:1337/stream.ts")
        self.assertEqual(result[3], 7)
        self.assertFalse(result[4])
        self.assertIsNone(result[5])
        self.assertIs(self.channel.get_stream_profile(), self.proxy_profile)
        self.assertIs(self.channel.get_stream_profile(), self.native_profile)

    def test_uninstall_restores_dispatcharr_methods(self):
        type(self).native_result = (
            "https://provider/stream",
            None,
            False,
            2,
            True,
            None,
        )
        original_resolver = self.url_utils.generate_stream_url
        original_profile_method = self.Channel.get_stream_profile
        self.integration.install_url_resolver_override()

        self.integration.uninstall_url_resolver_override()

        self.assertIs(self.url_utils.generate_stream_url, original_resolver)
        self.assertIs(self.views.generate_stream_url, original_resolver)
        self.assertIs(self.Channel.get_stream_profile, original_profile_method)


if __name__ == "__main__":
    unittest.main()
