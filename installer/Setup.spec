# Helper-owned one-file GUI setup. Built only from an explicitly supplied release payload.
from pathlib import Path
from installer.license_resources import licensing_datas

project = Path(SPECPATH).resolve().parent
installer = project / 'installer'
datas = [
    (str(project / 'LICENSE'), '.'),
    (str(project / 'locales.json'), '.'),
    (str(installer / 'installer_locales.json'), 'installer'),
    (str(project / 'assets' / 'wog_storage_helper.png'), 'assets'),
    (str(project / 'assets' / 'wog_storage_helper.ico'), 'assets'),
    (str(installer / 'resources' / 'payload.zip'), 'installer/resources'),
    (str(installer / 'resources' / 'payload_metadata.json'), 'installer/resources'),
    (str(installer / 'resources' / 'uninstaller.exe'), 'installer/resources'),
]
datas.extend(licensing_datas(project))
a = Analysis([str(installer / 'launcher.py')], pathex=[str(project)], binaries=[], datas=datas,
             hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['cv2', 'numpy', 'PIL', 'pytest'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='WoGStorageHelperSetup',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, uac_admin=False,
          icon=str(project / 'assets' / 'wog_storage_helper.ico'))
