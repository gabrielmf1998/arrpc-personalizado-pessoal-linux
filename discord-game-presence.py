#!/usr/bin/env python3
"""Rich Presence de jogos que o Discord nao detecta no Linux.

A base de jogos detectaveis do Discord so lista executaveis win32/darwin (ou
nenhum), entao nem o cliente nativo do RuneScape nem jogos rodando sob Proton
sao reconhecidos. Este daemon procura os processos, fala o protocolo IPC do
Discord direto e publica a presenca enquanto o jogo estiver aberto.
"""
import gzip
import json
import os
import pathlib
import re
import signal
import socket
import struct
import subprocess
import threading
import urllib.parse
import urllib.request
import time

PRISM = pathlib.Path.home() / ".local/share/PrismLauncher/instances"


def mc_pid():
    r = subprocess.run(["pgrep", "-f", "PrismLauncher/instances"], capture_output=True, text=True)
    pids = r.stdout.split()
    return pids[0] if pids else None


def mc_instance(pid):
    try:
        cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return None
    m = re.search(r"instances/([^/]+)/natives", cmdline)
    return m.group(1) if m else None


def mc_peers(pid):
    """IPs remotos das conexoes TCP do processo, sem loopback/LAN."""
    r = subprocess.run(["ss", "-tnp"], capture_output=True, text=True)
    peers = []
    for line in r.stdout.splitlines():
        if f"pid={pid}," not in line:
            continue
        cols = line.split()
        if len(cols) < 5:
            continue
        peer = cols[4].rsplit(":", 1)
        ip = peer[0].strip("[]").replace("::ffff:", "")
        if ip.startswith(("127.", "192.168.", "10.", "::1")):
            continue
        peers.append((ip, peer[1]))
    return peers


def mc_server_names(instance):
    """Hosts salvos no servers.dat da instancia (NBT: strings legiveis bastam)."""
    if not instance:
        return []
    path = PRISM / instance / "minecraft" / "servers.dat"
    if not path.exists():
        return []
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    hosts = re.findall(rb"[a-zA-Z0-9][a-zA-Z0-9.\-]+\.[a-z]{2,}(?::\d+)?", data)
    return [h.decode() for h in dict.fromkeys(hosts)]


def mpris(prop, player="org.mpris.MediaPlayer2.Stremio"):
    r = subprocess.run(
        ["busctl", "--user", "--json=short", "get-property", player,
         "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", prop],
        capture_output=True, text=True)
    if r.returncode:
        return None
    try:
        return json.loads(r.stdout)["data"]
    except (ValueError, KeyError):
        return None


def mpris_names(prefix):
    """Bus names MPRIS que comecam com o prefixo (o do Firefox muda a cada boot)."""
    r = subprocess.run(["busctl", "--user", "--list", "--no-legend"],
                       capture_output=True, text=True)
    full = f"org.mpris.MediaPlayer2.{prefix}"
    return [line.split()[0] for line in r.stdout.splitlines()
            if line.split() and line.split()[0].startswith(full)]


def mpris_meta(player):
    meta = mpris("Metadata", player) or {}
    return {k: (v["data"] if isinstance(v, dict) else v) for k, v in meta.items()}


_POSITION_ANCHOR = {}
_LAST_TRACK = {}
_LENGTH_CACHE = {}


def track_length(key, meta, video=None):
    """Duracao da faixa, em microssegundos.

    O Firefox publica mpris:length de forma intermitente - some do metadata no
    meio da reproducao. O ultimo valor visto fica guardado por faixa e, para
    video do YouTube, a duracao ainda pode ser lida da propria pagina.
    """
    length = meta.get("mpris:length")
    if length:
        _LENGTH_CACHE[key] = length
        return length
    if key in _LENGTH_CACHE:
        return _LENGTH_CACHE[key]
    if video:
        length = youtube_length(video)
        if length:
            _LENGTH_CACHE[key] = length
    return length


def youtube_length(video):
    """Duracao de um video do YouTube, lida da pagina (em microssegundos)."""
    try:
        request = urllib.request.Request(
            f"https://www.youtube.com/watch?v={video}",
            headers={"User-Agent": "Mozilla/5.0"})
        # a duracao so aparece perto de 1 MB de HTML, entao nao adianta cortar cedo
        with urllib.request.urlopen(request, timeout=8) as r:
            page = r.read(2_000_000).decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r'"lengthSeconds":"(\d+)"', page)
    return int(m.group(1)) * 1_000_000 if m else None


