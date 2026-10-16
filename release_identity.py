"""Product identity shared by the app and the three owned Windows executables."""
from __future__ import annotations

import os
import re

DEFAULT_RELEASE_VERSION = "1.1.0"
PRODUCT_NAME = "WoG Storage Helper"
AUTHOR = "deniy"
EXECUTABLES = {
    "app": ("WoGStorageHelper.exe", "WoG Storage Helper"),
    "uninstaller": ("WoGStorageHelperUninstall.exe", "WoG Storage Helper Uninstaller"),
    "setup": ("WoGStorageHelperSetup.exe", "WoG Storage Helper Setup"),
}


def release_version(value: str | None = None) -> str:
    version = value if value is not None else os.environ.get("WOG_HELPER_BUILD_VERSION", DEFAULT_RELEASE_VERSION)
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-beta\.[1-9]\d*)?", version):
        raise ValueError("Release version must be MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-beta.N.")
    if any(number > 65535 for number in numeric_version(version)):
        raise ValueError("Windows version components must fit unsigned 16-bit fields.")
    return version


def numeric_version(version: str) -> tuple[int, int, int, int]:
    core, separator, beta = version.partition("-beta.")
    return (*map(int, core.split(".")), int(beta) if separator else 0)


def display_version(version: str | None = None) -> str:
    """Show release series as 1.1; keep nonzero patches and old beta identities.

    The running app uses its source identity, never a launch-time environment
    override intended for build tools.
    """
    version = release_version(DEFAULT_RELEASE_VERSION if version is None else version)
    if "-beta." not in version and version.endswith(".0"):
        return version.rsplit(".", 1)[0]
    return version


def expected_strings(role: str, version: str | None = None) -> dict[str, str]:
    if role not in EXECUTABLES:
        raise ValueError("Only the owned app, uninstaller and setup roles are supported.")
    filename, description = EXECUTABLES[role]
    version = release_version(version)
    return {
        "CompanyName": AUTHOR,
        "FileDescription": description,
        "FileVersion": version,
        "InternalName": filename.removesuffix(".exe"),
        "LegalCopyright": "Copyright (c) 2026 deniy",
        "OriginalFilename": filename,
        "ProductName": PRODUCT_NAME,
        "ProductVersion": version,
    }


def pe_version_info(role: str, version: str | None = None):
    """Return a PyInstaller version resource; no certificate or signing is implied."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo,
    )
    version = release_version(version)
    numeric = numeric_version(version)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numeric, prodvers=numeric,
                          flags=0x2 if "-beta." in version else 0),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct(name, value) for name, value in expected_strings(role, version).items()
        ])]), VarFileInfo([VarStruct("Translation", [0x0409, 1200])])],
    )
