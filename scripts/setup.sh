#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
ENV_EXAMPLE="$ROOT_DIR/.env.example"
ENV_FILE="$ROOT_DIR/.env"
VERIFY_TARGETS="${VERIFY_TARGETS:-tests/test_demo.py tests/test_chain_ethereum.py tests/test_service_health.py}"
START_TIME="$(date +%s)"

log_step() {
  printf "\n[setup] %s\n" "$1"
}

log_warn() {
  printf "\n[warn] %s\n" "$1" >&2
}

fail() {
  printf "\n[error] %s\n" "$1" >&2
  exit 1
}

is_interactive() {
  [ -t 0 ] && [ -t 1 ] && [ "${SETUP_INTERACTIVE:-1}" != "0" ]
}

python_version_supported() {
  local candidate="$1"
  "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

find_python() {
  local requested="${PYTHON_BIN:-}"
  local candidates=()
  local candidate

  if [ -n "$requested" ]; then
    candidates+=("$requested")
  fi

  candidates+=(python3.12 python3.11 python3.10 python3)

  for candidate in "${candidates[@]}"; do
    if ! command -v "$candidate" >/dev/null 2>&1; then
      continue
    fi
    if python_version_supported "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  fail "Python 3.10+ is required. Set PYTHON_BIN=/path/to/python3.11 if needed."
}

env_get() {
  local key="$1"
  if [ ! -f "$ENV_FILE" ]; then
    return 0
  fi
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE"
}

env_set() {
  local key="$1"
  local value="$2"
  local tmp_file

  mkdir -p "$(dirname "$ENV_FILE")"
  tmp_file="$(mktemp "$ROOT_DIR/.env.tmp.XXXXXX")"

  if [ -f "$ENV_FILE" ]; then
    awk -v key="$key" -v value="$value" '
      BEGIN { updated = 0 }
      index($0, key "=") == 1 { print key "=" value; updated = 1; next }
      { print }
      END { if (!updated) print key "=" value }
    ' "$ENV_FILE" >"$tmp_file"
  else
    printf '%s=%s\n' "$key" "$value" >"$tmp_file"
  fi

  mv "$tmp_file" "$ENV_FILE"
}

prompt_setup_mode() {
  local selected="${SETUP_MODE:-}"

  if [ -n "$selected" ]; then
    printf '%s\n' "$selected"
    return 0
  fi

  if ! is_interactive; then
    printf '%s\n' "demo"
    return 0
  fi

  printf "\nChoose setup mode [demo/live] (default: demo): " >&2
  read -r selected
  selected="${selected:-demo}"
  printf '%s\n' "$selected"
}

prompt_env_value() {
  local key="$1"
  local label="$2"
  local required="${3:-0}"
  local secret="${4:-0}"
  local existing value rendered_existing

  existing="$(env_get "$key")"

  if ! is_interactive; then
    if [ "$required" = "1" ] && [ -z "$existing" ]; then
      fail "$key is required for live mode. Re-run interactively or prefill $ENV_FILE."
    fi
    if [ -n "$existing" ]; then
      export "$key=$existing"
    fi
    return 0
  fi

  rendered_existing="$existing"
  if [ "$secret" = "1" ] && [ -n "$rendered_existing" ]; then
    rendered_existing="[configured]"
  fi

  while true; do
    if [ "$secret" = "1" ]; then
      if [ -n "$rendered_existing" ]; then
        printf "%s [%s]: " "$label" "$rendered_existing" >&2
      else
        printf "%s: " "$label" >&2
      fi
      read -r -s value
      printf "\n" >&2
    else
      if [ -n "$rendered_existing" ]; then
        printf "%s [%s]: " "$label" "$rendered_existing" >&2
      else
        printf "%s: " "$label" >&2
      fi
      read -r value
    fi

    if [ -z "$value" ]; then
      value="$existing"
    fi

    if [ "$required" = "1" ] && [ -z "$value" ]; then
      printf "This value is required.\n" >&2
      continue
    fi

    break
  done

  env_set "$key" "$value"
  if [ -n "$value" ]; then
    export "$key=$value"
  fi
}

ensure_env_file() {
  if [ -f "$ENV_FILE" ]; then
    log_step "Reusing existing .env"
    return 0
  fi

  [ -f "$ENV_EXAMPLE" ] || fail "Missing $ENV_EXAMPLE"
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  log_step "Created .env from .env.example"
}

setup_env() {
  local mode="$1"

  env_set "JUPITER_SENTINEL_LIVE" "false"

  case "$mode" in
    demo)
      log_step "Demo mode selected; no secrets are required."
      ;;
    live)
      log_step "Live mode selected; prompting for required runtime values."
      env_set "JUPITER_SENTINEL_LIVE" "true"
      prompt_env_value "SOLANA_PRIVATE_KEY_PATH" "SOLANA_PRIVATE_KEY_PATH (path to your Solana keypair JSON)" 1 0
      prompt_env_value "JUP_API_KEY" "JUP_API_KEY (recommended for higher Jupiter rate limits)" 0 1
      prompt_env_value "SOLANA_PUBLIC_KEY" "SOLANA_PUBLIC_KEY (optional if the keypair file already matches your wallet)" 0 0
      prompt_env_value "ETHEREUM_RPC_URL" "ETHEREUM_RPC_URL (optional unless you use EVM or cross-chain modules)" 0 0
      ;;
    *)
      fail "Unsupported setup mode: $mode. Use demo or live."
      ;;
  esac
}

run_verification() {
  log_step "Running smoke tests to verify the installation"
  # shellcheck disable=SC2086
  "$VENV_DIR/bin/python" -m pytest -q $VERIFY_TARGETS
}

print_next_steps() {
  local mode="$1"
  local elapsed

  elapsed="$(( $(date +%s) - START_TIME ))"

  printf "\nSetup complete in %ss.\n" "$elapsed"
  printf "\nNext steps:\n"
  printf "  source .venv/bin/activate\n"
  printf "  python demo.py\n"
  printf "  make test\n"

  if [ "$mode" = "live" ]; then
    printf "  set -a && source .env && set +a\n"
    printf "  python -m jupiter_sentinel_cli --help\n"
  else
    printf "  Edit .env later if you want to enable live credentials.\n"
  fi

  printf "\nDemo note:\n"
  printf "  demo.py is deterministic and uses mocked Jupiter/Solana traffic, so it runs without wallet keys or API keys.\n"
}

main() {
  local python_bin setup_mode

  cd "$ROOT_DIR"

  python_bin="$(find_python)"
  log_step "Using $("$python_bin" -c 'import sys; print(sys.executable)')"

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    log_step "Creating virtual environment at $VENV_DIR"
    "$python_bin" -m venv "$VENV_DIR"
  else
    log_step "Reusing existing virtual environment at $VENV_DIR"
  fi

  log_step "Installing runtime and dev dependencies"
  if ! PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV_DIR/bin/python" -m pip install -r requirements.txt -e ".[dev]"; then
    fail "Dependency installation failed. Ensure PyPI is reachable, then re-run ./scripts/setup.sh."
  fi

  ensure_env_file
  setup_mode="$(prompt_setup_mode)"
  setup_env "$setup_mode"
  run_verification
  print_next_steps "$setup_mode"
}

main "$@"
