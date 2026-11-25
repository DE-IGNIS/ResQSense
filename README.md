# ResQSense – Disaster Rescue Assistance System

A Python-based disaster rescue assistance system that helps rescue teams identify possible human presence beneath rubble during natural disasters.

## Hardware

| Component | Role |
|-----------|------|
| **ESP32-C6** | Microcontroller bridge (USB-serial to sensors) |
| **HLK-LD2410D** | 24 GHz mmWave presence detection (UART, 256 kbaud) |
| **MAX30100** | Pulse oximeter – heart rate & SpO₂ (I²C, addr `0x57`) |

## Project Structure

```
ResQSense/
├── main.py                        # Application entry point
├── requirements.txt
│
├── src/
│   ├── hardware/                  # Sensor communication layer
│   │   ├── esp32.py               #   ESP32 serial connection & framing
│   │   ├── ld2410d.py             #   LD2410D binary protocol parser & commands
│   │   └── max30100.py            #   MAX30100 JSON parser & vital-sign processor
│   │
│   ├── core/                      # Business logic layer
│   │   ├── detection.py           #   Presence detection state machine & alerts
│   │   ├── sensor_processor.py    #   Validation, EMA filtering, outlier detection
│   │   ├── alerts.py              #   Alert fan-out & history management
│   │   └── backend_service.py     #   Façade API for the frontend
│   │
│   └── database/                  # Persistence layer
│       └── storage.py             #   SQLite: zones, readings, alerts, events
│
├── tests/                         # pytest test suite (66 tests)
├── firmware/                      # ESP32 firmware protocol spec
└── data/                          # Runtime data (SQLite DB)
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Flet Frontend (Himanshu)             │
│         (callbacks & queries via BackendService)         │
└───────────────────────┬─────────────────────────────────┘
                        │
              ┌─────────▼──────────┐
              │  BackendService     │   ← Façade: the only thing the UI touches
              │  (backend_service)  │
              └──┬──────┬───────┬──┘
                 │      │       │
        ┌────────▼┐ ┌───▼────┐ ┌▼──────────┐
        │Detection│ │Pipeline│ │AlertManager│
        │ Engine  │ │(valid/ │ │(fan-out +  │
        │         │ │ filter)│ │ history)   │
        └────┬────┘ └───┬───┘ └────────────┘
             │          │
   ┌─────────▼──────────▼───────────┐
   │      Hardware Layer             │
   │  ESP32Connection → LD2410D     │
   │                  → MAX30100    │
   └─────────────┬──────────────────┘
                 │  USB Serial
           ┌─────▼─────┐
           │  ESP32-C6  │
           │  (sensors) │
           └────────────┘
```

## Detection Levels

| Level | Meaning |
|-------|---------|
| `NO_SIGNAL` | No target detected |
| `EXPERIMENTAL` | Sensor triggered, low confidence — do not act on this alone |
| `PROBABLE` | Sustained radar signal, moderate energy |
| `CONFIRMED` | Corroborated by vitals (MAX30100) or very strong radar |

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run tests
python -m pytest tests/ -v

# Run backend (auto-detect ESP32)
python main.py

# Run with explicit port
python main.py --port /dev/ttyUSB0
```

## Frontend Integration

The UI developer only needs to import and use `BackendService`:

```python
from src.core.backend_service import BackendService

svc = BackendService()

# Register live-data callbacks
svc.on_radar_data(update_radar_widget)
svc.on_vitals_data(update_vitals_widget)
svc.on_alert(show_alert_banner)
svc.on_connection_status(update_status_bar)

# Start (auto-detects ESP32; runs in offline mode if none found)
svc.start()

# Pull data on demand
state = svc.get_detection_state()
zones = svc.list_zones()
history = svc.get_alert_history(limit=20)
vitals = svc.get_last_vitals()
status = svc.get_connection_status()

# Zone management
zone_id = svc.create_zone("Building A", description="Collapsed east wing")
svc.set_active_zone(zone_id)
svc.complete_zone(zone_id)

# Sensor commands
svc.enable_engineering_mode()
svc.set_radar_sensitivity(gate=3, moving=50, stationary=40)

# Shutdown
svc.stop()
```

## Team

- **Sudarsh** – Backend & hardware (this codebase)
- **Himanshu** – UI & Flet application (`src/ui/`, `main.py`)
