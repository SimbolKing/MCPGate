from __future__ import annotations

import argparse
import sys

from app.config import Settings
from app.db import create_db_engine, reset_db


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="清空 guard 的审批单与审计数据并重置序号")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认清空 approvals 与 audit_events 表",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="覆盖 GUARD_DATABASE_URL（默认读取配置）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.yes:
        print("请传入 --yes 以确认清空审批单与审计数据。", file=sys.stderr)
        return 2

    settings = Settings()
    db_url = args.database_url or settings.database_url
    engine = create_db_engine(db_url)
    try:
        reset_db(engine)
    finally:
        engine.dispose()

    print("guard 数据已重置（approvals、audit_events 已清空，下一张审批单为 AP000001）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
