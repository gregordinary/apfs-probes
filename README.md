# apfs-probes

Scripts that record what Apple's APFS tools and kernel write, run on
GitHub-hosted macOS runners.

Each probe under `probe/` creates disk images, drives `newfs_apfs`,
`fsck_apfs`, `diskutil`, `hdiutil`, the other tools in
`/System/Library/Filesystems/apfs.fs` and the kernel through ordinary system
calls, and records what each one did. The `probe` workflow runs one probe on
each runner it is given:

    gh workflow run probe.yml -f probe=discover -f runners='["macos-15","macos-26"]'

## The probes

- `discover`: versions of macOS, the APFS driver and the tools; a container
  in a plain file; a populated container with a case-insensitive and a
  case-sensitive volume; fresh containers from 1 MiB to 16 TiB; and the
  images `hdiutil create` makes.
- `tools`: the executables in `apfs.fs`, and what the informational ones
  report about a fresh and a populated container.
- `format`: one container per `newfs_apfs` option, and the same tree copied
  into two fresh containers, by `ditto` and by `hdiutil create -srcfolder`.
- `ops`: one image per file-system operation, each made by mounting a copy of
  an earlier image once, doing one thing, and unmounting, beside images whose
  mount did nothing.
- `ladder`: fresh containers at about 160 sizes and block sizes.
- `names`, `names-bmp`, `names-rest`: one file per Unicode code point, on a
  case-insensitive and a case-sensitive volume, with each name the volume
  refuses.
- `srcfolder`: `hdiutil create -srcfolder` images of a fixed tree, the same
  tree built in reverse order, and a tree with links, sparse and compressed
  files and extended attributes, each listed by `apfs_checkseal`.
- `timemachine`: local snapshots `tmutil localsnapshot` takes of an
  attached volume included in the backup, before and after a change to it;
  and Time Machine backing up one small folder to an attached image, before
  and after a change to it. Each snapshot is listed, mounted read-only and
  listed.
- `snapshots`: one image per step of taking a local snapshot, changing the
  volume under it, taking a second, mounting each read-only, and deleting
  both, each beside an image whose mount did nothing, as `ops` does for
  single operations; and thirty snapshots in one mount, each right after a
  write, each mounted read-only and listed, with one deleted from the
  middle.

## What a run keeps

Each runner's artifact, kept for seven days, holds:

- `steps.tsv`: one line per step, with its exit status, seconds and command
  line;
- `log/`: each step's standard output and error;
- `images/*.bd`: the nonzero blocks of each image the probe made, stored by
  `tools/blockdump.py`; `python3 tools/blockdump.py restore IMAGE.bd IMAGE`
  rebuilds the image byte for byte;
- the JSON records the probes write about the trees they build and the names
  they create.

## Releases

`tools/pack.py` makes the archive a release attaches for one probe, from
one run's artifacts:

    gh run download RUN -D run
    python3 tools/pack.py discover.tar.xz run/discover-*

`pack.py` checks each block dump against the digest in the `.json` beside it
and stores it decompressed, so a block that several images share is
compressed once in the archive. `blockdump.py restore` reads a dump in
either form:

    tar -xf discover.tar.xz
    python3 tools/blockdump.py restore discover-macos-15/images/populated.bd populated.img

## Licenses

Everything in this repository is licensed under MIT (`LICENSE-MIT`) or
Apache-2.0 (`LICENSE-APACHE`), at your option. The files attached to its
releases are covered in the next section.

## Release files

Each release attaches one archive per probe, holding the files saved from
runs of this repository's scripts on GitHub-hosted macOS runners: disk
images, each step's output, and the records the scripts write about each
run. The disk images are stored as block dumps (`.bd`), which hold each
image's nonzero blocks unchanged; `python3 tools/blockdump.py restore
IMAGE.bd IMAGE` rebuilds an image byte for byte. The content of the release
files comes from two sources:

- **Content from this repository's scripts:** the names, contents and
  extended attributes of the files the scripts create, and of any copies
  macOS makes of them; and the records the scripts write, such as
  `steps.tsv` and the JSON records. To the extent possible under law, this
  repository's authors have waived all copyright and related or neighboring
  rights to this content under CC0-1.0 (`LICENSE-CC0`).
- **Content from macOS:** everything else. This includes the container and
  volume superblocks, object maps, B-trees, checkpoints, space-manager
  records and snapshot records; the files and extended attributes macOS adds
  on its own when it mounts a volume or backs up to one, such as the
  `.Spotlight-V100` and `.fseventsd` folders and Time Machine's attributes;
  and the text macOS tools print, such as `diskutil`, `fsck_apfs` and
  `apfs_checkseal` listings, kept exactly as printed. This content is not
  the work of this repository's authors, and none of the repository's
  licenses applies to it.

The release files are published as test data, so that anyone building an
APFS implementation can check what it reads against what macOS writes. They
are provided as is, without warranty of any kind.

Apple, macOS and APFS are trademarks of Apple Inc. This repository is an
independent project.
