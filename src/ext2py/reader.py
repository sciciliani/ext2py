"""Pure-Python, read-only parser for ext2 / ext3 / ext4 filesystems.

On-disk layout reference: https://docs.kernel.org/filesystems/ext4/

Everything here only ever *reads* from the underlying file or device.
The ext3/ext4 journal is ignored: we read the on-disk state as-is.
"""

from __future__ import annotations

import errno
import os
import posixpath
import stat as stat_mod
import struct
from dataclasses import dataclass, field
from functools import lru_cache
from typing import BinaryIO, Callable, Iterator, List, Optional, Tuple, Union

EXT_MAGIC = 0xEF53
ROOT_INO = 2
SUPERBLOCK_OFFSET = 1024

# s_feature_compat
COMPAT_HAS_JOURNAL = 0x0004
COMPAT_DIR_INDEX = 0x0020
# s_feature_ro_compat
RO_COMPAT_SPARSE_SUPER = 0x0001
RO_COMPAT_HUGE_FILE = 0x0008
# s_feature_incompat
INCOMPAT_COMPRESSION = 0x0001
INCOMPAT_FILETYPE = 0x0002
INCOMPAT_RECOVER = 0x0004
INCOMPAT_JOURNAL_DEV = 0x0008
INCOMPAT_META_BG = 0x0010
INCOMPAT_EXTENTS = 0x0040
INCOMPAT_64BIT = 0x0080
INCOMPAT_MMP = 0x0100
INCOMPAT_FLEX_BG = 0x0200
INCOMPAT_EA_INODE = 0x0400
INCOMPAT_DIRDATA = 0x1000
INCOMPAT_CSUM_SEED = 0x2000
INCOMPAT_LARGEDIR = 0x4000
INCOMPAT_INLINE_DATA = 0x8000
INCOMPAT_ENCRYPT = 0x10000
INCOMPAT_CASEFOLD = 0x20000

INCOMPAT_SUPPORTED = (
    INCOMPAT_FILETYPE | INCOMPAT_RECOVER | INCOMPAT_META_BG | INCOMPAT_EXTENTS
    | INCOMPAT_64BIT | INCOMPAT_MMP | INCOMPAT_FLEX_BG | INCOMPAT_EA_INODE
    | INCOMPAT_CSUM_SEED | INCOMPAT_LARGEDIR | INCOMPAT_INLINE_DATA
    | INCOMPAT_ENCRYPT | INCOMPAT_CASEFOLD
)

INCOMPAT_NAMES = {
    INCOMPAT_COMPRESSION: "compression", INCOMPAT_FILETYPE: "filetype",
    INCOMPAT_RECOVER: "needs_recovery", INCOMPAT_JOURNAL_DEV: "journal_dev",
    INCOMPAT_META_BG: "meta_bg", INCOMPAT_EXTENTS: "extent", INCOMPAT_64BIT: "64bit",
    INCOMPAT_MMP: "mmp", INCOMPAT_FLEX_BG: "flex_bg", INCOMPAT_EA_INODE: "ea_inode",
    INCOMPAT_DIRDATA: "dirdata", INCOMPAT_CSUM_SEED: "metadata_csum_seed",
    INCOMPAT_LARGEDIR: "large_dir", INCOMPAT_INLINE_DATA: "inline_data",
    INCOMPAT_ENCRYPT: "encrypt", INCOMPAT_CASEFOLD: "casefold",
}

# i_flags
INODE_INDEX_FL = 0x00001000
INODE_HUGE_FILE_FL = 0x00040000
INODE_EXTENTS_FL = 0x00080000
INODE_INLINE_DATA_FL = 0x10000000

EXTENT_MAGIC = 0xF30A
XATTR_MAGIC = 0xEA020000
XATTR_INDEX_SYSTEM = 7

