#!/usr/bin/env bash
# Build a minimal ffprobe.exe.
#
# ffprobe only reads files, so unlike ffmpeg.exe it needs no encoders at all —
# dropping libx264/libx265 + the encoder set makes it roughly half the size.
# That also means it can be an LGPL build (no GPL codecs linked).
#
# Run after build-ffmpeg-min.sh, in the same MSYS2 MINGW64 environment.
# Output: $PREFIX/bin/ffprobe.exe

set -euo pipefail
export PATH=/mingw64/bin:$PATH

FFMPEG_SRC="${FFMPEG_SRC:-/c/ffbuild/FFmpeg}"
PREFIX="${PREFIX:-/c/ffbuild/prefix}"
JOBS="${JOBS:-$(nproc)}"

DEMUXERS="mov,matroska,mpegts,mpegps,mpegvideo,avi,flv,asf,rm,ogg,wav,aac,mp3,flac,image2,concat,hevc,h264,ivf,rawvideo,lavfi"
DECODERS="h264,hevc,mpeg4,mpeg2video,mpeg1video,vp8,vp9,mjpeg,flv,theora,wmv1,wmv2,wmv3,vc1,png,webp"
DECODERS+=",aac,aac_latm,ac3,eac3,mp3,mp2,flac,vorbis,opus,alac,truehd,dca"
DECODERS+=",pcm_s16le,pcm_s16be,pcm_u8,pcm_s24le,pcm_s32le,pcm_f32le,pcm_alaw,pcm_mulaw"
DECODERS+=",wrapped_avframe,rawvideo"
PARSERS="h264,hevc,mpeg4video,mpegvideo,vc1,mjpeg,png,vp8,vp9,aac,aac_latm,mpegaudio,ac3,eac3,flac,vorbis,opus,dca"
BSFS="h264_mp4toannexb,hevc_mp4toannexb,aac_adtstoasc,extract_extradata,vp9_superframe"
FILTERS="null,anull,format,aformat,aresample,scale,trim,concat,setpts,asetpts"
PROTOCOLS="file,pipe,concat"

cd "$FFMPEG_SRC"

./configure \
  --prefix="$PREFIX" \
  --target-os=mingw32 --arch=x86_64 \
  --cc=gcc \
  --pkg-config=pkg-config \
  --enable-static --disable-shared \
  --disable-everything \
  --disable-doc --disable-debug --disable-autodetect --disable-network \
  --enable-small \
  --enable-avcodec --enable-avformat --enable-avfilter \
  --enable-swscale --enable-swresample \
  --enable-avdevice --enable-indev=lavfi \
  --disable-ffmpeg --disable-ffplay --enable-ffprobe \
  --disable-encoders \
  --enable-protocol="$PROTOCOLS" \
  --enable-demuxer="$DEMUXERS" \
  --enable-decoder="$DECODERS" \
  --enable-parser="$PARSERS" \
  --enable-bsf="$BSFS" \
  --enable-filter="$FILTERS"

make -j"$JOBS"
make install

echo "=== 完成 ==="
ls -la "$PREFIX/bin/ffprobe.exe"
