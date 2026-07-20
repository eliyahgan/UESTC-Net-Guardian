from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
from typing import Any

from PIL import Image, ImageDraw
import pystray

from campus_guard import CampusGuard, CampusStatus
from guardian_config import (
    APP_DISPLAY_NAME,
    GuardianSettings,
    SettingsStore,
    StartupManager,
    app_data_dir,
    find_env_path,
    log_path,
)
from hotspot_guard import HotspotResult, PyWinRTBackend
from hotspot_worker import HotspotGuardWorker
from uestc_srun_autoconnect import RedactingFilter, SingleInstance, load_env_file


APP_VERSION = "1.0.0"
MUTEX_NAME = r"Local\UESTCNetGuardian_Tray_V1"


def build_guardian_logger(debug: bool = False) -> logging.Logger:
    destination = log_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("UESTCNetGuardian")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    redactor = RedactingFilter(
        (
            os.getenv("UESTC_USERNAME", ""),
            os.getenv("UESTC_PHONE", ""),
            os.getenv("UESTC_PASSWORD", ""),
        )
    )
    formatter = logging.Formatter(
        "%(asctime)s %(name)s:%(levelname)s:%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        destination,
        mode="a",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    logger.addHandler(file_handler)

    if debug and sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.DEBUG)
        console.setFormatter(formatter)
        console.addFilter(redactor)
        logger.addHandler(console)
    return logger


