"""Build a .pptx from the render artifacts: one slide per scene, with each
scene's narration WAV embedded on its slide.

Built from the individual artifacts (spec, narration clips, chart PNGs,
generated scene images) — never from the rendered video.
"""
import json
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_MARKER_STYLE
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)


def _rgb(hex_color: str) -> RGBColor:
    h = hex_color.lstrip("#")
    return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _add_native_chart(slide, scene: dict, bg: RGBColor,
                      fg: RGBColor, accent: RGBColor) -> bool:
    """Add an editable PowerPoint chart. Return False if it cannot be built."""
    chart_types = {
        "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "line": XL_CHART_TYPE.LINE_MARKERS,
        "pie": XL_CHART_TYPE.PIE,
    }
    try:
        data = CategoryChartData()
        data.categories = scene["labels"]
        data.add_series(scene.get("heading") or "Series 1", scene["values"])
        graphic = slide.shapes.add_chart(
            chart_types.get(scene.get("chart_type", "bar")),
            left=Inches(0.8), top=Inches(0.8),
            width=SLIDE_W - Inches(1.6), height=SLIDE_H - Inches(1.6),
            chart_data=data,
        )
        chart = graphic.chart
        chart.has_legend = False
        if scene.get("heading"):
            chart.has_title = True
            chart.chart_title.text_frame.text = scene["heading"]
            for paragraph in chart.chart_title.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.color.rgb = fg
        # A filled chart area keeps the theme background instead of PowerPoint's
        # default white box. Fall back to the slide fill if a version lacks a
        # formatting hook; the chart itself remains editable.
        for area in (chart, chart.plots[0]):
            try:
                area.format.fill.solid()
                area.format.fill.fore_color.rgb = bg
                area.format.line.fill.background()
            except (AttributeError, TypeError):
                pass

        series = chart.series[0]
        if scene.get("chart_type", "bar") == "pie":
            palette = [accent, fg, _rgb("#8b949e")]
            for i, point in enumerate(series.points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = palette[i % len(palette)]
            labels = chart.plots[0].data_labels
            labels.position = XL_LABEL_POSITION.BEST_FIT
            labels.show_category_name = True
            labels.show_percentage = True
            labels.font.color.rgb = fg
        else:
            if scene.get("chart_type", "bar") == "line":
                series.format.line.color.rgb = accent
                series.marker.style = XL_MARKER_STYLE.CIRCLE
                series.marker.size = 8
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = accent
            chart.category_axis.tick_labels.font.color.rgb = fg
            chart.value_axis.tick_labels.font.color.rgb = fg
            if scene.get("y_label"):
                chart.value_axis.has_title = True
                chart.value_axis.axis_title.text_frame.text = scene["y_label"]
                for paragraph in chart.value_axis.axis_title.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.color.rgb = fg
            try:
                chart.value_axis.major_gridlines.format.line.color.rgb = _rgb("#30363d")
            except (AttributeError, TypeError):
                pass
        return True
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        print(f"[warn] could not make editable PPTX chart ({exc}); using chart image")
        return False


def build_pptx(spec: dict, scene_artifacts: list, output_path: str):
    """scene_artifacts: per-scene dicts with optional keys
    narration_path, chart_png, image_png, bg_image_png, scrim (0-1)."""
    theme = spec.get("theme", {})
    bg = _rgb(theme.get("bg", "#0d1117"))
    fg = _rgb(theme.get("fg", "#f0f6fc"))
    accent = _rgb(theme.get("accent", "#58a6ff"))

    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H
    blank = prs.slide_layouts[6]

    for scene, art in zip(spec["timeline"], scene_artifacts):
        slide = prs.slides.add_slide(blank)

        # background fill (or faded image background)
        if art.get("bg_image_png"):
            slide.shapes.add_picture(art["bg_image_png"], 0, 0,
                                     width=SLIDE_W, height=SLIDE_H)
            scrim = art.get("scrim", 0.5)
            box = slide.shapes.add_shape(1, 0, 0, SLIDE_W, SLIDE_H)  # rectangle
            box.fill.solid()
            box.fill.fore_color.rgb = bg
            box.fill.transparency = 1.0 - scrim  # pptx: transparency of the fill
            box.line.fill.background()
        else:
            box = slide.shapes.add_shape(1, 0, 0, SLIDE_W, SLIDE_H)
            box.fill.solid()
            box.fill.fore_color.rgb = bg
            box.line.fill.background()

        stype = scene["type"]
        if stype in ("title", "outro"):
            tb = slide.shapes.add_textbox(Inches(1), Inches(2.6),
                                          SLIDE_W - Inches(2), Inches(1.5))
            tf = tb.text_frame
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            r = p.add_run()
            r.text = scene["text"]
            f = r.font
            f.size = Pt(scene.get("_title_pt", 44))
            f.bold = True
            f.color.rgb = accent if stype == "outro" else fg
            if scene.get("subtitle"):
                p2 = tf.add_paragraph()
                p2.alignment = PP_ALIGN.CENTER
                r2 = p2.add_run()
                r2.text = scene["subtitle"]
                r2.font.size = Pt(20)
                r2.font.color.rgb = accent
        elif stype == "bullets":
            has_panel = art.get("image_png")
            text_w = Inches(5.6) if has_panel else SLIDE_W - Inches(2.4)
            tx = Inches(1.2)
            if scene.get("heading"):
                hb = slide.shapes.add_textbox(tx, Inches(0.7), text_w, Inches(0.9))
                hp = hb.text_frame.paragraphs[0]
                hr = hp.add_run()
                hr.text = scene["heading"]
                hr.font.size = Pt(28)
                hr.font.bold = True
                hr.font.color.rgb = accent
            bb = slide.shapes.add_textbox(tx, Inches(1.9), text_w,
                                          SLIDE_H - Inches(2.6))
            btf = bb.text_frame
            for i, item in enumerate(scene.get("items", [])):
                p = btf.paragraphs[0] if i == 0 else btf.add_paragraph()
                p.alignment = PP_ALIGN.LEFT
                r = p.add_run()
                r.text = "\u2022 " + item
                r.font.size = Pt(18)
                r.font.color.rgb = fg
                p.space_after = Pt(12)
            if has_panel:
                slide.shapes.add_picture(
                    art["image_png"],
                    left=SLIDE_W - Inches(5.4), top=Inches(1.2),
                    width=Inches(4.8), height=Inches(4.8))
        elif stype == "chart":
            if not _add_native_chart(slide, scene, bg, fg, accent) and art.get("chart_png"):
                slide.shapes.add_picture(
                    art["chart_png"],
                    left=Inches(0.8), top=Inches(0.8),
                    width=SLIDE_W - Inches(1.6),
                    height=SLIDE_H - Inches(1.6))
        elif stype == "image" and art.get("image_png"):
            slide.shapes.add_picture(art["image_png"], 0, 0,
                                     width=SLIDE_W, height=SLIDE_H)
            if scene.get("caption"):
                cb = slide.shapes.add_textbox(
                    Inches(1), SLIDE_H - Inches(1.1),
                    SLIDE_W - Inches(2), Inches(0.7))
                cp = cb.text_frame.paragraphs[0]
                cp.alignment = PP_ALIGN.CENTER
                cr = cp.add_run()
                cr.text = scene["caption"]
                cr.font.size = Pt(16)
                cr.font.color.rgb = fg

        # Keep the script available to presenters in PowerPoint's notes pane.
        # The renderer speaks per-bullet narration in preference to the scene
        # narration, so mirror that precedence here and keep each clip on its
        # own line. Accessing notes_slide creates it only when there is text.
        per_bullet = scene.get("narration_per_bullet")
        if per_bullet:
            notes_text = "\n".join(per_bullet)
        else:
            notes_text = scene.get("narration", "")
        if notes_text:
            slide.notes_slide.notes_text_frame.text = notes_text

        # embed narration audio on the slide
        if art.get("narration_path"):
            slide.shapes.add_movie(
                art["narration_path"],
                left=SLIDE_W - Inches(1.05), top=SLIDE_H - Inches(1.05),
                width=Inches(0.8), height=Inches(0.8),
                poster_frame_image=None, mime_type="audio/wav"
            )

    prs.save(output_path)
    return output_path
