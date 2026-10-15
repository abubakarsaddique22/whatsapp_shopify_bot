"""ffmpeg conversion and the temporary-file rule (Phase 6, step 3).

Tests that need the real ffmpeg are skipped when it is not on PATH. The temporary-file tests use
fakes for the download and the conversion, so they always run."""

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from app.core.errors import AudioConversionError, ConfigError
from app.integrations.whatsapp.media import DownloadedMedia
from app.voice import convert
from app.voice.convert import WavFile, convert_to_wav, prepared_audio, wav_duration

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not on PATH")


def write_wav(path: Path, seconds: int = 1) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 16_000 * seconds)


def make_ogg(path: Path, seconds: int) -> None:
    """A real ogg/opus file (a sine tone), like a WhatsApp voice note."""
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:a", "libopus", str(path),
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        pytest.skip("this ffmpeg build cannot write ogg/opus")


# ---- wav_duration


def test_wav_duration(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    write_wav(path, seconds=3)
    assert wav_duration(path) == pytest.approx(3.0)


def test_wav_duration_of_a_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.wav"
    path.write_bytes(b"not a wav file")
    with pytest.raises(AudioConversionError):
        wav_duration(path)


# ---- the real ffmpeg


@needs_ffmpeg
async def test_ogg_is_converted_to_mono_16khz_wav(tmp_path: Path) -> None:
    source = tmp_path / "note.ogg"
    make_ogg(source, seconds=2)

    result = await convert_to_wav(source, tmp_path, max_seconds=90)

    with wave.open(str(result.path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16_000
        assert wav.getsampwidth() == 2
    assert result.duration_s == pytest.approx(2.0, abs=0.2)


@needs_ffmpeg
async def test_a_long_clip_is_cut_just_above_the_limit(tmp_path: Path) -> None:
    """5 seconds of audio with a 2 second limit: converted to about 3 seconds, so the duration
    is above the limit and the gate can refuse it."""
    source = tmp_path / "long.ogg"
    make_ogg(source, seconds=5)

    result = await convert_to_wav(source, tmp_path, max_seconds=2)

    assert 2.0 < result.duration_s <= 3.2


@needs_ffmpeg
async def test_a_damaged_file_is_a_conversion_error(tmp_path: Path) -> None:
    source = tmp_path / "damaged.ogg"
    source.write_bytes(b"this is not audio at all")
    with pytest.raises(AudioConversionError):
        await convert_to_wav(source, tmp_path, max_seconds=90)


async def test_a_missing_ffmpeg_is_a_config_error(tmp_path: Path) -> None:
    source = tmp_path / "note.ogg"
    source.write_bytes(b"x")
    with pytest.raises(ConfigError):
        await convert_to_wav(source, tmp_path, max_seconds=90, ffmpeg="ffmpeg-does-not-exist-xyz")


# ---- prepared_audio: temporary files are always removed


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Replace the download and the conversion. `state` can make either of them fail."""
    state = {"fail_convert": False}

    async def fake_download(media_id: str, folder: Path, *, max_bytes: int, timeout: float = 20.0):
        path = folder / "downloaded.bin"
        path.write_bytes(b"audio")
        return DownloadedMedia(path=path, mime_type="audio/ogg", size_bytes=5)

    async def fake_convert(source: Path, folder: Path, *, max_seconds: int, ffmpeg: str = "x"):
        if state["fail_convert"]:
            raise AudioConversionError("boom")
        path = folder / "converted.wav"
        write_wav(path, seconds=2)
        return WavFile(path=path, duration_s=2.0)

    monkeypatch.setattr(convert, "download_media", fake_download)
    monkeypatch.setattr(convert, "convert_to_wav", fake_convert)
    return state


async def test_prepared_audio_hands_over_the_wav_and_cleans_up(
    tmp_path: Path, fakes: dict[str, bool]
) -> None:
    async with prepared_audio("M1", max_bytes=1000, max_seconds=90, work_dir=tmp_path) as audio:
        assert audio.wav_path.exists()
        assert audio.duration_s == 2.0
        assert audio.size_bytes == 5
        assert audio.mime_type == "audio/ogg"
        assert len(list(tmp_path.rglob("*"))) > 1  # the folder and both files exist for now
    assert list(tmp_path.iterdir()) == []


async def test_temp_files_are_removed_when_the_block_raises(
    tmp_path: Path, fakes: dict[str, bool]
) -> None:
    """The transcription fails inside the block: the audio must still be gone afterwards."""
    with pytest.raises(RuntimeError):
        async with prepared_audio("M1", max_bytes=1000, max_seconds=90, work_dir=tmp_path):
            raise RuntimeError("transcription failed")
    assert list(tmp_path.iterdir()) == []


async def test_downloaded_file_is_removed_when_the_conversion_fails(
    tmp_path: Path, fakes: dict[str, bool]
) -> None:
    fakes["fail_convert"] = True
    with pytest.raises(AudioConversionError):
        async with prepared_audio("M1", max_bytes=1000, max_seconds=90, work_dir=tmp_path):
            pass
    assert list(tmp_path.iterdir()) == []
