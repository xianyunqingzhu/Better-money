"""Upload validation: streaming size limits, image signatures, statement allowlists."""
from __future__ import annotations

from fastapi import UploadFile

IMAGE_MAX_BYTES = 10 * 1024 * 1024          # 10 MiB per image
STATEMENT_MAX_BYTES = 20 * 1024 * 1024      # 20 MiB per CSV/XLSX/XLSM
MAX_IMAGES_PER_REQUEST = 10
CHUNK_SIZE = 64 * 1024

IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "webp")
STATEMENT_EXTENSIONS = ("csv", "xlsx", "xlsm")

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_WEBP_SIGNATURE = b"WEBP"
_ZIP_SIGNATURE = b"PK"


class UploadError(ValueError):
    """User-facing upload rejection with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in bounded chunks; raise on empty or oversized files."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            limit_mib = max_bytes // (1024 * 1024)
            raise UploadError(
                "file_too_large", f"文件超过 {limit_mib} MiB 上限，无法上传")
        chunks.append(chunk)
    if total == 0:
        raise UploadError("empty_file", "文件为空，无法上传")
    return b"".join(chunks)


def _detect_image_ext(data: bytes) -> str | None:
    if data.startswith(_PNG_SIGNATURE):
        return "png"
    if data.startswith(_JPEG_SIGNATURE):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == _WEBP_SIGNATURE:
        return "webp"
    return None


def validate_image(data: bytes, filename: str = "") -> str:
    """Return the canonical extension for verified image bytes."""
    ext = _detect_image_ext(data)
    if not ext:
        raise UploadError(
            "unsupported_file_type", "只支持 PNG / JPG / WEBP 图片")
    return ext


def validate_statement(data: bytes, filename: str = "") -> str:
    """Validate a bill file by extension and content signature."""
    name = (filename or "").lower()
    if name.endswith(".xls"):
        raise UploadError(
            "unsupported_file_type", "旧版 .xls 不支持，请导出为 .csv 或 .xlsx。")
    if name.endswith((".xlsx", ".xlsm")):
        if not data.startswith(_ZIP_SIGNATURE):
            raise UploadError(
                "unsupported_file_type", "文件不是有效的 Excel 工作簿")
        return name.rsplit(".", 1)[-1]
    if name.endswith(".csv"):
        for encoding in ("utf-8-sig", "gbk", "utf-8"):
            try:
                data.decode(encoding)
                return "csv"
            except UnicodeDecodeError:
                continue
        raise UploadError(
            "unsupported_file_type", "无法识别文件编码，请导出为 CSV 后重试。")
    raise UploadError(
        "unsupported_file_type", "仅支持 .csv / .xlsx / .xlsm 账单文件")
