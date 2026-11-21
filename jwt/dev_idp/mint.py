from __future__ import annotations

import argparse
import sys

from dev_idp.tokens import DevIdpSettings, find_user, mint_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="命令行签发开发用 JWT")
    parser.add_argument("--user", required=True, help="用户名（如 wang、li、sun）")
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help="有效期（秒）；传入负数可生成已过期的 token",
    )
    args = parser.parse_args(argv)

    settings = DevIdpSettings()
    user = find_user(args.user, settings.users_file)
    if user is None:
        print(f"error: unknown user '{args.user}'", file=sys.stderr)
        return 1

    token, _ = mint_token(user, settings, ttl=args.ttl)
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
