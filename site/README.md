# ContextMesh marketing site

Static HTML, CSS, and JavaScript. No package installation or build is required.

From the repository root, preview with:

```sh
python3 -m http.server 8000 --directory site
```

Open `http://localhost:8000`. All assets use relative paths so the site also works under the GitHub Pages `/ContextMesh/` project path. Without JavaScript, all usage patterns remain visible. With JavaScript, patterns use keyboard-accessible tabs; the SDK example has a clipboard action on secure origins.

## Publishing

In repository **Settings → Pages → Build and deployment**, select **GitHub Actions** as the source. The `Deploy marketing site` workflow publishes `site/` after changes to that directory on `main`, or when manually dispatched. Expected URL: <https://patrickauld.github.io/ContextMesh/>.

Only `site/` is uploaded. The service, credentials, and working data are never part of the deployment artifact.

## Content

`index.html` contains the product positioning, usage patterns, high-level architecture, and SDK integration example. It describes the pilot foundation and distinguishes illustrative workflows from live demonstrations. Keep integration and maturity claims aligned with the implementation as it evolves.
