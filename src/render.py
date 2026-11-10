"""Declarative video spec renderer.

Usage:
    python3 render.py spec.json [-o output.mp4] [--workdir /tmp/renders]

Scene durations are audio-driven when `narration` is present (duration = audio
length + padding), unless an explicit `duration` overrides them.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from moviepy import (ColorClip, CompositeVideoClip, ImageClip, TextClip,
                     VideoFileClip, AudioFileClip, CompositeAudioClip, concatenate_videoclips,
                     concatenate_audioclips, vfx)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "themes"))
import images as imggen
import tts
import presets
from validate import SpecError, load_and_validate, report

ANIM_IN = 0.6
SLIDE_DIST = 120
W, H = 1920, 1080


def rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _resolve_font(bold: bool = False) -> str:
    """Return a TTF path with wide glyph coverage (incl. U+2022 bullet)."""
    import os
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
        os.path.join(os.path.dirname(matplotlib.__file__),
                     "mpl-data", "fonts", "ttf",
                     "DejaVuSans%s.ttf" % ("-Bold" if bold else "")),
        os.path.join(os.path.dirname(matplotlib.__file__),
                     "mpl-data", "fonts", "ttf", "DejaVuSans.ttf"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise RuntimeError("No suitable TTF font found; install fonts-dejavu or matplotlib")


def load_spec(path: str) -> dict:
    spec = load_and_validate(path)
    theme = dict(presets.__dict__[spec.get("theme", {}).get("preset", "dark")])
    theme.update({k: v for k, v in spec.get("theme", {}).items() if k != "preset"})
    spec["theme"] = theme
    spec.setdefault("meta", {})
    spec["meta"].setdefault("resolution", {"width": 1920, "height": 1080})
    spec["meta"].setdefault("fps", 30)
    return spec


def synth_narration(scene: dict, workdir: Path, voice_cfg: dict):
    """Return (combined audio path, total duration, (text, duration) segments)."""
    texts = []
    per_bullet = scene.get("narration_per_bullet")
    if per_bullet:
        texts = per_bullet
    elif scene.get("narration"):
        texts = [scene["narration"]]
    if not texts:
        return None, 0.0, []
    paths = []
    for i, text in enumerate(texts):
        h = hashlib.sha1(text.encode()).hexdigest()[:10]
        out = workdir / f"narr_{h}_{i}.{voice_cfg.get('format', 'wav')}"
        if not out.exists():
            tts.synth_to_file(
                text, str(out),
                voice_slug=voice_cfg.get("slug"), voice_id=voice_cfg.get("voice_id"),
                model=voice_cfg.get("model", "voxtral-mini-tts-2603"),
                fmt=voice_cfg.get("format", "wav"),
            )
        paths.append(str(out))
    segments = [(text, tts.audio_duration(path)) for text, path in zip(texts, paths)]
    total_duration = sum(duration for _, duration in segments)
    if len(paths) == 1:
        return paths[0], total_duration, segments
    combined = workdir / f"narr_combined_{hashlib.sha1(json.dumps(paths).encode()).hexdigest()[:10]}.{voice_cfg.get('format', 'wav')}"
    if not combined.exists():
        merged = concatenate_audioclips([AudioFileClip(p) for p in paths])
        merged.write_audiofile(str(combined), logger=None)
    return str(combined), total_duration, segments


def scene_duration(scene: dict, audio_dur: float, default: float = 4.0) -> float:
    explicit = scene.get("duration")
    if audio_dur > 0:
        # Never cut narration short: explicit duration is a floor, not a cap.
        audio_driven = audio_dur + scene.get("padding", 1.0)
        if explicit:
            return max(explicit, audio_driven)
        return audio_driven
    if explicit:
        return explicit
    if scene.get("type") == "bullets":
        return 1.2 + scene.get("stagger", 0.4) * len(scene.get("items", [])) + 2.0
    return default



def centered_text_clip(text: str, theme: dict, size: int, color: str | None = None,
                       x_frac: float = 0.5, y_frac: float = 0.5,
                       max_w_frac: float = 0.85, bold: bool = False) -> TextClip:
    """Text centered at (x_frac, y_frac) of the frame.

    Auto height with a generous bottom margin: the box is always taller
    than the text, so descenders never clip.
    """
    clip = TextClip(
        text=text, font_size=size,
        color=color or theme["fg"],
        font=_resolve_font(bold),
        method="caption",
        text_align="center",
        vertical_align="center",
        size=(int(W * max_w_frac), None),
        margin=(15, 8, 15, int(size * 0.45)),
    )
    cx, cy = int(W * x_frac), int(H * y_frac)
    pos = (cx - clip.w // 2, cy - clip.h // 2)
    return clip.with_position(pos)


def scene_image_clip(scene: dict, theme: dict, workdir: Path):
    """Generate (if needed) and return the scene's AI image path, or None."""
    img_cfg = scene.get("image")
    if not img_cfg:
        return None
    if not img_cfg.get("prompt"):
        print("[warn] scene 'image' block missing 'prompt'; skipping")
        return None
    out = workdir / f"sceneimg_{hashlib.sha1(json.dumps(img_cfg, sort_keys=True).encode()).hexdigest()[:12]}.png"
    if not out.exists():
        try:
            imggen.generate_image(img_cfg["prompt"], str(out),
                                   model=img_cfg.get("model"), workdir=workdir)
        except RuntimeError as e:
            print(f"[warn] image generation failed ({e}); rendering without image")
            return None
    return str(out)


