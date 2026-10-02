"""Interactive, read-only shell for browsing an ext filesystem (``ext2py shell``)."""

from __future__ import annotations

import cmd
import fnmatch
import os
import posixpath
import shlex
import sys
import time
from typing import List, Optional, TextIO

from ext2py.reader import DirEntry, ExtError, ExtFS


def human_size(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def format_time(ts: float) -> str:
    t = time.localtime(ts)
    if abs(time.time() - ts) > 180 * 86400:
        return time.strftime("%b %d  %Y", t)
    return time.strftime("%b %d %H:%M", t)


def long_listing(fs: ExtFS, entries: List[tuple], human: bool = False) -> List[str]:
    """Format ``(name, inode)`` pairs like ``ls -l``."""
    rows = []
    for name, inode in entries:
        size = human_size(inode.size) if human else str(inode.size)
        if inode.is_symlink:
            try:
                name = f"{name} -> {fs._readlink(inode).decode('utf-8', 'replace')}"
            except (OSError, ExtError):
                pass
        rows.append((inode.filemode, str(inode.links_count), str(inode.uid), str(inode.gid),
                     size, format_time(inode.mtime), name))
    if not rows:
        return []
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    size_w = max(len(r[4]) for r in rows)
    return [
        f"{r[0]} {r[1]:>{widths[1]}} {r[2]:<{widths[2]}} {r[3]:<{widths[3]}} "
        f"{r[4]:>{size_w}} {r[5]} {r[6]}"
        for r in rows
    ]


class ExtShell(cmd.Cmd):
    """``cd`` / ``ls`` / ``get`` around a read-only :class:`ExtFS`."""

    doc_header = "Commands (type help <command>):"

    def __init__(self, fs: ExtFS, label: str = "", stdin: Optional[TextIO] = None,
                 stdout: Optional[TextIO] = None):
        super().__init__(stdin=stdin, stdout=stdout)
        if stdin is not None:
            self.use_rawinput = False
        self.fs = fs
        self.label = label
        self.cwd = "/"
        self.local_dir = os.getcwd()
        self.intro = (
            f"ext2py shell: {label} ({fs.sb.fs_type}, read-only). "
            "Type 'help' for commands, 'exit' to quit."
        )
        if fs.needs_recovery:
            self.intro += ("\nwarning: the journal needs recovery; the most recent "
                           "writes may be missing or inconsistent.")

    # -- plumbing ---------------------------------------------------------- #

    @property
    def prompt(self) -> str:  # type: ignore[override]
        return f"ext2py:{self.cwd}$ "

    def print(self, *args) -> None:
        print(*args, file=self.stdout)

    def error(self, msg: str) -> None:
        print(f"error: {msg}", file=self.stdout)

    def resolve(self, arg: str) -> str:
        """Lexically resolve ``arg`` against the current directory (like ``cd`` in a shell)."""
        path = posixpath.normpath(posixpath.join(self.cwd, arg or "."))
        return "/" + path.lstrip("/")

    def local_path(self, arg: str) -> str:
        return os.path.normpath(os.path.join(self.local_dir, os.path.expanduser(arg)))

    def args(self, line: str) -> List[str]:
        return shlex.split(line)

    def onecmd(self, line: str) -> bool:
        try:
            return bool(super().onecmd(line))
        except KeyboardInterrupt:
            self.print("^C")
        except ValueError as exc:   # shlex: unbalanced quotes
            self.error(str(exc))
        except (OSError, ExtError) as exc:
            self.error(exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc))
        return False

    def emptyline(self) -> bool:
        return False

    def default(self, line: str) -> bool:
        self.error(f"unknown command: {line.split()[0]} (try 'help')")
        return False

    def preloop(self) -> None:
        try:
            import readline
        except ImportError:
            return
        readline.set_completer_delims(" \t\n")
        if "libedit" in (readline.__doc__ or "") and sys.version_info < (3, 13):
            readline.parse_and_bind("bind ^I rl_complete")

    # -- completion -------------------------------------------------------- #

    def _complete_remote(self, text: str, dirs_only: bool = False) -> List[str]:
        head, _, tail = text.rpartition("/")
        base = self.resolve(head + "/" if head or text.startswith("/") else ".")
        try:
            entries = self.fs.scandir(base)
        except (OSError, ExtError):
            return []
        out = []
        for e in entries:
            if not e.name.startswith(tail):
                continue
            is_dir = self._entry_is_dir(base, e)
            if dirs_only and not is_dir:
                continue
            prefix = head + "/" if head or text.startswith("/") else ""
            out.append(prefix + e.name + ("/" if is_dir else ""))
        return out

    def _entry_is_dir(self, base: str, e: DirEntry) -> bool:
        try:
            return self.fs.isdir(posixpath.join(base, e.name))
        except (OSError, ExtError):
            return False

    def _complete_paths(self, text, line, begidx, endidx):
        return self._complete_remote(text)

    def complete_cd(self, text, line, begidx, endidx):
        return self._complete_remote(text, dirs_only=True)

    complete_ls = complete_ll = complete_cat = complete_stat = _complete_paths
    complete_get = complete_find = complete_tree = complete_readlink = _complete_paths

    def complete_lcd(self, text, line, begidx, endidx):
        base = self.local_path(os.path.dirname(text) or ".")
        try:
            names = os.listdir(base)
        except OSError:
            return []
        head = os.path.dirname(text)
        return [os.path.join(head, n) + "/" for n in names
                if n.startswith(os.path.basename(text)) and os.path.isdir(os.path.join(base, n))]

    # -- navigation -------------------------------------------------------- #

    def do_pwd(self, line: str) -> None:
        """pwd: print the current directory inside the filesystem."""
        self.print(self.cwd)

    def do_cd(self, line: str) -> None:
        """cd [DIR]: change directory inside the filesystem (default: /)."""
        args = self.args(line)
        path = self.resolve(args[0] if args else "/")
        if not self.fs.stat(path).is_dir:
            raise NotADirectoryError(20, "Not a directory")
        self.cwd = path

    def do_ls(self, line: str) -> None:
        """ls [-l] [-a] [-h] [PATH...]: list directory contents."""
        flags, paths = set(), []
        for a in self.args(line):
            if a.startswith("-") and len(a) > 1:
                flags.update(a[1:])
            else:
                paths.append(a)
        unknown = flags - set("lah")
        if unknown:
            return self.error(f"ls: unknown option -{''.join(sorted(unknown))}")
        paths = paths or ["."]
        for i, p in enumerate(paths):
            path = self.resolve(p)
            inode = self.fs.stat(path)
            if len(paths) > 1 and inode.is_dir:
                self.print(("\n" if i else "") + f"{p}:")
            if not inode.is_dir:
                self._print_listing([(p, self.fs.lstat(path))], flags)
                continue
            entries = sorted(self.fs._dir_entries(inode.ino), key=lambda e: e.name)
            if "a" not in flags:
                entries = [e for e in entries if not e.name.startswith(".")]
            items = []
            for e in entries:
                try:
                    items.append((e.name, self.fs.inode(e.inode)))
                except ExtError as exc:
                    self.error(f"{e.name}: {exc}")
            self._print_listing(items, flags)

    def _print_listing(self, items: List[tuple], flags: set) -> None:
        if "l" in flags:
            for row in long_listing(self.fs, items, human="h" in flags):
                self.print(row)
        else:
            for name, inode in items:
                suffix = "/" if inode.is_dir else "@" if inode.is_symlink else ""
                self.print(name + suffix)

    def do_ll(self, line: str) -> None:
        """ll [PATH...]: shorthand for ls -lh."""
        self.do_ls("-lh " + line)

    def do_tree(self, line: str) -> None:
        """tree [PATH]: show the directory tree."""
        args = self.args(line)
        root = self.resolve(args[0] if args else ".")
        self.print(root)
        self._tree(root, "")

    def _tree(self, path: str, indent: str) -> None:
        entries = sorted(self.fs.scandir(path), key=lambda e: e.name)
        for i, e in enumerate(entries):
            last = i == len(entries) - 1
            child = posixpath.join(path, e.name)
            inode = self.fs.inode(e.inode)
            self.print(f"{indent}{'└── ' if last else '├── '}{e.name}{'/' if inode.is_dir else ''}")
            if inode.is_dir:
                self._tree(child, indent + ("    " if last else "│   "))

    def do_find(self, line: str) -> None:
        """find [PATH] [-name PATTERN]: list files recursively, optionally matching a glob."""
        args = self.args(line)
        pattern = None
        if "-name" in args:
            i = args.index("-name")
            if i + 1 >= len(args):
                return self.error("find: -name needs a pattern")
            pattern = args[i + 1]
            del args[i:i + 2]
        root = self.resolve(args[0] if args else ".")
        for top, dirs, files in self.fs.walk(root):
            for name in dirs + files:
                if pattern is None or fnmatch.fnmatch(name, pattern):
                    self.print(posixpath.join(top, name))

    # -- inspection -------------------------------------------------------- #

    def do_cat(self, line: str) -> None:
        """cat FILE...: print file contents."""
        out = getattr(self.stdout, "buffer", None)
        for p in self.args(line):
            for chunk in self.fs.iter_read(self.resolve(p)):
                if out is not None:
                    out.write(chunk)
                else:
                    self.stdout.write(chunk.decode("utf-8", "replace"))
            if out is not None:
                out.flush()

    def do_stat(self, line: str) -> None:
        """stat PATH...: show inode details (does not follow a final symlink)."""
        for p in self.args(line):
            for row in format_stat(self.fs, self.resolve(p)):
                self.print(row)

    def do_readlink(self, line: str) -> None:
        """readlink PATH: print a symlink's target."""
        for p in self.args(line):
            self.print(self.fs.readlink(self.resolve(p)))

    def do_info(self, line: str) -> None:
        """info: show filesystem (superblock) information."""
        for row in format_info(self.fs):
            self.print(row)

    # -- local side and downloads ----------------------------------------- #

    def do_lpwd(self, line: str) -> None:
        """lpwd: print the local download directory."""
        self.print(self.local_dir)

    def do_lcd(self, line: str) -> None:
        """lcd [DIR]: change the local download directory (default: home)."""
        args = self.args(line)
        path = self.local_path(args[0] if args else "~")
        if not os.path.isdir(path):
            raise NotADirectoryError(20, f"Not a local directory: {path}")
        self.local_dir = path

    def do_lls(self, line: str) -> None:
        """lls [DIR]: list the local download directory."""
        args = self.args(line)
        for name in sorted(os.listdir(self.local_path(args[0] if args else "."))):
            self.print(name)

    def do_get(self, line: str) -> None:
        """get PATH [LOCAL]: download a file or directory (recursively) to the local side.

        LOCAL defaults to the local directory (see lcd / lpwd)."""
        args = self.args(line)
        if not args or len(args) > 2:
            return self.error("usage: get PATH [LOCAL]")
        src = self.resolve(args[0])
        dest = self.local_path(args[1] if len(args) > 1 else ".")
        errors = []
        n = self.fs.extract(
            src, dest,
            on_file=lambda s, d: self.print(f"{s} -> {d}"),
            on_error=lambda s, e: errors.append((s, e)),
        )
        for s, e in errors:
            self.error(f"{s}: {e}")
        self.print(f"{n} item(s) copied" + (f", {len(errors)} error(s)" if errors else ""))

    # -- exit -------------------------------------------------------------- #

    def do_exit(self, line: str) -> bool:
        """exit: leave the shell."""
        return True

    do_quit = do_exit

    def do_EOF(self, line: str) -> bool:
        self.print()
        return True


