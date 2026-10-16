# Version 1.2 public source status

This is the sanitized source for WoG Storage Helper 1.2. It contains no user
profile, logs, private diagnostic paths or full game screenshots. Recognition
fragments retain their original pixels, crop geometry and source hashes.

Own code and original assets: MIT, Copyright (c) 2026 deniy. Target platform:
64-bit Windows 10/11. The interface has 17 languages; transfers select T3-T6
equipment and gems. Intervals range from 1 minute to 24 hours.

## Changes

- Release identity is 1.2.0, displayed as 1.2 in Settings. Normal GitHub releases
  use the stable installer filename WoGStorageHelperSetup.exe. Earlier releases
  remain separate; the latest-release URL selects the newest regular release.

- Lower clipped-item evidence can authenticate only matching separator lanes.
  The whole visible top rail and top halo must match one existing cue; every
  contributed side lane must independently pass the original raw RMSE limit
  on that same cue. A failed side remains untouched. No asset or tolerance
  changes are used, and the final all-segment grid proof remains mandatory.
- A typed grid-proof failure after both headers are confirmed now keeps a
  separate display reason through the runtime wrappers. The explanation stays
  visible beside the countdown and is labelled as waiting in the event log
  in all 17 languages. Other layout failures retain their existing messages.
- The bounded retry budget, confirmed-panel opener receipts and terminal
  behavior after accepted but unconfirmed input are unchanged. This display
  reason never grants geometry, tier or transfer proof.

## Verification

PUBLIC_TEST_RESULT.json records the isolated synthetic tests run from this
exact exported tree. SOURCE_CONTENT_MANIFEST.json and SOURCE_SHA256SUMS.txt
record the delivered files. The public tests use supplied images, fake native
APIs and fake widgets; they issue no game input. Private native fixtures remain
outside this export.

Before export, 235 distinct waiting/input/UI/translation regression tests and
41 halo tests passed. Replay of 15 recorded problem frames accepts 11 complete
layouts and continues to reject four bright frames. Supplied-frame coordinator
replay recovers all 12 preparation and readback starts using the existing
bounded fresh-frame waits; it issues no opener or item input and is not a native
single-frame acceptance claim. An independent source-native check on 16 October 2026,
14:53:53-14:54:17 UTC, confirmed 12 transfers (one T4 and eleven T3) on the
previously rejected panel arrangement, with no pending input receipt or profile
change. That source-native check is separate from subsequent frozen executable
installation and acceptance; it is not evidence of either. Extended unattended
operation and every monitor or DPI arrangement are not established by these
checks. Private native screenshots are not distributed.

The release is unsigned. The SignPath Foundation application was declined for
insufficient public visibility; no certificate or pending approval is claimed.
Creating this snapshot does not install or publish the application. Earlier
release artifacts are preserved.