def mpris_position(player, track="", paused=False):
    """Posicao atual em microssegundos, estimada quando preciso.

    O Firefox so atualiza Position quando a pagina manda: fora desses momentos
    ele devolve 0 fixo, mesmo com o video no meio. Entao guardamos a ultima
    posicao real vista (ou zero, no comeco de uma faixa nova) junto do instante
    em que a vimos, e projetamos dali. Qualquer leitura real posterior
    reancora, o que corrige tambem os seeks do usuario.
    """
    key = (player, track)
    reported = mpris("Position", player)
    now = time.monotonic()

    if reported:  # leitura confiavel: reancora
        _POSITION_ANCHOR[key] = (reported, now)
        _LAST_TRACK[player] = track
        return reported
    if reported is None:  # player sumiu
        return None

    anchor = _POSITION_ANCHOR.get(key)
    if anchor is None:
        # zero so vale como inicio de faixa se a troca aconteceu sob nosso olhar;
        # na primeira vez que vemos um player o video pode estar no meio, e
        # ancorar em zero mostraria um progresso errado
        previous = _LAST_TRACK.get(player)
        _LAST_TRACK[player] = track
        if previous is None or previous == track:
            return None
        _POSITION_ANCHOR[key] = (0, now)
        return 0
    position, seen = anchor
    if paused:  # em pausa a posicao nao anda: so adia a ancora
        _POSITION_ANCHOR[key] = (position, now)
        return position
    return position + int((now - seen) * 1_000_000)


def firefox_player(wanted):
    """Player do Firefox cuja aba casa com o site pedido.

    O bus name muda a cada boot, e cada aba com midia vira um player; entao a
    escolha e pela URL, nao pelo nome.
    """
    for name in mpris_names("firefox"):
        url = mpris_meta(name).get("xesam:url") or ""
        if wanted(url):
            return name
    return None


def is_ytm(url):
    return "music.youtube.com" in url


def is_youtube(url):
    return "youtube.com" in url and not is_ytm(url)


def ytm_player():
    return firefox_player(is_ytm)


def ytm_running():
    return ytm_player() is not None


def yt_running():
    return firefox_player(is_youtube) is not None


def yt_title():
    """Titulo do video, que vai na primeira linha da presenca."""
    player = firefox_player(is_youtube)
    if not player:
        return "YouTube"
    return (mpris_meta(player).get("xesam:title") or "YouTube")[:128]


def yt_state():
    """Video em reproducao no YouTube: titulo, canal, capa e progresso."""
    player = firefox_player(is_youtube)
    if not player:  # aba fechada: encerra a presenca em vez de publicar generico
        return None
    meta = mpris_meta(player)
    channel = meta.get("xesam:artist")
    channel = channel[0] if isinstance(channel, list) and channel else channel
    title = meta.get("xesam:title") or "YouTube"

    extra = {}
    url = meta.get("xesam:url") or ""
    video = re.search(r"[?&]v=([\w-]{11})", url)
    if video:  # miniatura do proprio video como icone
        extra["large_image"] = f"https://i.ytimg.com/vi/{video.group(1)}/hqdefault.jpg"
        extra["large_text"] = title[:128]
        extra["buttons"] = [{"label": "Assistir no YouTube",
                             "url": f"https://youtu.be/{video.group(1)}"}]

    if mpris("PlaybackStatus", player) != "Playing":
        mpris_position(player, title, paused=True)  # congela o progresso
        return f"{channel} (pausado)" if channel else "Pausado", extra or None

    position = mpris_position(player, title)
    length = track_length(title, meta, video.group(1) if video else None)
    if position is not None and length:
        now = time.time()
        extra["timestamps"] = {
            "start": int(now - position / 1_000_000),
            "end": int(now + (length - position) / 1_000_000),
        }
    return (channel or "YouTube"), extra or None


_ART_CACHE = {}