# Directory entry file_type values
FT_NAMES = {1: "file", 2: "dir", 3: "chrdev", 4: "blkdev", 5: "fifo", 6: "socket", 7: "symlink"}
FT_DIR = 2

_ALIGN = 4096        # raw devices (/dev/rdisk*) only accept sector-aligned reads
_READ_CHUNK = 1 << 20
_MAX_SYMLINKS = 40


class ExtError(Exception):
    """The image is not a (supported) ext filesystem, or it is corrupt."""


# --------------------------------------------------------------------------- #
# Low-level source
# --------------------------------------------------------------------------- #

class Source:
    """Random-access reads from a file object, aligned for raw block devices."""

    def __init__(self, f: BinaryIO, offset: int = 0):
        self.f = f
        self.offset = offset
        try:
            self._fd: Optional[int] = f.fileno()
        except (AttributeError, OSError, ValueError):
            self._fd = None

    def _raw(self, pos: int, n: int) -> bytes:
        parts = []
        while n > 0:
            if self._fd is not None and hasattr(os, "pread"):
                chunk = os.pread(self._fd, n, pos)
            else:
                self.f.seek(pos)
                chunk = self.f.read(n)
            if not chunk:
                break
            parts.append(chunk)
            pos += len(chunk)
            n -= len(chunk)
        return b"".join(parts)

    def pread(self, pos: int, length: int) -> bytes:
        if length <= 0:
            return b""
        pos += self.offset
        start = pos - pos % _ALIGN
        end = -(-(pos + length) // _ALIGN) * _ALIGN
        data = self._raw(start, end - start)[pos - start:pos - start + length]
        if len(data) != length:
            raise ExtError(f"short read at byte {pos} (image truncated?)")
        return data


# --------------------------------------------------------------------------- #
# On-disk structures
# --------------------------------------------------------------------------- #

def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


@dataclass
class Superblock:
    inodes_count: int
    blocks_count: int
    free_blocks_count: int
    free_inodes_count: int
    first_data_block: int
    log_block_size: int
    blocks_per_group: int
    inodes_per_group: int
    mtime: int
    wtime: int
    magic: int
    state: int
    rev_level: int
    first_ino: int
    inode_size: int
    feature_compat: int
    feature_incompat: int
    feature_ro_compat: int
    uuid: bytes
    volume_name: str
    last_mounted: str
    desc_size: int
    first_meta_bg: int
    mkfs_time: int

    @classmethod
    def parse(cls, raw: bytes) -> "Superblock":
        magic = struct.unpack_from("<H", raw, 56)[0]
        if magic != EXT_MAGIC:
            raise ExtError("not an ext2/3/4 filesystem (bad superblock magic)")
        (inodes_count, blocks_lo, _r_blocks_lo, free_blocks_lo, free_inodes,
         first_data_block, log_block_size, _log_cluster, blocks_per_group,
         _clusters_per_group, inodes_per_group, mtime, wtime) = struct.unpack_from("<13I", raw, 0)
        state = struct.unpack_from("<H", raw, 58)[0]
        rev_level = struct.unpack_from("<I", raw, 76)[0]
        first_ino, inode_size = struct.unpack_from("<IH", raw, 84)
        compat, incompat, ro_compat = struct.unpack_from("<3I", raw, 92)
        desc_size = struct.unpack_from("<H", raw, 254)[0]
        first_meta_bg, mkfs_time = struct.unpack_from("<II", raw, 260)
        blocks_hi, _r_hi, free_blocks_hi = struct.unpack_from("<3I", raw, 336)
        if rev_level == 0:
            first_ino, inode_size = 11, 128
        is64 = bool(incompat & INCOMPAT_64BIT)
        return cls(
            inodes_count=inodes_count,
            blocks_count=blocks_lo | ((blocks_hi << 32) if is64 else 0),
            free_blocks_count=free_blocks_lo | ((free_blocks_hi << 32) if is64 else 0),
            free_inodes_count=free_inodes,
            first_data_block=first_data_block,
            log_block_size=log_block_size,
            blocks_per_group=blocks_per_group,
            inodes_per_group=inodes_per_group,
            mtime=mtime,
            wtime=wtime,
            magic=magic,
            state=state,
            rev_level=rev_level,
            first_ino=first_ino,
            inode_size=inode_size,
            feature_compat=compat,
            feature_incompat=incompat,
            feature_ro_compat=ro_compat,
            uuid=raw[104:120],
            volume_name=_cstr(raw[120:136]),
            last_mounted=_cstr(raw[136:200]),
            desc_size=(desc_size or 64) if is64 else 32,
            first_meta_bg=first_meta_bg,
            mkfs_time=mkfs_time,
        )

    @property
    def block_size(self) -> int:
        return 1024 << self.log_block_size

    @property
    def group_count(self) -> int:
        return -(-(self.blocks_count - self.first_data_block) // self.blocks_per_group)

    @property
    def uuid_str(self) -> str:
        h = self.uuid.hex()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

    @property
    def fs_type(self) -> str:
        if self.feature_incompat & (INCOMPAT_EXTENTS | INCOMPAT_64BIT | INCOMPAT_FLEX_BG):
            return "ext4"
        if self.feature_compat & COMPAT_HAS_JOURNAL:
            return "ext3"
        return "ext2"

    def incompat_names(self) -> List[str]:
        return [n for bit, n in sorted(INCOMPAT_NAMES.items()) if self.feature_incompat & bit]


def _ts(lo: int, extra: Optional[int]) -> float:
    secs = lo - (1 << 32) if lo & 0x80000000 else lo
    if extra is None:
        return float(secs)
    return secs + ((extra & 3) << 32) + (extra >> 2) / 1e9


@dataclass
class Inode:
    ino: int
    mode: int
    uid: int
    gid: int
    size: int
    atime: float
    ctime: float
    mtime: float
    crtime: Optional[float]
    dtime: int
    links_count: int
    blocks: int          # in 512-byte sectors
    flags: int
    generation: int
    file_acl: int
    block: bytes         # raw i_block (60 bytes): block map, extent root, inline data or symlink
    raw: bytes = field(repr=False)

    @classmethod
    def parse(cls, ino: int, raw: bytes, sb: Superblock) -> "Inode":
        (mode, uid_lo, size_lo, atime, ctime, mtime, dtime, gid_lo, links,
         blocks_lo, flags) = struct.unpack_from("<2H5I2HII", raw, 0)
        generation, file_acl_lo, size_hi = struct.unpack_from("<3I", raw, 100)
        blocks_hi, file_acl_hi, uid_hi, gid_hi = struct.unpack_from("<4H", raw, 116)

        extra_isize = struct.unpack_from("<H", raw, 128)[0] if len(raw) > 128 else 0
        extra_end = 128 + extra_isize

        def extra(off: int) -> Optional[int]:
            return struct.unpack_from("<I", raw, off)[0] if off + 4 <= extra_end else None

        blocks = blocks_lo
        if sb.feature_ro_compat & RO_COMPAT_HUGE_FILE:
            blocks |= blocks_hi << 32
            if flags & INODE_HUGE_FILE_FL:
                blocks *= sb.block_size // 512
        crtime = extra(0x90)
        return cls(
            ino=ino,
            mode=mode,
            uid=uid_lo | (uid_hi << 16),
            gid=gid_lo | (gid_hi << 16),
            size=size_lo | (size_hi << 32),
            atime=_ts(atime, extra(0x8C)),
            ctime=_ts(ctime, extra(0x84)),
            mtime=_ts(mtime, extra(0x88)),
            crtime=None if crtime is None else _ts(crtime, extra(0x94)),
            dtime=dtime,
            links_count=links,
            blocks=blocks,
            flags=flags,
            generation=generation,
            file_acl=file_acl_lo | (file_acl_hi << 32),
            block=raw[40:100],
            raw=raw,
        )

    @property
    def is_dir(self) -> bool:
        return stat_mod.S_ISDIR(self.mode)

    @property
    def is_file(self) -> bool:
        return stat_mod.S_ISREG(self.mode)

    @property
    def is_symlink(self) -> bool:
        return stat_mod.S_ISLNK(self.mode)

    @property
    def filemode(self) -> str:
        """``ls -l`` style mode string, e.g. ``drwxr-xr-x``."""
        return stat_mod.filemode(self.mode)

    def inline_xattrs(self) -> dict:
        """Extended attributes stored inside the inode body, as {(index, name): value}."""
        raw = self.raw
        if len(raw) <= 128:
            return {}
        start = 128 + struct.unpack_from("<H", raw, 128)[0]
        if start + 4 > len(raw) or struct.unpack_from("<I", raw, start)[0] != XATTR_MAGIC:
            return {}
        base = start + 4
        off = base
        out = {}
        while off + 16 <= len(raw) and struct.unpack_from("<I", raw, off)[0] != 0:
            name_len, index, value_offs, value_inum, value_size, _hash = \
                struct.unpack_from("<BBHIII", raw, off)
            name = raw[off + 16:off + 16 + name_len]
            if value_inum == 0:
                out[(index, name)] = raw[base + value_offs:base + value_offs + value_size]
            off += (16 + name_len + 3) & ~3
        return out


@dataclass(frozen=True)
class DirEntry:
    name: str
    inode: int
    file_type: int   # 0 when the filesystem lacks the "filetype" feature
    name_bytes: bytes = field(repr=False, default=b"")

    @property
    def is_dir(self) -> bool:
        return self.file_type == FT_DIR

    @property
    def type_name(self) -> str:
        return FT_NAMES.get(self.file_type, "unknown")


@dataclass(frozen=True)
class Run:
    """A contiguous mapping of ``length`` logical blocks to physical blocks."""
    logical: int
    physical: int
    length: int
    uninit: bool = False   # ext4 preallocated extent: reads back as zeros


# --------------------------------------------------------------------------- #
# Filesystem
# --------------------------------------------------------------------------- #

PathLike = Union[str, bytes, "os.PathLike[str]"]


def _split(path: bytes) -> List[bytes]:
    return [p for p in path.split(b"/") if p and p != b"."]


def _safe_name(name: str) -> bool:
    return name not in ("", ".", "..") and "/" not in name and "\0" not in name


class ExtFS:
    """A read-only ext2/ext3/ext4 filesystem.

    ``source`` is a path to an image / device, or an already-open binary file
    object. ``offset`` is the byte offset of the filesystem within it (for
    partitioned disks, see :func:`ext2py.partitions.open_fs`).
    """

    def __init__(self, source: Union[PathLike, BinaryIO], offset: int = 0):
        if isinstance(source, (str, bytes, os.PathLike)):
            self._file: BinaryIO = open(source, "rb", buffering=0)
            self._owns_file = True
        else:
            self._file = source
            self._owns_file = False
        try:
            self._src = Source(self._file, offset)
            self.sb = Superblock.parse(self._src.pread(SUPERBLOCK_OFFSET, 1024))
            unsupported = self.sb.feature_incompat & ~INCOMPAT_SUPPORTED
            if unsupported:
                names = [n for b, n in INCOMPAT_NAMES.items() if unsupported & b] or [hex(unsupported)]
                raise ExtError(f"unsupported filesystem features: {', '.join(names)}")
            if self.sb.blocks_per_group == 0 or self.sb.inodes_per_group == 0:
                raise ExtError("corrupt superblock")
        except BaseException:
            self.close()
            raise
        self.block_size = self.sb.block_size
        self.inode = lru_cache(maxsize=4096)(self._read_inode)
        self._read_meta_block = lru_cache(maxsize=256)(self._read_block)
        self._dir_entries = lru_cache(maxsize=256)(self._read_dir)

    # -- lifecycle -------------------------------------------------------- #

    def close(self) -> None:
        if getattr(self, "_owns_file", False):
            self._file.close()

    def __enter__(self) -> "ExtFS":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def needs_recovery(self) -> bool:
        """True if the journal has unreplayed transactions (recent writes may be missing)."""
        return bool(self.sb.feature_incompat & INCOMPAT_RECOVER)

    # -- blocks and group descriptors ------------------------------------- #

    def _read_block(self, block: int) -> bytes:
        return self._src.pread(block * self.block_size, self.block_size)

    def _group_has_super(self, group: int) -> bool:
        if not self.sb.feature_ro_compat & RO_COMPAT_SPARSE_SUPER or group <= 1:
            return True
        for base in (3, 5, 7):
            n = base
            while n < group:
                n *= base
            if n == group:
                return True
        return False

    def _group_desc(self, group: int) -> bytes:
        bs, ds = self.block_size, self.sb.desc_size
        per_block = bs // ds
        index = group // per_block
        if self.sb.feature_incompat & INCOMPAT_META_BG and index >= self.sb.first_meta_bg:
            first = index * per_block
            block = self.sb.first_data_block + first * self.sb.blocks_per_group
            block += 1 if self._group_has_super(first) else 0
        else:
            block = self.sb.first_data_block + 1 + index
        off = (group % per_block) * ds
        return self._read_meta_block(block)[off:off + ds]

    def _inode_table(self, group: int) -> int:
        desc = self._group_desc(group)
        table = struct.unpack_from("<I", desc, 8)[0]
        if self.sb.desc_size >= 64:
            table |= struct.unpack_from("<I", desc, 0x28)[0] << 32
        return table

    # -- inodes ------------------------------------------------------------ #

    def _read_inode(self, ino: int) -> Inode:
        if not 1 <= ino <= self.sb.inodes_count:
            raise ExtError(f"inode number {ino} out of range")
        group, index = divmod(ino - 1, self.sb.inodes_per_group)
        if group >= self.sb.group_count:
            raise ExtError(f"inode number {ino} out of range")
        pos = self._inode_table(group) * self.block_size + index * self.sb.inode_size
        return Inode.parse(ino, self._src.pread(pos, self.sb.inode_size), self.sb)

    @property
    def root(self) -> Inode:
        return self.inode(ROOT_INO)

    # -- block mapping ----------------------------------------------------- #

    def runs(self, inode: Inode) -> List[Run]:
        """Map the inode's logical blocks to physical blocks (holes are omitted)."""
        if inode.flags & INODE_EXTENTS_FL:
            out: List[Run] = []
            self._walk_extents(inode.block, out, None)
            out.sort(key=lambda r: r.logical)
            return out
        return self._blockmap_runs(inode)

    def _walk_extents(self, node: bytes, out: List[Run], expect_depth: Optional[int]) -> None:
        magic, entries, max_entries, depth = struct.unpack_from("<4H", node, 0)
        if magic != EXTENT_MAGIC:
            raise ExtError("corrupt extent tree (bad magic)")
        if depth > 5 or (expect_depth is not None and depth != expect_depth):
            raise ExtError("corrupt extent tree (bad depth)")
        if entries > max_entries or 12 + 12 * entries > len(node):
            raise ExtError("corrupt extent tree (too many entries)")
        for i in range(entries):
            off = 12 + 12 * i
            if depth == 0:
                lblock, length, start_hi, start_lo = struct.unpack_from("<IHHI", node, off)
                uninit = length > 32768
                if uninit:
                    length -= 32768
                out.append(Run(lblock, (start_hi << 32) | start_lo, length, uninit))
            else:
                _lblock, leaf_lo, leaf_hi = struct.unpack_from("<IIH", node, off)
                child = self._read_meta_block((leaf_hi << 32) | leaf_lo)
                self._walk_extents(child, out, depth - 1)

    def _blockmap_runs(self, inode: Inode) -> List[Run]:
        bs = self.block_size
        per = bs // 4
        nblocks = -(-inode.size // bs)
        ptrs = struct.unpack("<15I", inode.block)
        runs: List[List[int]] = []

        def add(logical: int, physical: int) -> None:
            if runs:
                last = runs[-1]
                if last[0] + last[2] == logical and last[1] + last[2] == physical:
                    last[2] += 1
                    return
            runs.append([logical, physical, 1])

        def walk(block: int, level: int, base: int) -> None:
            span = per ** (level - 1)
            for i, p in enumerate(struct.unpack(f"<{per}I", self._read_block(block))):
                logical = base + i * span
                if logical >= nblocks:
                    break
                if p:
                    if level == 1:
                        add(logical, p)
                    else:
                        walk(p, level - 1, logical)

        for i in range(min(12, nblocks)):
            if ptrs[i]:
                add(i, ptrs[i])
        logical = 12
        for level, ptr in ((1, ptrs[12]), (2, ptrs[13]), (3, ptrs[14])):
            if logical >= nblocks:
                break
            if ptr:
                walk(ptr, level, logical)
            logical += per ** level
        return [Run(*r) for r in runs]

    # -- file contents ----------------------------------------------------- #

    def _inline_data(self, inode: Inode) -> bytes:
        return inode.block + inode.inline_xattrs().get((XATTR_INDEX_SYSTEM, b"data"), b"")

    def iter_inode_data(self, inode: Inode, chunk_size: int = _READ_CHUNK) -> Iterator[bytes]:
        """Yield the contents of ``inode`` in chunks. Holes read as zeros."""
        remaining = inode.size
        if inode.flags & INODE_INLINE_DATA_FL:
            yield self._inline_data(inode)[:remaining]
            return
        bs = self.block_size
        pos = 0  # logical block we are at
        for run in self.runs(inode):
            if remaining <= 0:
                break
            if run.logical + run.length <= pos:
                continue  # overlapping extents: corrupt, ignore the overlap
            skip = max(0, pos - run.logical)
            if run.logical > pos:
                hole = min((run.logical - pos) * bs, remaining)
                yield from _zeros(hole, chunk_size)
                remaining -= hole
            nbytes = min((run.length - skip) * bs, remaining)
            if run.uninit:
                yield from _zeros(nbytes, chunk_size)
            else:
                start = (run.physical + skip) * bs
                done = 0
                while done < nbytes:
                    n = min(chunk_size, nbytes - done)
                    yield self._src.pread(start + done, n)
                    done += n
            remaining -= nbytes
            pos = run.logical + run.length
        if remaining > 0:
            yield from _zeros(remaining, chunk_size)

    def read_inode_data(self, inode: Inode) -> bytes:
        return b"".join(self.iter_inode_data(inode))

    def _readlink(self, inode: Inode) -> bytes:
        if not inode.is_symlink:
            raise OSError(errno.EINVAL, "not a symlink")
        ea_sectors = self.block_size // 512 if inode.file_acl else 0
        if (not inode.flags & (INODE_EXTENTS_FL | INODE_INLINE_DATA_FL)
                and inode.size < 60 and inode.blocks - ea_sectors <= 0):
            return inode.block[:inode.size]   # "fast" symlink: target stored in i_block
        return self.read_inode_data(inode)

    # -- directories ------------------------------------------------------- #

    def _parse_dirents(self, data: bytes, out: List[DirEntry]) -> None:
        has_ftype = bool(self.sb.feature_incompat & INCOMPAT_FILETYPE)
        off, end = 0, len(data)
        while off + 8 <= end:
            ino, rec_len, name_len, ftype = struct.unpack_from("<IHBB", data, off)
            if self.block_size >= 65536 and rec_len in (0, 65535):
                rec_len = 65536
            if not has_ftype:
                name_len |= ftype << 8
                ftype = 0
            if rec_len < 8 or rec_len % 4 or off + rec_len > end:
                break  # corrupt; stop parsing this block
            if ino and name_len and 8 + name_len <= rec_len:
                raw = data[off + 8:off + 8 + name_len]
                out.append(DirEntry(os.fsdecode(raw), ino, ftype, raw))
            off += rec_len

    def _read_dir(self, ino: int) -> Tuple[DirEntry, ...]:
        inode = self.inode(ino)
        if not inode.is_dir:
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory")
        out: List[DirEntry] = []
        if inode.flags & INODE_INLINE_DATA_FL:
            parent = struct.unpack_from("<I", inode.block, 0)[0]
            out.append(DirEntry(".", ino, FT_DIR, b"."))
            out.append(DirEntry("..", parent, FT_DIR, b".."))
            self._parse_dirents(inode.block[4:], out)
            extra = inode.inline_xattrs().get((XATTR_INDEX_SYSTEM, b"data"), b"")
            self._parse_dirents(extra, out)
        else:
            # Hashed (htree) directories are also readable linearly: index
            # blocks look like empty dirents to a linear scan.
            data = self.read_inode_data(inode)
            bs = self.block_size
            for off in range(0, len(data), bs):
                self._parse_dirents(data[off:off + bs], out)
        return tuple(out)

    # -- path API ---------------------------------------------------------- #

    def lookup(self, path: PathLike, follow_symlinks: bool = True) -> Inode:
        """Resolve an absolute path (relative paths are taken from ``/``)."""
        parts = _split(os.fsencode(path))
        cur = self.root
        links = 0
        while parts:
            name = parts.pop(0)
            if not cur.is_dir:
                raise NotADirectoryError(errno.ENOTDIR, "Not a directory", os.fsdecode(path))
            for entry in self._dir_entries(cur.ino):
                if entry.name_bytes == name:
                    break
            else:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", os.fsdecode(path))
            node = self.inode(entry.inode)
            if node.is_symlink and (parts or follow_symlinks):
                links += 1
                if links > _MAX_SYMLINKS:
                    raise OSError(errno.ELOOP, "Too many levels of symbolic links", os.fsdecode(path))
                target = self._readlink(node)
                if target.startswith(b"/"):
                    cur = self.root
                parts[:0] = _split(target)
                continue
            cur = node
        return cur

    def stat(self, path: PathLike) -> Inode:
        return self.lookup(path, follow_symlinks=True)

    def lstat(self, path: PathLike) -> Inode:
        return self.lookup(path, follow_symlinks=False)

    def exists(self, path: PathLike) -> bool:
        try:
            self.lookup(path)
            return True
        except (FileNotFoundError, NotADirectoryError):
            return False

    def isdir(self, path: PathLike) -> bool:
        try:
            return self.lookup(path).is_dir
        except (FileNotFoundError, NotADirectoryError):
            return False

    def scandir(self, path: PathLike = "/") -> List[DirEntry]:
        """Directory entries of ``path``, without ``.`` and ``..``."""
        inode = self.stat(path)
        if not inode.is_dir:
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory", os.fsdecode(path))
        return [e for e in self._dir_entries(inode.ino) if e.name not in (".", "..")]

    def listdir(self, path: PathLike = "/") -> List[str]:
        return [e.name for e in self.scandir(path)]

    def readlink(self, path: PathLike) -> str:
        return os.fsdecode(self._readlink(self.lstat(path)))

    def iter_read(self, path: PathLike, chunk_size: int = _READ_CHUNK) -> Iterator[bytes]:
        inode = self.stat(path)
        if inode.is_dir:
            raise IsADirectoryError(errno.EISDIR, "Is a directory", os.fsdecode(path))
        return self.iter_inode_data(inode, chunk_size)

    def read_file(self, path: PathLike) -> bytes:
        return b"".join(self.iter_read(path))

    def walk(self, top: PathLike = "/") -> Iterator[Tuple[str, List[str], List[str]]]:
        """Like :func:`os.walk` (top-down, symlinks to directories not followed)."""
        top = os.fsdecode(top)
        dirs, files = [], []
        for e in self.scandir(top):
            is_dir = e.is_dir if e.file_type else self.inode(e.inode).is_dir
            (dirs if is_dir else files).append(e.name)
        yield top, dirs, files
        for d in dirs:
            yield from self.walk(posixpath.join(top, d))

    # -- extraction -------------------------------------------------------- #

    def extract(
        self,
        path: PathLike,
        dest: PathLike,
        *,
        on_file: Optional[Callable[[str, str], None]] = None,
        on_error: Optional[Callable[[str, BaseException], None]] = None,
    ) -> int:
        """Copy ``path`` (file, symlink or directory tree) to the local ``dest``.

        Like ``cp -R``: if ``dest`` is an existing directory the item is
        copied *into* it. Symlinks are recreated as symlinks; device nodes,
        FIFOs and sockets are skipped. Returns the number of items written.
        ``on_file(src, dst)`` is called for each item, ``on_error(src, exc)``
        for each failure (if not given, the first failure is raised).
        """
        src = "/" + posixpath.normpath("/" + os.fsdecode(path)).lstrip("/")
        dest = os.fspath(dest)
        inode = self.lstat(src)
        if os.path.isdir(dest) and not os.path.islink(dest):
            dest = os.path.join(dest, posixpath.basename(src) or "root")
        counter = [0]
        self._extract(inode, src, dest, counter, on_file, on_error)
        return counter[0]

    def _extract(self, inode, src, dest, counter, on_file, on_error) -> None:
        try:
            if inode.is_dir:
                if os.path.islink(dest):
                    raise FileExistsError(errno.EEXIST, "refusing to write through symlink", dest)
                os.makedirs(dest, exist_ok=True)
                os.chmod(dest, 0o700)
                if on_file:
                    on_file(src, dest)
                counter[0] += 1
                for e in self._dir_entries(inode.ino):
                    if e.name in (".", ".."):
                        continue
                    child_src = posixpath.join(src, e.name)
                    if not _safe_name(e.name):
                        _report(on_error, child_src, ExtError(f"unsafe file name {e.name!r}"))
                        continue
                    self._extract(self.inode(e.inode), child_src, os.path.join(dest, e.name),
                                  counter, on_file, on_error)
                os.chmod(dest, (inode.mode & 0o7777) | 0o700)
            elif inode.is_symlink:
                os.symlink(self._readlink(inode), dest)
                counter[0] += 1
                if on_file:
                    on_file(src, dest)
                return
            elif inode.is_file:
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
                with os.fdopen(fd, "wb") as out:
                    for chunk in self.iter_inode_data(inode):
                        out.write(chunk)
                os.chmod(dest, (inode.mode & 0o777) | 0o600)
                counter[0] += 1
                if on_file:
                    on_file(src, dest)
            else:
                raise ExtError(f"skipped special file ({stat_mod.filemode(inode.mode)[0]})")
            os.utime(dest, (inode.atime, inode.mtime))
        except (OSError, ExtError) as exc:
            _report(on_error, src, exc)


def _report(on_error, src: str, exc: BaseException) -> None:
    if on_error is None:
        raise exc
    on_error(src, exc)


_ZERO_CHUNK = bytes(_READ_CHUNK)


def _zeros(n: int, chunk_size: int) -> Iterator[bytes]:
    while n > 0:
        k = min(n, chunk_size)
        yield _ZERO_CHUNK[:k] if k <= len(_ZERO_CHUNK) else bytes(k)
        n -= k
