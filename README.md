# arRPC Personalizado Pessoal Linux

Daemon de Rich Presence que faz o Discord mostrar jogos e apps que ele **não
detecta sozinho no Linux** — jogos nativos, jogos rodando sob Proton, e apps de
mídia (Stremio, YouTube Music) com o que está tocando.

Escrito em Python puro (só a biblioteca padrão), roda como serviço systemd de
usuário, sobrevive a reboot.

---

## Por que isso é necessário

O Discord detecta jogos comparando os processos em execução com a base pública
de jogos detectáveis (`https://discord.com/api/v9/applications/detectable`).
Essa base tem um problema no Linux:

| Situação | O que a base traz | Resultado |
|---|---|---|
| RuneScape nativo (`rs2client`) | só executáveis `win32` e `darwin` | nunca detectado |
| Jogo sob Proton (`gta-sa.exe`) | executável `win32`, mas o processo tem caminho Wine | não bate |
| TBH: Task Bar Hero | **nenhum** executável cadastrado | nunca detectado |
| Stremio / YouTube Music | não existem na base | nunca detectados |

Este daemon resolve isso pelo outro lado: procura os processos por conta
própria e **fala o protocolo IPC do Discord diretamente**, publicando a
presença. Não depende do arRPC oficial nem de plugin do Vencord.

## O que ele mostra hoje

| App | Como é detectado | O que aparece |
|---|---|---|
| **RuneScape 3** | processo `rs2client` | "Gielinor" |
| **Minecraft** (Prism Launcher) | processo da instância | nome da instância, **servidor conectado** e **lotação** (ex.: `860 of 5000`) |
| **Left 4 Dead 2** | `hl2_linux -game left4dead2` ou `.exe` no Proton | **campanha e capítulo** (ex.: "Dark Carnival — capítulo 3") |
| **GTA: San Andreas** | `gta-sa.exe` sob Proton | "Jogando" |
| **TBH: Task Bar Hero** | `TaskBarHero.exe` sob Proton | "Jogando" |
| **Stremio** | MPRIS | **série — episódio** (ex.: "Prison Break — Unearthed (2x9)") |
| **YouTube Music** (Firefox) | MPRIS + URL da aba | **faixa — artista**, tempo restante e botões de link |

---

## Instalação

### Um comando (qualquer distro)

Como o repositório é **privado**, o jeito mais simples é clonar autenticado
(o `gh` já cuida do token):

```bash
gh repo clone gabrielmf1998/arrpc-personalizado-pessoal-linux
cd arrpc-personalizado-pessoal-linux && ./install.sh
```

Ou pelo GitLab:

```bash
glab repo clone gabriel17166/arrpc-personalizado-pessoal-linux
cd arrpc-personalizado-pessoal-linux && ./install.sh
```

Numa máquina sem `gh`/`glab`, use um token com escopo `repo`:

```bash
export GITHUB_TOKEN=ghp_seu_token
curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://raw.githubusercontent.com/gabrielmf1998/arrpc-personalizado-pessoal-linux/main/install.sh \
  | bash -s
```

O `install.sh` repassa esse `GITHUB_TOKEN` ao baixar os demais arquivos. Se um
dia o repositório virar público, o `curl ... | bash` puro passa a funcionar sem
token.

O instalador:

1. detecta a distro (Arch, Debian/Ubuntu, Fedora, openSUSE, ou genérica);
2. instala as dependências (`python3`, `iproute2`/`ss`, `curl`);
3. instala o **Vesktop nativo** — pacote do repositório no Arch, `.deb`/`.rpm`
   oficial nas demais, tarball em `/opt` como reserva;
4. instala o daemon em `~/.local/bin` e o serviço em `~/.config/systemd/user`,
   já habilitado;
5. liga o log de console do L4D2 (necessário para saber o mapa).

Para pular a instalação do Vesktop (se você já usa outro cliente que exponha o
socket IPC): `SKIP_VESKTOP=1 curl ... | bash`.

### Manual

Passo a passo completo, sem script, em [`INSTALAR.txt`](INSTALAR.txt).

---

## Por que Vesktop nativo

O daemon precisa do socket `$XDG_RUNTIME_DIR/discord-ipc-0`. O Discord em
Flatpak roda em sandbox com `/run` isolado e o socket não fica visível para
processos de fora. Vesktop nativo (`.deb`/`.rpm`/pacote da distro) cria o
socket no runtime dir real do usuário, que é o que funciona aqui.

---

## Configuração

### Apps que precisam de Application ID próprio

Stremio e YouTube Music **não existem** na base do Discord, então não há
`client_id` público para eles. Crie um app seu:

1. https://discord.com/developers/applications → **New Application**
2. dê o nome que deve aparecer no perfil (ex.: `YouTube Music`)
3. em **General Information**, suba o ícone e copie o **Application ID**
4. coloque em `~/.config/discord-game-presence.json`:

