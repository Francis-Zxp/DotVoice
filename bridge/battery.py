"""Baseus input telemetry, verified against BaseusOffice 2.0.1.3 symbols/code.

HidDeviceListener::run reads input[3] into HidDeiveStatus.battery. No HID writes.
"""


def baseus_battery(event):
    if event.get('kind') != 2 or 'VID_ABC8&PID_CA08&MI_03' not in event.get('path', '').upper():
        return None
    try:
        report = bytes.fromhex(event.get('hex', ''))
    except (ValueError, TypeError):
        return None
    if len(report) < 16 or report[:3] != b'\x0a\x01\x00' or report[4] != 255:
        return None
    return report[3] if report[3] <= 100 else None
