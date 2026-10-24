# apfs-probes

Scripts that record what Apple's APFS tools and kernel write, run on
GitHub-hosted macOS runners.

Each probe under `probe/` creates disk images, drives `newfs_apfs`,
`fsck_apfs`, `diskutil`, `hdiutil`, the other tools in
`/System/Library/Filesystems/apfs.fs` and the kernel through ordinary system
calls, and records what each one did. The `probe` workflow runs one probe on
each runner it is given:

    gh workflow run probe.yml -f probe=discover -f runners='["macos-15","macos-26"]'

A probe that takes arguments gets them from the `args` input, and one that
runs past 35 minutes is given its limit by the `minutes` input:

    gh workflow run probe.yml -f probe=mutate -f args='loaded 1/2' -f runners='["macos-15"]'
    gh workflow run probe.yml -f probe=purgeable -f args='sweep 240' -f minutes=290

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
- `bounds`: fresh containers at every size in a range: by default the ranges
  between two ladder sizes laid out differently, and otherwise the ranges a
  run names as `BS/FIRST/LAST/STEP` arguments, which narrow each one to the
  size where one layout gives way to the next.
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
- `cryptogen`: what a volume records as its encryption is turned on and off,
  and volumes encrypted from the start. One volume holding a small tree is
  encrypted with a passphrase by `diskutil apfs encryptVolume`, decrypted by
  `decryptVolume`, written once more, and encrypted and decrypted again,
  each change waited on, with the volume mounted, until `diskutil apfs list`
  reports it complete; and a volume made encrypted by `newfs_apfs -E` and one
  added encrypted by `diskutil apfs addVolume -passphrase` are given the same
  tree. After each stage the container is checked with `fsck_apfs -n` and
  `-n -W` on its own disk while attached, where `fsck_apfs` reads an encrypted
  volume's keybag from, with an encrypted volume unlocked and not mounted,
  since neither tool checks a container holding a locked volume; it is checked
  on the physical disk under it and by `diskutil verifyVolume` beside that. The
  image is dumped, and every file is read back against what was written.
  The passphrase of every encrypted volume is `apfs-probes`, so each image
  can be unlocked.
- `xattrsize`: how large an extended attribute macOS sets on APFS. An
  ordinary attribute and a resource fork are each set on a fresh file at
  sizes doubling from 4 KiB to 2 GiB, then bisected to the byte at the first
  size refused, and each is read back by size. A resource fork written as
  4 KiB at position 0 and 4 KiB at 16 MiB is kept in a dumped container, for
  how a fork with a gap is stored.
- `observe`: what a runner lets a probe observe besides the images it
  leaves. One operation, sixteen files written to a mounted volume and
  synced, runs under `fs_usage`, DTrace's `io` provider and `ktrace`, and
  `fcntl` `F_LOG2PHYS_EXT` gives each file's extents, so each tracer's block
  numbers can be matched to them. The event codes `ktrace` filters on are
  found by value: the operation runs on an HFS+ volume with every class
  traced, and only the codes whose events carry the files' extents and
  lengths are kept and then used to trace the operation on a container. The
  probe also records the unified log with the APFS subsystem at debug level,
  the sysctl names and registry properties that mention APFS, the verbose
  modes of `apfs_checkseal`, `apfs_stats` and `sm_stats`, `eslogger`'s file
  events, and whether a virtual machine starts inside the runner.

