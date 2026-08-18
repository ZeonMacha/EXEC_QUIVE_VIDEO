# -*- coding: utf-8 -*-
"""Thin batch caller for Xiaowan Toolbox 一图流 (uses 小丸\\tools\\ffmpeg.exe)."""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
import xml.etree.ElementTree as ET
from pathlib import Path
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

STATE_FILE = Path(__file__).with_name("onepic_batch_state.json")

AUDIO_EXTS = {
    ".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".wma",
    ".ape", ".tak", ".tta", ".aiff", ".aif", ".opus", ".ac3",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

CREATE_NO_WINDOW = 0x08000000
WM_DROPFILES = 0x0233
WM_NULL = 0x0000
WH_GETMESSAGE = 3
PM_REMOVE = 1
GWL_EXSTYLE = -20
WS_EX_ACCEPTFILES = 0x00000010

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _TKDND = True
except Exception:
    DND_FILES = "DND_Files"
    TkinterDnD = None
    _TKDND = False


def xiaowan_ffmpeg(xiaowan_dir: str | Path) -> Path:
    return Path(xiaowan_dir) / "tools" / "ffmpeg.exe"


def xiaowan_config(xiaowan_dir: str | Path) -> Path:
    return Path(xiaowan_dir) / "xiaowan.exe.Config"


def is_xiaowan_dir(path: str | Path | None) -> bool:
    if not path:
        return False
    return xiaowan_ffmpeg(path).is_file()


def resolve_xiaowan_dir(path: str | Path | None) -> str:
    if not path:
        return ""
    item = Path(path)
    try:
        if is_xiaowan_dir(item):
            return str(item)
        if item.is_file() and item.name.lower() == "xiaowan.exe" and is_xiaowan_dir(item.parent):
            return str(item.parent)
        if item.name.lower() == "tools" and is_xiaowan_dir(item.parent):
            return str(item.parent)
        if item.is_file() and item.name.lower() == "ffmpeg.exe" and is_xiaowan_dir(item.parent.parent):
            return str(item.parent.parent)
    except OSError:
        return ""
    return ""


def detect_xiaowan_dir(saved: str | None = None) -> str:
    candidates: list[str] = []
    resolved = resolve_xiaowan_dir(saved)
    if resolved:
        return resolved
    for letter in "CDEFG":
        candidates.append(fr"{letter}:\小丸")
    seen: set[str] = set()
    for raw in candidates:
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        if is_xiaowan_dir(raw):
            return raw
    return ""


def load_xiaowan_onepic_settings(xiaowan_dir: str | Path) -> tuple[int, str]:
    fps, crf = 1, "24"
    config = xiaowan_config(xiaowan_dir)
    if not config.exists():
        return fps, crf
    tree = ET.parse(config)
    for add in tree.findall(".//add"):
        key = add.attrib.get("key")
        value = add.attrib.get("value", "").strip()
        if key == "OnePicFPS" and value:
            fps = max(1, int(float(value)))
        elif key == "OnePicCRF" and value:
            crf = value
    return fps, crf


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(data: dict) -> None:
    current = load_state()
    current.update(data)
    STATE_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def existing_dir(path: str | None) -> str:
    if not path:
        return ""
    item = Path(path)
    try:
        if item.is_dir():
            return str(item)
        parent = item.parent
        if parent.is_dir():
            return str(parent)
    except OSError:
        return ""
    return ""


def dialog_initialdir(path: str | None) -> dict:
    folder = existing_dir(path)
    return {"initialdir": folder} if folder else {}


def sanitize_prefix(prefix: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "", prefix.strip())
    return cleaned.replace("\n", "").replace("\r", "")


def output_path(audio_path: Path, prefix: str) -> Path:
    return audio_path.with_name(f"{sanitize_prefix(prefix)}{audio_path.stem}.flv")


def normalize_drop_path(raw: str) -> str:
    path = raw.strip().strip("{}")
    if path.lower().startswith("file:"):
        from urllib.parse import unquote, urlparse
        parsed = urlparse(path)
        path = unquote(parsed.path or path)
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        path = path.replace("/", "\\")
    return path


def collect_audio_files(paths: list[str]) -> list[str]:
    found: list[str] = []
    for raw in paths:
        item = Path(raw)
        if item.is_dir():
            found.extend(
                str(p) for p in sorted(item.rglob("*"))
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS
            )
        elif item.is_file() and item.suffix.lower() in AUDIO_EXTS:
            found.append(str(item))
    return found


def query_dropped_files(hdrop) -> list[str]:
    shell32 = ctypes.windll.shell32
    count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    paths: list[str] = []
    for index in range(count):
        length = shell32.DragQueryFileW(hdrop, index, None, 0)
        buffer = ctypes.create_unicode_buffer(length + 1)
        shell32.DragQueryFileW(hdrop, index, buffer, length + 1)
        if buffer.value:
            paths.append(buffer.value)
    shell32.DragFinish(hdrop)
    return paths


def hook_windows_file_drop(widget: tk.Misc, callback) -> object | None:
    """Accept Explorer file drops on a Tk window, including child widgets."""
    if os.name != "nt":
        return None

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    is_64 = ctypes.sizeof(ctypes.c_void_p) == 8
    LRESULT = ctypes.c_int64 if is_64 else ctypes.c_long
    get_long = user32.GetWindowLongPtrW if is_64 else user32.GetWindowLongW
    set_long = user32.SetWindowLongPtrW if is_64 else user32.SetWindowLongW
    get_long.restype = ctypes.c_void_p
    set_long.restype = ctypes.c_void_p
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", POINT),
        ]

    hook = {"id": None, "proc": None, "enum": None}

    def toplevel_hwnd() -> int:
        try:
            frame = widget.wm_frame()
            if frame:
                return int(str(frame), 16)
        except Exception:
            pass
        hwnd = widget.winfo_id()
        parent = user32.GetParent(hwnd)
        return parent or hwnd

    def enable_accept(hwnd: int) -> None:
        if not hwnd:
            return
        try:
            shell32.DragAcceptFiles(hwnd, True)
            style = get_long(hwnd, GWL_EXSTYLE) or 0
            set_long(hwnd, GWL_EXSTYLE, style | WS_EX_ACCEPTFILES)
        except Exception:
            pass

    def enable_tree() -> None:
        root_hwnd = toplevel_hwnd()
        enable_accept(root_hwnd)
        enable_accept(widget.winfo_id())

        def enum_cb(hwnd, _lparam):
            enable_accept(hwnd)
            return True

        hook["enum"] = WNDENUMPROC(enum_cb)
        user32.EnumChildWindows(root_hwnd, hook["enum"], 0)

    @HOOKPROC
    def getmsg_proc(ncode, wparam, lparam):
        if ncode >= 0 and int(wparam) == PM_REMOVE:
            msg = ctypes.cast(lparam, ctypes.POINTER(MSG)).contents
            if msg.message == WM_DROPFILES:
                try:
                    paths = query_dropped_files(msg.wParam)
                    widget.after(0, lambda p=paths: callback(p))
                except Exception:
                    try:
                        shell32.DragFinish(msg.wParam)
                    except Exception:
                        pass
                msg.message = WM_NULL
        return user32.CallNextHookEx(hook["id"], ncode, wparam, lparam)

    def attach() -> None:
        enable_tree()
        hook["proc"] = getmsg_proc
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        hook["id"] = user32.SetWindowsHookExW(
            WH_GETMESSAGE,
            hook["proc"],
            None,
            ctypes.windll.kernel32.GetCurrentThreadId(),
        )
        widget.after(400, enable_tree)

    widget.after_idle(attach)
    return hook