def build_title(scene, theme, workdir=None):
    bg = ColorClip(size=(W, H), color=rgb(theme["bg"]))
    clips = [bg]
    img_path = scene_image_clip(scene, theme, workdir) if workdir else None
    if img_path:
        clips = composite_bg_image(clips, scene, theme, img_path)
    title = centered_text_clip(scene["text"], theme, theme["title_font_size"],
                               bold=True, y_frac=0.42)
    clips.append(title)
    if scene.get("subtitle"):
        sub = centered_text_clip(scene["subtitle"], theme,
                                 int(theme["font_size"] * 0.55),
                                 color=theme["accent"], y_frac=0.58)
        clips.append(sub)
    return CompositeVideoClip(clips, size=(W, H))


def composite_bg_image(clips, scene, theme, img_path):
    """Insert a faded background image behind text with best-effort WCAG scrim."""
    img_cfg = scene["image"]
    opacity = img_cfg.get("opacity", 0.25)
    bg_img = ImageClip(img_path)
    scale = max(W / bg_img.w, H / bg_img.h)
    bg_img = bg_img.resized(scale)
    if bg_img.w > W or bg_img.h > H:
        bg_img = bg_img.cropped(x_center=bg_img.w / 2, y_center=bg_img.h / 2,
                                width=W, height=H)
    bg_img = bg_img.with_opacity(opacity).with_position((0, 0))
    scrim, passes = imggen.scrim_for_contrast(
        img_path, theme["fg"], theme["bg"],
        text_area=(0.5, 0.5, 0.7, 0.4),
        img_opacity=opacity,
    )
    if not passes:
        print(f"[warn] scene background image cannot reach 4.5:1 contrast "
              f"even at scrim {scrim:.2f}; text may be hard to read")
    scrim_clip = ColorClip(size=(W, H), color=rgb(theme["bg"])) \
        .with_opacity(scrim).with_position((0, 0))
    return [clips[0], bg_img, scrim_clip] + clips[1:]


def build_spotlight_panel(scene, theme, img_path):
    """Rounded image panel on one side; returns (clips_to_add, text_x_frac)."""
    placement = scene["image"].get("placement", "right")
    side = placement if placement in ("left", "right") else "right"
    panel_w = int(W * 0.38)
    panel_h = int(H * 0.70)
    x = int(W * 0.56) if side == "right" else int(W * 0.06)
    y = int(H * 0.15)

    img = ImageClip(img_path)
    scale = max(panel_w / img.w, panel_h / img.h)
    img = img.resized(scale)
    if img.w > panel_w or img.h > panel_h:
        img = img.cropped(x_center=img.w / 2, y_center=img.h / 2,
                          width=panel_w, height=panel_h)
    img = img.with_position((x, y))
    border = ColorClip(size=(panel_w + 8, panel_h + 8),
                       color=rgb(theme["accent"])).with_position((x - 4, y - 4))
    text_x = 0.28 if side == "right" else 0.72
    return [border, img], text_x


def _left_text_clip(text: str, theme: dict, size: int, color: str | None = None,
                    bold: bool = False, max_w_px: int | None = None) -> TextClip:
    """Left-aligned text via auto-sized label clips: true edge alignment,
    with margin keeping the box taller than the text (no descender clipping)."""
    font = _resolve_font(bold)
    if max_w_px:
        text = _wrap_to_width(text, size, font, max_w_px)
    return TextClip(
        text=text, font_size=size,
        color=color or theme["fg"],
        font=font, method="label", text_align="left",
        vertical_align="center",
        margin=(0, 2, 0, int(size * 0.45)),
    )


