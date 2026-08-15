#!/usr/bin/env bash
# Instalador do discord-game-presence: Vesktop nativo + daemon de Rich Presence.
#
#   curl -fsSL <URL_RAW>/install.sh | bash
#
# Suporta Arch, Debian/Ubuntu, Fedora, openSUSE e, como reserva, qualquer distro
# com systemd (Vesktop vai para /opt a partir do tarball oficial).
set -euo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/gabrielmf1998/arrpc-personalizado-pessoal-linux/main}"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
SKIP_VESKTOP="${SKIP_VESKTOP:-0}"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "rode como usuario normal, nao root (o servico e --user)"

detect_distro() {
    [ -r /etc/os-release ] || { echo unknown; return; }
    . /etc/os-release
    case "${ID}${ID_LIKE:-}" in
        *arch*|*cachyos*|*manjaro*|*endeavour*) echo arch ;;
        *debian*|*ubuntu*|*mint*|*pop*)         echo debian ;;
        *fedora*|*rhel*|*nobara*)               echo fedora ;;
        *suse*)                                 echo suse ;;
        *)                                      echo unknown ;;
    esac
}

pkg_install() {
    local distro="$1"; shift
    [ $# -gt 0 ] || return 0
    msg "instalando dependencias: $*"
    case "$distro" in
        arch)   sudo pacman -S --needed --noconfirm "$@" ;;
        debian) sudo apt-get update -qq && sudo apt-get install -y "$@" ;;
        fedora) sudo dnf install -y "$@" ;;
        suse)   sudo zypper --non-interactive install "$@" ;;
        *)      warn "distro desconhecida: instale manualmente: $*" ;;
    esac
}

# ---------------------------------------------------------------- dependencias
install_deps() {
    local distro="$1"
    case "$distro" in
        arch)   pkg_install arch python iproute2 systemd curl ;;
        debian) pkg_install debian python3 iproute2 curl ;;
        fedora) pkg_install fedora python3 iproute curl ;;
        suse)   pkg_install suse python3 iproute2 curl ;;
        *)      warn "instale manualmente: python3, iproute2 (ss), curl" ;;
    esac
    command -v busctl >/dev/null || warn "busctl ausente: Stremio e YouTube Music nao funcionarao"
}

# ------------------------------------------------------------------- Vesktop
# O Discord oficial nao expoe o socket de IPC em todos os empacotamentos, e o
# flatpak isola o /run do usuario. Vesktop nativo cria discord-ipc-0 no
# XDG_RUNTIME_DIR, que e exatamente o que o daemon precisa.
vesktop_installed() {
    command -v vesktop >/dev/null || [ -x /opt/Vesktop/vesktop ]
}

install_vesktop() {
    local distro="$1"
    if vesktop_installed; then
        msg "Vesktop ja instalado"
        return
    fi
    local api="https://api.github.com/repos/Vencord/Vesktop/releases/latest"
    local tmp; tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' RETURN

    case "$distro" in
        arch)
            sudo pacman -S --needed --noconfirm vesktop && return
            ;;
        debian)
            local url; url=$(curl -fsSL "$api" | grep -o 'https://[^"]*amd64\.deb' | head -1)
            [ -n "$url" ] || die "nao achei o .deb do Vesktop"
            curl -fL "$url" -o "$tmp/vesktop.deb"
            sudo apt-get install -y "$tmp/vesktop.deb" && return
            ;;
        fedora|suse)
            local url; url=$(curl -fsSL "$api" | grep -o 'https://[^"]*x86_64\.rpm' | head -1)
            [ -n "$url" ] || die "nao achei o .rpm do Vesktop"
            curl -fL "$url" -o "$tmp/vesktop.rpm"
            if [ "$distro" = fedora ]; then
                sudo dnf install -y "$tmp/vesktop.rpm" && return
            else
                sudo zypper --non-interactive install --allow-unsigned-rpm "$tmp/vesktop.rpm" && return
            fi
            ;;
    esac

    # reserva universal: tarball oficial em /opt
    msg "instalando Vesktop em /opt a partir do tarball"
    # o tarball x86_64 nao tem sufixo de arquitetura (arm64 tem)
    local url; url=$(curl -fsSL "$api" | grep -oE 'https://[^"]*/vesktop-[0-9.]+\.tar\.gz' | head -1)
    [ -n "$url" ] || die "nao achei o tarball do Vesktop"
    curl -fL "$url" -o "$tmp/vesktop.tar.gz"
    sudo mkdir -p /opt/Vesktop
    sudo tar -xzf "$tmp/vesktop.tar.gz" -C /opt/Vesktop --strip-components=1
    sudo ln -sf /opt/Vesktop/vesktop /usr/local/bin/vesktop
}

# --------------------------------------------------------------------- daemon
install_daemon() {
    mkdir -p "$BIN_DIR" "$UNIT_DIR"
    if [ -f "$(dirname "$0")/discord-game-presence.py" ]; then
        install -m 755 "$(dirname "$0")/discord-game-presence.py" "$BIN_DIR/discord-game-presence.py"
        install -m 644 "$(dirname "$0")/systemd/discord-game-presence.service" \
            "$UNIT_DIR/discord-game-presence.service"
    else  # instalacao via curl | bash: baixa do repositorio
        curl -fsSL "$REPO_RAW/discord-game-presence.py" -o "$BIN_DIR/discord-game-presence.py"
        chmod 755 "$BIN_DIR/discord-game-presence.py"
        curl -fsSL "$REPO_RAW/systemd/discord-game-presence.service" \
            -o "$UNIT_DIR/discord-game-presence.service"
    fi

    systemctl --user daemon-reload
    systemctl --user enable --now discord-game-presence.service
}

# ------------------------------------------------------------------- L4D2 map
# O L4D2 nao publica o mapa em lugar nenhum; a unica fonte e o log do console.
setup_l4d2() {
    local lib game cfg
    for lib in "$HOME/.local/share/Steam" "$HOME/.steam/steam" \
               /run/media/*/*/SteamLibrary /mnt/*/SteamLibrary; do
        game="$lib/steamapps/common/Left 4 Dead 2/left4dead2"
        [ -d "$game" ] || continue
        cfg="$game/cfg/autoexec.cfg"
        if ! grep -qs con_logfile "$cfg" 2>/dev/null; then
            printf 'con_logfile "console.log"\n' >> "$cfg"
            msg "L4D2: con_logfile ativado em $cfg"
        fi
        return
    done
}

main() {
    local distro; distro="$(detect_distro)"
    msg "distro detectada: $distro"
    install_deps "$distro"
    [ "$SKIP_VESKTOP" = 1 ] || install_vesktop "$distro"
    install_daemon
    setup_l4d2

    msg "pronto. servico: systemctl --user status discord-game-presence"
    cat <<'EOF'

Proximos passos:
  * Stremio e YouTube Music precisam de um Application ID proprio, criado em
    https://discord.com/developers/applications (o Discord nao os reconhece).
    Depois, coloque os ids em ~/.config/discord-game-presence.json:

      {"games": {"youtube-music": {"client_id": "SEU_ID"},
                 "libexec/stremio/stremio": {"client_id": "SEU_ID"}}}

  * Reinicie o daemon apos editar:
      systemctl --user restart discord-game-presence
EOF
}

main "$@"
