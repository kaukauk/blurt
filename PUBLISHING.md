# Publishing blurt to GitHub + the AUR

Everything is prepared. These are the steps only you can do (they need your
accounts). Replace `YOURUSER` / `YOURNAME` / email as appropriate.

## 0. Fill in the placeholders

Search the repo for `REPLACE_ME` and set them:

- `PKGBUILD`  → `# Maintainer:` line and `url=`
- `data/blurt.service` → `Documentation=` URL
- `README.md` → clone URL
- `LICENSE`   → copyright holder (currently "blurt authors")

```bash
cd ~/Documents/GitHub/linux-stt
grep -rn REPLACE_ME .   # find them all
```

## 1. Push to GitHub

Create an empty repo named **blurt** on GitHub (no README/license — we have
them), then:

```bash
git remote add origin git@github.com:YOURUSER/blurt.git
git branch -M main
git push -u origin main
```

## 2. Tag a release (the PKGBUILD downloads this tarball)

```bash
git tag v0.1.0
git push origin v0.1.0
```

GitHub auto-creates the source tarball at
`https://github.com/YOURUSER/blurt/archive/refs/tags/v0.1.0.tar.gz`, which is
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
