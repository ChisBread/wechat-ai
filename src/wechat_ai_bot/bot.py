"""
WeChat-AI Bot — Main bot class for Linux Docker container.
Integrates all components: visual message reading, RPA, plugins, MCP, MQTT.
"""

import argparse
import logging
import os
import queue
import signal
import time
import threading
from typing import Any, List

from wechat_ai_bot.common.config import Config
from wechat_ai_bot.common.queues import message_queue, rpa_task_queue
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.plugins.plugin_manager import PluginManager
from wechat_ai_bot.rpa.controller import RPAController
from wechat_ai_bot.rpa.image_processor import ImageProcessor
from wechat_ai_bot.rpa.xfce_window_manager import XFCEWindowManager
from wechat_ai_bot.rpa.ocr_processor import OCRProcessor
from wechat_ai_bot.services.core.message_factory_service import MessageFactoryService
from wechat_ai_bot.services.core.mqtt_service import MQTTService
from wechat_ai_bot.services.core.processor_service import ProcessorService
from wechat_ai_bot.services.core.rpa_service import RPAService
from wechat_ai_bot.services.core.visual_message_service import VisualMessageService
from wechat_ai_bot.services.functional.weixin_status_service import WeixinStatusService
from wechat_ai_bot.utils.helpers import ensure_dir_exists
from wechat_ai_bot.utils.logging_setup import setup_logging


