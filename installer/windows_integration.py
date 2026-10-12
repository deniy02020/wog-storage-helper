"""Ordinary per-user shortcuts/Apps registration, executed only by wizard actions."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys

from installer.core import (EXECUTABLE, MARKER, PRODUCT_DIRECTORY, UNINSTALLER,
                            IntegrationReceipt, InstallError, file_hash, safe_relative, validate_target)

REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\WoGStorageHelper"
DISPLAY_NAME = "WoG Storage Helper"


def ps_literal(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def run_powershell(script: str):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            capture_output=True, timeout=20)
    if result.returncode:
        raise InstallError("Windows could not complete the requested shortcut operation.")


def create_shortcut(path: Path, executable: Path, working_directory: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    run_powershell("$ErrorActionPreference='Stop'; "
                   "$shell=New-Object -ComObject WScript.Shell; "
                   f"$link=$shell.CreateShortcut({ps_literal(path)}); "
                   f"$link.TargetPath={ps_literal(executable)}; "
                   f"$link.WorkingDirectory={ps_literal(working_directory)}; "
                   f"$link.IconLocation={ps_literal(str(executable) + ',0')}; "
                   "$link.Description='WoG Storage Helper'; $link.Save()")


def shell_folder(csidl: int) -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    function = ctypes.WinDLL("shell32", use_last_error=True).SHGetFolderPathW
    function.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
    function.restype = ctypes.c_long
    if function(None, csidl, None, 0, buffer) != 0 or not buffer.value:
        raise InstallError("Windows did not provide the current user's shortcut directory.")
    return Path(buffer.value)


def helper_is_running() -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenMutexW(0x00100000, False, "Local\\WoGStorageHelper")
    if handle:
        kernel.CloseHandle(handle)
        return True
    return ctypes.get_last_error() != 2


class WindowsIntegration:
    def __init__(self, *, desktop=False, start_menu=True, enabled=True,
                 version="local-beta", folder_lookup=shell_folder, shortcut_writer=create_shortcut,
                 registry=None):
        self.desktop, self.start_menu, self.enabled = desktop, start_menu, enabled
        self.version = version
        self.folder_lookup, self.shortcut_writer = folder_lookup, shortcut_writer
        self.registry = registry

    def _registry(self):
        if self.registry is None:
            import winreg
            self.registry = winreg
        return self.registry

    def _read_registry(self):
        reg = self._registry()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
                values, index = {}, 0
                while True:
                    try:
                        name, value, kind = reg.EnumValue(key, index)
                    except OSError:
                        break
                    values[name] = (value, kind)
                    index += 1
                return values
        except FileNotFoundError:
            return None

    def _write_registry(self, values):
        reg = self._registry()
        if values is None:
            try:
                reg.DeleteKey(reg.HKEY_CURRENT_USER, REGISTRY_KEY)
            except FileNotFoundError:
                pass
            return
        current = self._read_registry() or {}
        with reg.CreateKey(reg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            for name in set(current) - set(values):
                reg.DeleteValue(key, name)
            for name, (value, kind) in values.items():
                reg.SetValueEx(key, name, 0, kind, value)

    def _paths(self):
        return {"desktop": self.folder_lookup(0x10) / "WoG Storage Helper.lnk",
                "start_menu": self.folder_lookup(0x02) / DISPLAY_NAME / "WoG Storage Helper.lnk",
                "uninstall": self.folder_lookup(0x02) / DISPLAY_NAME / "Uninstall.lnk"}

    def apply(self, target: Path, previous: dict | None = None, *, removing=False) -> IntegrationReceipt:
        if not self.enabled:
            return IntegrationReceipt({}, lambda: None)
        paths = self._paths()
        old_state = (previous or {}).get("integration", {})
        old_hashes = old_state.get("shortcut_hashes", {})
        desired = set() if removing else (({"desktop"} if self.desktop else set()) |
                                         ({"start_menu", "uninstall"} if self.start_menu else set()))
        affected = desired | (set(old_hashes) & set(paths))
        saved_files = {}
        saved_registry = self._read_registry()
        if saved_registry and saved_registry.get("InstallLocation", (None,))[0] != str(target):
            raise InstallError("The existing application registration belongs to another installation.")
        for identifier in affected:
            path = paths[identifier]
            if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
                raise InstallError("Linked shortcut files are not supported.")
            if path.exists():
                if not path.is_file() or old_hashes.get(identifier) != file_hash(path):
                    raise InstallError("An existing shortcut is not owned by this installation.")
                saved_files[path] = path.read_bytes()
            else:
                saved_files[path] = None

        def undo():
            for path, content in saved_files.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
            self._write_registry(saved_registry)

        try:
            for identifier in affected:
                path = paths[identifier]
                if identifier in desired:
                    executable = target / (UNINSTALLER if identifier == "uninstall" else EXECUTABLE)
                    self.shortcut_writer(path, executable, target)
                else:
                    path.unlink(missing_ok=True)
            if removing:
                self._write_registry(None)
            else:
                reg = self._registry()
                values = {"DisplayName": (DISPLAY_NAME, reg.REG_SZ),
                          "DisplayVersion": (self.version, reg.REG_SZ),
                          "InstallLocation": (str(target), reg.REG_SZ),
                          "DisplayIcon": (str(target / EXECUTABLE), reg.REG_SZ),
                          "UninstallString": ('"' + str(target / UNINSTALLER) + '"', reg.REG_SZ),
                          "NoModify": (1, reg.REG_DWORD), "NoRepair": (1, reg.REG_DWORD)}
                self._write_registry(values)
            return IntegrationReceipt({"shortcut_hashes": {identifier: file_hash(paths[identifier])
                                                            for identifier in desired}}, undo)
        except Exception:
            undo()
            raise


def deferred_cleanup_script(target: Path, parent: Path, deferred: dict[str, str], pid: int) -> str:
    """One native PowerShell cleanup, strictly limited to verified installer-owned files."""
    target = validate_target(target, parent)
    if set(deferred) - {UNINSTALLER, MARKER}:
        raise InstallError("Unexpected deferred cleanup file.")
    lines = ["$ErrorActionPreference='Stop'", f"Wait-Process -Id {int(pid)} -ErrorAction SilentlyContinue"]
    # Check these exact directories again after the process-exit wait. A changed
    # local junction must not redirect delayed deletion into a different tree.
    lines.extend([
        "function Test-WogDirectories {",
        f" foreach ($wogFolder in @({ps_literal(Path(parent).resolve())}, {ps_literal(target)})) {{",
        "  if (!(Test-Path -LiteralPath $wogFolder -PathType Container)) { return $false }",
        "  $wogItem = Get-Item -LiteralPath $wogFolder -Force",
        "  if (($wogItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }",
        " }",
        " return $true",
        "}",
        "if (!(Test-WogDirectories)) { return }",
    ])
    for name, digest in deferred.items():
        safe_relative(name)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise InstallError("Invalid deferred cleanup checksum.")
        literal = ps_literal(target / name)
        # A one-file PyInstaller bootloader parent can briefly keep the executable
        # open after its Python child exits. Retry that exact owned file only.
        lines.extend([
            "$wogDeadline = [DateTime]::UtcNow.AddSeconds(15)",
            f"while (Test-Path -LiteralPath {literal} -PathType Leaf) {{",
            " if (!(Test-WogDirectories)) { return }",
            f" if ((Get-FileHash -LiteralPath {literal} -Algorithm SHA256).Hash -ne '{digest}') {{ break }}",
            f" try {{ Remove-Item -LiteralPath {literal} -Force -ErrorAction Stop; break }}",
            " catch {",
            "  if ([DateTime]::UtcNow -ge $wogDeadline) { return }",
            "  Start-Sleep -Milliseconds 200",
            " }",
            "}",
        ])
    root_literal = ps_literal(target)
    lines.append(f"if ((Test-WogDirectories) -and (Test-Path -LiteralPath {root_literal} -PathType Container) -and "
                 f"((Get-ChildItem -LiteralPath {root_literal} -Force | Measure-Object).Count -eq 0)) "
                 f"{{ Remove-Item -LiteralPath {root_literal} }}")
    return "\n".join(lines)


def schedule_deferred_cleanup(target: Path, parent: Path, deferred: dict[str, str], pid: int):
    script = deferred_cleanup_script(target, parent, deferred, pid)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                      "-EncodedCommand", encoded], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
