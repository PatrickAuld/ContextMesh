#!/usr/bin/env python3
"""Build the static docs from repository Markdown and check local site links."""

from html import escape
from html.parser import HTMLParser
import posixpath
from pathlib import Path
import re
from string import Template
from urllib.parse import unquote, urlsplit

import markdown
from markdown.treeprocessors import Treeprocessor


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
REPO = "https://github.com/PatrickAuld/ContextMesh"
PUBLIC = "https://patrickauld.github.io/ContextMesh/"
# source, output directory, navigation title, description
PAGES = [
    ("docs/index.md", "docs", "Overview", "A guide to building shared, contextual memory into your agents with ContextMesh."),
    ("README.md", "docs/quickstart", "Quickstart", "Run ContextMesh locally, capture evidence, and retrieve sourced context."),
    ("docs/architecture.md", "docs/architecture", "Architecture", "Evidence, incremental graph versions, durable workers, retrieval, and disclosure boundaries."),
    ("docs/api.md", "docs/api", "API & integrations", "Connect harnesses through HTTP, the Rust client, and MCP."),
    ("docs/operations.md", "docs/operations", "Operations", "Configure identity and inference, rebuild graphs, audit sources, and redact information."),
    ("docs/testing.md", "docs/testing", "System validation", "Validate ContextMesh with black-box tests against real processes and PostgreSQL."),
    ("docs/evaluation.md", "docs/evaluation", "Evaluation strategy", "Benchmark ContextMesh memory quality, safety, efficiency, and auditability."),
]
OUTPUTS = {source: directory for source, directory, *_ in PAGES}


def relative_directory(target, current):
    return posixpath.relpath(target, current) + "/"


class LocalLinks(Treeprocessor):
    def __init__(self, md, source, directory):
        super().__init__(md)
        self.source = source
        self.directory = directory

    def run(self, root):
        for element in root.iter("a"):
            href = element.get("href", "")
            url = urlsplit(href)
            if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(self.source), unquote(url.path)))
            suffix = ("?" + url.query if url.query else "") + ("#" + url.fragment if url.fragment else "")
            if target in OUTPUTS:
                element.set("href", relative_directory(OUTPUTS[target], self.directory) + suffix)
            else:
                if not (ROOT / target).is_file():
                    raise ValueError(f"Missing source link in {self.source}: {href}")
                element.set("href", f"{REPO}/blob/main/{target}{suffix}")


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.links = []
        self.h1_count = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            if attrs["id"] in self.ids:
                raise ValueError(f"Duplicate id: {attrs['id']}")
            self.ids.add(attrs["id"])
        self.h1_count += tag == "h1"
        for key in ("href", "src"):
            if key in attrs:
                self.links.append(attrs[key])


def check_links():
    pages = {}
    for path in SITE.rglob("*.html"):
        parser = PageLinks()
        parser.feed(path.read_text())
        if parser.h1_count != 1:
            raise ValueError(f"Expected one main heading in {path}")
        pages[path.resolve()] = parser
    checked = 0
    for path, page in pages.items():
        for href in page.links:
            url = urlsplit(href)
            if url.scheme or url.netloc:
                continue
            if url.path.startswith("/"):
                raise ValueError(f"Root-relative link breaks project hosting in {path}: {href}")
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            if not target.is_relative_to(SITE):
                raise ValueError(f"Link escapes site in {path}: {href}")
            if target.is_dir():
                target /= "index.html"
            if not target.is_file():
                raise ValueError(f"Missing target in {path}: {href}")
            if url.fragment and target in pages and unquote(url.fragment) not in pages[target].ids:
                raise ValueError(f"Missing anchor in {path}: {href}")
            checked += 1
    print(f"Validated {len(pages)} HTML pages and {checked} local links/anchors.")


def main():
    template = Template((ROOT / "scripts/templates/docs.html").read_text())
    for index, (source, directory, title, description) in enumerate(PAGES):
        text = (ROOT / source).read_text()
        if source == "README.md":
            text = re.sub(r"^# ContextMesh\n", "# Quickstart\n", text, count=1)
        renderer = markdown.Markdown(
            extensions=["fenced_code", "tables", "toc", "sane_lists"],
            extension_configs={"toc": {"toc_depth": "2-3", "marker": ""}},
        )
        renderer.treeprocessors.register(LocalLinks(renderer, source, directory), "site_links", 1)
        body = renderer.convert(text)
        body = body.replace("<pre>", '<pre tabindex="0" aria-label="Code example">')
        body = body.replace("<table>", '<div class="table-scroll" tabindex="0" role="region" aria-label="Reference table"><table>')
        body = body.replace("</table>", "</table></div>")
        sidebar = "\n".join(
            f'<a href="{relative_directory(dest, directory)}"'
            + (' aria-current="page"' if dest == directory else "")
            + f'>{escape(label)}</a>'
            for _, dest, label, _ in PAGES
        )
        pagination = []
        for offset, label, css in [(-1, "PREVIOUS", "previous"), (1, "NEXT", "next")]:
            neighbor = index + offset
            if 0 <= neighbor < len(PAGES):
                _, dest, name, _ = PAGES[neighbor]
                pagination.append(f'<a class="{css}" href="{relative_directory(dest, directory)}"><span>{label}</span>{escape(name)}</a>')
        output = SITE / directory / "index.html"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(template.substitute(
            title=escape(title), description=escape(description),
            root=relative_directory(".", directory), canonical=PUBLIC + directory + "/",
            sidebar=sidebar, body=body, toc=renderer.toc,
            pagination="\n".join(pagination), source_url=f"{REPO}/edit/main/{source}",
        ))
    check_links()


if __name__ == "__main__":
    main()
