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
- `tools`: what each executable in `apfs.fs` prints with no arguments and
  with `-h`, and what the informational ones report about a fresh and a
  populated container.
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
