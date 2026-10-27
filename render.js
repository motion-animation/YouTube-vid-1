const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const puppeteer = require('puppeteer');

async function main() {
  const scriptPath = path.resolve(__dirname, 'script.json');
  const scriptData = JSON.parse(fs.readFileSync(scriptPath, 'utf8'));

  // Allow CLI or GitHub Actions env override for aspect ratio
  const formatOverride = process.env.VIDEO_FORMAT || scriptData?.meta?.format || '16:9';
  const isLandscape = formatOverride === '16:9';
  const width = isLandscape ? 1920 : 1080;
  const height = isLandscape ? 1080 : 1920;
  const fps = Number(scriptData?.meta?.fps || 30);

  scriptData.meta = {
    ...scriptData.meta,
    format: formatOverride,
    width,
    height,
    fps
  };

  // Compute total duration from scenes (or allow max duration cap for test renders)
  const scenes = scriptData.scenes || [];
  const computedDuration = scenes.reduce((maxEnd, s) => {
    const end = Number(s.start || 0) + Number(s.duration || 2.5);
    return Math.max(maxEnd, end);
  }, 10);

  const totalDurationSec = Number(process.env.RENDER_SECONDS || computedDuration);
  const totalFrames = Math.ceil(totalDurationSec * fps);

  console.log(`[Render] Format: ${formatOverride} (${width}x${height}) @ ${fps}fps | Duration: ${totalDurationSec}s (${totalFrames} frames)`);

  const browser = await puppeteer.launch({
    headless: 'new',
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      `--window-size=${width},${height}`
    ]
  });

  const page = await browser.newPage();
  await page.setViewport({ width, height, deviceScaleFactor: 1 });

  const htmlPath = 'file://' + path.resolve(__dirname, 'index.html');
  await page.goto(htmlPath, { waitUntil: 'networkidle0' });

  // Inject script.json directly so file:// protocol works without CORS issues
  await page.evaluate(async (data) => {
    await window.initStage(data);
  }, scriptData);

  const outFile = path.resolve(__dirname, 'output_visuals.mp4');
  const ffmpeg = spawn('ffmpeg', [
    '-y',
    '-f', 'image2pipe',
    '-vcodec', 'png',
    '-r', String(fps),
    '-i', '-',
    '-c:v', 'libx264',
    '-pix_fmt', 'yuv420p',
    '-preset', 'veryfast',
    '-crf', '18',
    outFile
  ]);

  ffmpeg.stderr.on('data', (d) => {
    // Log periodically on errors
    const msg = d.toString();
    if (msg.includes('Error')) console.error(msg);
  });

  for (let frame = 0; frame < totalFrames; frame++) {
    await page.evaluate((f, rate) => {
      window.seekToFrame(f, rate);
    }, frame, fps);

    const pngBuffer = await page.screenshot({ type: 'png' });
    const canWrite = ffmpeg.stdin.write(pngBuffer);
    if (!canWrite) {
      await new Promise((resolve) => ffmpeg.stdin.once('drain', resolve));
    }

    if (frame % (fps * 5) === 0) {
      console.log(`[Render] Frame ${frame}/${totalFrames} (${((frame / totalFrames) * 100).toFixed(1)}%)`);
    }
  }

  ffmpeg.stdin.end();
  await new Promise((resolve, reject) => {
    ffmpeg.on('close', (code) => (code === 0 ? resolve() : reject(new Error(`FFmpeg exited ${code}`))));
  });

  await browser.close();
  console.log(`[Render] Done -> ${outFile}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});