```json
{
  "games": {
    "youtube-music":           { "client_id": "SEU_ID_AQUI" },
    "libexec/stremio/stremio": { "client_id": "SEU_ID_AQUI" }
  }
}
```

5. `systemctl --user restart discord-game-presence`

O nome exibido no perfil é o **nome do app**, não o campo `details`.

### Adicionar um jogo novo

Descubra o `client_id` na base pública do Discord:

```bash
curl -s https://discord.com/api/v9/applications/detectable \
  | python3 -c 'import json,sys
for a in json.load(sys.stdin):
    if "nome do jogo" in a["name"].lower(): print(a["id"], a["name"])'
```

Descubra o processo:

```bash
ps aux | grep -i <jogo>
```

E acrescente ao `~/.config/discord-game-presence.json`:

```json
{"games": {"nome-do-processo": {"client_id": "...", "details": "Nome do Jogo",
                                "state": "Jogando", "large_text": "Nome do Jogo"}}}
```

O casamento é `pgrep -f`, ou seja, qualquer parte da linha de comando serve.
Isso é o que permite pegar jogos sob Proton, cujo processo aparece como
`S:\common\Jogo\jogo.exe`.

---

## Como cada detecção funciona

### Minecraft: servidor e lotação

1. acha o PID pela linha de comando (`PrismLauncher/instances`);
2. extrai o nome da instância de `-Djava.library.path=.../instances/<nome>/natives`;
3. lista as conexões TCP do processo com `ss -tnp`, descartando loopback e LAN;
4. resolve os hosts salvos no `servers.dat` da instância e casa com o IP
   conectado — assim mostra `play.exemplo.com` em vez de um IP de CDN;
5. faz um **Server List Ping** (handshake + status do protocolo Minecraft,
   implementado aqui em ~30 linhas) para obter `online`/`max`;
6. publica a lotação como `party.size`, que o Discord renderiza como
   "(860 of 5000)" ao lado do status.

### Left 4 Dead 2: mapa

O L4D2 não expõe o mapa em processo, socket ou arquivo aberto — mapas oficiais
vivem dentro de VPKs. A única fonte é o console. O instalador escreve
`con_logfile "console.log"` em `left4dead2/cfg/autoexec.cfg`; o daemon lê o fim
desse log e traduz o slug do mapa (`c2m3_coaster` → "Dark Carnival — capítulo 3").

> O log só passa a existir **depois de reiniciar o jogo** uma vez.

### Stremio e YouTube Music: MPRIS

Ambos publicam no D-Bus via `org.mpris.MediaPlayer2`. O daemon lê `Metadata`,
`PlaybackStatus` e `Position` com `busctl`.

Diferenças importantes entre os dois:

| | Stremio | YouTube Music (Firefox) |
|---|---|---|
| título / artista | sim | sim |
| `Position` e `mpris:length` | **não publica** | publica |
| tempo mostrado | tempo desde o início do título | tempo real da faixa |
| botões de link | — | faixa + busca do artista |

O bus name do Firefox muda a cada boot (`...firefox.instance_1_63`), então o
daemon procura por prefixo. A presença do YouTube Music só liga quando existe
aba tocando com URL de `music.youtube.com` — por isso ela usa um callable em
`running`, e não um nome de processo.

---

## Limites conhecidos

- **`type` de atividade é ignorado.** Mandar `type: 2` (Ouvindo) ou `3`
  (Assistindo) é aceito pelo IPC, mas o Discord responde com `type: 0` — via
  RPC um app comum só publica como "Jogando". O formato de barra do Spotify
  ("1:23 / 2:46") não é alcançável por esse caminho; o que aparece é o tempo
  restante.
- **Botões não aparecem para você mesmo**, só para quem vê seu perfil.
- **Stremio não publica posição de reprodução**, então não há progresso real do
  episódio — o cronômetro reinicia a cada título novo.
- O link do artista no YouTube Music leva à **busca**, porque o MPRIS não traz
  o canal.

---

## Operação

```bash
systemctl --user status discord-game-presence     # estado
systemctl --user restart discord-game-presence    # após editar config
journalctl --user -u discord-game-presence -f     # log
```

O daemon roda uma thread por app, cada uma esperando o processo aparecer. Se o
Discord estiver fechado ou o socket cair, a thread tenta de novo no próximo
ciclo (15s). O serviço tem `Restart=always`.

## Desinstalar

```bash
systemctl --user disable --now discord-game-presence
rm ~/.local/bin/discord-game-presence.py
rm ~/.config/systemd/user/discord-game-presence.service
rm -f ~/.config/discord-game-presence.json
systemctl --user daemon-reload
```

## Arquivos

```
discord-game-presence.py            o daemon
install.sh                          instalador multi-distro
INSTALAR.txt                        instalação manual, passo a passo
systemd/discord-game-presence.service
```
