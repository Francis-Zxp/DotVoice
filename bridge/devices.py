"""Device identities, selected explicitly. No default microphone fallback."""
PROFILES = {
    'baseus': {'id': 'baseus', 'name': '倍思 BS-OH085', 'short': 'BS-OH085',
               'identity': 'VID_ABC8&PID_CA08', 'microphone': 'BaseusPresent',
               'validation': '长按 / 短按已实测', 'modes': ['native']},
    'pv60': {'id': 'pv60', 'name': '汉王 PV60', 'short': 'PV60',
             'identity': 'VID_0611&PID_3001', 'microphone': 'Ai Pointer',
             'validation': '协议适配 · 等待实机验收', 'modes': ['hold', 'toggle']},
}


def get_profile(device):
    if device not in PROFILES:
        raise ValueError('不支持的设备型号')
    return PROFILES[device]


def available_profiles(found=None):
    """`found` is a RawMonitor device list; enumerate only when none is given."""
    if found is None:
        from .rawinput import devices
        found = devices(all_supported=True)
    paths = [d['path'].upper() for d in found]
    return [{**value, 'present': any(value['identity'] in path for path in paths)}
            for value in PROFILES.values()]
