# video-renderer

Declarative video generation pipeline: describe scenes in a JSON spec, get an
MP4 with animated visuals and Mistral TTS (Voxtral) voiceover.

## How it works

1. You provide a **spec** (JSON) — a timeline of scenes with text, charts,
   images, and optional narration.
2. The renderer synthesizes narration per scene via the Mistral TTS API,
   probes each clip's duration, and sizes each scene to its audio
   (audio-driven timeline). Explicit `duration` overrides when you want a
   scene to linger.
3. MoviePy composites the scenes with animations and encodes the final MP4.
   Narration subtitles and an editable PowerPoint deck are emitted alongside it.

## Running locally

```bash
git clone https://github.com/iamjonjackson/video-renderer.git
cd video-renderer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # then edit .env and add your MISTRAL_API_KEY
set -a; source .env; set +a   # load env vars (or use dotenv in your shell)

# Final videos, subtitle files, and PowerPoint decks go to ./output.
# Large render writes happen in the system temp folder before final copy.
python3 src/render.py samples/sample-spec.json -o output/demo.mp4
```

Notes:
- `.env` is gitignored; `.env.example` documents the variables.
- `output/` (rendered deliverables) is gitignored except its `.gitkeep`
  placeholder. Render caches and intermediate files use the system temp folder
  by default; pass `--workdir` to choose another cache location.
- Without `MISTRAL_API_KEY`, scenes render silently and AI image blocks are
  skipped with a warning — the pipeline never hard-fails on missing AI.
- System ffmpeg is used if installed; otherwise `imageio-ffmpeg` bundles one.

### GitHub Actions

A `Render test` workflow runs on PRs: renders `samples/sample-spec.json`
using the `MISTRAL_API_KEY` repo secret and uploads the MP4 as an artifact.

## Spec format (summary)

```json
{
  "meta":  { "fps": 30 },
  "theme": { "preset": "dark", "accent": "#58a6ff" },
  "voice": { "slug": "gb_jane_neutral" },
  "timeline": [
    { "type": "title",  "text": "Quarterly Review", "narration": "..." },
    { "type": "bullets", "heading": "Key Wins", "items": ["...", "..."] },
    { "type": "chart",  "chart_type": "bar", "labels": ["Jul","Aug","Sep"],
                        "values": [410, 455, 512], "narration": "..." },
    { "type": "image",  "src": "assets/slide.png", "anim": "kenburns" },
    { "type": "outro",  "text": "Thank you" }
  ]
}
```

- **Scene types**: `title`, `bullets`, `chart` (bar/line/pie), `image`, `outro`
- **Animations**: `fade`, `slide-up`, `zoom-in`, `kenburns` (images)
- **PPTX export**: every render also emits a `.pptx` alongside the MP4 —
  one slide per scene, built from the same artifacts (narration WAVs
  embedded on their slides, narration text in speaker notes, editable native
  charts, and generated images). Open in
  PowerPoint, tweak, and export to video from there for a near-identical
  result.
- **Subtitles**: narrated scenes produce a matching `.srt` file. Long scripts
  are split into readable cues; per-bullet narration follows its audio timing.
- **Branding**: add an optional top-level `branding` block to overlay a
  transparent PNG watermark in a frame corner. `scenes` can be `"all"` or a
  list of scene types.

```json
"branding": {
  "logo": "assets/logo.png",
  "position": "bottom-right",
  "margin": 48,
  "opacity": 0.85,
  "max_height": 0.1,
  "scenes": ["title", "outro"]
}
```
- **AI-generated images** (optional, any scene): add an `image` block —

```json
"image": {
  "prompt": "minimal isometric illustration of a rocket launch, deep blue palette",
  "placement": "background"   // "background" (faded, behind text) or
                               // "left" / "right" (spotlight panel beside text)
  "opacity": 0.25,             // background mode: image fade level
  "model": "mistral-medium-latest" // optional agent model override
}
```

For `background` placement the renderer measures WCAG contrast of the text
area against the composited backdrop and raises the scrim opacity until
AA (≥ 4.5:1) is met. If the target can't be reached even at maximum scrim,
the render continues with a warning (best-effort) — it never aborts.
`left`/`right` placements keep text on the solid theme background (contrast
by construction) with the image in a bordered panel.
- **Timing**: omit `duration` and provide `narration` for audio-driven scenes;
  `padding` (default 1.0s) adds a guaranteed tail buffer after narration.
- **Voice**: built-in voice slugs (list via `GET /v1/audio/voices`) or a
  cloned `voice_id` from a 2–3s audio sample.
- Full contract: `src/schema.json` (JSON Schema).

See `samples/sample-spec.json` for a complete example.

### Validating a spec

Every render checks `src/schema.json` first and reports all errors with their
scene numbers before starting. To validate without rendering:

```bash
python3 src/render.py samples/sample-spec.json --validate
# or
python3 src/validate.py samples/sample-spec.json
```

## Files

- `src/render.py` — spec → MP4 renderer (MoviePy + Matplotlib)
- `src/tts.py` — Mistral Voxtral TTS REST client (`/v1/audio/speech`)
- `src/schema.json` — JSON Schema for the spec
- `src/validate.py` — spec validation with scene-numbered errors
- `requirements.txt` — Python runtime dependencies
- `src/themes/presets.py` — dark/light color presets
- `demos/` — rendered example videos from `samples/sample-spec.json`

## Notes

- TTS is skipped gracefully (with a warning) when `MISTRAL_API_KEY` is unset.
- Never commit API keys; pass via environment only.

### Getting a Mistral API key

Students: request a key from your hackathon organiser.

- Free API access is not currently available from Mistral but the Pro (Education) option is very affordable if you wanted to sign up at [console.mistral.ai](https://console.mistral.ai)
- If you have a paid account, you can create a key under **API Keys**, copy it into your `.env`:
   `MISTRAL_API_KEY=...`
- Rate limits and model availability depend on your tier — see the
   **Limits** page in the console for your current allowances

## Contributing

Work happens on a branch in your fork **first**; the PR comes last.

1. **Fork** the repo, clone your fork, and create a branch: `git checkout -b your-feature`
2. **Pick an open issue** — issues are not exclusive; multiple teams may work the same one
3. **Build and test** — render the sample: `python3 src/render.py samples/sample-spec.json -o output/demo.mp4` (add your own `MISTRAL_API_KEY` in your fork under Settings → Secrets → Actions to run CI)
4. **Commit with co-author trailers** so teammates get credit: `Co-authored-by: Name <name@example.com>` (See `.github/PULL_REQUEST_TEMPLATE.md` for instructions)
5. **Push your branch and open a PR** (`gh pr create`, or the web UI's Contribute button), referencing the issue (`Closes #N`) and attaching screenshot/video evidence as per the PR template

> CI runs on PRs but without access to repo secrets — test locally or in your fork with your own `MISTRAL_API_KEY` if your change touches TTS or image generation.
