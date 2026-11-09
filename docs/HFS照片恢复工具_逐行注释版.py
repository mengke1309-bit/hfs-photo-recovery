# -*- coding: utf-8 -*-
# ============================================================================
# 《HFS+ 移动硬盘照片恢复工具》逐行注释版 —— 写给技术小白的代码导览
# ============================================================================
#
# 【这个软件是干什么的？】
#   你的移动硬盘是 Mac 的 HFS+ 格式，Windows 读不懂它；照片又被误删了。
#   本工具的办法叫「文件雕刻」：不去理解硬盘的文件系统（目录结构），
#   而是直接把硬盘当成一长串原始字节，从头到尾扫一遍，
#   寻找照片文件特有的「二进制签名」（例如 JPG 照片一定以 FF D8 FF 开头），
#   找到后按照格式规则算出文件长度，把这段字节原样抠出来保存——照片就回来了。
#   被删的文件只是「门牌号」被撕了，「房子」（数据）还在，所以能找回来。
#
# 【软件整体分四层，从下往上读最容易懂】
#   第 1 层 工具函数区：提权、枚举磁盘、解析各种照片格式（本文件前半部分）
#   第 2 层 扫描引擎：把硬盘切块读进内存，用签名搜照片（scan_disk）
#   第 3 层 后台线程：Scanner 负责扫描、Recoverer 负责恢复，不卡界面
#   第 4 层 图形界面：tkinter 画的窗口（苹果风格），负责和你交互
#
# 【注释约定】
#   每行代码上方都有一行以 # 开头的解释，# 开头的内容 Python 不会执行，
#   纯粹是写给人看的。你可以放心阅读，也可以运行本文件，效果与原版一致。
# ============================================================================

# 下面三引号包起来的是「模块文档字符串」，相当于程序的自我介绍，不影响运行。
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

# -------------------- 导入「工具箱」：Python 自带的功能模块 --------------------

# ctypes：让 Python 能直接调用 Windows 系统的底层功能（比如查询管理员权限）。
import ctypes
# io：内存里的「虚拟文件」，预览照片时把字节数据当成文件来读。
import io
# json：读写 JSON 格式文本，用来保存/载入扫描结果清单。
import json
# os：操作系统相关功能，比如拼文件路径、创建文件夹。
import os
# queue：线程之间安全传递消息的「信箱」（扫描线程 → 界面线程汇报进度）。
import queue
# re：正则表达式工具，用来检查拍摄时间格式是否合法。
import re
# struct：把原始字节翻译成数字（照片文件里的长度、偏移都是二进制存的）。
import struct
# subprocess：让 Python 去执行别的程序（这里用来调用 PowerShell 查磁盘信息）。
import subprocess
# sys：Python 解释器本身的信息（比如当前程序路径、退出程序）。
import sys
# threading：多线程——扫描/恢复在后台跑，界面不卡死。
import threading
# time：计时（统计扫描用了多少秒、生成时间戳文件名）。
import time
# traceback：出错时打印详细的错误位置，方便排查。
import traceback

# 尝试导入 tkinter（Python 自带的画窗口工具），它是本软件界面的基础。
try:
    # tkinter 主模块，惯例起个别名叫 tk，后面 tk.xxx 都是在画界面。
    import tkinter as tk
    # ttk=漂亮的控件库；filedialog=文件选择弹窗；messagebox=提示弹窗；
    # scrolledtext=带滚动条的文本框（日志区）；tkfont=字体工具。
    from tkinter import ttk, filedialog, messagebox, scrolledtext, font as tkfont
# 如果电脑上没有 tkinter（某些精简版 Python 没有），就走这里。
except ImportError:
    # 在黑色控制台窗口里打印一句提示。
    print("未找到 tkinter。请使用 python.org 官方版 Python 运行：py -3 本脚本.py")
    # 直接退出程序（数字 1 表示「异常退出」）。
    sys.exit(1)

# 尝试导入 Pillow（第三方图像库，负责解码照片缩略图）。
try:
    # Image=打开/缩放图片；ImageTk=把图片贴到 tkinter 界面上；ImageFile=读取设置。
    from PIL import Image, ImageTk, ImageFile
    # 允许打开「被截断」的图片（恢复出来的照片可能不完整，尽量显示能显示的部分）。
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    # 打个标记：Pillow 可用，界面可以显示预览图。
    HAS_PIL = True
# 没装 Pillow 也不影响核心恢复功能，只是没有预览图。
except ImportError:
    # 打个标记：没有预览能力。
    HAS_PIL = False

# ----------------------------- 常量（全局约定的一些数字） -----------------------------
# 常量是「定下来就不变」的值，全大写命名是程序员的约定俗成。

# CHUNK：每次从硬盘读 8MB 到内存里搜索（太大占内存，太慢小效率低）。
CHUNK = 8 * 1024 * 1024
# OVERLAP：相邻两块之间多读出 512 字节重叠区，防止照片开头正好被切块边界劈成两半而漏检。
OVERLAP = 512                 # 跨块重叠（扇区整数倍）
# SECTOR：硬盘的最小读写单位「扇区」= 512 字节，物理盘读取必须按它对齐。
SECTOR = 512                  # 物理盘扇区大小
# SUB：遇到读不动的大块时，切成 1MB 小块重试（坏扇区抢救粒度）。
SUB = 1024 * 1024             # 坏块抢救粒度 1MB
# MAX_CARVE：单个文件最大按 512MB 算，防止把垃圾数据误判成超大文件而耗尽时间。
MAX_CARVE = 512 * 1024 * 1024
# MIN_JPEG：小于 100KB 的「JPEG」多半是照片内部嵌的缩略图，直接过滤掉。
MIN_JPEG = 100 * 1024         # JPEG 最小有效尺寸（过滤缩略图/内嵌预览）
# MIN_RAW：RAW 格式（相机原始文件）至少 100KB，同理过滤杂碎数据。
MIN_RAW = 100 * 1024
# 预览用的 JPEG 最多读 32MB（预览不需要整个文件，省时间）。
PREVIEW_JPEG_CAP = 32 * 1024 * 1024
# RAW 里内嵌的预览图最多按 24MB 认可，超出视为数据异常。
PREVIEW_EMBED_CAP = 24 * 1024 * 1024

# 相机厂商英文名 → 对应 RAW 文件扩展名的对照表（用来给恢复的文件起正确的后缀）。
MAKE_TO_EXT = [
    ("SONY", ".arw"), ("NIKON", ".nef"), ("CANON", ".cr2"),
    ("OLYMPUS", ".orf"), ("PANASONIC", ".rw2"), ("PENTAX", ".pef"),
    ("LEICA", ".dng"), ("HASSELBLAD", ".3fr"), ("FUJIFILM", ".raf"),
]

# ----------------------------- 提权（检查/获取管理员权限） -----------------------------
# 直接读物理硬盘属于高危操作，Windows 要求必须是「管理员」身份。

# 定义函数 is_admin：检查当前是不是管理员。def 就是「定义一个功能」。
def is_admin():
    # try：试着做一件事，失败也不让程序崩溃。
    try:
        # 调用 Windows 系统接口 IsUserAnAdmin，返回非 0 就是管理员。
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    # 如果调用本身都失败了（极端情况），保守起见当作不是管理员。
    except Exception:
        return False

# 定义函数 relaunch_as_admin：以管理员身份重新启动本程序。
def relaunch_as_admin():
    # ShellExecuteW 是 Windows 的「启动程序」接口；参数 "runas" 表示「弹出 UAC 提权确认框」，
    # sys.executable 是当前 Python 解释器路径，后面跟着本脚本的路径，即以管理员身份再开一份自己。
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, '"%s"' % os.path.abspath(__file__), None, 1)
    # 退出当前这个没有权限的旧进程（新的管理员版本已经启动）。
    sys.exit(0)

# ----------------------------- 磁盘枚举（列出电脑上有哪些硬盘） -----------------------------
# 思路：Python 自己不擅长查硬件，于是「请外援」——调用 Windows 的 PowerShell 命令，
# 让它把磁盘信息整理成 JSON 文本返回，Python 再解析。

# 定义函数 ps_json：执行一条 PowerShell 命令，把输出的 JSON 转成 Python 列表。
def ps_json(cmd):
    # subprocess.run = 启动一个子进程执行命令；capture_output=True 表示把它的输出抓回来；
    # text=True 按文本处理；timeout=60 最多等 60 秒。
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True, text=True, timeout=60)
    # 取出命令打印的内容，并去掉首尾空白。
    data = out.stdout.strip()
    # 如果没有输出（比如一块盘都没查到），返回空列表。
    if not data:
        return []
    # 把 JSON 文本翻译成 Python 对象（列表或字典）。
    obj = json.loads(data)
    # PowerShell 的怪癖：只有 1 条结果时不给列表给单个对象，这里统一包成列表。
    return obj if isinstance(obj, list) else [obj]

# 定义函数 list_disks：列出所有物理磁盘（编号、名字、容量、接口类型）。
def list_disks():
    # 让 PowerShell 执行 Get-Disk 命令，选取 4 个字段并转成紧凑 JSON。
    rows = ps_json(
        "Get-Disk | Select-Object Number,FriendlyName,Size,BusType "
        "| ConvertTo-Json -Compress")
    # 准备一个空列表，用来装整理好的磁盘信息。
    disks = []
    # 遍历查询结果中的每一行（每行代表一块物理盘）。
    for r in rows:
        # 尝试解析这一行；数据有残缺就跳过，不让程序崩。
        try:
            # 把这行数据整理成统一的字典格式，追加到列表里。
            disks.append({
                # 磁盘编号（0、1、2……），打开 \\.\PhysicalDriveN 时要用。
                "num": int(r["Number"]),
                # 磁盘型号名（如 "Samsung Portable SSD T3"），没有就给空字符串。
                "name": str(r.get("FriendlyName") or ""),
                # 容量（字节数）。
                "size": int(r.get("Size") or 0),
                # 接口类型（USB / SATA / NVMe……），用来认出移动硬盘。
                "bus": str(r.get("BusType") or ""),
            })
        # 某行数据异常（缺字段等），忽略该行继续。
        except Exception:
            pass
    # 返回整理好的磁盘列表。
    return disks

# 定义函数 disk_num_of_drive_letter：根据盘符（如 "M"）反查它属于哪块物理盘。
# 用途：防止你把恢复出来的照片保存回源盘（那会覆盖待恢复的数据）。
def disk_num_of_drive_letter(letter):
    # 让 PowerShell 查「盘符 → 分区 → 所属磁盘编号」。
    rows = ps_json(
        "Get-Partition -DriveLetter %s | Select-Object DiskNumber | ConvertTo-Json -Compress"
        % letter.upper())
    # 如果查到了结果。
    if rows:
        # 尝试取出磁盘编号并转成整数返回。
        try:
            return int(rows[0]["DiskNumber"])
        # 数据格式不对就返回 None（表示不知道）。
        except Exception:
            return None
    # 没查到（盘符不存在等），返回 None。
    return None

# ----------------------------- 各格式长度解析 -----------------------------
# 「文件雕刻」的关键难题：找到文件开头后，怎么知道它到哪里结束？
# 每种格式有不同的算法，下面逐个实现。

