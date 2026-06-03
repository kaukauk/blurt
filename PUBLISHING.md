# Publishing blurt to GitHub + the AUR

Placeholders are already filled in (GitHub user `kaukauk`,
`karthik@gamebench.net`). These are the steps only you can do — they need your
accounts. (Optionally set your real name in `LICENSE`, currently "blurt authors".)

## ⚠️ 0. First, verify it works when *installed* (not via the dev venv)

On this machine blurt runs from a Python **3.12 venv** because the system Python
is 3.14, which `ctranslate2` may not support yet. The AUR package instead uses
the **system** Python via `python-faster-whisper` (AUR). Before publishing,
confirm that whole chain builds and runs on your current system Python:

```bash
yay -S python-faster-whisper python-sounddevice   # builds python-ctranslate2
python -c "import faster_whisper, sounddevice, ctranslate2; print('ok')"
```

If that fails (e.g. ctranslate2 won't build for Python 3.14), the package will
install but not run for users on that Python — hold off, or pin/patch the dep.
If it prints `ok`, you're good to publish.

## 1. Push to GitHub

Create an empty repo named **blurt** on GitHub (no README/license — we have
them), then:

```bash
git remote add origin git@github.com:kaukauk/blurt.git
git branch -M main
git push -u origin main
```

## 2. Tag a release (the PKGBUILD downloads this tarball)

```bash
git tag v0.1.0
git push origin v0.1.0
```

GitHub auto-creates the source tarball at
`https://github.com/kaukauk/blurt/archive/refs/tags/v0.1.0.tar.gz`, which is
what `source=()` in the PKGBUILD points to.

## 3. Lock the checksum and test the build

```bash
updpkgsums                 # rewrites sha256sums=() from the real tarball
makepkg -si                # build + install locally to verify it works
namcap PKGBUILD *.pkg.tar.zst   # optional: lint
```

Confirm: `systemctl --user enable --now blurt.service`, bind `blurt toggle`,
and dictate.

## 4. Publish to the AUR

One-time AUR account setup: create an account at https://aur.archlinux.org and
add your SSH public key (Account → My Account → SSH Public Key).

```bash
# generate .SRCINFO (required by the AUR)
makepkg --printsrcinfo > .SRCINFO

# clone the (empty) AUR repo and add the packaging files
git clone ssh://aur@aur.archlinux.org/blurt.git aur-blurt
cd aur-blurt
cp ../PKGBUILD ../.SRCINFO ../blurt.install .
git add PKGBUILD .SRCINFO blurt.install
git commit -m "Initial import: blurt 0.1.0"
git push
```

Your package is now live at `https://aur.archlinux.org/packages/blurt`.

## Updating later

1. Bump `pkgver` (and `pkgrel`) in `PKGBUILD`, commit/tag a new GitHub release.
2. `updpkgsums && makepkg --printsrcinfo > .SRCINFO`
3. Commit `PKGBUILD` + `.SRCINFO` to the AUR repo and `git push`.

## Notes

- The AUR dependencies `python-faster-whisper`, `python-ctranslate2`, and
  `python-sounddevice` are themselves AUR packages — AUR helpers (yay/paru)
  resolve them automatically; plain `makepkg` users must install them first.
- GPU is optional: CTranslate2 uses CUDA if `cuda`/`cudnn` are installed,
  otherwise CPU. You may want to mention this in the AUR package comments.
