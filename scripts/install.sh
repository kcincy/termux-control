#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ -z "${PREFIX:-}" || "$PREFIX" != /data/data/com.termux/files/usr ]]; then
  echo 'Execute este instalador no Termux nativo, em uma pasta dentro de $HOME.' >&2
  exit 1
fi
if [[ ! -f dist/index.html ]]; then
  echo 'Frontend compilado ausente. Baixe novamente o repositório completo.' >&2
  exit 1
fi
pkg install -y python nodejs-lts tmux termux-api
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m backend.cli setup
.venv/bin/python -m backend.cli doctor
bash scripts/start.sh
