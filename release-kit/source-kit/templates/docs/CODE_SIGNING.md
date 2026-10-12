# Code signing policy

Maintainer: **deniy**. Source: <https://github.com/deniy02020/wog-storage-helper>.

Beta.6 remains the previously released unsigned build. Beta.7 currently prepares
signing; no certificate, completed signing request or Foundation acceptance is
claimed. There is no automatic GitHub Release publication in these workflows.

The default Windows build needs no signing secrets. It runs the public synthetic
tests, builds the own application, a complete portable onedir archive and Setup,
then stores explicitly unsigned CI evidence. All outputs use a new
`.ci-work/beta7-preparation` directory. Existing releases are not overwritten.
Python/dependency versions and GitHub Actions commit SHAs are pinned; repeatable
commands do not imply byte-identical PE builds or signatures.

## Project responsibilities

| Responsibility | Project member |
| --- | --- |
| Committer and maintainer | [deniy](https://github.com/deniy02020) |
| Reviewer of proposed source/build changes | [deniy](https://github.com/deniy02020) |
| Release approval | [deniy](https://github.com/deniy02020) |

These describe current project responsibilities. Provider account roles,
signing-request approver permissions and required approval arrangements remain
pending provider review and configuration; no provisioned SignPath role is claimed.

## Privacy

The program keeps settings, event logs and captured screenshots on the user's
computer. It does not collect telemetry or transfer those files or other user
information to networked systems unless specifically requested by the user or
the person installing or operating it. Normal game sorting needs no server
or online account.

User-requested downloads and developer build/signing tools are separate online
operations. GitHub hosts source, CI and explicitly uploaded build artifacts;
optional SignPath requests send only the requested build artifacts for signing.
They do not upload the user's local profile, event logs or gameplay captures.
Use of those services is governed by the
[GitHub privacy statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement)
and [SignPath privacy policy](https://about.signpath.io/privacy-policy).

## Approved signing order

1. After the provider has approved the project, configure a protected
   `code-signing` environment and the actual IDs/slugs below. Review the exact
   source commit and build before manually requesting signatures on `main`.
2. Submit only the own app and own uninstaller, uploaded as separate GitHub ZIP
   artifacts. Save actual request IDs. Submission is **pending**, not signed.
3. After approval/completion, run `Resume approved signing requests` with phase
   `prepare-setup` and the original run ID/commit. The script requires the exact
   SignPath project, policy, artifact configuration and trusted GitHub origin.
   The source commit must still equal the workflow run's main commit; if main
   advanced, start a fresh build/request sequence rather than rebinding a receipt.
4. Independently verify Windows trust, code-signing EKU, timestamps, owned PE
   role/version metadata and the same signer. Replace only the own app in its
   complete original onedir bundle, then package the portable ZIP. Embed the
   exact signed app payload and exact signed uninstaller into an unsigned Setup.
5. Submit Setup separately and save its pending receipt. After completion,
   resume with `finalize-setup` using the setup-submission run ID. Recheck all
   three signatures, signer identity, embedded hashes and authentic licences.
   A verified signed result remains evidence for a separate release review.

Pending/denied requests, missing configuration, untrusted signatures, absent
timestamps, wrong roles/versions/origins or changed artifacts stop the sequence.
No unsigned fallback is promoted to a signed result. Foundation signing keys
are not downloaded; no self-signed certificate is generated or installed.
The code does not invent an unsigned-to-signed PE byte identity algorithm:
source binding relies on the provider's trusted GitHub request origin.

## Configuration after provider approval

Repository/environment variables:

- `SIGNPATH_ORGANIZATION_ID`: actual organization UUID.
- `SIGNPATH_PROJECT_SLUG`, `SIGNPATH_SIGNING_POLICY_SLUG`: actual approved slugs.
- `SIGNPATH_APP_CONFIGURATION`, `SIGNPATH_UNINSTALLER_CONFIGURATION`,
  `SIGNPATH_SETUP_CONFIGURATION`: three actual artifact configuration slugs.

Environment secret: `SIGNPATH_API_TOKEN`, scoped to the approved project/policy.
Never commit tokens, certificate keys, contacts or credential files.

Each configuration must use a ZIP root containing exactly one owned PE:
`WoGStorageHelper.exe`, `WoGStorageHelperUninstall.exe`, or
`WoGStorageHelperSetup.exe`, respectively. Dependency DLLs are included in the
portable/setup payload but are not submitted for signing as own project binaries.
Configure provider-required authors/approvers and account security after review;
this document does not claim those roles or MFA settings already exist.

The action submits asynchronously (`wait-for-completion: false`); its download
URL does not prove completion. Resumption uses a completed, origin-verified
request and then performs local Windows verification. Artifacts expire after
14 days; if the required evidence expires, start a fresh run.

Official references: [GitHub integration](https://docs.signpath.io/trusted-build-systems/github),
[request state/origin and download API](https://docs.signpath.io/build-system-integration),
[Foundation conditions](https://signpath.org/terms). These workflows do not
override provider policy or promise SmartScreen reputation.
