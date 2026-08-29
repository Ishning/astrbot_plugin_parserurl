"""Pixiv 限制级静态作品的本地媒体处理。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4
import zipfile

from PIL import Image, ImageFilter

from ...exception import SizeLimitException


def _blur_radius(strength: int) -> float:
    """将后台 30–100 的模糊程度线性映射到 Pillow 模糊半径。"""
    clamped = max(30, min(100, strength))
    return 15 + (clamped - 30) * 35 / 70


def _blur_cover(source: Path, output: Path, blur_strength: int) -> Path:
    """生成模糊封面，始终转换为 JPEG 以避免 PNG/RGBA 保存问题。"""
    with Image.open(source) as image:
        image.convert("RGB").filter(
            ImageFilter.GaussianBlur(radius=_blur_radius(blur_strength))
        ).save(
            output, "JPEG", quality=88
        )
    return output


def _build_pdf(sources: list[Path], output: Path) -> Path:
    """将正文页合成为单个 PDF；调用方保证至少传入一页。"""
    images: list[Image.Image] = []
    try:
        for source in sources:
            with Image.open(source) as image:
                images.append(image.convert("RGB"))
        images[0].save(output, "PDF", save_all=True, append_images=images[1:])
    finally:
        for image in images:
            image.close()
    return output


async def create_blurred_cover(
    source: Path, output_dir: Path, blur_strength: int = 70
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"pixiv_r18_cover_{uuid4().hex}.jpg"
    # Pillow 的 PDF/JPEG 编码在当前 AstrBot 运行环境中不能可靠地放入线程池，
    # 因此保持在当前协程中执行；网络下载仍由 Downloader 异步完成。
    return _blur_cover(source, output, blur_strength)


async def create_body_pdf(
    sources: list[Path], output_dir: Path, max_size_mb: int
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"pixiv_r18_pages_{uuid4().hex}.pdf"
    result = _build_pdf(sources, output)
    if result.stat().st_size > max_size_mb * 1024 * 1024:
        result.unlink(missing_ok=True)
        raise SizeLimitException()
    return result


async def create_ugoira_gif(
    archive: Path, output_dir: Path, frames: list[dict], max_size_mb: int = 5
) -> Path:
    """Convert a Pixiv ugoira ZIP archive to GIF and enforce its size limit."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"pixiv_ugoira_{uuid4().hex}.gif"
    images: list[Image.Image] = []
    try:
        with zipfile.ZipFile(archive) as zipped:
            for frame in frames:
                name = str(frame.get("file") or "")
                if not name:
                    continue
                try:
                    with zipped.open(name) as source:
                        images.append(Image.open(source).convert("RGB"))
                except (KeyError, OSError):
                    continue
        if not images:
            raise ValueError("ugoira ZIP 没有可用帧")
        durations = [int(frame.get("delay") or 100) for frame in frames[: len(images)]]
        images[0].save(output, "GIF", save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=False)
        if output.stat().st_size > max_size_mb * 1024 * 1024:
            output.unlink(missing_ok=True)
            raise SizeLimitException()
        return output
    finally:
        for image in images:
            image.close()
        archive.unlink(missing_ok=True)
