"""
get_intermediate.py
One-off helper: fetches dsebd.org's leaf certificate, finds the missing
intermediate via its Authority Information Access (AIA) extension,
downloads it, and writes a merged CA bundle (certifi + intermediate) to
dse-bundle.pem in the current directory.

Run once:
    pip install cryptography
    python get_intermediate.py

Then point the bot at the result (cmd.exe):
    set DSE_CA_BUNDLE=<full path printed at the end>
    python bot.py

Or PowerShell:
    $env:DSE_CA_BUNDLE = "<full path printed at the end>"
    python bot.py
"""

import socket
import ssl
import sys
import urllib.request
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

HOST = "www.dsebd.org"
PORT = 443
OUT_PATH = Path("dse-bundle.pem")


def get_leaf_cert_der() -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # we only want to inspect it, not trust it
    with socket.create_connection((HOST, PORT), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=HOST) as ssock:
            return ssock.getpeercert(binary_form=True)


def main() -> None:
    print(f"Connecting to {HOST}:{PORT} ...")
    der = get_leaf_cert_der()
    cert = x509.load_der_x509_certificate(der)

    print("Leaf subject:", cert.subject.rfc4514_string())
    print("Leaf issuer: ", cert.issuer.rfc4514_string())

    try:
        aia = cert.extensions.get_extension_for_oid(
            ExtensionOID.AUTHORITY_INFORMATION_ACCESS
        ).value
    except x509.ExtensionNotFound:
        print(
            "\nNo AIA extension on the leaf certificate — can't auto-locate "
            "the intermediate. The server may need a different fix; paste "
            "this script's output back for help."
        )
        sys.exit(1)

    issuer_url = None
    for desc in aia:
        if desc.access_method == AuthorityInformationAccessOID.CA_ISSUERS:
            issuer_url = desc.access_location.value
            break

    if not issuer_url:
        print("\nNo CA Issuers URL found in the AIA extension.")
        sys.exit(1)

    print("CA Issuers URL:", issuer_url)
    print("Downloading intermediate certificate...")

    with urllib.request.urlopen(issuer_url, timeout=15) as resp:
        data = resp.read()

    try:
        intermediate = x509.load_der_x509_certificate(data)
    except ValueError:
        intermediate = x509.load_pem_x509_certificate(data)

    intermediate_pem = intermediate.public_bytes(serialization.Encoding.PEM)

    print("Intermediate subject:", intermediate.subject.rfc4514_string())
    print("Intermediate issuer: ", intermediate.issuer.rfc4514_string())

    certifi_bundle = Path(certifi.where()).read_bytes()

    OUT_PATH.write_bytes(certifi_bundle + b"\n" + intermediate_pem)

    print(f"\nWrote merged bundle: {OUT_PATH.resolve()}")
    print("\nSet this before running the bot:")
    print(f'  cmd.exe:     set DSE_CA_BUNDLE={OUT_PATH.resolve()}')
    print(f'  PowerShell:  $env:DSE_CA_BUNDLE = "{OUT_PATH.resolve()}"')


if __name__ == "__main__":
    main()
