# -*- coding: utf-8 -*-
"""Minimal MUMX v2 reader: sections, entry trees, name table, XML rehydration.

Clean-room implementation based on the on-disk MUMX v2 container structure.
No third-party binary is used.
"""
import ctypes
import struct
from pathlib import Path

# ---- file / section structure ------------------------------------------------

def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def i64(b, o):
    return struct.unpack_from(">q", b, o)[0]


def read_leb(buf, pos):
    value = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("LEB128 out of range")
        x = buf[pos]
        pos += 1
        value |= (x & 0x7F) << shift
        if not (x & 0x80):
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("LEB128 too long")


def sections(b):
    if b[:4] != b"MUMX":
        raise ValueError("not MUMX")
    ver = u32(b, 4)
    count = u32(b, 8)
    cur = 12
    if ver >= 2:
        cur += 8 + u32(b, 16) + 8
    out = []
    for _ in range(count):
        hv = u32(b, cur)
        st = i64(b, cur + 4)
        ps = i64(b, cur + 12)
        po = i64(b, cur + 20)
        alg = u32(b, cur + 28)
        bs = u32(b, cur + 32)
        us = i64(b, cur + 36)
        bis = i64(b, cur + 44)
        out.append(dict(hv=hv, type=st, ps=ps, po=po, alg=alg, bs=bs,
                        us=us, bis=bis, off=cur + 52))
        cur += 52 + ps
    return out


# ---- Windows XPRESS decompressor --------------------------------------------

_NTDLL = None
_FRAG_WS = 0


