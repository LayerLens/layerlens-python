# Installation

## Requirements

- Python 3.8 or higher
- [uv](https://docs.astral.sh/uv/) (`pip install uv`), or pip (see [With pip](#with-pip))

`layerlens` is hosted on the LayerLens package index during early access. The
`layerlens` package on public PyPI is an unrelated project, so the commands below
make sure the SDK comes from the LayerLens index.

## Install from the LayerLens package index

In a virtual environment:

```bash
uv venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

uv pip install --extra-index-url https://sdk.layerlens.ai/package layerlens
```

uv takes each package from the first index that has it, so `layerlens` comes only
from the LayerLens index and its dependencies from PyPI. Add an extra the same way,
for example `"layerlens[cli]"` for the command-line tool.

## Verify Installation

```python
import layerlens
print(layerlens.__version__)
```

## Dependencies

The SDK installs the following dependencies automatically:

- `httpx` - HTTP client for API requests
- `pydantic` - Data validation and serialization

The `cli` extra adds `click`.

## Upgrading

To upgrade to the latest version:

```bash
uv pip install --upgrade --extra-index-url https://sdk.layerlens.ai/package layerlens
```

## With pip

pip merges every index it is given and installs whichever `layerlens` has the
higher version, so giving pip `--extra-index-url` for `layerlens` would install a
higher-versioned release of the unrelated PyPI project instead of this SDK. `--index-url` on its own doesn't work either: the LayerLens index hosts
only `layerlens`, so pip can't find its dependencies.

Install `layerlens` itself from the LayerLens index, then its dependencies:

```bash
pip install --no-deps --index-url https://sdk.layerlens.ai/package layerlens
pip install layerlens
```

The second command keeps the `layerlens` you just installed and adds its
dependencies from PyPI. To add an extra, name it in the second command:
`"layerlens[cli]"` instead of `layerlens`.

To upgrade, add `--upgrade` to the first command (it reads only the LayerLens
index), never to the second, then run the second again.
