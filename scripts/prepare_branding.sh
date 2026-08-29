#!/usr/bin/env bash
# Rasterise the brand SVG into the PNGs Mattermost will actually accept.
#
# Mattermost decodes uploaded images with a decoder registered for png, jpeg,
# gif, bmp, tiff and webp only — SVG is rejected outright, for both the login
# brand image and team icons. So the vector source is converted at build time.
#
# Outputs (git-ignored, regenerated on demand):
#   branding/generated/login-logo.png   wide wordmark for the login page
#   branding/generated/team-icon.png    square mark for the team sidebar
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="branding/logo.svg"
OUT="branding/generated"

[[ -f "$SRC" ]] || { echo "ERROR: $SRC not found"; exit 1; }
command -v magick >/dev/null 2>&1 || { echo "ERROR: ImageMagick ('magick') is required"; exit 1; }

mkdir -p "$OUT"

# Login page: the full wordmark, 2x for crisp rendering, transparent ground so
# it sits on whatever the login page uses.
magick -background none -density 300 "$SRC" \
       -resize 600x \
       -strip \
       "$OUT/login-logo.png"

# Team icon: square, so the wordmark is wrong here — crop to the glyph mark on
# the left, trim the transparent margin, then pad back out to a square.
magick -background none -density 400 "$SRC" \
       -crop 30%x100%+0+0 +repage \
       -trim +repage \
       -bordercolor none -border 12 \
       -resize 512x512 \
       -background none -gravity center -extent 512x512 \
       -strip \
       "$OUT/team-icon.png"

for f in login-logo team-icon; do
  printf '  %-14s %s\n' "$f.png" "$(magick identify -format '%wx%h  %k colours' "$OUT/$f.png")"
done
echo "branding assets written to $OUT/"
