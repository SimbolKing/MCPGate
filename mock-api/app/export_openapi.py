from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import Settings
from app.main import create_app
from app.openapi import build_openapi


def export_openapi_to_file(output_path: Path, settings: Settings | None = None) -> dict:
    app = create_app(settings=settings or Settings(), init_database=False)
    spec = build_openapi(app)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线导出降级后的 OpenAPI 3.0.3 文档")
    parser.add_argument(
        "--output",
        default="openapi.json",
        help="输出文件路径（默认 openapi.json）",
    )
    args = parser.parse_args(argv)
    export_openapi_to_file(Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