def ytm_art(meta):
    """Capa da musica. O Firefox nao publica mpris:artUrl, entao: miniatura do
    video quando a URL traz o id, senao a capa do album pela busca do iTunes."""
    url = meta.get("xesam:url") or ""
    video = re.search(r"[?&]v=([\w-]{11})", url)
    if video:
        return f"https://i.ytimg.com/vi/{video.group(1)}/hqdefault.jpg"

    artist = meta.get("xesam:artist")
    artist = artist[0] if isinstance(artist, list) and artist else artist
    term = " ".join(filter(None, [artist, meta.get("xesam:title")])).strip()
    if not term:
        return None
    if term in _ART_CACHE:
        return _ART_CACHE[term]

    art = None
    try:
        query = urllib.parse.urlencode({"term": term, "media": "music", "limit": 1})
        with urllib.request.urlopen(
                f"https://itunes.apple.com/search?{query}", timeout=5) as r:
            results = json.loads(r.read().decode("utf-8", "replace")).get("results")
        if results:
            art = results[0].get("artworkUrl100", "").replace("100x100", "600x600")
    except (OSError, ValueError):
        pass
    _ART_CACHE[term] = art
    return art


def ytm_state():
    """Musica atual, progresso na faixa e links."""
    player = ytm_player()
    if not player:  # aba fechada: encerra a presenca em vez de publicar generico
        return None
    meta = mpris_meta(player)
    artist = meta.get("xesam:artist")
    artist = artist[0] if isinstance(artist, list) and artist else artist
    title = meta.get("xesam:title") or "YouTube Music"
    label = f"{title} — {artist}" if artist else title

    extra = {}
    art = ytm_art(meta)
    if art:  # capa da musica no lugar do icone do app
        extra["large_image"] = art
        extra["large_text"] = meta.get("xesam:album") or title
    url = meta.get("xesam:url")
    if url:
        extra["buttons"] = [{"label": "Ouvir no YouTube Music", "url": url}]
    if artist:  # o MPRIS nao traz o canal do artista, entao vai pela busca
        query = urllib.parse.quote(artist)
        extra.setdefault("buttons", []).append(
            {"label": f"Artista: {artist}"[:31],
             "url": f"https://music.youtube.com/search?q={query}"})

    if mpris("PlaybackStatus", player) != "Playing":
        mpris_position(player, title, paused=True)  # congela o progresso
        return f"{label} (pausado)", extra or None

    # start + end dao ao Discord o quanto ja passou e o quanto falta
    position, length = mpris_position(player, title), track_length(title, meta)
    if position is not None and length:
        now = time.time()
        extra["timestamps"] = {
            "start": int(now - position / 1_000_000),
            "end": int(now + (length - position) / 1_000_000),
        }
    return label, extra or None


def stremio_state():
    """Titulo em reproducao e quanto falta para acabar, via MPRIS."""
    status = mpris("PlaybackStatus")
    if status is None:  # o Stremio fechou
        return None
    meta = mpris("Metadata") or {}

    def field(key):
        v = meta.get(key)
        return v["data"] if isinstance(v, dict) else v

    title = field("xesam:title")
    if not title:
        return ("Navegando" if status == "Stopped" else "Assistindo"), None
    artist = field("xesam:artist")  # serie, quando for episodio
    series = artist[0] if isinstance(artist, list) and artist else artist
    label = f"{series} — {title}" if series and series != title else title
    if status != "Playing":
        return f"{label} (pausado)", None

    # o Stremio nao publica mpris:length nem Position, entao o Discord so pode
    # contar o tempo desde o inicio deste titulo (o timer reinicia a cada troca)
    position, length = mpris("Position"), field("mpris:length")
    if position and length:
        now = time.time()
        return label, {"timestamps": {
            "start": int(now - position / 1_000_000),
            "end": int(now + (length - position) / 1_000_000),
        }}
    return label, None


