#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UESTC Srun campus-network auto login client.

The live UESTC portal switched from the legacy ePortal API to Srun.  This
client follows the public JavaScript served by ``aaa.uestc.edu.cn``:

* ``/cgi-bin/rad_user_info`` is the authoritative online-state check.
* ``/cgi-bin/get_challenge`` provides the short-lived challenge token.
* ``/cgi-bin/srun_portal`` performs challenge/HMAC/XEncode authentication.

Raw credentials are never placed in a URL or log message.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import html
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


VERSION = "4.0.0-srun"
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LOG_PATH = BASE_DIR / "BOCCHI THE ROCK.log"

STANDARD_BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
SRUN_BASE64_ALPHABET = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"


class PortalProtocolError(RuntimeError):
    """The portal returned an invalid or unsafe response."""


def load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip() or str(default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    portal_url: str
    wait_time: int
    max_retry_wait: int
    verify_tls: bool
    allow_insecure_http: bool
    debug: bool
    http_probe_url: str
    http_probe_expected: str
    ac_id_override: str

    @classmethod
    def from_environment(cls) -> "Settings":
        username = (
            os.getenv("UESTC_USERNAME", "").strip()
            or os.getenv("UESTC_PHONE", "").strip()
        )
        password = os.getenv("UESTC_PASSWORD", "")
        if not username or not password:
            raise ValueError("UESTC_USERNAME / UESTC_PASSWORD are not configured")

        wait_time = env_int("UESTC_WAIT_TIME", default=15, minimum=10)
        max_retry_wait = env_int("UESTC_MAX_RETRY_WAIT", default=300, minimum=wait_time)
        return cls(
            username=username,
            password=password,
            portal_url=os.getenv("UESTC_PORTAL_URL", "http://aaa.uestc.edu.cn/").strip(),
            wait_time=wait_time,
            max_retry_wait=max_retry_wait,
            verify_tls=env_bool("UESTC_VERIFY_TLS", default=True),
            allow_insecure_http=env_bool("UESTC_ALLOW_INSECURE_HTTP", default=False),
            debug=env_bool("UESTC_DEBUG", default=False) or "--debug" in sys.argv,
            http_probe_url=os.getenv(
                "UESTC_HTTP_PROBE_URL",
                "http://www.msftconnecttest.com/connecttest.txt",
            ).strip(),
            http_probe_expected=os.getenv(
                "UESTC_HTTP_PROBE_EXPECTED",
                "Microsoft Connect Test",
            ),
            ac_id_override=os.getenv("UESTC_AC_ID", "").strip(),
        )


class RedactingFilter(logging.Filter):
    """Last-resort protection against credentials reaching any handler."""

    FIELD_PATTERN = re.compile(
        r"(?i)((?:password|passwd|pwd|pass|username|userid|useraccount|phone|"
        r"token|challenge|info|chksum|querystring)\s*[=:]\s*)[^&\s,;'\"}]+"
    )

    def __init__(self, secrets: tuple[str, ...]):
        super().__init__()
        self.secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "***")
        message = self.FIELD_PATTERN.sub(r"\1***", message)
        record.msg = message
        record.args = ()
        return True


def build_logger(settings: Settings) -> logging.Logger:
    logger = logging.getLogger("UESTC-SRUN")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    redactor = RedactingFilter((settings.username, settings.password))
    formatter = logging.Formatter(
        "%(asctime)s %(name)s:%(levelname)s:%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_PATH,
        mode="a",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    logger.addHandler(file_handler)

    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG if settings.debug else logging.INFO)
        console.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)-8s %(message)s",
            datefmt="%H:%M:%S",
        ))
        console.addFilter(redactor)
        logger.addHandler(console)

    return logger


