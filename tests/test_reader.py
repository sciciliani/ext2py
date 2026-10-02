import io
import os
import struct

import pytest

from ext2py import ExtError, ExtFS, find_partitions, open_fs
from imagebuilder import ImageBuilder

LAYOUTS = {
    "ext2-1k": dict(block_size=1024),
    "ext2-4k-nofiletype": dict(block_size=4096, blocks=1024, filetype=False),
    "ext4-extents-64bit": dict(block_size=4096, blocks=2048, extents=True, bit64=True,
                               inode_size=256),
    "ext4-fragmented": dict(block_size=1024, blocks=8192, extents=True, fragment=True,
                            inode_size=256),
    "multigroup": dict(block_size=1024, blocks=8192, inodes_per_group=16, blocks_per_group=512),
}

BIG = bytes((i * 7) % 251 for i in range(300 * 1024))  # 1k blocks: needs double-indirect


def populate(b: ImageBuilder) -> None:
    b.mkdir("/etc")
    b.file("/etc/hostname", b"box\n")
    b.file("/etc/empty", b"")
    b.mkdir("/home")
    b.mkdir("/home/me", mode=0o700)
    b.file("/home/me/big.bin", BIG, mode=0o600)
    b.file("/home/me/name with spaces.txt", b"hi")
    b.symlink("/home/me/fast", "../../etc/hostname")
    b.symlink("/home/me/abs", "/etc")
    b.symlink("/home/me/slow", "/" + "x" * 70 + "/../etc/hostname")
    b.symlink("/home/me/loop", "loop")
    b.mkdir("/many")
    for i in range(120):
        b.file(f"/many/file{i:03}", str(i).encode())
    b.fifo("/etc/fifo")


@pytest.fixture(params=sorted(LAYOUTS))
def fs(request):
    b = ImageBuilder(**LAYOUTS[request.param])
    populate(b)
    with ExtFS(io.BytesIO(b.build())) as fs:
        yield fs


def test_superblock(fs):
    assert fs.sb.volume_name == "testvol"
    assert fs.sb.last_mounted == "/mnt/test"
    assert fs.sb.uuid_str == "00010203-0405-0607-0809-0a0b0c0d0e0f"
    assert not fs.needs_recovery


def test_listdir(fs):
    assert sorted(fs.listdir("/")) == ["etc", "home", "many"]
    assert sorted(fs.listdir("/etc")) == ["empty", "fifo", "hostname"]
    assert len(fs.listdir("/many")) == 120  # spans several directory blocks


def test_read_files(fs):
    assert fs.read_file("/etc/hostname") == b"box\n"
    assert fs.read_file("/etc/empty") == b""
    assert fs.read_file("/home/me/big.bin") == BIG
    assert fs.read_file("/home/me/name with spaces.txt") == b"hi"
    assert fs.read_file("/many/file119") == b"119"


def test_stat(fs):
    st = fs.stat("/home/me")
    assert st.is_dir and st.filemode == "drwx------"
    st = fs.stat("/home/me/big.bin")
    assert st.is_file and st.size == len(BIG) and st.uid == 1000 and st.mtime == 1700000000


def test_symlinks(fs):
    assert fs.readlink("/home/me/fast") == "../../etc/hostname"
    assert fs.read_file("/home/me/fast") == b"box\n"
    assert fs.readlink("/home/me/slow").startswith("/xxx")  # stored in a data block
    assert fs.lstat("/home/me/abs").is_symlink
    assert fs.stat("/home/me/abs").is_dir
    assert sorted(fs.listdir("/home/me/abs")) == ["empty", "fifo", "hostname"]
    assert fs.read_file("/home/me/abs/hostname") == b"box\n"
    with pytest.raises(OSError, match="symbolic links"):
        fs.stat("/home/me/loop")


def test_dotdot_and_errors(fs):
    assert fs.read_file("/home/../etc/./hostname") == b"box\n"
    assert fs.listdir("/..") == fs.listdir("/")
    with pytest.raises(FileNotFoundError):
        fs.stat("/nope")
    with pytest.raises(NotADirectoryError):
        fs.listdir("/etc/hostname")
    with pytest.raises(NotADirectoryError):
        fs.stat("/etc/hostname/x")
    with pytest.raises(IsADirectoryError):
        fs.read_file("/etc")


def test_walk(fs):
    seen = {top: (sorted(d), sorted(f)) for top, d, f in fs.walk("/home")}
    assert seen["/home"] == (["me"], [])
    assert seen["/home/me"][1] == sorted(
        ["big.bin", "name with spaces.txt", "fast", "abs", "slow", "loop"])


def test_sparse_triple_indirect():
    # 1k blocks: triple indirect starts at block 12 + 256 + 65536
    b = ImageBuilder(block_size=1024)
    size = (12 + 256 + 65536 + 10) * 1024
    b.file("/sparse", size=size, chunks={0: b"head", 5000: b"mid", size - 4: b"tail"})
    fs = ExtFS(io.BytesIO(b.build()))
    data = fs.read_file("/sparse")
    assert len(data) == size
    assert data[:4] == b"head" and data[5000:5003] == b"mid" and data[-4:] == b"tail"
    assert data.count(0) == size - 11


