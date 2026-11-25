"""
ESP32-C6 Serial Communication Module
=====================================

Manages the serial (UART) connection to the ESP32-C6 microcontroller.
The ESP32 acts as a bridge, forwarding raw data frames from the
HLK-LD2410D mmWave sensor and processed readings from the MAX30100
pulse-oximeter over a single USB-serial link.

Protocol Convention (ESP32 → Host):
  The ESP32 firmware prefixes each payload with a 1-byte channel tag so this
  module can demultiplex frames to the correct parser:
    0x01  –  HLK-LD2410D raw data frame  (passthrough)
    0x02  –  MAX30100 processed readings  (JSON line)
    0x03  –  ESP32 status / heartbeat
  Each message is framed as:  [TAG (1 B)] [LENGTH (2 B LE)] [PAYLOAD (N B)]

The module is intentionally thin: it owns the serial port and the
read/write threading, but knows nothing about sensor semantics.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional

import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
class Channel(IntEnum):
    """Channel tags sent by the ESP32 firmware."""
    LD2410D = 0x01
    MAX30100 = 0x02
    STATUS = 0x03


DEFAULT_BAUD = 256_000          # LD2410D default baud
DEFAULT_TIMEOUT = 1.0           # serial read timeout (seconds)
RECONNECT_INTERVAL = 3.0        # seconds between reconnect attempts
HEARTBEAT_TIMEOUT = 10.0        # mark connection unhealthy after this


@dataclass
class ConnectionStatus:
    """Snapshot of the serial link's health."""
    connected: bool = False
    port: str = ""
    baud_rate: int = DEFAULT_BAUD
    last_data_at: float = 0.0
    error: Optional[str] = None
    bytes_received: int = 0
    bytes_sent: int = 0
    reconnect_count: int = 0


