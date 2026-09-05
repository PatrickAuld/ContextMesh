# ContextMesh marketing and documentation site

Static HTML and CSS, with progressive enhancement for the homepage tabs and clipboard. Documentation pages require no JavaScript.

## Build

From the repository root, with Python 3.10+:

```sh
python3 -m pip install -r scripts/requirements-site.txt
python3 scripts/build_site.py
```

The build renders the Markdown pages registered in `scripts/build_site.py` into `site/docs/`, then checks every local HTML link, asset path, anchor, and main heading. Generated documentation is ignored by Git; edit the Markdown sources, not the generated HTML. The shared template is `scripts/templates/docs.html`, navigation metadata is in the generator, and documentation styles are in `site/docs.css`.

To view the built site locally:

```sh
python3 -m http.server 8000 --directory site
```

Open `http://localhost:8000`. Assets and links use relative paths so the same output works at the GitHub Pages `/ContextMesh/` project path. Keyboard navigation, page links, and code samples remain usable without JavaScript.

## Publishing

In repository **Settings → Pages → Build and deployment**, select **GitHub Actions** as the source. The `Deploy marketing site` workflow builds and checks the site on relevant pull requests. On `main`, it builds and deploys changes to the site, Markdown sources, or generator. It can also be manually dispatched.

- Marketing: <https://patrickauld.github.io/ContextMesh/>
- Documentation: <https://patrickauld.github.io/ContextMesh/docs/>

Only `site/` is uploaded; service source, working data, configuration, and generator dependencies are outside the published artifact.

Keep product positioning and examples in `index.html` aligned with the service as it evolves. Add documentation pages to the generator's `PAGES` list to include them in navigation and the publication build.
