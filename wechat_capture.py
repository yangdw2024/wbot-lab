"""Read the visible WeChat chat pane as OCR snippets, never as original messages."""
from __future__ import annotations

import ctypes
import os
import re
import sys
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from core import DATA_ROOT

ROOT = Path(__file__).resolve().parent
MODEL_ROOT = DATA_ROOT / 'models' / 'PaddleOCR'
RUNTIME = DATA_ROOT / 'runtime'
_engine = None
_engine_lock = threading.Lock()


class CaptureError(RuntimeError):
    pass


def prepare_runtime():
    """Keep inference caches and temporary files inside the project directory."""
    if not RUNTIME.resolve().is_relative_to(DATA_ROOT):
        raise CaptureError('运行目录超出所选数据目录')
    paths = {
        'TMP': RUNTIME / 'tmp', 'TEMP': RUNTIME / 'tmp', 'TMPDIR': RUNTIME / 'tmp',
        'USERPROFILE': RUNTIME / 'profile', 'APPDATA': RUNTIME / 'appdata',
        'LOCALAPPDATA': RUNTIME / 'localappdata', 'MPLCONFIGDIR': RUNTIME / 'matplotlib',
        'XDG_CACHE_HOME': RUNTIME / 'cache', 'PADDLE_PDX_CACHE_HOME': RUNTIME / 'paddlex',
    }
    for key, path in paths.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'


def get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            prepare_runtime()
            for name in ('text_detection', 'text_recognition'):
                for filename in ('inference.json', 'inference.pdiparams', 'inference.yml'):
                    if not (MODEL_ROOT / name / filename).is_file():
                        raise CaptureError('本地 OCR 模型不完整，请检查 models/PaddleOCR')
            try:
                from paddleocr import PaddleOCR
                _engine = PaddleOCR(
                    text_detection_model_name='PP-OCRv5_server_det',
                    text_detection_model_dir=str(MODEL_ROOT / 'text_detection'),
                    text_recognition_model_name='PP-OCRv5_server_rec',
                    text_recognition_model_dir=str(MODEL_ROOT / 'text_recognition'),
                    use_doc_orientation_classify=False, use_doc_unwarping=False,
                    use_textline_orientation=False, device='cpu', enable_mkldnn=False,
                    cpu_threads=4,
                )
            except Exception as exc:
                raise CaptureError('本地 OCR 初始化失败：' + str(exc)) from exc
        return _engine


def _normalize_title(text):
    return ''.join(c.casefold() for c in unicodedata.normalize('NFKC', text) if c.isalnum())


def _ocr_lines(engine, image):
    import numpy as np
    pixels = np.asarray(image.convert('RGB'))[:, :, ::-1].copy()
    out = []
    for result in engine.predict(pixels):
        texts = list(result.get('rec_texts', []))
        scores = list(result.get('rec_scores', []))
        polygons = result.get('rec_polys')
        if polygons is None:
            continue
        for index, raw in enumerate(texts):
            value = str(raw).strip()
            if not value or index >= len(polygons):
                continue
            points = polygons[index]
            xs = [int(p[0]) for p in points]
            ys = [int(p[1]) for p in points]
            score = float(scores[index]) if index < len(scores) else 0.0
            out.append({'text': value, 'score': score,
                        'box': [min(xs), min(ys), max(xs), max(ys)]})
    return out


