# Public Client

The `PublicClient` (synchronous) and `AsyncPublicClient` (asynchronous) classes provide access to public LayerLens API endpoints for browsing public models, benchmarks, benchmark content, fetching evaluations, and comparing evaluation results.

## Basic Usage

### Synchronous Client

```python
from layerlens import PublicClient

# Loads API key from the "LAYERLENS_API_KEY" environment variable
client = PublicClient()

# Browse public models
models = client.models.get(companies=["OpenAI"])

# Browse public benchmarks
benchmarks = client.benchmarks.get(languages=["English"])
```

### Asynchronous Client

```python
import asyncio
from layerlens import AsyncPublicClient

async def main():
    client = AsyncPublicClient()

    models = await client.models.get(companies=["OpenAI"])
    benchmarks = await client.benchmarks.get(languages=["English"])

asyncio.run(main())
```

### Accessing from an Authenticated Client

If you already have an authenticated `LayerLens` or `AsyncLayerLens` client, you can access public endpoints through the `.public` property:

```python
from layerlens import LayerLens

client = LayerLens()  # requires API key

# Access public endpoints through the authenticated client
public_models = client.public.models.get(query="claude")
```

## Constructor Parameters

### `PublicClient(api_key, base_url, timeout, max_retries)` and `AsyncPublicClient(api_key, base_url, timeout, max_retries)`

| Parameter     | Type                             | Required | Default           | Description                                         |
| ------------- | -------------------------------- | -------- | ----------------- | --------------------------------------------------- |
| `api_key`     | `str \| None`                    | Yes\*    | `None`            | Your LayerLens API key                              |
| `base_url`    | `str \| httpx.URL \| None`       | No       | LayerLens API URL | Custom API base URL                                 |
| `timeout`     | `float \| httpx.Timeout \| None` | No       | 10 minutes        | Request timeout configuration                       |
| `max_retries` | `int`                            | No       | `2`               | Retries of a 429 or a 5xx (500, 502, 503, 504); a lost connection is not retried |

\*Required unless set via the `LAYERLENS_API_KEY` environment variable

`client.public` on a `LayerLens` / `AsyncLayerLens` client is built with that client's key, URL,
timeout and `max_retries`.

