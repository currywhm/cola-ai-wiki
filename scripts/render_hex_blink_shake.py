#!/usr/bin/env python3
"""Render a blink-and-shake loop from the supplied black-and-white character."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter


FACE_WHITE = (254, 254, 254, 255)
INK = (24, 24, 26, 255)

EYES = (
    {
        "box": (385, 572, 532, 730),
        "curve": ((402, 674), (516, 674), (459, 624)),
    },
    {
        "box": (652, 524, 801, 692),
        "curve": ((668, 636), (787, 636), (727, 582)),
    },
)


def _quadratic_curve(
    start: tuple[int, int],
    control: tuple[int, int],
    end: tuple[int, int],
    samples: int = 32,
) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for index in range(samples + 1):
        t = index / samples
        inverse = 1 - t
        x = (
            inverse * inverse * start[0]
            + 2 * inverse * t * control[0]
            + t * t * end[0]
        )
        y = (
            inverse * inverse * start[1]
            + 2 * inverse * t * control[1]
            + t * t * end[1]
        )
        points.append((round(x), round(y)))
    return points


def render_blink(source: Image.Image, closure: float) -> Image.Image:
    """Return one blink state, where 0 is open and 1 is fully closed."""
    closure = max(0.0, min(1.0, closure))
    if closure <= 0:
        return source.copy()

    result = source.copy()
    size = source.size

    for eye in EYES:
        x0, y0, x1, y1 = eye["box"]
        eye_mask = Image.new("L", size, 0)
        mask_draw = ImageDraw.Draw(eye_mask)
        mask_draw.ellipse((x0 - 5, y0 - 5, x1 + 5, y1 + 5), fill=255)

        if closure < 1:
            coverage = y0 + (y1 - y0) * (0.10 + closure * 0.86)
            vertical_mask = Image.new("L", size, 0)
            vertical_draw = ImageDraw.Draw(vertical_mask)
            vertical_draw.rectangle((x0 - 6, y0 - 6, x1 + 6, coverage), fill=255)
            eye_mask = ImageChops.multiply(eye_mask, vertical_mask)

        eye_mask = eye_mask.filter(ImageFilter.GaussianBlur(1.5))
        result.paste(Image.new("RGBA", size, FACE_WHITE), (0, 0), eye_mask)

        if closure < 0.16:
            continue

        start, control, end = eye["curve"]
        points = _quadratic_curve(start, control, end)
        if closure < 1:
            progress = (closure - 0.16) / 0.84
            shift = round((1 - progress) * -35)
            points = [(x, y + shift) for x, y in points]
            line_width = max(11, round(22 - closure * 5))
        else:
            line_width = 22

        draw = ImageDraw.Draw(result)
        draw.line(points, fill=INK, width=line_width, joint="curve")
        radius = max(2, line_width // 2)
        for endpoint in (points[0], points[-1]):
            draw.ellipse(
                (
                    endpoint[0] - radius,
                    endpoint[1] - radius,
                    endpoint[0] + radius,
                    endpoint[1] + radius,
                ),
                fill=INK,
            )

    return result


def blink_amount(progress: float) -> float:
    """Create one quick blink around 29% through the loop."""
    center = 0.29
    half_width = 0.068
    distance = abs(progress - center)
    if distance >= half_width:
        return 0.0
    pulse = math.cos((distance / half_width) * math.pi / 2)
    return pulse**0.42


def motion(progress: float) -> tuple[float, float, float]:
    """Return translation and rotation for a calm sway plus a shake burst."""
    phase = progress * math.tau
    calm_x = 4.0 * math.sin(phase)
    calm_y = 2.5 * math.sin(phase * 2 + 0.45)
    calm_rotation = 0.55 * math.sin(phase + 0.7)

    shake_center = 0.70
    shake_half_width = 0.17
    shake_distance = abs(progress - shake_center)
    if shake_distance < shake_half_width:
        envelope = math.cos((shake_distance / shake_half_width) * math.pi / 2) ** 2
        shake_x = 18.0 * envelope * math.sin(phase * 13 + 0.3)
        shake_y = 11.0 * envelope * math.sin(phase * 17 + 1.1)
        shake_rotation = 2.0 * envelope * math.sin(phase * 11 + 0.6)
    else:
        shake_x = shake_y = shake_rotation = 0.0

    return (
        calm_x + shake_x,
        calm_y + shake_y,
        calm_rotation + shake_rotation,
    )


def render_frames(source: Image.Image, frame_count: int, output_size: int) -> list[Image.Image]:
    frames: list[Image.Image] = []
    center = (source.width / 2, source.height / 2)

    for index in range(frame_count):
        progress = index / frame_count
        frame = render_blink(source, blink_amount(progress))
        offset_x, offset_y, rotation = motion(progress)
        frame = frame.rotate(
            rotation,
            center=center,
            translate=(offset_x, offset_y),
            resample=Image.Resampling.BICUBIC,
            fillcolor=FACE_WHITE,
        )
        frame = frame.resize((output_size, output_size), Image.Resampling.LANCZOS)
        frames.append(frame.convert("RGB"))

    return frames


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--preview-dir", type=Path, default=Path("preview"))
    parser.add_argument("--size", type=int, default=600)
    parser.add_argument("--frames", type=int, default=48)
    parser.add_argument("--duration", type=int, default=70)
    args = parser.parse_args()

    source = Image.open(args.source).convert("RGBA")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.preview_dir.mkdir(parents=True, exist_ok=True)

    for name, closure in (("open", 0.0), ("half", 0.48), ("closed", 1.0)):
        frame = render_blink(source, closure)
        frame.resize((args.size, args.size), Image.Resampling.LANCZOS).save(
            args.preview_dir / f"hex_blink_{name}.png"
        )

    shake_progress = 0.70
    shake = render_blink(source, blink_amount(shake_progress))
    offset_x, offset_y, rotation = motion(shake_progress)
    shake = shake.rotate(
        rotation,
        center=(source.width / 2, source.height / 2),
        translate=(offset_x, offset_y),
        resample=Image.Resampling.BICUBIC,
        fillcolor=FACE_WHITE,
    )
    shake.resize((args.size, args.size), Image.Resampling.LANCZOS).save(
        args.preview_dir / "hex_shake_peak.png"
    )

    frames = render_frames(source, args.frames, args.size)
    gif_path = args.output_dir / "hex_blink_shake.gif"
    webp_path = args.output_dir / "hex_blink_shake.webp"

    frames[0].save(
        gif_path,
        save_all=True,
        append_images=frames[1:],
        duration=args.duration,
        loop=0,
        optimize=True,
        disposal=2,
        interlace=True,
    )
    frames[0].save(
        webp_path,
        save_all=True,
        append_images=frames[1:],
        duration=args.duration,
        loop=0,
        lossless=False,
        quality=92,
        method=6,
    )

    print(gif_path.resolve())
    print(webp_path.resolve())


if __name__ == "__main__":
    main()
