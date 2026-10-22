# WoG Storage Helper

Helper automatically transfers your selected equipment and gems to storage on a timer.

[Download for Windows — GitHub Releases](https://github.com/deniy02020/wog-storage-helper/releases/latest) ·
[Steam guide](https://steamcommunity.com/sharedfiles/filedetails/?id=3818783491)

This folder contains **version 1.6 source**. To install Helper, open **GitHub Releases** above and choose **WoGStorageHelperSetup.exe**. The source archive is for reviewing or building the code.

## Version 1.6

Fixes long access pauses after returning to the game and improves inventory recognition when glowing item frames cover the separators between cells.

Recognition runs locally. The installer includes the language models needed for all 17 game languages.

## Getting started

1. Install Helper and open the game.
2. Open Helper, choose the tiers and time between sorts. The defaults are T3–T6 and 30 minutes; choose 1 minute for a quick check.
3. Click **Start**. The first sort begins immediately, then the timer starts.

While running, the view shows the countdown and **Stop**. You can also stop Helper with **F8**.

When you use another app, Helper waits for the game. After 10 minutes without input, it may bring up the already running game and sort your items. New input interrupts its actions.

If storage is full, sorting and the timer stop. Free up space in the game, then click **Space cleared**.

The application is unsigned, so Windows may show a SmartScreen warning. If you're unsure, cancel the installation.

## For developers

Windows and Python 3.13 with Tkinter are required. Dependencies are pinned in the lockfiles.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe ocr_resources.py --fetch-native
.\.venv\Scripts\python.exe -B dashboard.py
```

Run tests:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -v
```

Ordinary launches leave the timer off. An explicit CLI start can use `--start --interval 14400`; it uses the same guards as the Start button. It cannot resume a full-storage pause.

See [docs/STATE.md](docs/STATE.md) for snapshot status and [installer/README.md](installer/README.md) for build instructions. Application and installer builds use `requirements-build-lock.txt`. `PUBLIC_TEST_RESULT.json`, when present, records this export's exact synthetic test run; the private native fixtures are not included.

Maintainer: **deniy**. Own code and original assets: [MIT](LICENSE). Separate rights and notices are retained in [ATTRIBUTION.md](ATTRIBUTION.md) and [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
