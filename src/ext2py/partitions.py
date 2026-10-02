"""Minimal MBR / GPT partition table parsing, to find ext filesystems on whole disks."""

from __future__ import annotations

import os
import struct
import uuid
from dataclasses import dataclass
from typing import BinaryIO, List, Optional, Union

from ext2py.reader import EXT_MAGIC, SUPERBLOCK_OFFSET, ExtError, ExtFS, Source

LINUX_FS_GUID = uuid.UUID("0FC63DAF-8483-4772-8E79-3D69D8477DE4")
_MBR_EXTENDED = (0x05, 0x0F, 0x85)
_MBR_GPT_PROTECTIVE = 0xEE


@dataclass
class Partition:
    number: int       # 1-based, as in /dev/sdaN or diskNsN
    offset: int       # bytes
    size: int         # bytes
    type: str         # MBR type byte (hex) or GPT type GUID
    name: str = ""

    @property
    def is_linux(self) -> bool:
        return self.type in ("0x83", str(LINUX_FS_GUID))


def _gpt(src: Source, sector: int) -> Optional[List[Partition]]:
    hdr = src.pread(sector, 92)
    if hdr[:8] != b"EFI PART":
        return None
    entries_lba, count, entry_size = struct.unpack_from("<QII", hdr, 72)
    if entry_size < 128 or count > 1024:
        return None
    table = src.pread(entries_lba * sector, count * entry_size)
    parts = []
    for i in range(count):
        e = table[i * entry_size:(i + 1) * entry_size]
        type_guid = uuid.UUID(bytes_le=e[:16])
        if type_guid.int == 0:
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        name = e[56:128].decode("utf-16-le", "replace").split("\0", 1)[0]
        parts.append(Partition(i + 1, first * sector, (last - first + 1) * sector, str(type_guid), name))
    return parts


def find_partitions(f: BinaryIO) -> List[Partition]:
    """Return the partitions on a disk image or device (empty if none found).

    Logical partitions inside MBR extended partitions are not listed.
    """
    src = Source(f)
    try:
        mbr = src.pread(0, 512)
    except ExtError:
        return []
    for sector in (512, 4096):
        try:
            parts = _gpt(src, sector)
        except ExtError:
            parts = None
        if parts is not None:
            return parts
    if mbr[510:512] != b"\x55\xaa":
        return []
    parts = []
    for i in range(4):
        _status, ptype, start, count = struct.unpack_from("<B3xB3xII", mbr, 446 + 16 * i)
        if ptype == 0 or ptype in _MBR_EXTENDED or ptype == _MBR_GPT_PROTECTIVE or count == 0:
            continue
        parts.append(Partition(i + 1, start * 512, count * 512, f"0x{ptype:02x}"))
    return parts


def _has_ext_magic(f: BinaryIO, offset: int) -> bool:
    try:
        sb = Source(f, offset).pread(SUPERBLOCK_OFFSET, 64)
    except ExtError:
        return False
    return struct.unpack_from("<H", sb, 56)[0] == EXT_MAGIC


def open_fs(
    path: Union[str, "os.PathLike[str]"],
    partition: Optional[int] = None,
    offset: Optional[int] = None,
) -> ExtFS:
    """Open an ext filesystem from a bare filesystem image or a partitioned disk.

    With neither ``partition`` nor ``offset``, a bare filesystem is tried
    first, then the partition table is searched for exactly one ext partition.
    """
    f = open(path, "rb", buffering=0)
    try:
        if offset is None:
            if partition is not None:
                parts = {p.number: p for p in find_partitions(f)}
                if partition not in parts:
                    raise ExtError(f"no partition {partition} (found: {sorted(parts) or 'none'})")
                offset = parts[partition].offset
            elif _has_ext_magic(f, 0):
                offset = 0
            else:
                found = [p for p in find_partitions(f) if _has_ext_magic(f, p.offset)]
                if not found:
                    raise ExtError("no ext2/3/4 filesystem found (not a bare image, no ext partition)")
                if len(found) > 1:
                    nums = ", ".join(str(p.number) for p in found)
                    raise ExtError(f"several ext partitions found ({nums}); pick one with --partition")
                offset = found[0].offset
        fs = ExtFS(f, offset)
    except BaseException:
        f.close()
        raise
    fs._owns_file = True
    return fs
