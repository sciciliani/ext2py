# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Pure-Python reader: superblock, group descriptors (incl. 64bit, meta_bg),
  inodes, block maps (direct to triple-indirect), ext4 extent trees, sparse
  files, uninitialized extents, inline data, fast/slow symlinks, directories
  (linear and htree).
- MBR / GPT partition detection.
- `ext2py shell` interactive browser (cd, ls, tree, find, cat, stat, get, lcd).
- `info`, `parts`, `ls`, `cat`, `stat`, `get` subcommands.

## [0.1.0] - 2026-10-02

### Added
- Initial package layout, `ext2py` CLI entry point, PyPI publishing workflow.
