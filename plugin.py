# plugin.py
"""Too Many Streams Plugin for Dispatcharr"""
# -*- coding: utf-8 -*-
import os
import logging
import socket
import threading
import sys
import inspect

# Configure logging as early as possible
logger = logging.getLogger('plugins.too_many_streams')
log_file = os.path.join(os.path.dirname(__file__), "debug.log")
try:
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)
except Exception:
    pass # Fallback to console only if file is unwritable
logger.setLevel(logging.INFO)

# DEFERRED IMPORTS to prevent startup crashes
TooManyStreams = None
TooManyStreamsConfig = None

def _import_plugin():
    global TooManyStreams, TooManyStreamsConfig
    if TooManyStreams is None:
        try:
            from .src.TooManyStreams import TooManyStreams as TMS
            from .src.TooManyStreamsConfig import TooManyStreamsConfig as TMSC
            TooManyStreams = TMS
            TooManyStreamsConfig = TMSC
            logger.info("TMS components imported successfully.")
        except Exception as e:
            logger.error(f"Failed to import plugin components: {e}", exc_info=True)
            raise

class Plugin:
    name = "too_many_streams"
    version = "2.2.0"
    description = "Handles scenarios where too many streams are open and what users see."
    
    # Static fields defined without DB access
    fields = [
        {"id": "stream_title", "label": "Stream Title", "type": "string", "default": "Sorry, this channel is unavailable."},
        {"id": "stream_description", "label": "Stream Description", "type": "string", "default": "While this channel is not currently available, here are some other channels you can watch."},
        {"id": "stream_channel_cols", "label": "Number of channel columns", "type": "number", "default": 5},
        {"id": "tms_image_path", "label": "Static Image Path", "type": "string", "default": ""},
        {"id": "tms_log_level", "label": "Log Level", "type": "string", "default": "INFO"},
    ]

    actions = [
        {"id": "apply_too_many_streams", "label": "Apply to all channels"},
        {"id": "remove_too_many_streams", "label": "Remove from all channels"},
        {"id": "save_plugin_config", "label": "Save Plugin Config"},
    ]

    _initialized = False
    _lock = threading.Lock()

    def __init__(self):
        # DO NOT initialize here. Dispatcharr imports classes early.
        pass

    def ensure_initialized(self):
        with self._lock:
            if self._initialized:
                return
            
            try:
                _import_plugin()
                
                config = TooManyStreamsConfig.get_config()
                logger.setLevel(config.tms_log_level)

                TooManyStreams.install_get_stream_override()

                HOST, PORT = TooManyStreamsConfig.get_host_and_port()
                
                # Start background server
                threading.Thread(
                    target=TooManyStreams.stream_still_mpegts_http_thread,
                    args=(config.tms_image_path,),
                    kwargs={"host": HOST, "port": PORT},
                    daemon=True,
                ).start()
                
                self._initialized = True
                logger.info("Too Many Streams plugin deferred initialization complete.")
            except Exception as e:
                logger.error(f"Initialization failed: {e}", exc_info=True)

    def run(self, action: str = None, params: dict = None, context: dict = None, *args, **kwargs):
        self.ensure_initialized()
        
        logger.info(f"Running action: {action}")
        
        if action == "apply_too_many_streams":
            TooManyStreams.apply_to_all_channels()
        elif action == "remove_too_many_streams":
            TooManyStreams.remove_from_all_channels()
        elif action == "save_plugin_config":
            settings = (context or {}).get("settings") or (context or {}).get("config") or (params or {})
            if settings:
                TooManyStreamsConfig.save_plugin_persistent_config(settings)
                return {"status": "ok", "message": "Settings saved."}
        
        return {"status": "ok"}