class SingleInstance:
    """Windows named-mutex guard.  The handle lives for the process lifetime."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str):
        self.name = name
        self.handle: Optional[int] = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError()
        if kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        self.handle = int(handle)
        return True

    def close(self) -> None:
        if self.handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        query = urlencode([
            (key, "***" if value else "")
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except Exception:
        return "<invalid-url>"


def parse_json_or_jsonp(text: str, expected_callback: str = "") -> dict[str, Any]:
    value = text.strip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        match = re.fullmatch(
            r"\s*([A-Za-z_$][\w$]*)\s*\((.*)\)\s*;?\s*",
            value,
            flags=re.DOTALL,
        )
        if not match:
            raise PortalProtocolError("portal response is neither JSON nor JSONP")
        callback, body = match.groups()
        if expected_callback and callback != expected_callback:
            raise PortalProtocolError("portal returned an unexpected JSONP callback")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise PortalProtocolError("portal JSONP body is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PortalProtocolError("portal response must be a JSON object")
    return payload


def _js_code_units(value: str) -> list[int]:
    raw = value.encode("utf-16le", errors="surrogatepass")
    if not raw:
        return []
    return list(struct.unpack("<" + "H" * (len(raw) // 2), raw))


def _sencode(value: str, include_length: bool) -> list[int]:
    units = _js_code_units(value)
    result: list[int] = []
    for offset in range(0, len(units), 4):
        word = 0
        for shift in range(4):
            if offset + shift < len(units):
                word |= units[offset + shift] << (shift * 8)
        result.append(word & 0xFFFFFFFF)
    if include_length:
        result.append(len(units))
    return result


def srun_xencode(value: str, key: str) -> bytes:
    """Port of the exact XEncode routine served by UESTC Portal.js."""
    if value == "":
        return b""
    values = _sencode(value, True)
    keys = _sencode(key, False)
    keys.extend([0] * (4 - len(keys)))

    n = len(values) - 1
    z = values[n]
    y = values[0]
    constant = 0x9E3779B9
    rounds = 6 + 52 // (n + 1)
    total = 0

    while rounds > 0:
        rounds -= 1
        total = (total + constant) & 0xFFFFFFFF
        e = (total >> 2) & 3
        for p in range(n):
            y = values[p + 1]
            mixed = ((z >> 5) ^ ((y << 2) & 0xFFFFFFFF))
            mixed += ((y >> 3) ^ ((z << 4) & 0xFFFFFFFF) ^ (total ^ y))
            mixed += keys[(p & 3) ^ e] ^ z
            values[p] = (values[p] + mixed) & 0xFFFFFFFF
            z = values[p]

        y = values[0]
        mixed = ((z >> 5) ^ ((y << 2) & 0xFFFFFFFF))
        mixed += ((y >> 3) ^ ((z << 4) & 0xFFFFFFFF) ^ (total ^ y))
        mixed += keys[(n & 3) ^ e] ^ z
        values[n] = (values[n] + mixed) & 0xFFFFFFFF
        z = values[n]

    return b"".join(struct.pack("<I", word) for word in values)


def srun_custom_base64(value: bytes) -> str:
    encoded = base64.b64encode(value).decode("ascii")
    table = str.maketrans(STANDARD_BASE64_ALPHABET, SRUN_BASE64_ALPHABET)
    return encoded.translate(table)


def build_srun_material(
    username: str,
    password: str,
    client_ip: str,
    ac_id: str,
    token: str,
) -> tuple[str, str, str]:
    hmd5 = hmac.new(
        token.encode("utf-8"),
        password.encode("utf-8"),
        hashlib.md5,
    ).hexdigest()
    raw_info = json.dumps(
        {
            "username": username,
            "password": password,
            "ip": client_ip,
            "acid": ac_id,
            "enc_ver": "srun_bx1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    info = "{SRBX1}" + srun_custom_base64(srun_xencode(raw_info, token))
    checksum_source = "".join([
        token, username,
        token, hmd5,
        token, ac_id,
        token, client_ip,
        token, "200",
        token, "1",
        token, info,
    ])
    checksum = hashlib.sha1(checksum_source.encode("utf-8")).hexdigest()
    return hmd5, info, checksum


@dataclass(frozen=True)
class PortalContext:
    origin: str
    ac_id: str
    nas_ip: str = ""
    ap_id: str = ""
    ap_ip: str = ""


@dataclass(frozen=True)
class AuthResult:
    success: bool
    message: str
    fatal: bool = False
    retry_after: int = 0


class SrunClient:
    OFFLINE_ERRORS = {
        "not_online_error",
        "no_response_data_error",
        "rd000",
    }

    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self.session = requests.Session()
        self.session.verify = settings.verify_tls
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Encoding": "identity",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        # Build a safe provisional context without touching the network.  This
        # lets the long-running loop survive Wi-Fi/DHCP not being ready at logon.
        self._validate_portal_url(settings.portal_url)
        portal_parts = urlsplit(settings.portal_url)
        origin = urlunsplit((portal_parts.scheme, portal_parts.netloc, "/", "", ""))
        self.context = PortalContext(
            origin=origin,
            ac_id=settings.ac_id_override or "1",
        )

    @staticmethod
    def _trusted_hostname(hostname: Optional[str]) -> bool:
        if not hostname:
            return False
        host = hostname.rstrip(".").lower()
        if host == "uestc.edu.cn" or host.endswith(".uestc.edu.cn"):
            return True
        try:
            return ipaddress.ip_address(host).is_private
        except ValueError:
            return False

    def _validate_portal_url(self, url: str, same_host_as: str = "") -> None:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"}:
            raise PortalProtocolError("portal URL uses an unsupported protocol")
        if not self._trusted_hostname(parts.hostname):
            raise PortalProtocolError("portal URL is not a UESTC or private-network host")
        if same_host_as:
            expected = (urlsplit(same_host_as).hostname or "").lower()
            if (parts.hostname or "").lower() != expected:
                raise PortalProtocolError("portal redirected to a different host")
        if parts.scheme.lower() == "http" and not self.settings.allow_insecure_http:
            raise PortalProtocolError(
                "portal uses plaintext HTTP; set UESTC_ALLOW_INSECURE_HTTP=1 only if accepted"
            )

    def discover_portal(self) -> PortalContext:
        self._validate_portal_url(self.settings.portal_url)
        try:
            response = self.session.get(
                self.settings.portal_url,
                timeout=10,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise PortalProtocolError("cannot reach the UESTC portal entry") from exc

        self._validate_portal_url(response.url, self.settings.portal_url)
        page_url = response.url
        soup = BeautifulSoup(response.text, "lxml")
        meta = soup.find("meta", attrs={
            "http-equiv": lambda value: isinstance(value, str) and value.lower() == "refresh"
        })
        if meta:
            content = str(meta.get("content", ""))
            match = re.search(r"(?:^|;)\s*url\s*=\s*(.+)\s*$", content, re.IGNORECASE)
            if match:
                page_url = urljoin(response.url, html.unescape(match.group(1).strip(" '\"")))
                self._validate_portal_url(page_url, self.settings.portal_url)

        parts = urlsplit(page_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        ac_id = self.settings.ac_id_override or query.get("ac_id") or query.get("acid") or "1"
        if not re.fullmatch(r"[0-9]+", ac_id):
            raise PortalProtocolError("portal returned an invalid AC ID")
        origin = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
        self.logger.info(
            "已识别 UESTC Srun 门户: host=%s, ac_id=%s",
            parts.hostname,
            ac_id,
        )
        return PortalContext(
            origin=origin,
            ac_id=ac_id,
            nas_ip=query.get("nas_ip", ""),
            ap_id=query.get("ap_id", ""),
            ap_ip=query.get("ap_ip", ""),
        )

    def _api_url(self, path: str) -> str:
        url = urljoin(self.context.origin, path)
        self._validate_portal_url(url, self.context.origin)
        return url

    def _request_json(self, path: str, params: dict[str, Any], timeout: int = 10) -> dict[str, Any]:
        callback = f"ue_{os.getpid()}_{int(time.time() * 1000)}"
        request_params = dict(params)
        request_params.setdefault("callback", callback)
        request_params.setdefault("_", str(int(time.time() * 1000)))
        try:
            response = self.session.get(
                self._api_url(path),
                params=request_params,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PortalProtocolError(f"portal API request failed: {path}") from exc
        if 300 <= response.status_code < 400:
            raise PortalProtocolError(f"portal API unexpectedly redirected: {path}")
        if response.status_code != 200:
            raise PortalProtocolError(
                f"portal API returned HTTP {response.status_code}: {path}"
            )
        return parse_json_or_jsonp(response.text, expected_callback=callback)

    def query_online_state(self) -> Optional[bool]:
        try:
            payload = self._request_json("/cgi-bin/rad_user_info", {}, timeout=6)
        except PortalProtocolError as exc:
            self.logger.warning("Srun 在线状态接口不可用: %s", exc)
            return None

        error = str(payload.get("error", "")).strip().lower()
        result = str(payload.get("res", "")).strip().lower()
        if error == "ok" or result == "ok":
            return True
        if error in self.OFFLINE_ERRORS or result in self.OFFLINE_ERRORS:
            return False
        if "not_online" in error or "not_online" in result:
            return False
        self.logger.warning("Srun 在线状态返回未知结果: error=%s, res=%s", error, result)
        return None

    def http_probe_online(self) -> bool:
        try:
            response = self.session.get(
                self.settings.http_probe_url,
                timeout=6,
                allow_redirects=False,
            )
        except requests.RequestException:
            return False
        if response.status_code == 204 and not self.settings.http_probe_expected:
            return True
        return (
            response.status_code == 200
            and response.text.strip() == self.settings.http_probe_expected.strip()
        )

    def is_online(self) -> bool:
        state = self.query_online_state()
        if state is not None:
            self.logger.debug("Srun 在线状态: %s", "online" if state else "offline")
            return state
        fallback = self.http_probe_online()
        self.logger.debug("HTTP 连通性回退: %s", "online" if fallback else "offline")
        return fallback

    def get_challenge(self) -> tuple[str, str]:
        payload = self._request_json(
            "/cgi-bin/get_challenge",
            {"username": self.settings.username, "ip": ""},
        )
        if str(payload.get("error", "")).lower() not in {"", "ok"}:
            raise PortalProtocolError("Srun challenge request was rejected")
        token = str(payload.get("challenge", ""))
        client_ip = str(payload.get("client_ip") or payload.get("online_ip") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{16,128}", token):
            raise PortalProtocolError("Srun returned an invalid challenge token")
        try:
            ipaddress.ip_address(client_ip)
        except ValueError as exc:
            raise PortalProtocolError("Srun returned an invalid client IP") from exc
        return token, client_ip

    @staticmethod
    def _response_message(payload: dict[str, Any]) -> str:
        values = [
            payload.get("error_msg"),
            payload.get("suc_msg"),
            payload.get("error"),
            payload.get("ecode"),
        ]
        return " | ".join(str(value) for value in values if value not in (None, ""))[:300]

    def authenticate(self) -> AuthResult:
        # Re-discover AC ID after network changes; the current campus segment may differ.
        self.context = self.discover_portal()
        token, client_ip = self.get_challenge()
        hmd5, info, checksum = build_srun_material(
            self.settings.username,
            self.settings.password,
            client_ip,
            self.context.ac_id,
            token,
        )
        params = {
            "action": "login",
            "username": self.settings.username,
            "password": "{MD5}" + hmd5,
            "os": "Windows NT",
            "name": "Windows",
            "nas_ip": self.context.nas_ip,
            "double_stack": "0",
            "chksum": checksum,
            "info": info,
            "ac_id": self.context.ac_id,
            "ip": client_ip,
            "n": "200",
            "type": "1",
            "captchaId": "",
            "captchaVal": "",
            "ap_id": self.context.ap_id,
            "ap_ip": self.context.ap_ip,
            "mac": "",
        }
        payload = self._request_json("/cgi-bin/srun_portal", params, timeout=12)
        error = str(payload.get("error", "")).strip().lower()
        result = str(payload.get("res", "")).strip().lower()
        ecode = str(payload.get("ecode", "")).strip().upper()
        error_msg = str(payload.get("error_msg", "")).strip().lower()
        message = self._response_message(payload) or "portal returned no message"

        if error == "ok" or result == "ok":
            for _ in range(5):
                time.sleep(2)
                if self.query_online_state() is True:
                    return AuthResult(True, message)
            return AuthResult(False, "门户返回成功，但在线状态复核失败", retry_after=15)

        if error_msg == "ip_already_online_error" or error == "ip_already_online_error":
            if self.query_online_state() is True:
                return AuthResult(True, "IP 已在线")
            return AuthResult(False, message, retry_after=15)

        fatal_markers = (
            "e2531",
            "password",
            "密码错误",
            "用户名或密码",
            "账号或密码",
            "account_locked",
        )
        combined = " ".join((ecode.lower(), error, error_msg, message.lower()))
        if any(marker in combined for marker in fatal_markers):
            return AuthResult(False, message, fatal=True)
        if "e2533" in combined:
            return AuthResult(False, message, fatal=True, retry_after=300)
        if "e2532" in combined:
            return AuthResult(False, message, retry_after=10)
        return AuthResult(False, message)

    def run(self) -> int:
        self.logger.info(
            "UESTC Srun AutoConnect 已启动: version=%s pid=%s interval=%ss",
            VERSION,
            os.getpid(),
            self.settings.wait_time,
        )
        failures = 0
        cycle = 0
        while True:
            cycle += 1
            try:
                if self.is_online():
                    failures = 0
                    self.logger.debug("[周期#%s] 已认证，等待 %ss", cycle, self.settings.wait_time)
                    time.sleep(self.settings.wait_time)
                    continue

                self.logger.info("[周期#%s] Srun 状态为未认证，开始登录", cycle)
                result = self.authenticate()
                if result.success:
                    failures = 0
                    self.logger.info("[周期#%s] 自动认证成功", cycle)
                    time.sleep(self.settings.wait_time)
                    continue
                if result.fatal:
                    self.logger.critical(
                        "认证被门户拒绝，已停止以避免账号锁定: %s",
                        result.message,
                    )
                    return 3

                failures += 1
                exponent = min(failures - 1, 8)
                delay = min(
                    max(result.retry_after, self.settings.wait_time * (2 ** exponent)),
                    self.settings.max_retry_wait,
                )
                self.logger.error(
                    "[周期#%s] 自动认证失败: %s；%ss 后重试",
                    cycle,
                    result.message,
                    delay,
                )
                time.sleep(delay)
            except KeyboardInterrupt:
                self.logger.info("收到停止请求")
                return 0
            except Exception as exc:
                failures += 1
                exponent = min(failures - 1, 8)
                delay = min(
                    self.settings.wait_time * (2 ** exponent),
                    self.settings.max_retry_wait,
                )
                self.logger.error(
                    "[周期#%s] 未处理异常已隔离: %s；%ss 后重试",
                    cycle,
                    exc,
                    delay,
                )
                time.sleep(delay)


def main() -> int:
    load_env_file()
    try:
        settings = Settings.from_environment()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    logger = build_logger(settings)
    mutex = SingleInstance("Local\\UESTC_AutoConnect_Srun_V4")
    try:
        if not mutex.acquire():
            logger.warning("已有一个 UESTC AutoConnect 实例在运行，本实例退出")
            return 0
        if urlsplit(settings.portal_url).scheme.lower() == "http":
            logger.warning("校园网门户使用明文 HTTP；请勿复用统一身份认证密码")
        if not settings.verify_tls:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        client = SrunClient(settings, logger)
        return client.run()
    except Exception as exc:
        logger.critical("启动失败: %s", exc)
        return 1
    finally:
        mutex.close()


if __name__ == "__main__":
    raise SystemExit(main())
