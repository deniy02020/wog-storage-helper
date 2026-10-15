# Version 1.0 public source status

This is the sanitized source for WoG Storage Helper 1.0. It contains no user
profile, logs, private diagnostic paths or full game screenshots. Recognition
fragments retain their original pixels, crop geometry and source hashes.

Own code and original assets: MIT, Copyright (c) 2026 deniy. Target platform:
64-bit Windows 10/11. The interface has 17 languages; transfers select T3-T6
equipment and gems. Intervals range from 1 minute to 24 hours.

## Changes

- Release identity is 1.0.0, displayed as 1.0 in Settings. Normal GitHub releases
  use the stable installer filename WoGStorageHelperSetup.exe. Earlier releases
  remain separate; the latest-release URL selects the newest regular release.

- Cursor or focus changes detected before input are distinguished from changes
  after accepted input. Only a cancellation with no outstanding input can wait
  and reacquire the game. Accepted input with no confirmed result stops.
- Opening a game panel now records the issued click until a fresh view proves
  the expected next control or panel. An unconfirmed opener cannot be replayed
  by the two-second retry path.
- The compact running view distinguishes opening panels, checking inventory
  and waiting. A deferred check keeps its reason visible with the countdown.
- Profile readers accept a transparent Windows directory alias only when the
  filesystem proves it is the same directory. A distinct external path is
  still rejected.

## Verification

PUBLIC_TEST_RESULT.json records the isolated synthetic tests run from this
exact exported tree. SOURCE_CONTENT_MANIFEST.json and SOURCE_SHA256SUMS.txt
record the delivered files. The public tests use supplied images, fake native
APIs and fake widgets; they issue no game input. Private native fixtures remain
outside this export.

Native acceptance of the installed 1.0 build is a separate check and is not
claimed by this source export. Extended unattended operation and every monitor
or DPI arrangement are not established by synthetic tests.

The release is unsigned. The SignPath Foundation application was declined for
insufficient public visibility; no certificate or pending approval is claimed.
Creating this snapshot does not install or publish the application. Earlier
release artifacts are preserved.
