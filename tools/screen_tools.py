"""
螢幕截圖工具

截圖路徑直接以回傳值交給呼叫端 (agent.py 會把它加入 Discord 附件)，
不使用全域變數暫存，避免多位使用者同時截圖時拿錯檔案。
"""

import os
from datetime import datetime

# Discord 一般伺服器附件上限是 10MB，保留餘裕抓 8MB 門檻再轉檔，避免邊界誤差上傳失敗
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
# screenshots/ 從不清理會無限增長，只保留最近 N 張，其餘自動刪除
MAX_KEPT_SCREENSHOTS = 30


def _cleanup_old_screenshots(output_dir: str, keep: int = MAX_KEPT_SCREENSHOTS) -> None:
    """只保留最近 keep 張截圖 (依修改時間排序)，避免 screenshots/ 資料夾無限增長"""
    try:
        files = sorted(
            (
                os.path.join(output_dir, name)
                for name in os.listdir(output_dir)
                if name.startswith("screenshot_")
            ),
            key=os.path.getmtime,
        )
        for old_path in files[:-keep]:
            try:
                os.remove(old_path)
            except OSError:
                pass
    except OSError:
        pass  # 目錄不存在或無法列出時直接略過，不影響本次截圖


def _shrink_to_upload_limit(filepath: str) -> str:
    """
    若 PNG 超過 Discord 附件上限，改轉存為壓縮/縮小的 JPEG 以確保能傳送。
    回傳最終實際儲存的檔案路徑 (未超過上限時原樣回傳 PNG 路徑)。
    """
    if os.path.getsize(filepath) <= MAX_UPLOAD_BYTES:
        return filepath

    from PIL import Image

    jpeg_path = os.path.splitext(filepath)[0] + ".jpg"
    with Image.open(filepath) as img:
        rgb_img = img.convert("RGB")

    quality = 85
    scale = 1.0
    while True:
        target = rgb_img
        if scale < 1.0:
            target = rgb_img.resize((max(int(rgb_img.width * scale), 1), max(int(rgb_img.height * scale), 1)))
        target.save(jpeg_path, "JPEG", quality=quality, optimize=True)

        if os.path.getsize(jpeg_path) <= MAX_UPLOAD_BYTES or (quality <= 40 and scale <= 0.4):
            break
        if quality > 40:
            quality -= 15
        else:
            scale *= 0.8

    os.remove(filepath)
    return jpeg_path


def take_screenshot(output_dir: str = "screenshots") -> str:
    """
    擷取當前螢幕畫面並儲存為圖片，回傳圖片的絕對路徑。
    若電腦處於鎖定/Session 0 無 GUI 狀態，會拋出明確提示。
    若原始 PNG 超過 Discord 附件上限 (多螢幕常見)，會自動壓縮/縮小轉成 JPEG 再回傳。
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    filepath = os.path.abspath(os.path.join(output_dir, filename))

    # 優先嘗試 PIL ImageGrab
    try:
        from PIL import ImageGrab
        try:
            img = ImageGrab.grab(all_screens=True)
        except Exception:
            img = ImageGrab.grab()
        img.save(filepath, "PNG")
    except Exception:
        # 次選嘗試 mss
        try:
            import mss
            with mss.MSS() as sct:
                sct.shot(output=filepath)
        except Exception as e_mss:
            raise RuntimeError(
                f"無法截取螢幕畫面 (可能原因: 電腦螢幕已鎖定、處於休眠或背景服務無 GUI 權限)。錯誤: {e_mss}"
            )

    _cleanup_old_screenshots(output_dir)
    return _shrink_to_upload_limit(filepath)
