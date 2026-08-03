#!/usr/bin/env python3
"""
OTP Pad Generator

Generates one-time pad sets as pocket-sized PDFs ready for printing.
Each set is a PDF containing pages of random key material.
Print each PDF twice to get your A and B copies.

Usage:
    python3 otp_generator.py --codewords words.txt --sets 10 --pages 1000 --output ./pads
    python3 otp_generator.py --codewords words.txt --sets 10 --pages 1000 --a7 --output ./pads

Arguments:
    --codewords   Path to file with one codeword per line
    --sets        Number of paired sets to generate (default: 10)
    --pages       Number of pages per set (default: 1000)
    --output      Output directory for PDFs (default: ./output)
    --chars       Characters of key material per pad page (default: 665 for A6, 375 for A7)
    --fontsize    Font size in points (default: 9)
    --a7          Layout two pad pages per A6 sheet (cut along the dashed line to get A7)
    --no-auth     Omit the AUTH group from page headers

Each page header carries an AUTH group: five extra key letters reserved for
message authentication, generated alongside the key body but never part of it.
See the manual (otp.md, Authentication) for the procedure it supports.
"""

import argparse
import os
import sys
from reportlab.lib.units import mm
from reportlab.lib.pagesizes import A6
from reportlab.pdfgen import canvas


# A6 = 105mm x 148mm (the paper we print on)
SHEET_WIDTH, SHEET_HEIGHT = A6

# Group formatting
GROUP_SIZE = 5
A6_GROUPS_PER_ROW = 7
A6_CHARS_PER_ROW = A6_GROUPS_PER_ROW * GROUP_SIZE  # 35
A7_GROUPS_PER_ROW = 5
A7_CHARS_PER_ROW = A7_GROUPS_PER_ROW * GROUP_SIZE  # 25


def get_random_bytes(n: int) -> bytes:
    """Return n bytes from the operating system's CSPRNG."""
    return os.urandom(n)


def generate_random_letters(count: int) -> str:
    """
    Generate `count` uniformly random uppercase letters A-Z
    using rejection sampling over CSPRNG bytes for unbiased output.
    """
    letters = []
    while len(letters) < count:
        raw = get_random_bytes((count - len(letters)) * 2)
        for byte in raw:
            if len(letters) >= count:
                break
            # Rejection sampling: 26 * 9 = 234, reject 234-255
            if byte < 234:
                letters.append(chr(65 + (byte % 26)))
    return "".join(letters)


def load_codewords(filepath: str) -> list[str]:
    """Load codewords from file, one per line, stripped and uppercased."""
    try:
        with open(filepath, "r") as f:
            words = [line.strip().upper() for line in f if line.strip()]
    except OSError as e:
        print(f"ERROR: Cannot read codewords file: {e}")
        sys.exit(1)
    return words