Each method also takes `timeout=`: that call's timeout in place of the client's (seconds or an
`httpx.Timeout`; `None` is no timeout). Without it, the call uses the client's timeout: see
[Timeout Configuration](client.md#timeout-configuration).

## Public Models

### `models.get(...)`

Retrieves a list of public models with optional filtering, sorting, and pagination.

#### Parameters

| Parameter            | Type                   | Required | Description                                                                                  |
| -------------------- | ---------------------- | -------- | -------------------------------------------------------------------------------------------- |
| `query`              | `str \| None`          | No       | Full-text search on model name                                                               |
| `name`               | `str \| None`          | No       | Filter by model name                                                                         |
| `key`                | `str \| None`          | No       | Filter by model key                                                                          |
| `ids`                | `List[str] \| None`    | No       | Filter by specific model IDs                                                                 |
| `categories`         | `List[str] \| None`    | No       | Filter by categories (e.g. `transformer`, `moe`, `open-source`, `closed-source`, `usa`, `china`, `size-sm`, `size-md`, `size-lg`, `size-xl`) |
| `companies`          | `List[str] \| None`    | No       | Filter by company names                                                                      |
| `regions`            | `List[str] \| None`    | No       | Filter by regions                                                                            |
| `licenses`           | `List[str] \| None`    | No       | Filter by license types                                                                      |
| `sizes`              | `List[str] \| None`    | No       | Filter by size (Small, Medium, Large, Extra Large)                                           |
| `sort_by`            | `str \| None`          | No       | Sort column: `name`, `created_at`, `released_at`, `architecture_type`, `context_length`, `license`, `region` |
| `order`              | `str \| None`          | No       | Sort order: `asc` or `desc`                                                                  |
| `page`               | `int \| None`          | No       | Page number (1-based)                                                                        |
| `page_size`          | `int \| None`          | No       | Results per page                                                                             |
| `include_deprecated` | `bool \| None`         | No       | Include deprecated models (default: false)                                                   |
| `timeout`            | `float \| httpx.Timeout \| None` | No | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `PublicModelsListResponse` containing:

- `models`: List of `PublicModelDetail` objects (a page holds at most 100)
- `categories`: List of available category strings
- `count`: Number of models that match the filters, across all pages (deprecated ones only with `include_deprecated=True`)
- `total_count`: Number of models in the catalogue, deprecated ones included, whatever the filters

A request the API refuses raises its error; see [Errors](errors.md).

#### PublicModelDetail Properties

| Property               | Type             | Description                        |
| ---------------------- | ---------------- | ---------------------------------- |
| `id`                   | `str`            | Unique model identifier            |
| `key`                  | `str`            | Unique model key                   |
| `name`                 | `str`            | Human-readable model name          |
| `description`          | `str \| None`    | Text description                   |
| `company`              | `str \| None`    | Model provider company             |
| `released_at`          | `int \| None`    | Release timestamp                  |
| `parameters`           | `float \| None`  | Number of parameters               |
| `modality`             | `str \| None`    | Model modality                     |
| `context_length`       | `int \| None`    | Maximum context length             |
| `architecture_type`    | `str \| None`    | Architecture type                  |
| `license`              | `str \| None`    | License type                       |
| `open_weights`         | `bool \| None`   | Whether weights are open           |
| `region`               | `str \| None`    | Region                             |
| `key_takeaways`        | `List[str] \| None` | Key takeaways                   |
| `deprecated`           | `bool \| None`   | Whether the model is deprecated    |
| `cost_per_input_token` | `str \| None`    | Cost per input token               |
| `cost_per_output_token`| `str \| None`    | Cost per output token              |

#### Example

```python
from layerlens import PublicClient

client = PublicClient()

# Get newest OpenAI models
response = client.models.get(
    companies=["OpenAI"],
    sort_by="released_at",
    order="desc",
    page_size=5,
)

for model in response.models:
    print(f"{model.name} - {model.context_length} context length")
```

## Public Benchmarks

### `benchmarks.get(...)`

Retrieves a list of public benchmarks with optional filtering, sorting, and pagination.

#### Parameters

| Parameter            | Type                   | Required | Description                                |
| -------------------- | ---------------------- | -------- | ------------------------------------------ |
| `query`              | `str \| None`          | No       | Full-text search                           |
| `name`               | `str \| None`          | No       | Filter by name                             |
| `key`                | `str \| None`          | No       | Filter by key                              |
| `ids`                | `List[str] \| None`    | No       | Filter by specific IDs                     |
| `categories`         | `List[str] \| None`    | No       | Filter by categories                       |
| `languages`          | `List[str] \| None`    | No       | Filter by languages                        |
| `sort_by`            | `str \| None`          | No       | Sort column (currently: `name`)            |
| `order`              | `str \| None`          | No       | Sort order: `asc` or `desc`                |
| `page`               | `int \| None`          | No       | Page number (1-based)                      |
| `page_size`          | `int \| None`          | No       | Results per page                           |
| `include_deprecated` | `bool \| None`         | No       | Include deprecated benchmarks              |
| `timeout`            | `float \| httpx.Timeout \| None` | No | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `PublicBenchmarksListResponse` containing:

- `datasets`: List of `PublicBenchmarkDetail` objects (a page holds at most 100)
- `categories`: List of available category strings
- `count`: Number of benchmarks that match the filters, across all pages
- `total_count`: Number of benchmarks in the catalogue, whatever the filters

A request the API refuses raises its error; see [Errors](errors.md).

#### PublicBenchmarkDetail Properties

| Property          | Type               | Description                           |
| ----------------- | ------------------ | ------------------------------------- |
| `id`              | `str`              | Unique benchmark identifier           |
| `key`             | `str`              | Unique benchmark key                  |
| `name`            | `str`              | Human-readable name                   |
| `description`     | `str \| None`      | Text description (the API's `full_description`) |
| `short_description` | `str \| None`    | One-line summary                      |
| `prompt_count`    | `int \| None`      | Number of prompts in the benchmark    |
| `language`        | `str \| None`      | Language of the benchmark             |
| `categories`      | `List[str] \| None`| Categories                            |
| `characteristics` | `List[str] \| None`| Characteristics                       |
| `deprecated`      | `bool \| None`     | Whether the benchmark is deprecated   |
| `is_public`       | `bool \| None`     | Whether the benchmark is public       |

### `benchmarks.get_prompts(benchmark_id, ...)`

Fetches prompts/content from a public benchmark with optional search and pagination.

#### Parameters

| Parameter      | Type                   | Required | Description                                    |
| -------------- | ---------------------- | -------- | ---------------------------------------------- |
| `benchmark_id` | `str`                  | Yes      | The benchmark ID to fetch prompts from         |
| `page`         | `int \| None`          | No       | Page number (1-based)                          |
| `page_size`    | `int \| None`          | No       | Results per page                               |
| `search_field` | `str \| None`          | No       | Search field: `id`, `input`, or `truth`        |
| `search_value` | `str \| None`          | No       | Search value                                   |
| `sort_by`      | `str \| None`          | No       | Sort field: `id`, `input`, or `truth`          |
| `sort_order`   | `str \| None`          | No       | Sort order: `asc` or `desc`                    |
| `timeout`      | `float \| httpx.Timeout \| None` | No | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `BenchmarkPromptsResponse` containing:

- `status`: Response status string
- `data.prompts`: List of `BenchmarkPrompt` objects
- `data.count`: Total number of prompts

A request the API refuses raises its error; see [Errors](errors.md).

#### BenchmarkPrompt Properties

| Property | Type  | Description                            |
| -------- | ----- | -------------------------------------- |
| `id`     | `str` | Unique prompt identifier               |
| `input`  | `str \| List \| Dict` | The prompt input          |
| `truth`  | `str` | The expected/ground truth answer       |

### `benchmarks.get_all_prompts(benchmark_id)`

Fetches all prompts from a benchmark by automatically handling pagination.

#### Parameters

| Parameter      | Type                   | Required | Description                             |
| -------------- | ---------------------- | -------- | --------------------------------------- |
| `benchmark_id` | `str`                  | Yes      | The benchmark ID to fetch prompts from  |
| `timeout`      | `float \| httpx.Timeout \| None` | No | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `List[BenchmarkPrompt]` containing all prompts in the benchmark.

#### Example

```python
from layerlens import PublicClient

client = PublicClient()

# List benchmarks
benchmarks = client.benchmarks.get(query="mmlu")

if benchmarks and benchmarks.datasets:
    benchmark = benchmarks.datasets[0]

    # Get first page of prompts
    prompts = client.benchmarks.get_prompts(benchmark.id, page=1, page_size=10)

    if prompts:
        print(f"Total prompts: {prompts.data.count}")
        for prompt in prompts.data.prompts:
            print(f"  Input: {str(prompt.input)[:80]}...")
            print(f"  Truth: {prompt.truth[:50]}")

    # Or fetch all prompts at once
    all_prompts = client.benchmarks.get_all_prompts(benchmark.id)
    print(f"All prompts: {len(all_prompts)}")
```

## Evaluations

### `evaluations.get_by_id(id, ...)`

Retrieves a single evaluation by its unique identifier, including the full evaluation summary.

#### Parameters

| Parameter | Type                             | Required | Description                      |
| --------- | -------------------------------- | -------- | -------------------------------- |
| `id`      | `str`                            | Yes      | The unique evaluation identifier |
| `timeout` | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns an `Evaluation`. Raises `NotFoundError` if no evaluation has this id. See [Evaluations](evaluations.md) for the full `Evaluation` object properties.

### `evaluations.get_many(...)`

Retrieves evaluations with optional pagination, sorting, and filtering.

#### Parameters

| Parameter         | Type                             | Required | Description                                                                        |
| ----------------- | -------------------------------- | -------- | ---------------------------------------------------------------------------------- |
| `page`            | `int \| None`                    | No       | Page number for pagination (1-based, defaults to 1)                                |
| `page_size`       | `int \| None`                    | No       | Number of evaluations per page (default and maximum: 100)                            |
| `sort_by`         | `str \| None`                    | No       | Sort by field: `submitted_at`, `accuracy`, or `average_duration`                   |
| `order`           | `str \| None`                    | No       | Sort order: `asc` or `desc`                                                        |
| `model_ids`       | `List[str] \| None`              | No       | Filter by model IDs                                                                |
| `benchmark_ids`   | `List[str] \| None`              | No       | Filter by benchmark/dataset IDs                                                    |
| `status`          | `EvaluationStatus \| None`       | No       | Filter by evaluation status                                                        |
| `unique`          | `bool`                           | No       | If `True`, deduplicate by model+benchmark pair, keeping only the latest evaluation |
| `timeout`         | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns an `EvaluationsResponse` object containing:

- `evaluations`: List of `Evaluation` objects
- `pagination`: Pagination metadata with `page`, `page_size`, `total_pages`, and `total_count`

A request the API refuses raises its error; see [Errors](errors.md).

The API returns at most 100 rows a page: a larger `page_size` is read as 100, and `pagination.page_size` and `total_pages` say so, so paging through `total_pages` reads every evaluation.

#### Example

```python
from layerlens import PublicClient
from layerlens.models import EvaluationStatus

client = PublicClient()

# Get a specific evaluation by ID (with full summary); raises NotFoundError for an unknown id
evaluation = client.evaluations.get_by_id("eval_abc123")
print(f"{evaluation.model_name} on {evaluation.benchmark_name}: {evaluation.accuracy:.2f}%")
if evaluation.summary:
    print(f"Goal: {evaluation.summary.goal}")
    for takeaway in evaluation.summary.analysis_summary.key_takeaways:
        print(f"  - {takeaway}")

# List successful evaluations sorted by accuracy
response = client.evaluations.get_many(
    status=EvaluationStatus.SUCCESS,
    sort_by="accuracy",
    order="desc",
    page_size=10,
)
if response:
    print(f"Top evaluations ({response.pagination.total_count} total):")
    for e in response.evaluations:
        print(f"  {e.model_name}: {e.accuracy:.2f}%")
```

## Comparisons

### `comparisons.compare(...)`

Compares results between two evaluations side-by-side.

#### Parameters

| Parameter          | Type                   | Required | Description                                                                |
| ------------------ | ---------------------- | -------- | -------------------------------------------------------------------------- |
| `evaluation_id_1`  | `str`                  | Yes      | First evaluation ID                                                        |
| `evaluation_id_2`  | `str`                  | Yes      | Second evaluation ID                                                       |
| `page`             | `int \| None`          | No       | Page number (1-based)                                                      |
| `page_size`        | `int \| None`          | No       | Results per page                                                           |
| `outcome_filter`   | `str \| None`          | No       | Filter by outcome (see below)                                              |
| `search`           | `str \| None`          | No       | Search within results                                                      |
| `timeout`          | `float \| httpx.Timeout \| None` | No | This call's timeout instead of the client's; `None` is no timeout |

#### Outcome Filter Options

| Value                | Description                                    |
| -------------------- | ---------------------------------------------- |
| `"all"`              | All results (default)                          |
| `"both_succeed"`     | Both models answered correctly                 |
| `"both_fail"`        | Both models answered incorrectly               |
| `"reference_fails"`  | First model fails, second succeeds             |
| `"comparison_fails"` | Second model fails, first succeeds             |

#### Returns

Returns a `ComparisonResponse` containing:

- `results`: List of `ComparisonResult` objects
- `total_count`: Total number of comparable results
- `correct_count_1`: Number of correct answers for evaluation 1
- `total_results_1`: Total results for evaluation 1
- `correct_count_2`: Number of correct answers for evaluation 2
- `total_results_2`: Total results for evaluation 2

A request the API refuses raises its error; see [Errors](errors.md).

#### ComparisonResult Properties

| Property      | Type            | Description                           |
| ------------- | --------------- | ------------------------------------- |
| `result_id_1` | `int \| None`   | Result ID from evaluation 1           |
| `result_id_2` | `int \| None`   | Result ID from evaluation 2           |
| `prompt`      | `str`           | The prompt text                       |
| `truth`       | `str`           | The ground truth answer               |
| `result1`     | `str \| None`   | Model 1's response                    |
| `score1`      | `float \| None` | Model 1's score                       |
| `result2`     | `str \| None`   | Model 2's response                    |
| `score2`      | `float \| None` | Model 2's score                       |

#### Example

```python
from layerlens import PublicClient

client = PublicClient()

comparison = client.comparisons.compare(
    evaluation_id_1="eval-abc",
    evaluation_id_2="eval-def",
    outcome_filter="reference_fails",
    page=1,
    page_size=20,
)

if comparison:
    print(f"Eval 1: {comparison.correct_count_1}/{comparison.total_results_1}")
    print(f"Eval 2: {comparison.correct_count_2}/{comparison.total_results_2}")

    for result in comparison.results:
        print(f"  Prompt: {result.prompt[:80]}...")
        print(f"  Model 1 score: {result.score1}, Model 2 score: {result.score2}")
```

### `comparisons.compare_models(...)`

Compares two models on a benchmark by automatically finding their most recent successful evaluations. This is a convenience method that wraps `compare()`.

#### Parameters

| Parameter        | Type                   | Required | Description                                |
| ---------------- | ---------------------- | -------- | ------------------------------------------ |
| `benchmark_id`   | `str`                  | Yes      | Benchmark ID to compare on                 |
| `model_id_1`     | `str`                  | Yes      | First model ID                             |
| `model_id_2`     | `str`                  | Yes      | Second model ID                            |
| `page`           | `int \| None`          | No       | Page number (1-based)                      |
| `page_size`      | `int \| None`          | No       | Results per page                           |
| `outcome_filter` | `str \| None`          | No       | Filter by outcome (same options as `compare`) |
| `search`         | `str \| None`          | No       | Search within results                      |
| `timeout`        | `float \| httpx.Timeout \| None` | No | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `ComparisonResponse` (same as `compare()`). A request the API refuses raises its error.

Raises `ValueError` if no successful evaluation is found for either model on the given benchmark.

#### Example

```python
from layerlens import PublicClient

client = PublicClient()

# Compare two models on AIME 2025 - no need to look up evaluation IDs
comparison = client.comparisons.compare_models(
    benchmark_id="682bddc1e014f9fa440f8a91",
    model_id_1="699f9761e014f9c3072b0513",
    model_id_2="699f9761e014f9c3072b0512",
    page=1,
    page_size=10,
)

if comparison:
    print(f"Model 1: {comparison.correct_count_1}/{comparison.total_results_1} correct")
    print(f"Model 2: {comparison.correct_count_2}/{comparison.total_results_2} correct")
```
