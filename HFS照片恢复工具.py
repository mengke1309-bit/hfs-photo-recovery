# -*- coding: utf-8 -*-
"""
HFS+ 移动硬盘照片恢复工具（Windows 版）· 两段式 · Apple 风格 UI
=================================================================
流程：扫描（只读，不写任何文件）→ 浏览清单 + 缩略图预览 → 勾选 → 恢复选中文件。

原理：文件雕刻（File Carving）。不依赖文件系统（HFS+/exFAT/NTFS 均可），
直接以只读方式扫描硬盘原始扇区，按照片文件的二进制特征定位照片。

支持：JPEG / 索尼ARW / 尼康NEF / 佳能CR2、CR3 / 奥林巴斯ORF / 松下RW2 /
      宾得PEF / 通用DNG / 富士RAF

安全：源盘全程只读；恢复目标目录强制校验不得在源盘上。

用法：py -3 HFS照片恢复工具.py   （或直接双击打包好的 exe）
"""

import ctypes
import io
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import traceback

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext, font as tkfont
except ImportError:
    print("未找到 tkinter。请使用 python.org 官方版 Python 运行：py -3 本脚本.py")
    sys.exit(1)

try:
    from PIL import Image, ImageTk, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ----------------------------- 常量 -----------------------------

CHUNK = 8 * 1024 * 1024
OVERLAP = 512                 # 跨块重叠（扇区整数倍）
SECTOR = 512                  # 物理盘扇区大小
SUB = 1024 * 1024             # 坏块抢救粒度 1MB
MAX_CARVE = 512 * 1024 * 1024
MIN_JPEG = 100 * 1024         # JPEG 最小有效尺寸（过滤缩略图/内嵌预览）
MIN_RAW = 100 * 1024
PREVIEW_JPEG_CAP = 32 * 1024 * 1024
PREVIEW_EMBED_CAP = 24 * 1024 * 1024

MAKE_TO_EXT = [
    ("SONY", ".arw"), ("NIKON", ".nef"), ("CANON", ".cr2"),
    ("OLYMPUS", ".orf"), ("PANASONIC", ".rw2"), ("PENTAX", ".pef"),
    ("LEICA", ".dng"), ("HASSELBLAD", ".3fr"), ("FUJIFILM", ".raf"),
]

# ----------------------------- 提权 -----------------------------

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def relaunch_as_admin():
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, '"%s"' % os.path.abspath(__file__), None, 1)
    sys.exit(0)

# ----------------------------- 磁盘枚举 -----------------------------

def ps_json(cmd):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True, text=True, timeout=60)
    data = out.stdout.strip()
    if not data:
        return []
    obj = json.loads(data)
    return obj if isinstance(obj, list) else [obj]

def list_disks():
    rows = ps_json(
        "Get-Disk | Select-Object Number,FriendlyName,Size,BusType "
        "| ConvertTo-Json -Compress")
    disks = []
    for r in rows:
        try:
            disks.append({
                "num": int(r["Number"]),
                "name": str(r.get("FriendlyName") or ""),
                "size": int(r.get("Size") or 0),
                "bus": str(r.get("BusType") or ""),
            })
        except Exception:
            pass
    return disks

def disk_num_of_drive_letter(letter):
    rows = ps_json(
        "Get-Partition -DriveLetter %s | Select-Object DiskNumber | ConvertTo-Json -Compress"
        % letter.upper())
    if rows:
        try:
            return int(rows[0]["DiskNumber"])
        except Exception:
            return None
    return None

# ----------------------------- 各格式长度解析 -----------------------------

def carve_jpeg(fh, start):
    fh.seek(start)
    total = 0
    carry = b""
    while total < MAX_CARVE:
        chunk = fh.read(64 * 1024)  # 小步读取，避免无关区域坏扇区拖垮整个文件
        if not chunk:
            return None
        buf = carry + chunk
        i = buf.find(b"\xff\xd9", 1)
        if i >= 0:
            length = total - len(carry) + i + 2
            return length if length >= MIN_JPEG else None
        carry = buf[-1:]
        total += len(chunk)
    return None

