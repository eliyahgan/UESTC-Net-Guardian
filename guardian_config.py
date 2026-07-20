from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
import sys
import tempfile as tempfile_module
from typing import Any


APP_ID = "UESTCNetGuardian"
APP_DISPLAY_NAME = "UESTC 网络与热点守护"
RUN_VALUE_NAME = APP_ID
LEGACY_STARTUP_NAME = "UESTC AutoConnect.lnk"


def app_data_dir() -> Path:
    override = os.getenv("UESTC_GUARDIAN_DATA_DIR", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    root = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    candidates.append(root / APP_ID)
    # A portable/on-dir copy may live in a protected directory.  Keep a
    # deterministic project-local fallback for such environments (including
    # restricted desktop sandboxes), then use the per-user temp directory as a
    # final escape hatch.
    candidates.append(runtime_dir() / ".guardian-data")
    candidates.append(Path(tempfile_module.gettempdir()) / APP_ID)
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return candidates[-1]


def log_path() -> Path:
    return app_data_dir() / "UESTCNetGuardian.log"


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_env_path() -> Path:
    override = os.getenv("UESTC_ENV_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    base = runtime_dir()
    candidates = [base / ".env"]
    if getattr(sys, "frozen", False):
        # The onedir build lives at <project>/dist/UESTCNetGuardian/.
        candidates.extend((base.parent / ".env", base.parent.parent / ".env"))
    candidates.append(Path.cwd() / ".env")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


@dataclass
class GuardianSettings:
    campus_enabled: bool = True
    hotspot_enabled: bool = True
    hotspot_check_interval: int = 10

    @classmethod
    def from_mapping(cls, value: Any) -> "GuardianSettings":
        if not isinstance(value, dict):
            return cls()
        interval = value.get("hotspot_check_interval", 10)
        try:
            interval = max(5, min(300, int(interval)))
        except (TypeError, ValueError):
            interval = 10
        return cls(
            campus_enabled=bool(value.get("campus_enabled", True)),
            hotspot_enabled=bool(value.get("hotspot_enabled", True)),
            hotspot_check_interval=interval,
        )


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or settings_path()

    def load(self) -> GuardianSettings:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return GuardianSettings()
        return GuardianSettings.from_mapping(payload)

    def save(self, settings: GuardianSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n"
        fd, temporary_name = tempfile_module.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class StartupManager:
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(self, command: str | None = None):
        self.command = command or self._default_command()

    @staticmethod
    def _default_command() -> str:
        if getattr(sys, "frozen", False):
            return f'"{Path(sys.executable).resolve()}"'
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        script = Path(__file__).resolve().with_name("guardian_app.py")
        return f'"{pythonw}" "{script}"'

    @staticmethod
    def legacy_shortcut_path() -> Path:
        appdata = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
        return (
            appdata
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
            / LEGACY_STARTUP_NAME
        )

    def is_enabled(self) -> bool:
        if os.name != "nt":
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
        except OSError:
            return False
        return str(value).strip().casefold() == self.command.strip().casefold()

    def enable(self) -> None:
        if os.name != "nt":
            raise OSError("Windows startup is only available on Windows")
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, self.command)
        self._remove_legacy_shortcut()

    def disable(self) -> None:
        if os.name == "nt":
            import winreg

            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    self.RUN_KEY,
                    0,
                    winreg.KEY_SET_VALUE,
                ) as key:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
        self._remove_legacy_shortcut()

    def _remove_legacy_shortcut(self) -> None:
        try:
            self.legacy_shortcut_path().unlink(missing_ok=True)
        except OSError:
            # The current startup setting is still valid; deployment can clean
            # up the legacy shortcut later if another process temporarily owns it.
            pass