# ---------------------------------------------------------------------------
# ESP32 Serial Manager
# ---------------------------------------------------------------------------
class ESP32Connection:
    """
    Thread-safe serial connection to the ESP32-C6.

    Usage
    -----
    >>> conn = ESP32Connection()
    >>> conn.on_frame(Channel.LD2410D, my_ld2410_handler)
    >>> conn.on_frame(Channel.MAX30100, my_max30100_handler)
    >>> conn.connect("/dev/ttyUSB0")
    >>> # ... later ...
    >>> conn.disconnect()
    """

    def __init__(
        self,
        baud_rate: int = DEFAULT_BAUD,
        timeout: float = DEFAULT_TIMEOUT,
        auto_reconnect: bool = True,
    ):
        self._baud_rate = baud_rate
        self._timeout = timeout
        self._auto_reconnect = auto_reconnect

        self._serial: Optional[serial.Serial] = None
        self._port: str = ""
        self._running = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Callback registry: channel → list[callable(bytes)]
        self._handlers: dict[Channel, list[Callable[[bytes], None]]] = {
            ch: [] for ch in Channel
        }

        self._status = ConnectionStatus()
        self._status_lock = threading.Lock()

    # ---- public: lifecycle ------------------------------------------------

    def connect(self, port: str) -> None:
        """Open the serial port and start the reader thread."""
        if self._running.is_set():
            logger.warning("Already connected – disconnect first.")
            return

        self._port = port
        self._open_port()
        self._running.set()
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="esp32-reader"
        )
        self._reader_thread.start()
        logger.info("ESP32 reader thread started on %s @ %d baud", port, self._baud_rate)

    def disconnect(self) -> None:
        """Stop the reader thread and close the serial port."""
        self._running.clear()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=3.0)
        self._close_port()
        logger.info("ESP32 connection closed.")

    @property
    def is_connected(self) -> bool:
        return self._running.is_set() and self._serial is not None and self._serial.is_open

    def get_status(self) -> ConnectionStatus:
        """Return a *copy* of the current connection status."""
        with self._status_lock:
            return ConnectionStatus(**self._status.__dict__)

    # ---- public: data flow ------------------------------------------------

    def on_frame(self, channel: Channel, handler: Callable[[bytes], None]) -> None:
        """Register *handler* to be called whenever a frame arrives on *channel*."""
        self._handlers[channel].append(handler)

    def send_raw(self, data: bytes) -> bool:
        """
        Send raw bytes to the ESP32.

        Returns True on success, False if the port is unavailable.
        """
        with self._lock:
            if not self.is_connected:
                return False
            try:
                written = self._serial.write(data)  # type: ignore[union-attr]
                with self._status_lock:
                    self._status.bytes_sent += written
                return True
            except serial.SerialException as exc:
                logger.error("Write failed: %s", exc)
                self._mark_error(str(exc))
                return False

    def send_command(self, channel: Channel, payload: bytes) -> bool:
        """
        Send a framed command to the ESP32.

        Frame format mirrors the inbound convention:
          [TAG (1 B)] [LENGTH (2 B LE)] [PAYLOAD]
        """
        frame = struct.pack("<BH", int(channel), len(payload)) + payload
        return self.send_raw(frame)

    # ---- public: port discovery -------------------------------------------

    @staticmethod
    def list_ports() -> list[dict[str, str]]:
        """
        Return a list of available serial ports with metadata.

        Each dict contains keys: ``port``, ``description``, ``hwid``.
        """
        return [
            {
                "port": p.device,
                "description": p.description,
                "hwid": p.hwid,
            }
            for p in serial.tools.list_ports.comports()
        ]

    @staticmethod
    def find_esp32_port() -> Optional[str]:
        """
        Auto-detect a likely ESP32 port by scanning VID/PID or description.

        Returns the first match, or ``None``.
        """
        esp32_identifiers = [
            "CP210",   # Silicon Labs CP2102/CP2104
            "CH340",   # WCH CH340
            "CH910",   # WCH CH9102
            "FTDI",    # FTDI adapters
            "ESP32",
            "USB Serial",
            "USB-SERIAL",
        ]
        for p in serial.tools.list_ports.comports():
            desc = (p.description or "").upper()
            hwid = (p.hwid or "").upper()
            for ident in esp32_identifiers:
                if ident.upper() in desc or ident.upper() in hwid:
                    return p.device
        return None

    # ---- private: low-level -----------------------------------------------

    def _open_port(self) -> None:
        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self._timeout,
            )
            with self._status_lock:
                self._status.connected = True
                self._status.port = self._port
                self._status.baud_rate = self._baud_rate
                self._status.error = None
            logger.info("Serial port %s opened.", self._port)
        except serial.SerialException as exc:
            logger.error("Cannot open %s: %s", self._port, exc)
            self._mark_error(str(exc))
            raise

    def _close_port(self) -> None:
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.close()
            self._serial = None
        with self._status_lock:
            self._status.connected = False

    def _mark_error(self, msg: str) -> None:
        with self._status_lock:
            self._status.error = msg
            self._status.connected = False

    # ---- private: reader loop ---------------------------------------------

    def _read_loop(self) -> None:
        """
        Continuously read framed messages from the serial port.

        Frame format:  [TAG 1B] [LEN 2B LE] [PAYLOAD LEN B]

        If the ESP32 firmware sends *raw LD2410D passthrough* without our
        envelope, the module also scans for the LD2410D native header
        (F4 F3 F2 F1) and re-tags them automatically.
        """
        buf = bytearray()

        while self._running.is_set():
            try:
                with self._lock:
                    if self._serial is None or not self._serial.is_open:
                        break
                    chunk = self._serial.read(self._serial.in_waiting or 1)

                if not chunk:
                    continue

                buf.extend(chunk)
                with self._status_lock:
                    self._status.bytes_received += len(chunk)
                    self._status.last_data_at = time.time()

                # --- try to consume complete frames from buf ---
                buf = self._consume_buffer(buf)

            except serial.SerialException as exc:
                logger.warning("Serial read error: %s", exc)
                self._mark_error(str(exc))
                if self._auto_reconnect:
                    self._attempt_reconnect()
                else:
                    break
            except Exception:
                logger.exception("Unexpected error in reader thread")

    def _consume_buffer(self, buf: bytearray) -> bytearray:
        """
        Parse as many complete frames out of *buf* as possible.

        Supports two frame formats:
        1. **Envelope format** – [TAG 1B] [LEN 2B LE] [PAYLOAD]
        2. **Raw LD2410D passthrough** – starts with F4 F3 F2 F1,
           ends with F8 F7 F6 F5.
        """
        LD2410_HEADER = bytes([0xF4, 0xF3, 0xF2, 0xF1])
        LD2410_FOOTER = bytes([0xF8, 0xF7, 0xF6, 0xF5])

        while len(buf) >= 3:
            # Check for raw LD2410D header first
            if bytes(buf[:4]) == LD2410_HEADER:
                footer_pos = buf.find(LD2410_FOOTER, 4)
                if footer_pos == -1:
                    break  # incomplete – wait for more data
                frame_end = footer_pos + 4
                raw_frame = bytes(buf[:frame_end])
                buf = buf[frame_end:]
                self._dispatch(Channel.LD2410D, raw_frame)
                continue

            # Envelope format: [TAG] [LEN_LO] [LEN_HI] [PAYLOAD…]
            tag = buf[0]
            if tag not in (0x01, 0x02, 0x03):
                # Unknown byte – discard and resync
                buf.pop(0)
                continue

            payload_len = struct.unpack_from("<H", buf, 1)[0]
            total = 3 + payload_len
            if len(buf) < total:
                break  # incomplete

            payload = bytes(buf[3:total])
            buf = buf[total:]

            try:
                channel = Channel(tag)
            except ValueError:
                logger.debug("Unknown channel tag 0x%02X – skipping", tag)
                continue

            self._dispatch(channel, payload)

        return buf

    def _dispatch(self, channel: Channel, data: bytes) -> None:
        """Call all registered handlers for *channel*."""
        for handler in self._handlers[channel]:
            try:
                handler(data)
            except Exception:
                logger.exception("Handler error on channel %s", channel.name)

    # ---- private: reconnect -----------------------------------------------

    def _attempt_reconnect(self) -> None:
        """Try to re-open the same port after a transient failure."""
        self._close_port()
        while self._running.is_set():
            logger.info("Reconnecting to %s in %.1fs …", self._port, RECONNECT_INTERVAL)
            time.sleep(RECONNECT_INTERVAL)
            try:
                self._open_port()
                with self._status_lock:
                    self._status.reconnect_count += 1
                logger.info("Reconnected to %s", self._port)
                return
            except serial.SerialException:
                continue