def _decompressor():
    global _NTDLL, _FRAG_WS
    if _NTDLL is not None:
        return _NTDLL
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.RtlGetCompressionWorkSpaceSize.argtypes = [
        ctypes.c_ushort, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong)]
    ntdll.RtlGetCompressionWorkSpaceSize.restype = ctypes.c_long
    ntdll.RtlDecompressBufferEx.argtypes = [
        ctypes.c_ushort, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    ntdll.RtlDecompressBufferEx.restype = ctypes.c_long
    c1 = ctypes.c_ulong()
    c2 = ctypes.c_ulong()
    st = ntdll.RtlGetCompressionWorkSpaceSize(3, ctypes.byref(c1), ctypes.byref(c2))
    if st != 0:
        raise RuntimeError("RtlGetCompressionWorkSpaceSize 0x%08X" % (st & 0xFFFFFFFF))
    _NTDLL = ntdll
    _FRAG_WS = c2.value
    return _NTDLL


def _compressor():
    ntdll = _decompressor()
    if not hasattr(_compressor, "_ready"):
        ntdll.RtlCompressBuffer.argtypes = [
            ctypes.c_ushort, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        ntdll.RtlCompressBuffer.restype = ctypes.c_long
        c1 = ctypes.c_ulong()
        c2 = ctypes.c_ulong()
        st = ntdll.RtlGetCompressionWorkSpaceSize(3, ctypes.byref(c1), ctypes.byref(c2))
        if st != 0:
            raise RuntimeError("compressor workspace failed 0x%08X" % (st & 0xFFFFFFFF))
        _compressor._workspace = c1.value
        _compressor._ready = True
    return ntdll


def xpress_compress(data):
    ntdll = _compressor()
    src = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cap = len(data) + 65536
    while True:
        dst = (ctypes.c_ubyte * cap)()
        final = ctypes.c_ulong()
        workspace = (ctypes.c_ubyte * _compressor._workspace)()
        st = ntdll.RtlCompressBuffer(3, src, len(data), dst, cap, 1048576,
                                     ctypes.byref(final), ctypes.byref(workspace))
        if st == 0:
            return bytes(dst[:final.value])
        # STATUS_BUFFER_TOO_SMALL
        if (st & 0xFFFFFFFF) != 0xC0000023:
            raise RuntimeError("RtlCompressBuffer 0x%08X" % (st & 0xFFFFFFFF))
        cap *= 2


def compress_stream(data, block_size=1048576):
    """Build a MUMX XPRESS stream with its block index at the end."""
    blocks = []
    offsets = []
    pos = 0
    last_u = last_c = 0
    for start in range(0, len(data), block_size):
        chunk = data[start:start + block_size]
        comp = xpress_compress(chunk)
        stored = comp if len(comp) < len(chunk) else chunk
        offsets.append((start, pos))
        blocks.append(stored)
        pos += len(stored)
        last_u = len(chunk)
        last_c = len(stored)
    index = bytearray()
    index += struct.pack(">I", 1)
    index += struct.pack(">I", block_size)
    index += struct.pack(">q", len(blocks))
    index += struct.pack(">I", last_u)
    index += struct.pack(">I", last_c)
    index += struct.pack(">I", 2)  # reserved / index sub-version (28-byte header)
    for uoff, coff in offsets:
        for v in (uoff, coff):
            while True:
                b = v & 0x7F
                v >>= 7
                if v:
                    index.append(b | 0x80)
                else:
                    index.append(b)
                    break
    return b"".join(blocks) + bytes(index), len(index)


def decompress_stream(data, compressed_alg, block_size, uncompressed_size, block_index_size):
    if compressed_alg == 0:
        return bytes(data)
    if block_index_size == 0 or block_index_size > len(data):
        raise ValueError("invalid block index size")
    idx = data[-block_index_size:]
    if u32(idx, 0) != 1:
        raise ValueError("block index version != 1")
    block_count = i64(idx, 8)
    last_uncomp = u32(idx, 16)
    last_comp = u32(idx, 20)
    pos = 28
    blocks = []
    for _ in range(block_count):
        a, pos = read_leb(idx, pos)
        c, pos = read_leb(idx, pos)
        blocks.append((a, c))
    stream = data[:len(data) - block_index_size]
    out = bytearray(uncompressed_size)
    ntdll = _decompressor()
    workspace = (ctypes.c_ubyte * _FRAG_WS)()
    for i, (uoff, coff) in enumerate(blocks):
        if i + 1 < block_count:
            usize = blocks[i + 1][0] - uoff
            csize = blocks[i + 1][1] - coff
        else:
            # The header stores the last block's sizes, not end offsets.
            usize = last_uncomp
            csize = last_comp
        if csize >= usize:
            out[uoff:uoff + usize] = stream[coff:coff + usize]
            continue
        src = (ctypes.c_ubyte * csize).from_buffer_copy(stream[coff:coff + csize])
        dst = (ctypes.c_ubyte * usize)()
        final = ctypes.c_ulong()
        st = ntdll.RtlDecompressBufferEx(3, dst, usize, src, csize,
                                         ctypes.byref(final), ctypes.byref(workspace))
        if st != 0:
            raise RuntimeError("block %d decompress 0x%08X" % (i, st & 0xFFFFFFFF))
        if final.value != usize:
            raise ValueError("block %d size mismatch" % i)
        out[uoff:uoff + usize] = bytes(dst)
    return bytes(out)


def decompress_outer(b, sec):
    return decompress_stream(b[sec["off"]:sec["off"] + sec["ps"]],
                             sec["alg"], sec["bs"], sec["us"], sec["bis"])


def decompress_pool_at(b, off):
    hv = u32(b, off)
    if hv not in (1, 2):
        raise ValueError("inner pool header version %d" % hv)
    ps = i64(b, off + 12)
    alg = u32(b, off + 28)
    bs = u32(b, off + 32)
    us = i64(b, off + 36)
    bis = i64(b, off + 44)
    poff = off + (52 if hv >= 2 else 28)
    return decompress_stream(b[poff:poff + ps], alg, bs, us, bis)


# ---- name table -------------------------------------------------------------

def parse_name_table(pool):
    names = []
    by_offset = {}
    p = 0
    while p < len(pool):
        rec = p
        n, p = read_leb(pool, p)
        if n == 0 or n > 4096 or p + n >= len(pool):
            break
        name = pool[p:p + n]
        p += n
        if pool[p] != 0:
            break
        p += 1
        by_offset[rec] = name
        names.append((rec, name))
    return names, by_offset


# ---- entry trees ------------------------------------------------------------

class Node:
    __slots__ = ("tag", "attrs", "children", "size", "hash_prefix", "declaration_id",
                 "flags", "has_hash", "hash")

    def __init__(self):
        self.tag = b""
        self.attrs = []
        self.children = []
        self.size = 0
        self.hash_prefix = 0
        self.declaration_id = 0
        self.flags = 0
        self.has_hash = False
        self.hash = b""


def read_string(pool, off):
    if off >= len(pool):
        raise ValueError("string offset out of range")
    n, p = read_leb(pool, off)
    if p + n > len(pool):
        raise ValueError("string length out of range")
    return pool[p:p + n]


def parse_node(trees, cursor, string_pool, depth=0, budget=None):
    """Return (node, cursor-after-this-node-in-the-parent).

    A ref node advances the parent cursor only past the ref; the target node is
    parsed from a separate cursor. This mirrors the C++ cursor-by-reference
    semantics in the reference reader.
    """
    if budget is None:
        budget = [2000000]
    if depth > 512 or budget[0] <= 0:
        raise ValueError("node budget/depth")
    budget[0] -= 1
    ref, cursor = read_leb(trees, cursor)
    if ref != 0:
        node, _ = parse_node(trees, ref, string_pool, depth + 1, budget)
        return node, cursor
    node = Node()
    flags, cursor = read_leb(trees, cursor)
    node.flags = flags
    if flags & 2:
        node.declaration_id, cursor = read_leb(trees, cursor)
        hlen, cursor = read_leb(trees, cursor)
        if cursor + hlen > len(trees):
            raise ValueError("hash out of range")
        node.hash = trees[cursor:cursor + hlen]
        if hlen >= 8:
            node.has_hash = True
            node.hash_prefix = int.from_bytes(node.hash[:8], "big")
        cursor += hlen
        _, cursor = read_leb(trees, cursor)  # size1
        node.size, cursor = read_leb(trees, cursor)  # size2
        if flags & 0x40:
            blen, cursor = read_leb(trees, cursor)
            cursor += blen
    tag_id, cursor = read_leb(trees, cursor)
    node.tag = read_string(string_pool, tag_id)
    ac, cursor = read_leb(trees, cursor)
    if ac > 4096:
        raise ValueError("too many attrs")
    for _ in range(ac):
        nid, cursor = read_leb(trees, cursor)
        vid, cursor = read_leb(trees, cursor)
        node.attrs.append((read_string(string_pool, nid), read_string(string_pool, vid)))
    cc, cursor = read_leb(trees, cursor)
    if cc > 4000000:
        raise ValueError("too many children")
    for _ in range(cc):
        child, cursor = parse_node(trees, cursor, string_pool, depth + 1, budget)
        node.children.append(child)
    return node, cursor


def parse_asn1_node(trees, cursor, depth=0, budget=None):
    if budget is None:
        budget = [4000000]
    if depth > 512 or budget[0] <= 0:
        raise ValueError("asn1 node budget/depth")
    budget[0] -= 1
    ref, cursor = read_leb(trees, cursor)
    if ref != 0:
        node, _ = parse_asn1_node(trees, ref, depth + 1, budget)
        return node, cursor
    node = {"flags": 0, "buffer_id": 0, "size": 0, "size1": 0, "size2": 0,
            "hash": b"", "children": []}
    flags, cursor = read_leb(trees, cursor)
    node["flags"] = flags
    if flags & 2:
        _, cursor = read_leb(trees, cursor)
        hlen, cursor = read_leb(trees, cursor)
        if hlen > 64 or cursor + hlen > len(trees):
            raise ValueError("asn1 hash out of range")
        node["hash"] = trees[cursor:cursor + hlen]
        cursor += hlen
        node["size1"], cursor = read_leb(trees, cursor)
        node["size2"], cursor = read_leb(trees, cursor)
    node["buffer_id"], cursor = read_leb(trees, cursor)
    node["size"], cursor = read_leb(trees, cursor)
    if flags & 8:
        cc, cursor = read_leb(trees, cursor)
        if cc > 1000000:
            raise ValueError("asn1 too many children")
        for _ in range(cc):
            child, cursor = parse_asn1_node(trees, cursor, depth + 1, budget)
            node["children"].append(child)
    return node, cursor


def serialize_asn1(node, data_pool, out):
    begin = node["buffer_id"]
    end = begin + node["size"]
    if begin <= len(data_pool) and end <= len(data_pool) and node["size"]:
        out += data_pool[begin:end]
    for child in node["children"]:
        serialize_asn1(child, data_pool, out)


# ---- XML serialization ------------------------------------------------------

def append_escaped(out, text, attribute):
    prev = 0
    for ch in text:
        if ch == 0x26:      # &
            out += b"&amp;"
        elif ch == 0x3C:    # <
            out += b"&lt;"
        elif ch == 0x3E:    # >
            out += b"&gt;"
        elif ch == 0x22:    # "
            out += b"&quot;" if attribute else b'"'
        elif ch == 0x0D:    # \r
            out += b"&#xD;" if attribute else b"\r"
        elif ch == 0x0A:    # \n
            if attribute:
                out += b"&#xA;"
            else:
                if prev != 0x0D:
                    out += b"\r"
                out += b"\n"
        elif ch == 0x09:    # \t
            out += b"&#x9;" if attribute else b"\t"
        else:
            out.append(ch)
        prev = ch


def subtree_has_text(node):
    return any(not child.tag for child in node.children)


def serialize_node(node, out, depth=0, inline_mode=False):
    if node.tag.startswith(b"<?xml"):
        out += node.tag
        out += b"\r\n"
        for child in node.children:
            serialize_node(child, out, 0, False)
        return
    if not node.tag:
        if node.attrs:
            append_escaped(out, node.attrs[0][1], False)
        return
    parent_formatted = not inline_mode
    has_text = subtree_has_text(node)
    indent = b"" if inline_mode else b"  " * depth
    out += indent
    out += b"<" + node.tag
    for name, value in node.attrs:
        out += b" " + name + b'="'
        append_escaped(out, value, True)
        out += b'"'
    if not node.children:
        if node.flags & 0x10:
            out += b"></" + node.tag + b">"
        else:
            out += b" />"
        if parent_formatted:
            out += b"\r\n"
        return
    out += b">"
    break_lines = parent_formatted and not has_text
    for child in node.children:
        if break_lines and not out.endswith(b"\r\n"):
            out += b"\r\n"
        serialize_node(child, out, depth + 1, inline_mode or has_text)
    if break_lines:
        if not out.endswith(b"\r\n"):
            out += b"\r\n"
        out += indent
    out += b"</" + node.tag + b">"
    if parent_formatted:
        out += b"\r\n"


def rehydrate_xml(root, string_pool):
    decl = read_string(string_pool, root.declaration_id) if root.declaration_id else b""
    if decl:
        root.tag = decl
    text = bytearray()
    if root.flags & 0x20:
        text += b"\xEF\xBB\xBF"
    serialize_node(root, text, 0, False)
    while text.endswith(b"\r\n"):
        del text[-2:]
    if root.flags & 0x08:
        text += b"\r\n"
    return bytes(text)


# ---- container reader -------------------------------------------------------

class Mumx:
    def __init__(self, path):
        self.path = Path(path)
        self.b = self.path.read_bytes()
        self.secs = sections(self.b)
        self.data_pool = None
        self.name_pool = None
        self.name_section_index = None
        self.names = []
        self.name_by_offset = {}
        self.entries = []
        self.entry_sections = []
        self._load()

    def _load(self):
        # section 0: primary data pool / string pool
        for s in self.secs:
            if s["type"] == 0:
                self.data_pool = decompress_outer(self.b, s)
                break
        if self.data_pool is None:
            raise ValueError("no section 0")

        # name table: first well-formed section type 3/4
        for s in self.secs:
            if s["type"] not in (3, 4):
                continue
            pool = decompress_outer(self.b, s)
            names, by = parse_name_table(pool)
            if len(names) > 1:
                self.name_pool = pool
                self.name_section_index = self.secs.index(s)
                self.names, self.name_by_offset = names, by
                break
        if not self.names:
            raise ValueError("no name table")

        # entry sections: type 1 (XML tree) / 2 (ASN.1)
        for s in self.secs:
            if s["type"] not in (1, 2):
                continue
            trees = decompress_pool_at(self.b, s["off"])
            v = u32(self.b, s["off"])
            size_a = i64(self.b, s["off"] + 12)
            header_b = s["off"] + (52 if v >= 2 else 28) + size_a
            table_pool = decompress_pool_at(self.b, header_b)
            table = []
            p = 0
            while p < len(table_pool):
                entry_id, p = read_leb(table_pool, p)
                offset, p = read_leb(table_pool, p)
                table.append((entry_id, offset))
                self.entries.append(dict(section=len(self.entry_sections),
                                         entry_id=entry_id, offset=offset))
            self.entry_sections.append(dict(trees=trees, table=table,
                                            asn1=(s["type"] == 2),
                                            section_type=s["type"],
                                            section_index=self.secs.index(s)))
        if not self.entries:
            raise ValueError("no entries")

        entry_by_id = {}
        for e in self.entries:
            entry_by_id.setdefault(e["entry_id"], e)
        self.entry_by_name = {}
        for rec, value in self.names:
            e = entry_by_id.get(rec)
            if e is not None:
                self.entry_by_name.setdefault(value, e)

    def entry_for_name(self, name):
        if isinstance(name, str):
            name = name.encode()
        return self.entry_by_name.get(name)

    def extract(self, name):
        e = self.entry_for_name(name)
        if e is None:
            raise KeyError(name)
        sec = self.entry_sections[e["section"]]
        if sec["asn1"]:
            root, _ = parse_asn1_node(sec["trees"], e["offset"])
            out = bytearray()
            serialize_asn1(root, self.data_pool, out)
            return bytes(out)
        root, _ = parse_node(sec["trees"], e["offset"], self.data_pool)
        return rehydrate_xml(root, self.data_pool)
