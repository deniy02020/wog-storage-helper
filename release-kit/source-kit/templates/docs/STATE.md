# Version 1.1 public source status

This is the sanitized source for WoG Storage Helper 1.1. It contains no user
profile, logs, private diagnostic paths or full game screenshots. Recognition
fragments retain their original pixels, crop geometry and source hashes.

Own code and original assets: MIT, Copyright (c) 2026 deniy. Target platform:
64-bit Windows 10/11. The interface has 17 languages; transfers select T3-T6
equipment and gems. Intervals range from 1 minute to 24 hours.

## Changes

- Release identity is 1.1.0, displayed as 1.1 in Settings. Normal GitHub releases
  use the stable installer filename WoGStorageHelperSetup.exe. Earlier releases
  remain separate; the latest-release URL selects the newest regular release.

- Transfer confirmation can distinguish a new item arriving elsewhere from
  an unchanged source item. It requires the actual destination badge to match
  the moved tier, and rejects unrelated storage occupancy changes. A physical
  destination fallback uses storage dimensions and scale, never inventory
  dimensions or a conflicting known tier.
- A failed transfer readback records one bounded local diagnostic. It does not
  retry the item input. The event log shows unconfirmed transfers in Attention
  in all 17 interface languages, retaining the original reason in Details.
- Inventory-grid recognition can recover columns from intact rows when long
  item-border runs fail the original shape, pitch or phase checks. The same
  lattice bounds and full-grid checks remain mandatory.

## Verification

PUBLIC_TEST_RESULT.json records the isolated synthetic tests run from this
exact exported tree. SOURCE_CONTENT_MANIFEST.json and SOURCE_SHA256SUMS.txt
record the delivered files. The public tests use supplied images, fake native
APIs and fake widgets; they issue no game input. Private native fixtures remain
outside this export.

Native acceptance of the installed 1.1 build is a separate check and is not
claimed by this source export. Extended unattended operation and every monitor
or DPI arrangement are not established by synthetic tests.

The release is unsigned. The SignPath Foundation application was declined for
insufficient public visibility; no certificate or pending approval is claimed.
Creating this snapshot does not install or publish the application. Earlier
release artifacts are preserved.
