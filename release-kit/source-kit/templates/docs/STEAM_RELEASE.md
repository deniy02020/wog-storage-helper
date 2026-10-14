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

## Beta.9

This update distinguishes a cursor or focus cancellation before a click from
an unconfirmed result after a click. The former can wait and reacquire the game;
the latter stops further input. Panel-opening clicks must also be confirmed
before another attempt. The running view shows opening or waiting accurately.

Profile loading now supports two Windows paths to the same folder when the
filesystem confirms their identity. Existing settings are retained.

Normal operation is local. Settings, logs and game captures stay on the computer.
The program uses ordinary Windows mouse input and screenshots. It does not
read game memory, inject code or intercept network traffic.

The current beta is unsigned, so Windows may show a SmartScreen warning.
If you are unsure, cancel the installation. The SignPath Foundation application
was declined; no signing approval or certificate is claimed.

The included source tests are synthetic. See PUBLIC_TEST_RESULT.json for this
export's result. They do not establish extended unattended operation or every
physical monitor and DPI arrangement.

Maintainer: **deniy**. Own code and original assets use MIT; dependency notices
and game-fragment rights are retained separately. No official endorsement by
Valve or the game's publisher is claimed.
