"""No console, no elevation, and no installation until the wizard's button."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from pathlib import Path

from installer.wizard import Wizard


class InstallerInstance:
    def __init__(self, kernel=None, last_error=None):
        self.kernel = kernel
        self.last_error = last_error or ctypes.get_last_error
        self.handle = None

    def acquire(self):
        if self.handle:
            return True
        kernel = self.kernel or ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel = kernel
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        ctypes.set_last_error(0)
        handle = kernel.CreateMutexW(None, False, "Local\\WoGStorageHelperInstaller")
        if not handle:
            raise OSError(self.last_error(), "Windows could not open the installer instance guard.")
        if self.last_error() == 183:
            kernel.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            self.kernel.CloseHandle(handle)


def main(*, uninstalling=False, argv=None):
    parser = argparse.ArgumentParser(description="WoG Storage Helper per-user setup")
    parser.add_argument("--install-root", type=Path, help="Isolated parent directory; target is its WoGStorageHelper child.")
    parser.add_argument("--no-integrations", action="store_true", help="Do not create shortcuts or Apps registration.")
    parser.add_argument("--review", action="store_true", help="Preview only; installation and deletion stay disabled.")
    parser.add_argument("--language", default="ru")
    args = parser.parse_args(argv)
    guard = InstallerInstance()
    if not guard.acquire():
        return 0
    try:
        wizard = Wizard(uninstalling=uninstalling, install_root=args.install_root,
                        integrations=not args.no_integrations, review=args.review, language=args.language)
        wizard.mainloop()
        return 0
    finally:
        guard.close()


if __name__ == "__main__":
    raise SystemExit(main())

