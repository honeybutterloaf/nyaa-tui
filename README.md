# nyaa-tui

A terminal interface for searching nyaa.si, reading release descriptions, managing
aria2 downloads, playing local videos with mpv, and tracking watch progress.

## Install

Install the required external programs using your operating system's package
manager:

- `aria2c` for downloads
- `mpv` for video playback

Then install the app:

```sh
python -m pip install nyaa-tui
```

To enable inline images in release descriptions:

```sh
python -m pip install 'nyaa-tui[images]'
```

If installing from a locally built wheel, use its path instead of `nyaa-tui`.

## Run

```sh
nyaa-tui
```

You can also run it as a Python module:

```sh
python -m nyaa_tui
```

Use `nyaa-tui --check-images` to check whether optional image support is
available in the active Python environment.

## Build a wheel

From the project root:

```sh
python -m pip install build
python -m build
```

The distributable wheel will be created in `dist/`. Install it with:

```sh
python -m pip install dist/nyaa_tui-0.1.0-py3-none-any.whl
```

## License

This project is licensed under the GNU General Public License, version 3 or
(at your option) any later version. See [LICENSE](LICENSE).
