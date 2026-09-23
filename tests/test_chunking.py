import subprocess

from app.audio import Chunk, extract_chunk, normalize, plan_chunks, probe, wav_duration
from tests.conftest import make_audio


def test_65_minute_file_makes_3_chunks(tmp_path):
    # 65 minutes of silence at a tiny bitrate: ~4 MB, generated in a few seconds.
    long_file = make_audio(tmp_path / "long.mp3", 65 * 60, kind="silence",
                           fmt_args=("-c:a", "libmp3lame", "-b:a", "8k"))
    info = probe(str(long_file))
    assert abs(info.duration - 3900) < 1

    chunks = plan_chunks(info.duration, 1800)
    assert len(chunks) == 3
    assert [(c.index, c.start, c.end) for c in chunks[:2]] == [(0, 0.0, 1800.0), (1, 1800.0, 3600.0)]
    assert chunks[2].index == 2 and chunks[2].start == 3600.0 and abs(chunks[2].end - 3900) < 1


def test_plan_edges():
    assert plan_chunks(3900, 1800) == [Chunk(0, 0, 1800), Chunk(1, 1800, 3600), Chunk(2, 3600, 3900)]
    assert plan_chunks(100, 1800) == [Chunk(0, 0.0, 100.0)]
    assert plan_chunks(3600, 1800) == [Chunk(0, 0, 1800), Chunk(1, 1800, 3600)]
    # A sub-second tail is folded into the last chunk instead of becoming its own.
    assert plan_chunks(3600.4, 1800) == [Chunk(0, 0, 1800), Chunk(1, 1800, 3600.4)]


def test_real_split_has_right_lengths(tmp_path):
    src = make_audio(tmp_path / "clip.mp3", 7.5)
    wav = str(tmp_path / "norm.wav")
    normalize(str(src), wav)
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
                           "-of", "csv=p=0", wav], capture_output=True, text=True).stdout.strip()
    assert info == "16000,1"

    plan = plan_chunks(wav_duration(wav), 3)
    assert len(plan) == 3
    lengths = []
    for c in plan:
        out = str(tmp_path / f"c{c.index}.wav")
        extract_chunk(wav, c, c.index == len(plan) - 1, out)
        lengths.append(wav_duration(out))
    assert abs(lengths[0] - 3) < 0.005 and abs(lengths[1] - 3) < 0.005
    assert abs(sum(lengths) - wav_duration(wav)) < 0.01
