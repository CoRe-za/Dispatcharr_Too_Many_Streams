import logging
import os
import shutil
import subprocess
import threading
import time
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from django.db import close_old_connections

from .PillowImageGen import PillowImageGen
from .TooManyStreamsConfig import TooManyStreamsConfig

logger = logging.getLogger('plugins.too_many_streams.StreamServer')

class StreamServer:
    def __init__(self, host, port, image_path=None, refresh_signal=None):
        self.host = host
        self.port = port
        self.image_path = image_path or os.path.join(os.path.dirname(__file__), "..", "img", "too_many_streams2.jpg")
        self.dynamic_image = not bool(image_path)
        self.refresh_signal = refresh_signal or threading.Event()

        self.process = None
        self.clients = []
        self.clients_lock = threading.Lock()
        # _start_ffmpeg can be reached while restart state is already guarded.
        self.process_lock = threading.RLock()

        self._running = True
        self._httpd = None
        
        # Ensure image directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.image_path)), exist_ok=True)
        
        self.ffmpeg_bin = shutil.which("ffmpeg")
        if not self.ffmpeg_bin:
            logger.error("FFmpeg not found! StreamServer cannot start.")

    def _get_ffmpeg_cmd(self, img_path):
        config = TooManyStreamsConfig.get_config()
        encoder = config.video_encoder or "libx264"
        
        cmd = [
            self.ffmpeg_bin, 
            "-loop", "1", 
            "-framerate", "1", 
            "-i", img_path,
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-c:v", encoder,
        ]
        
        # Add encoder-specific flags
        if "nvenc" in encoder:
            cmd.extend(["-preset", "p1", "-tune", "ull"])
        elif "qsv" in encoder:
             cmd.extend(["-preset", "veryfast"])
        else:
             cmd.extend(["-preset", "ultrafast", "-tune", "stillimage"])

        cmd.extend([
            "-r", "1", 
            "-g", "1",
            "-b:v", "800k", 
            "-c:a", "aac", 
            "-b:a", "96k", 
            "-f", "mpegts", 
            "pipe:1"
        ])
        
        return cmd

    def _start_ffmpeg(self):
        with self.process_lock:
            if self.process:
                try:
                    if self.process.poll() is None:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                except Exception as e:
                    logger.warning(f"Error terminating FFmpeg: {e}")
            
            if not self._running:
                return

            if not os.path.exists(self.image_path) and self.dynamic_image:
                 try:
                     PillowImageGen(out_path=self.image_path).generate(force=True)
                 except Exception as e:
                     logger.error(f"Failed to generate initial image: {e}")

            if not os.path.isfile(self.image_path):
                logger.error("Fallback image does not exist: %s", self.image_path)
                self.process = None
                return

            cmd = self._get_ffmpeg_cmd(self.image_path)
            try:
                self.process = subprocess.Popen(
                    cmd, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.DEVNULL
                )
            except Exception as e:
                logger.error(f"Failed to start FFmpeg: {e}")
                self.process = None

    def _image_updater_loop(self):
        logger.info("Starting Image Updater loop")
        if not self.dynamic_image:
            logger.info("Using configured static fallback image: %s", self.image_path)
            return
        try:
            PillowImageGen(out_path=self.image_path).generate()
        except Exception:
            logger.exception("Failed to generate initial fallback image")
        finally:
            close_old_connections()

        while self._running:
            signaled = self.refresh_signal.wait(timeout=60)
            self.refresh_signal.clear()
            
            if not self._running:
                break

            if signaled:
                time.sleep(2) # Buffer for DB consistency
            
            try:
                close_old_connections()
                gen = PillowImageGen(out_path=self.image_path)
                if gen.get_active_streams() or signaled:
                    if gen.generate():
                        logger.info("Image updated, restarting FFmpeg stream.")
                        self._start_ffmpeg()
            except Exception as e:
                logger.error(f"Image update failed: {e}")
            finally:
                close_old_connections()

    def _broadcaster_loop(self):
        logger.info("Starting Broadcaster loop")
        while self._running:
            proc = self.process
            
            if not proc or not proc.stdout or proc.stdout.closed:
                if not self._running: break
                time.sleep(0.5)
                if self.process is None:
                    self._start_ffmpeg()
                continue
            
            try:
                buf = proc.stdout.read(1316 * 16)
                if not buf:
                    if proc.poll() is not None:
                        if self.process == proc and self._running:
                            logger.warning("FFmpeg process exited. Restarting.")
                            self._start_ffmpeg()
                    time.sleep(0.1)
                    continue
                
                with self.clients_lock:
                    clients = self.clients[:]
                for q in clients:
                    try:
                        q.put_nowait(buf)
                    except queue.Full:
                        # Keep slow clients near live output instead of making
                        # them play an ever-growing stale backlog.
                        try:
                            q.get_nowait()
                            q.put_nowait(buf)
                        except (queue.Empty, queue.Full):
                            pass
            except Exception as e:
                if self._running:
                    logger.error(f"Broadcaster error: {e}")
                time.sleep(1)

    def stop(self):
        """Stops the server and all background processes."""
        logger.info("Stopping StreamServer...")
        self._running = False
        self.refresh_signal.set() # Wake up updater

        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception as e:
                logger.error(f"Error shutting down HTTP server: {e}")

        with self.process_lock:
            if self.process:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=2)
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        logger.debug("Could not kill FFmpeg process", exc_info=True)
                self.process = None

        with self.clients_lock:
            self.clients = []

    def start(self):
        if not self.ffmpeg_bin:
            return

        self._start_ffmpeg()

        threading.Thread(target=self._image_updater_loop, daemon=True, name="TMS_ImageUpdater").start()
        threading.Thread(target=self._broadcaster_loop, daemon=True, name="TMS_Broadcaster").start()

        server_instance = self

        class StreamHTTPHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/", "/stream.ts"):
                    self.send_response(404)
                    self.end_headers()
                    return

                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Connection", "keep-alive")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                q = queue.Queue(maxsize=50)
                with server_instance.clients_lock:
                    server_instance.clients.append(q)
                
                server_instance.refresh_signal.set()

                try:
                    while server_instance._running:
                        try:
                            chunk = q.get(timeout=1.0)
                            self.wfile.write(chunk)
                        except queue.Empty:
                            continue
                except (ConnectionResetError, BrokenPipeError):
                    pass
                except Exception:
                    logger.debug("Fallback stream client disconnected", exc_info=True)
                finally:
                    with server_instance.clients_lock:
                        if q in server_instance.clients:
                            server_instance.clients.remove(q)

            def log_message(self, format, *args):
                pass

        logger.info(f"Starting TooManyStreams HTTP Server on {self.host}:{self.port}")
        ThreadingHTTPServer.allow_reuse_address = True
        self._httpd = ThreadingHTTPServer((self.host, self.port), StreamHTTPHandler)
        try:
            self._httpd.serve_forever()
        except Exception as e:
            if self._running:
                logger.error(f"HTTP Server crashed: {e}")
        finally:
            close_old_connections()
