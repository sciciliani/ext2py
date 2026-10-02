"""Tiny pure-Python ext2/ext4 image *writer*, used only to build test fixtures.

It lays out structures by hand (superblock, group descriptors, inode tables,
directory blocks, block maps / extent trees, inline data) so the reader can be
tested without mke2fs. Bitmaps are not written; the reader never uses them.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

S_IFDIR, S_IFREG, S_IFLNK, S_IFIFO = 0o040000, 0o100000, 0o120000, 0o010000
FT = {S_IFREG: 1, S_IFDIR: 2, S_IFLNK: 7, S_IFIFO: 5}


@dataclass
class Node:
    mode: int
    data: bytes = b""
    size: Optional[int] = None                 # for sparse files: logical size
    chunks: Optional[Dict[int, bytes]] = None  # for sparse files: {offset: bytes}
    children: Dict[str, "Node"] = field(default_factory=dict)
    mtime: int = 1700000000
    uid: int = 1000
    gid: int = 1000
    ino: int = 0
    inline: bool = False
    uninit: bool = False


class ImageBuilder:
    def __init__(self, block_size: int = 1024, blocks: int = 4096, *, extents: bool = False,
                 bit64: bool = False, inode_size: int = 128, inodes_per_group: int = 256,
                 blocks_per_group: Optional[int] = None, filetype: bool = True,
                 fragment: bool = False, inline_data: bool = False, journal: bool = False):
        self.bs = block_size
        self.blocks = blocks
        self.extents = extents
        self.bit64 = bit64
        self.inode_size = inode_size
        self.ipg = inodes_per_group
        self.bpg = blocks_per_group or 8 * block_size
        self.filetype = filetype
        self.fragment = fragment          # leave a gap after every data block -> many extents
        self.inline_data = inline_data
        self.journal = journal
        self.root = Node(S_IFDIR | 0o755)

    # -- tree building ----------------------------------------------------- #

    def _parent(self, path: str) -> tuple:
        parts = [p for p in path.split("/") if p]
        node = self.root
        for p in parts[:-1]:
            node = node.children[p]
        return node, parts[-1]

    def mkdir(self, path: str, mode: int = 0o755, inline: bool = False) -> None:
        parent, name = self._parent(path)
        parent.children[name] = Node(S_IFDIR | mode, inline=inline)

    def file(self, path: str, data: bytes = b"", mode: int = 0o644, *, size: Optional[int] = None,
             chunks: Optional[Dict[int, bytes]] = None, inline: bool = False,
             uninit: bool = False, mtime: int = 1700000000) -> None:
        parent, name = self._parent(path)
        parent.children[name] = Node(S_IFREG | mode, data, size, chunks, inline=inline,
                                     uninit=uninit, mtime=mtime)

    def symlink(self, path: str, target: str) -> None:
        parent, name = self._parent(path)
        parent.children[name] = Node(S_IFLNK | 0o777, target.encode())

    def fifo(self, path: str) -> None:
        parent, name = self._parent(path)
        parent.children[name] = Node(S_IFIFO | 0o644)

    # -- layout ------------------------------------------------------------ #

    def build(self) -> bytes:
        bs = self.bs
        self.img = bytearray(self.blocks * bs)
        first_data_block = 1 if bs == 1024 else 0
        self.groups = -(-(self.blocks - first_data_block) // self.bpg)
        self.inodes_count = self.ipg * self.groups
        desc_size = 64 if self.bit64 else 32
        gdt_blocks = -(-self.groups * desc_size // bs)
        table_blocks = -(-self.ipg * self.inode_size // bs)

        self.next_block = first_data_block + 1 + gdt_blocks
        tables = []
        for _ in range(self.groups):
            tables.append(self.next_block)
            self.next_block += table_blocks
        self.tables = tables

        # assign inode numbers: root=2, the rest from 11
        nodes: List[tuple] = []
        next_ino = [11]

        def assign(node: Node, parent: Node) -> None:
            nodes.append((node, parent))
            for child in node.children.values():
                child.ino = next_ino[0]
                next_ino[0] += 1
                assign(child, node)

        self.root.ino = 2
        assign(self.root, self.root)
        assert next_ino[0] <= self.inodes_count, "too many inodes"
        for node, parent in nodes:
            self._write_node(node, parent)

        # group descriptors
        gdt = (first_data_block + 1) * bs
        for g, table in enumerate(tables):
            off = gdt + g * desc_size
            struct.pack_into("<III", self.img, off, 0, 0, table & 0xFFFFFFFF)
            if self.bit64:
                struct.pack_into("<I", self.img, off + 0x28, table >> 32)

        # superblock
        sb = bytearray(1024)
        struct.pack_into("<13I", sb, 0, self.inodes_count, self.blocks, 0,
                         self.blocks - self.next_block, self.inodes_count - next_ino[0],
                         first_data_block, (bs >> 10).bit_length() - 1, 0, self.bpg, self.bpg,
                         self.ipg, 0, 1700000000)
        struct.pack_into("<HHH", sb, 56, 0xEF53, 1, 1)
        struct.pack_into("<I", sb, 76, 1)  # dynamic rev
        struct.pack_into("<IH", sb, 84, 11, self.inode_size)
        incompat = (0x2 if self.filetype else 0) | (0x40 if self.extents else 0) \
            | (0x80 if self.bit64 else 0) | (0x8000 if self.inline_data else 0)
        compat = 0x4 if self.journal else 0
        struct.pack_into("<3I", sb, 92, compat, incompat, 0)
        sb[104:120] = bytes(range(16))
        sb[120:128] = b"testvol\0"
        sb[136:146] = b"/mnt/test\0"
        struct.pack_into("<H", sb, 254, desc_size if self.bit64 else 0)
        self.img[1024:2048] = sb
        return bytes(self.img)

    def alloc(self, n: int = 1) -> int:
        start = self.next_block
        self.next_block += n + (1 if self.fragment else 0)
        assert self.next_block <= self.blocks, "image full"
        return start

    def _block_contents(self, node: Node) -> tuple:
        """(logical size, {logical block: bytes}) - zero blocks are holes when sparse."""
        bs = self.bs
        if node.chunks is not None:
            blocks: Dict[int, bytearray] = {}
            for off, data in node.chunks.items():
                for i, b in enumerate(data):
                    blk, o = divmod(off + i, bs)
                    blocks.setdefault(blk, bytearray(bs))[o] = b
            return node.size, {k: bytes(v) for k, v in blocks.items()}
        data = node.data
        return len(data), {i // bs: data[i:i + bs].ljust(bs, b"\0")
                           for i in range(0, len(data), bs)}

    def _dir_data(self, node: Node, parent: Node) -> bytes:
        entries = [(b".", node.ino, 2), (b"..", parent.ino, 2)]
        entries += [(name.encode(), c.ino, FT.get(c.mode & 0o170000, 0))
                    for name, c in node.children.items()]
        return self._pack_dirents(entries, self.bs, multi_block=True)

    def _pack_dirents(self, entries, region: int, multi_block: bool) -> bytes:
        out, block = bytearray(), bytearray()
        last = None
        for name, ino, ft in entries:
            size = (8 + len(name) + 3) & ~3
            if len(block) + size > region:
                assert multi_block, "inline dir too large for the test builder"
                struct.pack_into("<H", block, last + 4, region - last)
                out += block.ljust(region, b"\0")
                block = bytearray()
            last = len(block)
            if self.filetype:
                block += struct.pack("<IHBB", ino, size, len(name), ft) + name
            else:
                block += struct.pack("<IHH", ino, size, len(name)) + name
            block += b"\0" * (size - 8 - len(name))
        if last is not None:
            struct.pack_into("<H", block, last + 4, region - last)
        out += block.ljust(region, b"\0")
        return bytes(out)

    def _write_node(self, node: Node, parent: Node) -> None:
        bs = self.bs
        kind = node.mode & 0o170000
        flags = 0
        i_block = bytearray(60)
        xattr_data: Optional[bytes] = None
        sectors = 0
        if kind == S_IFDIR and node.inline:
            entries = [(name.encode(), c.ino, FT.get(c.mode & 0o170000, 0))
                       for name, c in node.children.items()]
            i_block[:4] = struct.pack("<I", parent.ino)
            i_block[4:] = self._pack_dirents(entries, 56, multi_block=False)
            size, flags, xattr_data = 60, 0x10000000, b""
        elif kind == S_IFDIR:
            data = self._dir_data(node, parent)
            size = len(data)
            i_block, flags, sectors = self._map(data_blocks={i // bs: data[i:i + bs]
                                                             for i in range(0, size, bs)},
                                                size=size, uninit=False)
        elif kind == S_IFLNK and len(node.data) < 60:
            size = len(node.data)
            i_block[:size] = node.data
        elif node.inline:
            size = len(node.data)
            i_block[:min(size, 60)] = node.data[:60]
            xattr_data = node.data[60:]
            flags = 0x10000000
        elif kind in (S_IFREG, S_IFLNK):
            size, blocks = self._block_contents(node)
            i_block, flags, sectors = self._map(blocks, size, node.uninit)
        else:
            size = 0

        raw = bytearray(self.inode_size)
        links = 2 + sum(1 for c in node.children.values() if c.mode & 0o170000 == S_IFDIR) \
            if kind == S_IFDIR else 1
        struct.pack_into("<2H5I2HII", raw, 0, node.mode, node.uid & 0xFFFF, size & 0xFFFFFFFF,
                         node.mtime, node.mtime, node.mtime, 0, node.gid & 0xFFFF, links,
                         sectors, flags)
        raw[40:100] = i_block
        struct.pack_into("<I", raw, 108, size >> 32)
        struct.pack_into("<HH", raw, 120, node.uid >> 16, node.gid >> 16)
        if self.inode_size > 128:
            struct.pack_into("<H", raw, 128, 32)
            struct.pack_into("<I", raw, 0x90, node.mtime - 1000)   # crtime
            if xattr_data is not None:
                start = 128 + 32
                struct.pack_into("<I", raw, start, 0xEA020000)
                base = start + 4
                value_off = self.inode_size - base - ((len(xattr_data) + 3) & ~3)
                struct.pack_into("<BBHIII", raw, base, 4, 7, value_off, 0, len(xattr_data), 0)
                raw[base + 16:base + 20] = b"data"
                raw[base + value_off:base + value_off + len(xattr_data)] = xattr_data
                assert base + 24 <= base + value_off, "inline data too large for inode"
        else:
            assert xattr_data is None, "inline data needs inode_size > 128"

        group, index = divmod(node.ino - 1, self.ipg)
        pos = self.tables[group] * bs + index * self.inode_size
        self.img[pos:pos + self.inode_size] = raw

    def _write_block(self, block: int, data: bytes) -> None:
        self.img[block * self.bs:(block + 1) * self.bs] = data.ljust(self.bs, b"\0")

    def _map(self, data_blocks: Dict[int, bytes], size: int, uninit: bool) -> tuple:
        """Allocate and write data blocks; return (i_block, flags, sectors)."""
        placed = {}
        for lblk in sorted(data_blocks):
            phys = self.alloc()
            self._write_block(phys, data_blocks[lblk])
            placed[lblk] = phys
        if self.extents:
            i_block, meta = self._extent_tree(placed, uninit)
            return i_block, 0x80000, (len(placed) + meta) * (self.bs // 512)
        i_block, meta = self._block_map(placed)
        return i_block, 0, (len(placed) + meta) * (self.bs // 512)

    def _block_map(self, placed: Dict[int, int]) -> tuple:
        per = self.bs // 4
        ptrs = [0] * 15
        meta = [0]
        tables: Dict[tuple, List[int]] = {}

        def table(key: tuple) -> List[int]:
            if key not in tables:
                tables[key] = [0] * per
            return tables[key]

        # Build a tree of index tables keyed by (level-root, path...), then write.
        roots = {1: 12, 2: 13, 3: 14}
        for lblk, phys in placed.items():
            if lblk < 12:
                ptrs[lblk] = phys
                continue
            rel, level = lblk - 12, 1
            while rel >= per ** level:
                rel -= per ** level
                level += 1
            digits = []
            for _ in range(level):
                digits.append(rel % per)
                rel //= per
            digits.reverse()
            for depth in range(level):
                key = (level,) + tuple(digits[:depth])
                table(key)
            tables[(level,) + tuple(digits[:-1])][digits[-1]] = phys

        def write(key: tuple) -> int:
            level = key[0]
            entries = tables[key]
            depth = len(key) - 1
            if depth < level - 1:
                for i in range(per):
                    child = key + (i,)
                    if child in tables:
                        entries[i] = write(child)
            blk = self.alloc()
            meta[0] += 1
            self._write_block(blk, struct.pack(f"<{per}I", *entries))
            return blk

        for level, slot in roots.items():
            if (level,) in tables:
                ptrs[slot] = write((level,))
        return struct.pack("<15I", *ptrs), meta[0]

    def _extent_tree(self, placed: Dict[int, int], uninit: bool) -> tuple:
        runs: List[List[int]] = []
        for lblk in sorted(placed):
            phys = placed[lblk]
            if runs and runs[-1][0] + runs[-1][2] == lblk and runs[-1][1] + runs[-1][2] == phys \
                    and runs[-1][2] < 32767:
                runs[-1][2] += 1
            else:
                runs.append([lblk, phys, 1])

        def leaf_entries(rs) -> bytes:
            return b"".join(struct.pack("<IHHI", l, n + (32768 if uninit else 0), p >> 32,
                                        p & 0xFFFFFFFF) for l, p, n in rs)

        if len(runs) <= 4:
            hdr = struct.pack("<4HI", 0xF30A, len(runs), 4, 0, 0)
            return (hdr + leaf_entries(runs)).ljust(60, b"\0"), 0
        per_leaf = (self.bs - 12) // 12
        leaves = [runs[i:i + per_leaf] for i in range(0, len(runs), per_leaf)]
        assert len(leaves) <= 4, "extent tree too deep for the test builder"
        index = b""
        for rs in leaves:
            blk = self.alloc()
            self._write_block(blk, struct.pack("<4HI", 0xF30A, len(rs), per_leaf, 0, 0)
                              + leaf_entries(rs))
            index += struct.pack("<IIHH", rs[0][0], blk & 0xFFFFFFFF, blk >> 32, 0)
        hdr = struct.pack("<4HI", 0xF30A, len(leaves), 4, 1, 0)
        return (hdr + index).ljust(60, b"\0"), len(leaves)
