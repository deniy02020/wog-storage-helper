"""Explicit build-time Authenticode gates. Never creates certificates or changes trust.

Verification reads PE metadata and Windows trust. It never launches the target EXE.
Cloud signing is asynchronous: finalization accepts only the returned signed bytes.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit
from zipfile import ZipFile

from installer.core import EXECUTABLE, PRODUCT_DIRECTORY, InstallError, file_hash
from release_identity import expected_strings, release_version

CODE_SIGNING_OID = "1.3.6.1.5.5.7.3.3"
THUMBPRINT = re.compile(r"[A-Fa-f0-9]{40}")


def _powershell() -> Path:
    if os.name != "nt":
        raise InstallError("Authenticode verification requires Windows.")
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
        raise OSError("Windows did not return its system directory.")
    path = Path(buffer.value) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not path.is_file():
        raise InstallError("The built-in Windows PowerShell verifier is unavailable.")
    return path


def query_windows_trust(path: Path) -> dict:
    target = str(Path(path).resolve(strict=True)).replace("'", "''")
    script = """$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding;
Import-Module ($PSHOME+'\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1');
Import-Module ($PSHOME+'\\Modules\\Microsoft.PowerShell.Management\\Microsoft.PowerShell.Management.psd1');
$target='TARGET';
$sig=Get-AuthenticodeSignature -LiteralPath $target;
$vi=(Get-Item -LiteralPath $target).VersionInfo;
$eku=@($sig.SignerCertificate.Extensions | Where-Object {$_.Oid.Value -eq '2.5.29.37'} |
  ForEach-Object {$_.EnhancedKeyUsages} | ForEach-Object {$_.Value});
@{status=$sig.Status.ToString();signer_thumbprint=$sig.SignerCertificate.Thumbprint;
  signer_subject=$sig.SignerCertificate.Subject;timestamp_thumbprint=$sig.TimeStamperCertificate.Thumbprint;
  eku=$eku;CompanyName=$vi.CompanyName;FileDescription=$vi.FileDescription;
  FileVersion=$vi.FileVersion;InternalName=$vi.InternalName;LegalCopyright=$vi.LegalCopyright;
  OriginalFilename=$vi.OriginalFilename;ProductName=$vi.ProductName;ProductVersion=$vi.ProductVersion} |
  ConvertTo-Json -Compress -Depth 5
