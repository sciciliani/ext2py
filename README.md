# ext2py

**Browse ext2 / ext3 / ext4 Linux filesystems from Python on macOS, Linux or Windows: no kernel drivers, no FUSE, no root for disk images.**

`ext2py` is a pure-Python, zero-dependency, **read-only** ext2/3/4 parser. It ships with
an interactive shell to `cd`, `ls` and download files out of Linux disk images,
partitions and devices.

[![PyPI](https://img.shields.io/pypi/v/ext2py.svg)](https://pypi.org/project/ext2py/)
[![Python](https://img.shields.io/pypi/pyversions/ext2py.svg)](https://pypi.org/project/ext2py/)
[![CI](https://github.com/sciciliani/ext2py/actions/workflows/ci.yml/badge.svg)](https://github.com/sciciliani/ext2py/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> ⚠️ Early alpha. ext2py never writes to the source image or device.

## Install

```bash
pip install ext2py
```

## Interactive shell

```console
$ ext2py shell disk.img
ext2py shell: disk.img (ext4, read-only). Type 'help' for commands, 'exit' to quit.
ext2py:/$ cd home/me
ext2py:/home/me$ ls -l
-rw-r--r-- 1 1000 1000 16 Nov 14  2023 notes.txt
ext2py:/home/me$ get notes.txt
/home/me/notes.txt -> /Users/you/notes.txt
1 item(s) copied
```

Commands: `ls [-lah]`, `ll`, `cd`, `pwd`, `tree`, `find [-name GLOB]`, `cat`, `stat`,
`readlink`, `info`, `get PATH [LOCAL]` (recursive for directories), and the local-side
`lcd`, `lpwd`, `lls`. Tab completion works on paths inside the image.

## One-shot commands

```bash
ext2py info  disk.img                   # superblock summary
ext2py parts disk.img                   # partition table (MBR / GPT)
ext2py ls    disk.img /home -l          # list a directory
ext2py cat   disk.img /etc/hostname     # print a file
ext2py stat  disk.img /etc/passwd       # inode details
ext2py get   disk.img /home/me ./out    # copy files out
```

Whole-disk images are handled automatically when they contain a single ext partition;
otherwise use `--partition N` (see `ext2py parts`) or `--offset BYTES`.

Physical disks on macOS: find the disk with `diskutil list`, then point at the raw
device (`sudo ext2py shell /dev/rdisk4`). Reading a device needs read permission on
it, which is the only reason `sudo` is involved. Image files need no privileges.

## Python API

```python
from ext2py import ExtFS, open_fs

with ExtFS("partition.img") as fs:          # or open_fs("disk.img") for partitioned disks
    print(fs.listdir("/etc"))
    data = fs.read_file("/etc/os-release")
    st = fs.stat("/etc/passwd")               # Inode: mode, uid, gid, size, mtime, ...
    for top, dirs, files in fs.walk("/home"):
        ...
    fs.extract("/home/me", "./out")           # like cp -R
```

## Features

- [x] ext2, ext3 and ext4 (the journal is ignored; reads the on-disk state)
- [x] Direct, indirect, double- and triple-indirect block maps; sparse files
- [x] ext4 extent trees, uninitialized extents, inline data, 64bit, flex_bg, meta_bg
- [x] Hashed (htree) directories, fast and slow symlinks
- [x] Partitioned images (MBR / GPT)
- [ ] Encrypted directories (names are shown raw, contents are not decrypted)
- [ ] Extended attributes outside the inode, MBR logical partitions

## Development

```bash
git clone https://github.com/sciciliani/ext2py && cd ext2py
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The tests build ext2/ext4 images in pure Python (`tests/imagebuilder.py`), so no
`mke2fs` is required.

## License

MIT — see [LICENSE](LICENSE).
