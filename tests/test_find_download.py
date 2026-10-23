"""Download recovery handles UUID filenames, partial files and stable saves."""

import importlib.util
import os
from pathlib import Path

import pytest
from PIL import Image

spec = importlib.util.spec_from_file_location(
    "find_download", Path(__file__).parents[1] / "scripts" / "find_download.py"
)
download = importlib.util.module_from_spec(spec)
spec.loader.exec_module(download)


def make_image(path, modified, color="orange", fmt="PNG"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 12), color).save(path, format=fmt)
    os.utime(path, (modified, modified))


def test_uuid_images_recent_only_no_unrelated_temp_recursion(tmp_path):
    temp, downloads = tmp_path / "temp", tmp_path / "Downloads"
    wanted = temp / "playwright-artifacts-test" / "uuid-without-extension"
    make_image(wanted, 200)
    make_image(downloads / "old.png", 50)
    make_image(temp / "unrelated" / "new.png", 300)
    partial = wanted.parent / "partial"
    partial.write_bytes(wanted.read_bytes()[:30])
    os.utime(partial, (300, 300))
    result = download.recent_images(100, temp, downloads)
    assert [entry["path"] for entry in result["candidates"]] == [str(wanted.resolve())]
    assert result["candidates"][0]["format"] == "PNG"
    assert result["candidates"][0]["width"] == 8
    assert not result["has_more"]


def test_multiple_candidates_bounded_without_automatically_choosing(tmp_path):
    for index in range(4):
        make_image(tmp_path / "Downloads" / f"{index}.png", 200 + index)
    result = download.recent_images(100, tmp_path / "temp", tmp_path / "Downloads", limit=2)
    assert len(result["candidates"]) == 2
    assert result["has_more"]
    assert result["candidates"][0]["path"].endswith("3.png")
    assert not (tmp_path / "temp").exists()


def test_save_uuid_preserves_bytes_reuses_and_refuses_collision(tmp_path):
    source, output = tmp_path / "uuid", tmp_path / "captures" / "海报.png"
    make_image(source, 200)
    first = download.save_image(source, output)
    assert first["saved"]["path"] == str(output.resolve())
    assert not first["reused"]
    assert source.read_bytes() == output.read_bytes()
    assert download.save_image(source, output)["reused"]
    make_image(source, 300, "blue")
    with pytest.raises(ValueError, match="different content"):
        download.save_image(source, output)
    assert output.read_bytes() != source.read_bytes()


def test_require_timezone_and_correct_image_extension(tmp_path):
    assert download.timestamp("2026-10-11T15:45:30+08:00") == download.timestamp(
        "2026-10-11T07:45:30Z"
    )
    with pytest.raises(ValueError, match="timezone"):
        download.timestamp("2026-10-11T15:45:30")
    source = tmp_path / "uuid"
    make_image(source, 200, fmt="JPEG")
    with pytest.raises(ValueError, match="extension"):
        download.save_image(source, tmp_path / "wrong.png")
    assert not (tmp_path / "wrong.png").exists()


def test_cli_returns_json_error_for_missing_source(capsys, tmp_path):
    assert download.main(["--source", str(tmp_path / "missing"), "--output", "image.png"]) == 1
    assert '"ok": false' in capsys.readouterr().out
