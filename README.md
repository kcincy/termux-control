# TERMUX CONTROL

Controle seu próprio Termux pelo navegador: terminal real, arquivos, scripts,
processos e monitoramento. O servidor roda no Android, sem root e sem PC ligado.
O dashboard já vem compilado: não é necessário usar npm nem compilar no celular.

## 1. Instalar no Termux

Use Termux atualizado, em armazenamento interno. Execute:

```bash
pkg update -y
pkg install -y git
cd ~
git clone https://github.com/kcincy/termux-control.git
cd termux-control
bash scripts/install.sh
```

O instalador instala Python, Node.js e tmux, pede suas credenciais e inicia o
servidor. Crie uma senha exclusiva com **pelo menos 14 caracteres**. Não existe
senha padrão. A pasta sugerida para seus arquivos é `~/termux-workspace`.

## 2. Acessar pelo PC com Tailscale

1. Instale o app Tailscale no Android e no PC. Entre na mesma conta/rede privada
   e deixe os dois conectados. No app Android, copie o IP do celular: `100.x.y.z`.
2. Na instalação, configure os campos abaixo. Troque `100.x.y.z` pelo IP real:

   | Pergunta do instalador | Resposta |
   | --- | --- |
   | Interface | `0.0.0.0` |
   | Porta | `8765` |
   | Origens permitidas | `http://127.0.0.1:8765,http://localhost:8765,http://100.x.y.z:8765` |
   | Confirma HTTP somente em loopback/Tailscale? | `s` |
   | Solicitar wake lock? | `s`, se desejar tentar manter o serviço com tela apagada |

3. No PC conectado ao Tailscale, abra **`http://100.x.y.z:8765`**, entre com seu
   usuário e senha e vá em **Terminal → Nova sessão**. Comandos executam no celular.

No próprio celular, o endereço é **`http://127.0.0.1:8765`**.
Se houver regras de acesso na sua tailnet, elas precisam permitir a porta TCP 8765
do celular para o PC. [Guia oficial do Tailscale](https://tailscale.com/docs/how-to/connect-to-devices).

Já instalou com outras opções? Reconfigure assim; o setup pede uma nova senha:

```bash
cd ~/termux-control
bash scripts/stop.sh
.venv/bin/python -m backend.cli setup
bash scripts/start.sh
```

## Comandos do dia a dia

```bash
cd ~/termux-control
bash scripts/start.sh       # iniciar
bash scripts/stop.sh        # parar o dashboard
bash scripts/restart.sh     # reiniciar
.venv/bin/python -m backend.cli doctor  # diagnóstico
```

Fechar o navegador preserva as sessões tmux. Parar o dashboard também preserva
terminais/scripts; encerre-os pelo painel quando quiser pará-los.

Para iniciar após reboot, instale Termux:Boot da mesma origem do Termux, abra esse
app uma vez e execute `bash scripts/install-boot.sh`. Retire restrições de bateria
do Termux e Tailscale nas configurações do Android.

## Cuidados importantes

- `0.0.0.0` também escuta no Wi-Fi. Use HTTP apenas pelo endereço Tailscale ou
  loopback. Nunca abra a porta no roteador nem use túneis públicos/Funnel.
- Terminal e scripts têm as permissões do usuário Termux. Só execute código seu
  ou confiável. A exclusão de arquivos pede confirmação e só remove pastas vazias.
- Senhas são armazenadas como hash. Configuração, sessões e logs ficam fora do
  repositório, em `~/.local/share/termux-control`. Não publique essa pasta.
- O indicador Tailscale pode mostrar “Não verificado” mesmo com a VPN funcionando:
  o app Android normalmente não fornece uma CLI consultável pelo painel.
- O Android pode suspender ou encerrar Termux/VPN. Wake lock e boot não garantem
  execução permanente; sessões não sobrevivem a reboot ou morte do tmux.
- O código e o frontend foram verificados no Windows. Instalação, PTY/tmux,
  Tailscale e funcionamento com tela apagada ainda exigem teste em Android real.

Este repositório contém a distribuição de execução: `backend/`, `dist/`, scripts,
dependências Python e avisos de licença. Os fontes de desenvolvimento React,
ambientes locais, credenciais e dados de teste não fazem parte desta distribuição.