def run_ffmpeg(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def audio_duration_seconds(audio_path: Path, ffmpeg: Path) -> float:
    if audio_path.suffix.lower() == ".wav":
        try:
            with wave.open(str(audio_path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                if rate:
                    return frames / float(rate)
        except Exception:
            pass

    proc = run_ffmpeg([str(ffmpeg), "-i", str(audio_path)])
    text = (proc.stderr or "") + (proc.stdout or "")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        raise RuntimeError(f"无法读取音频时长：{audio_path.name}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def prepare_cover_jpg(src: Path, dest: Path, ffmpeg: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in {".jpg", ".jpeg"}:
        shutil.copy2(src, dest)
        return
    proc = run_ffmpeg(
        [str(ffmpeg), "-y", "-i", str(src), str(dest)]
    )
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError("封面转 JPG 失败：\n" + (proc.stderr or proc.stdout or ""))


def export_one(
    audio_path: Path,
    cover_jpg: Path,
    fps: int,
    crf: str,
    work_dir: Path,
    skip_existing: bool,
    prefix: str,
    ffmpeg: Path,
) -> str:
    output = output_path(audio_path, prefix)
    if skip_existing and output.exists():
        return "skip"

    duration = audio_duration_seconds(audio_path, ffmpeg)
    seconds = max(1, int(math.ceil(duration - 1e-9)))

    # Old Xiaowan ffmpeg is unreliable with Unicode paths; use ASCII temp names.
    temp_audio = work_dir / f"in{audio_path.suffix.lower()}"
    temp_video = work_dir / "SinglePictureVideo.mp4"
    temp_out = work_dir / "out.flv"
    for item in (temp_audio, temp_video, temp_out):
        if item.exists():
            item.unlink()

    shutil.copy2(audio_path, temp_audio)

    video_cmd = [
        str(ffmpeg),
        "-loop", "1",
        "-r", str(fps),
        "-t", str(seconds),
        "-f", "image2",
        "-i", str(cover_jpg),
        "-c:v", "libx264",
        "-crf", str(crf),
        "-y", str(temp_video),
    ]
    proc = run_ffmpeg(video_cmd, cwd=work_dir)
    if proc.returncode != 0 or not temp_video.exists():
        raise RuntimeError((proc.stderr or proc.stdout or "生成静帧视频失败").strip())

    mux_cmd = [
        str(ffmpeg),
        "-i", str(temp_video),
        "-i", str(temp_audio),
        "-c:v", "copy",
        "-c:a", "copy",
        "-y", str(temp_out),
    ]
    proc = run_ffmpeg(mux_cmd, cwd=work_dir)
    if proc.returncode != 0 or not temp_out.exists():
        raise RuntimeError((proc.stderr or proc.stdout or "封装 FLV 失败").strip())

    if output.exists():
        output.unlink()
    shutil.move(str(temp_out), str(output))

    for item in (temp_audio, temp_video):
        if item.exists():
            item.unlink()
    return "ok"


_BaseApp = TkinterDnD.Tk if _TKDND else tk.Tk


class App(_BaseApp):
    def __init__(self) -> None:
        super().__init__()
        self.title("小丸一图流 · 批量调用")
        self.geometry("820x620")
        self.minsize(720, 520)

        self.cover_var = tk.StringVar()
        self.cover_dir = ""
        self.audio_dir = ""
        self.xiaowan_var = tk.StringVar()
        self.ffmpeg_info_var = tk.StringVar()
        self.prefix_var = tk.StringVar(value="2_")
        self.skip_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="就绪")
        self.hint_var = tk.StringVar()
        self.audios: list[str] = []
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.ui_queue: queue.Queue[tuple] = queue.Queue()
        self.fps, self.crf = 1, "24"

        self._build()
        self._restore()
        self._drop_hook = None
        self._enable_file_drop()
        self.after(120, self._pump_queues)

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 6}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, **pad)

        xiaowan_row = ttk.Frame(frm)
        xiaowan_row.pack(fill="x")
        ttk.Label(xiaowan_row, text="小丸目录").pack(side="left")
        xiaowan_entry = ttk.Entry(xiaowan_row, textvariable=self.xiaowan_var)
        xiaowan_entry.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(xiaowan_row, text="选择…", command=self.choose_xiaowan).pack(side="left")
        xiaowan_entry.bind("<FocusOut>", lambda _e: self._apply_xiaowan_dir(self.xiaowan_var.get()))
        xiaowan_entry.bind("<Return>", lambda _e: self._apply_xiaowan_dir(self.xiaowan_var.get()))

        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="封面图").pack(side="left")
        ttk.Entry(row, textvariable=self.cover_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="选择…", command=self.choose_cover).pack(side="left")
        ttk.Label(frm, text="可把封面图、音频文件或文件夹直接拖进窗口").pack(anchor="w", pady=(4, 0))

        prefix_row = ttk.Frame(frm)
        prefix_row.pack(fill="x", pady=(8, 0))
        ttk.Label(prefix_row, text="文件名前缀").pack(side="left")
        prefix_entry = ttk.Entry(prefix_row, textvariable=self.prefix_var, width=12)
        prefix_entry.pack(side="left", padx=8)
        ttk.Label(prefix_row, textvariable=self.hint_var).pack(side="left")
        self.prefix_var.trace_add("write", lambda *_: self._update_hint())

        ttk.Label(frm, textvariable=self.ffmpeg_info_var).pack(anchor="w", pady=(4, 8))

        mid = ttk.Frame(frm)
        mid.pack(fill="both", expand=True)
        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="音频列表（可拖入）").pack(anchor="w")
        self.listbox = tk.Listbox(left, selectmode="extended")
        scroll = ttk.Scrollbar(left, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        btns = ttk.Frame(mid)
        btns.pack(side="right", fill="y", padx=(10, 0))
        ttk.Button(btns, text="添加文件", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="添加文件夹", command=self.add_folder).pack(fill="x", pady=2)
        ttk.Button(btns, text="移除选中", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="清空列表", command=self.clear_list).pack(fill="x", pady=2)

        opts = ttk.Frame(frm)
        opts.pack(fill="x", pady=8)
        ttk.Checkbutton(opts, text="已有同名 flv 则跳过", variable=self.skip_var).pack(side="left")
        self.progress = ttk.Progressbar(opts, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        ttk.Label(opts, textvariable=self.status_var, width=18).pack(side="left")

        actions = ttk.Frame(frm)
        actions.pack(fill="x")
        self.start_btn = ttk.Button(actions, text="开始批量生成", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)

        ttk.Label(frm, text="日志").pack(anchor="w", pady=(8, 0))
        self.log = tk.Text(frm, height=12, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

    def _restore(self) -> None:
        state = load_state()
        cover = state.get("cover")
        if cover and Path(cover).exists():
            self.cover_var.set(cover)
        if "prefix" in state:
            self.prefix_var.set(state.get("prefix") or "")
        self.cover_dir = existing_dir(state.get("cover_dir")) or existing_dir(cover)
        self.audio_dir = existing_dir(state.get("audio_dir"))
        found = detect_xiaowan_dir(state.get("xiaowan_dir"))
        if found:
            self.xiaowan_var.set(found)
        elif state.get("xiaowan_dir"):
            self.xiaowan_var.set(state.get("xiaowan_dir") or "")
        self._refresh_xiaowan_info()
        self._update_hint()

    def _update_hint(self) -> None:
        prefix = sanitize_prefix(self.prefix_var.get())
        example = f"{prefix}01.曲名.flv" if prefix else "01.曲名.flv"
        self.hint_var.set(f"示例：{example}")

    def _persist(self, **extra) -> None:
        data = {
            "cover": self.cover_var.get().strip(),
            "prefix": self.prefix_var.get(),
            "cover_dir": self.cover_dir,
            "audio_dir": self.audio_dir,
            "xiaowan_dir": self.xiaowan_var.get().strip(),
        }
        data.update(extra)
        save_state(data)

    def _remember_cover_dir(self, path: str) -> None:
        folder = existing_dir(path)
        if folder:
            self.cover_dir = folder

    def _remember_audio_dir(self, paths: list[str]) -> None:
        for raw in paths:
            folder = existing_dir(raw)
            if folder:
                self.audio_dir = folder
                return

    def _ffmpeg(self) -> Path:
        return xiaowan_ffmpeg(self.xiaowan_var.get().strip())

    def _refresh_xiaowan_info(self) -> None:
        folder = self.xiaowan_var.get().strip()
        ffmpeg = xiaowan_ffmpeg(folder) if folder else Path()
        if folder and ffmpeg.is_file():
            self.fps, self.crf = load_xiaowan_onepic_settings(folder)
            self.ffmpeg_info_var.set(
                f"调用小丸自带 ffmpeg：{ffmpeg}    参数：{self.fps} fps / CRF {self.crf} / 复制音频 / 输出 .flv"
            )
        else:
            self.fps, self.crf = 1, "24"
            self.ffmpeg_info_var.set(
                "未找到小丸 ffmpeg，请选择小丸工具箱安装目录（目录内应有 tools\\ffmpeg.exe）"
            )

    def _apply_xiaowan_dir(self, path: str, persist: bool = True) -> bool:
        resolved = resolve_xiaowan_dir(path.strip())
        if resolved:
            self.xiaowan_var.set(resolved)
            self._refresh_xiaowan_info()
            if persist:
                self._persist()
            return True
        self._refresh_xiaowan_info()
        return False

    def choose_xiaowan(self) -> None:
        folder = filedialog.askdirectory(
            title="选择小丸工具箱目录",
            **dialog_initialdir(self.xiaowan_var.get()),
        )
        if not folder:
            return
        if not self._apply_xiaowan_dir(folder):
            messagebox.showwarning(
                "目录无效",
                "未在该目录找到 tools\\ffmpeg.exe。\n请选择小丸工具箱的安装目录。",
            )

    def choose_cover(self) -> None:
        path = filedialog.askopenfilename(
            title="选择封面图",
            filetypes=[
                ("图片", "*.jpg;*.jpeg;*.png;*.bmp;*.webp"),
                ("全部", "*.*"),
            ],
            **dialog_initialdir(self.cover_dir),
        )
        if path:
            self.cover_var.set(path)
            self._remember_cover_dir(path)
            self._persist()

    def _enable_file_drop(self) -> None:
        if _TKDND:
            self._register_tkdnd(self)
            return
        self._drop_hook = hook_windows_file_drop(self, self._on_drop)

    def _register_tkdnd(self, widget) -> None:
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_tkdnd)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._register_tkdnd(child)

    def _on_tkdnd(self, event) -> str:
        data = getattr(event, "data", "") or ""
        try:
            paths = [p.strip().strip("{}") for p in self.tk.splitlist(data) if p]
        except tk.TclError:
            paths = [data.strip().strip("{}")] if data.strip() else []
        self._on_drop(paths)
        return "copy"

    def _on_drop(self, paths: list[str]) -> None:
        if not paths:
            return
        cleaned = [normalize_drop_path(p) for p in paths]
        cleaned = [p for p in cleaned if p]
        images = [
            p for p in cleaned
            if Path(p).is_file() and Path(p).suffix.lower() in IMAGE_EXTS
        ]
        remaining = [p for p in cleaned if p not in images]
        if images:
            cover = images[0]
            self.cover_var.set(cover)
            self._remember_cover_dir(cover)
            self._persist()
            self._log(f"已设置封面：{Path(cover).name}")
            if len(images) > 1:
                self._log(f"拖入了 {len(images)} 张图片，只用了第一张")
        audios = collect_audio_files(remaining)
        if audios:
            self._remember_audio_dir(remaining or audios)
            self._append_audios(audios)
            self._persist()
        elif not images:
            self._log("拖入的内容里没有可用的图片或音频")

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择音频",
            filetypes=[
                ("音频", "*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.wma *.ape"),
                ("全部", "*.*"),
            ],
            **dialog_initialdir(self.audio_dir),
        )
        if paths:
            self._remember_audio_dir(list(paths))
            self._persist()
        self._append_audios(paths)

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(
            title="选择音频文件夹",
            **dialog_initialdir(self.audio_dir),
        )
        if not folder:
            return
        self._remember_audio_dir([folder])
        self._persist()
        root = Path(folder)
        found = [
            str(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS
        ]
        self._append_audios(found)

    def _append_audios(self, paths) -> None:
        existing = set(self.audios)
        added = 0
        for path in paths:
            if path and path not in existing:
                self.audios.append(path)
                self.listbox.insert("end", path)
                existing.add(path)
                added += 1
        self.status_var.set(f"共 {len(self.audios)} 个音频")
        if added:
            self._log(f"已添加 {added} 个音频")

    def remove_selected(self) -> None:
        indexes = list(self.listbox.curselection())
        for i in reversed(indexes):
            self.listbox.delete(i)
            del self.audios[i]
        self.status_var.set(f"共 {len(self.audios)} 个音频")

    def clear_list(self) -> None:
        self.audios.clear()
        self.listbox.delete(0, "end")
        self.status_var.set("就绪")

    def _log(self, text: str) -> None:
        self.log_queue.put(text)

    def _ui(self, name: str, *args) -> None:
        self.ui_queue.put((name, args))

    def _pump_queues(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        while True:
            try:
                name, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if name == "status":
                self.status_var.set(args[0])
            elif name == "progress":
                self.progress.configure(value=args[0])
            elif name == "done":
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
            elif name == "xiaowan_info":
                self._refresh_xiaowan_info()
        self.after(120, self._pump_queues)

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        cover = self.cover_var.get().strip()
        if not cover or not Path(cover).exists():
            messagebox.showwarning("缺少封面", "请先选择一张封面图。")
            return
        if not self.audios:
            messagebox.showwarning("缺少音频", "请先添加要处理的音频。")
            return
        ffmpeg = self._ffmpeg()
        if not ffmpeg.is_file():
            messagebox.showerror(
                "找不到小丸工具",
                "未找到 tools\\ffmpeg.exe。\n请先在顶部选择小丸工具箱的安装目录。",
            )
            return

        prefix = sanitize_prefix(self.prefix_var.get())
        xiaowan_dir = self.xiaowan_var.get().strip()
        self._persist(cover=cover, prefix=self.prefix_var.get())
        self.stop_flag.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(maximum=len(self.audios), value=0)
        self.worker = threading.Thread(
            target=self._run_batch,
            args=(cover, prefix, ffmpeg, xiaowan_dir),
            daemon=True,
        )
        self.worker.start()

    def stop(self) -> None:
        self.stop_flag.set()
        self.status_var.set("正在停止…")

    def _run_batch(self, cover: str, prefix: str, ffmpeg: Path, xiaowan_dir: str) -> None:
        ok = skip = fail = 0
        work_dir = Path(tempfile.mkdtemp(prefix="xiaowan_onepic_"))
        cover_jpg = work_dir / "marukotemp.jpg"
        try:
            self.fps, self.crf = load_xiaowan_onepic_settings(xiaowan_dir)
            self._ui("xiaowan_info")
            self._log(f"封面：{cover}")
            self._log(f"前缀：{prefix or '（无）'}")
            self._log(f"小丸目录：{xiaowan_dir}")
            self._log(f"读取小丸设置：fps={self.fps}, crf={self.crf}")
            prepare_cover_jpg(Path(cover), cover_jpg, ffmpeg)
            total = len(self.audios)
            for index, audio in enumerate(self.audios, start=1):
                if self.stop_flag.is_set():
                    self._log("已停止。")
                    break
                name = Path(audio).name
                self._ui("status", f"{index}/{total}  {name}")
                try:
                    result = export_one(
                        Path(audio),
                        cover_jpg,
                        self.fps,
                        self.crf,
                        work_dir,
                        self.skip_var.get(),
                        prefix,
                        ffmpeg,
                    )
                    if result == "skip":
                        skip += 1
                        self._log(f"[跳过] {name}")
                    else:
                        ok += 1
                        self._log(f"[完成] {output_path(Path(audio), prefix).name}")
                except Exception as exc:
                    fail += 1
                    self._log(f"[失败] {name}  {exc}")
                self._ui("progress", index)
        except Exception as exc:
            self._log(f"[错误] {exc}")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            self._ui("status", f"完成 成功{ok} / 跳过{skip} / 失败{fail}")
            self._log(f"结束：成功 {ok}，跳过 {skip}，失败 {fail}")
            self._ui("done")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    main()