# 定义函数 carve_jpeg：从 start 位置开始，测量一个 JPEG 文件有多长。
# JPEG 以 FF D8 FF 开头、以 FF D9 结尾，所以只要找到结尾标记即可。
def carve_jpeg(fh, start):
    # 把「读取光标」移动到文件起点（fh 是可以读硬盘的文件对象）。
    fh.seek(start)
    # total：已经读取的字节数，防止死循环。
    total = 0
    # carry：上一次读剩下的一点点尾巴（结尾标记可能正好横跨两次读取的边界）。
    carry = b""
    # 循环读取，直到超过最大允许大小。
    while total < MAX_CARVE:
        # 每次读 64KB 小步前进——小步是为了躲开坏扇区：哪怕文件后面的区域坏了，
        # 也只影响那一小步，不会让整张好好的照片报废。
        chunk = fh.read(64 * 1024)  # 小步读取，避免无关区域坏扇区拖垮整个文件
        # 读到空数据说明到硬盘末尾了，这个文件不完整，放弃。
        if not chunk:
            return None
        # 把上次的尾巴和本次新数据拼起来搜索（接住跨边界的结尾标记）。
        buf = carry + chunk
        # 在拼接后的数据里找 JPEG 结尾标记 FF D9（从位置 1 开始找，跳过开头）。
        i = buf.find(b"\xff\xd9", 1)
        # 找到了！
        if i >= 0:
            # 计算文件总长度：之前累计的长度 + 本次找到的位置 + 标记本身的 2 字节。
            length = total - len(carry) + i + 2
            # 太短的是垃圾/缩略图，返回 None 表示不要；够长就返回长度。
            return length if length >= MIN_JPEG else None
        # 没找到：把本次数据最后 1 字节留作下次的「尾巴」（防止标记被切开）。
        carry = buf[-1:]
        # 累计已读字节数，继续下一轮循环。
        total += len(chunk)
    # 超过 512MB 都没找到结尾，说明开头是误报，放弃。
    return None
