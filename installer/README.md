# Local Windows installer source

The wizard builds from an explicitly provided, complete application release
archive. No executable/payload is included in this source snapshot.

Prepare the pinned build environment and application bundle first:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build-lock.txt
.\build.bat
.\.venv\Scripts\python.exe -B package_release.py --create
.\build_installer.bat --payload releases\<created-archive>.zip --version 1.3.0
```

The builder creates a branded GUI setup and a separate uninstaller. The normal
install uses a per-user directory and existing user-level Windows integrations.
Update/uninstall preserve the user profile and unmanaged or changed files.
The source build does not install or publish the result.

Application/dependency licence notices accompany the wrapper. Clean-system
installation, update/removal, shortcuts and signing are distinct checks;
synthetic public tests are not evidence that those checks ran on every computer.
