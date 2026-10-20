# Version 1.4 public source status

WoG Storage Helper 1.4.0 adds offline, message-specific automatic reconnection.
The running scheduler checks for disconnections during the sorting countdown.
The first attempt is five minutes after detection; subsequent attempts are at
least five minutes apart. The exact game title/message, paired buttons, fresh
pixels and normal Windows input guards are required. Generic confirmations are
not reconnect targets. Offline Rewards requires its own title and two labels.

After rejoining, a fresh inventory cycle replaces the old item decision. An
interrupted transfer is not counted as successful. Stop/F8 cancels both sorting
and connection checks. The existing foreground and ten-minute idle policy still
applies. The installer bundles checksum-pinned Tesseract/Leptonica libraries and
17 official tessdata_fast language models, with original upstream notices.
There is no OCR service, account or runtime download. Developers obtain the
unmodified native dependency with `python ocr_resources.py --fetch-native`.

The local checks observed a five-minute automatic reconnect in Russian at 100%,
then a separately tested automatic Offline Rewards close and two confirmed T3
transfers. These are not seventeen native reconnect tests. PUBLIC_TEST_RESULT.json
records this exact export's isolated tests. Installed executable acceptance is a
separate release gate. Private full game frames and logs are excluded.

## Earlier 1.3 observations (preserved)

WoG Storage Helper 1.3.0 for Windows 10/11 x64. Interface translations cover
17 languages. Only selected T3-T6 equipment and gems are transferred.

Panel recognition now has paired language-neutral icon models at three genuine
native raster sizes. Stock Russian caption matching remains compatible with
existing profiles. Translated headings fall back to the icon pairs; fresh
validation repeats the accepted icon proof and independently proves the grids.
The stock storage opener uses the house icon rather than its translated caption.
Custom calibration samples retain their original matching behavior.

PUBLIC_TEST_RESULT.json records the isolated offline test run from this exact
export. Synthetic compositions are not native all-language or all-DPI tests.
Private native screenshots and diagnostic logs are intentionally not distributed.
Native checks have verified the icon path and both grids in multiple game
languages at the game's 50% window setting. English also has a genuine 100%
capture check. These observations do not establish every monitor/DPI combination.
Small-raster motion uses a second bounded symmetric kernel while retaining the
original alias, direction, phase and score limits. Small grid borders require
one common visible separator line across every cell segment.
Installed executable acceptance is a separate release gate. Do not infer it
from this source snapshot or its offline tests.

The release is unsigned. The SignPath Foundation application was declined for
insufficient public visibility; no certificate or pending approval is claimed.
Own code and original assets: MIT, Copyright (c) 2026 deniy. Tiny game-derived
recognition crops retain their separate notices. Profiles and full frames are
excluded. Earlier release artifacts remain preserved.