def _tiff_walk(fh, start, endian, ifd_off, ends, make_box, flags, visited, preview_box):
    if ifd_off in visited or ifd_off <= 0 or ifd_off > 64 * 1024 * 1024:
        return
    visited.add(ifd_off)
    fh.seek(start + ifd_off)
    raw = fh.read(2)
    if len(raw) < 2:
        return
    n = struct.unpack(endian + "H", raw)[0]
    if n > 400:
        return
    entries = fh.read(n * 12)
    if len(entries) < n * 12:
        return
    fh.seek(start + ifd_off + 2 + n * 12)
    nb = fh.read(4)
    ends.append(ifd_off + 2 + n * 12 + 4)

    tags = {}
    for i in range(n):
        ent = entries[i * 12:(i + 1) * 12]
        tag, typ, cnt, val = struct.unpack(endian + "HHII", ent)
        unit = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}.get(typ, 1)
        total = cnt * unit
        if total <= 4:
            vbytes = ent[8:12][:total]
        else:
            if val > MAX_CARVE or total > 1024 * 1024:
                continue
            fh.seek(start + val)
            vbytes = fh.read(total)
            ends.append(val + total)
            if len(vbytes) < total:
                continue
        tags[tag] = (typ, cnt, vbytes)

    def ints_of(tag):
        if tag not in tags:
            return []
        typ, cnt, vb = tags[tag]
        if typ == 3:
            return [struct.unpack(endian + "H", vb[j * 2:j * 2 + 2])[0]
                    for j in range(min(cnt, len(vb) // 2))]
        return [struct.unpack(endian + "I", vb[j * 4:j * 4 + 4])[0]
                for j in range(min(cnt, len(vb) // 4))]

    if 0x010F in tags and make_box[0] is None:
        make_box[0] = tags[0x010F][2].split(b"\x00")[0].decode("ascii", "ignore").strip().upper()
    if 0xC612 in tags:
        flags["dng"] = True
    if preview_box[0] is None and 0x201 in tags and 0x202 in tags:
        o, c = ints_of(0x201), ints_of(0x202)
        if o and c and 0 < o[0] < MAX_CARVE and 0 < c[0] < PREVIEW_EMBED_CAP:
            preview_box[0] = (o[0], c[0])
    if 0x8769 in tags:
        for v in ints_of(0x8769)[:1]:
            _tiff_walk(fh, start, endian, v, ends, make_box, flags, visited, preview_box)
    if 0x14A in tags:
        for v in ints_of(0x14A):
            _tiff_walk(fh, start, endian, v, ends, make_box, flags, visited, preview_box)

    for o_tag, c_tag in ((0x111, 0x117), (0x144, 0x145), (0x201, 0x202)):
        offs, cnts = ints_of(o_tag), ints_of(c_tag)
        if len(cnts) == 1 and len(offs) > 1:
            cnts = cnts * len(offs)
        for o, c in zip(offs, cnts):
            if 0 < o < MAX_CARVE and 0 < c < MAX_CARVE:
                ends.append(o + c)

    if len(nb) == 4:
        nxt = struct.unpack(endian + "I", nb)[0]
        if nxt:
            _tiff_walk(fh, start, endian, nxt, ends, make_box, flags, visited, preview_box)

def parse_tiff(fh, start):
    fh.seek(start)
    hdr = fh.read(16)
    if len(hdr) < 16:
        return None
    if hdr[:2] == b"II":
        endian = "<"
    elif hdr[:2] == b"MM":
        endian = ">"
    else:
        return None
    if struct.unpack(endian + "H", hdr[2:4])[0] != 42:
        return None
    ifd0 = struct.unpack(endian + "I", hdr[4:8])[0]
    is_cr2 = hdr[8:10] == b"CR"

    ends, make_box, flags, visited, preview_box = [8], [None], {"dng": False}, set(), [None]
    try:
        _tiff_walk(fh, start, endian, ifd0, ends, make_box, flags, visited, preview_box)
    except Exception:
        return None
    size = max(ends) if ends else 0
    if size < MIN_RAW or size > MAX_CARVE:
        return None

    make = make_box[0] or ""
    if is_cr2:
        ext = ".cr2"
    elif flags["dng"]:
        ext = ".dng"
    else:
        ext = ".tiff"
        for key, e in MAKE_TO_EXT:
            if key in make:
                ext = e
                break
    return {"size": size, "ext": ext, "preview": preview_box[0]}

def parse_raf(fh, start):
    fh.seek(start)
    h = fh.read(0x64)
    if len(h) < 0x64 or not h.startswith(b"FUJIFILMCCD-RAW"):
        return None
    joff, jlen, coff, clen = struct.unpack(">IIII", h[0x54:0x64])
    size = max(joff + jlen, coff + clen)
    if size < MIN_RAW or size > MAX_CARVE:
        return None
    prev = (joff, jlen) if 0 < joff < MAX_CARVE and 0 < jlen < PREVIEW_EMBED_CAP else None
    return {"size": size, "ext": ".raf", "preview": prev}

# ----------------------------- EXIF 读取（拍摄时间等） -----------------------------

def _read_ifd_entries(fh, start, endian, ifd_off):
    fh.seek(start + ifd_off)
    raw = fh.read(2)
    if len(raw) < 2:
        return {}
    n = struct.unpack(endian + "H", raw)[0]
    if n > 300:
        return {}
    entries = fh.read(n * 12)
    if len(entries) < n * 12:
        return {}
    tags = {}
    for i in range(n):
        ent = entries[i * 12:(i + 1) * 12]
        tag, typ, cnt, val = struct.unpack(endian + "HHII", ent)
        unit = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}.get(typ, 1)
        total = cnt * unit
        if total <= 4:
            vb = ent[8:12][:total]
        else:
            if val > 16 * 1024 * 1024 or total > 4096:
                continue
            fh.seek(start + val)
            vb = fh.read(total)
            if len(vb) < total:
                continue
        tags[tag] = (typ, cnt, vb)
    return tags

def _tag_str(tags, tag):
    if tag not in tags:
        return None
    s = tags[tag][2].split(b"\x00")[0].decode("ascii", "ignore").strip()
    return s or None

def read_exif_tiff(fh, start):
    """从 TIFF/EXIF 结构读取拍摄时间（DateTimeOriginal 优先）"""
    try:
        fh.seek(start)
        hdr = fh.read(8)
        if len(hdr) < 8:
            return {}
        if hdr[:2] == b"II":
            endian = "<"
        elif hdr[:2] == b"MM":
            endian = ">"
        else:
            return {}
        if struct.unpack(endian + "H", hdr[2:4])[0] != 42:
            return {}
        ifd0 = struct.unpack(endian + "I", hdr[4:8])[0]
        if not (0 < ifd0 < 16 * 1024 * 1024):
            return {}
        t0 = _read_ifd_entries(fh, start, endian, ifd0)
        dt = _tag_str(t0, 0x0132)
        if 0x8769 in t0:
            vb = t0[0x8769][2]
            if len(vb) >= 4:
                exif_off = struct.unpack(endian + "I", vb[:4])[0]
                if 0 < exif_off < 16 * 1024 * 1024:
                    te = _read_ifd_entries(fh, start, endian, exif_off)
                    dto = _tag_str(te, 0x9003) or _tag_str(te, 0x9004)
                    if dto:
                        dt = dto
        return {"dt": dt if valid_dt(dt) else None}
    except Exception:
        return {}

def read_exif_jpeg(fh, start):
    """JPEG 的 APP1 中找 Exif TIFF"""
    try:
        fh.seek(start)
        head = fh.read(256 * 1024)
        i = head.find(b"Exif\x00\x00")
        if i < 0:
            return {}
        return read_exif_tiff(fh, start + i + 6)
    except Exception:
        return {}

def valid_dt(s):
    return bool(s) and re.match(r"^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$", s) \
        and not s.startswith("0000")

def fmt_dt_display(s):
    """EXIF 'YYYY:MM:DD HH:MM:SS' -> 'YYYY-MM-DD HH:MM:SS'"""
    if not valid_dt(s):
        return "未知时间"
    return s[:4] + "-" + s[5:7] + "-" + s[8:10] + s[10:]

def hit_time_parts(hit):
    """返回 (年, 年-月) 或 None"""
    s = hit.get("dt")
    if not valid_dt(s):
        return None
    return s[:4], s[:4] + "-" + s[5:7]

def make_hit_name(hit, seq):
    """生成有意义的文件名：拍摄时间_序号.扩展名；无时间则 恢复_序号_偏移"""
    ext = hit["ext"]
    if valid_dt(hit.get("dt")):
        s = hit["dt"]
        stamp = "%s%s%s_%s%s%s" % (s[0:4], s[5:7], s[8:10], s[11:13], s[14:16], s[17:19])
        return "%s_%04d%s" % (stamp, seq, ext)
    return "恢复_%04d_偏移%d%s" % (seq, hit["off"], ext)

def _cr3_moov_info(fh, moov_start, moov_size, file_start):
    """在 moov 内找 CMT2（EXIF TIFF）与 CMT1（JPEG 预览）"""
    info = {"exif": {}, "preview": None}
    end = moov_start + moov_size
    pos = moov_start + 8
    for _ in range(64):
        if pos + 8 > end:
            break
        fh.seek(pos)
        b = fh.read(16)
        if len(b) < 8:
            break
        size, typ = struct.unpack(">I4s", b[:8])
        hdr = 8
        if size == 1 and len(b) >= 16:
            size = struct.unpack(">Q", b[8:16])[0]
            hdr = 16
        if size < hdr or pos + size > end:
            break
        payload_off = pos + hdr
        payload_len = size - hdr
        if typ == b"CMT2" and payload_len > 16:
            fh.seek(payload_off)
            head = fh.read(min(payload_len, 4096))
            for sig in (b"II*\x00", b"MM\x00*"):
                i = head.find(sig)
                if i >= 0:
                    info["exif"] = read_exif_tiff(fh, payload_off + i)
                    break
        elif typ == b"CMT1" and payload_len > 100:
            info["preview"] = (payload_off - file_start,
                               min(payload_len, PREVIEW_EMBED_CAP))
        pos += size
    return info

def parse_cr3(fh, start):
    """佳能 CR3：走查 ISO BMFF 盒结构定长，并读取 EXIF 与内嵌预览"""
    fh.seek(start)
    h = fh.read(16)
    if len(h) < 16 or h[4:8] != b"ftyp" or h[8:12] != b"crx ":
        return None
    pos, total = start, 0
    exif, preview = {}, None
    for _ in range(64):
        fh.seek(pos)
        b = fh.read(16)
        if len(b) < 8:
            break
        size, typ = struct.unpack(">I4s", b[:8])
        if size == 1 and len(b) >= 16:
            size = struct.unpack(">Q", b[8:16])[0]
        if size < 8 or size > MAX_CARVE:
            break
        if typ == b"moov" and size < 64 * 1024 * 1024:
            info = _cr3_moov_info(fh, pos, size, start)
            exif, preview = info["exif"], info["preview"]
        total += size
        pos += size
        if total > MAX_CARVE:
            return None
    if total < MIN_RAW:
        return None
    return {"size": total, "ext": ".cr3", "preview": preview,
            "dt": exif.get("dt")}

def detect(fh, abs_off, sig_kind):
    try:
        if sig_kind == "jpg":
            fh.seek(abs_off)
            head = fh.read(4)
            if len(head) < 4 or head[:3] != b"\xff\xd8\xff":
                return None
            if head[3] not in (0xE0, 0xE1, 0xE2, 0xDB, 0xC4, 0xC8, 0xEE):
                return None
            length = carve_jpeg(fh, abs_off)
            if not length:
                return None
            return {"off": abs_off, "len": length, "ext": ".jpg", "kind": "jpg",
                    "dt": read_exif_jpeg(fh, abs_off).get("dt")}
        if sig_kind == "tiff":
            r = parse_tiff(fh, abs_off)
            if not r:
                return None
            return {"off": abs_off, "len": r["size"], "ext": r["ext"], "kind": "tiff",
                    "dt": read_exif_tiff(fh, abs_off).get("dt")}
        if sig_kind == "raf":
            r = parse_raf(fh, abs_off)
            if not r:
                return None
            return {"off": abs_off, "len": r["size"], "ext": r["ext"], "kind": "raf", "dt": None}
        if sig_kind == "cr3":
            r = parse_cr3(fh, abs_off)
            if not r:
                return None
            return {"off": abs_off, "len": r["size"], "ext": r["ext"], "kind": "cr3",
                    "dt": r.get("dt")}
    except Exception:
        return None
    return None

PATTERNS = [
    (b"\xff\xd8\xff", "jpg", 0),
    (b"II*\x00", "tiff", 0),
    (b"MM\x00*", "tiff", 0),
    (b"FUJIFILMCCD-RAW", "raf", 0),
    (b"ftypcrx ", "cr3", -4),
]
KIND_ENABLED_KEY = {"jpg": "jpg", "tiff": "raw", "raf": "raf", "cr3": "cr3"}

# ----------------------------- 预览图提取 -----------------------------

def extract_preview_bytes(fh, hit):
    off, length, kind = hit["off"], hit["len"], hit["kind"]
    try:
        if kind == "jpg":
            fh.seek(off)
            return fh.read(min(length, PREVIEW_JPEG_CAP))
        if kind == "raf":
            r = parse_raf(fh, off)
            if r and r["preview"]:
                po, pl = r["preview"]
                fh.seek(off + po)
                return fh.read(pl)
        if kind == "tiff":
            r = parse_tiff(fh, off)
            if r and r["preview"]:
                po, pl = r["preview"]
                fh.seek(off + po)
                return fh.read(pl)
        if kind == "cr3":
            r = parse_cr3(fh, off)
            if r and r["preview"]:
                po, pl = r["preview"]
                fh.seek(off + po)
                return fh.read(pl)
            # 兜底：在文件头部直接搜内嵌 JPEG
            fh.seek(off)
            head = fh.read(min(length, 16 * 1024 * 1024))
            i = head.find(b"\xff\xd8\xff")
            while i >= 0:
                j = head.find(b"\xff\xd9", i + 3)
                if j > 0 and j - i > 2000:
                    return head[i:j + 2]
                i = head.find(b"\xff\xd8\xff", i + 3)
    except Exception:
        return None
    return None

# ----------------------------- 扫描线程（只登记，不写文件） -----------------------------

def _align_up(n, unit=SECTOR):
    return ((n + unit - 1) // unit) * unit

class AlignedReader:
    """
    物理盘读取适配器：对外提供普通文件式 seek/read（任意偏移、任意长度），
    底层统一转换为 512 字节扇区对齐的读取，兼容对对齐有严格要求的 USB 存储驱动。
    """
    def __init__(self, raw):
        self.raw = raw
        self.pos = 0

    def seek(self, pos):
        self.pos = pos

    def read(self, size):
        if size <= 0:
            return b""
        off = self.pos
        start = off - (off % SECTOR)
        end = _align_up(off + size)
        self.raw.seek(start)
        buf = self.raw.read(end - start)
        rel = off - start
        data = buf[rel:rel + size]
        self.pos = off + len(data)
        return data

    def close(self):
        self.raw.close()

def scan_disk(fh, disk_size, enabled, q, stop_flag, log):
    """核心扫描循环（可单测）。fh 为 AlignedReader。只读，不写任何文件。"""
    scanned = 0
    carved_ranges = []
    counts = {}
    t0 = time.time()
    while scanned < disk_size and not stop_flag.is_set():
        remaining = disk_size - scanned
        want = min(CHUNK + OVERLAP, _align_up(remaining))
        try:
            fh.seek(scanned)
            data = fh.read(want)
            if not data:
                break
            blocks = [(scanned, data)]
        except OSError as e:
            log("大块读取失败（%s），降级为 1MB 粒度重试…" % e)
            blocks = []
            end = scanned + min(CHUNK, remaining)
            sub = scanned
            while sub < end and not stop_flag.is_set():
                try:
                    fh.seek(sub)
                    d = fh.read(min(SUB, _align_up(end - sub)))
                    if d:
                        blocks.append((sub, d))
                except OSError as e2:
                    log("读取失败，跳过 1MB（偏移 %.2f GB）：%s" % (
                        sub / 1073741824.0, e2))
                sub += SUB

        for base, chunk in blocks:
            if stop_flag.is_set():
                break
            # 先收集本块全部候选，按偏移升序处理：
            # 保证 CR3 等母文件先被识别，其内嵌的 JPEG 预览/EXIF 缩略图
            # 会落入已雕刻区间而被跳过，避免一份照片捞出多份垃圾文件。
            cands = []
            for pat, kind, delta in PATTERNS:
                if not enabled.get(KIND_ENABLED_KEY[kind], True):
                    continue
                pos = 0
                while True:
                    i = chunk.find(pat, pos)
                    if i < 0:
                        break
                    pos = i + 1
                    abs_off = base + i + delta
                    if abs_off >= 0:
                        cands.append((abs_off, kind))
            cands.sort()
            for abs_off, kind in cands:
                if stop_flag.is_set():
                    break
                if any(s <= abs_off < s + l for s, l in carved_ranges):
                    continue
                hit = detect(fh, abs_off, kind)
                if hit:
                    carved_ranges.append((hit["off"], hit["len"]))
                    counts[hit["ext"]] = counts.get(hit["ext"], 0) + 1
                    hit["name"] = make_hit_name(hit, sum(counts.values()))
                    q.put(("hit", hit))

        scanned += min(CHUNK, remaining)
        q.put(("progress", min(scanned, disk_size)))

    total = sum(counts.values())
    elapsed = time.time() - t0
    if stop_flag.is_set():
        q.put(("scandone", "已手动停止。找到 %d 个文件。" % total))
    else:
        q.put(("scandone",
            "扫描完成！用时 %.0f 秒，找到 %d 个文件：%s" % (
                elapsed, total,
                "，".join("%s×%d" % (k, v) for k, v in sorted(counts.items())) or "无")))

class Scanner(threading.Thread):
    def __init__(self, disk_num, disk_size, enabled, q):
        super().__init__(daemon=True)
        self.disk_num = disk_num
        self.disk_size = disk_size
        self.enabled = enabled
        self.q = q
        self.stop_flag = threading.Event()
        self.counts = {}

    def log(self, msg):
        self.q.put(("log", msg))

    def run(self):
        path = r"\\.\PhysicalDrive%d" % self.disk_num
        try:
            fh = AlignedReader(open(path, "rb", buffering=0))
        except PermissionError:
            self.q.put(("error", "无法打开磁盘：请以管理员身份运行本工具。"))
            return
        except OSError as e:
            self.q.put(("error", "无法打开磁盘：%s" % e))
            return

        self.log("开始扫描 PhysicalDrive%d（%.1f GB）..." % (
            self.disk_num, self.disk_size / 1073741824.0))
        try:
            scan_disk(fh, self.disk_size, self.enabled, self.q,
                      self.stop_flag, self.log)
            fh.close()
        except Exception:
            self.log("扫描异常：\n" + traceback.format_exc())
            self.q.put(("error", "扫描中断。"))

# ----------------------------- 恢复线程（按选中的清单写文件） -----------------------------

class Recoverer(threading.Thread):
    def __init__(self, disk_num, hits, out_dir, q):
        super().__init__(daemon=True)
        self.disk_num = disk_num
        self.hits = hits
        self.out_dir = out_dir
        self.q = q
        self.stop_flag = threading.Event()

    def run(self):
        path = r"\\.\PhysicalDrive%d" % self.disk_num
        try:
            fh = AlignedReader(open(path, "rb", buffering=0))
        except OSError as e:
            self.q.put(("error", "无法打开磁盘：%s" % e))
            return
        ok, fail = 0, 0
        total_bytes = sum(h["len"] for h in self.hits)
        done_bytes = 0
        try:
            for idx, h in enumerate(self.hits, 1):
                if self.stop_flag.is_set():
                    break
                # 按拍摄时间 年/月 归档；无时间的进「未知时间」
                tp = hit_time_parts(h)
                rel_dir = os.path.join(tp[0], tp[1]) if tp else "未知时间"
                sub = os.path.join(self.out_dir, rel_dir)
                os.makedirs(sub, exist_ok=True)
                name = h.get("name") or make_hit_name(h, idx)
                try:
                    fh.seek(h["off"])
                    remaining = h["len"]
                    with open(os.path.join(sub, name), "wb") as out:
                        while remaining > 0:
                            data = fh.read(min(4 * 1024 * 1024, remaining))
                            if not data:
                                break
                            out.write(data)
                            done_bytes += len(data)
                            remaining -= len(data)
                            self.q.put(("rprogress", done_bytes, total_bytes))
                    ok += 1
                    self.q.put(("log", "已恢复 %s（%.1fMB）" % (name, h["len"] / 1048576.0)))
                except OSError as e:
                    fail += 1
                    self.q.put(("log", "恢复失败 %s：%s" % (name, e)))
            fh.close()
        except Exception:
            self.q.put(("log", "恢复异常：\n" + traceback.format_exc()))
        self.q.put(("recdone", "恢复结束：成功 %d 个，失败 %d 个。保存位置：%s" % (
            ok, fail, self.out_dir)))

# ============================= Apple 风格 UI =============================

# Apple 调色板（HIG）
A_BG      = "#f5f5f7"   # 窗口底色
A_CARD    = "#ffffff"   # 卡片
A_LINE    = "#d2d2d7"   # 分隔线/描边
A_TEXT    = "#1d1d1f"   # 主文字
A_GRAY    = "#86868b"   # 次要文字
A_BLUE    = "#0071e3"   # Apple 蓝（按钮）
A_BLUE_H  = "#0077ed"   # 悬停
A_BLUE_D  = "#0060c9"   # 按下
A_GRAY_BG = "#e8e8ed"   # 灰按钮底
A_GRAY_H  = "#dcdce1"
A_GREEN   = "#34c759"
A_ORANGE  = "#ff9f0a"
A_RED     = "#ff3b30"

F_TITLE = ("Segoe UI", 21, "bold")
F_SUB   = ("Segoe UI", 10)
F_SECT  = ("Segoe UI", 11, "bold")
F_BODY  = ("Segoe UI", 10)
F_SMALL = ("Segoe UI", 9)
F_MONO  = ("Cascadia Mono", 9)

class Card(tk.Frame):
    """白卡片：细描边 + 内边距"""
    def __init__(self, parent, padx=20, pady=16, **kw):
        super().__init__(parent, bg=A_CARD,
                         highlightbackground="#e4e4e9", highlightthickness=1,
                         bd=0, **kw)
        self.inner = tk.Frame(self, bg=A_CARD)
        self.inner.pack(fill="both", expand=True, padx=padx, pady=pady)

class PillButton(tk.Label):
    """胶囊按钮（Apple 圆角风，tk 近似实现）"""
    KINDS = {
        "primary":   (A_BLUE, "#ffffff", A_BLUE_H),
        "secondary": (A_GRAY_BG, A_TEXT, A_GRAY_H),
        "danger":    (A_RED, "#ffffff", "#e0342b"),
        "ghost":     (A_CARD, A_BLUE, "#eef4fd"),
    }
    def __init__(self, parent, text, command, kind="primary", font=F_BODY):
        self._kind = kind
        self._cmd = command
        self._enabled = True
        bg, fg, hover = self.KINDS[kind]
        self._bg, self._fg, self._hover = bg, fg, hover
        super().__init__(parent, text=text, bg=bg, fg=fg, font=font,
                         padx=18, pady=7, cursor="hand2")
        self.configure(highlightbackground=bg, highlightthickness=1)
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._state() and self.configure(bg=self._hover))
        self.bind("<Leave>", lambda e: self._state() and self.configure(bg=self._bg))

    def _state(self):
        return self._enabled

    def _click(self, _e):
        if self._enabled and self._cmd:
            self._cmd()

    def set_enabled(self, on):
        self._enabled = bool(on)
        if on:
            self.configure(bg=self._bg, fg=self._fg, cursor="hand2")
        else:
            self.configure(bg=A_GRAY_BG, fg=A_GRAY, cursor="arrow")

class PillToggle(tk.Label):
    """格式筛选胶囊开关（iOS 风）"""
    def __init__(self, parent, text, value=True):
        self._value = bool(value)
        super().__init__(parent, text=text, font=F_SMALL,
                         padx=14, pady=5, cursor="hand2")
        self.bind("<Button-1>", lambda e: self.set(not self._value))
        self._paint()

    def _paint(self):
        if self._value:
            self.configure(bg="#e8f0fe", fg=A_BLUE,
                           highlightbackground=A_BLUE, highlightthickness=1)
        else:
            self.configure(bg=A_CARD, fg=A_GRAY,
                           highlightbackground=A_LINE, highlightthickness=1)

    def set(self, v):
        self._value = bool(v)
        self._paint()

    def get(self):
        return self._value

class AppleProgress(tk.Canvas):
    """细圆角进度条（Apple 风）"""
    def __init__(self, parent, height=6, **kw):
        super().__init__(parent, height=height, bg=A_BG,
                         highlightthickness=0, bd=0, **kw)
        self.frac = 0.0
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, frac):
        self.frac = max(0.0, min(1.0, frac))
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        r = h // 2
        self._pill(0, 0, w, h, A_GRAY_BG)
        fw = int(w * self.frac)
        if fw > 2:
            self._pill(0, 0, max(fw, h), h, A_BLUE)

    def _pill(self, x, y, w, h, color):
        r = h // 2
        self.create_oval(x, y, x + h, y + h, fill=color, outline="")
        self.create_oval(x + w - h, y, x + w, y + h, fill=color, outline="")
        self.create_rectangle(x + r, y, x + w - r, y + h, fill=color, outline="")

class TrafficLights(tk.Canvas):
    """macOS 红绿灯装饰"""
    def __init__(self, parent):
        super().__init__(parent, width=64, height=20, bg=A_BG,
                         highlightthickness=0, bd=0)
        for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            x = 8 + i * 20
            self.create_oval(x, 4, x + 12, 16, fill=c, outline="")

# ----------------------------- 主界面 -----------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("照片恢复")
        self.geometry("900x740")
        self.minsize(800, 660)
        self.configure(bg=A_BG)
        self.q = queue.Queue()
        self.scanner = None
        self.recoverer = None
        self.disks = []
        self.hits = []
        self.hit_iids = []
        self._display_order = []
        self.preview_token = 0
        self.preview_img = None
        self._prog_mode = None     # "scan" / "recover"
        self._prog_max = 1

        self._setup_ttk_style()
        self._build_header()
        self._build_scan_card()
        self._build_result_card()
        self._build_bottom()

        self.refresh_disks()
        self.after(150, self.poll_queue)

    # ---------- 样式 ----------

    def _setup_ttk_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview",
                     background=A_CARD, fieldbackground=A_CARD, foreground=A_TEXT,
                     rowheight=30, font=F_BODY, borderwidth=0)
        st.configure("Treeview.Heading",
                     background=A_BG, foreground=A_GRAY, font=F_SMALL,
                     relief="flat", borderwidth=0, padding=(8, 6))
        st.map("Treeview",
               background=[("selected", A_BLUE)],
               foreground=[("selected", "#ffffff")])
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.configure("TCombobox", padding=6, fieldbackground=A_CARD,
                     background=A_CARD, foreground=A_TEXT)
        st.configure("TSeparator", background=A_LINE)

    # ---------- 布局 ----------

    def _build_header(self):
        head = tk.Frame(self, bg=A_BG)
        head.pack(fill="x", padx=24, pady=(16, 4))
        TrafficLights(head).pack(side="left", pady=(8, 0))
        tk.Label(head, text="照片恢复", bg=A_BG, fg=A_TEXT,
                 font=F_TITLE).pack(side="left", padx=(16, 0))
        # 右侧权限胶囊
        if is_admin():
            tk.Label(head, text="● 管理员权限", bg=A_BG, fg=A_GREEN,
                     font=F_SMALL).pack(side="right", pady=(14, 0))
        else:
            bar = tk.Frame(head, bg="#fff7ed",
                           highlightbackground="#ffd9a8", highlightthickness=1)
            bar.pack(side="right", pady=(10, 0))
            tk.Label(bar, text="需要管理员权限", bg="#fff7ed", fg="#b25000",
                     font=F_SMALL, padx=10, pady=6).pack(side="left")
            b = PillButton(bar, "获取权限", relaunch_as_admin, "primary", F_SMALL)
            b.pack(side="left", padx=(4, 8), pady=3)
        tk.Label(self, text="HFS+ 移动硬盘 · 误删照片找回 · 扫描全程只读，确认后再恢复",
                 bg=A_BG, fg=A_GRAY, font=F_SUB).pack(anchor="w", padx=24)

    def _build_scan_card(self):
        self.scan_card = Card(self)
        self.scan_card.pack(fill="x", padx=24, pady=(12, 0))
        c = self.scan_card.inner

        tk.Label(c, text="第 1 步 · 扫描", bg=A_CARD, fg=A_TEXT,
                 font=F_SECT).grid(row=0, column=0, sticky="w", columnspan=8)
        tk.Label(c, text="选择移动硬盘（HFS+ 盘通常显示为「未知」，按容量和品牌辨认）",
                 bg=A_CARD, fg=A_GRAY, font=F_SMALL).grid(row=1, column=0, sticky="w",
                                                          columnspan=8, pady=(2, 10))

        self.disk_var = tk.StringVar()
        self.disk_combo = ttk.Combobox(c, textvariable=self.disk_var,
                                       state="readonly", width=76, font=F_BODY)
        self.disk_combo.grid(row=2, column=0, sticky="we", columnspan=7)
        self.refresh_btn = PillButton(c, "刷新", self.refresh_disks, "secondary", F_SMALL)
        self.refresh_btn.grid(row=2, column=7, padx=(8, 0), sticky="e")

        tk.Label(c, text="照片格式", bg=A_CARD, fg=A_GRAY,
                 font=F_SMALL).grid(row=3, column=0, sticky="w", pady=(14, 6))
        fmt = tk.Frame(c, bg=A_CARD)
        fmt.grid(row=4, column=0, columnspan=8, sticky="w")
        self.tg_jpg = PillToggle(fmt, "JPEG · jpg", False)
        self.tg_raw = PillToggle(fmt, "RAW · arw / nef / cr2 / dng", False)
        self.tg_raf = PillToggle(fmt, "富士 · raf", False)
        self.tg_cr3 = PillToggle(fmt, "佳能 · cr3", True)
        for w in (self.tg_jpg, self.tg_raw, self.tg_raf, self.tg_cr3):
            w.pack(side="left", padx=(0, 8))

        act = tk.Frame(c, bg=A_CARD)
        act.grid(row=5, column=0, columnspan=8, sticky="w", pady=(18, 2))
        self.start_btn = PillButton(act, "开始扫描", self.start_scan, "primary")
        self.start_btn.pack(side="left")
        self.stop_btn = PillButton(act, "停止", self.stop_scan, "secondary")
        self.stop_btn.pack(side="left", padx=10)
        self.stop_btn.set_enabled(False)
        self.load_btn = PillButton(act, "载入扫描结果", self.load_scan_file, "ghost")
        self.load_btn.pack(side="left", padx=10)

    def _build_result_card(self):
        self.result_card = Card(self)
        c = self.result_card.inner

        top = tk.Frame(c, bg=A_CARD)
        top.pack(fill="x")
        tk.Label(top, text="第 2 步 · 选择要恢复的照片", bg=A_CARD, fg=A_TEXT,
                 font=F_SECT).pack(side="left")
        self.result_label = tk.Label(top, text="", bg=A_CARD, fg=A_GRAY, font=F_SMALL)
        self.result_label.pack(side="left", padx=(12, 0))

        tools = tk.Frame(c, bg=A_CARD)
        tools.pack(fill="x", pady=(8, 10))
        PillButton(tools, "全选", self.select_all, "secondary", F_SMALL).pack(side="left")
        PillButton(tools, "全不选", self.select_none, "secondary", F_SMALL).pack(side="left", padx=8)
        # 明确视觉化按钮：不再使用容易被误认为纯文字的 ghost 样式
        self.save_scan_btn = PillButton(tools, "保存扫描结果 (.json)", self.save_scan_file,
                                        "secondary", F_SMALL)
        self.save_scan_btn.pack(side="left", padx=(8, 0))
        PillButton(tools, "返回重新扫描", self.back_to_scan, "ghost", F_SMALL).pack(side="right")

        # 恢复操作固定在列表上方，窗口不够高时也能看到
        rec = tk.Frame(c, bg=A_CARD)
        rec.pack(fill="x", pady=(0, 10))
        tk.Label(rec, text="恢复到", bg=A_CARD, fg=A_GRAY, font=F_SMALL).pack(side="left")
        self.out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "恢复的照片"))
        self.out_entry = tk.Entry(rec, textvariable=self.out_var, font=F_SMALL,
                                  bg=A_CARD, fg=A_TEXT, relief="flat",
                                  highlightthickness=1, highlightbackground=A_LINE,
                                  highlightcolor=A_BLUE, width=36)
        self.out_entry.pack(side="left", padx=(8, 6), ipady=4)
        PillButton(rec, "浏览…", self.pick_out, "secondary", F_SMALL).pack(side="left")
        self.rec_sel_btn = PillButton(rec, "恢复选中文件", lambda: self.start_recover(False), "primary")
        self.rec_sel_btn.pack(side="right")
        self.rec_all_btn = PillButton(rec, "恢复全部文件", lambda: self.start_recover(True), "secondary")
        self.rec_all_btn.pack(side="right", padx=(0, 8))
        self.rec_stop_btn = PillButton(rec, "停止恢复", self.stop_recover, "danger", F_SMALL)
        self.rec_stop_btn.pack(side="right", padx=(0, 8))
        self.rec_stop_btn.set_enabled(False)

        body = tk.Frame(c, bg=A_CARD)
        body.pack(fill="both", expand=True)

        # 左：列表
        list_wrap = tk.Frame(body, bg=A_CARD, highlightbackground="#e4e4e9",
                             highlightthickness=1)
        list_wrap.pack(side="left", fill="both", expand=True)
        cols = ("name", "dt", "size", "offset")
        self.tree = ttk.Treeview(list_wrap, columns=cols, show="headings",
                                 selectmode="extended")
        self.tree.heading("name", text="文件名（按拍摄时间生成）")
        self.tree.heading("dt", text="拍摄时间")
        self.tree.heading("size", text="大小")
        self.tree.heading("offset", text="磁盘偏移")
        self.tree.column("name", width=200, anchor="w")
        self.tree.column("dt", width=140, anchor="center")
        self.tree.column("size", width=70, anchor="e")
        self.tree.column("offset", width=120, anchor="e")
        self.tree.tag_configure("odd", background="#fafafc")
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        # 右：预览
        pv = tk.Frame(body, bg=A_CARD, width=280)
        pv.pack(side="right", fill="y", padx=(14, 0))
        pv.pack_propagate(False)
        tk.Label(pv, text="预览", bg=A_CARD, fg=A_GRAY, font=F_SMALL).pack(anchor="w")
        self.preview_label = tk.Label(pv, bg="#f0f0f3", fg=A_GRAY,
                                      text="选中左侧文件\n查看预览",
                                      font=F_SMALL)
        self.preview_label.pack(fill="both", expand=True, pady=(4, 0))
        self.preview_label.configure(highlightbackground="#e4e4e9", highlightthickness=1)
        self.preview_info = tk.Label(pv, text="", bg=A_CARD, fg=A_GRAY,
                                     font=F_SMALL, wraplength=270, justify="left")
        self.preview_info.pack(anchor="w", pady=(6, 0))

    def _build_bottom(self):
        bot = tk.Frame(self, bg=A_BG)
        bot.pack(fill="both", expand=True, padx=24, pady=(12, 16))

        self.pbar = AppleProgress(bot)
        self.pbar.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(bot, textvariable=self.status_var, bg=A_BG, fg=A_GRAY,
                 font=F_SMALL).pack(anchor="w", pady=(6, 8))

        log_card = Card(bot, padx=12, pady=10)
        log_card.pack(fill="both", expand=True)
        tk.Label(log_card.inner, text="运行日志", bg=A_CARD, fg=A_GRAY,
                 font=F_SMALL).pack(anchor="w")
        self.logbox = scrolledtext.ScrolledText(
            log_card.inner, height=8, state="disabled",
            font=F_MONO, bg=A_CARD, fg="#4b4b50",
            relief="flat", borderwidth=0, highlightthickness=0,
            insertbackground=A_TEXT)
        self.logbox.pack(fill="both", expand=True, pady=(4, 0))

    # ---------- 基础 ----------

    def log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def refresh_disks(self):
        try:
            self.disks = list_disks()
        except Exception as e:
            messagebox.showerror("错误", "枚举磁盘失败：%s" % e)
            return
        items = ["磁盘 %d  ·  %s  ·  %.1f GB  ·  %s" % (
            d["num"], d["name"] or "未知设备", d["size"] / 1073741824.0, d["bus"])
            for d in self.disks]
        self.disk_combo["values"] = items
        for idx, d in enumerate(self.disks):
            if "USB" in d["bus"].upper():
                self.disk_combo.current(idx)
                break
        if not self.disk_var.get() and items:
            self.disk_combo.current(0)

    def pick_out(self):
        d = filedialog.askdirectory()
        if d:
            self.out_var.set(d)

    def current_disk(self):
        idx = self.disk_combo.current()
        if idx < 0 or idx >= len(self.disks):
            return None
        return self.disks[idx]

    # ---------- 扫描 ----------

    def start_scan(self):
        disk = self.current_disk()
        if not disk:
            messagebox.showwarning("提示", "请先选择要扫描的移动硬盘")
            return
        if not is_admin():
            messagebox.showwarning("权限不足", "需要管理员权限，请点击右上角「获取权限」。")
            return
        if not messagebox.askyesno("开始扫描",
                "将对【磁盘 %d | %s | %.1f GB】进行只读扫描（不写任何文件）。\n"
                "扫描完成后由你勾选要恢复的文件。\n\n大容量硬盘可能需要较长时间，是否开始？"
                % (disk["num"], disk["name"], disk["size"] / 1073741824.0)):
            return

        enabled = {"jpg": self.tg_jpg.get(), "raw": self.tg_raw.get(),
                   "raf": self.tg_raf.get(), "cr3": self.tg_cr3.get()}
        self.hits = []
        self.hit_iids = []
        self.scanner = Scanner(disk["num"], disk["size"], enabled, self.q)
        self.scanner.start()
        self._prog_mode = "scan"
        self._prog_max = disk["size"]
        self.pbar.set(0)
        self.start_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.status_var.set("扫描中…")
        self.log("开始扫描磁盘 %d（%s）…" % (disk["num"], disk["name"]))

    def stop_scan(self):
        if self.scanner:
            self.scanner.stop_flag.set()
            self.status_var.set("正在停止…")

    def show_results(self):
        self.scan_card.pack_forget()
        self.result_card.pack(fill="both", expand=True, padx=24, pady=(12, 0))
        self.rebuild_tree()
        self.status_var.set("扫描结束，共找到 %d 个文件。请勾选要恢复的文件。" % len(self.hits))
        self.pbar.set(1.0)

    def rebuild_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.hit_iids = []
        # 按拍摄时间倒序展示，未知时间排最后
        ordered = sorted(range(len(self.hits)),
                         key=lambda i: self.hits[i].get("dt") or "", reverse=True)
        for row, i in enumerate(ordered):
            h = self.hits[i]
            tag = ("odd",) if row % 2 else ()
            name = h.get("name") or make_hit_name(h, i + 1)
            iid = self.tree.insert("", "end", values=(
                name, fmt_dt_display(h.get("dt")),
                "%.1f MB" % (h["len"] / 1048576.0),
                "0x%X" % h["off"]), tags=tag)
            self.hit_iids.append(iid)
        self._display_order = ordered
        cnt = {}
        for h in self.hits:
            cnt[h["ext"]] = cnt.get(h["ext"], 0) + 1
        self.result_label.configure(text="共 %d 个文件 · %s" % (
            len(self.hits),
            " · ".join("%s × %d" % (k, v) for k, v in sorted(cnt.items())) or "无"))

    def back_to_scan(self):
        self.result_card.pack_forget()
        self.scan_card.pack(fill="x", padx=24, pady=(12, 0))
        self.status_var.set("就绪")
        self.pbar.set(0)

    # ---------- 选择 & 预览 ----------

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def select_none(self):
        self.tree.selection_remove(self.tree.get_children())

    def selected_hits(self):
        sel = set(self.tree.selection())
        out = []
        for row, iid in enumerate(self.hit_iids):
            if iid in sel:
                out.append(self.hits[self._display_order[row]])
        return out

    def on_select(self, _evt=None):
        hits = self.selected_hits()
        if not hits:
            return
        hit = hits[0]
        self.preview_info.configure(text="%s · %.1f MB · 偏移 %d" % (
            hit["ext"], hit["len"] / 1048576.0, hit["off"]))
        if not HAS_PIL:
            self.preview_label.configure(image="", text="（未安装 Pillow，无预览）")
            return
        disk = self.current_disk()
        if not disk:
            return
        self.preview_token += 1
        token = self.preview_token
        self.preview_label.configure(image="", text="读取预览中…")
        threading.Thread(target=self._load_preview,
                         args=(token, disk["num"], hit), daemon=True).start()

    def _load_preview(self, token, disk_num, hit):
        try:
            fh = AlignedReader(open(r"\\.\PhysicalDrive%d" % disk_num, "rb", buffering=0))
            data = extract_preview_bytes(fh, hit)
            fh.close()
            if not data:
                self.q.put(("preview_fail", token, "（该文件无内嵌预览图）"))
                return
            im = Image.open(io.BytesIO(data))
            im.thumbnail((280, 280))
            self.q.put(("preview", token, im))
        except Exception as e:
            self.q.put(("preview_fail", token, "预览失败：%s" % e))

    def show_preview(self, token, im):
        if token != self.preview_token:
            return
        self.preview_img = ImageTk.PhotoImage(im)
        self.preview_label.configure(image=self.preview_img, text="")

    # ---------- 恢复 ----------

    def start_recover(self, recover_all):
        disk = self.current_disk()
        if not disk:
            messagebox.showwarning("提示", "请先选择源移动硬盘")
            return
        hits = self.hits if recover_all else self.selected_hits()
        if not hits:
            messagebox.showwarning("提示", "请先在列表中勾选要恢复的文件")
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning("提示", "请选择保存目录")
            return
        m = re.match(r"^([A-Za-z]):", out)
        if m:
            dn = disk_num_of_drive_letter(m.group(1))
            if dn is not None and dn == disk["num"]:
                messagebox.showerror("危险操作已阻止",
                    "保存目录就在被恢复的移动硬盘上！\n写入数据会覆盖待恢复的照片，请选择电脑上的其他磁盘。")
                return
        total_mb = sum(h["len"] for h in hits) / 1048576.0
        if not messagebox.askyesno("开始恢复",
                "将恢复 %d 个文件（共 %.0f MB）到：\n%s\n\n是否开始？"
                % (len(hits), total_mb, out)):
            return
        os.makedirs(out, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(out, "恢复结果_" + stamp)
        self.recoverer = Recoverer(disk["num"], hits, out_dir, self.q)
        self.recoverer.start()
        self._prog_mode = "recover"
        self.pbar.set(0)
        self.rec_sel_btn.set_enabled(False)
        self.rec_all_btn.set_enabled(False)
        self.rec_stop_btn.set_enabled(True)
        self.log("开始恢复 %d 个文件到 %s …" % (len(hits), out_dir))

    def stop_recover(self):
        if self.recoverer:
            self.recoverer.stop_flag.set()
            self.status_var.set("正在停止恢复…")

    # ---------- 扫描结果存取 ----------

    def save_scan_file(self):
        disk = self.current_disk()
        if not self.hits:
            messagebox.showwarning("提示", "当前没有扫描结果可保存")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("扫描结果", "*.json")],
            initialfile="扫描结果.json")
        if not path:
            return
        data = {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "disk": disk,
            "hits": self.hits,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        self.log("扫描结果已保存：%s" % path)

    def load_scan_file(self):
        path = filedialog.askopenfilename(filetypes=[("扫描结果", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            hits = data["hits"]
            saved_disk = data.get("disk") or {}
        except Exception as e:
            messagebox.showerror("错误", "读取扫描结果失败：%s" % e)
            return
        disk = self.current_disk()
        if disk and saved_disk:
            if disk["size"] != saved_disk.get("size"):
                messagebox.showerror("磁盘不匹配",
                    "当前选中的磁盘与扫描结果保存时的磁盘容量不一致，\n"
                    "请先选择正确的那块移动硬盘再载入。")
                return
            messagebox.showinfo("确认磁盘",
                "请确认当前选中的磁盘就是当时扫描的那块：\n%s（%.1f GB）"
                % (saved_disk.get("name", ""), (saved_disk.get("size") or 0) / 1073741824.0))
        self.hits = hits
        self.show_results()
        self.log("已载入扫描结果：%s（%d 个文件，扫描于 %s）" % (
            path, len(hits), data.get("saved_at", "未知时间")))

    # ---------- 队列轮询 ----------

    def poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.log(item[1])
                elif kind == "hit":
                    self.hits.append(item[1])
                    h = item[1]
                    self.status_var.set("扫描中… 已找到 %d 个文件（最新 %s %.1fMB）" % (
                        len(self.hits), h["ext"], h["len"] / 1048576.0))
                elif kind == "progress":
                    if self._prog_mode == "scan" and self.scanner:
                        self.pbar.set(item[1] / max(1, self.scanner.disk_size))
                elif kind == "rprogress":
                    done, total = item[1], item[2]
                    if total > 0:
                        self.pbar.set(done / total)
                        self.status_var.set("恢复中… %.1f%%" % (100.0 * done / total))
                elif kind == "preview":
                    self.show_preview(item[1], item[2])
                elif kind == "preview_fail":
                    if item[1] == self.preview_token:
                        self.preview_img = None
                        self.preview_label.configure(image="", text=item[2])
                elif kind == "scandone":
                    self.log(item[1])
                    self.start_btn.set_enabled(True)
                    self.stop_btn.set_enabled(False)
                    if self.hits:
                        self.show_results()
                    else:
                        self.status_var.set(item[1])
                        messagebox.showinfo("扫描结束", item[1] + "\n未找到可恢复的照片。")
                elif kind == "recdone":
                    self.log(item[1])
                    self.status_var.set(item[1])
                    self.rec_sel_btn.set_enabled(True)
                    self.rec_all_btn.set_enabled(True)
                    self.rec_stop_btn.set_enabled(False)
                    messagebox.showinfo("恢复完成", item[1])
                elif kind == "error":
                    self.log("错误：" + item[1])
                    messagebox.showerror("错误", item[1])
                    self.start_btn.set_enabled(True)
                    self.stop_btn.set_enabled(False)
                    self.rec_sel_btn.set_enabled(True)
                    self.rec_all_btn.set_enabled(True)
                    self.rec_stop_btn.set_enabled(False)
        except queue.Empty:
            pass
        self.after(150, self.poll_queue)

if __name__ == "__main__":
    App().mainloop()
