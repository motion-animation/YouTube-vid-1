import argparse
import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import wave
from pathlib import Path

import gdown
import numpy as np
from moviepy import AudioFileClip, CompositeAudioClip
from playwright.async_api import async_playwright


def get_drive_url(cli_url: str | None) -> str | None:
    if cli_url and cli_url.strip():
        return cli_url.strip()
    env_url = os.environ.get("GDRIVE_AUDIO_URL", "").strip()
    if env_url:
        return env_url
    link_file = os.path.join("audio", "drive_link.txt")
    if os.path.exists(link_file):
        with open(link_file, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    return s
    return None


def download_from_gdrive(url: str) -> str | None:
    os.makedirs("audio", exist_ok=True)
    m = re.search(r"/d/([a-zA-Z0-9_-]+)|[?&]id=([a-zA-Z0-9_-]+)", url)
    file_id = (m.group(1) or m.group(2)) if m else None
    dl_url = f"https://drive.google.com/uc?id={file_id}" if file_id else url
    print(f"[Drive] Downloading audio directly from Google Drive: {dl_url}")
    gdown.download(dl_url, output="audio/", quiet=False)
    return find_voiceover()


def find_voiceover():
    patterns = [
        "audio/*.mp3", "audio/*.wav", "audio/*.m4a", "audio/*.aac", "audio/*.ogg", "audio/*.flac",
        "*.mp3", "*.wav", "*.m4a", "*.aac",
    ]
    for pat in patterns:
        matches = sorted(glob.glob(pat))
        if matches:
            return matches[0]
    return None


def build_sfx_track(manifest, total_duration: float, out_wav: str) -> str | None:
    sr = 44100
    total_samples = int((total_duration + 1.0) * sr)
    mix = np.zeros(total_samples, dtype=np.float32)
    sfx_cache = {}
    placed = 0

    for sc in manifest["scenes"]:
        for sfx in sc.get("sfx", []):
            sfx_path = os.path.join("sfx", sfx["file"])
            if not os.path.exists(sfx_path):
                continue
            if sfx_path not in sfx_cache:
                with wave.open(sfx_path, "rb") as wf:
                    raw = wf.readframes(wf.getnframes())
                    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
                    if wf.getnchannels() > 1:
                        data = data.reshape(-1, wf.getnchannels()).mean(axis=1)
                    sfx_cache[sfx_path] = data
            samples = sfx_cache[sfx_path]
            start_idx = int((sc["start"] + sfx["offset_s"]) * sr)
            if 0 <= start_idx < total_samples:
                end_idx = min(total_samples, start_idx + len(samples))
                mix[start_idx:end_idx] += samples[: end_idx - start_idx] * float(sfx.get("volume", 0.4))
                placed += 1

    if placed == 0:
        return None

    mix = np.clip(mix, -1.0, 1.0)
    pcm = (mix * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    print(f"[Audio] Pre-mixed {placed} SFX cues into {out_wav}")
    return out_wav


async def render_scene_chunk(context, scene, work_root, fps):
    scene_id = scene["id"]
    dur = scene["duration"]
    num_frames = max(1, int(round(dur * fps)))
    scene_dir = os.path.join(work_root, f"scene_{scene_id:03d}")
    os.makedirs(scene_dir, exist_ok=True)

    page = await context.new_page()
    await page.set_viewport_size({"width": 1280, "height": 720})
    file_url = Path(os.path.abspath(scene["file"])).as_uri()
    await page.goto(file_url, wait_until="domcontentloaded")
    await page.evaluate("window.__RENDER_MODE__ = true;")

    for f_idx in range(num_frames):
        t = f_idx / fps
        await page.evaluate(f"window.seek({t:.4f});")
        out_jpg = os.path.join(scene_dir, f"f_{f_idx:05d}.jpg")
        await page.screenshot(path=out_jpg, type="jpeg", quality=92)

    await page.close()

    # Encode this scene chunk in parallel using FFmpeg, then delete raw frames
    chunk_mp4 = os.path.join(work_root, f"chunk_{scene_id:03d}.mp4")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", os.path.join(scene_dir, "f_%05d.jpg"),
        "-t", f"{dur:.4f}",
        "-vf", "scale=1920:1080",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
        chunk_mp4,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    shutil.rmtree(scene_dir, ignore_errors=True)
    print(f"[Rendered 1080p] Scene {scene_id:03d}/{len(os.listdir('scenes'))} ({num_frames} frames, {dur:.2f}s)")
    return scene_id, chunk_mp4


async def render_all_chunks(scenes, work_root, fps, workers):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        # device_scale_factor=1.5 turns 1280x720 CSS stage into crisp 1920x1080 Full HD!
        context = await browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5)
        sem = asyncio.Semaphore(workers)

        async def worker(sc):
            async with sem:
                return await render_scene_chunk(context, sc, work_root, fps)

        results = await asyncio.gather(*(worker(sc) for sc in scenes))
        await browser.close()
    results.sort(key=lambda x: x[0])
    return [chunk for _, chunk in results]


def merge_chunks_and_audio(manifest, chunk_paths, out_mp4, voiceover_path):
    concat_txt = "_concat_list.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for cp in chunk_paths:
            f.write(f"file '{os.path.abspath(cp)}'\n")

    silent_mp4 = "_silent_1080p.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", silent_mp4],
        check=True,
    )

    total_dur = manifest["scenes"][-1]["end"]
    audio_layers = []
    if voiceover_path and os.path.exists(voiceover_path):
        print(f"[Audio] Layering voiceover: {voiceover_path}")
        vo = AudioFileClip(voiceover_path)
        safe_vo_dur = min(total_dur, max(0.1, vo.duration - 0.05))
        audio_layers.append(vo.subclipped(0, safe_vo_dur))
    else:
        print("[Audio] No voiceover found — rendering with SFX only.")

    sfx_wav = build_sfx_track(manifest, total_dur, "_sfx_track.wav")
    if sfx_wav and os.path.exists(sfx_wav):
        sfx_clip = AudioFileClip(sfx_wav).subclipped(0, total_dur)
        audio_layers.append(sfx_clip)

    mixed_wav = "_final_audio.wav"
    if audio_layers:
        mixed_audio = CompositeAudioClip(audio_layers).with_duration(total_dur)
        mixed_audio.write_audiofile(mixed_wav, fps=44100)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", silent_mp4, "-i", mixed_wav,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "-shortest",
                out_mp4,
            ],
            check=True,
        )
    else:
        shutil.move(silent_mp4, out_mp4)

    for tmp in (concat_txt, silent_mp4, sfx_wav, mixed_wav):
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    print(f"[Done] Final 1080p video saved to {out_mp4}")


