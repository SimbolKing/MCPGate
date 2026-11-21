from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from dev_idp.tokens import PACKAGE_ROOT, DevIdpSettings

PEM_WARNING_HEADER = (
    "# DEV ONLY: RSA-2048 private key for MCPGate local development and evaluation.\n"
    "# DO NOT USE IN PRODUCTION OR ANY REAL ENVIRONMENT.\n"
)


def generate_keypair(key_path: Path, jwks_path: Path, kid: str) -> dict[str, Any]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    public_jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwks = {
        "keys": [
            {
                "kty": public_jwk["kty"],
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": public_jwk["n"],
                "e": public_jwk["e"],
            }
        ]
    }

    key_path.parent.mkdir(parents=True, exist_ok=True)
    jwks_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(PEM_WARNING_HEADER + pem_bytes.decode("ascii"), encoding="ascii")
    jwks_path.write_text(json.dumps(jwks, indent=2) + "\n", encoding="utf-8")
    return jwks


def main(argv: list[str] | None = None) -> int:
    settings = DevIdpSettings()
    parser = argparse.ArgumentParser(description="生成开发用 RSA-2048 密钥对与 JWKS 文件")
    parser.add_argument(
        "--key-file",
        default=str(PACKAGE_ROOT / settings.key_file),
        help="私钥输出路径（默认 keys/private.pem）",
    )
    parser.add_argument(
        "--jwks-file",
        default=str(PACKAGE_ROOT / settings.jwks_file),
        help="JWKS 输出路径（默认 keys/jwks.json）",
    )
    parser.add_argument(
        "--kid",
        default=settings.kid,
        help="JWK 的 kid 字段（默认 mcpgate-dev-1）",
    )
    args = parser.parse_args(argv)
    generate_keypair(Path(args.key_file), Path(args.jwks_file), args.kid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
