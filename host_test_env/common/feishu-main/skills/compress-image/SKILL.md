---
name: compress-image
description: Compress large local image files before passing them to MCP vision/image-understanding tools, especially when high resolution, large byte size, PNG screenshots, camera photos, or scanned images may cause upload slowness or vision service timeouts. Use for JPEG, PNG, WebP, BMP, TIFF, and other Pillow-readable raster images when a smaller derivative is needed for analysis.
---

# Compress Image

Use this skill when an image is too large for reliable vision processing or when the MCP vision service times out on an image input. Create a smaller derivative in the current workspace, then send that derivative to the vision tool instead of the original.

## Workflow

1. Keep the original image unchanged.
2. Choose an output path in the current workspace, not inside this skill directory.
3. Run `scripts/compress_image.py` with a practical long-edge and byte target.
4. Confirm the output exists and is non-empty.
5. Use the compressed output for MCP vision calls.

## Command

```bash
python3 nanobot/skills/compress-image/scripts/compress_image.py \
  /abs/path/input.png \
  /abs/path/input.compressed.jpg
```

For aggressive compression before a vision call:

```bash
python3 nanobot/skills/compress-image/scripts/compress_image.py \
  /abs/path/input.png \
  /abs/workspace/input.vision.jpg \
  --max-edge 1600 \
  --max-bytes 3000000 \
  --quality 82
```

## Options

- `input`: source image path.
- `output`: optional output image path. If omitted, the script writes `<input-stem>.compressed.jpg` beside the input.
- `--max-edge <px>`: resize so the longest side is at most this many pixels. Default: `2000`.
- `--max-bytes <bytes>`: try to keep the output below this byte size by lowering quality and then dimensions. Default: `4000000`.
- `--quality <1-95>`: starting JPEG/WebP quality. Default: `85`.
- `--min-quality <1-95>`: lowest JPEG/WebP quality to try before downscaling further. Default: `55`.
- `--format <auto|jpeg|png|webp|keep>`: output format. Default: `auto`, which favors JPEG for strong compression.
- `--force`: recompress even when the input already fits the target size and dimensions.

## Rules

- Use absolute paths when possible.
- Prefer JPEG for screenshots, photos, and scanned documents sent to vision tools; the script flattens transparency onto white when writing JPEG.
- Use `--format png` only when transparency or exact pixel colors matter more than byte size.
- Do not install OpenCV, NumPy, or ImageMagick for this skill. The script intentionally uses only Python plus Pillow.
- If Pillow is missing, install only the lightweight dependency in the active environment: `python3 -m pip install Pillow`.
- Never write compressed images into the skill installation directory; write them to the active project or workspace.
