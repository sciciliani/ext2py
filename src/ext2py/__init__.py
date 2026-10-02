"""ext2py - read-only ext2/ext3/ext4 filesystem reader in pure Python."""

__version__ = "0.1.0"

from ext2py.partitions import Partition, find_partitions, open_fs
from ext2py.reader import DirEntry, ExtError, ExtFS, Inode, Superblock

__all__ = [
    "__version__", "ExtFS", "ExtError", "Inode", "DirEntry", "Superblock",
    "Partition", "find_partitions", "open_fs",
]
