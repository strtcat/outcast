# Maintainer: strtcat
pkgname=outcast-git
_pkgname=outcast
pkgver=0.20.0.r0.g0000000   # placeholder — overwritten by pkgver() / .SRCINFO
pkgrel=1
pkgdesc="Keyboard-first personal feed aggregator and player for YouTube, Twitch and Kick (PySide6/Qt)"
arch=('any')
url="https://github.com/strtcat/outcast"
license=('MIT')
depends=(
    'python'
    'pyside6'
    'mpv'
    'yt-dlp'
    'curl'
)
optdepends=(
    'streamlink: Twitch live streams and VODs'
    'python-curl_cffi: required to list Kick VODs and live streams'
    'imagemagick: thumbnail resizing/compression (lighter cache)'
    'graphicsmagick: alternative thumbnail resizing backend'
    'ffmpeg: fallback thumbnail resizing if ImageMagick/GraphicsMagick are absent'
    'libnotify: desktop notifications'
)
makedepends=('git')
provides=('outcast')
conflicts=('outcast')
install="$_pkgname.install"
source=("$_pkgname::git+https://github.com/strtcat/outcast.git")
sha256sums=('SKIP')

pkgver() {
    cd "$srcdir/$_pkgname"
    # If you publish git tags like v0.20.0 -> "0.20.0.r12.gabcdef1".
    # Otherwise falls back to "rNNN.gHASH".
    git describe --long --tags --abbrev=7 2>/dev/null \
        | sed 's/^v//;s/\([^-]*-g\)/r\1/;s/-/./g' \
    || printf 'r%s.g%s' "$(git rev-list --count HEAD)" "$(git rev-parse --short=7 HEAD)"
}

package() {
    cd "$srcdir/$_pkgname"

    # Program logic under /usr/lib/outcast.
    install -Dm644 src/outcast.py        "$pkgdir/usr/lib/outcast/outcast.py"
    install -Dm644 src/i18n.py           "$pkgdir/usr/lib/outcast/i18n.py"
    install -Dm644 src/outcast_db.py     "$pkgdir/usr/lib/outcast/outcast_db.py"
    install -Dm755 src/outcast-worker.sh "$pkgdir/usr/lib/outcast/outcast-worker.sh"
    install -Dm755 src/outcast-live.sh   "$pkgdir/usr/lib/outcast/outcast-live.sh"

    # Translation catalogs (XDG data dir).
    install -Dm644 src/locales/en.json   "$pkgdir/usr/share/outcast/locales/en.json"
    install -Dm644 src/locales/es.json   "$pkgdir/usr/share/outcast/locales/es.json"

    # Executable launcher on PATH.
    install -Dm755 src/outcast           "$pkgdir/usr/bin/outcast"

    # Desktop entry + license.
    install -Dm644 outcast.desktop       "$pkgdir/usr/share/applications/outcast.desktop"
    install -Dm644 LICENSE               "$pkgdir/usr/share/licenses/outcast/LICENSE"
}
