# -*- coding: utf-8 -*-
"""MUMX v2 packer/patcher.

Supports replacing, adding and deleting named blobs in a collection.mumx.
Changed MUM XML is appended as a new entry tree; existing trees/offsets are
left untouched, so shared refs in the container stay valid.
"""
import argparse
import multiprocessing
import os
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.parsers.expat as expat
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mumx_lib import (Mumx, Node, serialize_node, compress_stream,  # noqa: E402
                      parse_node, parse_asn1_node, read_leb, read_string,
                      decompress_pool_at)

BOM = b"\xEF\xBB\xBF"
VERSION = "1.0.8"


def leb(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def make_inner_header(sec_type, payload_size, pool_offset, uncompressed_size,
                      block_index_size):
    return struct.pack(">IqqqIIqq", 2, sec_type, payload_size, pool_offset,
                       2, 1048576, uncompressed_size, block_index_size)


class XmlToNode:
    def __init__(self):
        self.root = None
        self.stack = []
        self.decl = None
        self.bom = False

    def _decl(self, version, encoding, standalone):
        s = '<?xml version="%s"' % (version or "1.0")
        if encoding:
            s += ' encoding="%s"' % encoding
        if standalone == 1:
            s += ' standalone="yes"'
        elif standalone == 0:
            s += ' standalone="no"'
        s += "?>"
        self.decl = s.encode("utf-8")

    def _start(self, name, attrs):
        n = Node()
        n.tag = name.encode("utf-8")
        n.attrs = [(k.encode("utf-8"), str(v).encode("utf-8")) for k, v in attrs.items()]
        n.flags = 0
        if self.root is None:
            decl = self.decl or b'<?xml version="1.0" encoding="utf-8"?>'
            r = Node()
            r.tag = decl
            r.flags = 0x06 | (0x20 if self.bom else 0)
            r.children = [n]
            self.root = r
            self.stack = [n]
        else:
            self.stack[-1].children.append(n)
            self.stack.append(n)

    def _end(self, name):
        if self.stack:
            self.stack.pop()

    def _chars(self, data):
        if not self.stack or not data.strip():
            return
        t = Node()
        t.tag = b""
        t.attrs = [(b"", data.encode("utf-8"))]
        t.flags = 0
        self.stack[-1].children.append(t)

    def parse(self, data):
        self.bom = data.startswith(BOM)
        if self.bom:
            data = data[3:]
        p = expat.ParserCreate()
        p.XmlDeclHandler = self._decl
        p.StartElementHandler = self._start
        p.EndElementHandler = self._end
        p.CharacterDataHandler = self._chars
        p.Parse(data, True)
        if self.root is None:
            raise ValueError("empty XML")
        return self.root, self.bom


def serialize_mum(root, bom, trailing):
    out = bytearray()
    serialize_node(root, out, 0, False)
    while out.endswith(b"\r\n"):
        del out[-2:]
    if bom:
        out = bytearray(BOM + bytes(out))
    if trailing:
        out += b"\r\n"
    return bytes(out)


class Builder:
    def __init__(self, m):
        self.m = m
        self.strings = bytearray(m.data_pool)
        self.names = bytearray(m.name_pool)
        self.trees = [bytearray(es["trees"]) for es in m.entry_sections]
        self.tables = [list(es["table"]) for es in m.entry_sections]
        self.entry_by_name = {k: dict(v) for k, v in m.entry_by_name.items()}
        self.dirty = False
        self.empty_off = self.add_string(b"")

    def add_string(self, value):
        off = len(self.strings)
        self.strings += leb(len(value)) + value
        return off

    def add_name(self, name):
        off = len(self.names)
        self.names += leb(len(name)) + name + b"\x00"
        return off

    def _encode_node(self, node, root=False):
        out = bytearray()
        out += leb(0)  # ref = inline
        out += leb(node.flags)
        if node.flags & 2:
            out += leb(node.declaration_id)
            out += leb(len(node.hash))
            out += node.hash
            out += leb(node.size)  # size1
            out += leb(node.size)  # size2
        if root or node.tag:
            tag_id = self.add_string(node.tag)
        else:
            tag_id = self.empty_off
        out += leb(tag_id)
        out += leb(len(node.attrs))
        for k, v in node.attrs:
            out += leb(self.add_string(k))
            out += leb(self.add_string(v))
        out += leb(len(node.children))
        for child in node.children:
            out += self._encode_node(child, False)
        return bytes(out)

    def make_tree(self, data):
        root, bom = XmlToNode().parse(data)
        trailing = data.endswith(b"\r\n")
        if trailing:
            root.flags |= 0x08
        body = serialize_mum(root, bom, trailing)
        root.size = len(body)
        root.hash = hashlib.sha256(body).digest()
        if root.children:
            root.declaration_id = self.add_string(root.tag)
        return self._encode_node(root, True)

    def _asn1_section(self):
        for i, es in enumerate(self.m.entry_sections):
            if es["asn1"]:
                return i
        raise ValueError("container has no ASN.1 entry section")

    def make_asn1_tree(self, data):
        # Store the raw blob in section 0 and point an ASN.1 leaf at it.
        off = len(self.strings)
        self.strings += data
        digest = hashlib.sha256(data).digest()
        out = bytearray()
        out += leb(0)          # ref = inline
        out += leb(2)          # flags: hash + size
        out += leb(0)          # ignored
        out += leb(len(digest)) + digest
        out += leb(len(data))  # size1
        out += leb(len(data))  # size2
        out += leb(off)        # data-pool offset
        out += leb(len(data))  # data size
        return bytes(out)

    def replace(self, name, data):
        name = name.encode("utf-8") if isinstance(name, str) else name
        entry = self.entry_by_name.get(name)
        if entry is None:
            raise KeyError("not found: %s" % name.decode("utf-8", "replace"))
        es = entry["section"]
        if self.m.extract(name) == data:
            return  # identical: leave the original tree and offsets alone
        if self.m.entry_sections[es]["asn1"]:
            tree = self.make_asn1_tree(data)
        else:
            tree = self.make_tree(data)
        off = len(self.trees[es])
        self.trees[es] += tree
        table = self.tables[es]
        for i, (eid, old) in enumerate(table):
            if eid == entry["entry_id"]:
                table[i] = (eid, off)
                break
        else:
            raise ValueError("entry table pair not found")
        self.entry_by_name[name] = dict(entry, offset=off)
        self.dirty = True

    def delete(self, name):
        name = name.encode("utf-8") if isinstance(name, str) else name
        entry = self.entry_by_name.get(name)
        if entry is None:
            raise KeyError("not found: %s" % name.decode("utf-8", "replace"))
        es = entry["section"]
        self.tables[es] = [(eid, off) for eid, off in self.tables[es]
                           if eid != entry["entry_id"]]
        del self.entry_by_name[name]
        self.dirty = True

    def add(self, name, data):
        name = name.encode("utf-8") if isinstance(name, str) else name
        if name in self.entry_by_name:
            raise KeyError("already exists: %s" % name.decode("utf-8", "replace"))
        if name.lower().endswith(b".cat"):
            es = self._asn1_section()
            tree = self.make_asn1_tree(data)
        else:
            es = 0
            tree = self.make_tree(data)
        off = len(self.trees[es])
        self.trees[es] += tree
        eid = self.add_name(name)
        self.tables[es].append((eid, off))
        self.entry_by_name[name] = dict(section=es, entry_id=eid, offset=off)
        self.dirty = True

    def rebuild_index(self):
        sec = next((s for s in self.m.secs if s["type"] == 5), None)
        if sec is None:
            return None
        payload = self.m.b[sec["off"]:sec["off"] + sec["ps"]]
        p = 0
        version, p = read_leb(payload, p)
        if version != 1:
            return None
        nlen, p = read_leb(payload, p)
        name = payload[p:p + nlen]
        p += nlen
        comparer, p = read_leb(payload, p)
        value_type, p = read_leb(payload, p)
        if value_type != 0:
            return None
        names = set(self.entry_by_name)
        mapping = {}
        for cat in names:
            if not cat.lower().endswith(b".cat"):
                continue
            mum = cat[:-4] + b".mum"
            if mum not in names:
                continue
            if mum == b"update.mum" or mum.startswith(b"Package_for_"):
                continue
            mapping[mum] = cat
        keys = sorted(mapping, key=(lambda x: x.lower()) if comparer == 1 else (lambda x: x))
        pool = bytearray()
        offsets = {}
        table = bytearray()
        def pool_string(value):
            if value in offsets:
                return offsets[value]
            off = len(pool)
            pool.extend(leb(len(value)))
            pool.extend(value)
            offsets[value] = off
            return off
        for key in keys:
            table.extend(leb(pool_string(key)))
            table.extend(leb(pool_string(mapping[key])))
        out = bytearray()
        out.extend(leb(1))
        out.extend(leb(len(name)))
        out.extend(name)
        out.extend(leb(comparer))
        out.extend(leb(value_type))
        pool_stream, pool_bis = compress_stream(bytes(pool))
        inner = struct.pack(">IqqqIIqq", 2, 0, len(pool_stream), 0,
                            2, 1048576, len(pool), pool_bis)
        inner += pool_stream
        inner += bytes(table)
        out += inner
        return bytes(out), len(keys)

    def build(self, out_path, hash_mode="preserve", rebuild_index=True,
              force_repack=False):
        m = self.m
        if not self.dirty and hash_mode == "preserve" and not force_repack:
            out = Path(out_path)
            if out.resolve() != m.path.resolve():
                out.write_bytes(m.b)
                print("NO CHANGES: copied original container byte-for-byte")
                print("use --repack to force a rebuild")
            else:
                print("no changes: input/output are the same file")
            return m.b
        sec0_stream, sec0_bis = compress_stream(bytes(self.strings))
        sec1_stream, sec1_bis = compress_stream(bytes(self.names))

        index_override = self.rebuild_index() if rebuild_index else None
        index_count = index_override[1] if index_override else None
        entry_blobs = {}
        for i, es in enumerate(m.entry_sections):
            tree_stream, tree_bis = compress_stream(bytes(self.trees[i]))
            table_bytes = b"".join(leb(eid) + leb(off) for eid, off in self.tables[i])
            table_stream, table_bis = compress_stream(table_bytes)
            outer = m.secs[es["section_index"]]
            inner_po = struct.unpack_from(">q", m.b, outer["off"] + 20)[0]
            inner = (
                make_inner_header(es["section_type"], len(tree_stream), inner_po,
                                  len(self.trees[i]), tree_bis)
                + tree_stream
                + make_inner_header(es["section_type"], len(table_stream), inner_po,
                                    len(table_bytes), table_bis)
                + table_stream
            )
            entry_blobs[es["section_index"]] = inner

        blobs = {}
        for i, sec in enumerate(m.secs):
            if i == m.name_section_index:
                blobs[i] = (sec1_stream, 2, 1048576, len(self.names), sec1_bis)
            elif i in entry_blobs:
                blobs[i] = (entry_blobs[i], 0, 0, len(entry_blobs[i]), 0)
            elif sec["type"] == 0:
                blobs[i] = (sec0_stream, 2, 1048576, len(self.strings), sec0_bis)
            elif sec["type"] == 5 and index_override is not None:
                blobs[i] = (index_override[0], 0, 0, 0, 0)
            else:
                payload = m.b[sec["off"]:sec["off"] + sec["ps"]]
                blobs[i] = (payload, sec["alg"], sec["bs"], sec["us"], sec["bis"])

        orig_hash = m.b[0x14:0x34]
        head = bytearray()
        head += b"MUMX"
        head += struct.pack(">I", 2)
        head += struct.pack(">I", len(m.secs))
        head += struct.pack(">I", 0x800C)
        head += struct.pack(">I", 32)
        head += orig_hash
        head += struct.pack(">I", 2)
        head += struct.pack(">I", 1048576)

        out = bytearray(head)
        for i, sec in enumerate(m.secs):
            payload, alg, bs, us, bis = blobs[i]
            po = index_count if sec["type"] == 5 and index_count is not None else sec["po"]
            out += struct.pack(">IqqqIIqq", 2, sec["type"], len(payload),
                               po, alg, bs, us, bis)
            out += payload

        if hash_mode == "sha256":
            # Replace the 32-byte hash with SHA-256 of the file with that field zeroed.
            base = bytearray(out)
            base[0x14:0x34] = b"\x00" * 32
            out = bytes(base[:0x14]) + hashlib.sha256(base).digest() + bytes(base[0x34:])
        Path(out_path).write_bytes(out)
        return bytes(out)


_WORKER_M = None


def _worker_init(container_path):
    global _WORKER_M
    _WORKER_M = Mumx(container_path)


def _worker_write(item):
    name, text, out_dir = item
    data = _WORKER_M.extract(name)
    path = Path(out_dir) / text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def extract_all(m, out_dir, mum_only=False, name_filter=None, jobs=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items = []
    for rec, name in m.names:
        text = name.decode("utf-8", "replace")
        if name_filter and text != name_filter:
            continue
        if mum_only and not text.lower().endswith(".mum"):
            continue
        if m.entry_for_name(name) is None:
            continue
        items.append((name, text))
    total = len(items)
    if jobs is None:
        jobs = min(8, os.cpu_count() or 1)
    jobs = max(1, min(int(jobs), 32))
    print("extracting", total, "entries ->", out, flush=True)
    if not mum_only:
        print("note: .manifest entries come first; .mum/.cat follow", flush=True)
    print("workers:", jobs, flush=True)
    if jobs > 1 and total > 1 and getattr(m, "path", None) is not None:
        tasks = [(name, text, str(out)) for name, text in items]
        try:
            with ProcessPoolExecutor(max_workers=jobs, initializer=_worker_init,
                                     initargs=(str(m.path),)) as pool:
                n = 0
                for _ in pool.map(_worker_write, tasks, chunksize=16):
                    n += 1
                    if n % 2000 == 0:
                        print("  %d/%d" % (n, total), flush=True)
            print("extracted", n, "->", out)
            return n
        except Exception as e:
            print("parallel extraction failed:", e, flush=True)
            print("falling back to single process", flush=True)
    n = 0
    for name, text in items:
        data = m.extract(name)
        p = out / text
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        n += 1
        if n % 2000 == 0:
            print("  %d/%d" % (n, total), flush=True)
    print("extracted", n, "->", out)
    return n


def pack_from_dir(m, b, src_dir, sync=False):
    src = Path(src_dir)
    allowed = (".mum", ".cat", ".manifest", ".xml")
    files = [p for p in src.iterdir()
             if p.is_file() and p.suffix.lower() in allowed]
    seen = set()
    unchanged = changed = added = 0
    changed_ext = Counter()
    added_ext = Counter()
    for p in files:
        name = p.name.encode("utf-8")
        seen.add(name)
        data = p.read_bytes()
        if m.entry_for_name(name) is not None:
            if m.extract(name) == data:
                unchanged += 1
                continue
            b.replace(name, data)
            changed += 1
            changed_ext[p.suffix.lower()] += 1
        else:
            b.add(name, data)
            added += 1
            added_ext[p.suffix.lower()] += 1
    # Only mirror all extensions when the directory is a full extraction.
    sync_all = any(p.suffix.lower() in (".cat", ".manifest", ".xml") for p in files)
    deleted = 0
    deleted_ext = Counter()
    if sync:
        for rec, name in m.names:
            if name in seen:
                continue
            if m.entry_for_name(name) is None:
                continue
            if not sync_all and not name.lower().endswith(b".mum"):
                continue
            b.delete(name)
            deleted += 1
            text = name.decode("utf-8", "replace")
            deleted_ext[Path(text).suffix.lower()] += 1
    print("dir", "unchanged", unchanged, "changed", changed,
          "added", added, "deleted", deleted)
    if changed_ext:
        print("changed by type:", dict(changed_ext))
    if added_ext:
        print("added by type:", dict(added_ext))
    if deleted_ext:
        print("deleted by type:", dict(deleted_ext))


def find_wimlib():
    env = os.environ.get("WIMLIB")
    if env and Path(env).is_file():
        return env
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
        bases.append(Path(sys.executable).parent)
    here = Path(__file__).resolve().parent
    bases.extend([here, here / "vendor", Path.cwd(), Path.cwd() / "vendor"])
    for base in bases:
        for name in ("wimlib-imagex.exe", "wimlib-imagex"):
            p = base / name
            if p.exists():
                return str(p)
    for name in ("wimlib-imagex", "wimlib-imagex.exe"):
        p = shutil.which(name)
        if p:
            return p
    raise FileNotFoundError(
        "wimlib-imagex not found; put it in vendor/ or PATH, or set WIMLIB")


def resolve_esd(arg=None):
    p = Path(arg) if arg else Path.cwd()
    if p.is_file():
        return p
    found = sorted(p.glob("*.mumx.esd"))
    if not found and p.name.endswith("_extract"):
        candidate = p.parent / p.name[:-len("_extract")]
        if candidate.is_file():
            return candidate
    if not found:
        # An extracted directory may live next to the only ESD in its parent.
        siblings = sorted(p.parent.glob("*.mumx.esd"))
        if len(siblings) == 1:
            print("using sibling ESD:", siblings[0])
            return siblings[0]
        if len(siblings) > 1:
            raise RuntimeError(
                "no exact ESD for %s; multiple siblings: %s"
                % (p, ", ".join(x.name for x in siblings)))
        raise FileNotFoundError(
                "no *.mumx.esd in %s or its parent; an extracted directory alone cannot be packed" % p)
    if len(found) > 1:
        raise RuntimeError("multiple .mumx.esd files; give one explicitly")
    return found[0]


def default_extract_dir(esd):
    return Path(esd).parent / (Path(esd).name + "_extract")


def unique_path(path):
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        candidate = path.parent / ("%s_%d%s" % (stem, i, suffix))
        if not candidate.exists():
            return candidate
    raise RuntimeError("cannot find a free output name for %s" % path)


def edited_path(path):
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    candidate = path.parent / ("%s_edited%s" % (stem, suffix))
    if not candidate.exists():
        return candidate
    for i in range(2, 1000):
        candidate = path.parent / ("%s_edited_%d%s" % (stem, i, suffix))
        if not candidate.exists():
            return candidate
    raise RuntimeError("cannot find a free output name for %s" % path)


def pack_esd(esd, workdir, out, sync=False, force=False, hash_mode="preserve",
             rebuild_index=True, force_repack=False):
    esd = Path(esd)
    workdir = Path(workdir)
    if not workdir.is_dir():
        raise FileNotFoundError("extract directory not found: %s" % workdir)
    out = Path(out)
    with tempfile.TemporaryDirectory(prefix="MumxPack_src_") as tmp:
        container = esd_to_container(esd, tmp)
        m = Mumx(container)
        b = Builder(m)
        pack_from_dir(m, b, workdir, sync=sync)
        if not b.dirty and hash_mode == "preserve" and not force_repack:
            if out.exists() and not force:
                raise RuntimeError("%s exists; use -f" % out)
            if out.resolve() != esd.resolve():
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(esd, out)
            print("NO CHANGES: copied original ESD byte-for-byte ->", out)
            print("use --repack to force a rebuild")
            return out
        newc = Path(tmp) / "new.mumx"
        b.build(str(newc), hash_mode, rebuild_index=rebuild_index,
                force_repack=force_repack)
        container_to_esd(newc, out, force=force)
        print("wrote", out)
        return out


def pause():
    if not sys.stdin.isatty():
        return
    try:
        input("Press Enter to exit...")
    except EOFError:
        pass


def drag_mode(paths):
    for raw in paths:
        p = Path(raw)
        try:
            if p.is_dir():
                if not p.name.endswith("_extract"):
                    raise RuntimeError("directory name is not *_extract: %s" % p)
                if not any(p.glob("*.mum")):
                    raise RuntimeError("no .mum files in directory; refusing empty sync: %s" % p)
                esd = resolve_esd(p)
                out = unique_path(Path(str(p) + ".esd"))
                pack_esd(esd, p, out, sync=True)
            elif p.name.lower().endswith(".mumx.esd"):
                dest = default_extract_dir(p)
                if dest.exists() and any(dest.iterdir()):
                    print("output exists, not overwriting:", dest)
                    print("delete it or use extract --force")
                else:
                    auto_extract(p, dest)
            elif p.name.lower().endswith(".mumx") or p.name.lower() == "collection.mumx":
                dest = default_extract_dir(p)
                if dest.exists() and any(dest.iterdir()):
                    print("output exists, not overwriting:", dest)
                    print("delete it or use extract --force")
                else:
                    m = Mumx(p)
                    extract_all(m, dest)
            else:
                raise RuntimeError("unsupported file: %s" % p)
        except Exception as e:
            print("ERROR:", p)
            print(" ", e)
    pause()


def simple_extract(argv):
    ap = argparse.ArgumentParser(prog="MumxPack extract")
    ap.add_argument("path", nargs="?", help=".mumx.esd or directory (default: cwd)")
    ap.add_argument("--only-mum", action="store_true", help="only .mum files")
    ap.add_argument("--only-container", action="store_true",
                    help="only collection.mumx")
    ap.add_argument("--only-update", action="store_true",
                    help="only update.mum")
    ap.add_argument("--name", help="only one exact entry name")
    ap.add_argument("--jobs", type=int, default=None, help="parallel workers (default: auto)")
    ap.add_argument("--dest", help="output directory (default: <esd>_extract)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    modes = sum(bool(x) for x in (a.only_mum, a.only_container, a.only_update, a.name))
    if modes > 1:
        ap.error("choose only one of --only-mum / --only-container / --only-update / --name")
    name_filter = "update.mum" if a.only_update else a.name
    try:
        esd = resolve_esd(a.path)
        auto_extract(esd, a.dest, force=a.force, mums=a.only_mum,
                     container_only=a.only_container, name_filter=name_filter,
                     jobs=a.jobs)
    except Exception as e:
        ap.error(str(e))


def simple_pack(argv):
    ap = argparse.ArgumentParser(prog="MumxPack pack")
    ap.add_argument("path", nargs="?", help=".mumx.esd or directory (default: cwd)")
    ap.add_argument("--dir", help="extracted files (default: <esd>_extract)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--sync", action="store_true", help="delete MUMs not in --dir")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-rebuild-index", action="store_true",
                    help="keep the original index table")
    ap.add_argument("--repack", action="store_true",
                    help="rebuild even when no entries changed")
    a = ap.parse_args(argv)
    try:
        esd = resolve_esd(a.path)
        workdir = Path(a.dir) if a.dir else default_extract_dir(esd)
        out = Path(a.out) if a.out else unique_path(Path(str(workdir) + ".esd"))
        pack_esd(esd, workdir, out, sync=a.sync, force=a.force,
                 rebuild_index=not a.no_rebuild_index, force_repack=a.repack)
    except Exception as e:
        ap.error(str(e))


def interactive():
    try:
        esd = resolve_esd()
    except Exception as e:
        print(e)
        print("put a *.mumx.esd here, or use: MumxPack extract <esd> / MumxPack pack <esd>")
        return
    dest = default_extract_dir(esd)
    if not sys.stdin.isatty():
        print_extract_hint(esd)
        return
    print("found:", esd)
    print("output:", dest)
    print("  1) extract all files")
    print("  2) extract only .mum")
    print("  3) extract collection.mumx only")
    print("  4) pack to <extract-dir-name>.esd")
    print("  q) quit")
    try:
        choice = input("> ").strip().lower()
    except EOFError:
        return
    if choice == "1":
        auto_extract(esd, dest)
    elif choice == "2":
        auto_extract(esd, dest, mums=True)
    elif choice == "3":
        auto_extract(esd, dest, container_only=True)
    elif choice == "4":
        pack_esd(esd, dest, unique_path(Path(str(dest) + ".esd")))
    if getattr(sys, "frozen", False):
        pause()


def print_extract_hint(esd=None):
    print("no action taken.")
    if esd:
        print("input:", esd)
    else:
        found = sorted(Path.cwd().glob("*.mumx.esd"))
        if found:
            print("found:", ", ".join(str(p) for p in found))
    print("choose explicitly (default dest: <esd-file-name>_extract):")
    print("  MumxPack extract                         extract all entries")
    print("  MumxPack extract --only-mum              extract only .mum")
    print("  MumxPack extract --only-container        extract collection.mumx only")
    print("  MumxPack pack                            pack back to <extract-dir>.esd")


def esd_to_container(esd, tmpdir):
    wimlib = find_wimlib()
    subprocess.run([wimlib, "extract", str(esd), "1", "--dest-dir=" + str(tmpdir)],
                   check=True)
    container = Path(tmpdir) / "collection.mumx"
    if not container.exists():
        raise FileNotFoundError("collection.mumx not found in %s" % esd)
    return container


def container_to_esd(container, out, force=False):
    out = Path(out)
    if out.exists() and not force:
        raise RuntimeError("%s exists; use --force" % out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wimlib = find_wimlib()
    with tempfile.TemporaryDirectory(prefix="MumxPack_wim_") as tmp:
        wimdir = Path(tmp) / "content"
        wimdir.mkdir()
        shutil.copy2(container, wimdir / "collection.mumx")
        if out.exists():
            out.unlink()
        subprocess.run([wimlib, "capture", str(wimdir), str(out), "content",
                        "--compress=none"], check=True)


def apply_ops(m, b, args):
    if args.dir:
        pack_from_dir(m, b, args.dir, sync=args.sync)
    for name, path in args.replace:
        b.replace(name, Path(path).read_bytes())
        print("replace", name)
    for name, path in args.add:
        b.add(name, Path(path).read_bytes())
        print("add", name)
    for name in args.delete:
        b.delete(name)
        print("delete", name)


def auto_extract(esd_path, dest=None, force=False, mums=False, container_only=False,
                 name_filter=None, jobs=None):
    src = Path(esd_path) if esd_path else None
    if src is None:
        found = sorted(Path.cwd().glob("*.mumx.esd"))
        if not found:
            raise FileNotFoundError("no *.mumx.esd in current directory")
        if len(found) > 1:
            raise RuntimeError("multiple .mumx.esd files; use --esd PATH")
        src = found[0]
    if dest is None:
        dest = src.parent / (src.name + "_extract")
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()) and not force:
        raise RuntimeError("%s is not empty; use --force or another --dest" % dest)
    dest.mkdir(parents=True, exist_ok=True)
    wimlib = find_wimlib()
    print("extract", src, "->", dest)
    with tempfile.TemporaryDirectory(prefix="MumxPack_") as tmp:
        subprocess.run([wimlib, "extract", str(src), "1", "--dest-dir=" + tmp],
                       check=True)
        container = Path(tmp) / "collection.mumx"
        if not container.exists():
            raise FileNotFoundError("collection.mumx not found in %s" % src)
        if container_only:
            shutil.copy2(container, dest / "collection.mumx")
            print("container only ->", dest / "collection.mumx")
            return dest
        m = Mumx(container)
        extract_all(m, dest, mum_only=mums, name_filter=name_filter, jobs=jobs)
    return dest


SECTION_NAMES = {
    0: "data-pool",
    1: "entries",
    2: "entries-alt",
    3: "name-table",
    4: "name-table-alt",
    5: "index",
}


def resolve_container(arg=None):
    p = Path(arg) if arg else Path.cwd()
    if p.is_file():
        return p
    found = sorted(p.glob("*.mumx.esd"))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise RuntimeError("multiple .mumx.esd files; give one explicitly")
    found = sorted(p.glob("collection.mumx")) or sorted(p.glob("*.mumx"))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise RuntimeError("multiple .mumx containers; give one explicitly")
    raise FileNotFoundError("no .mumx.esd or collection.mumx in %s" % p)


def load_for_read(path):
    p = Path(path)
    if p.name.lower().endswith(".esd"):
        with tempfile.TemporaryDirectory(prefix="MumxPack_read_") as tmp:
            container = esd_to_container(p, tmp)
            return Mumx(container), p, True
    return Mumx(p), p, False


def entry_size(m, name):
    e = m.entry_for_name(name)
    if e is None:
        return None
    sec = m.entry_sections[e["section"]]
    if sec["asn1"]:
        root, _ = parse_asn1_node(sec["trees"], e["offset"])
        return root["size2"]
    root, _ = parse_node(sec["trees"], e["offset"], m.data_pool)
    return root.size


def simple_info(argv):
    ap = argparse.ArgumentParser(prog="MumxPack info")
    ap.add_argument("path", nargs="?", help=".mumx.esd, collection.mumx, or directory")
    a = ap.parse_args(argv)
    try:
        path = resolve_container(a.path)
        m, src, is_esd = load_for_read(path)
        import struct as _struct
        version = _struct.unpack_from(">I", m.b, 4)[0]
        section_count = _struct.unpack_from(">I", m.b, 8)[0]
        hash_alg = _struct.unpack_from(">I", m.b, 12)[0]
        hash_len = _struct.unpack_from(">I", m.b, 16)[0]
        content_hash = m.b[0x14:0x14 + hash_len]
        default_alg = _struct.unpack_from(">I", m.b, 0x14 + hash_len)[0]
        default_block = _struct.unpack_from(">I", m.b, 0x18 + hash_len)[0]
        print("source        :", src)
        print("wim wrapper   :", "yes" if is_esd else "no")
        print("file size     :", len(m.b))
        print("MUMX version  :", version)
        print("sections      :", section_count)
        print("hash alg      : 0x%08X" % hash_alg)
        print("hash          :", content_hash.hex())
        print("default alg   :", default_alg)
        print("default block :", default_block)
        print("entries       :", len(m.entries))
        print("names         :", len(m.names))
        print("extensions:")
        counts = Counter()
        for _, name in m.names:
            text = name.decode("utf-8", "replace")
            counts[text.rsplit(".", 1)[-1].lower() if "." in text else ""] += 1
        for ext, n in counts.most_common():
            print("  .%-12s %d" % (ext, n))
        print("sections:")
        for i, s in enumerate(m.secs):
            print("  [%d] type=%d %-14s payload=%d alg=%d block=%d uncompressed=%d index=%d"
                  % (i, s["type"], SECTION_NAMES.get(s["type"], "?"), s["ps"],
                     s["alg"], s["bs"], s["us"], s["bis"]))
    except Exception as e:
        ap.error(str(e))


def simple_list(argv):
    ap = argparse.ArgumentParser(prog="MumxPack list")
    ap.add_argument("path", nargs="?", help=".mumx.esd, collection.mumx, or directory")
    ap.add_argument("--mum", action="store_true", help="only .mum")
    ap.add_argument("--all", action="store_true", help="print all matching names")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--sizes", action="store_true", help="print entry sizes")
    a = ap.parse_args(argv)
    try:
        path = resolve_container(a.path)
        m, src, _ = load_for_read(path)
        names = []
        for _, name in m.names:
            text = name.decode("utf-8", "replace")
            if a.mum and not text.lower().endswith(".mum"):
                continue
            if m.entry_for_name(name) is None:
                continue
            names.append(name)
        print("source:", src)
        print("entries:", len(names))
        limit = len(names) if a.all else a.limit
        for name in names[:limit]:
            text = name.decode("utf-8", "replace")
            if a.sizes:
                size = entry_size(m, name)
                print("%10s  %s" % (size if size is not None else "?", text))
            else:
                print(text)
        if limit < len(names):
            print("... %d more (use --all)" % (len(names) - limit))
    except Exception as e:
        ap.error(str(e))


def simple_dump(argv):
    ap = argparse.ArgumentParser(prog="MumxPack dump")
    ap.add_argument("path", nargs="?", help=".mumx.esd, collection.mumx, or directory")
    ap.add_argument("--out", help="output directory (default: <name>_sections)")
    ap.add_argument("--section", type=int, help="section index (default: all)")
    a = ap.parse_args(argv)
    try:
        path = resolve_container(a.path)
        m, src, _ = load_for_read(path)
        out = Path(a.out) if a.out else Path(path).parent / (Path(path).name + "_sections")
        out.mkdir(parents=True, exist_ok=True)
        by_index = {es["section_index"]: es for es in m.entry_sections}
        for i, s in enumerate(m.secs):
            if a.section is not None and i != a.section:
                continue
            if s["type"] == 0:
                (out / ("section%d_data.bin" % i)).write_bytes(m.data_pool)
            elif s["type"] in (3, 4):
                (out / ("section%d_names.bin" % i)).write_bytes(m.name_pool)
            elif s["type"] in (1, 2):
                es = by_index[i]
                (out / ("section%d_trees.bin" % i)).write_bytes(es["trees"])
                table = b"".join(leb(eid) + leb(off) for eid, off in es["table"])
                (out / ("section%d_table.bin" % i)).write_bytes(table)
            else:
                (out / ("section%d.bin" % i)).write_bytes(
                    m.b[s["off"]:s["off"] + s["ps"]])
            print("section", i, "->", out)
    except Exception as e:
        ap.error(str(e))


def simple_index(argv):
    ap = argparse.ArgumentParser(prog="MumxPack index")
    ap.add_argument("path", nargs="?", help=".mumx.esd, collection.mumx, or directory")
    ap.add_argument("key", nargs="?", help="optional key to look up")
    a = ap.parse_args(argv)
    try:
        path = resolve_container(a.path)
        m, src, _ = load_for_read(path)
        print("source:", src)
        for i, s in enumerate(m.secs):
            if s["type"] != 5:
                continue
            payload = m.b[s["off"]:s["off"] + s["ps"]]
            p = 0
            version, p = read_leb(payload, p)
            if version != 1:
                print("index", i, "unsupported version", version)
                continue
            nlen, p = read_leb(payload, p)
            name = payload[p:p + nlen].decode("utf-8", "replace")
            p += nlen
            comparer, p = read_leb(payload, p)
            value_type, p = read_leb(payload, p)
            print("index %d: name=%s comparer=%d valueType=%d entries=%d"
                  % (i, name, comparer, value_type, s["po"]))
            if a.key is None:
                continue
            inner_off = s["off"] + p
            inner_version = struct.unpack_from(">I", m.b, inner_off)[0]
            inner_ps = struct.unpack_from(">q", m.b, inner_off + 12)[0]
            header_size = 52 if inner_version >= 2 else 28
            pool = decompress_pool_at(m.b, inner_off)
            table_off = inner_off + header_size + inner_ps
            table_end = s["off"] + s["ps"]
            table = m.b[table_off:table_end]
            q = 0
            found = False
            while q < len(table):
                try:
                    key_id, q = read_leb(table, q)
                    value, q = read_leb(table, q)
                except Exception:
                    break
                try:
                    key = read_string(pool, key_id).decode("utf-8", "replace")
                except Exception:
                    continue
                if key != a.key:
                    continue
                found = True
                if value_type == 0:
                    out = read_string(pool, value).decode("utf-8", "replace")
                elif value_type == 1:
                    out = "true" if value else "false"
                elif value_type == 2:
                    out = str(value)
                else:
                    out = str(value)
                print("  key:", key)
                print("  value:", out)
            if not found:
                print("  key not found:", a.key)
    except Exception as e:
        ap.error(str(e))


def simple_selftest(argv):
    ap = argparse.ArgumentParser(prog="MumxPack selftest")
    ap.add_argument("path", nargs="?", help=".mumx.esd, collection.mumx, or directory")
    a = ap.parse_args(argv)
    try:
        path = resolve_container(a.path)
        m, src, _ = load_for_read(path)
        print("OK:", src)
        print("sections:", len(m.secs), "entries:", len(m.entries), "names:", len(m.names))
    except Exception as e:
        ap.error(str(e))


def print_help():
    print("""MumxPack - MUMX extract/pack tool

Usage:
  MumxPack --version                    show version
  MumxPack                              interactive menu
  MumxPack extract [file|dir]           extract all entries
  MumxPack extract --only-mum           extract all .mum files
  MumxPack extract --only-update        extract only update.mum
  MumxPack mum                          same as extract --only-mum
  MumxPack extract --only-container     extract collection.mumx only
  MumxPack pack [file|dir]              pack back to <extract-dir>.esd
  MumxPack pack --sync                  delete .mum not present in directory
  MumxPack pack --dir DIR               use a specific extract directory
  MumxPack pack --out FILE              use a specific output name
  MumxPack pack --force                 overwrite output
  MumxPack pack --no-rebuild-index      keep the original index table
  MumxPack pack --repack                rebuild even if nothing changed
  MumxPack info [file|dir]              show header and sections
  MumxPack list [file|dir] [--mum]     list entry names
  MumxPack dump [file|dir]             dump decompressed sections
  MumxPack index [file|dir]            show index table headers
  MumxPack selftest [file|dir]         load and validate the container

Drag and drop:
  xxx.mumx.esd          -> xxx.mumx.esd_extract\\
  xxx.mumx.esd_extract  -> xxx.mumx.esd_extract.esd
  collection.mumx       -> collection.mumx_extract\\

Defaults:
  extract destination   <esd-file-name>_extract
  pack source           <esd-file-name>_extract
  pack output           <extract-dir-name>.esd (unique suffix if exists)

Packing requires a base .mumx.esd or collection.mumx.
An extracted directory alone is not a complete container.
""")


def main():
    argv = sys.argv[1:]
    if argv in (["-h"], ["--help"]):
        print_help()
        return
    if argv in (["-V"], ["--version"]):
        print("MumxPack", VERSION)
        return
    if not argv:
        interactive()
        return
    if argv[0] in ("extract", "unpack"):
        simple_extract(argv[1:])
        return
    if argv[0] in ("pack", "repack"):
        simple_pack(argv[1:])
        return
    if argv[0] == "info":
        simple_info(argv[1:])
        return
    if argv[0] == "list":
        simple_list(argv[1:])
        return
    if argv[0] == "dump":
        simple_dump(argv[1:])
        return
    if argv[0] == "index":
        simple_index(argv[1:])
        return
    if argv[0] == "selftest":
        simple_selftest(argv[1:])
        return
    if argv[0] == "mum":
        simple_extract(["--only-mum"] + argv[1:])
        return
    if not argv[0].startswith("-") and Path(argv[0]).exists():
        drag_mode(argv)
        return

    ap = argparse.ArgumentParser(prog="MumxPack")
    ap.add_argument("container", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--all", action="store_true", help="extract all entries (default is no action)")
    ap.add_argument("--esd", metavar="FILE", help=".mumx.esd to extract (default: find in cwd)")
    ap.add_argument("--dest", default=None, metavar="DIR",
                    help="extract destination (default: <esd-file-name>_extract)")
    ap.add_argument("--force", action="store_true", help="allow non-empty --dest")
    ap.add_argument("--mums", action="store_true", help="unpack only .mum files")
    ap.add_argument("--container-only", action="store_true",
                    help="only extract collection.mumx, do not unpack its entries")
    ap.add_argument("--extract", metavar="DIR")
    ap.add_argument("--mum", action="store_true", help="extract only .mum files")
    ap.add_argument("--jobs", type=int, default=None, help="parallel workers (default: auto)")
    ap.add_argument("--dir", metavar="DIR", help="replace/add MUMs from directory")
    ap.add_argument("--sync", action="store_true", help="delete .mum not present in --dir")
    ap.add_argument("--replace", nargs=2, action="append", default=[],
                    metavar=("NAME", "FILE"))
    ap.add_argument("--add", nargs=2, action="append", default=[],
                    metavar=("NAME", "FILE"))
    ap.add_argument("--delete", action="append", default=[], metavar="NAME")
    ap.add_argument("--hash", choices=["preserve", "sha256"], default="preserve")
    args = ap.parse_args()

    raw_input = args.container or args.esd
    pack_ops = bool(args.dir or args.replace or args.add or args.delete or args.out)
    extract_action = args.all or args.mums or args.container_only
    if args.esd and not args.container and not pack_ops and not args.extract:
        if not extract_action:
            print_extract_hint(args.esd)
            return
        try:
            auto_extract(args.esd, args.dest, force=args.force, mums=args.mums,
                         container_only=args.container_only, jobs=args.jobs)
        except Exception as e:
            ap.error(str(e))
        return
    if not raw_input:
        if args.extract:
            ap.error("--extract needs a container path")
        if not extract_action:
            print_extract_hint()
            return
        try:
            auto_extract(args.esd, args.dest, force=args.force, mums=args.mums,
                         container_only=args.container_only, jobs=args.jobs)
        except Exception as e:
            ap.error(str(e))
        return

    is_esd = Path(raw_input).suffix.lower() == ".esd"
    if is_esd and extract_action and not args.out and not args.extract and not pack_ops:
        try:
            auto_extract(raw_input, args.dest, force=args.force, mums=args.mums,
                         container_only=args.container_only)
        except Exception as e:
            ap.error(str(e))
        return
    if is_esd:
        if not args.out and not args.extract and not pack_ops:
            print_extract_hint(raw_input)
            return
        if not args.out and (args.dir or args.replace or args.add or args.delete):
            workdir = Path(args.dir) if args.dir else default_extract_dir(Path(raw_input))
            args.out = str(unique_path(Path(str(workdir) + ".esd")))
        if not args.out and not args.extract:
            ap.error("output is required; e.g. extract-dir-name.esd")
        try:
            with tempfile.TemporaryDirectory(prefix="MumxPack_src_") as tmp:
                container = esd_to_container(raw_input, tmp)
                m = Mumx(container)
                if args.extract:
                    extract_all(m, args.extract, mum_only=args.mum, jobs=args.jobs)
                    if not args.out:
                        return
                b = Builder(m)
                apply_ops(m, b, args)
                if not b.dirty and args.hash == "preserve":
                    out = Path(args.out)
                    if out.exists() and not args.force:
                        raise RuntimeError("%s exists; use --force" % out)
                    if out.resolve() != Path(raw_input).resolve():
                        shutil.copy2(raw_input, out)
                    print("NO CHANGES: copied original ESD byte-for-byte ->", out)
                    return
                newc = Path(tmp) / "new.mumx"
                b.build(str(newc), args.hash)
                container_to_esd(newc, args.out, force=args.force)
                print("wrote", args.out)
                return
        except Exception as e:
            ap.error(str(e))

    if not args.container:
        try:
            auto_extract(args.esd, args.dest, force=args.force, mums=args.mums,
                         container_only=args.container_only, jobs=args.jobs)
        except Exception as e:
            ap.error(str(e))
        return

    m = Mumx(args.container)
    if args.extract:
        extract_all(m, args.extract, mum_only=args.mum, jobs=args.jobs)
        if not args.out:
            return
    if not args.out:
        ap.error("output file is required unless only --extract is used")

    b = Builder(m)
    apply_ops(m, b, args)
    data = b.build(args.out, args.hash)
    print("wrote", args.out, len(data))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
