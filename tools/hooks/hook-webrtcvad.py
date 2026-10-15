"""The maintained Windows wheel has a different distribution name."""
from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata('webrtcvad-wheels')
hiddenimports = ['_webrtcvad']