- `accept`: whether macOS accepts the containers another implementation
  formats. `accept/` holds 66 containers that ferrosys's APFS writer
  formatted, each a block dump, and `images.json`, which states what macOS
  should report of each: the container's identifier and capacity, and every
  volume's name, identifier and kind of name, with its root directory's
  owner, mode, flags and times. Forty-five are empty. In the eight named
  `tree-` the first volume holds a generated tree: directories, files from
  empty to past a mebibyte, symbolic links, hard links, device nodes, pipes,
  sockets, attributes either side of 3,800 bytes, resource forks, names past
  ASCII, owners and times before 1970. In the four named `corpus-` it holds
  a volume of the `corpus` probe's containers (the `corpus-1` release), read
  back by ferrosys and written again with paths removed, replaced, hidden and
  added: the files and attributes the probe wrote, its compressed files
  passed through as macOS stored them, and nothing macOS wrote there on its
  own. Among those files is the probe's character device numbered 0,0, of
  which `fsck_apfs -W` warns on the containers macOS wrote as well, so a
  container may draw that warning once for each device node it is stated to
  hold numbered zero, and nothing besides. In the five named `fit-` the
  first volume holds a generated tree as well, and the container is the
  size ferrosys finds for it: `fit-ramdisk`, one case-sensitive volume with
  no room asked for, records no block free and is full; `fit-v2` is two
  volumes at the smallest size with a slot for each; and `fit-slack`,
  `fit-room-b4k` and `fit-room-b64k` are sized to leave room any user can
  write: a fifth of the container, 64 MiB and 256 MiB. Each of those is
  expected to take its room (`room:BYTES` in `images.json`): before the
  probe's file, the runner's own user writes that many bytes into the
  first volume, a mebibyte a write, and reads them back after. For each populated
  container, `images.json` states every
  entry as the mounted volume's manifest describes one, and what
  `apfs_checkseal -q -v` lists of it. They cover every block size, every release
  profile, sizes from the smallest to 16 TiB, one to a hundred volumes and
  both kinds of name. Twenty-five are to be accepted, and a twenty-sixth
  too but for `fsck_apfs -W`'s warning that its times are later than the
  runner's clock, which they are. Two are to be refused: one has a checksum
  left stale, and the other a free queue's node limit of zero, which
  `fsck_apfs` passes. The other seventeen measure which node limits macOS
  takes: each sets one free queue's limit, the internal pool's or the main
  device's, to a value beside the one the writer gives or beside two
  mebibytes of nodes, and each is to be refused, as the one of zero is.
  Each container goes through one batch:
  `fsck_apfs -n -W`; an attach whose `diskutil apfs list` is the one
  stated, which gives a name of more than 127 bytes as its leading whole
  characters within them; every volume mounted read-only, described by
  `diskutil info`, its entries recorded and listed again by `apfs_checkseal
  -q -v`; every volume mounted read-write and given a file, which a
  container stated full refuses for want of room (`ENOSPC`), to
  hold no file or the start of the one written when it is mounted again;
  `fsck_apfs -n -W` again, and
  the image dumped as macOS left it; and every volume mounted once more and
  the file read back. Three containers `newfs_apfs` formats on the runner go
  through the same batch, so a check that fails on Apple's own container is
  a fault of the check. `verdicts.json` gives each container's verdict beside
  what was expected of it, with the release. The argument `few` runs the
  containers of at most five volumes and `many` the one of a hundred;
  `controls` runs the three `newfs_apfs` containers, `limits` the
  seventeen that set a node limit, `trees` the eight that hold a tree,
  `corpus` the four that hold a corpus volume, and `fit` the five sized to
  their tree; `root` runs the four named `root-`, each of whose volumes
  holds a snapshot of itself that it roots from: a root's shape, one named
  with 255 bytes, the largest block size, and two volumes with one each.
  Each such snapshot is to be listed by `diskutil apfs listSnapshots` alone,
  by its name, identifier and transaction, as the one its volume roots to,
  and to mount read-only holding the volume as it was written, in the
  read-only pass and again after the file is written. Of a populated volume, every
  entry is held by path to what is stated:
  its inode number, mode, owner, group, flags and four times; a file's link
  count, size and content, a link's target, a device's number, a
  directory's names, and every entry's attributes, a resource fork among a
  regular file's alone, since `listxattr` lists none on another kind; and
  `apfs_checkseal`'s line for it, with the attributes it lists held to the
  inode, since it lists an inode of several names' attributes once. What
  macOS reports of a directory's link count and size, and of a file's
  blocks, is recorded beside the verdict.

- `resize`: what a container records once `diskutil apfs resizeContainer`
  has resized it. Two sparse 64 GiB images are partitioned by `diskutil
  partitionDisk`. One holds a 2 GiB APFS partition with the rest of the disk
  free, and its container is shrunk to 1 GiB and then grown to the end of
  the disk; the other's partition covers the disk, and its container is
  shrunk to 2 GiB. After each step the volume is mounted read-write and
  given a file, `fsck_apfs -n` checks the partition, `diskutil info`
  records where it begins, and the whole image is dumped.

- `records`: three things a populated volume's records hold that no earlier
  image shows. `names` creates names that share one 22-bit directory hash,
  some of one length and some of two, and two groups of three, each group in
  the order listed in one directory and in the opposite order in another, on
  a volume that folds case and on one formatted with `newfs_apfs -e`. `attrs`
  sets an ordinary attribute at every size from 1 to 4,096 bytes and makes a
  symbolic link at every target length from 1 to 1,023 bytes, to show where
  each stops being held in its record, and `attrnames` sets attributes named
  with 1, 10 and 120 bytes at every size from 3,780 to 3,820 bytes, to show
  whether that size depends on the name. `blocks` copies the fixed tree of
  `opsutil.py` with `ditto` into containers formatted at 8, 16 and 64 KiB
  blocks. Each container is unmounted, mounted again and looked up by path,
  checked with `fsck_apfs -n -W` and dumped. The arguments `names`, `attrs`,
  `attrnames` and `blocks` run one part each; no argument runs all four. The
  names include pairs that set one candidate order against another: a
  shorter name greater in bytes, a capital that sorts the other way once
  folded, and a precomposed letter that sorts the other way decomposed.
