# WoG Storage Helper

A local Windows helper for Steam app 4891320. Maintainer: **deniy**.

[Source repository](https://github.com/deniy02020/wog-storage-helper) ·
[Code signing policy](docs/CODE_SIGNING.md) ·
[Download Beta.6](https://github.com/deniy02020/wog-storage-helper/releases/tag/v0.2.0-beta.6)

This source revision prepares the Beta.7 Windows build/signing pipeline.
The released Beta.6 archive is preserved separately; this preparation does
not claim a signed release, SignPath approval or widespread adoption.

Choose any of T3–T6, set an interval of at least 30 minutes, and press Start.
The first cycle begins immediately. Equipment and gems use the same tier
selection; item levels are not filters. T1/T2 are recognised internally but
never transferred. The running view shows the countdown and Stop.

The helper locates the game and its panels automatically. It repeats inventory
sorting and checks lower inventory rows when necessary. Existing storage pages
are detected automatically, from 2 to 6; the purchase button is never a page.
Two confirmed full sweeps stop the timer. After freeing space, press
“Space cleared” to start a fresh check. Stop or F8 cancels the worker.

When you are using another application, the helper waits for the game to become
foreground. After 10 minutes without computer input it may temporarily bring
the game forward and restore the previous window afterwards. It uses ordinary
Windows input and screenshots; it does not inject game code, read game memory,
intercept network traffic or change achievements.

## Run from source

Windows 10/11 and Python with Tkinter are required for the application.
The development environment uses Python 3.13 and the versions in the lockfiles.
Python installation and dependency downloads are setup steps; normal operation
is local and needs no server, account, telemetry or paid API.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -B dashboard.py
```

Source runs create their own local `profile` directory. This snapshot contains
no saved profile or user data. The timer remains off until Start is pressed.
There are 17 interface languages; the system language is detected automatically.

## Public tests

The included tests use generated pixels, fake motion/desktop boundaries and
mock widgets. They do not require the game or send native game input.

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -v
```

`PUBLIC_TEST_RESULT.json`, when present, records the exact public subset run
from this exported tree. The earlier local 913-test development corpus included
withheld private native fixtures and is **not reproduced by this public
snapshot**. Public synthetic passes do not prove clean-Windows installation,
all monitors/DPI settings or extended AFK operation.

## Build application and installer

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build-lock.txt
.\build.bat
.\.venv\Scripts\python.exe -B package_release.py --create
.\build_installer.bat --payload releases\<archive-created-by-package_release>.zip --version 0.2.0-beta.7
```

GitHub Windows CI builds from pinned dependencies and action commits, using
an isolated `.ci-work/beta7-preparation` workspace. Its default output is
explicitly unsigned evidence, with no signing secret required and no automatic
release publication. Builds can be repeated; byte-identical PE/signature output
is not claimed. Approved signing remains a separate, resumable operation.

The application build is an onedir bundle: distribute the complete
`dist\WoGStorageHelper` folder, including `_internal`. The installer builder
takes that locally created, verified release archive; no compiled payload or
installer executable is included here. Building does not publish anything.

## Beta limits and integrity

This is beta software. Successful real-game observations are bounded evidence,
not a guarantee for every computer or future game UI. A healthy but unproved
scroll boundary may allow verified visible progress and keep the chosen timer
with a pending status. Unknown cells never count as proof that the full bag was
checked. Unverified input is not repeated; check the event journal if stopped.

`SOURCE_CONTENT_MANIFEST.json` lists exact exported files, SHA256, image
dimensions and source-copy sanitisation. `SOURCE_SHA256SUMS.txt` is the checksum
index. Recognition fragments are unchanged tiny PNGs; private source filenames
are replaced by `private-source-withheld.png` in exported provenance. That
sentinel is not a missing distribution asset. Original source hashes and crop
geometry remain, but full game frames and session history are withheld.

## Licence

Helper's own code and original assets: [MIT](LICENSE), Copyright (c) 2026 deniy.
Game-derived fragments and dependencies retain their separate rights; see
[ATTRIBUTION.md](ATTRIBUTION.md), [AUTHORS.md](AUTHORS.md) and
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt). No Valve or publisher
endorsement is claimed. Profiles, private diagnostic frames, logs, credentials,
virtual environments and old compiled releases are excluded from this source.