# 定义函数 _tiff_walk：遍历 TIFF 结构（RAW 文件的骨架）。
# 【背景知识】索尼/尼康/佳能 CR2 等 RAW 文件，本质都是 TIFF 格式。
# TIFF 像一本「带索引的书」：索引叫 IFD（图像文件目录），每条索引 12 字节，
# 记录着「照片的某段数据放在哪个位置、有多长」。把所有数据段的末尾位置收集起来，
# 最大的那个末尾位置就是整个文件的长度。
# 参数 ends/make_box/flags/preview_box 是「收集结果的篮子」，由调用方传入。
def _tiff_walk(fh, start, endian, ifd_off, ends, make_box, flags, visited, preview_box):
    # 防死循环：这个 IFD 位置访问过了、或者位置明显不合法，就直接返回。
    if ifd_off in visited or ifd_off <= 0 or ifd_off > 64 * 1024 * 1024:
        return
    # 把这个 IFD 位置登记到「已访问」集合里。
    visited.add(ifd_off)
    # 跳到 IFD 所在位置（文件起点 + IFD 偏移）。
    fh.seek(start + ifd_off)
    # 先读 2 字节：这是「本目录有多少条索引」的数量。
    raw = fh.read(2)
    # 读不够 2 字节说明数据残缺，放弃这个目录。
    if len(raw) < 2:
        return
    # 把 2 字节翻译成整数（条目数量）。endian 表示字节序（大端/小端，下详）。
    n = struct.unpack(endian + "H", raw)[0]
    # 条目数量超过 400 明显是垃圾数据，放弃（正常照片 IFD 就几十条）。
    if n > 400:
        return
    # 每条索引 12 字节，一次性读出全部条目。
    entries = fh.read(n * 12)
    # 读不够说明数据残缺，放弃。
    if len(entries) < n * 12:
        return
    # 跳过条目区，读接下来的 4 字节——那是「下一个 IFD 的位置」（链表指针）。
    fh.seek(start + ifd_off + 2 + n * 12)
    nb = fh.read(4)
    # 把「本目录区自己的末尾位置」登记到 ends 篮子里。
    ends.append(ifd_off + 2 + n * 12 + 4)

    # tags：把本目录里解析出的索引条目存成「标签号 → 内容」的字典。
    tags = {}
    # 逐条解析 n 个索引条目。
    for i in range(n):
        # 切出第 i 条的 12 字节。
        ent = entries[i * 12:(i + 1) * 12]
        # 12 字节拆成 4 个数：标签号、数据类型、数据个数、数据值/数据位置。
        tag, typ, cnt, val = struct.unpack(endian + "HHII", ent)
        # 每种数据类型的单个数据占多少字节（查表，查不到按 1 算）。
        unit = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}.get(typ, 1)
        # 该条目的数据总字节数 = 个数 × 单个大小。
        total = cnt * unit
        # TIFF 的省空间技巧：数据不超过 4 字节时，直接塞在「值」字段里。
        if total <= 4:
            # 从条目后 4 字节中取出真正的数据。
            vbytes = ent[8:12][:total]
        # 数据较大时，「值」字段存的是数据的存放位置（偏移量）。
        else:
            # 位置或大小离谱的是坏数据，跳过这条。
            if val > MAX_CARVE or total > 1024 * 1024:
                continue
            # 跳到数据真正存放的位置。
            fh.seek(start + val)
            # 读出数据内容。
            vbytes = fh.read(total)
            # 这段数据也占空间，把它的末尾位置登记到 ends。
            ends.append(val + total)
            # 没读够长度说明数据残缺，跳过。
            if len(vbytes) < total:
                continue
        # 把解析好的条目存进 tags 字典：标签号 → (类型, 个数, 内容)。
        tags[tag] = (typ, cnt, vbytes)

    # 定义内部小函数 ints_of：把某个标签的内容翻译成整数列表。
    # （内部函数只在本函数里用，是 Python 的常见写法。）
    def ints_of(tag):
        # 这个标签不存在，返回空列表。
        if tag not in tags:
            return []
        # 取出该标签的类型、个数、内容。
        typ, cnt, vb = tags[tag]
        # 类型 3 表示「16 位短整数」，每 2 字节翻译一个数。
        if typ == 3:
            return [struct.unpack(endian + "H", vb[j * 2:j * 2 + 2])[0]
                    for j in range(min(cnt, len(vb) // 2))]
        # 其他情况按「32 位整数」处理，每 4 字节翻译一个数。
        return [struct.unpack(endian + "I", vb[j * 4:j * 4 + 4])[0]
                for j in range(min(cnt, len(vb) // 4))]

    # 标签 0x010F 是「相机厂商」（Make），还没记录过就提取出来。
    if 0x010F in tags and make_box[0] is None:
        # 内容是字节串，按 \x00 截断、转成文字、去空格、转大写，存入篮子。
        make_box[0] = tags[0x010F][2].split(b"\x00")[0].decode("ascii", "ignore").strip().upper()
    # 标签 0xC612 是 DNG 格式专属标记，有它就打个「这是 DNG」的标记。
    if 0xC612 in tags:
        flags["dng"] = True
    # 标签 0x201/0x202 是「内嵌预览图」的位置和长度，找到就记下来（界面预览要用）。
    if preview_box[0] is None and 0x201 in tags and 0x202 in tags:
        # 取出预览图的偏移和长度。
        o, c = ints_of(0x201), ints_of(0x202)
        # 数值合理才采纳（防止坏数据）。
        if o and c and 0 < o[0] < MAX_CARVE and 0 < c[0] < PREVIEW_EMBED_CAP:
            preview_box[0] = (o[0], c[0])
    # 标签 0x8769 是「EXIF 子目录」的指针，里面存拍摄时间等，递归进去逛一圈。
    if 0x8769 in tags:
        for v in ints_of(0x8769)[:1]:
            _tiff_walk(fh, start, endian, v, ends, make_box, flags, visited, preview_box)
    # 标签 0x14A 是「子 IFD」指针列表（有些 RAW 有多个），逐个递归进去。
    if 0x14A in tags:
        for v in ints_of(0x14A):
            _tiff_walk(fh, start, endian, v, ends, make_box, flags, visited, preview_box)

    # 三对「数据位置/数据长度」标签：0x111/0x117 是图像数据，0x144/0x145 是切片数据，
    # 0x201/0x202 是预览图。它们指向的每段数据都占文件空间，末尾都要登记。
    for o_tag, c_tag in ((0x111, 0x117), (0x144, 0x145), (0x201, 0x202)):
        # 取出位置列表和长度列表。
        offs, cnts = ints_of(o_tag), ints_of(c_tag)
        # 有的相机只写一个长度却写多个位置（长度相同），把长度复制补齐。
        if len(cnts) == 1 and len(offs) > 1:
            cnts = cnts * len(offs)
        # 把每段数据的「位置+长度=末尾」登记进 ends。
        for o, c in zip(offs, cnts):
            if 0 < o < MAX_CARVE and 0 < c < MAX_CARVE:
                ends.append(o + c)

    # 如果刚才读到了「下一个 IFD 的位置」（4 字节）。
    if len(nb) == 4:
        # 翻译成整数。
        nxt = struct.unpack(endian + "I", nb)[0]
        # 位置非 0 说明还有下一个目录，递归进去继续逛。
        if nxt:
            _tiff_walk(fh, start, endian, nxt, ends, make_box, flags, visited, preview_box)

# 定义函数 parse_tiff：解析一个 TIFF 系 RAW 文件，返回 {大小, 扩展名, 预览位置}。
def parse_tiff(fh, start):
    # 跳到文件开头。
    fh.seek(start)
    # 读文件头 16 字节。
    hdr = fh.read(16)
    # 读不够 16 字节，不可能是合法文件，放弃。
    if len(hdr) < 16:
        return None
    # 头 2 字节是 "II"：表示「小端序」（低位字节排前面，Intel 习惯）。
    if hdr[:2] == b"II":
        endian = "<"
    # 头 2 字节是 "MM"：表示「大端序」（高位字节排前面，Motorola 习惯）。
    elif hdr[:2] == b"MM":
        endian = ">"
    # 都不是，就不是 TIFF 文件，放弃。
    else:
        return None
    # TIFF 的「暗号」：第 3-4 字节必须是数字 42，不是就放弃。
    if struct.unpack(endian + "H", hdr[2:4])[0] != 42:
        return None
    # 第 5-8 字节：第一个 IFD（索引目录）的位置。
    ifd0 = struct.unpack(endian + "I", hdr[4:8])[0]
    # 第 9-10 字节是 "CR" 的话，这是佳能 CR2 的标记。
    is_cr2 = hdr[8:10] == b"CR"

    # 准备收集结果的「篮子」：ends 装所有数据段末尾（初始含文件头 8 字节），
    # make_box 装厂商名，flags 装 DNG 标记，visited 防重复，preview_box 装预览位置。
    ends, make_box, flags, visited, preview_box = [8], [None], {"dng": False}, set(), [None]
    # 尝试从第一个 IFD 开始遍历整个 TIFF 结构。
    try:
        _tiff_walk(fh, start, endian, ifd0, ends, make_box, flags, visited, preview_box)
    # 遍历中任何异常（坏数据等）都视为解析失败。
    except Exception:
        return None
    # 文件大小 = 所有数据段末尾的最大值。
    size = max(ends) if ends else 0
    # 太小（不是真 RAW）或太大（误判）都放弃。
    if size < MIN_RAW or size > MAX_CARVE:
        return None

    # 取出厂商名（没取到就是空字符串）。
    make = make_box[0] or ""
    # 佳能 CR2 标记优先。
    if is_cr2:
        ext = ".cr2"
    # 其次看 DNG 标记。
    elif flags["dng"]:
        ext = ".dng"
    # 否则按厂商名对照表确定扩展名。
    else:
        # 兜底扩展名 .tiff（对照表没命中时用）。
        ext = ".tiff"
        # 遍历「厂商名 → 扩展名」对照表。
        for key, e in MAKE_TO_EXT:
            # 厂商名字符串里包含关键词（如 "NIKON"）就采用对应扩展名。
            if key in make:
                ext = e
                break
    # 返回解析结果：大小、扩展名、内嵌预览图位置。
    return {"size": size, "ext": ext, "preview": preview_box[0]}

# 定义函数 parse_raf：解析富士 RAF 格式。
# RAF 的文件头里直接写明了 JPEG 预览段和 RAW 数据段的位置与长度，最省事。
def parse_raf(fh, start):
    # 跳到文件开头。
    fh.seek(start)
    # 读文件头 0x64（100）字节。
    h = fh.read(0x64)
    # 读不够或开头不是富士的固定签名 "FUJIFILMCCD-RAW"，放弃。
    if len(h) < 0x64 or not h.startswith(b"FUJIFILMCCD-RAW"):
        return None
    # 头部固定位置 0x54~0x64 存了 4 个大端整数：JPEG 位置、JPEG 长度、RAW 位置、RAW 长度。
    joff, jlen, coff, clen = struct.unpack(">IIII", h[0x54:0x64])
    # 文件大小 = 两段数据末尾中较大的那个。
    size = max(joff + jlen, coff + clen)
    # 大小不合理就放弃。
    if size < MIN_RAW or size > MAX_CARVE:
        return None
    # JPEG 段就是现成的预览图，数值合理就记录它的位置和长度。
    prev = (joff, jlen) if 0 < joff < MAX_CARVE and 0 < jlen < PREVIEW_EMBED_CAP else None
    # 返回解析结果。
    return {"size": size, "ext": ".raf", "preview": prev}
# ----------------------------- EXIF 读取（拍摄时间等） -----------------------------
# 【背景知识】EXIF 是照片里的「信息卡」：拍摄时间、相机型号、光圈快门等。
# 本工具只关心「拍摄时间」，用来给恢复的文件起名、按年月分类。

# 定义函数 _read_ifd_entries：读取一个 IFD 目录里的所有标签（轻量版，只用于 EXIF）。
def _read_ifd_entries(fh, start, endian, ifd_off):
    # 跳到 IFD 所在位置。
    fh.seek(start + ifd_off)
    # 读 2 字节：条目数量。
    raw = fh.read(2)
    # 读不到就返回空字典。
    if len(raw) < 2:
        return {}
    # 翻译成整数。
    n = struct.unpack(endian + "H", raw)[0]
    # 数量超过 300 视为坏数据，返回空。
    if n > 300:
        return {}
    # 读出全部条目（每条 12 字节）。
    entries = fh.read(n * 12)
    # 读不够返回空。
    if len(entries) < n * 12:
        return {}
    # 准备空字典装解析结果。
    tags = {}
    # 逐条解析。
    for i in range(n):
        # 切出第 i 条的 12 字节。
        ent = entries[i * 12:(i + 1) * 12]
        # 拆成：标签号、类型、个数、值/位置。
        tag, typ, cnt, val = struct.unpack(endian + "HHII", ent)
        # 查每种类型单个数据占几字节。
        unit = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}.get(typ, 1)
        # 总字节数。
        total = cnt * unit
        # 小数据（≤4字节）直接嵌在条目里。
        if total <= 4:
            vb = ent[8:12][:total]
        # 大数据要去「值」指向的位置读。
        else:
            # 位置或大小离谱就跳过（EXIF 字段都很小，4KB 封顶足够）。
            if val > 16 * 1024 * 1024 or total > 4096:
                continue
            # 跳到数据位置。
            fh.seek(start + val)
            # 读出数据。
            vb = fh.read(total)
            # 读不够就跳过。
            if len(vb) < total:
                continue
        # 存入字典。
        tags[tag] = (typ, cnt, vb)
    # 返回解析出的所有标签。
    return tags

# 定义函数 _tag_str：把某个标签的内容转成字符串（时间、厂商名都是文本）。
def _tag_str(tags, tag):
    # 标签不存在返回 None。
    if tag not in tags:
        return None
    # 取内容字节 → 按 \x00 截断 → 转成文字 → 去掉首尾空白。
    s = tags[tag][2].split(b"\x00")[0].decode("ascii", "ignore").strip()
    # 空字符串返回 None，否则返回文字。
    return s or None

# 定义函数 read_exif_tiff：从 TIFF/EXIF 结构里读拍摄时间。
def read_exif_tiff(fh, start):
    """从 TIFF/EXIF 结构读取拍摄时间（DateTimeOriginal 优先）"""
    # 任何一步失败都只返回空字典，绝不让程序崩溃（try 包住整个函数体）。
    try:
        # 跳到 TIFF 开头。
        fh.seek(start)
        # 读 8 字节文件头。
        hdr = fh.read(8)
        # 读不够返回空。
        if len(hdr) < 8:
            return {}
        # 判断字节序："II" 小端。
        if hdr[:2] == b"II":
            endian = "<"
        # "MM" 大端。
        elif hdr[:2] == b"MM":
            endian = ">"
        # 都不是就不是 TIFF，返回空。
        else:
            return {}
        # 检查 TIFF 暗号 42。
        if struct.unpack(endian + "H", hdr[2:4])[0] != 42:
            return {}
        # 读出第一个 IFD 的位置。
        ifd0 = struct.unpack(endian + "I", hdr[4:8])[0]
        # 位置必须在合理范围内。
        if not (0 < ifd0 < 16 * 1024 * 1024):
            return {}
        # 读取主目录的所有标签。
        t0 = _read_ifd_entries(fh, start, endian, ifd0)
        # 先试试主目录里的 0x0132（修改时间，保底用）。
        dt = _tag_str(t0, 0x0132)
        # 如果主目录里有 0x8769（EXIF 子目录指针）。
        if 0x8769 in t0:
            # 取出指针的内容字节。
            vb = t0[0x8769][2]
            # 内容至少 4 字节才够存一个位置。
            if len(vb) >= 4:
                # 翻译出 EXIF 子目录的位置。
                exif_off = struct.unpack(endian + "I", vb[:4])[0]
                # 位置合理才继续。
                if 0 < exif_off < 16 * 1024 * 1024:
                    # 读取 EXIF 子目录的标签。
                    te = _read_ifd_entries(fh, start, endian, exif_off)
                    # 0x9003 是「拍摄时间」、0x9004 是「数字化时间」，哪个有用哪个。
                    dto = _tag_str(te, 0x9003) or _tag_str(te, 0x9004)
                    # 找到了就替换掉保底时间。
                    if dto:
                        dt = dto
        # 校验时间格式，合法就返回，不合法返回 None。
        return {"dt": dt if valid_dt(dt) else None}
    # 出任何错都返回空字典。
    except Exception:
        return {}

# 定义函数 read_exif_jpeg：从 JPEG 文件里读拍摄时间。
def read_exif_jpeg(fh, start):
    """JPEG 的 APP1 中找 Exif TIFF"""
    try:
        # 跳到 JPEG 开头。
        fh.seek(start)
        # 读开头 256KB（EXIF 一定在文件头部）。
        head = fh.read(256 * 1024)
        # JPEG 的 EXIF 藏在 "Exif\x00\x00" 这 6 字节标记后面，搜一下。
        i = head.find(b"Exif\x00\x00")
        # 没找到说明这张照片没有 EXIF，返回空。
        if i < 0:
            return {}
        # 找到后，跳过 6 字节标记，后面就是一个标准 TIFF 结构，交给上面的函数解析。
        return read_exif_tiff(fh, start + i + 6)
    # 出错返回空。
    except Exception:
        return {}

# 定义函数 valid_dt：检查时间字符串是不是 "2024:05:01 13:22:45" 这种标准格式。
def valid_dt(s):
    # 非空 + 正则匹配格式 + 不以 "0000" 开头（相机没设时间会存 0000:00:00）。
    return bool(s) and re.match(r"^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$", s) \
        and not s.startswith("0000")

# 定义函数 fmt_dt_display：把 EXIF 时间格式转成给人看的格式。
def fmt_dt_display(s):
    """EXIF 'YYYY:MM:DD HH:MM:SS' -> 'YYYY-MM-DD HH:MM:SS'"""
    # 不合法的时间一律显示「未知时间」。
    if not valid_dt(s):
        return "未知时间"
    # 把日期部分的冒号换成横杠（时间部分的冒号保留）。
    return s[:4] + "-" + s[5:7] + "-" + s[8:10] + s[10:]

# 定义函数 hit_time_parts：从文件信息里拆出「年」和「年-月」，用于分类建文件夹。
def hit_time_parts(hit):
    """返回 (年, 年-月) 或 None"""
    # 取出拍摄时间。
    s = hit.get("dt")
    # 没有合法时间就返回 None（恢复时归入「未知时间」文件夹）。
    if not valid_dt(s):
        return None
    # 返回二元组：("2024", "2024-05")。
    return s[:4], s[:4] + "-" + s[5:7]

# 定义函数 make_hit_name：给恢复的文件起一个有意义的名字。
def make_hit_name(hit, seq):
    """生成有意义的文件名：拍摄时间_序号.扩展名；无时间则 恢复_序号_偏移"""
    # 取扩展名（如 .cr3）。
    ext = hit["ext"]
    # 如果有合法拍摄时间。
    if valid_dt(hit.get("dt")):
        s = hit["dt"]
        # 把 "2024:05:01 13:22:45" 拼成 "20240501_132245"。
        stamp = "%s%s%s_%s%s%s" % (s[0:4], s[5:7], s[8:10], s[11:13], s[14:16], s[17:19])
        # 返回如 "20240501_132245_0007.cr3"。
        return "%s_%04d%s" % (stamp, seq, ext)
    # 没有时间就用「恢复_序号_偏移」起名（偏移是它在硬盘上的位置，保证不重名）。
    return "恢复_%04d_偏移%d%s" % (seq, hit["off"], ext)

# 定义函数 _cr3_moov_info：在佳能 CR3 的 moov 盒子里找 EXIF 和预览图。
# 【背景知识】CR3 用的是 ISO BMFF 结构：文件由一个个「盒子」组成，
# 每个盒子开头 8 字节写着「我多长 + 我叫什么」。moov 盒子里有子盒子：
# CMT1 装着 JPEG 预览图，CMT2 装着 EXIF 信息。
def _cr3_moov_info(fh, moov_start, moov_size, file_start):
    """在 moov 内找 CMT2（EXIF TIFF）与 CMT1（JPEG 预览）"""
    # 准备结果字典：exif 装拍摄时间，preview 装预览图位置。
    info = {"exif": {}, "preview": None}
    # moov 盒子的结束位置。
    end = moov_start + moov_size
    # 从 moov 内容区开始（跳过 8 字节盒头）。
    pos = moov_start + 8
    # 最多遍历 64 个子盒子（防止坏数据造成死循环）。
    for _ in range(64):
        # 剩余空间不够一个盒头（8 字节）就结束。
        if pos + 8 > end:
            break
        # 跳到当前子盒子的位置。
        fh.seek(pos)
        # 读 16 字节（兼容「加长盒头」的情况）。
        b = fh.read(16)
        # 读不够 8 字节就结束。
        if len(b) < 8:
            break
        # 前 8 字节 = 盒子长度（大端 32 位）+ 盒子类型（4 个字母）。
        size, typ = struct.unpack(">I4s", b[:8])
        # 默认盒头 8 字节。
        hdr = 8
        # 长度字段为 1 是「加长盒头」约定：真实长度是后面的 64 位整数。
        if size == 1 and len(b) >= 16:
            size = struct.unpack(">Q", b[8:16])[0]
            hdr = 16
        # 长度必须大于盒头且不超出 moov 边界，否则数据损坏，停止。
        if size < hdr or pos + size > end:
            break
        # 算出盒子内容的位置和长度。
        payload_off = pos + hdr
        payload_len = size - hdr
        # CMT2 盒子：里面是 EXIF（TIFF 结构）。
        if typ == b"CMT2" and payload_len > 16:
            # 跳到内容区。
            fh.seek(payload_off)
            # 读开头最多 4KB（TIFF 头一定在开头附近）。
            head = fh.read(min(payload_len, 4096))
            # 在内容里找 TIFF 签名（小端 "II*\0" 或大端 "MM\0*"）。
            for sig in (b"II*\x00", b"MM\x00*"):
                i = head.find(sig)
                # 找到就从该位置解析 EXIF，存进结果。
                if i >= 0:
                    info["exif"] = read_exif_tiff(fh, payload_off + i)
                    break
        # CMT1 盒子：里面是一张 JPEG 预览图，记下位置和长度（相对文件开头）。
        elif typ == b"CMT1" and payload_len > 100:
            info["preview"] = (payload_off - file_start,
                               min(payload_len, PREVIEW_EMBED_CAP))
        # 移动到下一个子盒子。
        pos += size
    # 返回找到的信息。
    return info

# 定义函数 parse_cr3：解析佳能 CR3 文件，算出大小并提取 EXIF、预览位置。
def parse_cr3(fh, start):
    """佳能 CR3：走查 ISO BMFF 盒结构定长，并读取 EXIF 与内嵌预览"""
    # 跳到文件开头。
    fh.seek(start)
    # 读 16 字节验证身份。
    h = fh.read(16)
    # CR3 的第一个盒子必须是 ftyp 且品牌是 "crx "（crx 加空格），否则不是 CR3。
    if len(h) < 16 or h[4:8] != b"ftyp" or h[8:12] != b"crx ":
        return None
    # pos 指向当前盒子位置，total 累计文件总长度。
    pos, total = start, 0
    # 准备 EXIF 和预览的存放变量。
    exif, preview = {}, None
    # 最多遍历 64 个盒子。
    for _ in range(64):
        # 跳到当前盒子。
        fh.seek(pos)
        # 读盒头。
        b = fh.read(16)
        # 读不够说明文件到头了，停止遍历。
        if len(b) < 8:
            break
        # 解析长度和类型。
        size, typ = struct.unpack(">I4s", b[:8])
        # 处理加长盒头。
        if size == 1 and len(b) >= 16:
            size = struct.unpack(">Q", b[8:16])[0]
        # 长度不合法就停止。
        if size < 8 or size > MAX_CARVE:
            break
        # 遇到 moov 盒子（且大小正常），进去找 EXIF 和预览图。
        if typ == b"moov" and size < 64 * 1024 * 1024:
            info = _cr3_moov_info(fh, pos, size, start)
            exif, preview = info["exif"], info["preview"]
        # 累计文件总长度。
        total += size
        # 移动到下一个盒子。
        pos += size
        # 总长超过上限，判定为误判，放弃。
        if total > MAX_CARVE:
            return None
    # 总长度太小，不是真的 RAW 文件。
    if total < MIN_RAW:
        return None
    # 返回结果：大小、扩展名、预览位置、拍摄时间。
    return {"size": total, "ext": ".cr3", "preview": preview,
            "dt": exif.get("dt")}
# 定义函数 detect：总调度——在硬盘 abs_off 位置发现疑似签名后，
# 根据签名种类调用对应的解析器，确认是不是真照片，是就返回它的完整信息。
def detect(fh, abs_off, sig_kind):
    # 整个函数包在 try 里：解析任何意外都当作「不是照片」，绝不中断扫描。
    try:
        # 如果签名指向 JPEG。
        if sig_kind == "jpg":
            # 跳过去读 4 字节再核对一次（扫描时找到的 3 字节签名可能撞车）。
            fh.seek(abs_off)
            head = fh.read(4)
            # 前 3 字节必须是 FF D8 FF。
            if len(head) < 4 or head[:3] != b"\xff\xd8\xff":
                return None
            # 第 4 字节必须是合法段标记之一（E0/E1=APP段、DB/C4=表、C8=保留、EE=Adobe）。
            if head[3] not in (0xE0, 0xE1, 0xE2, 0xDB, 0xC4, 0xC8, 0xEE):
                return None
            # 测量文件长度（找 FF D9 结尾）。
            length = carve_jpeg(fh, abs_off)
            # 测不出来就放弃。
            if not length:
                return None
            # 返回文件档案：位置、长度、扩展名、种类、拍摄时间。
            return {"off": abs_off, "len": length, "ext": ".jpg", "kind": "jpg",
                    "dt": read_exif_jpeg(fh, abs_off).get("dt")}
        # 如果签名指向 TIFF 系 RAW（ARW/NEF/CR2 等）。
        if sig_kind == "tiff":
            # 调 parse_tiff 解析。
            r = parse_tiff(fh, abs_off)
            # 解析失败就放弃。
            if not r:
                return None
            # 返回档案。
            return {"off": abs_off, "len": r["size"], "ext": r["ext"], "kind": "tiff",
                    "dt": read_exif_tiff(fh, abs_off).get("dt")}
        # 如果签名指向富士 RAF。
        if sig_kind == "raf":
            r = parse_raf(fh, abs_off)
            if not r:
                return None
            # RAF 的 EXIF 解析未实现，拍摄时间留空（文件名退化为偏移命名）。
            return {"off": abs_off, "len": r["size"], "ext": r["ext"], "kind": "raf", "dt": None}
        # 如果签名指向佳能 CR3。
        if sig_kind == "cr3":
            r = parse_cr3(fh, abs_off)
            if not r:
                return None
            return {"off": abs_off, "len": r["size"], "ext": r["ext"], "kind": "cr3",
                    "dt": r.get("dt")}
    # 任何异常：返回 None。
    except Exception:
        return None
    # 未知的签名种类：返回 None。
    return None

# PATTERNS：扫描时用的「签名 → 种类 → 位置修正」对照表。
# 硬盘数据里搜到某个签名，就按对应种类调用 detect。
PATTERNS = [
    (b"\xff\xd8\xff", "jpg", 0),          # JPEG 开头标记
    (b"II*\x00", "tiff", 0),              # TIFF 小端开头（多数 RAW）
    (b"MM\x00*", "tiff", 0),              # TIFF 大端开头
    (b"FUJIFILMCCD-RAW", "raf", 0),       # 富士 RAF 开头
    (b"ftypcrx ", "cr3", -4),             # CR3：ftyp 在文件头 +4 处，所以往前回退 4 字节
]
# 签名种类 → 界面格式开关的键名对照（TIFF 系在界面上叫 "raw"）。
KIND_ENABLED_KEY = {"jpg": "jpg", "tiff": "raw", "raf": "raf", "cr3": "cr3"}

# ----------------------------- 预览图提取 -----------------------------
# 在结果列表里点一个文件时，从硬盘上把它的预览图字节抠出来给界面显示。

# 定义函数 extract_preview_bytes：根据文件档案提取预览图原始字节。
def extract_preview_bytes(fh, hit):
    # 拆开档案：位置、长度、种类。
    off, length, kind = hit["off"], hit["len"], hit["kind"]
    # 任何失败都返回 None（界面显示「无预览」即可）。
    try:
        # JPEG 文件本身就是图，直接读前一部分就能显示。
        if kind == "jpg":
            fh.seek(off)
            return fh.read(min(length, PREVIEW_JPEG_CAP))
        # RAF：预览图位置在文件头里写着，解析后去那个位置读。
        if kind == "raf":
            r = parse_raf(fh, off)
            if r and r["preview"]:
                po, pl = r["preview"]
                fh.seek(off + po)
                return fh.read(pl)
        # TIFF 系 RAW：预览图位置在 IFD 的标签 0x201/0x202 里。
        if kind == "tiff":
            r = parse_tiff(fh, off)
            if r and r["preview"]:
                po, pl = r["preview"]
                fh.seek(off + po)
                return fh.read(pl)
        # CR3：预览图在 moov 盒子的 CMT1 里。
        if kind == "cr3":
            r = parse_cr3(fh, off)
            if r and r["preview"]:
                po, pl = r["preview"]
                fh.seek(off + po)
                return fh.read(pl)
            # 兜底方案：找不到 CMT1 就在文件头 16MB 里直接搜 JPEG 签名。
            fh.seek(off)
            head = fh.read(min(length, 16 * 1024 * 1024))
            # 循环找 JPEG 开头 FF D8 FF。
            i = head.find(b"\xff\xd8\xff")
            while i >= 0:
                # 再找对应的结尾 FF D9。
                j = head.find(b"\xff\xd9", i + 3)
                # 找到且尺寸像张图（>2KB），就截出来返回。
                if j > 0 and j - i > 2000:
                    return head[i:j + 2]
                # 继续找下一个。
                i = head.find(b"\xff\xd8\xff", i + 3)
    # 出错返回 None。
    except Exception:
        return None
    # 没有可用的预览。
    return None

# ----------------------------- 扫描线程（只登记，不写文件） -----------------------------

# 定义小函数 _align_up：把数字向上取整到 unit（默认 512）的整数倍。
# 例如 _align_up(1000) = 1024。物理盘读取长度必须是扇区整数倍时会用到。
def _align_up(n, unit=SECTOR):
    return ((n + unit - 1) // unit) * unit

# 定义类 AlignedReader：物理盘「对齐读取」翻译官。
# 【为什么需要它】很多 USB 硬盘盒的主控芯片很挑剔：用 Windows 直接读物理盘时，
# 读取的起始位置和长度必须是 512 字节的整数倍，否则直接报错拒绝。
# 但上层代码想从任意位置读任意长度（比如从照片开头读），这个类就负责翻译。
class AlignedReader:
    """
    物理盘读取适配器：对外提供普通文件式 seek/read（任意偏移、任意长度），
    底层统一转换为 512 字节扇区对齐的读取，兼容对对齐有严格要求的 USB 存储驱动。
    """
    # 初始化：raw 是真正的物理盘文件对象。
    def __init__(self, raw):
        self.raw = raw
        self.pos = 0

    # seek：只记录「逻辑光标」位置，不真正动硬盘（懒到 read 时才动）。
    def seek(self, pos):
        self.pos = pos

    # read：上层想要「从 pos 读 size 字节」，这里翻译成对齐读取。
    def read(self, size):
        # 读 0 字节或负数，返回空。
        if size <= 0:
            return b""
        # 上层想要的起点。
        off = self.pos
        # 把起点向下对齐到 512 的整数倍（多读一点前面）。
        start = off - (off % SECTOR)
        # 把终点向上对齐到 512 的整数倍（多读一点后面）。
        end = _align_up(off + size)
        # 真正去硬盘上读对齐后的区间。
        self.raw.seek(start)
        buf = self.raw.read(end - start)
        # 算出上层要的数据在缓冲里的相对位置，把多读的「边角料」切掉。
        rel = off - start
        data = buf[rel:rel + size]
        # 更新逻辑光标。
        self.pos = off + len(data)
        # 返回上层要的那段数据。
        return data

    # close：关闭时关掉真正的物理盘句柄。
    def close(self):
        self.raw.close()

# 定义函数 scan_disk：核心扫描循环（独立成函数是为了方便自动化测试）。
# 参数：fh=对齐读取器、disk_size=盘容量、enabled=格式开关、q=消息信箱、
# stop_flag=停止信号、log=写日志的函数。
def scan_disk(fh, disk_size, enabled, q, stop_flag, log):
    """核心扫描循环（可单测）。fh 为 AlignedReader。只读，不写任何文件。"""
    # scanned：已经扫到硬盘哪个位置了（从 0 开始）。
    scanned = 0
    # carved_ranges：已确认的文件区间列表，用来跳过「照片内部嵌的小图」。
    carved_ranges = []
    # counts：统计每种扩展名找到多少个。
    counts = {}
    # 记录开始时间（算扫描耗时）。
    t0 = time.time()
    # 主循环：没扫完整块盘、且用户没点停止，就继续。
    while scanned < disk_size and not stop_flag.is_set():
        # 还剩多少字节没扫。
        remaining = disk_size - scanned
        # 本次想读的长度：8MB 块 + 512 重叠，但不超过剩余量（向上对齐到扇区）。
        want = min(CHUNK + OVERLAP, _align_up(remaining))
        # 尝试读这一大块。
        try:
            fh.seek(scanned)
            data = fh.read(want)
            # 读到空数据说明到盘尾了，结束扫描。
            if not data:
                break
            # 读成功：这一块数据加入待搜索列表（块起点 + 数据）。
            blocks = [(scanned, data)]
        # 读失败（多半是坏扇区）：启动「降级抢救」。
        except OSError as e:
            log("大块读取失败（%s），降级为 1MB 粒度重试…" % e)
            blocks = []
            # 本大块的结束位置。
            end = scanned + min(CHUNK, remaining)
            # 从大块起点开始，按 1MB 小步重试。
            sub = scanned
            while sub < end and not stop_flag.is_set():
                try:
                    fh.seek(sub)
                    d = fh.read(min(SUB, _align_up(end - sub)))
                    if d:
                        blocks.append((sub, d))
                # 这 1MB 也读不动：真坏块，记日志跳过它。
                except OSError as e2:
                    log("读取失败，跳过 1MB（偏移 %.2f GB）：%s" % (
                        sub / 1073741824.0, e2))
                sub += SUB

        # 逐块在内存里搜索照片签名。
        for base, chunk in blocks:
            # 用户点了停止就立刻退出。
            if stop_flag.is_set():
                break
            # 先收集本块全部候选，按偏移升序处理：
            # 保证 CR3 等母文件先被识别，其内嵌的 JPEG 预览/EXIF 缩略图
            # 会落入已雕刻区间而被跳过，避免一份照片捞出多份垃圾文件。
            cands = []
            # 遍历每种签名。
            for pat, kind, delta in PATTERNS:
                # 这种格式在界面上没勾选就跳过。
                if not enabled.get(KIND_ENABLED_KEY[kind], True):
                    continue
                # 在本块数据里反复查找该签名。
                pos = 0
                while True:
                    i = chunk.find(pat, pos)
                    # 找不到了就换下一种签名。
                    if i < 0:
                        break
                    # 下次从这个位置之后继续找（允许签名重叠）。
                    pos = i + 1
                    # 算出签名在整块硬盘上的绝对位置（加上位置修正）。
                    abs_off = base + i + delta
                    # 位置合法就加入候选列表。
                    if abs_off >= 0:
                        cands.append((abs_off, kind))
            # 候选按位置从小到大排序。
            cands.sort()
            # 逐个候选尝试解析。
            for abs_off, kind in cands:
                if stop_flag.is_set():
                    break
                # 如果这个位置落在「已确认的文件区间」里，说明它是照片内部嵌的图，跳过。
                if any(s <= abs_off < s + l for s, l in carved_ranges):
                    continue
                # 调 detect 确认是不是真照片、多长、什么时间拍的。
                hit = detect(fh, abs_off, kind)
                # 确认是真照片。
                if hit:
                    # 把它的区间登记到「已确认」列表（抑制嵌套误报）。
                    carved_ranges.append((hit["off"], hit["len"]))
                    # 统计该扩展名数量 +1。
                    counts[hit["ext"]] = counts.get(hit["ext"], 0) + 1
                    # 给它起好文件名（拍摄时间_序号）。
                    hit["name"] = make_hit_name(hit, sum(counts.values()))
                    # 把档案放进消息信箱，通知界面「又找到一个」。
                    q.put(("hit", hit))

        # 推进扫描位置（本块扫完了，跳到下一块；重叠区会重扫但会被去重）。
        scanned += min(CHUNK, remaining)
        # 通知界面更新进度条。
        q.put(("progress", min(scanned, disk_size)))

    # 扫描结束：汇总统计。
    total = sum(counts.values())
    elapsed = time.time() - t0
    # 区分「用户主动停止」和「自然扫完」两种结局，通知界面。
    if stop_flag.is_set():
        q.put(("scandone", "已手动停止。找到 %d 个文件。" % total))
    else:
        q.put(("scandone",
            "扫描完成！用时 %.0f 秒，找到 %d 个文件：%s" % (
                elapsed, total,
                "，".join("%s×%d" % (k, v) for k, v in sorted(counts.items())) or "无")))
# 定义类 Scanner：扫描线程。
# 【为什么用线程】扫 900GB 硬盘要几十分钟。如果在界面线程里扫，
# 窗口会彻底卡死（转圈圈）。线程让扫描在后台跑，界面照常能点。
# threading.Thread 是 Python 的线程基类，继承它就获得了「后台运行」能力。
class Scanner(threading.Thread):
    # 初始化：记住要扫哪块盘、盘多大、扫哪些格式、消息信箱放哪。
    def __init__(self, disk_num, disk_size, enabled, q):
        # 调用父类初始化；daemon=True 表示主程序退出时本线程自动跟着结束。
        super().__init__(daemon=True)
        self.disk_num = disk_num
        self.disk_size = disk_size
        self.enabled = enabled
        self.q = q
        # 停止信号旗：初始为「未停止」，界面点「停止」时会把它竖起来。
        self.stop_flag = threading.Event()
        self.counts = {}

    # 小工具：往信箱里塞一条日志消息。
    def log(self, msg):
        self.q.put(("log", msg))

    # run：线程启动后自动执行这个方法（threading 的约定）。
    def run(self):
        # Windows 访问物理盘的特殊路径写法：\\.\PhysicalDriveN（N 是磁盘编号）。
        path = r"\\.\PhysicalDrive%d" % self.disk_num
        # 以「只读二进制、不带缓冲」方式打开物理盘，并套上对齐读取翻译官。
        try:
            fh = AlignedReader(open(path, "rb", buffering=0))
        # 权限不够：通知界面提示用管理员身份运行。
        except PermissionError:
            self.q.put(("error", "无法打开磁盘：请以管理员身份运行本工具。"))
            return
        # 其他打开失败（盘被拔了等）。
        except OSError as e:
            self.q.put(("error", "无法打开磁盘：%s" % e))
            return

        # 记一条开始日志。
        self.log("开始扫描 PhysicalDrive%d（%.1f GB）..." % (
            self.disk_num, self.disk_size / 1073741824.0))
        # 调用核心扫描循环（所有意外都兜住，保证线程不无声死掉）。
        try:
            scan_disk(fh, self.disk_size, self.enabled, self.q,
                      self.stop_flag, self.log)
            fh.close()
        except Exception:
            self.log("扫描异常：\n" + traceback.format_exc())
            self.q.put(("error", "扫描中断。"))

# ----------------------------- 恢复线程（按选中的清单写文件） -----------------------------
# 【两段式设计的核心】扫描阶段只记录「哪里有照片、多长」，
# 用户在界面上勾选之后，恢复线程才按清单去硬盘上把数据读出来写进新文件。

# 定义类 Recoverer：恢复线程（同样在后台跑，不卡界面）。
class Recoverer(threading.Thread):
    # 初始化：记住源盘编号、要恢复的文件清单、输出目录、消息信箱。
    def __init__(self, disk_num, hits, out_dir, q):
        super().__init__(daemon=True)
        self.disk_num = disk_num
        self.hits = hits
        self.out_dir = out_dir
        self.q = q
        self.stop_flag = threading.Event()

    # run：线程入口。
    def run(self):
        # 打开源物理盘（只读）。
        path = r"\\.\PhysicalDrive%d" % self.disk_num
        try:
            fh = AlignedReader(open(path, "rb", buffering=0))
        except OSError as e:
            self.q.put(("error", "无法打开磁盘：%s" % e))
            return
        # ok/fail：成功、失败计数。
        ok, fail = 0, 0
        # 预先算出总字节数（进度条要用）。
        total_bytes = sum(h["len"] for h in self.hits)
        done_bytes = 0
        try:
            # 逐个文件恢复（enumerate 同时给出序号，从 1 开始）。
            for idx, h in enumerate(self.hits, 1):
                # 用户点了停止就中断。
                if self.stop_flag.is_set():
                    break
                # 按拍摄时间 年/月 归档；无时间的进「未知时间」
                tp = hit_time_parts(h)
                # 拼出该文件的分类子目录（如 "2024/2024-05"）。
                rel_dir = os.path.join(tp[0], tp[1]) if tp else "未知时间"
                sub = os.path.join(self.out_dir, rel_dir)
                # 创建目录（已存在也不报错）。
                os.makedirs(sub, exist_ok=True)
                # 取扫描时生成好的文件名（没有就现场起一个）。
                name = h.get("name") or make_hit_name(h, idx)
                # 开始抠这个文件的数据。
                try:
                    # 跳到该文件在硬盘上的起始位置。
                    fh.seek(h["off"])
                    remaining = h["len"]
                    # 以「写入二进制」模式创建目标文件。
                    with open(os.path.join(sub, name), "wb") as out:
                        # 每次最多搬 4MB，直到搬完整个文件。
                        while remaining > 0:
                            data = fh.read(min(4 * 1024 * 1024, remaining))
                            if not data:
                                break
                            out.write(data)
                            done_bytes += len(data)
                            remaining -= len(data)
                            # 每搬一块就汇报一次进度。
                            self.q.put(("rprogress", done_bytes, total_bytes))
                    # 这个文件恢复成功。
                    ok += 1
                    self.q.put(("log", "已恢复 %s（%.1fMB）" % (name, h["len"] / 1048576.0)))
                # 读盘失败（坏扇区等）：记失败，继续下一个。
                except OSError as e:
                    fail += 1
                    self.q.put(("log", "恢复失败 %s：%s" % (name, e)))
            fh.close()
        # 恢复过程中的任何其他异常都记录到日志。
        except Exception:
            self.q.put(("log", "恢复异常：\n" + traceback.format_exc()))
        # 全部结束：通知界面汇报战果。
        self.q.put(("recdone", "恢复结束：成功 %d 个，失败 %d 个。保存位置：%s" % (
            ok, fail, self.out_dir)))
# ============================= Apple 风格 UI =============================
# 以下是界面层。技术背景：tkinter 自带的控件长相比较「复古」，
# 这里用「画布自绘 + 配色规范」模拟苹果 macOS 的设计语言（HIG）。

# Apple 调色板（HIG）：苹果官方设计规范里的经典配色。
A_BG      = "#f5f5f7"   # 窗口底色（苹果官网同款浅灰）
A_CARD    = "#ffffff"   # 卡片（纯白）
A_LINE    = "#d2d2d7"   # 分隔线/描边（浅灰）
A_TEXT    = "#1d1d1f"   # 主文字（近黑）
A_GRAY    = "#86868b"   # 次要文字（中灰）
A_BLUE    = "#0071e3"   # Apple 蓝（按钮主色）
A_BLUE_H  = "#0077ed"   # 悬停时略亮的蓝
A_BLUE_D  = "#0060c9"   # 按下时略暗的蓝
A_GRAY_BG = "#e8e8ed"   # 灰色按钮底色
A_GRAY_H  = "#dcdce1"   # 灰色按钮悬停色
A_GREEN   = "#34c759"   # 苹果绿（成功/正常状态）
A_ORANGE  = "#ff9f0a"   # 苹果橙（警告）
A_RED     = "#ff3b30"   # 苹果红（危险操作）

# 字体规范：Windows 没有苹果的 SF 字体，用系统自带的 Segoe UI 替代（气质接近）。
F_TITLE = ("Segoe UI", 21, "bold")   # 大标题
F_SUB   = ("Segoe UI", 10)           # 副标题
F_SECT  = ("Segoe UI", 11, "bold")   # 区块标题
F_BODY  = ("Segoe UI", 10)           # 正文
F_SMALL = ("Segoe UI", 9)            # 小字
F_MONO  = ("Cascadia Mono", 9)       # 等宽字体（日志区，字符对齐好看）

# 定义类 Card：白色圆角卡片（视觉分组容器）。继承 tk.Frame（基础容器）。
class Card(tk.Frame):
    """白卡片：细描边 + 内边距"""
    # padx/pady 是卡片内容离边框的内边距。
    def __init__(self, parent, padx=20, pady=16, **kw):
        # 初始化外层框架：白底 + 1 像素浅灰描边。
        super().__init__(parent, bg=A_CARD,
                         highlightbackground="#e4e4e9", highlightthickness=1,
                         bd=0, **kw)
        # 里面再套一层框架专门放内容，统一加上内边距。
        self.inner = tk.Frame(self, bg=A_CARD)
        self.inner.pack(fill="both", expand=True, padx=padx, pady=pady)

# 定义类 PillButton：胶囊按钮。继承 tk.Label（用文字标签模拟按钮）。
# 【为什么用 Label 模拟】tkinter 原生 Button 在 Windows 上样式很难自定义，
# 用 Label 自己控制背景色、悬停变色，反而能做出苹果风。
class PillButton(tk.Label):
    """胶囊按钮（Apple 圆角风，tk 近似实现）"""
    # 四种按钮风格的配色表：(底色, 文字色, 悬停色)。
    KINDS = {
        "primary":   (A_BLUE, "#ffffff", A_BLUE_H),    # 主按钮：蓝底白字
        "secondary": (A_GRAY_BG, A_TEXT, A_GRAY_H),    # 次按钮：灰底黑字
        "danger":    (A_RED, "#ffffff", "#e0342b"),    # 危险按钮：红底白字
        "ghost":     (A_CARD, A_BLUE, "#eef4fd"),      # 幽灵按钮：白底蓝字（像链接）
    }
    # 初始化：text=按钮文字，command=点击后执行的函数，kind=风格，font=字体。
    def __init__(self, parent, text, command, kind="primary", font=F_BODY):
        # 记住风格和点击函数。
        self._kind = kind
        self._cmd = command
        self._enabled = True
        # 按风格查配色。
        bg, fg, hover = self.KINDS[kind]
        self._bg, self._fg, self._hover = bg, fg, hover
        # 初始化 Label：文字、颜色、字体、内边距；cursor="hand2" 让鼠标悬停变「小手」。
        super().__init__(parent, text=text, bg=bg, fg=fg, font=font,
                         padx=18, pady=7, cursor="hand2")
        # 描边颜色与底色一致（视觉上看不到边框，只起占位作用）。
        self.configure(highlightbackground=bg, highlightthickness=1)
        # 绑定鼠标事件：左键点击 → 执行命令。
        self.bind("<Button-1>", self._click)
        # 鼠标移入 → 变悬停色（前提：按钮处于可用状态）。
        self.bind("<Enter>", lambda e: self._state() and self.configure(bg=self._hover))
        # 鼠标移出 → 恢复原色。
        self.bind("<Leave>", lambda e: self._state() and self.configure(bg=self._bg))

    # 内部函数：返回按钮是否可用。
    def _state(self):
        return self._enabled

    # 内部函数：处理点击——可用且有命令时才执行。
    def _click(self, _e):
        if self._enabled and self._cmd:
            self._cmd()

    # 公有方法：启用/禁用按钮（比如扫描中禁用「开始扫描」）。
    def set_enabled(self, on):
        self._enabled = bool(on)
        if on:
            # 恢复彩色 + 小手光标。
            self.configure(bg=self._bg, fg=self._fg, cursor="hand2")
        else:
            # 变灰 + 普通箭头光标，表示「现在点不了」。
            self.configure(bg=A_GRAY_BG, fg=A_GRAY, cursor="arrow")

# 定义类 PillToggle：格式筛选胶囊开关（iOS 风格，点一下开、再点一下关）。
class PillToggle(tk.Label):
    """格式筛选胶囊开关（iOS 风）"""
    # 初始化：text=显示文字，value=初始开关状态。
    def __init__(self, parent, text, value=True):
        self._value = bool(value)
        super().__init__(parent, text=text, font=F_SMALL,
                         padx=14, pady=5, cursor="hand2")
        # 点击 → 翻转开关状态。
        self.bind("<Button-1>", lambda e: self.set(not self._value))
        self._paint()

    # 内部函数：按当前状态重新上色。
    def _paint(self):
        if self._value:
            # 开：浅蓝底 + 蓝字 + 蓝描边。
            self.configure(bg="#e8f0fe", fg=A_BLUE,
                           highlightbackground=A_BLUE, highlightthickness=1)
        else:
            # 关：白底 + 灰字 + 灰描边。
            self.configure(bg=A_CARD, fg=A_GRAY,
                           highlightbackground=A_LINE, highlightthickness=1)

    # 公有方法：设置开关状态并刷新外观。
    def set(self, v):
        self._value = bool(v)
        self._paint()

    # 公有方法：读取开关状态。
    def get(self):
        return self._value

# 定义类 AppleProgress：细圆角进度条（苹果风）。
# tkinter 没有现成的好看见进度条，用 Canvas（画布）自己画。
class AppleProgress(tk.Canvas):
    """细圆角进度条（Apple 风）"""
    def __init__(self, parent, height=6, **kw):
        super().__init__(parent, height=height, bg=A_BG,
                         highlightthickness=0, bd=0, **kw)
        self.frac = 0.0
        # 窗口大小变化时重画（进度条宽度要跟着窗口走）。
        self.bind("<Configure>", lambda e: self._draw())

    # 公有方法：设置进度（0.0 ~ 1.0）。
    def set(self, frac):
        # 限制在 0~1 之间（防止越界）。
        self.frac = max(0.0, min(1.0, frac))
        self._draw()

    # 内部函数：真正的绘制逻辑。
    def _draw(self):
        # 清空画布。
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        # 画布还没渲染出来时宽度为 0，跳过。
        if w < 4:
            return
        r = h // 2
        # 先画整条灰色轨道。
        self._pill(0, 0, w, h, A_GRAY_BG)
        # 再按进度比例画蓝色填充段。
        fw = int(w * self.frac)
        if fw > 2:
            self._pill(0, 0, max(fw, h), h, A_BLUE)

    # 内部函数：画一个「胶囊形」（两个半圆 + 中间矩形拼出来）。
    def _pill(self, x, y, w, h, color):
        r = h // 2
        self.create_oval(x, y, x + h, y + h, fill=color, outline="")
        self.create_oval(x + w - h, y, x + w, y + h, fill=color, outline="")
        self.create_rectangle(x + r, y, x + w - r, y + h, fill=color, outline="")

# 定义类 TrafficLights：窗口左上角的红黄绿三个圆点（macOS 标志性装饰，纯好看）。
class TrafficLights(tk.Canvas):
    """macOS 红绿灯装饰"""
    def __init__(self, parent):
        super().__init__(parent, width=64, height=20, bg=A_BG,
                         highlightthickness=0, bd=0)
        # 画三个圆：红、黄、绿。
        for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            x = 8 + i * 20
            self.create_oval(x, 4, x + 12, 16, fill=c, outline="")

# ----------------------------- 主界面 -----------------------------
# 定义类 App：整个应用程序的主窗口。继承 tk.Tk（tkinter 的顶级窗口）。
class App(tk.Tk):
    # 初始化：程序启动时执行一次，搭建整个界面。
    def __init__(self):
        # 初始化 tk 主窗口。
        super().__init__()
        # 窗口标题栏文字。
        self.title("照片恢复")
        # 窗口初始大小 900×740 像素。
        self.geometry("900x740")
        # 窗口最小允许拖到 800×660（再小内容就挤不下了）。
        self.minsize(800, 660)
        # 窗口背景色。
        self.configure(bg=A_BG)
        # q：消息信箱，后台线程（扫描/恢复）通过它向界面汇报。
        self.q = queue.Queue()
        # scanner/recoverer：后台线程对象（还没启动时为 None）。
        self.scanner = None
        self.recoverer = None
        # disks：枚举到的磁盘列表。
        self.disks = []
        # hits：扫描到的文件档案列表。
        self.hits = []
        # hit_iids：列表控件里每一行的内部编号（和 hits 对应）。
        self.hit_iids = []
        # _display_order：列表显示顺序（按拍摄时间排序后的下标映射）。
        self._display_order = []
        # preview_token：预览请求的「票号」（防止旧的预览结果覆盖新的）。
        self.preview_token = 0
        # preview_img：当前显示的预览图对象（必须挂引用，否则被垃圾回收消失）。
        self.preview_img = None
        self._prog_mode = None     # 进度条当前服务模式："scan"（扫描）或 "recover"（恢复）
        self._prog_max = 1         # 进度条满刻度对应的数值

        # 依次搭建界面的各个部分。
        self._setup_ttk_style()
        self._build_header()
        self._build_scan_card()
        self._build_result_card()
        self._build_bottom()

        # 启动时自动枚举一次磁盘。
        self.refresh_disks()
        # 每 150 毫秒查一次消息信箱（取后台线程的汇报）。
        self.after(150, self.poll_queue)

    # ---------- 样式 ----------

    # 定义函数 _setup_ttk_style：统一设置 ttk 控件（主要是文件列表）的样式。
    def _setup_ttk_style(self):
        st = ttk.Style(self)
        # 尝试使用 clam 主题（tkinter 里最便于自定义颜色的主题）。
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        # 文件列表：白底、深色字、行高 30。
        st.configure("Treeview",
                     background=A_CARD, fieldbackground=A_CARD, foreground=A_TEXT,
                     rowheight=30, font=F_BODY, borderwidth=0)
        # 列表表头：灰底灰字、扁平无框。
        st.configure("Treeview.Heading",
                     background=A_BG, foreground=A_GRAY, font=F_SMALL,
                     relief="flat", borderwidth=0, padding=(8, 6))
        # 列表选中行：蓝底白字（苹果风）。
        st.map("Treeview",
               background=[("selected", A_BLUE)],
               foreground=[("selected", "#ffffff")])
        # 去掉 Treeview 默认的内边距布局，让内容撑满。
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        # 下拉框样式。
        st.configure("TCombobox", padding=6, fieldbackground=A_CARD,
                     background=A_CARD, foreground=A_TEXT)
        # 分隔线颜色。
        st.configure("TSeparator", background=A_LINE)

    # ---------- 布局 ----------

    # 定义函数 _build_header：搭建顶部标题栏。
    def _build_header(self):
        head = tk.Frame(self, bg=A_BG)
        head.pack(fill="x", padx=24, pady=(16, 4))
        # 左边放红绿灯装饰。
        TrafficLights(head).pack(side="left", pady=(8, 0))
        # 红绿灯右边放大标题。
        tk.Label(head, text="照片恢复", bg=A_BG, fg=A_TEXT,
                 font=F_TITLE).pack(side="left", padx=(16, 0))
        # 右侧权限胶囊：根据当前权限显示不同内容。
        if is_admin():
            # 已是管理员：显示绿色小字。
            tk.Label(head, text="● 管理员权限", bg=A_BG, fg=A_GREEN,
                     font=F_SMALL).pack(side="right", pady=(14, 0))
        else:
            # 不是管理员：显示黄色提示条 + 「获取权限」按钮。
            bar = tk.Frame(head, bg="#fff7ed",
                           highlightbackground="#ffd9a8", highlightthickness=1)
            bar.pack(side="right", pady=(10, 0))
            tk.Label(bar, text="需要管理员权限", bg="#fff7ed", fg="#b25000",
                     font=F_SMALL, padx=10, pady=6).pack(side="left")
            b = PillButton(bar, "获取权限", relaunch_as_admin, "primary", F_SMALL)
            b.pack(side="left", padx=(4, 8), pady=3)
        # 标题下方一行灰色小字，说明软件用途。
        tk.Label(self, text="HFS+ 移动硬盘 · 误删照片找回 · 扫描全程只读，确认后再恢复",
                 bg=A_BG, fg=A_GRAY, font=F_SUB).pack(anchor="w", padx=24)

    # 定义函数 _build_scan_card：搭建「第 1 步 · 扫描」卡片。
    def _build_scan_card(self):
        self.scan_card = Card(self)
        self.scan_card.pack(fill="x", padx=24, pady=(12, 0))
        c = self.scan_card.inner

        # 区块标题（grid 布局：按行列摆放，columnspan=8 表示占满整行）。
        tk.Label(c, text="第 1 步 · 扫描", bg=A_CARD, fg=A_TEXT,
                 font=F_SECT).grid(row=0, column=0, sticky="w", columnspan=8)
        # 提示文字。
        tk.Label(c, text="选择移动硬盘（HFS+ 盘通常显示为「未知」，按容量和品牌辨认）",
                 bg=A_CARD, fg=A_GRAY, font=F_SMALL).grid(row=1, column=0, sticky="w",
                                                          columnspan=8, pady=(2, 10))

        # 磁盘选择下拉框（只读，只能从列表里选）。
        self.disk_var = tk.StringVar()
        self.disk_combo = ttk.Combobox(c, textvariable=self.disk_var,
                                       state="readonly", width=76, font=F_BODY)
        self.disk_combo.grid(row=2, column=0, sticky="we", columnspan=7)
        # 「刷新」按钮：重新枚举磁盘（插拔硬盘后点）。
        self.refresh_btn = PillButton(c, "刷新", self.refresh_disks, "secondary", F_SMALL)
        self.refresh_btn.grid(row=2, column=7, padx=(8, 0), sticky="e")

        # 「照片格式」标签。
        tk.Label(c, text="照片格式", bg=A_CARD, fg=A_GRAY,
                 font=F_SMALL).grid(row=3, column=0, sticky="w", pady=(14, 6))
        # 四个格式开关，横排摆放。
        fmt = tk.Frame(c, bg=A_CARD)
        fmt.grid(row=4, column=0, columnspan=8, sticky="w")
        # 默认只勾选佳能 CR3（用户相机拍的），其他默认关闭减少垃圾文件。
        self.tg_jpg = PillToggle(fmt, "JPEG · jpg", False)
        self.tg_raw = PillToggle(fmt, "RAW · arw / nef / cr2 / dng", False)
        self.tg_raf = PillToggle(fmt, "富士 · raf", False)
        self.tg_cr3 = PillToggle(fmt, "佳能 · cr3", True)
        for w in (self.tg_jpg, self.tg_raw, self.tg_raf, self.tg_cr3):
            w.pack(side="left", padx=(0, 8))

        # 操作按钮行：开始扫描 / 停止 / 载入之前的扫描结果。
        act = tk.Frame(c, bg=A_CARD)
        act.grid(row=5, column=0, columnspan=8, sticky="w", pady=(18, 2))
        self.start_btn = PillButton(act, "开始扫描", self.start_scan, "primary")
        self.start_btn.pack(side="left")
        self.stop_btn = PillButton(act, "停止", self.stop_scan, "secondary")
        self.stop_btn.pack(side="left", padx=10)
        # 初始状态「停止」不可点（还没开始扫）。
        self.stop_btn.set_enabled(False)
        self.load_btn = PillButton(act, "载入扫描结果", self.load_scan_file, "ghost")
        self.load_btn.pack(side="left", padx=10)
    # 定义函数 _build_result_card：搭建「第 2 步 · 选择要恢复的照片」卡片。
    # 注意：这里只创建不显示（没有 pack），扫描完成后才显示出来。
    def _build_result_card(self):
        self.result_card = Card(self)
        c = self.result_card.inner

        # 顶部标题行。
        top = tk.Frame(c, bg=A_CARD)
        top.pack(fill="x")
        tk.Label(top, text="第 2 步 · 选择要恢复的照片", bg=A_CARD, fg=A_TEXT,
                 font=F_SECT).pack(side="left")
        # 统计信息（找到多少个文件），扫描完才填文字。
        self.result_label = tk.Label(top, text="", bg=A_CARD, fg=A_GRAY, font=F_SMALL)
        self.result_label.pack(side="left", padx=(12, 0))

        # 工具行：全选 / 全不选 / 保存扫描结果 / 返回重新扫描。
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
        # 输出路径输入框，默认在桌面建「恢复的照片」文件夹。
        self.out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "恢复的照片"))
        self.out_entry = tk.Entry(rec, textvariable=self.out_var, font=F_SMALL,
                                  bg=A_CARD, fg=A_TEXT, relief="flat",
                                  highlightthickness=1, highlightbackground=A_LINE,
                                  highlightcolor=A_BLUE, width=36)
        self.out_entry.pack(side="left", padx=(8, 6), ipady=4)
        # 「浏览…」按钮：弹文件夹选择框。
        PillButton(rec, "浏览…", self.pick_out, "secondary", F_SMALL).pack(side="left")
        # 三个恢复按钮靠右排（pack side=right 是从右往左摆）。
        self.rec_sel_btn = PillButton(rec, "恢复选中文件", lambda: self.start_recover(False), "primary")
        self.rec_sel_btn.pack(side="right")
        self.rec_all_btn = PillButton(rec, "恢复全部文件", lambda: self.start_recover(True), "secondary")
        self.rec_all_btn.pack(side="right", padx=(0, 8))
        self.rec_stop_btn = PillButton(rec, "停止恢复", self.stop_recover, "danger", F_SMALL)
        self.rec_stop_btn.pack(side="right", padx=(0, 8))
        # 初始状态「停止恢复」不可点。
        self.rec_stop_btn.set_enabled(False)

        # 中部主体：左边文件列表，右边预览图。
        body = tk.Frame(c, bg=A_CARD)
        body.pack(fill="both", expand=True)

        # 左：列表（带细灰边框的容器）。
        list_wrap = tk.Frame(body, bg=A_CARD, highlightbackground="#e4e4e9",
                             highlightthickness=1)
        list_wrap.pack(side="left", fill="both", expand=True)
        # 定义四列。
        cols = ("name", "dt", "size", "offset")
        self.tree = ttk.Treeview(list_wrap, columns=cols, show="headings",
                                 selectmode="extended")
        # 设置各列表头文字。
        self.tree.heading("name", text="文件名（按拍摄时间生成）")
        self.tree.heading("dt", text="拍摄时间")
        self.tree.heading("size", text="大小")
        self.tree.heading("offset", text="磁盘偏移")
        # 设置各列宽度和对齐方式。
        self.tree.column("name", width=200, anchor="w")
        self.tree.column("dt", width=140, anchor="center")
        self.tree.column("size", width=70, anchor="e")
        self.tree.column("offset", width=120, anchor="e")
        # 斑马纹：奇数行浅灰底（可读性更好）。
        self.tree.tag_configure("odd", background="#fafafc")
        # 右侧滚动条，与列表联动。
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # 选中行变化时触发 on_select（更新右侧预览）。
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        # 右：预览区（固定宽 280）。
        pv = tk.Frame(body, bg=A_CARD, width=280)
        pv.pack(side="right", fill="y", padx=(14, 0))
        # 禁止容器随内容缩放（保持固定宽度）。
        pv.pack_propagate(False)
        tk.Label(pv, text="预览", bg=A_CARD, fg=A_GRAY, font=F_SMALL).pack(anchor="w")
        # 预览图显示区：没图时显示提示文字。
        self.preview_label = tk.Label(pv, bg="#f0f0f3", fg=A_GRAY,
                                      text="选中左侧文件\n查看预览",
                                      font=F_SMALL)
        self.preview_label.pack(fill="both", expand=True, pady=(4, 0))
        self.preview_label.configure(highlightbackground="#e4e4e9", highlightthickness=1)
        # 预览图下方的小字信息（格式、大小、偏移）。
        self.preview_info = tk.Label(pv, text="", bg=A_CARD, fg=A_GRAY,
                                     font=F_SMALL, wraplength=270, justify="left")
        self.preview_info.pack(anchor="w", pady=(6, 0))

    # 定义函数 _build_bottom：搭建底部区域（进度条 + 状态文字 + 日志）。
    def _build_bottom(self):
        bot = tk.Frame(self, bg=A_BG)
        bot.pack(fill="both", expand=True, padx=24, pady=(12, 16))

        # 苹果风细进度条。
        self.pbar = AppleProgress(bot)
        self.pbar.pack(fill="x")
        # 状态文字（如「扫描中… 已找到 100 个文件」）。
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(bot, textvariable=self.status_var, bg=A_BG, fg=A_GRAY,
                 font=F_SMALL).pack(anchor="w", pady=(6, 8))

        # 日志卡片。
        log_card = Card(bot, padx=12, pady=10)
        log_card.pack(fill="both", expand=True)
        tk.Label(log_card.inner, text="运行日志", bg=A_CARD, fg=A_GRAY,
                 font=F_SMALL).pack(anchor="w")
        # 日志文本框：只读（state=disabled 防止用户误改）、等宽字体。
        self.logbox = scrolledtext.ScrolledText(
            log_card.inner, height=8, state="disabled",
            font=F_MONO, bg=A_CARD, fg="#4b4b50",
            relief="flat", borderwidth=0, highlightthickness=0,
            insertbackground=A_TEXT)
        self.logbox.pack(fill="both", expand=True, pady=(4, 0))

    # ---------- 基础 ----------

    # 定义方法 log：往日志框追加一行文字。
    def log(self, msg):
        # 临时解锁文本框。
        self.logbox.configure(state="normal")
        # 在末尾插入消息和换行。
        self.logbox.insert("end", msg + "\n")
        # 滚动到最后一行（最新日志永远可见）。
        self.logbox.see("end")
        # 重新锁定为只读。
        self.logbox.configure(state="disabled")

    # 定义方法 refresh_disks：重新枚举磁盘并填进下拉框。
    def refresh_disks(self):
        # 调用底层枚举函数，失败就弹错误框。
        try:
            self.disks = list_disks()
        except Exception as e:
            messagebox.showerror("错误", "枚举磁盘失败：%s" % e)
            return
        # 把每块盘格式化成一行可读文字。
        items = ["磁盘 %d  ·  %s  ·  %.1f GB  ·  %s" % (
            d["num"], d["name"] or "未知设备", d["size"] / 1073741824.0, d["bus"])
            for d in self.disks]
        self.disk_combo["values"] = items
        # 智能预选：优先选中 USB 接口的盘（移动硬盘通常是 USB）。
        for idx, d in enumerate(self.disks):
            if "USB" in d["bus"].upper():
                self.disk_combo.current(idx)
                break
        # 没有 USB 盘就选第一块。
        if not self.disk_var.get() and items:
            self.disk_combo.current(0)

    # 定义方法 pick_out：弹出文件夹选择框，让用户选恢复保存目录。
    def pick_out(self):
        d = filedialog.askdirectory()
        if d:
            self.out_var.set(d)

    # 定义方法 current_disk：返回当前下拉框选中的磁盘信息。
    def current_disk(self):
        idx = self.disk_combo.current()
        if idx < 0 or idx >= len(self.disks):
            return None
        return self.disks[idx]
    # ---------- 扫描 ----------

    # 定义方法 start_scan：点击「开始扫描」时执行。
    def start_scan(self):
        # 取出用户选中的磁盘。
        disk = self.current_disk()
        if not disk:
            messagebox.showwarning("提示", "请先选择要扫描的移动硬盘")
            return
        # 权限检查：必须是管理员。
        if not is_admin():
            messagebox.showwarning("权限不足", "需要管理员权限，请点击右上角「获取权限」。")
            return
        # 二次确认弹窗（读盘是大事，让用户再确认一次目标和只读承诺）。
        if not messagebox.askyesno("开始扫描",
                "将对【磁盘 %d | %s | %.1f GB】进行只读扫描（不写任何文件）。\n"
                "扫描完成后由你勾选要恢复的文件。\n\n大容量硬盘可能需要较长时间，是否开始？"
                % (disk["num"], disk["name"], disk["size"] / 1073741824.0)):
            return

        # 收集四个格式开关的状态。
        enabled = {"jpg": self.tg_jpg.get(), "raw": self.tg_raw.get(),
                   "raf": self.tg_raf.get(), "cr3": self.tg_cr3.get()}
        # 清空上一次的扫描结果。
        self.hits = []
        self.hit_iids = []
        # 创建并启动扫描线程（后台开始跑）。
        self.scanner = Scanner(disk["num"], disk["size"], enabled, self.q)
        self.scanner.start()
        # 进度条切换到「扫描模式」，满刻度 = 磁盘总字节数。
        self._prog_mode = "scan"
        self._prog_max = disk["size"]
        self.pbar.set(0)
        # 按钮状态切换：「开始」禁用、「停止」启用。
        self.start_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.status_var.set("扫描中…")
        self.log("开始扫描磁盘 %d（%s）…" % (disk["num"], disk["name"]))

    # 定义方法 stop_scan：点击「停止」时执行——竖起停止旗，线程看到后会自己停下。
    def stop_scan(self):
        if self.scanner:
            self.scanner.stop_flag.set()
            self.status_var.set("正在停止…")

    # 定义方法 show_results：扫描结束后，从「扫描卡片」切换到「结果卡片」。
    def show_results(self):
        # 隐藏扫描卡片。
        self.scan_card.pack_forget()
        # 显示结果卡片。
        self.result_card.pack(fill="both", expand=True, padx=24, pady=(12, 0))
        # 把扫描结果填进列表。
        self.rebuild_tree()
        self.status_var.set("扫描结束，共找到 %d 个文件。请勾选要恢复的文件。" % len(self.hits))
        self.pbar.set(1.0)

    # 定义方法 rebuild_tree：把文件档案填进列表控件（按拍摄时间倒序）。
    def rebuild_tree(self):
        # 清空列表旧内容。
        self.tree.delete(*self.tree.get_children())
        self.hit_iids = []
        # 按拍摄时间倒序展示，未知时间（空字符串）排最后
        ordered = sorted(range(len(self.hits)),
                         key=lambda i: self.hits[i].get("dt") or "", reverse=True)
        # 按排好的顺序逐行插入。
        for row, i in enumerate(ordered):
            h = self.hits[i]
            # 奇数行加斑马纹标签。
            tag = ("odd",) if row % 2 else ()
            name = h.get("name") or make_hit_name(h, i + 1)
            iid = self.tree.insert("", "end", values=(
                name, fmt_dt_display(h.get("dt")),
                "%.1f MB" % (h["len"] / 1048576.0),
                "0x%X" % h["off"]), tags=tag)
            self.hit_iids.append(iid)
        # 记住「显示行号 → hits 下标」的映射（勾选时要还原）。
        self._display_order = ordered
        # 统计各格式数量，更新到标题旁的统计文字。
        cnt = {}
        for h in self.hits:
            cnt[h["ext"]] = cnt.get(h["ext"], 0) + 1
        self.result_label.configure(text="共 %d 个文件 · %s" % (
            len(self.hits),
            " · ".join("%s × %d" % (k, v) for k, v in sorted(cnt.items())) or "无"))

    # 定义方法 back_to_scan：点击「返回重新扫描」时切回扫描卡片。
    def back_to_scan(self):
        self.result_card.pack_forget()
        self.scan_card.pack(fill="x", padx=24, pady=(12, 0))
        self.status_var.set("就绪")
        self.pbar.set(0)

    # ---------- 选择 & 预览 ----------

    # 定义方法 select_all：全选列表所有行。
    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    # 定义方法 select_none：取消所有选择。
    def select_none(self):
        self.tree.selection_remove(self.tree.get_children())

    # 定义方法 selected_hits：把界面上勾选的行，还原成文件档案列表。
    def selected_hits(self):
        sel = set(self.tree.selection())
        out = []
        # 遍历显示顺序，行被选中就把对应的档案加进来。
        for row, iid in enumerate(self.hit_iids):
            if iid in sel:
                out.append(self.hits[self._display_order[row]])
        return out

    # 定义方法 on_select：列表选中变化时触发——更新右侧预览。
    def on_select(self, _evt=None):
        hits = self.selected_hits()
        if not hits:
            return
        # 只预览第一个选中项。
        hit = hits[0]
        self.preview_info.configure(text="%s · %.1f MB · 偏移 %d" % (
            hit["ext"], hit["len"] / 1048576.0, hit["off"]))
        # 没有 Pillow 就提示无法预览。
        if not HAS_PIL:
            self.preview_label.configure(image="", text="（未安装 Pillow，无预览）")
            return
        disk = self.current_disk()
        if not disk:
            return
        # 票号 +1（旧预览线程回来时凭票号知道自己过时了，直接丢弃结果）。
        self.preview_token += 1
        token = self.preview_token
        self.preview_label.configure(image="", text="读取预览中…")
        # 预览要从硬盘读数据，放到后台线程，避免卡住列表点击。
        threading.Thread(target=self._load_preview,
                         args=(token, disk["num"], hit), daemon=True).start()

    # 定义方法 _load_preview：后台线程——从硬盘抠出预览图字节并解码。
    def _load_preview(self, token, disk_num, hit):
        try:
            fh = AlignedReader(open(r"\\.\PhysicalDrive%d" % disk_num, "rb", buffering=0))
            data = extract_preview_bytes(fh, hit)
            fh.close()
            if not data:
                self.q.put(("preview_fail", token, "（该文件无内嵌预览图）"))
                return
            # 用 Pillow 解码图片字节，并缩放到 280 以内。
            im = Image.open(io.BytesIO(data))
            im.thumbnail((280, 280))
            # 图片对象通过信箱送回界面线程（tkinter 控件只能在主线程操作）。
            self.q.put(("preview", token, im))
        except Exception as e:
            self.q.put(("preview_fail", token, "预览失败：%s" % e))

    # 定义方法 show_preview：在主线程里把预览图贴到界面上。
    def show_preview(self, token, im):
        # 票号对不上说明用户已经点了别的文件，丢弃这张旧图。
        if token != self.preview_token:
            return
        # 转成 tkinter 能显示的图片对象（必须存到 self 上防止被回收）。
        self.preview_img = ImageTk.PhotoImage(im)
        self.preview_label.configure(image=self.preview_img, text="")

    # ---------- 恢复 ----------

    # 定义方法 start_recover：点击「恢复选中/全部文件」时执行。
    def start_recover(self, recover_all):
        disk = self.current_disk()
        if not disk:
            messagebox.showwarning("提示", "请先选择源移动硬盘")
            return
        # 确定要恢复哪些文件：全部 或 勾选的。
        hits = self.hits if recover_all else self.selected_hits()
        if not hits:
            messagebox.showwarning("提示", "请先在列表中勾选要恢复的文件")
            return
        # 检查输出目录。
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning("提示", "请选择保存目录")
            return
        # 【最重要的安全检查】输出目录绝不能在被恢复的源盘上！
        # 往源盘写数据会覆盖还没恢复的照片，等于亲手毁掉它们。
        m = re.match(r"^([A-Za-z]):", out)
        if m:
            # 查输出目录所在盘符属于哪块物理盘。
            dn = disk_num_of_drive_letter(m.group(1))
            # 如果就是源盘，坚决阻止并解释原因。
            if dn is not None and dn == disk["num"]:
                messagebox.showerror("危险操作已阻止",
                    "保存目录就在被恢复的移动硬盘上！\n写入数据会覆盖待恢复的照片，请选择电脑上的其他磁盘。")
                return
        # 弹确认框，告知文件数和总量。
        total_mb = sum(h["len"] for h in hits) / 1048576.0
        if not messagebox.askyesno("开始恢复",
                "将恢复 %d 个文件（共 %.0f MB）到：\n%s\n\n是否开始？"
                % (len(hits), total_mb, out)):
            return
        # 创建输出根目录。
        os.makedirs(out, exist_ok=True)
        # 每次恢复建一个带时间戳的子文件夹，多次恢复互不覆盖。
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(out, "恢复结果_" + stamp)
        # 创建并启动恢复线程。
        self.recoverer = Recoverer(disk["num"], hits, out_dir, self.q)
        self.recoverer.start()
        # 进度条切换到「恢复模式」。
        self._prog_mode = "recover"
        self.pbar.set(0)
        # 按钮状态切换。
        self.rec_sel_btn.set_enabled(False)
        self.rec_all_btn.set_enabled(False)
        self.rec_stop_btn.set_enabled(True)
        self.log("开始恢复 %d 个文件到 %s …" % (len(hits), out_dir))

    # 定义方法 stop_recover：点击「停止恢复」时竖起停止旗。
    def stop_recover(self):
        if self.recoverer:
            self.recoverer.stop_flag.set()
            self.status_var.set("正在停止恢复…")

    # ---------- 扫描结果存取 ----------
    # 【用途】900GB 的盘扫一次要近一小时。把扫描结果存成 JSON 文件后，
    # 下次打开软件直接「载入扫描结果」就能勾选恢复，不用重扫。

    # 定义方法 save_scan_file：把扫描结果保存成 JSON 文件。
    def save_scan_file(self):
        disk = self.current_disk()
        if not self.hits:
            messagebox.showwarning("提示", "当前没有扫描结果可保存")
            return
        # 弹「另存为」对话框，默认文件名「扫描结果.json」。
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("扫描结果", "*.json")],
            initialfile="扫描结果.json")
        # 用户点了取消就直接返回。
        if not path:
            return
        # 组织要保存的数据：版本号、保存时间、磁盘信息、文件清单。
        data = {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "disk": disk,
            "hits": self.hits,
        }
        # 写入 JSON 文件（UTF-8 编码，中文不乱码）。
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        self.log("扫描结果已保存：%s" % path)

    # 定义方法 load_scan_file：载入之前保存的扫描结果。
    def load_scan_file(self):
        # 弹文件选择框。
        path = filedialog.askopenfilename(filetypes=[("扫描结果", "*.json")])
        if not path:
            return
        # 读取并解析 JSON。
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            hits = data["hits"]
            saved_disk = data.get("disk") or {}
        except Exception as e:
            messagebox.showerror("错误", "读取扫描结果失败：%s" % e)
            return
        # 安全检查：载入的结果必须来自当前选中的那块盘（按容量比对），
        # 防止拿着 A 盘的扫描结果去 B 盘上恢复，读出垃圾数据。
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
        # 载入清单并直接跳到结果界面。
        self.hits = hits
        self.show_results()
        self.log("已载入扫描结果：%s（%d 个文件，扫描于 %s）" % (
            path, len(hits), data.get("saved_at", "未知时间")))

    # ---------- 队列轮询 ----------
    # 【核心机制】后台线程不能碰界面控件（tkinter 的限制），它们只往「信箱」q 里
    # 塞消息；主线程每 150 毫秒开一次信箱，按消息类型更新界面。

    # 定义方法 poll_queue：检查消息信箱，处理后台线程发来的所有消息。
    def poll_queue(self):
        # try 的用法：一直取消息直到信箱空了（queue.Empty 异常）为止。
        try:
            while True:
                # 非阻塞取一条消息（没有就立刻抛 Empty 异常）。
                item = self.q.get_nowait()
                kind = item[0]
                # 消息类型：日志 → 追加到日志框。
                if kind == "log":
                    self.log(item[1])
                # 消息类型：新发现一个文件 → 加入清单并更新状态文字。
                elif kind == "hit":
                    self.hits.append(item[1])
                    h = item[1]
                    self.status_var.set("扫描中… 已找到 %d 个文件（最新 %s %.1fMB）" % (
                        len(self.hits), h["ext"], h["len"] / 1048576.0))
                # 消息类型：扫描进度 → 更新进度条。
                elif kind == "progress":
                    if self._prog_mode == "scan" and self.scanner:
                        self.pbar.set(item[1] / max(1, self.scanner.disk_size))
                # 消息类型：恢复进度 → 更新进度条和百分比。
                elif kind == "rprogress":
                    done, total = item[1], item[2]
                    if total > 0:
                        self.pbar.set(done / total)
                        self.status_var.set("恢复中… %.1f%%" % (100.0 * done / total))
                # 消息类型：预览图就绪 → 显示出来。
                elif kind == "preview":
                    self.show_preview(item[1], item[2])
                # 消息类型：预览失败 → 显示原因。
                elif kind == "preview_fail":
                    if item[1] == self.preview_token:
                        self.preview_img = None
                        self.preview_label.configure(image="", text=item[2])
                # 消息类型：扫描结束 → 记日志、恢复按钮状态、切到结果界面。
                elif kind == "scandone":
                    self.log(item[1])
                    self.start_btn.set_enabled(True)
                    self.stop_btn.set_enabled(False)
                    if self.hits:
                        self.show_results()
                    else:
                        self.status_var.set(item[1])
                        messagebox.showinfo("扫描结束", item[1] + "\n未找到可恢复的照片。")
                # 消息类型：恢复结束 → 记日志、恢复按钮状态、弹完成提示。
                elif kind == "recdone":
                    self.log(item[1])
                    self.status_var.set(item[1])
                    self.rec_sel_btn.set_enabled(True)
                    self.rec_all_btn.set_enabled(True)
                    self.rec_stop_btn.set_enabled(False)
                    messagebox.showinfo("恢复完成", item[1])
                # 消息类型：出错 → 记日志、弹错误框、重置所有按钮。
                elif kind == "error":
                    self.log("错误：" + item[1])
                    messagebox.showerror("错误", item[1])
                    self.start_btn.set_enabled(True)
                    self.stop_btn.set_enabled(False)
                    self.rec_sel_btn.set_enabled(True)
                    self.rec_all_btn.set_enabled(True)
                    self.rec_stop_btn.set_enabled(False)
        # 信箱空了（正常情况），跳出循环。
        except queue.Empty:
            pass
        # 150 毫秒后再次检查信箱（如此循环往复，直到程序退出）。
        self.after(150, self.poll_queue)

# 【程序入口】
# 这行的意思是：只有「直接运行本文件」时才执行下面的代码；
# 如果本文件是被别的程序 import 的，就不执行（方便测试）。
if __name__ == "__main__":
    # 创建主窗口对象，并进入「事件循环」——程序从此一直运行，
    # 等待你的点击、键盘等操作，直到你关闭窗口。
    App().mainloop()