def format_stat(fs: ExtFS, path: str) -> List[str]:
    inode = fs.lstat(path)
    kind = ("directory" if inode.is_dir else "symlink" if inode.is_symlink
            else "regular file" if inode.is_file else "special file")
    rows = [
        f"  File: {path}" + (f" -> {fs.readlink(path)}" if inode.is_symlink else ""),
        f"  Type: {kind}",
        f"  Size: {inode.size}  Blocks: {inode.blocks}  Inode: {inode.ino}  Links: {inode.links_count}",
        f"  Mode: ({inode.mode & 0o7777:04o}/{inode.filemode})  Uid: {inode.uid}  Gid: {inode.gid}",
        f"Access: {time.ctime(inode.atime)}",
        f"Modify: {time.ctime(inode.mtime)}",
        f"Change: {time.ctime(inode.ctime)}",
    ]
    if inode.crtime is not None:
        rows.append(f" Birth: {time.ctime(inode.crtime)}")
    rows.append(f" Flags: 0x{inode.flags:08x}")
    return rows


def format_info(fs: ExtFS) -> List[str]:
    sb = fs.sb
    return [
        f"Filesystem:     {sb.fs_type}",
        f"Volume name:    {sb.volume_name or '<none>'}",
        f"UUID:           {sb.uuid_str}",
        f"Last mounted:   {sb.last_mounted or '<not available>'}",
        f"Block size:     {sb.block_size}",
        f"Blocks:         {sb.blocks_count} ({human_size(sb.blocks_count * sb.block_size)})",
        f"Free blocks:    {sb.free_blocks_count} ({human_size(sb.free_blocks_count * sb.block_size)})",
        f"Inodes:         {sb.inodes_count} (free: {sb.free_inodes_count})",
        f"Inode size:     {sb.inode_size}",
        f"Block groups:   {sb.group_count}",
        f"Features:       {' '.join(sb.incompat_names()) or '<none>'}",
        f"Last written:   {time.ctime(sb.wtime) if sb.wtime else '<never>'}",
        f"Needs recovery: {'yes' if fs.needs_recovery else 'no'}",
    ]