class Bot:
    """WeChat-AI Bot: Linux platform runtime for WeChat AI automation."""

    def __init__(self, config_path: str = "/config/config.yaml"):
        ensure_dir_exists("/config/runtime_images")

        self.config = Config(config_path)
        setup_logging(
            log_dir=self.config.get("logging.path", "/config/logs"),
            log_level=self.config.get("logging.level", logging.INFO),
        )
        self.logger = logging.getLogger(self.__class__.__name__)

        self.is_running = False
        self.chat_window_ready = False
        self.started_at = time.time()
        self._window_init_thread = None
        self._components: List[Any] = []

        # ---- RPA Components ----
        self.image_processor = ImageProcessor()
        self.ocr_processor = OCRProcessor(
            ocr_config=self.config.get("rpa.ocr", {})
        )
        self.window_manager = XFCEWindowManager(
            image_processor=self.image_processor,
            ocr_processor=self.ocr_processor,
            rpa_config=self.config.get("rpa", {}),
        )
        self.rpa_controller = RPAController(
            window_manager=self.window_manager,
            ocr_processor=self.ocr_processor,
            image_processor=self.image_processor,
            rpa_config=self.config.get("rpa", {}),
        )

        # ---- User Info (config-driven on Linux, no process memory dump) ----
        wechat_cfg = self.config.get("wechat_user", {})
        self.user_info = UserInfo(
            account=wechat_cfg.get("account", ""),
            nickname=wechat_cfg.get("nickname", "WeChat-AI"),
            avatar_url=wechat_cfg.get("avatar_url", ""),
            version="4.1.0",  # Linux WeChat version
        )

        # ---- Queues ----
        self.message_queue: queue.Queue = message_queue
        self.rpa_task_queue: queue.Queue = rpa_task_queue

        # ---- Plugin Manager ----
        self.plugin_manager = PluginManager(self)

        # ---- Services ----
        # Message factory (portable)
        self.message_factory_service = MessageFactoryService(self.user_info, None)

        # Processor (portable)
        self.processor_service = ProcessorService(
            user_info=self.user_info,
            message_queue=self.message_queue,
            rpa_task_queue=self.rpa_task_queue,
            db=None,
            message_factory_service=self.message_factory_service,
            plugin_manager=self.plugin_manager,
        )

        # RPA service (portable)
        self.rpa_service = RPAService(self.rpa_task_queue, self.rpa_controller)

        # Visual message service (Linux-native, replaces DatabaseService)
        visual_config = self.config.get("visual_message", {})
        self.visual_message_service = VisualMessageService(
            window_manager=self.window_manager,
            image_processor=self.image_processor,
            ocr_processor=self.ocr_processor,
            message_queue=self.message_queue,
            poll_interval=visual_config.get("poll_interval", 2.0),
            max_message_age=visual_config.get("max_message_age", 300),
            dedup_cache_size=visual_config.get("dedup_cache_size", 100),
        )

        # WeChat status monitor
        weixin_config = self.config.config.get("dingtalk", {})
        self.weixin_status_service = WeixinStatusService(
            config=self.config,
            window_manager=self.window_manager,
            image_processor=self.image_processor,
            ocr_processor=self.ocr_processor,
        )

        # MQTT (optional)
        mqtt_config = self.config.get("mqtt", {})
        self.mqtt_service = None
        if mqtt_config.get("host") and mqtt_config.get("port"):
            self.mqtt_service = MQTTService(
                user_info=self.user_info,
                db=None,
                rpa_task_queue=self.rpa_task_queue,
                mqtt_config=mqtt_config,
            )
            self.logger.info(f"MQTT enabled: {mqtt_config['host']}:{mqtt_config['port']}")
        else:
            self.logger.info("MQTT disabled (no host/port configured)")

        # ---- Component registry (for setup/teardown) ----
        self._components = [
            self.image_processor,
            self.ocr_processor,
            self.plugin_manager,
            self.processor_service,
            self.rpa_service,
            self.visual_message_service,
        ]
        if self.mqtt_service:
            self._components.append(self.mqtt_service)

        self.mcp_app = None

        self.logger.info("=" * 60)
        self.logger.info("WeChat-AI Bot initialized (Linux)")
        self.logger.info(f"  User: {self.user_info.nickname}")
        self.logger.info(f"  MCP port: {os.environ.get('MCP_PORT') or self.config.get('mcp.port', 8000)}")
        self.logger.info(f"  Visual poll interval: {visual_config.get('poll_interval', 2.0)}s")
        self.logger.info("=" * 60)

    def setup(self):
        """Initialize all components. Blocking operations (window init, etc.)."""
        self.logger.info("--- Bot Setup Start ---")

        # Plugin loading
        self.plugin_manager.setup()

        # Start all services
        for component in self._components:
            name = component.__class__.__name__
            try:
                if hasattr(component, "setup"):
                    component.setup()
                    self.logger.info(f"  {name}: setup complete")
            except Exception as e:
                self.logger.error(f"  {name}: setup failed: {e}")

        self.is_running = True
        self._start_window_init_loop()

        for component in self._components:
            name = component.__class__.__name__
            try:
                if hasattr(component, "start"):
                    component.start()
                    self.logger.info(f"  {name}: started")
            except Exception as e:
                self.logger.error(f"  {name}: start failed: {e}")

        self.logger.info("--- Bot Setup Complete ---")

    def _start_window_init_loop(self):
        self._window_init_thread = threading.Thread(
            target=self._window_init_loop,
            daemon=True,
            name="WindowInitLoop",
        )
        self._window_init_thread.start()

    def _window_init_loop(self):
        self.logger.info("Waiting for WeChat chat window...")
        retry = 0
        while self.is_running and not self.chat_window_ready:
            retry += 1
            if self.window_manager.init_chat_window():
                self.chat_window_ready = True
                self.logger.info("Chat window initialized successfully")
                return
            self.logger.warning(f"Chat window init failed, retry {retry}")
            time.sleep(3)

    def start(self):
        """Start the bot. Blocks on MCP server until signal."""
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            self.setup()

            # Start MCP server (blocking)
            self.logger.info("Starting MCP server...")
            from wechat_ai_bot.mcp.app import create_app
            self.mcp_app = create_app(self.user_info, self.config, bot=self)
            self.mcp_app.run("streamable-http")

        except Exception as e:
            self.logger.critical(f"Critical error: {e}", exc_info=True)
        finally:
            self.teardown()

    def teardown(self):
        """Graceful shutdown of all components."""
        self.logger.info("--- Bot Teardown ---")
        self.is_running = False
        if self._window_init_thread and self._window_init_thread.is_alive():
            self._window_init_thread.join(timeout=5)

        for component in reversed(self._components):
            name = component.__class__.__name__
            try:
                if hasattr(component, "stop"):
                    component.stop()
                    self.logger.info(f"  {name}: stopped")
            except Exception as e:
                self.logger.error(f"  {name}: stop error: {e}")

        self.logger.info("--- Bot Teardown Complete ---")

    def _signal_handler(self, sig: int, frame: Any):
        self.logger.info(f"Received signal {signal.Signals(sig).name}, shutting down...")
        self.is_running = False


def main():
    parser = argparse.ArgumentParser(description="WeChat-AI Bot (Linux)")
    parser.add_argument("--config", default="/config/config.yaml", help="Config file path")
    args = parser.parse_args()

    bot = Bot(config_path=args.config)
    try:
        bot.start()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
