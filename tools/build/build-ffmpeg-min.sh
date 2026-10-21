#!/usr/bin/env bash
# Build a minimal static ffmpeg.exe + ffprobe.exe for 视频分割小工具.
#
# Why: the official Windows builds are ~97 MB per executable (statically linked
# full avcodec). This tool only needs container remuxing, a handful of decoders,
# libx264 + aac encoders, the scale filter and a few bitstream filters, so
# --disable-everything gets the pair down to a few MB.
#
# Run inside an MSYS2 MINGW64 shell (see README "环境重建"):
#   MSYS2_PATH_TYPE=inherit /mingw64/bin/bash tools/build/build-ffmpeg-min.sh
#
# Prerequisites:
#   pacman -S --needed mingw-w64-x86_64-gcc mingw-w64-x86_64-nasm \
#                     mingw-w64-x86_64-pkg-config make git
#
# Output: $PREFIX/bin/ffmpeg.exe, $PREFIX/bin/ffprobe.exe
#         (copy those two into tools/ffmpeg/)

set -euo pipefail
export PATH=/mingw64/bin:$PATH

FFMPEG_REF="${FFMPEG_REF:-n8.1.2}"
X264_SRC="${X264_SRC:-/c/ffbuild/x264}"
FFMPEG_SRC="${FFMPEG_SRC:-/c/ffbuild/FFmpeg}"
PREFIX="${PREFIX:-/c/ffbuild/prefix}"
JOBS="${JOBS:-$(nproc)}"

# ---------------------------------------------------------------- components
# Containers we read. matroska covers mkv/webm; mov covers mp4/m4v/mov.
DEMUXERS="mov,matroska,mpegts,mpegps,mpegvideo,avi,flv,asf,rm,ogg,wav,aac,mp3,flac,image2,concat,hevc,h264,ivf,rawvideo,lavfi"
# Containers we write: mp4/mov use the same muxer (movenc); matroska for mkv.
# null/rawvideo/image2 exist for the test suite.
MUXERS="mp4,mov,matroska,null,rawvideo,image2,adts"

# Decoders matter even in -c copy mode: ffprobe's keyframe listing and any
# re-encode path have to decode the source. Unknown codecs degrade gracefully
# (keyframe scan fails -> plain stream copy).
DECODERS="h264,hevc,mpeg4,mpeg2video,mpeg1video,vp8,vp9,mjpeg,flv,theora,wmv1,wmv2,wmv3,vc1,png,webp"
DECODERS+=",aac,aac_latm,ac3,eac3,mp3,mp2,flac,vorbis,opus,alac,truehd,dca"
DECODERS+=",pcm_s16le,pcm_s16be,pcm_u8,pcm_s24le,pcm_s32le,pcm_f32le,pcm_alaw,pcm_mulaw"
# lavfi sources hand the pipeline wrapped_avframe packets; rawvideo is used by
# the test suite to dump single frames.
DECODERS+=",wrapped_avframe,rawvideo"
# libx264 drives smart cut and 精确重编码; aac for the re-encode audio track.
# rawvideo/wrapped_avframe/pcm_s16le are used by the test suite.
ENCODERS="libx264,libx265,aac,rawvideo,wrapped_avframe,pcm_s16le"

PARSERS="h264,hevc,mpeg4video,mpegvideo,vc1,mjpeg,png,vp8,vp9,aac,aac_latm,mpegaudio,ac3,eac3,flac,vorbis,opus,dca"
# h264/hevc_mp4toannexb + aac_adtstoasc are what remuxing between mp4/ts needs.
BSFS="h264_mp4toannexb,hevc_mp4toannexb,aac_adtstoasc,extract_extradata,vp9_superframe"

FILTERS="scale,format,aformat,aresample,anull,null,trim,concat,setpts,asetpts,fps,pad,copy,split,copy,select"
# Test-suite sources/analysis only; harmless to ship.
FILTERS+=",testsrc,testsrc2,sine,nullsrc,color,geq,showinfo"

# file/pipe cover normal input/output plus -progress pipe:1.
PROTOCOLS="file,pipe,concat"

echo "=== 1/2  x264 ==="
if [ ! -f "$X264_SRC/libx264.a" ]; then
  echo "缺少 x264 源码/构建，请先构建 x264 到 $X264_SRC" >&2
  exit 1
fi
if [ ! -f "$PREFIX/lib/libx264.a" ]; then
  ( cd "$X264_SRC" && \
    ./configure --host=x86_64-w64-mingw32 --prefix="$PREFIX" \
                --enable-static --disable-cli --enable-strip >/dev/null && \
    make -j"$JOBS" >/dev/null && make install >/dev/null )
fi
echo "x264: $(ls -la "$PREFIX/lib/libx264.a" | awk '{print $5}') bytes"

echo "=== 2/2  ffmpeg $FFMPEG_REF ==="
if [ ! -d "$FFMPEG_SRC/.git" ]; then
  git clone --depth 1 --branch "$FFMPEG_REF" https://github.com/FFmpeg/FFmpeg.git "$FFMPEG_SRC"
fi
cd "$FFMPEG_SRC"

export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
./configure \
  --prefix="$PREFIX" \
  --target-os=mingw32 --arch=x86_64 \
  --cc=gcc --cross-prefix= \
  --pkg-config=pkg-config --pkg-config-flags=--static \
  --extra-cflags="-I$PREFIX/include" \
  --extra-ldflags="-L$PREFIX/lib -static" \
  --extra-ldexeflags="-static -static-libgcc -static-libstdc++" \
  --enable-static --disable-shared \
  --disable-everything \
  --disable-doc --disable-debug --disable-autodetect --disable-network \
  --enable-small \
  --enable-gpl --enable-libx264 --enable-libx265 \
  --enable-avcodec --enable-avformat --enable-avfilter \
  --enable-swscale --enable-swresample \
  --enable-avdevice --enable-indev=lavfi \
  --disable-ffplay --enable-ffmpeg --enable-ffprobe \
  --enable-protocol="$PROTOCOLS" \
  --enable-demuxer="$DEMUXERS" \
  --enable-muxer="$MUXERS" \
  --enable-decoder="$DECODERS" \
  --enable-encoder="$ENCODERS" \
  --enable-parser="$PARSERS" \
  --enable-bsf="$BSFS" \
  --enable-filter="$FILTERS"

make -j"$JOBS"
make install

echo "=== 完成 ==="
ls -la "$PREFIX/bin/"
