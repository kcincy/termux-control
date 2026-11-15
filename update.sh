#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
umask 077
cd -- "$(git rev-parse --show-toplevel)"
case "$(git remote get-url origin)" in
  https://github.com/kcincy/termux-control|https://github.com/kcincy/termux-control.git|git@github.com:kcincy/termux-control.git|ssh://git@github.com/kcincy/termux-control.git) ;;
  *) echo 'Origin diferente de kcincy/termux-control. Atualização cancelada.' >&2; exit 1 ;;
esac
if [[ ! -x .venv/bin/python ]]; then
  echo 'Ambiente Python não encontrado. Execute este comando na instalação existente do TERMUX CONTROL.' >&2
  exit 1
fi
if [[ -f backend/updater.py ]]; then
  exec .venv/bin/python -m backend.updater
fi
# The first update loads the updater without replacing the running version first.
git fetch --no-tags origin main
git show FETCH_HEAD:backend/updater.py | .venv/bin/python -
