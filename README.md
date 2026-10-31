# MumxPack

Extract, edit and repack Windows servicing `MUMX` containers.

`MumxPack` works with both:

- `collection.mumx` - the MUMX v2 binary XML container
- `*.mumx.esd` - a WIM wrapper containing `collection.mumx`

It can unpack every named entry (`.mum`, `.cat`, `.manifest`, ...),
replace, add or delete individual entries, and write the result back to a
`collection.mumx` or a new `.mumx.esd`.

## Important: an extracted directory is not a standalone container

`extract` writes individual files. That directory is **not** a complete
MUMX container and cannot be packed by itself.

Packing always needs a base container:

- the original `*.mumx.esd`, or
- a `collection.mumx`

MumxPack is a patcher: it applies the directory's `.mum` changes onto that
base container, then writes a new ESD/container. It does not compile a new
MUMX container from a bare directory.

If the exact ESD name is missing, MumxPack 1.0.3+ uses the only sibling
`*.mumx.esd` in the parent directory. If there is no ESD or
`collection.mumx` anywhere, packing fails.

Do not use `wimlib capture` on the extracted directory as a replacement
for a MUMX ESD. That creates a WIM containing loose files, not a valid
`collection.mumx`.

## Features

- Inspect the container header and section table
- List entries and extension counts
- Extract all entries from `collection.mumx`
- Extract only `.mum` files
- Extract only `update.mum`
- Extract only one named entry
- Extract only `collection.mumx` from an ESD
- Replace / add / delete individual entries:
  - `.mum` (XML entry tree)
  - `.manifest` / `.xml` (XML entry tree)
  - `.cat` (ASN.1 binary entry)
- Repack to `collection.mumx`
- Repack to a single-file `.mumx.esd`
- No-op fast path: if nothing changed, the original container or ESD is copied byte-for-byte

## Requirements

- Windows
- Python 3.10+ (tested with Python 3.14)
- wimlib 1.14.5 is included in `vendor\`:
  - `vendor\wimlib-imagex.exe`
  - `vendor\libwim-15.dll`

`MumxPack` also accepts wimlib from `PATH` or from:

```text
set WIMLIB=C:\path\to\wimlib-imagex.exe
```

## Drag and drop

Drag files or folders onto `MumxPack.exe`:

```text
xxx.mumx.esd
  -> xxx.mumx.esd_extract\        (all entries, no container)

xxx.mumx.esd_extract\
  -> xxx.mumx.esd_extract.esd     (pack directory back to ESD)

collection.mumx
  -> collection.mumx_extract\     (all entries)
```

The console stays open after a drag operation so the result can be read.

## No-change behavior

If nothing changed, `pack` does not rebuild the ESD. It copies the source
ESD byte-for-byte and prints:

```text
NO CHANGES: copied original ESD byte-for-byte
use --repack to force a rebuild
```

This is intentional: an unchanged copy keeps the original hash and avoids
rewriting 127 MB of identical data.

To force a real recompression even when nothing changed:

```text
MumxPack pack --repack
```

Dragging `_extract` back onto the EXE requires a base `*.mumx.esd` in the
same parent directory (or a base passed explicitly on the command line).

## Commands

Run from the directory containing the `.mumx.esd`:

```text
MumxPack extract
MumxPack extract --only-mum
MumxPack extract --only-update
MumxPack extract --only-container
MumxPack extract --name "exact-name.mum"
MumxPack extract --jobs 8
MumxPack mum
MumxPack pack
MumxPack info
MumxPack list --mum
MumxPack list --mum --sizes
MumxPack dump --out sections
MumxPack index
MumxPack selftest
```

`mum` means **all `.mum` files** (same as `extract --only-mum`).
Use `extract --only-update` when only `update.mum` is wanted.

`--dir` reads `.mum`, `.cat`, `.manifest` and `.xml` files and reports
changes by type:

```text
dir unchanged 57770 changed 3 added 2 deleted 1
changed by type: {'.cat': 1, '.manifest': 1, '.mum': 1}
```

With `--sync`, a full extraction directory also deletes entries that are
missing from the directory. A directory containing only `.mum` files
only syncs `.mum` entries.

Defaults:

```text
extract destination : <esd-file-name>_extract
pack source         : <esd-file-name>_extract
pack output         : <extract-dir-name>.esd (unique suffix if it exists)
```

Examples:

```text
MumxPack extract --dest out
MumxPack pack --dir out --out out.esd
MumxPack pack --sync
MumxPack pack --force
MumxPack pack --no-rebuild-index
```

Explicit container editing:

```text
MumxPack collection.mumx new.mumx --replace "A.mum" A.mum
MumxPack collection.mumx new.mumx --add "B.mum" B.mum
MumxPack collection.mumx new.mumx --delete "C.mum"
```

## Build a single EXE

The required wimlib binaries are already in `vendor\`:

```text
vendor\wimlib-imagex.exe
vendor\libwim-15.dll
```

Then run:

```text
build.cmd
```

Output:

```text
dist\MumxPack.exe
```

The EXE is a PyInstaller one-file build and includes Python, `wimlib-imagex.exe` and `libwim-15.dll`.

## Third-party notices

`vendor\wimlib-imagex.exe` and `vendor\libwim-15.dll` are unmodified
wimlib 1.14.5 binaries, copyright 2012-2023 Eric Biggers, licensed under
the GNU General Public License version 3 or later.

See `vendor\NOTICE.txt` and `vendor\COPYING`.

## MUMX notes

`collection.mumx` is a MUMX v2 binary container with:

- section 0: string pool + raw data
- section 1: name table
- section 2/3: XML / ASN.1 entry trees
- section 4: index table

Payloads are XPRESS-compressed. The reader/writer uses Windows `RtlDecompressBufferEx` / `RtlCompressBuffer` through `ctypes`.

The header content-hash field is preserved by default. The exact Microsoft content-hash input format has not been reproduced. The package-catalog index (`PackageCatalogMap`) is rebuilt by default when entries change; use `--no-rebuild-index` to keep the original table.

Test any modified image in a VM before using it.

