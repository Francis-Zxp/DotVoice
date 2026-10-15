"""Read cached HID capabilities only; never send a device command."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ctypes as c
from ctypes import wintypes as w
import json
from bridge.rawinput import devices, user32


class Caps(c.Structure):
    _fields_ = [("usage", w.USHORT), ("page", w.USHORT), ("input", w.USHORT),
                ("output", w.USHORT), ("feature", w.USHORT), ("reserved", w.USHORT * 17),
                ("links", w.USHORT), ("ib", w.USHORT), ("iv", w.USHORT), ("id", w.USHORT),
                ("ob", w.USHORT), ("ov", w.USHORT), ("od", w.USHORT),
                ("fb", w.USHORT), ("fv", w.USHORT), ("fd", w.USHORT)]


class Value(c.Structure):
    _fields_ = [("page", w.USHORT), ("report", c.c_ubyte), ("alias", c.c_ubyte),
                ("field", w.USHORT), ("link", w.USHORT), ("link_usage", w.USHORT),
                ("link_page", w.USHORT), ("flags", c.c_ubyte * 6), ("bits", w.USHORT),
                ("count", w.USHORT), ("reserved", w.USHORT * 5), ("units_exp", w.ULONG),
                ("units", w.ULONG), ("min", w.LONG), ("max", w.LONG),
                ("pmin", w.LONG), ("pmax", w.LONG), ("usage", w.USHORT * 8)]


hid = c.WinDLL('hid')
hid.HidP_GetCaps.argtypes = [c.c_void_p, c.POINTER(Caps)]
hid.HidP_GetValueCaps.argtypes = [c.c_int, c.POINTER(Value), c.POINTER(w.USHORT), c.c_void_p]
result = []
for device in devices():
    n = w.UINT()
    user32.GetRawInputDeviceInfoW(device['handle'], 0x20000005, None, c.byref(n))
    if not n.value:
        continue
    data = c.create_string_buffer(n.value)
    user32.GetRawInputDeviceInfoW(device['handle'], 0x20000005, data, c.byref(n))
    caps = Caps()
    status = hid.HidP_GetCaps(data, c.byref(caps))
    item = {'path': device['path'], 'caps_status': hex(status), 'page': caps.page,
            'usage': caps.usage, 'input_bytes': caps.input, 'output_bytes': caps.output,
            'feature_bytes': caps.feature, 'values': []}
    for report_type, count in [(0, caps.iv), (2, caps.fv)]:
        if not count:
            continue
        values = (Value * count)()
        size = w.USHORT(count)
        status = hid.HidP_GetValueCaps(report_type, values, c.byref(size), data)
        for value in values[:size.value]:
            item['values'].append({'type': report_type, 'page': value.page,
                                   'usage_min': value.usage[0],
                                   'usage_max': value.usage[1] if value.flags[0] else value.usage[0],
                                   'report': value.report, 'bits': value.bits,
                                   'count': value.count, 'min': value.min, 'max': value.max})
    result.append(item)
print(json.dumps(result, indent=2))
