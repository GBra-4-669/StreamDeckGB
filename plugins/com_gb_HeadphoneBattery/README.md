# Headphone Battery

Zero-polling Bluetooth headphone battery for StreamDeckGB.

- Tracks the paired **Px7 S2e** via BlueZ `org.bluez.Battery1` signals (GDBus) — no polling at all.
- Big centered number with a "% or no %" switch.
- The number is colored by customizable zones: red / yellow / green thresholds (defaults 10% / 20%), each with a pickable color. No background fill — just the label color.
- No off state: the last known percentage is stored in the action settings, so the key keeps showing a number even when the headphones are powered off.
