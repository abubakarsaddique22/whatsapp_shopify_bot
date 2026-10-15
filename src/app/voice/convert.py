"""Phase 6, step 3: turn a downloaded voice note into a WAV file for the speech provider.

The one function the pipeline uses:

    async with prepared_audio(media_id, max_bytes=..., max_seconds=...) as audio:
        audio.wav_path        # 16 kHz, mono, 16-bit WAV
        audio.duration_s      # real length, measured from the WAV
        audio.size_bytes      # size of the downloaded file
        ...transcribe(audio.wav_path)...
    # here every temporary file is already gone, also when the block raised an error

Privacy (blueprint 6.9): the audio exists only inside this block, in a temporary folder with a
random name. The folder is removed when the block ends, whether it ended well or with an error.
Audio never goes into logs, and nothing here stores it.

ffmpeg must be installed and on PATH (winget install Gyan.FFmpeg, then restart the shell).
Conversion stops after max_seconds + 1 seconds of audio: a longer clip then shows a duration
above max_seconds, so the gate can refuse it without converting a ten-minute file.
"""

import asyncio
import tempfile
import uuid
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AudioConversionError, ConfigError
from app.integrations.whatsapp.media import download_media

FFMPEG = "ffmpeg"
CONVERT_TIMEOUT_SECONDS = 30.0
SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class WavFile:
    path: Path
    duration_s: float


@dataclass(frozen=True)
class PreparedAudio:
    wav_path: Path
    duration_s: float
    size_bytes: int  # size of the file as downloaded
    mime_type: str  # type of the file as downloaded


def wav_duration(path: Path) -> float:
    """Length of a WAV file in seconds."""
    try:
        with wave.open(str(path), "rb") as wav:
            rate = wav.getframerate()
            return wav.getnframes() / rate if rate else 0.0
    except (wave.Error, EOFError, OSError):
        raise AudioConversionError("the converted audio could not be read") from None


async def convert_to_wav(
    source: Path,
    folder: Path,
    *,
    max_seconds: int,
    ffmpeg: str = FFMPEG,
    timeout: float = CONVERT_TIMEOUT_SECONDS,
) -> WavFile:
    """ffmpeg: any audio -> mono 16 kHz 16-bit WAV in `folder` (random file name)."""
    target = folder / f"{uuid.uuid4().hex}.wav"
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-t",
        str(max_seconds + 1),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        raise ConfigError(
            "ffmpeg was not found. Install it (winget install Gyan.FFmpeg) and restart the shell"
        ) from None

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise AudioConversionError("ffmpeg took too long") from None

    if process.returncode != 0 or not target.exists():
        tail = stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise AudioConversionError(
            "ffmpeg could not convert the audio",
            detail={"returncode": process.returncode, "stderr_tail": tail},
        )
    return WavFile(path=target, duration_s=wav_duration(target))


@asynccontextmanager
async def prepared_audio(
    media_id: str,
    *,
    max_bytes: int,
    max_seconds: int,
    work_dir: Path | None = None,
    ffmpeg: str = FFMPEG,
) -> AsyncIterator[PreparedAudio]:
    """Download the voice note, convert it, hand it over, and delete everything afterwards."""
    with tempfile.TemporaryDirectory(
        prefix="wa-audio-", dir=work_dir, ignore_cleanup_errors=True
    ) as name:
        folder = Path(name)
        media = await download_media(media_id, folder, max_bytes=max_bytes)
        wav = await convert_to_wav(media.path, folder, max_seconds=max_seconds, ffmpeg=ffmpeg)
        yield PreparedAudio(
            wav_path=wav.path,
            duration_s=wav.duration_s,
            size_bytes=media.size_bytes,
            mime_type=media.mime_type,
        )
