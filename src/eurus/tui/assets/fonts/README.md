# Bundled terminal fonts

Drop `.ttf` or `.otf` font files into this folder to bundle them with your Eurus
installation. Files placed here ship inside the wheel (see the hatch `artifacts`
entry in `pyproject.toml`) and are offered before the download catalog:

- `/font list` — shows bundled files first, then the downloadable catalog.
- `/font install <filename|stem>` — prefers a bundled file with that name and
  never touches the network for it. Install goes to `~/.termux/font.ttf` on
  Termux (with an automatic backup) or to the platform font folder elsewhere.

Nothing here is downloaded automatically; downloading only happens when you
explicitly run `/font install <catalog-key>` and no bundled file matches.
The repository itself ships this folder empty on purpose.