def draw_pad_page(
    c: canvas.Canvas,
    codeword: str,
    page_num: int,
    chars_per_page: int,
    font_size: float,
    groups_per_row: int,
    chars_per_row: int,
    box_left: float,
    box_bottom: float,
    box_width: float,
    box_height: float,
    with_auth: bool = True,
):
    """Draw a single pad page within the given bounding box."""
    margin_h = 4 * mm
    margin_top = 5 * mm
    margin_bottom = 4 * mm

    left = box_left + margin_h
    right = box_left + box_width - margin_h
    content_width = right - left

    # Key material: body plus an optional AUTH group, reserved for
    # authentication and excluded from the key body (see otp.md)
    extra = GROUP_SIZE if with_auth else 0
    key_chars = generate_random_letters(chars_per_page + extra)
    body_chars = key_chars[:chars_per_page]
    auth_group = key_chars[chars_per_page:] if with_auth else None

    # Header
    y = box_bottom + box_height - margin_top
    c.setFont("Courier-Bold", font_size)
    c.drawString(left, y, codeword)
    if auth_group:
        c.drawCentredString(box_left + box_width / 2, y, f"AUTH {auth_group}")
    c.drawRightString(right, y, f"{page_num:04d}")

    # Separator
    y -= 2 * mm
    c.setLineWidth(0.3)
    c.line(left, y, right, y)
    header_bottom = y

    # Footer
    footer_y = box_bottom + margin_bottom
    c.setFont("Courier-Bold", 5.5)
    footer_text = "USE ONCE \u2014 DESTROY AFTER USE"
    c.drawCentredString(box_left + box_width / 2, footer_y - 1 * mm, footer_text)
    footer_top = footer_y + 1 * mm

    # Body: distribute rows evenly vertically
    num_rows = -(-chars_per_page // chars_per_row)
    body_height = header_bottom - footer_top

    if num_rows > 1:
        row_spacing = body_height / (num_rows + 1)
    else:
        row_spacing = body_height / 2

    min_spacing = font_size * 0.38 * mm
    row_spacing = max(row_spacing, min_spacing)

    # Shading
    shade_height = font_size * 0.45 * mm
    shade_color = 0.88

    # Measure group width for horizontal distribution
    c.setFont("Courier", font_size)
    single_group_width = c.stringWidth("X" * GROUP_SIZE, "Courier", font_size)

    # Horizontal spacing: distribute groups evenly across content width
    if groups_per_row > 1:
        total_groups_width = single_group_width * groups_per_row
        total_gap = content_width - total_groups_width
        group_gap = total_gap / (groups_per_row - 1)
    else:
        group_gap = 0

    all_groups = [body_chars[i:i + GROUP_SIZE] for i in range(0, len(body_chars), GROUP_SIZE)]

    y = header_bottom - row_spacing
    group_idx = 0

    for row_num in range(num_rows):
        # Alternating row shading
        if row_num % 2 == 1:
            c.saveState()
            c.setFillGray(shade_color)
            c.rect(
                left - 1 * mm,
                y - shade_height * 0.3,
                content_width + 2 * mm,
                shade_height,
                fill=1, stroke=0,
            )
            c.restoreState()

        # Draw each group at calculated x position
        c.setFont("Courier", font_size)
        for g in range(groups_per_row):
            if group_idx >= len(all_groups):
                break
            x = left + g * (single_group_width + group_gap)
            c.drawString(x, y, all_groups[group_idx])
            group_idx += 1

        y -= row_spacing


def generate_set_pdf_a6(
    output_path: str,
    codeword: str,
    num_pages: int,
    chars_per_page: int,
    font_size: float,
    with_auth: bool = True,
):
    """Generate OTP set as A6 pages (one pad page per sheet)."""
    c = canvas.Canvas(output_path, pagesize=A6)
    c.setTitle(f"OTP \u2014 {codeword}")

    for page_num in range(1, num_pages + 1):
        draw_pad_page(
            c, codeword, page_num, chars_per_page, font_size,
            groups_per_row=A6_GROUPS_PER_ROW,
            chars_per_row=A6_CHARS_PER_ROW,
            box_left=0, box_bottom=0,
            box_width=SHEET_WIDTH, box_height=SHEET_HEIGHT,
            with_auth=with_auth,
        )
        c.showPage()

        if page_num % 100 == 0:
            print(f"  [{codeword}] {page_num}/{num_pages} pages generated")

    c.save()


def generate_set_pdf_a7(
    output_path: str,
    codeword: str,
    num_pages: int,
    chars_per_page: int,
    font_size: float,
    with_auth: bool = True,
):
    """Generate OTP set as A7 — two pad pages side by side on landscape A6."""
    # Landscape A6: 148mm wide x 105mm tall
    landscape_a6 = (SHEET_HEIGHT, SHEET_WIDTH)
    c = canvas.Canvas(output_path, pagesize=landscape_a6)
    c.setTitle(f"OTP \u2014 {codeword}")

    sheet_w, sheet_h = landscape_a6
    half_width = sheet_w / 2

    page_num = 1
    while page_num <= num_pages:
        # Left half
        draw_pad_page(
            c, codeword, page_num, chars_per_page, font_size,
            groups_per_row=A7_GROUPS_PER_ROW,
            chars_per_row=A7_CHARS_PER_ROW,
            box_left=0, box_bottom=0,
            box_width=half_width, box_height=sheet_h,
            with_auth=with_auth,
        )

        # Right half (if it exists)
        if page_num + 1 <= num_pages:
            draw_pad_page(
                c, codeword, page_num + 1, chars_per_page, font_size,
                groups_per_row=A7_GROUPS_PER_ROW,
                chars_per_row=A7_CHARS_PER_ROW,
                box_left=half_width, box_bottom=0,
                box_width=half_width, box_height=sheet_h,
                with_auth=with_auth,
            )

        # Cut line: dashed vertical line down the middle
        c.saveState()
        c.setStrokeGray(0.5)
        c.setLineWidth(0.3)
        c.setDash(2, 2)
        c.line(half_width, 0, half_width, sheet_h)
        c.restoreState()

        c.showPage()

        if page_num % 100 == 0 or (page_num + 1) % 100 == 0:
            print(f"  [{codeword}] {min(page_num + 1, num_pages)}/{num_pages} pages generated")

        page_num += 2

    c.save()


def calc_max_chars(font_size: float, a7: bool = False) -> int:
    """Calculate maximum characters that fit on one pad page."""
    margin_top = 5 * mm
    margin_bottom = 4 * mm
    header_space = 4 * mm
    footer_space = 2 * mm

    if a7:
        # A7 portrait: 74mm wide x 105mm tall (half of landscape A6)
        page_height = SHEET_WIDTH  # 105mm
        chars_per_row = A7_CHARS_PER_ROW
    else:
        page_height = SHEET_HEIGHT
        chars_per_row = A6_CHARS_PER_ROW

    available = page_height - margin_top - margin_bottom - header_space - footer_space
    min_spacing = font_size * 0.38 * mm
    max_rows = int(available / min_spacing)
    return max_rows * chars_per_row


def main():
    parser = argparse.ArgumentParser(description="Generate OTP pad sets as pocket-sized PDFs")
    parser.add_argument("--codewords", required=True, help="Path to codewords file (one per line)")
    parser.add_argument("--sets", type=int, default=10, help="Number of sets to generate")
    parser.add_argument("--pages", type=int, default=1000, help="Pad pages per set")
    parser.add_argument("--output", default="./output", help="Output directory")
    parser.add_argument("--chars", type=int, default=None, help="Key chars per pad page (default: 665 for A6, 375 for A7)")
    parser.add_argument("--fontsize", type=float, default=None, help="Font size in pt (default: 9)")
    parser.add_argument("--a7", action="store_true", help="Two pad pages per A6 sheet (cut to get A7)")
    parser.add_argument("--no-auth", action="store_true", help="Omit the AUTH group from page headers")
    args = parser.parse_args()
    with_auth = not args.no_auth

    # Defaults based on format
    if args.a7:
        font_size = args.fontsize or 9
        chars_per_page = args.chars or 375
        chars_per_row = A7_CHARS_PER_ROW
        groups_per_row = A7_GROUPS_PER_ROW
        format_label = "A7 (2-up on A6)"
    else:
        font_size = args.fontsize or 9
        chars_per_page = args.chars or 665
        chars_per_row = A6_CHARS_PER_ROW
        groups_per_row = A6_GROUPS_PER_ROW
        format_label = "A6"

    # Load codewords
    codewords = load_codewords(args.codewords)
    if len(codewords) < args.sets:
        print(f"ERROR: Need {args.sets} codewords but file only contains {len(codewords)}")
        sys.exit(1)

    # Codewords become filenames, and each set must be unique
    seen = set()
    for word in codewords[:args.sets]:
        if word in seen:
            print(f"ERROR: Duplicate codeword '{word}' — its set would overwrite the previous PDF")
            sys.exit(1)
        seen.add(word)
        if not all(ch.isalnum() or ch in "-_" for ch in word):
            print(f"ERROR: Codeword '{word}' is unsafe as a filename (use A-Z, 0-9, '-', '_')")
            sys.exit(1)

    # Create output directory
    os.makedirs(args.output, exist_ok=True)

    # Validate
    max_chars = calc_max_chars(font_size, args.a7)
    if chars_per_page > max_chars:
        print(f"ERROR: {chars_per_page} chars won't fit on {format_label} at {font_size}pt. Maximum is {max_chars}.")
        sys.exit(1)

    num_rows = -(-chars_per_page // chars_per_row)

    print(f"Format: {format_label}")
    print(f"Generating {args.sets} OTP sets, {args.pages} pad pages each, {chars_per_page} chars/page")
    print(f"Auth group in header: {'yes' if with_auth else 'no'}")
    print(f"Font: Courier {font_size}pt")
    print(f"Layout: {num_rows} rows of {groups_per_row}x{GROUP_SIZE} groups ({chars_per_row} chars/row)")
    print(f"Max chars per pad page at this font size: {max_chars}")
    if args.a7:
        num_sheets = -(-args.pages // 2)
        print(f"Paper: {num_sheets} A6 sheets per set (cut vertically to separate)")
    print(f"Output: {args.output}")
    print()

    for i in range(args.sets):
        codeword = codewords[i]
        filename = f"{codeword}.pdf"
        filepath = os.path.join(args.output, filename)

        print(f"Set {i + 1}/{args.sets}: {codeword}")
        if args.a7:
            generate_set_pdf_a7(filepath, codeword, args.pages, chars_per_page, font_size, with_auth)
        else:
            generate_set_pdf_a6(filepath, codeword, args.pages, chars_per_page, font_size, with_auth)
        print(f"  Saved: {filepath}")
        print()

    print("=" * 50)
    print("DONE")
    print()
    print("Each PDF contains one complete set.")
    print("PRINT EACH PDF TWICE to get your A and B copies.")
    if args.a7:
        print("Cut each sheet vertically along the dashed line")
        print("to separate into A7 pad pages.")
    print("Store both copies in a sealed envelope labeled")
    print("with the codeword. Destroy this digital data")
    print("and wipe the generation machine when finished.")


if __name__ == "__main__":
    main()
