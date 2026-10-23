from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import POINTER, Structure, WINFUNCTYPE, byref, c_void_p, windll
from ctypes.wintypes import BOOL, BYTE, DWORD, HANDLE, WORD


from wifi_monitor import WifiMonitor


logger = logging.getLogger(__name__)


class GUID(Structure):
    """ctypes GUID layout (ctypes.wintypes does not export GUID)."""

    _fields_ = [
        ("Data1", DWORD),
        ("Data2", WORD),
        ("Data3", WORD),
        ("Data4", BYTE * 8),
    ]


class WLAN_NOTIFICATION_DATA(Structure):
    _fields_ = [
        ("NotificationSource", DWORD),
        ("NotificationCode", DWORD),
        ("InterfaceGuid", GUID),
        ("dwDataSize", DWORD),
        ("pData", c_void_p),
    ]


WLAN_NOTIFICATION_CALLBACK = WINFUNCTYPE(
    None,
    POINTER(WLAN_NOTIFICATION_DATA),
    c_void_p,
)

WLAN_NOTIFICATION_SOURCE_ALL = 0xFFFFFFFF
WLAN_CLIENT_VERSION = 2


class NativeWifiMonitor(WifiMonitor):
    """Windows native WLAN notification monitor with netsh state parsing."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._wlanapi = None
        self._client_handle = HANDLE(0)
        self._notification_callback = None

    def run(self) -> None:
        if sys.platform != "win32":
            logger.info("Native WLAN monitor is Windows-only; using polling")
            super().run()
            return

        try:
            self._open_native_wlan()
            logger.info("Native WLAN notifications enabled")
            self._process_state(self.read_state())
        except Exception as exc:
            logger.warning("Native WLAN monitor unavailable; using polling", exc_info=exc)
            super().run()
            return

        try:
            while not self._stop_event.is_set():
                if self._paused:
                    self._pause_event.wait(timeout=0.5)
                    continue
                # Safety net: a WLAN notification calls _process_state only
                # once, which can never satisfy debounce_count >= 2 on its
                # own. Re-reading on the poll interval confirms the state
                # even if a notification is missed or arrives just once.
                try:
                    self._process_state(self.read_state())
                except Exception:
                    logger.exception("Native WLAN periodic state read failed")
                self._stop_event.wait(timeout=self.interval_seconds)
        finally:
            self._close_native_wlan()
            logger.info("Native WLAN monitor stopped")

    def _open_native_wlan(self) -> None:
        if self._wlanapi is None:
            self._wlanapi = windll.wlanapi

        wlanapi = self._wlanapi
        wlanapi.WlanOpenHandle.argtypes = [
            DWORD,
            c_void_p,
            POINTER(DWORD),
            POINTER(HANDLE),
        ]
        wlanapi.WlanOpenHandle.restype = DWORD

        negotiated_version = DWORD(0)
        result = wlanapi.WlanOpenHandle(
            WLAN_CLIENT_VERSION,
            None,
            byref(negotiated_version),
            byref(self._client_handle),
        )
        if result != 0:
            raise OSError(f"WlanOpenHandle failed with {result}")

        wlanapi.WlanRegisterNotification.argtypes = [
            HANDLE,
            DWORD,
            BOOL,
            WLAN_NOTIFICATION_CALLBACK,
            c_void_p,
            c_void_p,
            POINTER(DWORD),
        ]
        wlanapi.WlanRegisterNotification.restype = DWORD

        self._notification_callback = WLAN_NOTIFICATION_CALLBACK(
            self._on_native_notification
        )
        previous_source = DWORD(0)
        result = wlanapi.WlanRegisterNotification(
            self._client_handle,
            WLAN_NOTIFICATION_SOURCE_ALL,
            False,
            self._notification_callback,
            None,
            None,
            byref(previous_source),
        )
        if result != 0:
            raise OSError(f"WlanRegisterNotification failed with {result}")

    def _close_native_wlan(self) -> None:
        if self._wlanapi is None:
            return

        try:
            result = self._wlanapi.WlanCloseHandle(
                self._client_handle,
                None,
            )
            if result != 0:
                logger.warning("WlanCloseHandle failed with %s", result)
        except Exception:
            logger.exception("Failed to close native WLAN handle")

        self._notification_callback = None
        self._client_handle = HANDLE(0)

    def _on_native_notification(
        self,
        notification_data: POINTER(WLAN_NOTIFICATION_DATA),
        context: c_void_p,
    ) -> None:
        del notification_data
        del context

        if self._paused or self._stop_event.is_set():
            return

        try:
            state = self.read_state()
            self._process_state(state)
        except Exception:
            logger.exception("Native WLAN notification handling failed")
