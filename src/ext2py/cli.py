"""Command-line entry point for `ext2py` (also `python -m ext2py`)."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from typing import List, Optional

from ext2py import __version__
from ext2py.partitions import find_partitions, open_fs
from ext2py.reader import ExtError, ExtFS
from ext2py.shell import ExtShell, format_info, format_stat, human_size


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ext2py",
        description="Read-only ext2/ext3/ext4 filesystem browser. Never writes to the image.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    image = argparse.ArgumentParser(add_help=False)
    image.add_argument("image", help="filesystem image, disk image or device (e.g. /dev/rdisk4)")
    where = image.add_mutually_exclusive_group()
    where.add_argument("-p", "--partition", type=int, metavar="N",
                       help="use partition N of a partitioned disk (see 'parts')")
    where.add_argument("--offset", type=int, metavar="BYTES",
                       help="byte offset of the filesystem within the image")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("shell", parents=[image], help="interactive shell: cd, ls, cat, get ...")
    sub.add_parser("info", parents=[image], help="show filesystem information")
    sub.add_parser("parts", help="list partitions of a disk image").add_argument("image")
    p = sub.add_parser("ls", parents=[image], help="list a directory")
    p.add_argument("path", nargs="?", default="/")
    p.add_argument("-l", action="store_true", help="long listing")
    p.add_argument("-a", action="store_true", help="include hidden entries")
    p.add_argument("-H", "--human", action="store_true", help="human-readable sizes")
    p = sub.add_parser("cat", parents=[image], help="print a file to stdout")
    p.add_argument("path")
    p = sub.add_parser("stat", parents=[image], help="show inode details")
    p.add_argument("path")
    p = sub.add_parser("get", parents=[image], help="copy a file or directory tree out")
    p.add_argument("path")
    p.add_argument("dest", nargs="?", default=".")
    return parser


def run(args: argparse.Namespace) -> int:
    if args.command == "parts":
        with open(args.image, "rb", buffering=0) as f:
            parts = find_partitions(f)
        if not parts:
            print("no partition table found")
            return 0
        for part in parts:
            print(f"{part.number:>3}  offset={part.offset:<14} size={human_size(part.size):<8} "
                  f"type={part.type}{'  (linux)' if part.is_linux else ''}  {part.name}")
        return 0

    with open_fs(args.image, partition=args.partition, offset=args.offset) as fs:
        if args.command == "shell":
            ExtShell(fs, label=os.path.basename(args.image)).cmdloop()
        elif args.command == "info":
            print("\n".join(format_info(fs)))
        elif args.command == "ls":
            flags = "".join(f for f, on in (("l", args.l), ("a", args.a), ("h", args.human)) if on)
            ExtShell(fs).do_ls(shlex.join(([f"-{flags}"] if flags else []) + [args.path]))
        elif args.command == "cat":
            out = sys.stdout.buffer
            for chunk in fs.iter_read(args.path):
                out.write(chunk)
            out.flush()
        elif args.command == "stat":
            print("\n".join(format_stat(fs, args.path)))
        elif args.command == "get":
            return _get(fs, args.path, args.dest)
    return 0


def _get(fs: ExtFS, path: str, dest: str) -> int:
    errors = []
    n = fs.extract(path, dest, on_error=lambda s, e: errors.append((s, e)))
    for s, e in errors:
        print(f"ext2py: {s}: {e}", file=sys.stderr)
    print(f"{n} item(s) copied" + (f", {len(errors)} error(s)" if errors else ""), file=sys.stderr)
    return 1 if errors else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return run(args)
    except BrokenPipeError:
        sys.stderr.close()   # e.g. `ext2py cat img /big | head`
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ExtError) as exc:
        if isinstance(exc, OSError) and exc.strerror:
            msg = f"{exc.filename}: {exc.strerror}" if exc.filename else exc.strerror
        else:
            msg = str(exc)
        print(f"ext2py: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