def main():
    parser = argparse.ArgumentParser(description="Parallel 1080p HTML + SFX + Voiceover Renderer")
    parser.add_argument("--fps", type=int, default=25, help="Output FPS (default: 25)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent Chromium + FFmpeg workers")
    parser.add_argument("--output", type=str, default="output_1080p.mp4", help="Output MP4 filename")
    parser.add_argument("--audio-url", type=str, default=None, help="Google Drive share URL or File ID")
    parser.add_argument("--preview", action="store_true", help="Render only first 3 scenes")
    args = parser.parse_args()

    with open("scenes.json", "r", encoding="utf-8") as f:
        manifest = json.load(f)

    if args.preview:
        manifest["scenes"] = manifest["scenes"][:3]

    voiceover = find_voiceover()
    if not voiceover:
        drive_url = get_drive_url(args.audio_url)
        if drive_url:
            voiceover = download_from_gdrive(drive_url)

    work_root = "_render_chunks"
    if os.path.exists(work_root):
        shutil.rmtree(work_root)
    os.makedirs(work_root, exist_ok=True)

    chunk_paths = asyncio.run(render_all_chunks(manifest["scenes"], work_root, args.fps, args.workers))
    merge_chunks_and_audio(manifest, chunk_paths, args.output, voiceover)

    shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    main()