def test_sparse_extents():
    b = ImageBuilder(block_size=4096, extents=True, inode_size=256)
    b.file("/sparse", size=1 << 30, chunks={123456789: b"needle"})
    fs = ExtFS(io.BytesIO(b.build()))
    runs = fs.runs(fs.stat("/sparse"))
    assert len(runs) == 1
    total = 0
    for chunk in fs.iter_read("/sparse"):
        idx = chunk.find(b"needle")
        if idx >= 0:
            assert total + idx == 123456789
        total += len(chunk)
    assert total == 1 << 30


def test_uninitialized_extents_read_as_zero():
    b = ImageBuilder(block_size=1024, extents=True, inode_size=256)
    b.file("/prealloc", b"garbage!" * 512, uninit=True)
    fs = ExtFS(io.BytesIO(b.build()))
    assert fs.read_file("/prealloc") == bytes(4096)


def test_inline_data():
    b = ImageBuilder(block_size=4096, extents=True, inode_size=256, inline_data=True)
    b.mkdir("/d", inline=True)
    b.file("/d/small", b"tiny", inline=True)
    b.file("/d/medium", bytes(range(90)), inline=True)  # 60 in i_block + 30 in xattr
    fs = ExtFS(io.BytesIO(b.build()))
    assert sorted(fs.listdir("/d")) == ["medium", "small"]
    assert fs.read_file("/d/small") == b"tiny"
    assert fs.read_file("/d/medium") == bytes(range(90))
    assert fs.read_file("/d/../d/small") == b"tiny"


def test_crtime_from_large_inode():
    b = ImageBuilder(block_size=4096, inode_size=256)
    b.file("/f", b"x")
    fs = ExtFS(io.BytesIO(b.build()))
    assert fs.stat("/f").crtime == 1700000000 - 1000
    b = ImageBuilder(block_size=4096, inode_size=128)
    b.file("/f", b"x")
    assert ExtFS(io.BytesIO(b.build())).stat("/f").crtime is None


def test_rejects_non_ext():
    with pytest.raises(ExtError, match="magic"):
        ExtFS(io.BytesIO(bytes(8192)))


def test_rejects_unknown_incompat_feature():
    img = bytearray(ImageBuilder().build())
    struct.pack_into("<I", img, 1024 + 96, 0x2 | 0x1000)  # dirdata
    with pytest.raises(ExtError, match="dirdata"):
        ExtFS(io.BytesIO(bytes(img)))


def _mbr_disk(fs_img: bytes, start_sector: int = 2048) -> bytes:
    mbr = bytearray(512)
    struct.pack_into("<B3xB3xII", mbr, 446, 0, 0x83, start_sector, len(fs_img) // 512)
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr) + bytes(start_sector * 512 - 512) + fs_img


def test_partitioned_disk(tmp_path):
    b = ImageBuilder()
    b.file("/hello", b"world")
    disk = tmp_path / "disk.img"
    disk.write_bytes(_mbr_disk(b.build()))
    with open(disk, "rb") as f:
        parts = find_partitions(f)
    assert [(p.number, p.offset, p.is_linux) for p in parts] == [(1, 2048 * 512, True)]
    with open_fs(disk) as fs:  # auto-detected
        assert fs.read_file("/hello") == b"world"
    with open_fs(disk, partition=1) as fs:
        assert fs.read_file("/hello") == b"world"
    with pytest.raises(ExtError, match="no partition 2"):
        open_fs(disk, partition=2)


def test_extract(fs, tmp_path):
    errors = []
    n = fs.extract("/home", tmp_path, on_error=lambda s, e: errors.append(s))
    assert errors == []
    me = tmp_path / "home" / "me"
    assert (me / "big.bin").read_bytes() == BIG
    assert os.readlink(me / "fast") == "../../etc/hostname"
    assert (me / "big.bin").stat().st_mtime == 1700000000
    assert n == 2 + 6

    errors = []
    fs.extract("/etc", tmp_path / "etc-copy", on_error=lambda s, e: errors.append(s))
    assert (tmp_path / "etc-copy" / "hostname").read_bytes() == b"box\n"
    assert errors == ["/etc/fifo"]  # special files are skipped and reported


def test_extract_never_writes_through_symlinks(tmp_path):
    # A crafted image with duplicate names: a symlink "x" -> outside, then a dir "x".
    b = ImageBuilder()
    b.mkdir("/d")
    b.symlink("/d/x", str(tmp_path / "outside"))
    b.mkdir("/d/y")
    b.file("/d/y/payload", b"pwned")
    img = bytearray(b.build())
    i = img.find(b"y\0\0\0", 2048)  # rename directory entry "y" -> "x"
    img[i:i + 1] = b"x"
    (tmp_path / "outside").mkdir()
    fs = ExtFS(io.BytesIO(bytes(img)))
    errors = []
    fs.extract("/d", tmp_path / "out", on_error=lambda s, e: errors.append(s))
    assert errors and not (tmp_path / "outside" / "payload").exists()
