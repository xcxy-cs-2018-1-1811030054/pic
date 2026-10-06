# -*- coding: utf-8 -*-
"""视频抽帧模块：OpenCV 均匀抽取关键帧，输出 JPEG 字节流供搜索"""
from __future__ import annotations

import os
import shutil
import tempfile

import cv2
import numpy as np
from typing import List, Tuple

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm",
              ".flv", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp",
              ".gif", ".tif", ".tiff"}

MAX_SIDE = 1600      # 上传前限制图片最长边，加快上传
JPEG_QUALITY = 90


def imread_unicode(path: str):
    """支持中文路径的图片读取（Windows 下 cv2.imread 不认中文路径）"""
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _open_capture(path: str):
    """打开视频；中文路径打不开时拷贝到临时文件再打开"""
    cap = cv2.VideoCapture(path)
    if cap.isOpened():
        return cap, None
    cap.release()
    if any(ord(c) > 127 for c in path):
        tmp = os.path.join(tempfile.mkdtemp(prefix="imagehunter_v_"),
                           "video" + os.path.splitext(path)[1])
        shutil.copyfile(path, tmp)
        cap = cv2.VideoCapture(tmp)
        if cap.isOpened():
            return cap, tmp
        cap.release()
    raise ValueError("无法打开视频文件（格式不支持或文件损坏）")


def probe_video(path: str) -> dict:
    cap, tmp = _open_capture(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return {"fps": fps, "frames": n,
                "duration": (n / fps) if fps else 0.0, "w": w, "h": h}
    finally:
        cap.release()
        _cleanup_tmp(tmp)


def _cleanup_tmp(tmp):
    if tmp:
        try:
            shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def extract_keyframes(path: str, max_frames: int = 6) -> List[Tuple[int, float, np.ndarray]]:
    """
    均匀抽取关键帧（跳过首尾各 2%，避免黑屏片头片尾）
    返回 [(frame_index, timestamp_sec, bgr_ndarray), ...]
    """
    info = probe_video(path)
    n = info["frames"]
    if n <= 0:
        n = int(info["duration"] * info["fps"]) or 100
    max_frames = max(1, min(int(max_frames), 12))
    start = int(n * 0.02)
    end = max(start + 1, int(n * 0.98))
    step = max((end - start) / max_frames, 1)
    idxs = sorted({min(int(start + i * step), n - 1) for i in range(max_frames)})

    cap, tmp = _open_capture(path)
    out = []
    try:
        for i in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, fr = cap.read()
            if ok and fr is not None:
                out.append((i, i / info["fps"], fr))
    finally:
        cap.release()
        _cleanup_tmp(tmp)
    if not out:
        raise ValueError("抽帧失败：未能从视频中读取任何帧")
    return out


def to_jpeg_bytes(bgr, max_side: int = MAX_SIDE,
                  quality: int = JPEG_QUALITY) -> bytes:
    """BGR 图像 -> 缩放 -> JPEG 字节流"""
    h, w = bgr.shape[:2]
    scale = max_side / float(max(h, w))
    if scale < 1.0:
        bgr = cv2.resize(bgr, (max(int(w * scale), 1), max(int(h * scale), 1)),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("JPEG 编码失败")
    return buf.tobytes()


def load_image_as_jpeg(path: str) -> bytes:
    """读取图片文件并统一转成 JPEG 字节流"""
    img = imread_unicode(path)
    if img is None:
        raise ValueError("无法读取图片（格式不支持或文件损坏）")
    return to_jpeg_bytes(img)
