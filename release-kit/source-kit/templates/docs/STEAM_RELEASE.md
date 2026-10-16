# Using WoG Storage Helper

Helper transfers selected T3-T6 equipment and gems to storage on a timer.
Install it, open the game, choose the tiers and interval, then press **Start**.
The first sort starts immediately. Later sorts use your chosen interval,
from 1 minute to 24 hours. The default is 30 minutes.

The running view shows the countdown and **Stop**. **F8** also stops Helper.
When you use another application, Helper waits for the game. After 10 minutes
without input, it may bring up the already running game to sort your items.
New input interrupts its actions.

If storage is full, sorting and the timer stop. Free up space in the game,
then click **Space cleared** to continue.

## Version 1.2

The app version is shown in Settings. The installer is named
WoGStorageHelperSetup.exe on GitHub Releases.

This update improves inventory recognition when a partly visible row of items
meets the grid separators. The complete item rows still require confirmation.

When item slots cannot be recognised, the explanation remains visible beside
the countdown. It is translated in all 17 languages and labelled as waiting
in the event log, rather than asking for panels that are already visible.

If a transfer cannot be confirmed, Helper stops further input. The event is
shown in the journal's Attention filter; Details retains the original reason.

Normal operation is local. Settings, logs and game captures stay on the computer.
The program uses ordinary Windows mouse input and screenshots. It does not
read game memory, inject code or intercept network traffic.

The application is unsigned, so Windows may show a SmartScreen warning.
If you are unsure, cancel the installation. The SignPath Foundation application
was declined; no signing approval or certificate is claimed.

The included source tests are synthetic. See PUBLIC_TEST_RESULT.json for this
export's result. They do not establish extended unattended operation or every
physical monitor and DPI arrangement.

Maintainer: **deniy**. Own code and original assets use MIT; dependency notices
and game-fragment rights are retained separately. No official endorsement by
Valve or the game's publisher is claimed.