def _wrap_to_width(text: str, size: int, font: str, max_w_px: int,
                   max_lines: int = 3) -> str:
    """Word-wrap so each line renders narrower than max_w_px."""
    from PIL import ImageFont
    f = ImageFont.truetype(font, size)
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if f.getbbox(trial)[2] <= max_w_px or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines[:max_lines])


def build_bullets(scene, theme, workdir=None, reveal_offsets=None, duration=None):
    bg = ColorClip(size=(W, H), color=rgb(theme["bg"]))
    clips = [bg]
    text_x_frac = 0.5
    panel_clips = []
    img_path = scene_image_clip(scene, theme, workdir) if workdir else None
    if img_path:
        img_cfg = scene["image"]
        placement = img_cfg.get("placement", "background")
        if placement in ("left", "right"):
            panel_clips, text_x_frac = build_spotlight_panel(scene, theme, img_path)
            clips.extend(panel_clips)
        else:
            clips = composite_bg_image(clips, scene, theme, img_path)
    max_w = 0.5 if panel_clips else 0.7
    x = int(W * (text_x_frac - max_w / 2))
    if scene.get("heading"):
        h_size = int(theme["font_size"] * 0.8)
        heading = _left_text_clip(scene["heading"], theme, h_size,
                                  color=theme["accent"], bold=True,
                                  max_w_px=int(W * max_w))
        clips.append(heading.with_position((x, int(H * 0.16))))
    n = len(scene["items"])
    b_size = int(theme["font_size"] * 0.55)
    top, bottom = 0.30, 0.85
    step = (bottom - top) / max(n, 1)
    reveal_offsets = reveal_offsets or [i * scene.get("stagger", 0.4)
                                        for i in range(n)]
    for i, item in enumerate(scene["items"]):
        bullet = _left_text_clip("• " + item, theme, b_size,
                                 max_w_px=int(W * max_w))
        y = int(H * (top + i * step))
        offset = reveal_offsets[i] if i < len(reveal_offsets) else i * scene.get("stagger", 0.4)
        remaining = max(0.0, (duration or 0.0) - offset)
        if duration is not None and remaining <= 0:
            continue
        bullet = bullet.with_position((x, y)).with_start(offset)
        if duration is not None:
            bullet = bullet.with_duration(remaining)
        else:
            bullet = bullet.with_duration(60)
        bullet = bullet.with_effects([vfx.FadeIn(min(0.25, remaining or 0.25))])
        clips.append(bullet)
    return CompositeVideoClip(clips, size=(W, H))


def build_image(scene, theme, workdir):
    src = scene["src"]
    img_clip = ImageClip(src)
    scale = max(W / img_clip.w, H / img_clip.h)
    img_clip = img_clip.resized(scale)
    if img_clip.w > W or img_clip.h > H:
        img_clip = img_clip.cropped(x_center=img_clip.w / 2, y_center=img_clip.h / 2,
                                     width=W, height=H)
    img_clip = img_clip.with_position("center")
    clips = [img_clip]
    if scene.get("caption"):
        cap_h = int(H * 0.12)
        cap = centered_text_clip(scene["caption"], theme,
                                 int(theme["font_size"] * 0.5), y_frac=0.92)
        cap_bg = ColorClip(size=(W, cap_h), color=(0, 0, 0)).with_opacity(0.6)
        cap_bg = cap_bg.with_position((0, H - cap_h))
        clips.extend([cap_bg, cap])
    return CompositeVideoClip(clips, size=(W, H))