- `ramdisk`: what macOS reads of Apple's restore RAM disk, the container
  the macOS 26.6.2 restore image carries. The runner fetches that one member
  of the restore image from Apple's CDN by range requests
  (`apple-ramdisk.py`), holds it and the container inside it to their
  sha256, attaches the container read-only, and records every entry as the
  `corpus` probe records a volume's: names, modes, owners, flags, four
  times, sizes, link targets, each attribute's name, length and digest, a
  compressed file's type, and each file's digest. The container is deleted
  on the runner, and no byte of it is kept.
- `reserve`: how much of a container's free space macOS writes into.
  `reserve/` holds 47 containers that ferrosys's APFS writer formatted, each
  around one file of zeros and sized to leave a stated count of blocks
  free, 300, 2,500 or 12,000: every block size, containers from about a
  thousand blocks to two million, and two of two volumes. A zero block is left out of
  a block dump, so the zeros cost nothing to send. Each trial restores a
  container, mounts its first volume read-write and fills it until a call
  fails: `block` writes a block a call, each synced, as root; `user` does
  the same as the runner's own user; `mib` writes 1 MiB a call and then a
  block a call; `files` creates empty files; `trace` and `stream` write a
  block a call (synced) and 1 MiB a call, and record wherever the blocks
  available or the file's allocated blocks move by more than a write. A
  trial named `KIND.K` is one of several of a kind, each on a fresh copy. Each records what it took,
  the call that failed and its errno, and `statvfs` along the way, then
  whether an empty file and a block still go in. The second volume of a
  two-volume container is asked for one block after the first is full.
  `sm_stats -v` reads the free extents before and after, and `fsck_apfs -n
  -W` checks the container after. The kernel names the blocks a change
  asks for and the margin beside them where it refuses one, and
  `system-log.ndjson` keeps those messages, with `times/` bounding each
  trial's. `results.json` gathers every trial's record. Arguments narrow
  the run: a block size (`b4k`), a trial's kind, `again` for the repeated
  trials, a container's name, and a last `K/N` that takes the Kth of N
  equal shares.
- `xattrnames`: which extended attribute names macOS takes. On a fresh
  container `newfs_apfs` formats, `setxattr` is asked for names of every
  length to 300 bytes and longer, of characters of two to four bytes to
  either side of 127 bytes, and of the `security.`, `trusted.`, `user.`
  and `system.posix_acl_` namespaces, each read back, listed and removed.
  `xattrnames/` holds three containers ferrosys's APFS writer formatted:
  two with attributes named up to the longest it holds at 4 and 64 KiB
  blocks, and one with names of 127 bytes at most; each with attributes of
  those namespaces, among them two POSIX ACLs. Each is
  checked by `fsck_apfs -n -W`, its volume mounted read-only, listed, read
  and listed by `apfs_checkseal -q -v`; mounted read-write, an attribute set
  on every file and each one it holds set again; then checked and listed
  again, and dumped.

- `purgeable`: where the `Purgeable` word `diskutil apfs listSnapshots`
  reports of a snapshot comes from. `tag` asks `apfs_systemsnapshot`, as
  root, to take a snapshot of a container `newfs_apfs` formats and to tag it
  as the one to root from, and to tag the snapshot `accept/root-ramdisk.bd`
  holds, listing the snapshots after each step and dumping both containers.
  `sweep MINUTES` changes the objects that describe that container's
  snapshot: its volume superblock and the snapshot's copy of it, the object
  holding the snapshot's identifier, the object maps, and the root nodes of
  the trees of snapshots, of the object map's snapshots and of extent
  references. Each try inverts some bits of one 32-bit word, writes the
  object's check words again, attaches the image, mounts its volume
  read-only and lists its snapshots. Every word is first inverted whole, and
  a word whose listing differs from the unchanged image's is split in halves
  down to the single bits that change it. `sweep/summary.json` names those
  bits.

- `group`: what macOS writes when it makes a System and Data volume group,
  and how a firmlink is kept. `firmlinks` reads `/usr/share/firmlinks` and
  the directories it names, at `/` and through the system's snapshot mounted
  read-only on its own, and keeps only counts and flag values: the file's
  lines and fields, how many fields are absolute, the directories'
  `st_flags` beside those of `/bin`, the names of their `com.apple.fs.`
  attributes and how each value relates to its line's fields. No path or
  value of the file is kept. `group` makes an empty container with `diskutil
  apfs createContainer` and adds a System volume with the system role and a
  Data volume with the data role grouped with it, gives Data a few
  directories and files and System two directories, one kept as a firmlink
  to Data's `Users` with what `firmlinks` found and one left plain, and
  mounts both again. Each stage lists the container, its volume groups and
  its volumes, and ends in `fsck_apfs -n -W` and a dump.

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
Apache-2.0 (`LICENSE-APACHE`), at your option, the containers under
`accept/` among it. The files attached to its releases are covered in the
next section.

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
