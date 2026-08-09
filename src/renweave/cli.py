from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import RenWeavePipeline
from .provider import ModelProfile
from .rpa import RpaArchive, script_member


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="renweave",
        description="RenWeave / 织译：理解场景与剧情关系的 Ren'Py 多语言本地化引擎",
    )
    parser.add_argument("--version", action="version", version="RenWeave 0.3.0")
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="识别并分析一个 Ren'Py 项目，不调用 AI")
    analyze.add_argument("target", help="游戏根目录、game 目录或游戏程序")
    analyze.add_argument("--workspace", required=True, help="RenWeave 独立工作目录")
    analyze.add_argument("--source-language", default="auto", help="源语言代码或名称；默认自动识别")
    analyze.add_argument("--unrpyc", help="自定义 unrpyc.py 路径；默认按需下载固定版本")
    analyze.add_argument("--no-tool-download", action="store_true", help="禁止自动下载反编译器")

    run = commands.add_parser("run", help="执行场景级翻译流水线")
    run.add_argument("target", help="游戏根目录、game 目录或游戏程序")
    run.add_argument("--workspace", required=True, help="RenWeave 独立工作目录")
    run.add_argument("--provider", required=True, help="模型配置 JSON")
    run.add_argument("--source-language", default="auto", help="源语言代码或名称；默认自动识别")
    run.add_argument("--target-language", required=True, help="任意目标语言代码或名称，例如 es-ES、ja、Deutsch")
    run.add_argument("--unrpyc", help="自定义 unrpyc.py 路径；默认按需下载固定版本")
    run.add_argument("--no-tool-download", action="store_true", help="禁止自动下载反编译器")
    run.add_argument("--limit", type=int, default=0, help="开发时限制翻译场景数；0 表示全部")
    run.add_argument(
        "--repair-attempts",
        type=int,
        default=2,
        help="仅对校验失败文本执行的低 Token 修复次数；默认 2",
    )
    run.add_argument("--install", action="store_true", help="构建成功后安装到游戏的 game/tl/<language>")
    run.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="安装时允许覆盖不是由 RenWeave 生成的同名文件",
    )

    build = commands.add_parser("build", help="从工作区中已验证的译文重新生成 Ren'Py 语言包")
    build.add_argument("--workspace", required=True, help="已有的 RenWeave 工作目录")
    build.add_argument("--target-language", help="覆盖工作区记录的目标语言")
    build.add_argument("--install", action="store_true", help="构建成功后安装到原游戏目录")
    build.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="安装时允许覆盖不是由 RenWeave 生成的同名文件",
    )

    decompile = commands.add_parser("decompile", help="解包并反编译项目中缺少 RPY 源码的 RPYC/RPYMC")
    decompile.add_argument("target", help="游戏根目录、game 目录或游戏程序")
    decompile.add_argument("--workspace", required=True, help="RenWeave 独立工作目录")
    decompile.add_argument("--unrpyc", help="自定义 unrpyc.py 路径；默认按需下载固定版本")
    decompile.add_argument("--no-tool-download", action="store_true", help="禁止自动下载反编译器")

    provider = commands.add_parser("provider-check", help="离线检查模型配置")
    provider.add_argument("config", help="模型配置 JSON")

    unpack = commands.add_parser("unpack", help="安全解包 RPA 2.0/3.0/3.2 归档")
    unpack.add_argument("archive", help="RPA 文件")
    unpack.add_argument("--output", required=True, help="输出目录")
    unpack.add_argument("--scripts-only", action="store_true", help="只提取 RPY/RPYC/PY/PYC")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "provider-check":
            profile = ModelProfile.load(args.config)
            profile.validate()
            print(json.dumps({
                "valid": True,
                "name": profile.name,
                "model": profile.model,
                "base_url": profile.base_url,
                "api_key_configured": bool(profile.resolved_api_key()),
            }, ensure_ascii=False, indent=2))
            return 0

        if args.command == "unpack":
            with RpaArchive(args.archive) as archive:
                manifest = archive.extract(
                    args.output,
                    include=script_member if args.scripts_only else None,
                )
            print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
            return 0

        pipeline = RenWeavePipeline(args.workspace)
        if args.command == "decompile":
            manifest = pipeline.decompile(
                args.target,
                unrpyc_path=args.unrpyc,
                allow_tool_download=not args.no_tool_download,
            )
            print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "analyze":
            index, knowledge = pipeline.analyze(
                args.target,
                source_language=args.source_language,
                unrpyc_path=args.unrpyc,
                allow_tool_download=not args.no_tool_download,
            )
            print(json.dumps({
                "project": index.project.name,
                "renpy_version": index.project.renpy_version,
                "source_files": len(index.files),
                "compiled_scripts": len(index.project.compiled_scripts),
                "archives": len(index.project.archives),
                "scenes": len(index.scenes),
                "text_units": len(index.text_units),
                "characters": len(knowledge.characters),
                "storylines": len(knowledge.storylines),
                "workspace": str(Path(args.workspace).resolve()),
            }, ensure_ascii=False, indent=2))
            return 0

        if args.command == "build":
            manifest = pipeline.build(
                requested_language=args.target_language,
                install=args.install,
                overwrite_existing=args.overwrite_existing,
            )
            print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
            return 0

        profile = ModelProfile.load(args.provider)
        state = pipeline.translate(
            args.target,
            args.source_language,
            args.target_language,
            profile,
            limit=max(0, args.limit),
            install=args.install,
            overwrite_existing=args.overwrite_existing,
            repair_attempts=max(0, args.repair_attempts),
            unrpyc_path=args.unrpyc,
            allow_tool_download=not args.no_tool_download,
        )
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
        return 0 if not state.failed_scene_ids else 2
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"RenWeave error: {exc}", file=sys.stderr)
        return 1
