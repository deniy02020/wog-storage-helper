# Helper-owned one-file GUI setup. Built only from an explicitly supplied release payload.
from pathlib import Path
import os
from installer.license_resources import licensing_datas
from release_identity import pe_version_info

project = Path(SPECPATH).resolve().parent
installer = project / 'installer'
resources = Path(os.environ.get('WOG_HELPER_INSTALLER_RESOURCES', str(installer / 'resources')))
datas = [
    (str(project / 'LICENSE'), '.'),
    (str(project / 'locales.json'), '.'),
    (str(installer / 'installer_locales.json'), 'installer'),
    (str(project / 'assets' / 'wog_storage_helper.png'), 'assets'),
    (str(project / 'assets' / 'wog_storage_helper.ico'), 'assets'),
    (str(resources / 'payload.zip'), 'installer/resources'),
    (str(resources / 'payload_metadata.json'), 'installer/resources'),
    (str(resources / 'uninstaller.exe'), 'installer/resources'),
]
datas.extend(licensing_datas(project))
a = Analysis([str(installer / 'launcher.py')], pathex=[str(project)], binaries=[], datas=datas,
             hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['cv2', 'numpy', 'PIL', 'pytest'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='WoGStorageHelperSetup',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, uac_admin=False,
          icon=str(project / 'assets' / 'wog_storage_helper.ico'), version=pe_version_info('setup'))