""".replace("TARGET", target)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run([str(_powershell()), "-NoLogo", "-NoProfile", "-NonInteractive",
                                 "-EncodedCommand", encoded], check=True, capture_output=True,
                                encoding="utf-8", timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise InstallError("Windows could not complete its built-in signature verification.") from error
    try:
        document = json.loads(result.stdout.lstrip("\ufeff"))
    except (ValueError, TypeError) as error:
        raise InstallError("Windows returned an invalid signature report.") from error
    if not isinstance(document, dict):
        raise InstallError("Windows returned an invalid signature report.")
    return document


def verify_owned_executable(path: Path, *, role: str, version: str,
                            expected_signer: str | None = None, query=query_windows_trust) -> dict:
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".exe":
        raise InstallError("The signing gate requires an existing executable file.")
    expected = expected_strings(role, version)
    before = file_hash(path)
    document = query(path)
    if document.get("status") != "Valid":
        raise InstallError("Authenticode signature is missing or not trusted by Windows.")
    signer = document.get("signer_thumbprint")
    timestamp = document.get("timestamp_thumbprint")
    if not isinstance(signer, str) or not THUMBPRINT.fullmatch(signer):
        raise InstallError("The signing certificate thumbprint is invalid.")
    if not isinstance(timestamp, str) or not THUMBPRINT.fullmatch(timestamp):
        raise InstallError("A verified timestamp signer is required for release artifacts.")
    if not isinstance(document.get("eku"), list) or CODE_SIGNING_OID not in document["eku"]:
        raise InstallError("The certificate is not declared for code signing.")
    if expected_signer is not None and (
            not isinstance(expected_signer, str) or not THUMBPRINT.fullmatch(expected_signer)
            or signer.upper() != expected_signer.upper()):
        raise InstallError("The artifacts must use the same signing certificate.")
    if any(document.get(name) != value for name, value in expected.items()):
        raise InstallError("Signed PE product metadata does not match the owned release identity.")
    if file_hash(path) != before:
        raise InstallError("The executable changed during signature verification.")
    return {"role": role, "version": release_version(version), "sha256": before,
            "status": "Valid", "signer_thumbprint": signer.upper(),
            "signer_subject": document.get("signer_subject", ""),
            "timestamp_thumbprint": timestamp.upper(), "pe_metadata": expected}


def verify_signed_payload(payload: Path, *, version: str, expected_signer: str | None = None,
                          verifier=verify_owned_executable) -> dict:
    """Inspect only the own app EXE from the ZIP, without running extracted code."""
    payload = Path(payload).resolve(strict=True)
    before = file_hash(payload)
    with ZipFile(payload) as archive, tempfile.TemporaryDirectory(prefix="wog-signature-") as directory:
        name = PRODUCT_DIRECTORY + "/" + EXECUTABLE
        entries = [entry for entry in archive.infolist() if entry.filename == name]
        if len(entries) != 1 or not 1 <= entries[0].file_size <= 100_000_000:
            raise InstallError("The payload must contain exactly one bounded owned app executable.")
        target = Path(directory) / EXECUTABLE
        target.write_bytes(archive.read(entries[0]))
        report = verifier(target, role="app", version=version, expected_signer=expected_signer)
    if file_hash(payload) != before:
        raise InstallError("The ZIP changed during app signature verification.")
    return report


class CertificateStoreSigner:
    """Optional local signer using an existing certificate, never a private-key file.

    An explicitly selected Microsoft-signed SignTool is required. No SDK is installed.
    """
    def __init__(self, *, signtool: Path, thumbprint: str, timestamp_url: str,
                 query=query_windows_trust):
        self.signtool = Path(signtool).resolve(strict=True)
        if self.signtool.name.lower() != "signtool.exe" or not self.signtool.is_file():
            raise InstallError("Select an existing Windows SDK SignTool executable.")
        if not isinstance(thumbprint, str) or not THUMBPRINT.fullmatch(thumbprint):
            raise InstallError("Select an existing certificate by its exact SHA1 thumbprint.")
        url = urlsplit(timestamp_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
            raise InstallError("Use an explicit HTTPS timestamp service URL without credentials.")
        tool_before = file_hash(self.signtool)
        trust = query(self.signtool)
        subject = trust.get("signer_subject", "")
        if (trust.get("status") != "Valid"
                or not isinstance(subject, str)
                or not re.search(r"(?:^|,\s*)CN=Microsoft Corporation(?:,|$)", subject)
                or not re.search(r"(?:^|,\s*)O=Microsoft Corporation(?:,|$)", subject)
                or not isinstance(trust.get("eku"), list) or CODE_SIGNING_OID not in trust["eku"]
                or file_hash(self.signtool) != tool_before):
            raise InstallError("The chosen SignTool must have a valid Microsoft signature.")
        self.tool_hash, self.thumbprint, self.timestamp_url = tool_before, thumbprint.upper(), timestamp_url

    def __call__(self, path: Path):
        if file_hash(self.signtool) != self.tool_hash:
            raise InstallError("The verified Microsoft SignTool changed before signing.")
        subprocess.run([str(self.signtool), "sign", "/sha1", self.thumbprint,
                        "/fd", "SHA256", "/tr", self.timestamp_url, "/td", "SHA256",
                        str(Path(path).resolve(strict=True))], check=True, timeout=60)


def finalize_setup(setup: Path, *, metadata: Path, payload: Path, uninstaller: Path,
                   version: str, output: Path, verifier=verify_owned_executable,
                   payload_verifier=verify_signed_payload, archive_factory=None,
                   expected_signer: str | None = None) -> dict:
    """Verify returned cloud-signed Setup before replacing unsigned build hashes."""
    from installer.license_resources import verify_wrapper_licenses
    from PyInstaller.archive.readers import CArchiveReader
    version = release_version(version)
    original = json.loads(Path(metadata).read_text(encoding="utf-8"))
    if (original.get("signing_state") != "awaiting_setup_signature"
            or original.get("version") != version):
        raise InstallError("Finalization requires this version's pending signed-input build report.")
    options = {"version": version}
    if expected_signer is not None:
        if not isinstance(expected_signer, str) or not THUMBPRINT.fullmatch(expected_signer):
            raise InstallError("The expected signer pin must be an exact certificate thumbprint.")
        options["expected_signer"] = expected_signer
    app = payload_verifier(payload, **options)
    signer = app["signer_thumbprint"]
    if expected_signer is not None and signer.upper() != expected_signer.upper():
        raise InstallError("The artifacts do not use the pinned signing certificate.")
    remove = verifier(uninstaller, role="uninstaller", version=version, expected_signer=signer)
    install = verifier(setup, role="setup", version=version, expected_signer=signer)
    if (original.get("sha256") != file_hash(Path(payload)) or original.get("exe_sha256") != app["sha256"]
            or original.get("uninstaller_sha256") != remove["sha256"]):
        raise InstallError("Signed Setup inputs differ from the verified build report.")
    archive = (archive_factory or CArchiveReader)(str(setup))
    names = {name.replace("\\", "/"): name for name in archive.toc}
    if len(names) != len(archive.toc):
        raise InstallError("Signed Setup contains duplicate normalized resources.")
    def extract(name):
        if name not in names:
            raise InstallError("Signed Setup is missing an expected embedded resource.")
        return archive.extract(names[name])
    embedded_metadata = json.loads(extract("installer/resources/payload_metadata.json"))
    if (embedded_metadata.get("sha256") != original["sha256"]
            or embedded_metadata.get("exe_sha256") != app["sha256"]
            or embedded_metadata.get("uninstaller_sha256") != remove["sha256"]
            or embedded_metadata.get("version") != version
            or hashlib.sha256(extract("installer/resources/payload.zip")).hexdigest() != original["sha256"]
            or hashlib.sha256(extract("installer/resources/uninstaller.exe")).hexdigest() != remove["sha256"]):
        raise InstallError("Signed Setup does not embed the exact verified signed input artifacts.")
    licenses = verify_wrapper_licenses(Path(setup), original["license_sha256"])
    if file_hash(Path(setup)) != install["sha256"]:
        raise InstallError("Signed Setup changed during its embedded resource audit.")
    result = {**original, "signing_state": "signed", "signatures": {"app": app, "uninstaller": remove, "setup": install},
              "setup": str(Path(setup).resolve()), "setup_sha256": install["sha256"],
              "setup_bytes": Path(setup).stat().st_size, "setup_licenses": licenses}
    with Path(output).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify", type=Path)
    mode.add_argument("--sign", type=Path)
    mode.add_argument("--finalize-setup", type=Path)
    parser.add_argument("--role", choices=("app", "uninstaller", "setup"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-signer")
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--uninstaller", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--signtool", type=Path)
    parser.add_argument("--certificate-thumbprint")
    parser.add_argument("--timestamp-url")
    args = parser.parse_args(argv)
    final_options = (args.metadata, args.payload, args.uninstaller, args.output)
    tool_options = (args.signtool, args.certificate_thumbprint, args.timestamp_url)
    if args.sign:
        if any(value is None for value in (args.role, args.signtool, args.certificate_thumbprint, args.timestamp_url)):
            parser.error("--sign requires --role, --signtool, --certificate-thumbprint and --timestamp-url")
        if any(value is not None for value in final_options):
            parser.error("Setup finalization options cannot be used with --sign.")
        if args.expected_signer is not None and args.expected_signer.upper() != args.certificate_thumbprint.upper():
            parser.error("--expected-signer must match the explicitly selected certificate thumbprint.")
        identity = query_windows_trust(args.sign)
        if any(identity.get(name) != value for name, value in expected_strings(args.role, args.version).items()):
            raise InstallError("Only an executable with this owned release identity may be signed.")
        signer = CertificateStoreSigner(signtool=args.signtool, thumbprint=args.certificate_thumbprint,
                                         timestamp_url=args.timestamp_url)
        signer(args.sign)
        report = verify_owned_executable(args.sign, role=args.role, version=args.version,
                                         expected_signer=args.certificate_thumbprint)
    elif args.verify:
        if args.role is None:
            parser.error("--verify requires --role")
        if any(value is not None for value in (*final_options, *tool_options)):
            parser.error("Signing and finalization options cannot be used with --verify.")
        report = verify_owned_executable(args.verify, role=args.role, version=args.version,
                                         expected_signer=args.expected_signer)
    else:
        if args.role is not None or any(value is not None for value in tool_options):
            parser.error("Signing tool and role options cannot be used with --finalize-setup.")
        if any(value is None for value in (args.metadata, args.payload, args.uninstaller, args.output)):
            parser.error("--finalize-setup requires --metadata, --payload, --uninstaller and --output")
        report = finalize_setup(args.finalize_setup, metadata=args.metadata, payload=args.payload,
                                uninstaller=args.uninstaller, version=args.version, output=args.output,
                                expected_signer=args.expected_signer)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
