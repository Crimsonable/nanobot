#!/usr/bin/env python3
"""Compress a local raster image for vision-tool ingestion.

The script intentionally avoids heavy image stacks such as OpenCV and NumPy.
It requires Pillow for decoding and encoding raster formats.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - exercised only when dependency is absent
    sys.stderr.write(
        "Missing dependency: Pillow. Install it with: python3 -m pip install Pillow\n"
    )
    raise SystemExit(2)


FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def quality_value(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 95:
        raise argparse.ArgumentTypeError("must be between 1 and 95")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a smaller image derivative for vision service calls."
    )
    parser.add_argument("input", type=Path, help="source image path")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="output image path; default is <input-stem>.compressed.jpg",
    )
    parser.add_argument("--max-edge", type=positive_int, default=2000)
    parser.add_argument("--max-bytes", type=positive_int, default=4_000_000)
    parser.add_argument("--quality", type=quality_value, default=85)
    parser.add_argument("--min-quality", type=quality_value, default=55)
    parser.add_argument(
        "--format",
        choices=("auto", "jpeg", "png", "webp", "keep"),
        default="auto",
        help="output format; auto favors JPEG for compression",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="recompress even when the input already satisfies limits",
    )
    return parser.parse_args()


def output_format(input_path: Path, output_path: Path, requested: str) -> str:
    if requested == "jpeg" or requested == "auto":
        return "JPEG"
    if requested == "png":
        return "PNG"
    if requested == "webp":
        return "WEBP"
    if requested == "keep":
        suffix = input_path.suffix.lower()
    else:
        suffix = output_path.suffix.lower()

    if suffix in (".jpg", ".jpeg"):
        return "JPEG"
    if suffix == ".png":
        return "PNG"
    if suffix == ".webp":
        return "WEBP"
    return "JPEG"


def default_output_path(input_path: Path, fmt: str) -> Path:
    return input_path.with_name(
        f"{input_path.stem}.compressed{FORMAT_EXTENSIONS.get(fmt, '.jpg')}"
    )


def flatten_for_jpeg(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def fit_to_edge(image: Image.Image, max_edge: int) -> Image.Image:
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        return image.copy()
    scale = max_edge / longest
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def save_image(image: Image.Image, path: Path, fmt: str, quality: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs: dict[str, object] = {"format": fmt}
    if fmt == "JPEG":
        image = flatten_for_jpeg(image)
        save_kwargs.update(quality=quality, optimize=True, progressive=True)
    elif fmt == "WEBP":
        save_kwargs.update(quality=quality, method=6)
    elif fmt == "PNG":
        save_kwargs.update(optimize=True, compress_level=9)
    image.save(path, **save_kwargs)


def fits_limits(path: Path, image: Image.Image, max_edge: int, max_bytes: int) -> bool:
    return (
        path.stat().st_size <= max_bytes
        and max(image.size) <= max_edge
    )


def compress(args: argparse.Namespace) -> Path:
    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise SystemExit(f"Input image does not exist: {input_path}")
    if args.min_quality > args.quality:
        raise SystemExit("--min-quality cannot be greater than --quality")

    with Image.open(input_path) as opened:
        image = ImageOps.exif_transpose(opened)
        fmt = output_format(input_path, args.output or input_path, args.format)
        output_path = (
            args.output.expanduser().resolve()
            if args.output
            else default_output_path(input_path, fmt).resolve()
        )

        if not args.force and fits_limits(input_path, image, args.max_edge, args.max_bytes):
            if output_path != input_path:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(input_path, output_path)
            return output_path

        working = fit_to_edge(image, args.max_edge)
        quality = args.quality
        while True:
            save_image(working, output_path, fmt, quality)
            if output_path.stat().st_size <= args.max_bytes:
                return output_path
            if fmt not in ("JPEG", "WEBP"):
                return output_path
            if quality > args.min_quality:
                quality = max(args.min_quality, quality - 8)
                continue
            width, height = working.size
            if max(width, height) <= 512:
                return output_path
            working = working.resize(
                (max(1, round(width * 0.85)), max(1, round(height * 0.85))),
                Image.Resampling.LANCZOS,
            )
            quality = args.quality


def main() -> int:
    args = parse_args()
    output_path = compress(args)
    size = output_path.stat().st_size
    with Image.open(output_path) as result:
        width, height = result.size
    print(f"{output_path}")
    print(f"{width}x{height} {size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
