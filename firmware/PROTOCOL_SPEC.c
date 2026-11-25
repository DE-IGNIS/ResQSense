/*
 * ResQSense – ESP32-C6 Firmware Reference
 * ========================================
 *
 * This file documents the serial protocol the ESP32 firmware must
 * implement so that the Python backend can communicate with it.
 *
 * The actual firmware should be written in Arduino / ESP-IDF and
 * flashed to the ESP32-C6.  This file serves as the specification.
 *
 * -----------------------------------------------------------------
 * SERIAL LINK
 * -----------------------------------------------------------------
 *   Baud rate  : 256 000  (matches LD2410D default)
 *   Data bits  : 8
 *   Parity     : None
 *   Stop bits  : 1
 *
 * -----------------------------------------------------------------
 * ESP32 → HOST  (upstream)
 * -----------------------------------------------------------------
 *   The ESP32 can send data in two modes:
 *
 *   MODE A – Raw LD2410D passthrough
 *     The ESP32 simply forwards the LD2410D UART output byte-for-byte.
 *     The Python backend auto-detects LD2410D frames by scanning for
 *     the native header (F4 F3 F2 F1) and footer (F8 F7 F6 F5).
 *
 *   MODE B – Envelope framing (recommended)
 *     Each message is prefixed with a channel tag:
 *
 *       [TAG 1B] [LENGTH 2B little-endian] [PAYLOAD N B]
 *
 *     Channel tags:
 *       0x01  –  HLK-LD2410D raw frame (passthrough)
 *       0x02  –  MAX30100 JSON reading
 *       0x03  –  ESP32 status / heartbeat
 *
 *   MAX30100 JSON format:
 *     Single sample:  {"ir": 54321, "red": 43210, "ts": 1234567.89}
 *     Batch:          {"samples": [{"ir":…,"red":…}, …], "ts": …}
 *
 *   Status/heartbeat (channel 0x03):
 *     Plain UTF-8 text, e.g. "OK" or "TEMP=42.5"
 *
 * -----------------------------------------------------------------
 * HOST → ESP32  (downstream)
 * -----------------------------------------------------------------
 *   The Python backend sends commands using the same envelope format:
 *     [TAG 1B] [LENGTH 2B LE] [PAYLOAD]
 *
 *   Channel 0x01 payloads are LD2410D command frames that the ESP32
 *   should forward to the LD2410D UART as-is.
 *
 * -----------------------------------------------------------------
 * HARDWARE WIRING (reference)
 * -----------------------------------------------------------------
 *   ESP32-C6 GPIO4  (TX) → LD2410D RX
 *   ESP32-C6 GPIO5  (RX) ← LD2410D TX
 *   ESP32-C6 GPIO6  (SDA) → MAX30100 SDA  (I²C, 3.3V, with 4.7kΩ pull-up)
 *   ESP32-C6 GPIO7  (SCL) → MAX30100 SCL  (I²C, 3.3V, with 4.7kΩ pull-up)
 *   ESP32-C6 USB    → Host computer (serial over USB)
 *
 * -----------------------------------------------------------------
 * MAX30100 I²C INITIALISATION SEQUENCE
 * -----------------------------------------------------------------
 *   1. Reset:  write 0x40 to register 0x06
 *   2. Mode:   write 0x03 to register 0x06  (SpO₂ + HR mode)
 *   3. SpO₂:   configure register 0x07  (sample rate, pulse width)
 *   4. LEDs:   configure register 0x09  (Red + IR current)
 *   5. Read FIFO (register 0x05) in a loop, 4 bytes per sample:
 *        bytes[0:1] = IR (16-bit big-endian)
 *        bytes[2:3] = Red (16-bit big-endian)
 *   6. Apply DC-removal filter, then send JSON over serial.
 */
