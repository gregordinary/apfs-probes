# apfs-probes

Scripts that record what Apple's APFS tools and kernel write, run on
GitHub-hosted macOS runners.

Each probe under `probe/` creates disk images, drives `newfs_apfs`,
`fsck_apfs`, `diskutil`, `hdiutil`, the other tools in
`/System/Library/Filesystems/apfs.fs` and the kernel through ordinary system
calls, and records what each one did. The `probe` workflow runs one probe on
each runner it is given:

    gh workflow run probe.yml -f probe=discover -f runners='["macos-15","macos-26"]'

A probe that takes arguments gets them from the `args` input:

    gh workflow run probe.yml -f probe=mutate -f args='loaded 1/2' -f runners='["macos-15"]'

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
- `mutate`: every byte of a fresh container's nonzero blocks, and of the
  object blocks of a container holding a small tree, changed one at a time,
  with what `fsck_apfs -n -W` says about each change. `mutate.py` tells an
  object by its checksum alone, a rule its header states with its sources,
  so the bytes are chosen by position rather than by meaning. The arguments
  `fresh` or `loaded`, and a shard `K/N`, share a sweep out between runs.
  `tools/fieldmap.py` reads a sweep back as each block's fields: adjacent
  bytes the checker complains about in the same words, with the names it
  uses for them. With `--records` it reads the B-tree nodes of the swept
  image with the layout the sweep finds, checks that reading against every
  node, and gives each byte of a key or value its place in a record, so the
  same byte of every record of a kind is read together.

- `compress`: files of known content stored compressed on an attached
  image, by `afsctool` (installed from Homebrew) with each compressor it
  offers, and by `probe/compressutil.py` through libcompression with LZVN,
  LZFSE and LZBITMAP; each file read back by the kernel after the volume is
  mounted again; and libcompression's encoding of small known inputs by every
  algorithm its public header declares.

- `lzbitmap`: libcompression's LZBITMAP encoding of 1,366 small inputs that
  each vary one thing a step at a time, such as a run's length, a match's
  distance or length, or the literals between two copies of the same bytes,
  each decoded back and kept beside its input.

- `decmpfs`: which compression type numbers the kernel reads a file under.
  Under every number from 0 to 63, in the `com.apple.decmpfs` attribute and in
  the resource fork, an attached image holds LZRAVEN's, LZMESH's and LZ4's
  encoding of known content, where libcompression declares them, and a payload no
  algorithm wrote; LZFSE and LZBITMAP at the numbers the kernel reads them
  under are the controls. Each file is read back after a remount, in a
  process of its own under a time limit.
- `typesurvey`: which compression types macOS itself writes. Every compressed
  file under `/System`, `/usr`, `/bin`, `/sbin`, `/Library` and `/Applications`
  is tallied by the type its header records, each tree within a time budget
  of its own; and contents of several kinds, among them copies of Apple's own
  executables, are copied by `ditto --hfsCompression` onto an attached image
  and onto the runner's own volume, and through `aa archive` and
  `aa extract`, with each file's type recorded.
- `inlinelimit`: where the kernel stops reading content kept after the
  compression header. Types 3, 7, 11 and 13 holding this repository's content
  compressed, zlib's raw chunk under type 3, and type 9, at attribute lengths
  a byte apart from 3,790 to 3,815, each with content that does not compress
  and with content of more than twice its encoding, read back by the kernel
  after a remount.
- `type9`: what the kernel reads compression type 9 as, the type of about
  30,000 small files each macOS release ships. Files of type 9 and 10 that
  hold this repository's bytes, varied one field at a time, are read back by
  the kernel on an attached image, with the system log's messages during each
  read. Every file of type 9 or 10 under `/System`, `/usr`, `/Library`,
  `/bin` and `/sbin` is read on the runner itself: the records keep its path,
  its sizes, verdicts about its compressed form against its content, and the
  SHA-256 of either where it is 32 bytes or longer, never the bytes. Copies
  of a sample's compression attribute, unchanged and with one byte changed at
  a time, are read back by the kernel on a second image, which is not kept.
- `type9layout`: compression type 9's layout, asked of files of this
  repository's content only: the content after the marker byte 0xCC at sizes
  from 0 to 1 MiB and at every size where the kernel stops reading it, every
  other marker value, header sizes that cut or extend
  the content, and type 10 with the content in the resource fork, alone and in
  type 8's table; each file read back by the kernel after a remount.
- `corpus`: test data for a reader of APFS. Ten containers per run cover
  every block size from 4 to 64 KiB, both kinds of name, one and two
  volumes, and two Time Machine snapshots with changes between and after
  them, so that any two of those choices appear together in some container.
  Every volume holds the same tree: every kind of file, small and large
  attributes, names that test the volume's equivalence, a directory of
  2,500 entries, clones, and a file of each compression type macOS reads
  from zlib to LZBITMAP, all of this repository's own text. Each container
  is mounted read-only twice after it is written, and each time every
  volume and snapshot is recorded as macOS reports it and the image is
  dumped again. The run checks that macOS read every file as written, that
  the two passes recorded the same, and that all three dumps are one image,
  so each manifest describes exactly the image beside it. `corpus.json`
  names each image, its volumes and their snapshots, and the manifest each
  is held to.
- `clonerefs`: what macOS records about the blocks clones share as the files
  sharing them change. Eight scenes, each in a fresh container of 4 KiB
  blocks unless another size is asked for: a written clone whose source is
  then removed; one whose source is overwritten and then emptied; a source
  written while its clone is not, then the clone written; a clone
  made after a Time Machine snapshot of the blocks the snapshot holds; a
  clone of a clone; two clones of one file; snapshots deleted while clones
  use their blocks; and a clone of a file kept in its resource fork. After
  each stage the image is checked with `fsck_apfs -n` and `-n -W` and
  dumped, and every file is read back against the content the scene's
  operations give it.
- `reformat`: whether a container made where another container was keeps
  the older one's checkpoints, and which of the two macOS and `fsck_apfs`
  then read. Two 2 GiB containers with two volumes each, one on a whole disk
  and one in a GUID partition map, are mounted, written and unmounted until
  `fsck_apfs` checks a transaction of at least 40. A copy of each is then
  made into a new container in each way Apple's tools offer: `newfs_apfs` on
  the image file, on the attached disk and on the partition; `diskutil
  eraseVolume`, `eraseDisk` and `partitionDisk`; and `diskutil apfs
  deleteContainer` followed by `createContainer` and `addVolume`. Each new
  image is dumped as it was made, checked with `fsck_apfs -n` and `-n -W`,
  attached and listed, every volume macOS lists is mounted and its root
  listed, and the image is dumped again. Each way can be run alone by naming
  it in `args`, such as `gpt-newfs`.

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
- **Content from macOS and other tools:** everything else. This includes
  the container and volume superblocks, object maps, B-trees, checkpoints,
  space-manager records and snapshot records; the files and extended
  attributes macOS adds on its own when it mounts a volume or backs up to
  one, such as the `.Spotlight-V100` and `.fseventsd` folders and Time
  Machine's attributes; the compressed forms libcompression and `afsctool`,
  a third-party tool installed from Homebrew, make of this repository's
  content; and the text macOS tools print, such as `diskutil`, `fsck_apfs`
  and `apfs_checkseal` listings, kept exactly as printed. This content is
  not the work of this repository's authors, and none of the repository's
  licenses applies to it.

The release files are published as test data, so that anyone building an
APFS implementation can check what it reads against what macOS writes. They
are provided as is, without warranty of any kind.

Apple, macOS and APFS are trademarks of Apple Inc. This repository is an
independent project.