def extract_visible_snippets(image, expected_group, *, engine=None, main_window=True):
    """Recognize a verified header and the visible chat pane in one in-memory image.

    The OCR rows are deliberately not called messages: images, timestamps and UI
    labels can also contain text, and sender identity is not reliably available.
    """
    expected = _normalize_title(expected_group)
    if len(expected) < 4:
        raise CaptureError('请填写完整群名，至少包含 4 个文字或字母')
    width, height = image.size
    if width < 360 or height < 300:
        raise CaptureError('微信窗口太小，请放大窗口后重试')
    engine = engine or get_engine()
    header_height = min(160, max(70, int(height * 0.22)))
    header = _ocr_lines(engine, image.crop((0, 0, width, header_height)))
    matches = [line for line in header if expected in _normalize_title(line['text'])]
    # The main WeChat window has a session list on the left. A match there alone
    # is not proof that this group is the open conversation.
    if main_window:
        matches = [line for line in matches if line['box'][0] >= width * 0.20]
    if not matches:
        raise CaptureError('没有在当前聊天标题中确认群名；请打开指定群并放大微信窗口')
    title = max(matches, key=lambda line: line['box'][0])
    left = max(0, title['box'][0] - 20)
    top = min(height - 1, title['box'][3] + 12)
    # Exclude the lower input area; this conservative crop can miss some lines.
    bottom = max(0, int(height * 0.77))
    if width - left < 220 or bottom - top < 100:
        raise CaptureError('聊天区域过小，请放大微信窗口后重试')
    pane = image.crop((left, top, width, bottom))
    detected = _ocr_lines(engine, pane)
    detected.sort(key=lambda item: (item['box'][1], item['box'][0]))
    snippets = []
    for row in detected:
        text = row['text'].strip()
        if not text or _normalize_title(text) == expected:
            continue
        snippets.append({'text': text, 'score': round(row['score'], 3)})
    if len(snippets) > 1000:
        raise CaptureError('本次识别文本过多，请缩小可见范围后重试')
    return {'group': expected_group, 'lines': snippets,
            'captured_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'note': '仅识别当前可见区域，可能包含图片中的文字或界面标签；采集时间不是消息发送时间。'}


def _foreground_wechat_rect(expected_group):
    if sys.platform != 'win32':
        raise CaptureError('当前版本只支持 Windows 微信窗口读取')
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    hwnd = user32.GetForegroundWindow()
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        raise CaptureError('请切换到已显示的微信窗口，确保窗口没有最小化')
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = kernel32.OpenProcess(0x1000, False, pid.value)
    if not process:
        raise CaptureError('无法确认前台窗口所属程序，请以相同权限运行微信和本工具')
    try:
        size = wintypes.DWORD(32768)
        image_name = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, image_name, ctypes.byref(size)):
            raise CaptureError('无法确认前台窗口所属程序')
        exe = Path(image_name.value).name.casefold()
    finally:
        kernel32.CloseHandle(process)
    if exe not in ('weixin.exe', 'wechat.exe'):
        raise CaptureError('采集时微信不是前台窗口，请在倒计时内切换到指定群聊')
    title_buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title_buf, len(title_buf))
    title = title_buf.value
    main_window = _normalize_title(expected_group) not in _normalize_title(title)
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise CaptureError('无法获取微信窗口的位置')
    if rect.right - rect.left < 360 or rect.bottom - rect.top < 300:
        raise CaptureError('微信窗口太小，请放大窗口后重试')
    # Reject obviously occluded or off-screen windows. This is a conservative
    # sample, not proof that every pixel is unobscured.
    for fx in (0.35, 0.60, 0.85):
        for fy in (0.15, 0.40, 0.65):
            point = wintypes.POINT(int(rect.left + (rect.right-rect.left)*fx),
                                   int(rect.top + (rect.bottom-rect.top)*fy))
            child = user32.WindowFromPoint(point)
            if not child or user32.GetAncestor(child, 2) != hwnd:
                raise CaptureError('微信窗口可能被遮挡或移出屏幕，请完整显示后重试')
    return (rect.left, rect.top, rect.right, rect.bottom), main_window


def capture_current_window(expected_group, on_phase=None):
    """Capture screen pixels only while WeChat is the foreground application."""
    rect, main_window = _foreground_wechat_rect(expected_group)
    try:
        from PIL import ImageGrab
        image = ImageGrab.grab(bbox=rect, all_screens=True)
    except Exception as exc:
        raise CaptureError('无法读取微信窗口画面：' + str(exc)) from exc
    after_rect, after_main_window = _foreground_wechat_rect(expected_group)
    if after_rect != rect or after_main_window != main_window:
        raise CaptureError('采集期间微信窗口发生变化，请重新读取')
    captured_at = datetime.now().astimezone().isoformat(timespec='seconds')
    if on_phase:
        on_phase('recognizing')
    result = extract_visible_snippets(image, expected_group, main_window=main_window)
    result['captured_at'] = captured_at
    return result
