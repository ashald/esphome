#!/usr/bin/env bash
# Build an ESPHome host-platform firmware without PlatformIO.
#
# `esphome compile` needs PlatformIO to install the `native` platform, which is not
# possible without access to the PlatformIO registry. The host platform is plain
# Linux C++, so compiling the generated sources with g++ gives the same binary.
#
# Usage: poc/device/build_host.sh poc/device/tunnel-demo.yaml
set -euo pipefail

config="$1"
name="$(basename "$config" .yaml)"
root="$(cd "$(dirname "$config")" && pwd)/.esphome/build/$name"

esphome compile --only-generate "$config"

# Take the build flags esphome generated for the environment
mapfile -t flags < <(
  sed -n '/AUTO GENERATED CODE BEGIN/,/AUTO GENERATED CODE END/p' "$root/platformio.ini" |
    sed -n '/^build_flags =/,/^[a-z_]* =/p' | grep -E '^\s+-' | sed 's/^\s*//'
)

# Always rebuild everything: generated headers (defines.h) change with the config
# and this script does not track header dependencies. A full build takes seconds.
obj="$root/.obj"
rm -rf "$obj"
mkdir -p "$obj"
find "$root/src" \( -name '*.cpp' -o -name '*.c' \) | sort |
  xargs -r -P "$(nproc)" -I{} sh -c '
    src="{}"; rel="${src#'"$root"'/src/}"; out="'"$obj"'/$(echo "$rel" | tr / _).o"
    case "$src" in
      *.c) gcc -c -O1 -I"'"$root"'/src" '"${flags[*]//-std=gnu++20/}"' -o "$out" "$src" ;;
      *)   g++ -c -O1 -I"'"$root"'/src" '"${flags[*]}"' -o "$out" "$src" ;;
    esac'

mapfile -t objs < <(find "$obj" -name '*.o' | sort)
g++ -o "$root/program" "${objs[@]}" -lpthread
echo "Built $root/program"
