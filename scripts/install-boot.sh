#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
project_dir="$PWD"
boot_dir="$HOME/.termux/boot"
mkdir -p -- "$boot_dir"
boot_file="$boot_dir/termux-control"
if [[ -e "$boot_file" ]]; then
  echo "Já existe: $boot_file. Revise o arquivo antes de substituir." >&2
  exit 1
fi
printf '#!/data/data/com.termux/files/usr/bin/bash\n' > "$boot_file"
printf 'sleep 15\ncd -- %q\nbash scripts/start.sh\n' "$project_dir" >> "$boot_file"
chmod 700 -- "$boot_file"
echo 'Boot configurado. Instale Termux:Boot da mesma origem do Termux e abra o app uma vez.'
echo 'Inicia uma única vez; não existe loop de reinicialização.'
