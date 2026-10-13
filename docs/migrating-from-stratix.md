# Migrating from Stratix

The SDK's client has been renamed from Stratix to LayerLens. **For almost everyone, upgrading needs no changes:** every old name, environment variable and the `stratix` command keep working exactly as before. The one exception is a `from layerlens import *` that relies on the old names (see below). Each one now tells you its replacement, and they will be removed in a future major release, so it's worth updating when convenient.

## What was renamed

| Before                                                       | Now                                                           |
| ------------------------------------------------------------ | ------------------------------------------------------------- |
| `from layerlens import Stratix`                              | `from layerlens import LayerLens`                             |
| `AsyncStratix`                                               | `AsyncLayerLens`                                              |
| `StratixError`                                               | `LayerLensError`                                              |
| `Atlas`, `AsyncAtlas`, `AtlasError` (older names)            | `LayerLens`, `AsyncLayerLens`, `LayerLensError`               |
| `LAYERLENS_STRATIX_API_KEY` (or `LAYERLENS_ATLAS_API_KEY`)   | `LAYERLENS_API_KEY`                                           |
| `LAYERLENS_STRATIX_BASE_URL` (or `LAYERLENS_ATLAS_BASE_URL`) | `LAYERLENS_BASE_URL`                                          |
| the `stratix` command                                        | the `layerlens` command                                       |
| `map_agui_to_stratix()`, returning `{"stratix_event": …}`    | `map_agui_to_layerlens()`, returning `{"layerlens_event": …}` |

Nothing else changed: the package is still `layerlens`, `Client` and `AsyncClient` still work, and every method, argument and API call is the same.

## How the old names behave

- **They are the same objects.** `Stratix is LayerLens`, so `isinstance(client, Stratix)` and `except StratixError:` around SDK calls behave exactly as before.
- **Each use raises a `DeprecationWarning`** that names the replacement and points at your line of code.
- **Environment variables:** if `LAYERLENS_API_KEY` is set, it is used and the old variables are ignored. Otherwise the old variable is used, with a warning.
- **`from layerlens import *` no longer brings in the old names.** They are not in `__all__`, so star-imports stay free of warnings. If you star-import and then call `Stratix()`, import it explicitly (`from layerlens import Stratix`) or switch to `LayerLens`.
- **The `stratix` command** runs the same CLI and prints a one-line notice to stderr. Its stdout is identical to `layerlens`, so scripts that parse output (`--format json`, `ci report >> $GITHUB_STEP_SUMMARY`) are unaffected.

## Finding what to update

Python only shows a `DeprecationWarning` for code run directly as a script or under a test runner such as pytest. To see every warning in an application, run it with:

```bash
python -W default::DeprecationWarning your_app.py
```

Then:

1. **Code:** replace the old names with the new ones from the table above.
2. **Configuration:** rename the environment variables wherever they are set: shell profiles, `.env` files, CI secrets, container and deployment configs.
3. **Scripts and CI jobs:** replace the `stratix` command with `layerlens`.

## A CLI fix that comes with the rename

`LAYERLENS_API_KEY` is now the CLI's API key variable, and the CLI sends it as an API key. Earlier releases sent it as a login token, which the API rejected, so `LAYERLENS_API_KEY=… layerlens trace list` failed unless `LAYERLENS_STRATIX_API_KEY` was also set. `layerlens whoami` now checks an API key against the API and prints the organization and project it belongs to.
