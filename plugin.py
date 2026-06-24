# plugin.py
"""Too Many Streams Plugin for Dispatcharr"""
import fcntl
import logging
import os
import threading

from .TooManyStreams import TooManyStreams
from .TooManyStreamsConfig import TooManyStreamsConfig

logger = logging.getLogger('plugins.too_many_streams')

_TMS_LOCK_PATH = "/tmp/tms-stream-server.lock"
_tms_lock_fh = None


class Plugin:
    name = "too_many_streams"
    version = "3.1.0"
    description = "Handles scenarios where too many streams are open and what users see."
    author = "CoRe-za"
    min_dispatcharr_version = "v0.23.0"
    initialized = False

    fields = [
        {
            "id": "stream_title",
            "label": "Stream Title",
            "type": "string",
            "default": "Sorry, this channel is unavailable.",
            "placeholder": "The title displayed on the 'Too Many Streams' image.",
            "help_text": "The title displayed on the 'Too Many Streams' image.",
        },
        {
            "id": "stream_description",
            "label": "Stream Description",
            "type": "string",
            "default": "While this channel is not currently available, here are some other channels you can watch.",
            "placeholder": "The description displayed on the 'Too Many Streams' image.",
            "help_text": "The description displayed on the 'Too Many Streams' image.",
        },
        {
            "id": "stream_channel_cols",
            "label": "Number of channel columns",
            "type": "number",
            "default": 5,
            "placeholder": "The number of columns of channels to display on the 'Too Many Streams' image.",
            "help_text": "The number of columns of channels to display on the 'Too Many Streams' image.",
        },
        {
            "id": "tms_image_path",
            "label": "Static Image Path",
            "type": "string",
            "default": "",
            "placeholder": "Path to a static image to use instead of the dynamic image.",
            "help_text": "Path to a static image to use instead of the dynamic image.",
        },
        {
            "id": "tms_log_level",
            "label": "Log Level",
            "type": "string",
            "default": "INFO",
            "placeholder": "Log level for the plugin.",
            "help_text": "Log level for the plugin.",
        },
        {
            "id": "video_encoder",
            "label": "Video Encoder",
            "type": "string",
            "default": "libx264",
            "placeholder": "libx264",
            "help_text": "FFmpeg encoder (e.g., libx264, h264_nvenc, h264_qsv, h264_omx, h264_videotoolbox). Use libx264 if unsure.",
        },
        {
            "id": "theme_bg_color",
            "label": "Background Color",
            "type": "string",
            "default": "#0F172A",
            "placeholder": "#0F172A",
            "help_text": "Hex code for the main background.",
        },
        {
            "id": "theme_card_bg_color",
            "label": "Card Background Color",
            "type": "string",
            "default": "#1E293B",
            "placeholder": "#1E293B",
            "help_text": "Hex code for the channel card background.",
        },
        {
            "id": "theme_card_border_color",
            "label": "Card Border Color",
            "type": "string",
            "default": "#334155",
            "placeholder": "#334155",
            "help_text": "Hex code for the card border.",
        },
        {
            "id": "theme_text_color",
            "label": "Text Color",
            "type": "string",
            "default": "#F8FAFC",
            "placeholder": "#F8FAFC",
            "help_text": "Hex code for the main text.",
        },
        {
            "id": "theme_accent_color",
            "label": "Accent Color",
            "type": "string",
            "default": "#38BDF8",
            "placeholder": "#38BDF8",
            "help_text": "Hex code for accents (e.g., channel number pill).",
        },
        {
            "id": "theme_accent_text_color",
            "label": "Accent Text Color",
            "type": "string",
            "default": "#0F172A",
            "placeholder": "#0F172A",
            "help_text": "Hex code for text inside accent pills.",
        },
    ]

    actions = [
        {
            "id": "apply_too_many_streams",
            "label": "Apply 'Too Many Streams' to channels",
            "description": "Adds the 'Too Many Streams' stream to the bottom of all channels.",
            "button_label": "Apply",
            "button_variant": "filled",
            "button_color": "green",
            "confirm": {
                "required": True,
                "title": "Apply 'Too Many Streams'?",
                "message": "This adds the 'Too Many Streams' stream to the bottom of all channels.",
            },
        },
        {
            "id": "remove_too_many_streams",
            "label": "Remove 'Too Many Streams' from channels",
            "description": "Removes the 'Too Many Streams' stream from all channels.",
            "button_label": "Remove",
            "button_variant": "filled",
            "button_color": "red",
            "confirm": {
                "required": True,
                "title": "Remove 'Too Many Streams'?",
                "message": "Removes the 'Too Many Streams' stream from all channels.",
            },
        },
        {
            "id": "save_plugin_config",
            "label": "Save Plugin Config",
            "description": "Saves the current plugin configuration to persistent storage.",
            "button_label": "Save Config",
            "button_variant": "outline",
            "button_color": "blue",
            "confirm": {
                "required": True,
                "title": "Save Plugin Config to disk?",
                "message": "Saves the current plugin configuration to persistent storage.",
            },
        },
        {
            "id": "search_for_config",
            "label": "Search for Persistent Config",
            "description": "Manually searches for and reloads the persistent configuration file from disk.",
            "button_label": "Reload Config",
            "button_variant": "outline",
            "button_color": "gray",
        },
    ]

    def __init__(self):
        self._server = None
        self._server_thread = None
        self._init_lock = threading.Lock()
        self.initialize()

    def initialize(self):
        if self.initialized:
            return
        with self._init_lock:
            if self.initialized:
                return

            config = TooManyStreamsConfig.get_config()
            logger.setLevel(config.tms_log_level)

            HOST, PORT = TooManyStreamsConfig.get_host_and_port()
            image_to_use = config.tms_image_path

            TooManyStreams.cleanup_channel_streams()
            TooManyStreams.install_channel_get_stream_override()
            TooManyStreams.install_url_utils_override()

            # Use a file lock so only one uWSGI worker starts the stream server
            global _tms_lock_fh
            if _tms_lock_fh is None:
                try:
                    fh = open(_TMS_LOCK_PATH, "w")
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    _tms_lock_fh = fh
                except OSError:
                    logger.info("Another worker holds the stream server lock - skipping server start")
                    self.initialized = True
                    return

            from .StreamServer import StreamServer
            self._server = StreamServer(
                host=HOST,
                port=PORT,
                image_path=image_to_use,
                refresh_signal=TooManyStreams.REFRESH_SIGNAL,
            )
            self._server_thread = threading.Thread(
                target=self._server.start,
                daemon=True,
                name="TMS_StreamServer",
            )
            self._server_thread.start()

            self.initialized = True
            logger.info("Too Many Streams plugin initialized.")

    def run(self, action: str, params: dict, context: dict):
        log = context.get("logger", logger)
        log.info(f"Running action: {action}")

        if action == "apply_too_many_streams":
            TooManyStreams.apply_to_all_channels()
        elif action == "remove_too_many_streams":
            TooManyStreams.remove_from_all_channels()
        elif action == "save_plugin_config":
            settings = context.get("settings") or context.get("config") or params or {}
            log.info(f"Saving settings: {settings}")
            if settings:
                TooManyStreamsConfig.save_plugin_persistent_config(settings)
                TooManyStreams.trigger_refresh()
            else:
                log.warning("No settings found to save.")
        elif action == "search_for_config":
            log.info("Manually searching for and reloading config...")
            TooManyStreamsConfig.clear_cache()
            TooManyStreamsConfig.get_config()
            TooManyStreams.trigger_refresh()

        return {"status": "ok"}

    def stop(self, context: dict):
        log = context.get("logger", logger)
        log.info("Too Many Streams plugin stopping...")
        if self._server is not None:
            try:
                self._server.stop()
            except Exception as e:
                log.error(f"Error stopping stream server: {e}")
        global _tms_lock_fh
        if _tms_lock_fh is not None:
            try:
                fcntl.flock(_tms_lock_fh, fcntl.LOCK_UN)
                _tms_lock_fh.close()
            except Exception:
                pass
            _tms_lock_fh = None
        self.initialized = False
        log.info("Too Many Streams plugin stopped.")
