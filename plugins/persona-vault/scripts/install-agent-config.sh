#!/usr/bin/env sh
# Installs the shared pvg-client.js plus thin pvg-agent-memo / pvg-rag-search launchers.
# All request policy lives in pvg-client.js; this script only deploys and configures.
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/persona-vault-gateway"
bin_dir="$HOME/.local/bin"
lib_dir="${XDG_DATA_HOME:-$HOME/.local/share}/persona-vault-gateway"
env_file="$config_dir/env"
memo_bin="$bin_dir/pvg-agent-memo"
rag_bin="$bin_dir/pvg-rag-search"
client_src="$script_dir/pvg-client.js"
client_bin="$lib_dir/pvg-client.js"

# Values the caller passes explicitly win over the saved config.
explicit_url="${PERSONA_VAULT_GATEWAY_URL:-}"
explicit_token="${PERSONA_VAULT_TOKEN:-}"
replace_token=0
for arg in "$@"; do
    case "$arg" in
        --replace-token) replace_token=1 ;;
        -h|--help)
            echo "Usage: install-agent-config.sh [--replace-token]"
            echo "  --replace-token  prompt (hidden) for a new token and replace the saved one"
            echo "A PERSONA_VAULT_TOKEN / PERSONA_VAULT_GATEWAY_URL in the environment also replaces saved values."
            exit 0
            ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

shell_quote() {
    printf "'%s'" "$(printf "%s" "$1" | sed "s/'/'\\\\''/g")"
}

read_value() {
    prompt="$1"
    default="${2:-}"
    if [ -n "$default" ]; then
        printf "%s [%s]: " "$prompt" "$default" >&2
    else
        printf "%s: " "$prompt" >&2
    fi
    if [ -r /dev/tty ] && [ -w /dev/tty ]; then
        IFS= read -r value < /dev/tty || value=""
    else
        IFS= read -r value || value=""
    fi
    printf "%s" "${value:-$default}"
}

read_secret() {
    prompt="$1"
    printf "%s: " "$prompt" >&2
    if [ -r /dev/tty ] && [ -w /dev/tty ]; then
        stty -echo < /dev/tty 2>/dev/null || true
        IFS= read -r value < /dev/tty || value=""
        stty echo < /dev/tty 2>/dev/null || true
    else
        IFS= read -r value || value=""
    fi
    printf "\n" >&2
    printf "%s" "$value"
}

write_launcher() {
    tmp="$1.$$.tmp"
    {
        printf '#!/usr/bin/env sh\n'
        printf 'command -v node >/dev/null 2>&1 || { echo "Node.js is required for PersonaVault helpers." >&2; exit 127; }\n'
        printf 'exec node %s %s "$@"\n' "$(shell_quote "$client_bin")" "$2"
    } > "$tmp"
    chmod 700 "$tmp"
    mv "$tmp" "$1"
}

if ! command -v node >/dev/null 2>&1; then
    echo "node is required for the PersonaVault helpers and plugin hooks." >&2
    exit 1
fi
if [ ! -f "$client_src" ]; then
    echo "Missing $client_src. Run the installer from the plugin's scripts directory, or download pvg-client.js next to it." >&2
    exit 1
fi

mkdir -p "$config_dir" "$bin_dir" "$lib_dir"
chmod 700 "$config_dir"
cp "$client_src" "$client_bin.$$.tmp"
chmod 644 "$client_bin.$$.tmp"
mv "$client_bin.$$.tmp" "$client_bin"
write_launcher "$memo_bin" agent-memo
write_launcher "$rag_bin" rag-search

if [ -f "$env_file" ]; then
    . "$env_file"
fi
url="${explicit_url:-${PERSONA_VAULT_GATEWAY_URL:-}}"
token="${explicit_token:-${PERSONA_VAULT_TOKEN:-}}"
if [ "$replace_token" = 1 ] && [ -z "$explicit_token" ]; then
    token=""
fi
if [ -z "$url" ]; then
    url="$(read_value "Gateway URL")"
fi
if [ -z "$token" ]; then
    token="$(read_secret "Agent token")"
fi

if [ -z "$url" ] || [ -z "$token" ]; then
    cat <<EOF
Agent helpers installed, but token config was skipped.

Create or rotate an agent token at:
  <gateway-url>/admin/tokens

Then rerun:
  $script_dir/install-agent-config.sh

Installed:
  $memo_bin
  $rag_bin
  $client_bin
EOF
    exit 0
fi

tmp_env="$env_file.$$.tmp"
(
    umask 077
    {
        printf "export PERSONA_VAULT_GATEWAY_URL=%s\n" "$(shell_quote "$url")"
        printf "export PERSONA_VAULT_TOKEN=%s\n" "$(shell_quote "$token")"
    } > "$tmp_env"
)
chmod 600 "$tmp_env"
mv "$tmp_env" "$env_file"

cat <<EOF
Installed:
  $env_file
  $memo_bin
  $rag_bin
  $client_bin

Add this to your shell profile if needed:
  export PATH="\$HOME/.local/bin:\$PATH"

Replace a rotated token later with:
  $script_dir/install-agent-config.sh --replace-token

Verify helpers without writing a test memo:
  "$memo_bin" --help
  "$rag_bin" "hello from \$(hostname)"
EOF
