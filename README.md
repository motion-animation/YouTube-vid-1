# Tape Market — 72-Scene HTML + SFX + MoviePy 720p Video Pack

Ready-to-run faceless motion graphics video pack generated from `output.srt` (118 cues, 10m16s) in the clean Google/Apple Keynote light theme (`1280x720`).

## Zero-Data-Waste Google Drive Audio Support
You do **not** need to download your audio from Google Drive and re-upload it to GitHub:
1. Make sure your audio file on Google Drive is shared as **"Anyone with the link"**.
2. Unzip and upload this pack to your GitHub repo.
3. Either:
   - Go to **Actions → Render Tape Market 720p Video → Run workflow** and paste your Google Drive link into **`gdrive_audio_url`**, **OR**
   - Paste your Google Drive link into `audio/drive_link.txt` (or set a GitHub Repo Secret named `GDRIVE_AUDIO_URL`).
4. GitHub Actions will download the audio directly on its server, sync all 72 HTML scenes + SFX with MoviePy/FFmpeg, and give you `output_720p.mp4` in Artifacts!
