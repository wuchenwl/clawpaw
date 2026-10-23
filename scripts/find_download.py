"""Find recent image downloads without scanning unrelated temporary directories."""

import argparse
import filecmp
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError

FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--since must include a timezone, such as +08:00 or Z")
    return parsed.timestamp()


def image_info(path):
    path = Path(path).resolve()
    stat = path.stat()
    if not path.is_file() or not stat.st_size:
        raise ValueError("Source must be a nonempty image file")
    with Image.open(path) as image:
        fmt, size = image.format, image.size
        if fmt not in FORMATS:
            raise ValueError("Only PNG, JPEG and WebP images are supported")
        image.verify()
    # verify() alone does not decode every format; reject incomplete downloads as well.
    with Image.open(path) as image:
        image.load()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "format": fmt,
        "width": size[0],
        "height": size[1],
    }


def recent_images(since, temp_dir, downloads_dir, limit=10):
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    roots = [Path(downloads_dir)]
    roots.extend(p for p in Path(temp_dir).glob("playwright-artifacts-*") if p.is_dir())
    recent = []
    unreadable_roots = 0
    for root in roots:
        try:
            for path in root.iterdir():
                try:
                    if path.is_file() and not path.is_symlink():
                        stat = path.stat()
                        if stat.st_mtime >= since and stat.st_size:
                            recent.append((stat.st_mtime, path))
                except OSError:
                    continue
        except FileNotFoundError:
            continue
        except OSError:
            unreadable_roots += 1
    candidates = []
    for _, path in sorted(recent, key=lambda entry: entry[0], reverse=True):
        try:
            info = image_info(path)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            continue
        candidates.append(info)
        if len(candidates) > limit:
            break
    return {
        "candidates": candidates[:limit],
        "has_more": len(candidates) > limit,
        "unreadable_roots": unreadable_roots,
    }


def save_image(source, output):
    info = image_info(source)
    source, output = Path(info["path"]), Path(output).resolve()
    suffix = output.suffix.lower()
    valid = {FORMATS[info["format"]]}
    if info["format"] == "JPEG":
        valid.add(".jpeg")
    if suffix not in valid:
        raise ValueError(f"Output extension must match {info['format']}: {sorted(valid)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    reused = False
    try:
        destination = output.open("xb")
    except FileExistsError:
        if not filecmp.cmp(source, output, shallow=False):
            raise ValueError(
                "Output exists with different content; choose another filename"
            ) from None
        reused = True
    else:
        try:
            with destination, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, destination)
            image_info(output)
        except Exception:
            output.unlink(missing_ok=True)
            raise
    return {"saved": image_info(output), "reused": reused}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--since", help="Download click start time, ISO 8601 with timezone")
    mode.add_argument("--source", help="Explicitly selected image path to save")
    parser.add_argument("--output", help="Stable destination; required with --source")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--temp-dir", default=tempfile.gettempdir())
    parser.add_argument("--downloads-dir", default=str(Path.home() / "Downloads"))
    args = parser.parse_args(argv)
    try:
        if args.source:
            if not args.output:
                raise ValueError("--source requires --output")
            result = save_image(args.source, args.output)
        else:
            if args.output:
                raise ValueError("--output requires --source")
            result = recent_images(
                timestamp(args.since), args.temp_dir, args.downloads_dir, args.limit
            )
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