def steam_libraries():
    """Todas as bibliotecas Steam, inclusive em outros discos."""
    roots = [pathlib.Path.home() / ".local/share/Steam",
             pathlib.Path.home() / ".steam/steam"]
    libs = []
    for root in roots:
        vdf = root / "steamapps/libraryfolders.vdf"
        if not vdf.exists():
            continue
        libs.append(root)
        try:
            text = vdf.read_text(errors="replace")
        except OSError:
            continue
        libs += [pathlib.Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
    return list(dict.fromkeys(libs))


def steam_game_dir(name):
    """Pasta de um jogo instalado, procurando em todas as bibliotecas."""
    for lib in steam_libraries():
        path = lib / "steamapps/common" / name
        if path.is_dir():
            return path
    return None


def l4d2_log():
    game = steam_game_dir("Left 4 Dead 2")
    return game / "left4dead2/console.log" if game else None

L4D2_MAPS = {
    "c1": "Dead Center", "c2": "Dark Carnival", "c3": "Swamp Fever",
    "c4": "Hard Rain", "c5": "The Parish", "c6": "The Passing",
    "c7": "The Sacrifice", "c8": "No Mercy", "c9": "Crash Course",
    "c10": "Death Toll", "c11": "Dead Air", "c12": "Blood Harvest",
    "c13": "Cold Stream", "c14": "The Last Stand",
}


def l4d2_state():
    """Mapa atual, lido do console.log (precisa de con_logfile no autoexec.cfg)."""
    log = l4d2_log()
    if not log:
        return "Jogando", None
    try:
        with log.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return "Jogando", None

    hits = re.findall(r"^(?:Loading map|Map:|Host_NewGame on map)\s*\"?([\w.]+?)\"?\s*$",
                      tail, re.M)
    if not hits:
        return "No menu", None
    slug = hits[-1].removesuffix(".bsp")
    campaign = L4D2_MAPS.get(re.match(r"c\d+", slug).group() if re.match(r"c\d+", slug) else "")
    m = re.search(r"m(\d+)", slug)
    if campaign:
        return (f"{campaign} — capítulo {m.group(1)}" if m else campaign), None
    return slug, None


def varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def read_varint(sock):
    n = shift = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("servidor fechou")
        n |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return n
        shift += 7


def mc_ping(host, port):
    """Server List Ping: (online, max) de jogadores, ou None."""
    try:
        with socket.create_connection((host, port), timeout=4) as s:
            h = (varint(0) + varint(765) + varint(len(host)) + host.encode()
                 + struct.pack(">H", port) + varint(1))
            s.sendall(varint(len(h)) + h)
            s.sendall(varint(1) + varint(0))  # status request
            read_varint(s)  # tamanho do pacote
            read_varint(s)  # id do pacote
            length = read_varint(s)
            body = b""
            while len(body) < length:
                chunk = s.recv(length - len(body))
                if not chunk:
                    break
                body += chunk
            players = json.loads(body.decode("utf-8", "replace")).get("players", {})
            return players["online"], players["max"]
    except (OSError, ValueError, KeyError, ConnectionError):
        return None


def mc_state():
    """Servidor em que o jogo esta conectado (com lotacao), ou singleplayer."""
    pid = mc_pid()
    if not pid:
        return "Jogando", None
    instance = mc_instance(pid)
    peers = mc_peers(pid)
    if not peers:
        return (f"Singleplayer — {instance}" if instance else "Singleplayer"), None

    # casa o IP conectado com um host salvo em servers.dat; senao mostra o IP
    ip, port = peers[0]
    name = ip
    for host in mc_server_names(instance):
        candidate = host.split(":")[0]
        try:
            ips = {i[4][0] for i in socket.getaddrinfo(candidate, None)}
        except OSError:
            continue
        if any(p in ips for p, _ in peers):
            name = candidate
            break
    else:
        if port != "25565":
            name = f"{ip}:{port}"

    # a lotacao vai so no party; o Discord ja a escreve ao lado do state
    return f"em {name}", mc_ping(name.split(":")[0], int(port))

# nome do processo -> presenca. O client_id vem da base de jogos detectaveis
# do Discord (https://discord.com/api/v9/applications/detectable).
GAMES = {
    "rs2client": {
        "client_id": "357606832899883008",
        "details": "RuneScape 3",
        "state": "Gielinor",
        "large_text": "RuneScape",
    },
    "TaskBarHero.exe": {
        "client_id": "1510483737685655632",
        "details": "TBH: Task Bar Hero",
        "state": "Jogando",
        "large_text": "TBH: Task Bar Hero",
    },
    # Toca no Firefox, entao a presenca segue o MPRIS, nao um processo proprio.
    # Precisa de um app criado em discord.com/developers/applications.
    "youtube-music": {
        "client_id": "1537975657735262368",
        "details": "YouTube Music",
        "state": ytm_state,
        "large_text": "YouTube Music",
        "large_image": ("https://cdn.discordapp.com/app-icons/1537975657735262368/"
                        "1637a3eccd8d9c22acf127e814bf088b.png"),
        "running": ytm_running,
        "type": 2,  # Ouvindo
        "timer_per_title": True,
    },
    # Videos comuns do YouTube, tambem no Firefox e tambem via MPRIS. Precisa de
    # um app proprio: "YouTube" nao existe na base detectavel do Discord.
    "youtube": {
        "client_id": "1538427046760423474",
        "details": yt_title,   # titulo do video
        # usado quando o video nao expoe id na URL (live, shorts, embed)
        "large_image": ("https://cdn.discordapp.com/app-icons/1538427046760423474/"
                        "3f7a95b3c425b18bda601ff2f2e44b2d.png"),
        "state": yt_state,     # canal, capa e progresso
        "large_text": "YouTube",
        "running": yt_running,
        "type": 3,  # Assistindo
        "timer_per_title": True,
    },
    # O Stremio nao esta na base detectavel do Discord: o client_id vem de um app
    # criado em discord.com/developers/applications. Sem ele, a entrada fica inativa.
    "libexec/stremio/stremio": {
        "client_id": "1536170931540463647",
        "details": "Stremio",
        "state": stremio_state,
        "large_text": "Stremio",
        # nao ha Art Assets no app; usa o proprio icone dele
        "large_image": ("https://cdn.discordapp.com/app-icons/1536170931540463647/"
                        "2c1838e508bcbe74dbafaaef7180be93.png"),
        "type": 3,  # Assistindo
        "timer_per_title": True,
    },
    "gta-sa.exe": {
        "client_id": "363447565905166336",
        "details": "Grand Theft Auto: San Andreas",
        "state": "Jogando",
        "large_text": "Grand Theft Auto: San Andreas",
    },
    # casa tanto o binario nativo (hl2_linux -game left4dead2) quanto o .exe sob Proton
    "left4dead2": {
        "client_id": "356954277803065354",
        "details": "Left 4 Dead 2",
        "state": l4d2_state,
        "large_text": "Left 4 Dead 2",
    },
    "PrismLauncher/instances": {
        "client_id": "1410791091501928458",
        # o Discord ja mostra "Minecraft: Java Edition" como titulo; details repete
        "details": lambda: mc_instance(mc_pid()) or "Minecraft",
        "state": mc_state,  # servidor conectado, atualizado a cada poll
        "large_text": "Minecraft",
    },
}

POLL = 15
IPC_TIMEOUT = 15

CONFIG = pathlib.Path(
    os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")
) / "discord-game-presence.json"


def load_config():
    """Aplica ~/.config/discord-game-presence.json sobre a tabela padrao.

    Formato: {"games": {"<processo>": {"client_id": "...", "details": "...",
    "state": "...", "large_text": "...", "large_image": "..."}}}. Entradas
    novas sao adicionadas; as existentes tem so os campos citados trocados.
    Um client_id vazio desliga a entrada.
    """
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return
    for proc, fields in (data.get("games") or {}).items():
        if proc in GAMES:
            GAMES[proc].update(fields)
        elif fields.get("client_id"):
            GAMES[proc] = {"details": proc, "state": "Jogando",
                           "large_text": proc, **fields}


def ipc_path():
    base = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    for i in range(10):
        p = os.path.join(base, f"discord-ipc-{i}")
        if os.path.exists(p):
            return p
    return None


def send(sock, op, payload):
    data = json.dumps(payload).encode()
    sock.sendall(struct.pack("<II", op, len(data)) + data)
    header = sock.recv(8)
    if len(header) < 8:
        raise ConnectionError("IPC fechou")
    _, length = struct.unpack("<II", header)
    body = b""
    while len(body) < length:
        body += sock.recv(length - len(body))
    return json.loads(body)


def running(proc):
    # a chave e o nome do processo, ou uma funcao quando o processo nao basta
    if callable(proc):
        return proc()
    # -f porque processos sob Proton aparecem com caminho windows completo
    return subprocess.run(["pgrep", "-f", proc], capture_output=True).returncode == 0


def changed(old, new):
    """Compara estados, tolerando o jitter do fim previsto da reproducao."""
    if old[0] != new[0] or type(old[1]) is not type(new[1]):
        return True
    if isinstance(new[1], dict):  # so republica em pausa/seek, nao a cada poll
        if new[1].get("buttons") != old[1].get("buttons"):
            return True
        ends = (new[1].get("timestamps", {}).get("end", 0),
                old[1].get("timestamps", {}).get("end", 0))
        return abs(ends[0] - ends[1]) > 5
    return old[1] != new[1]


def clear_presence(client_id):
    """Apaga do Discord a presenca desse app.

    Necessario porque o cliente mantem a ultima atividade publicada mesmo
    depois que a conexao morre: se o daemon for morto (um `systemctl restart`,
    por exemplo), a presenca fica pendurada ate alguem mandar apagar.
    """
    path = ipc_path()
    if not path:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(IPC_TIMEOUT)
        sock.connect(path)
        try:
            send(sock, 0, {"v": 1, "client_id": client_id})
            send(sock, 1, {"cmd": "SET_ACTIVITY",
                           "args": {"pid": os.getpid(), "activity": None},
                           "nonce": str(time.time())})
        finally:
            sock.close()
    except (OSError, ValueError):
        pass  # Discord fechado: nao ha presenca pendurada para apagar


def session(proc, game):
    """Publica a presenca de uma partida, do inicio ao fim do jogo."""
    path = ipc_path()
    if not path:
        return

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(IPC_TIMEOUT)  # sem isso um Discord mudo travaria a thread
    sock.connect(path)
    send(sock, 0, {"v": 1, "client_id": game["client_id"]})

    start = int(time.time())

    def publish(state, restart_timer=False):
        # o segundo item e o tamanho da party (lista) ou campos extras (dict:
        # "timestamps" e "buttons")
        nonlocal start
        text, extra = state
        if restart_timer:  # titulo novo: o contador recomeca do zero
            start = int(time.time())
        details = game["details"]
        fields = extra if isinstance(extra, dict) else {}
        activity = {
            "type": game.get("type", 0),
            "details": details() if callable(details) else details,
            "state": text,
            "timestamps": fields.get("timestamps", {"start": start}),
            "assets": {
                # nome de um Art Asset do app, ou uma URL de imagem; o state
                # pode trocar a arte a cada faixa (capa da musica, por exemplo)
                "large_image": fields.get("large_image") or game.get("large_image", "game"),
                "large_text": fields.get("large_text") or game["large_text"],
            },
        }
        if fields.get("buttons"):
            activity["buttons"] = fields["buttons"][:2]  # o Discord aceita 2
        if extra and not isinstance(extra, dict):  # "(45 of 1000)" ao lado do state
            activity["party"] = {"id": game["client_id"], "size": list(extra)}
        send(sock, 1, {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(time.time()),
        })

    def current_state():
        s = game["state"]
        if not callable(s):
            return (s, None)
        return s()  # None significa "nao ha mais nada tocando"

    try:
        state = current_state()
        if state is None:  # o app saiu entre a checagem e agora
            return
        publish(state)

        while running(proc):
            time.sleep(POLL)
            if not running(proc):
                break  # nao publica nada depois que o app fechou
            new = current_state()
            if new is None:
                break
            if changed(state, new):  # ex.: trocou de servidor, deu seek/pausa
                restart = game.get("timer_per_title") and new[0] != state[0]
                state = new
                publish(state, restart)
    finally:
        try:
            send(sock, 1, {"cmd": "SET_ACTIVITY",
                           "args": {"pid": os.getpid(), "activity": None},
                           "nonce": str(time.time())})
        except (OSError, ValueError):
            pass  # IPC ja caiu; o clear do proximo start resolve
        sock.close()


def watch(proc, game):
    """Espera o jogo abrir, publica a presenca, repete."""
    alive = game.get("running", proc)
    while True:
        if running(alive):
            try:
                session(alive, game)
            except (OSError, ConnectionError, ValueError):
                pass  # Discord fechado ou IPC caiu; tenta de novo depois
        time.sleep(POLL)


def main():
    load_config()
    active = [(proc, game) for proc, game in GAMES.items() if game.get("client_id")]
    ids = list(dict.fromkeys(game["client_id"] for _, game in active))

    for client_id in ids:  # restos de uma execucao anterior
        clear_presence(client_id)

    def shutdown(*_):
        for client_id in ids:
            clear_presence(client_id)
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    for proc, game in active:
        threading.Thread(target=watch, args=(proc, game), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
