"""One-shot Windows mobile-hotspot health check.

The default backend uses the public Windows Runtime tethering API.  It starts
the hotspot with *no* session configuration, so Windows keeps using the SSID,
passphrase, band, and authentication settings already stored by Settings.

This module deliberately never reads the access-point configuration.  In
particular, neither the hotspot SSID nor its passphrase can reach a result or a
log through this code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


HotspotStatus = Literal[
    "on",
    "started",
    "transition",
    "no_profile",
    "unsupported",
    "failed",
    "error",
]


@dataclass(frozen=True, slots=True)
class HotspotResult:
    """Structured result returned by :func:`ensure_once`."""

    status: HotspotStatus
    client_count: int | None = None
    max_client_count: int | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class TetheringStartResult:
    """Backend-neutral result of a hotspot start operation."""

    success: bool
    status_name: str
    message: str = ""


class HotspotBackend(Protocol):
    """Small injectable boundary around the Windows Runtime API."""

    async def disable_no_connections_timeout(self) -> None: ...

    def get_internet_connection_profile(self) -> Any | None: ...

    def get_tethering_capability(self, profile: Any) -> tuple[bool, str]: ...

    def create_tethering_manager(self, profile: Any) -> Any: ...

    def get_operational_state(self, manager: Any) -> str: ...

    def get_client_counts(self, manager: Any) -> tuple[int | None, int | None]: ...

    async def start_tethering(self, manager: Any) -> TetheringStartResult: ...


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    return str(name if name is not None else value)


class PyWinRTBackend:
    """Production backend backed by ``Windows.Networking.NetworkOperators``.

    Imports are intentionally lazy.  That keeps the rest of the application
    and the unit tests usable on machines where the optional PyWinRT wheels are
    not installed.
    """

    def __init__(self) -> None:
        from winrt.windows.networking.connectivity import NetworkInformation
        from winrt.windows.networking.networkoperators import (
            NetworkOperatorTetheringManager,
            TetheringCapability,
            TetheringOperationalState,
            TetheringOperationStatus,
        )

        self._NetworkInformation = NetworkInformation
        self._TetheringManager = NetworkOperatorTetheringManager
        self._TetheringCapability = TetheringCapability
        self._TetheringState = TetheringOperationalState
        self._TetheringOperationStatus = TetheringOperationStatus

    async def disable_no_connections_timeout(self) -> None:
        manager_type = self._TetheringManager
        if manager_type.is_no_connections_timeout_enabled():
            await manager_type.disable_no_connections_timeout_async()

    def get_internet_connection_profile(self) -> Any | None:
        profile = self._NetworkInformation.get_internet_connection_profile()
        if profile is not None:
            return profile
        # During a DHCP/portal transition Windows can briefly return no
        # "internet" profile even though an Ethernet/Wi-Fi profile exists.
        # Prefer the profile with the best current connectivity in that case;
        # the manager is recreated on every check, so no stale profile is kept.
        try:
            profiles = list(self._NetworkInformation.get_connection_profiles())
        except Exception:
            return None
        if not profiles:
            return None
        try:
            return max(
                profiles,
                key=lambda candidate: int(candidate.get_network_connectivity_level()),
            )
        except Exception:
            return profiles[0]

    def get_tethering_capability(self, profile: Any) -> tuple[bool, str]:
        capability = self._TetheringManager.get_tethering_capability_from_connection_profile(
            profile
        )
        return capability == self._TetheringCapability.ENABLED, _enum_name(capability)

    def create_tethering_manager(self, profile: Any) -> Any:
        return self._TetheringManager.create_from_connection_profile(profile)

    def get_operational_state(self, manager: Any) -> str:
        state = manager.tethering_operational_state
        if state == self._TetheringState.ON:
            return "on"
        if state == self._TetheringState.OFF:
            return "off"
        if state == self._TetheringState.IN_TRANSITION:
            return "transition"
        return "unknown"

    def get_client_counts(self, manager: Any) -> tuple[int | None, int | None]:
        return int(manager.client_count), int(manager.max_client_count)

    async def start_tethering(self, manager: Any) -> TetheringStartResult:
        # Passing no configuration is intentional: Windows uses the user's
        # existing persistent hotspot configuration for this session.
        result = await manager.start_tethering_async()
        status_name = _enum_name(result.status)
        success_statuses = {self._TetheringOperationStatus.SUCCESS}
        already_on = getattr(self._TetheringOperationStatus, "ALREADY_ON", None)
        if already_on is not None:
            success_statuses.add(already_on)
        success = result.status in success_statuses
        return TetheringStartResult(
            success=success,
            status_name=status_name,
            message=getattr(result, "additional_error_message", "") or "",
        )


def _client_counts(
    backend: HotspotBackend, manager: Any
) -> tuple[int | None, int | None]:
    """Read informational counters without making them a health-check failure."""

    try:
        return backend.get_client_counts(manager)
    except Exception:
        return None, None


def _error_message(action: str, exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{action}: {type(exc).__name__}: {detail}"
    return f"{action}: {type(exc).__name__}"


async def ensure_once(backend: HotspotBackend | None = None) -> HotspotResult:
    """Ensure the Windows mobile hotspot is enabled once.

    The function is side-effect free while the hotspot is already on or in a
    transition.  When it is off, it invokes ``StartTetheringAsync()`` without
    arguments so that the existing Windows configuration remains untouched.
    """

    try:
        active_backend = backend if backend is not None else PyWinRTBackend()
        await active_backend.disable_no_connections_timeout()

        profile = active_backend.get_internet_connection_profile()
        if profile is None:
            return HotspotResult(
                status="no_profile",
                message="No suitable upstream connection profile is available.",
            )

        supported, capability_name = active_backend.get_tethering_capability(profile)
        if not supported:
            return HotspotResult(
                status="unsupported",
                message=f"Tethering capability is {capability_name}.",
            )

        manager = active_backend.create_tethering_manager(profile)
        state = active_backend.get_operational_state(manager)
        client_count, max_client_count = _client_counts(active_backend, manager)

        if state == "on":
            return HotspotResult(
                status="on",
                client_count=client_count,
                max_client_count=max_client_count,
                message="Mobile hotspot is on.",
            )

        if state == "transition":
            return HotspotResult(
                status="transition",
                client_count=client_count,
                max_client_count=max_client_count,
                message="Mobile hotspot is changing state.",
            )

        if state != "off":
            return HotspotResult(
                status="error",
                client_count=client_count,
                max_client_count=max_client_count,
                message=f"Unknown mobile-hotspot state: {state}.",
            )

        operation = await active_backend.start_tethering(manager)
        client_count, max_client_count = _client_counts(active_backend, manager)
        if operation.success:
            return HotspotResult(
                status="started",
                client_count=client_count,
                max_client_count=max_client_count,
                message="Mobile hotspot was started.",
            )

        detail = operation.message.strip()
        message = f"Could not start mobile hotspot ({operation.status_name})."
        if detail:
            message = f"{message} {detail}"
        return HotspotResult(
            status="failed",
            client_count=client_count,
            max_client_count=max_client_count,
            message=message,
        )
    except Exception as exc:
        return HotspotResult(
            status="error",
            message=_error_message("Mobile-hotspot check failed", exc),
        )


__all__ = [
    "HotspotBackend",
    "HotspotResult",
    "HotspotStatus",
    "PyWinRTBackend",
    "TetheringStartResult",
    "ensure_once",
]
