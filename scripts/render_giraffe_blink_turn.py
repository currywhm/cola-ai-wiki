#!/usr/bin/env python3
"""Render a subtle blink-and-head-turn animation from the supplied portrait."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter


EYES = (
    # bbox, color-sampling offset, eyelid endpoints and curve depth
    ((148, 281, 214, 378), -90, ((157, 323), (205, 323), 17)),
    ((327, 300, 393, 397), None, ((336, 343), (384, 343), 17)),
)


def _elliptical_mask(size: tuple[int, int], box: tuple[int, int, int, int]) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    x0, y0, x1, y1 = box
    draw.ellipse((x0 - 4, y0 - 4, x1 + 4, y1 + 4), fill=255)
    return mask


def _curve_points(
    start: tuple[int, int],
    end: tuple[int, int],
    depth: int,
    samples: int = 20,
) -> list[tuple[int, int]]:
    """Return a smooth quadratic curve between two eyelid endpoints."""
    x0, y0 = start
    x1, y1 = end
    cx = (x0 + x1) / 2
    cy = ((y0 + y1) / 2) + depth
    points = []
    for index in range(samples + 1):
        t = index / samples
        inv = 1 - t
        x = inv * inv * x0 + 2 * inv * t * cx + t * t * x1
        y = inv * inv * y0 + 2 * inv * t * cy + t * t * y1
        points.append((round(x), round(y)))
    return points


def render_blink(source: Image.Image, closure: float) -> Image.Image:
    """Apply one blink state, where 0 is open and 1 is fully closed."""
    closure = max(0.0, min(1.0, closure))
    if closure == 0:
        return source.copy()

    result = source.copy()
    size = source.size

    for box, sample_offset, eyelid in EYES:
        x0, y0, x1, y1 = box
        eye_mask = _elliptical_mask(size, box)

        # Sample adjacent face color rather than inventing a flat fill. This
        # preserves the yellow-to-cream shading across the lower eye area.
        if sample_offset is not None:
            sampled_face = ImageChops.offset(source, sample_offset, 0)
        else:
            strip = Image.new("RGB", (1, max(2, y1 - y0)))
            strip_pixels = strip.load()
            top_color = (254, 230, 128)
            bottom_color = (254, 224, 115)
            for strip_y in range(strip.height):
                mix = strip_y / max(1, strip.height - 1)
                strip_pixels[0, strip_y] = tuple(
                    round(top + (bottom - top) * mix)
                    for top, bottom in zip(top_color, bottom_color)
                )
            sampled_face = strip.resize(size, Image.Resampling.BILINEAR)
        if closure < 1:
            coverage = y0 + (y1 - y0) * (0.08 + closure * 0.82)
            vertical_mask = Image.new("L", size, 0)
            vertical_draw = ImageDraw.Draw(vertical_mask)
            vertical_draw.rectangle((x0 - 5, y0 - 5, x1 + 5, coverage), fill=255)
            eye_mask = ImageChops.multiply(eye_mask, vertical_mask)
        else:
            coverage = y1

        eye_mask = eye_mask.filter(ImageFilter.GaussianBlur(1.1))
        result.paste(sampled_face, (0, 0), eye_mask)

        if closure < 0.18:
            continue

        base_lid = _curve_points(
            eyelid[0],
            eyelid[1],
            eyelid[2],
        )
        if closure < 1:
            # During the closing motion, move the upper lid down in step with
            # the sampled skin patch while retaining a slightly asymmetric arc.
            progress = (closure - 0.18) / 0.82
            shift = round((1 - progress) * -18 + progress * 8)
            points = [(x, y + shift) for x, y in base_lid]
            lid_width = max(3, round(6 - closure))
        else:
            points = base_lid
            lid_width = 6

        draw = ImageDraw.Draw(result)
        draw.line(points, fill=(60, 36, 20, 255), width=lid_width, joint="curve")
        radius = max(1, lid_width // 2)
        for endpoint in (points[0], points[-1]):
            draw.ellipse(
                (
                    endpoint[0] - radius,
                    endpoint[1] - radius,
                    endpoint[0] + radius,
                    endpoint[1] + radius,
                ),
                fill=(60, 36, 20, 255),
            )

    return result


def shear_head(
    source: Image.Image,
    top_shift: float,
    tilt_degrees: float = 0.0,
) -> Image.Image:
    """Shift the top of the portrait and anchor the neck at the bottom edge."""
    width, height = source.size
    # Pillow inverse affine mapping: x_in = x_out - shift + shift * y / height.
    affine = (1, top_shift / height, -top_shift, 0, 1, 0)
    moved = source.transform(
        source.size,
        Image.Transform.AFFINE,
        affine,
        resample=Image.Resampling.BICUBIC,
        fillcolor=(255, 255, 255),
    )
    if tilt_degrees:
        moved = moved.rotate(
            tilt_degrees,
            center=(450, 650),
            resample=Image.Resampling.BICUBIC,
            fillcolor=(255, 255, 255),
        )
    return moved


def blink_amount(progress: float) -> float:
    """Create a quick double-frame blink around 56% through the loop."""
    center = 0.56
    half_width = 0.055
    distance = abs(progress - center)
    if distance >= half_width:
        return 0.0
    pulse = math.cos((distance / half_width) * math.pi / 2)
    return pulse**0.45


def render_frames(source: Image.Image, frame_count: int, output_size: int) -> list[Image.Image]:
    frames: list[Image.Image] = []
    for index in range(frame_count):
        progress = index / frame_count
        sway = math.sin(progress * math.tau)
        top_shift = 18.0 * sway
        animated = render_blink(source, blink_amount(progress))
        animated = shear_head(animated, top_shift, -0.65 * sway)
        if output_size != source.width:
            animated = animated.resize(
                (output_size, output_size),
                Image.Resampling.LANCZOS,
            )
        frames.append(animated.convert("RGB"))
    return frames


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--preview-dir", type=Path, default=Path("preview"))
    parser.add_argument("--size", type=int, default=520)
    parser.add_argument("--frames", type=int, default=45)
    parser.add_argument("--duration", type=int, default=70)
    args = parser.parse_args()

    source = Image.open(args.source).convert("RGBA")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.preview_dir.mkdir(parents=True, exist_ok=True)

    # QA stills make the facial edits easy to inspect without stepping a GIF.
    closed = render_blink(source, 1.0)
    half_closed = render_blink(source, 0.55)
    sheared = shear_head(source, 18, -0.65)
    closed.resize((args.size, args.size), Image.Resampling.LANCZOS).save(
        args.preview_dir / "giraffe_blink_closed.png"
    )
    half_closed.resize((args.size, args.size), Image.Resampling.LANCZOS).save(
        args.preview_dir / "giraffe_blink_half.png"
    )
    sheared.resize((args.size, args.size), Image.Resampling.LANCZOS).save(
        args.preview_dir / "giraffe_head_turned.png"
    )

    frames = render_frames(source, args.frames, args.size)
    gif_path = args.output_dir / "giraffe_blink_turn.gif"
    webp_path = args.output_dir / "giraffe_blink_turn.webp"
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
