# SignPath Foundation application draft

This document retains a factual application draft as a reference for a possible
future submission. The original Foundation application was declined at this
stage in the provider response reported by the maintainer on 13 October 2026
because the project lacked sufficient public visibility and independent trust
signals. This reference does not record a new submission, provider agreement,
account verification or signing approval.

- Project: **WoG Storage Helper**.
- Public maintainer/author: **deniy**; repository owner: **deniy02020**.
- Category: **Gaming**.
- Homepage/source: <https://github.com/deniy02020/wog-storage-helper>.
- Platform: Windows; Python/Tkinter with pinned dependencies and PyInstaller.
- Own source/original product assets: MIT, Copyright (c) 2026 deniy.
- Reputation: a new project with bounded local tests and user observations;
  no widespread adoption, established download count or security reputation is claimed.
- Download URL/provider: use the actual public download/release page when it
  exists and can be fetched; repository source and unsigned CI evidence alone
  are not described as an accepted signed release.

## Functionality

A local user-controlled companion for **[WoG] War of Genesis: Idle Loot**
(Steam app [4891320](https://store.steampowered.com/app/4891320/)). Users select
T3–T6 and a timer; Start begins immediately. It locates the game window, reads
screenshots and uses ordinary Windows mouse input to sort and transfer selected
equipment/gems into existing storage pages. Stop/F8 cancels automation. It does
not inject game code, read game memory, intercept traffic, write achievements,
collect telemetry or require an online account for normal operation.

## Materials and review question

Only the own app, own uninstaller and own Setup are intended for project
signatures. Authentic third-party notices accompany dependencies. Small
game-derived image matching fragments retain separate game rights, as disclosed
in `ATTRIBUTION.md`; the maintainer authorized their inclusion and distribution.
Independent publisher permission has not been verified. This does not make the
fragments MIT or establish publisher endorsement. Please review their eligibility under Foundation conditions before
accepting the application. No game executable or full gameplay capture is bundled.

## Private fields and prerequisites

Account first/last name and authorized contact email must be entered privately
in the provider form and are omitted from the public source. GitHub MFA was
reported enabled by the maintainer; this was not observed via an account API.
SignPath MFA, appointed approvers, organization/project/policy IDs and certificate
approval remain unestablished. Complete those requirements with real values.
Review the completed form and provider terms before the final per-action consent.

Before submission, attach the actual successful Windows CI run and public source
revision. Test passes are not a clean-system or broad hardware validation claim.
See [code signing policy](CODE_SIGNING.md) and
[official Foundation application](https://signpath.org/apply).
