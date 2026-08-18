#!/usr/bin/env sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./install.sh [--force]

Install codex-sessions to ~/.local/bin.

Options:
  --force    Replace an existing ~/.local/bin/codex-sessions entry.
EOF
}

force=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --force)
            force=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_file="$script_dir/codex_sessions.py"
bin_dir="${HOME}/.local/bin"
target="${bin_dir}/codex-sessions"

if [ ! -f "$source_file" ]; then
    echo "missing script: $source_file" >&2
    exit 1
fi

mkdir -p "$bin_dir"
chmod +x "$source_file"

if [ -e "$target" ] || [ -L "$target" ]; then
    current=$(readlink "$target" 2>/dev/null || true)
    if [ "$current" = "$source_file" ]; then
        echo "already installed: $target"
    elif [ "$force" -eq 1 ]; then
        rm -f "$target"
        ln -s "$source_file" "$target"
        echo "reinstalled: $target"
    else
        echo "target already exists: $target" >&2
        echo "run './install.sh --force' to replace it" >&2
        exit 1
    fi
else
    ln -s "$source_file" "$target"
    echo "installed: $target"
fi

case ":${PATH}:" in
    *":${bin_dir}:"*)
        ;;
    *)
        echo "warning: $bin_dir is not in PATH" >&2
        echo "add this to your shell rc file:" >&2
        echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
        ;;
esac

echo "run: codex-sessions"