def create_tray_image(level: str = "normal", size: int = 64) -> Image.Image:
    colors = {
        "normal": (28, 132, 198, 255),
        "healthy": (30, 165, 105, 255),
        "warning": (232, 151, 34, 255),
        "error": (207, 63, 63, 255),
        "disabled": (112, 121, 132, 255),
    }
    color = colors.get(level, colors["normal"])
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 64
    shield = [(32, 3), (56, 12), (53, 39), (43, 53), (32, 61), (21, 53), (11, 39), (8, 12)]
    draw.polygon([(int(x * scale), int(y * scale)) for x, y in shield], fill=color)
    width = max(2, int(4 * scale))
    white = (255, 255, 255, 245)
    draw.arc(
        (int(16 * scale), int(18 * scale), int(48 * scale), int(49 * scale)),
        215,
        325,
        fill=white,
        width=width,
    )
    draw.arc(
        (int(23 * scale), int(26 * scale), int(41 * scale), int(44 * scale)),
        215,
        325,
        fill=white,
        width=width,
    )
    radius = max(2, int(3.5 * scale))
    center_x, center_y = int(32 * scale), int(43 * scale)
    draw.ellipse(
        (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
        fill=white,
    )
    return image


CAMPUS_LABELS = {
    "starting": "正在启动",
    "disabled": "守护已停用",
    "checking": "正在检查",
    "online": "已连接",
    "authenticating": "正在认证",
    "fatal": "认证已暂停",
    "config_error": "账号未配置",
    "error": "暂时不可用",
    "stopped": "已停止",
}

HOTSPOT_LABELS = {
    "starting": "正在启动",
    "disabled": "守护已停用",
    "on": "已开启",
    "started": "已自动恢复",
    "transition": "正在切换状态",
    "no_profile": "等待上游网络",
    "unsupported": "当前设备不支持",
    "failed": "自动开启失败",
    "error": "暂时不可用",
}


class GuardianTrayApp:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.store = SettingsStore()
        self.settings = self.store.load()
        self.startup = StartupManager()
        self._settings_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._ready = False
        self._closing = False
        self._campus_status = CampusStatus(
            "starting" if self.settings.campus_enabled else "disabled"
        )
        self._hotspot_status = HotspotResult(
            "error",
            message="starting" if self.settings.hotspot_enabled else "disabled",
        )
        self._hotspot_display_code = (
            "starting" if self.settings.hotspot_enabled else "disabled"
        )

        self.campus_guard = CampusGuard(logger, self._on_campus_status)
        self.hotspot_guard = HotspotGuardWorker(
            logger,
            interval=self.settings.hotspot_check_interval,
            result_callback=self._on_hotspot_status,
        )
        self.icon = pystray.Icon(
            "UESTCNetGuardian",
            create_tray_image("normal"),
            APP_DISPLAY_NAME,
            self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(self._campus_status_text, None, enabled=False),
            pystray.MenuItem(self._hotspot_status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "自动连接校园网",
                self._toggle_campus,
                checked=lambda item: self.settings.campus_enabled,
            ),
            pystray.MenuItem(
                "自动保持热点",
                self._toggle_hotspot,
                checked=lambda item: self.settings.hotspot_enabled,
            ),
            pystray.MenuItem(
                "开机自启动",
                self._toggle_startup,
                checked=lambda item: self.startup.is_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即检查并修复", self._check_now, default=True),
            pystray.MenuItem("打开 Windows 热点设置", self._open_hotspot_settings),
            pystray.MenuItem("查看运行日志", self._open_log),
            pystray.MenuItem("打开数据目录", self._open_data_dir),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"退出 {APP_DISPLAY_NAME}", self._quit),
        )

    def _campus_status_text(self, item: pystray.MenuItem) -> str:
        with self._status_lock:
            status = self._campus_status
        return f"校园网：{CAMPUS_LABELS.get(status.code, status.code)}"

    def _hotspot_status_text(self, item: pystray.MenuItem) -> str:
        with self._status_lock:
            code = self._hotspot_display_code
            status = self._hotspot_status
        text = HOTSPOT_LABELS.get(code, code)
        if code in {"on", "started"} and status.client_count is not None:
            text += f" · {status.client_count} 台设备"
        return f"热点：{text}"

    def _save_settings(self) -> None:
        try:
            self.store.save(self.settings)
        except OSError as exc:
            self.logger.error("保存托盘设置失败: %s", exc)
            self._notify("设置保存失败", str(exc))

    def _toggle_campus(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        with self._settings_lock:
            self.settings.campus_enabled = not self.settings.campus_enabled
            enabled = self.settings.campus_enabled
            self._save_settings()
        with self._status_lock:
            self._campus_status = CampusStatus("starting" if enabled else "disabled")
        self.campus_guard.set_enabled(enabled)
        self._update_ui()

    def _toggle_hotspot(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        with self._settings_lock:
            self.settings.hotspot_enabled = not self.settings.hotspot_enabled
            enabled = self.settings.hotspot_enabled
            self._save_settings()
        with self._status_lock:
            self._hotspot_display_code = "starting" if enabled else "disabled"
        self.hotspot_guard.set_enabled(enabled)
        self._update_ui()

    def _toggle_startup(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        try:
            if self.startup.is_enabled():
                self.startup.disable()
                self._notify("开机自启动", "已关闭登录后自动启动。")
            else:
                self.startup.enable()
                self._notify("开机自启动", "已开启登录后自动启动。")
        except OSError as exc:
            self.logger.error("切换开机自启动失败: %s", exc)
            self._notify("无法修改开机自启动", str(exc))
        self._update_ui()

    def _check_now(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        if self.settings.campus_enabled:
            self.campus_guard.check_now()
        if self.settings.hotspot_enabled:
            self.hotspot_guard.check_now()

    def _on_campus_status(self, status: CampusStatus) -> None:
        with self._status_lock:
            previous = self._campus_status
            self._campus_status = status
        if status.code == "fatal" and previous.code != "fatal":
            self._notify("校园网认证已暂停", status.message or "请检查账号和密码。")
        elif status.code == "online" and previous.code == "authenticating":
            self._notify("校园网已连接", "自动认证成功。")
        self._update_ui()

    def _on_hotspot_status(self, status: HotspotResult) -> None:
        with self._status_lock:
            previous_code = self._hotspot_display_code
            self._hotspot_status = status
            self._hotspot_display_code = status.status
        if status.status == "started" and previous_code != "started":
            self._notify("移动热点已恢复", "已按 Windows 中现有的热点配置重新开启。")
        self._update_ui()

    def _icon_level(self) -> str:
        with self._status_lock:
            campus = self._campus_status.code
            hotspot = self._hotspot_display_code
        if not self.settings.campus_enabled and not self.settings.hotspot_enabled:
            return "disabled"
        if campus in {"fatal", "config_error"} or hotspot in {"unsupported", "failed"}:
            return "error"
        if campus in {"error"} or hotspot in {"error", "no_profile"}:
            return "warning"
        campus_ok = not self.settings.campus_enabled or campus == "online"
        hotspot_ok = not self.settings.hotspot_enabled or hotspot in {"on", "started"}
        return "healthy" if campus_ok and hotspot_ok else "normal"

    def _update_ui(self) -> None:
        if not self._ready or self._closing:
            return
        try:
            self.icon.icon = create_tray_image(self._icon_level())
            self.icon.title = (
                f"{APP_DISPLAY_NAME} | "
                f"{self._campus_status_text(None)} | {self._hotspot_status_text(None)}"
            )[:127]
            self.icon.update_menu()
        except Exception as exc:
            self.logger.debug("刷新托盘状态失败: %s", exc)

    def _notify(self, title: str, message: str) -> None:
        if not self._ready or self._closing:
            return
        try:
            self.icon.notify(message[:240], title[:63])
        except Exception as exc:
            self.logger.debug("显示系统通知失败: %s", exc)

    def _open_path(self, value: str | Path, description: str) -> None:
        try:
            os.startfile(str(value))  # type: ignore[attr-defined]
        except OSError as exc:
            self.logger.error("打开%s失败: %s", description, exc)
            self._notify(f"无法打开{description}", str(exc))

    def _open_hotspot_settings(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._open_path("ms-settings:network-mobilehotspot", "热点设置")

    def _open_log(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        destination = log_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.touch(exist_ok=True)
        self._open_path(destination, "运行日志")

    def _open_data_dir(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        destination = app_data_dir()
        destination.mkdir(parents=True, exist_ok=True)
        self._open_path(destination, "数据目录")

    def _quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._closing = True
        icon.stop()

    def _setup(self, icon: pystray.Icon) -> None:
        self._ready = True
        icon.visible = True
        self.campus_guard.start()
        self.hotspot_guard.start()
        self.campus_guard.set_enabled(self.settings.campus_enabled)
        self.hotspot_guard.set_enabled(self.settings.hotspot_enabled)
        self.logger.info(
            "%s 已启动: version=%s pid=%s campus=%s hotspot=%s",
            APP_DISPLAY_NAME,
            APP_VERSION,
            os.getpid(),
            self.settings.campus_enabled,
            self.settings.hotspot_enabled,
        )
        self._update_ui()

    def run(self) -> None:
        try:
            self.icon.run(setup=self._setup)
        finally:
            self._closing = True
            self.campus_guard.stop()
            self.hotspot_guard.stop()
            self.logger.info("%s 已退出", APP_DISPLAY_NAME)


def write_diagnostic(path: Path) -> int:
    payload: dict[str, Any] = {
        "app": APP_DISPLAY_NAME,
        "version": APP_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": str(Path(sys.executable).resolve()),
        "env_path": str(find_env_path()),
        "env_exists": find_env_path().is_file(),
        "startup_enabled": StartupManager().is_enabled(),
        "settings": SettingsStore().load().__dict__,
    }
    try:
        backend = PyWinRTBackend()
        profile = backend.get_internet_connection_profile()
        if profile is None:
            payload["hotspot"] = {"status": "no_profile"}
        else:
            supported, capability = backend.get_tethering_capability(profile)
            manager = backend.create_tethering_manager(profile)
            current, maximum = backend.get_client_counts(manager)
            payload["hotspot"] = {
                "supported": supported,
                "capability": capability,
                "state": backend.get_operational_state(manager),
                "client_count": current,
                "max_client_count": maximum,
            }
    except Exception as exc:
        payload["hotspot"] = {
            "status": "error",
            "message": f"{type(exc).__name__}: {exc}",
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_DISPLAY_NAME)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--startup", choices=("enable", "disable", "status"))
    parser.add_argument("--diagnostic-output", type=Path)
    parser.add_argument("--version", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.version:
        if sys.stdout is not None:
            print(APP_VERSION)
        return 0
    if args.startup:
        manager = StartupManager()
        if args.startup == "enable":
            manager.enable()
            return 0
        if args.startup == "disable":
            manager.disable()
            return 0
        return 0 if manager.is_enabled() else 1
    if args.diagnostic_output:
        return write_diagnostic(args.diagnostic_output.resolve())

    env_path = find_env_path()
    load_env_file(env_path)
    logger = build_guardian_logger(args.debug)
    mutex = SingleInstance(MUTEX_NAME)
    try:
        if not mutex.acquire():
            logger.info("已有一个 %s 实例在运行，本实例退出", APP_DISPLAY_NAME)
            return 0
        GuardianTrayApp(logger).run()
        return 0
    except Exception as exc:
        logger.critical("托盘程序启动失败: %s", exc)
        return 1
    finally:
        mutex.close()


if __name__ == "__main__":
    raise SystemExit(main())
