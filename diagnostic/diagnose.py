from pathlib import Path
from urllib.parse import urlsplit, unquote


def load_urls(path: Path) -> list[tuple[str, str | None]]:
    if not path.exists():
        raise FileNotFoundError(
            f"URL file not found: {path.resolve()}"
        )

    results = []

    for raw in path.read_text(
        encoding="utf-8",
        errors="ignore",
    ).splitlines():

        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if not (
            line.startswith("http://")
            or line.startswith("https://")
        ):
            print(f"Skipping invalid URL: {line}")
            continue

        parsed = urlsplit(line)

        # Filename supplied after '#'
        filename = unquote(parsed.fragment).strip()

        if not filename:
            filename = None

        results.append((line, filename))

    return results


urls = load_urls(Path("urls.txt"))

print(f"Loaded {len(urls)} URLs\n")

for i, (url, filename) in enumerate(urls, 1):
    print(
        f"{i:03d} | "
        f"{filename or '<server-provided filename>'} | "
        f"{url}"
    )