def build_chart(scene, theme, workdir):
    fig, ax = plt.subplots(figsize=(19.2, 10.8), dpi=100)
    fig.patch.set_facecolor(theme["bg"])
    ax.set_facecolor(theme["bg"])
    ax.tick_params(colors=theme["fg"], labelsize=18)
    for spine in ax.spines.values():
        spine.set_color(theme["fg"])
    if scene.get("chart_type", "bar") == "bar":
        bars = ax.bar(scene["labels"], scene["values"],
                      color=theme["accent"], width=0.55)
    elif scene["chart_type"] == "line":
        ax.plot(scene["labels"], scene["values"], color=theme["accent"],
                linewidth=4, marker="o", markersize=10)
    else:
        ax.pie(scene["values"], labels=scene["labels"], colors=[theme["accent"], theme["fg"], "#888"],
               textprops={"color": theme["fg"], "fontsize": 18}, autopct="%1.0f%%")
    if scene["chart_type"] != "pie":
        if scene.get("y_label"):
            ax.set_ylabel(scene["y_label"], color=theme["fg"], fontsize=20)
        ax.set_title(scene.get("heading", ""), color=theme["fg"],
                     fontsize=28, pad=20)
        ax.grid(axis="y", alpha=0.25, color=theme["fg"])
    fig.tight_layout(pad=3.5)
    out = workdir / f"chart_{hashlib.sha1(json.dumps(scene).encode()).hexdigest()[:10]}.png"
    fig.savefig(out, facecolor=theme["bg"])
    plt.close(fig)
    # Fit inside the frame with a 6% margin on all sides
    margin = 0.06
    img = ImageClip(str(out))
    scale = min(W * (1 - 2 * margin) / img.w, H * (1 - 2 * margin) / img.h)
    img = img.resized(scale)
    x = (W - img.w) // 2
    y = (H - img.h) // 2
    img = img.with_position((x, y))
    return CompositeVideoClip([img], size=(W, H))


def build_outro(scene, theme):
    bg = ColorClip(size=(W, H), color=rgb(theme["bg"]))
    txt = centered_text_clip(scene["text"], theme, theme["title_font_size"],
                             color=theme["accent"], bold=True)
    return CompositeVideoClip([bg, txt], size=(W, H))


BUILDERS = {
    "title": build_title,
    "bullets": build_bullets,
    "image": build_image,
    "chart": build_chart,
    "outro": build_outro,
}


def apply_anim(clip, anim: str, duration: float):
    if anim in ("fade", "kenburns"):
        return clip.with_effects([vfx.FadeIn(ANIM_IN), vfx.FadeOut(ANIM_IN)])
    if anim == "slide-up":
        def pos(t):
            return ("center", H / 2 + min(t / ANIM_IN, 1) * SLIDE_DIST)
        return clip.with_position(pos).with_effects([vfx.FadeIn(ANIM_IN)])
    if anim == "zoom-in":
        return clip.resized(lambda t: 1.0 + min(t / (duration * 0.8), 1) * 0.05) \
                   .with_effects([vfx.FadeIn(ANIM_IN)])
    return clip


def _ffmpeg_exe() -> str:
    from shutil import which
    p = which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def apply_branding(clip, branding, scene_type, duration):
    """Overlay an optional transparent logo without changing scene geometry."""
    if not branding:
        return clip
    scenes = branding.get("scenes", "all")
    if scenes != "all" and scene_type not in scenes:
        return clip
    logo_path = Path(branding["logo"])
    if not logo_path.is_file():
        return clip

    from PIL import Image
    image = Image.open(logo_path).convert("RGBA")
    max_height = max(1, int(H * branding.get("max_height", 0.10)))
    max_width = int(W * 0.22)
    image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    logo = ImageClip(np.asarray(image), transparent=True)
    opacity = branding.get("opacity", 0.85)
    if opacity < 1:
        logo = logo.with_opacity(opacity)
    margin = int(branding.get("margin", 48))
    position = branding.get("position", "bottom-right")
    x = margin if position.endswith("left") else W - margin - logo.w
    y = margin if position.startswith("top") else H - margin - logo.h
    logo = logo.with_position((x, y)).with_duration(duration)
    return CompositeVideoClip([clip, logo], size=(W, H)).with_duration(duration)


def _format_srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _subtitle_chunks(text: str, duration: float, max_chars: int = 42) -> list[str]:
    words = []
    for word in text.split():
        words.extend(word[i:i + max_chars] for i in range(0, len(word), max_chars))
    if not words:
        return []
    target_count = max(1, int(np.ceil(duration / 6.8)), int(np.ceil(len(text) / max_chars)))
    target_count = min(target_count, len(words))
    chunks, current = [], []
    remaining_groups = target_count
    for index, word in enumerate(words):
        candidate = " ".join(current + [word])
        left_words = len(words) - index
        should_break = bool(current) and (
            len(candidate) > max_chars or
            (remaining_groups > 1 and len(current) >= max(1, int(np.ceil(left_words / remaining_groups))))
        )
        if should_break:
            chunks.append(" ".join(current))
            remaining_groups -= 1
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks


def _write_srt(segments, path: Path):
    """Write narration cues to an SRT beside the rendered video."""
    cues = []
    for start, text, duration in segments:
        chunks = _subtitle_chunks(text, duration)
        weights = [max(1, len(chunk)) for chunk in chunks]
        total_weight = sum(weights)
        cursor = start
        allocated = 0.0
        for index, (chunk, weight) in enumerate(zip(chunks, weights)):
            cue_duration = duration * weight / total_weight
            # Allocate the last cue the remainder to avoid rounding drift.
            if index == len(chunks) - 1:
                cue_duration = duration - allocated
            end = cursor + cue_duration
            cues.append((cursor, end, chunk))
            cursor = end
            allocated += cue_duration
    contents = []
    for number, (start, end, text) in enumerate(cues, 1):
        contents.append(f"{number}\n{_format_srt_time(start)} --> {_format_srt_time(end)}\n{text}")
    path.write_text("\n\n".join(contents) + ("\n" if contents else ""), encoding="utf-8")


def _publish_render(staged_video: str, destination: Path):
    """Copy completed artifacts to the requested location after local rendering."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(staged_video)
    shutil.copy2(staged, destination)
    for suffix in (".pptx", ".srt"):
        sidecar = staged.with_suffix(suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, destination.with_suffix(suffix))


def _export_pptx(spec, scene_artifacts, output):
    try:
        import pptx_export
        pptx_path = str(Path(output).with_suffix(".pptx"))
        pptx_export.build_pptx(spec, scene_artifacts, pptx_path)
        print(f"[done] {pptx_path} ({len(scene_artifacts)} slides)")
    except Exception as e:
        print(f"[warn] pptx export failed: {e}")


def render(spec_path: str, output: str, workdir: str | None = None):
    spec = load_spec(spec_path)
    theme = spec["theme"]
    voice_cfg = spec.get("voice", {})
    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = tempfile.TemporaryDirectory(prefix="video-renderer-")
    output = str(Path(staging.name) / destination.name)
    if workdir is None:
        spec_key = hashlib.sha1(str(Path(spec_path).resolve()).encode()).hexdigest()[:12]
        workdir = Path(tempfile.gettempdir()) / f"video-renderer-cache-{spec_key}"
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    video_clips, audios = [], []
    scene_artifacts = []
    t_cursor = 0.0
    fps = spec["meta"]["fps"]
    stream = len(spec["timeline"]) > 12
    seg_dir = Path(staging.name) / "segments"
    subtitle_segments = []
    branding = spec.get("branding")
    if branding:
        logo = Path(branding["logo"])
        if not logo.is_absolute():
            logo = Path(spec_path).resolve().parent / logo
        branding = dict(branding, logo=str(logo))
        if not logo.is_file():
            print(f"[warn] branding logo not found at {logo}; continuing without watermark")
            branding = None
    if stream:
        seg_dir.mkdir(exist_ok=True)
        print(f"[stream] long timeline ({len(spec['timeline'])} scenes); rendering per-scene segments")
    for i, scene in enumerate(spec["timeline"]):
        stype = scene["type"]
        try:
            audio_path, audio_dur, speech_segments = synth_narration(
                scene, workdir, voice_cfg)
        except RuntimeError as e:
            if "MISTRAL_API_KEY" in str(e):
                print(f"[warn] {e} — rendering scene {i} without narration")
                audio_path, audio_dur = None, 0.0
                speech_segments = [(text, 0.0) for text in
                                   (scene.get("narration_per_bullet") or
                                    ([scene["narration"]] if scene.get("narration") else []))]
            else:
                raise

        dur = scene_duration(scene, audio_dur)
        builder = BUILDERS[stype]
        if stype == "bullets":
            if scene.get("narration_per_bullet") and audio_path:
                reveal_offsets = []
                elapsed = 0.0
                for _, segment_duration in speech_segments:
                    reveal_offsets.append(elapsed)
                    elapsed += segment_duration
            else:
                reveal_offsets = [n * scene.get("stagger", 0.4)
                                  for n in range(len(scene["items"]))]
            clip = builder(scene, theme, workdir, reveal_offsets, dur)
        elif stype in ("image", "chart", "title"):
            clip = builder(scene, theme, workdir)
        else:
            clip = builder(scene, theme)

        clip = clip.with_duration(dur)
        clip = apply_anim(clip, scene.get("anim", "fade"), dur)
        # Animations like zoom-in resize frames over time even though the
        # clip's nominal size stays 1920x1080; composite onto the fixed canvas
        # so encoding always sees constant frame dimensions (variable-size
        # streams decode as garbage/static in players).
        if scene.get("anim") in ("zoom-in", "kenburns"):
            clip = CompositeVideoClip([clip.with_position("center")], size=(W, H))\
                .with_duration(dur)

        clip = apply_branding(clip, branding, stype, dur)

        if audio_path:
            audios.append(AudioFileClip(audio_path).with_start(t_cursor))

        # Bound memory on long timelines: render each scene straight to a
        # disk segment and release it, instead of holding every composite.
        if stream:
            seg = seg_dir / f"seg_{i:03d}.mp4"
            clip.write_videofile(str(seg), fps, codec="libx264", logger=None)
            clip.close()
            del clip
        else:
            video_clips.append(clip)

        art = {}
        if audio_path:
            art["narration_path"] = audio_path
        img_path = scene_image_clip(scene, theme, workdir) if scene.get("image") else None
        if stype == "chart":
            art["chart_png"] = str(workdir / f"chart_{hashlib.sha1(json.dumps(scene).encode()).hexdigest()[:10]}.png")
        if img_path:
            if scene.get("image", {}).get("placement", "background") in ("left", "right"):
                art["image_png"] = img_path
            else:
                art["bg_image_png"] = img_path
                scrim, _ = imggen.scrim_for_contrast(
                    img_path, theme["fg"], theme["bg"], img_opacity=scene["image"].get("opacity", 0.25))
                art["scrim"] = scrim
        if stype == "image":
            art["image_png"] = scene["src"]
        scene_artifacts.append(art)

        if speech_segments:
            elapsed = 0.0
            for text, segment_duration in speech_segments:
                if segment_duration <= 0:
                    segment_duration = max(0.8, len(text.split()) / 2.5)
                cue_duration = min(segment_duration, max(0.0, dur - elapsed))
                if cue_duration > 0:
                    subtitle_segments.append((t_cursor + elapsed, text, cue_duration))
                elapsed += segment_duration

        print(f"[scene {i}] {stype}: {dur:.2f}s starting at {t_cursor:.2f}s"
              + (f" (narration {audio_dur:.2f}s)" if audio_path else ""))
        t_cursor += dur

    if stream:
        # Concatenate segments with the ffmpeg concat demuxer (O(1) memory;
        # MoviePy re-reading 31 segment files OOMs). Identical codecs/params
        # make stream copy safe.
        seg_files = sorted(seg_dir.glob("seg_*.mp4"))
        concat_list = seg_dir / "concat.txt"
        concat_list.write_text("".join("file '" + str(p.resolve()) + "'\n" for p in seg_files))
        ffmpeg = _ffmpeg_exe()
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", str(concat_list), "-c", "copy", output]
        subprocess.run(cmd, check=True)
        # Mux narration: one pass with all audio offsets as inputs
        if audios:
            mixed = workdir / "narration_all.wav"
            CompositeAudioClip(audios).write_audiofile(str(mixed), logger=None)
            tmp = str(Path(output).with_suffix(".mux.mp4"))
            subprocess.run([ffmpeg, "-y", "-loglevel", "error",
                           "-i", output, "-i", str(mixed),
                           "-c:v", "copy", "-c:a", "aac",
                           "-map", "0:v", "-map", "1:a",
                           "-shortest", tmp], check=True)
            Path(tmp).replace(output)
        _export_pptx(spec, scene_artifacts, output)
        if subtitle_segments:
            _write_srt(subtitle_segments, Path(output).with_suffix(".srt"))
        _publish_render(output, destination)
        staging.cleanup()
        print(f"[done] {destination} ({t_cursor:.1f}s at {fps}fps, streamed)")
        return

    final = concatenate_videoclips(video_clips, method="compose")
    if audios:
        final = final.with_audio(CompositeAudioClip(audios))
    final.write_videofile(output, fps=fps, codec="libx264",
                          audio_codec="aac", logger=None)
    _export_pptx(spec, scene_artifacts, output)
    if subtitle_segments:
        _write_srt(subtitle_segments, Path(output).with_suffix(".srt"))
    _publish_render(output, destination)
    staging.cleanup()
    print(f"[done] {destination} ({t_cursor:.1f}s at {fps}fps)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("spec")
    p.add_argument("-o", "--output", default="output/video.mp4")
    p.add_argument("--workdir", default=None)
    p.add_argument("--validate", action="store_true",
                   help="only validate the spec; do not render")
    args = p.parse_args()
    try:
        spec = load_spec(args.spec)
    except SpecError as e:
        print(report(args.spec, e), file=sys.stderr)
        sys.exit(1)
    if args.validate:
        print(f"[spec] {args.spec}: OK ({len(spec['timeline'])} scenes)")
        sys.exit(0)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    render(args.spec, args.output, args.workdir)
