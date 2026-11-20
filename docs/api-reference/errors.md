# Error Handling

The LayerLens Python SDK provides a comprehensive exception hierarchy to help you handle different error conditions gracefully. This guide covers all available exception types and best practices for error handling.

## Exception Hierarchy

All LayerLens exceptions inherit from the base `LayerLensError` class:

```
LayerLensError
├── EnvironmentBuildError
├── APIError
│   ├── APIConnectionError
│   │   └── APITimeoutError
│   ├── APIResponseValidationError
│   └── APIStatusError
│       ├── BadRequestError (400)
│       ├── AuthenticationError (401)
│       ├── PaymentRequiredError (402)
│       ├── PermissionDeniedError (403)
│       ├── NotFoundError (404)
│       ├── ConflictError (409)
│       │   └── EnvironmentConflictError
│       ├── UnprocessableEntityError (422)
│       ├── RateLimitError (429)
│       └── InternalServerError (500+)
```

## Exception Types

Several examples below run an evaluation. `evaluations.create` takes the `Model` and the
`Benchmark` objects, not their keys, so look them up first, with a key that works (the keys
must name a model and a benchmark your project holds). The examples for a base, connection,
timeout or key error list evaluations instead, so they need no setup. Each catches only its own
error: the connection example needs LayerLens out of reach (with a wrong key it raises
`AuthenticationError`), and the timeout example a slow answer (with no network at all it raises
`APIConnectionError`, as [Network Issues](#network-issues) shows):

```python
import layerlens

client = layerlens.LayerLens()
model = client.models.get_by_key("openai/gpt-4o")
benchmark = client.benchmarks.get_by_key("arc-agi-2")
```

### Base Exceptions

#### `LayerLensError`

Base exception for all LayerLens-related errors.

```python
import layerlens

try:
    client = layerlens.LayerLens()
    evaluations = client.evaluations.get_many(page_size=10)
except layerlens.LayerLensError as e:
    print(f"LayerLens error occurred: {e}")
```

#### `APIError`

Base exception for all API-related errors. Contains additional context about the request.

**Properties:**

- `message`: Error message
- `request`: The HTTP request that caused the error
- `body`: Response body (if available)

```python
import layerlens

try:
    client = layerlens.LayerLens()
    evaluations = client.evaluations.get_many(page_size=10)
except layerlens.APIError as e:
    print(f"API error: {e.message}")
    print(f"Request URL: {e.request.url}")
    print(f"Response body: {e.body}")
```

### Connection Errors

#### `APIConnectionError`

Raised when the client cannot connect to the API server.

**Common causes:**

- Network connectivity issues
- DNS resolution problems
- Server is down
- Firewall blocking requests

```python
import layerlens

try:
    client = layerlens.LayerLens()
    evaluations = client.evaluations.get_many(page_size=10)
except layerlens.APIConnectionError as e:
    print("Connection failed - check your network connection")
    print(f"Error details: {e}")
```

#### `APITimeoutError`

Raised when a request times out.

```python
import layerlens

try:
    client = layerlens.LayerLens(timeout=0.2)  # Very short timeout
    evaluations = client.evaluations.get_many(page_size=10)
except layerlens.APITimeoutError:
    print("Request timed out - try increasing timeout or check network")
```

### HTTP Status Errors

All HTTP status errors inherit from `APIStatusError` and include additional properties:

**Properties:**

- `status_code`: HTTP status code
- `response`: Full HTTP response object
- `request_id`: Request ID for tracking (if provided by server)

#### `BadRequestError` (400)

Request was malformed or contained invalid parameters.

```python
import layerlens

try:
    client = layerlens.LayerLens()
    # The API needs an evaluation goal of at least 10 characters
    judge = client.judges.create(name="Quality", evaluation_goal="Too short")
except layerlens.BadRequestError as e:
    print(f"Bad request: {e}")
    print(f"Status code: {e.status_code}")
```

#### `AuthenticationError` (401)

API key is missing, invalid, or expired.

```python
import layerlens

try:
    client = layerlens.LayerLens(api_key="invalid_key")
    evaluations = client.evaluations.get_many(page_size=10)
except layerlens.AuthenticationError:
    print("Authentication failed - check your API key")
    print("Make sure LAYERLENS_API_KEY is set correctly")
```

#### `PaymentRequiredError` (402)

The organization has no credit left for this request: top up, or wait for running evaluations
to finish. A request that costs credit, such as creating an evaluation, a trace evaluation or a
judge optimization, gets it; an evaluation holds its cost until it finishes. Reads are not
affected.

```python
import layerlens

try:
    client = layerlens.LayerLens()
    evaluation = client.evaluations.create(model=model, benchmark=benchmark)
except layerlens.PaymentRequiredError as e:
    print(f"No credit left: {e}")
    print("Top up the organization's credit, or wait for running evaluations to finish")
```

The client does not retry a 402. In 1.11.0 a 402 raised a plain `APIStatusError`;
`except layerlens.APIStatusError` still catches it.

#### `PermissionDeniedError` (403)

Valid API key but insufficient permissions for the requested operation. For example, reading a
trace an environment run recorded needs an API key with Automation access: a key with Standard
access gets this error for it, and its trace list leaves those traces out. A key API Gateway
refuses also gets a bare 403 `Forbidden`.

```python
import layerlens

try:
    client = layerlens.LayerLens()
    trace = client.traces.get(attempt_trace_id)  # a trace an environment run recorded
except layerlens.PermissionDeniedError as e:
    print(f"Permission denied: {e.body}")
    print("Use an API key with the access it names (Manage organization → API keys)")
```

#### `NotFoundError` (404)

Requested resource (model, benchmark, evaluation, trace, judge, ...) does not exist. A `get`,
`get_by_id` or `delete` raises it for an unknown id, with these exceptions:

- `scorers.delete` and `scorers.update` return `False` for any failure, an unknown id included.
- `models.delete_custom` raises `BadRequestError` (400) for an id that names no custom model.
- `models.get_by_key`, `benchmarks.get_by_key` and `trace_evaluations.get_results` return
  `None` when there is nothing to return.

```python
import layerlens

try:
    client = layerlens.LayerLens()
    evaluation = client.evaluations.get_by_id("000000000000000000000000")
except layerlens.NotFoundError as e:
    print(f"Not found: {e.body}")
```

#### `ConflictError` (409)

Request conflicts with current resource state.

```python
import layerlens

try:
    client = layerlens.LayerLens()
    # A scorer name is unique in a project
    scorer = client.scorers.create(
        name="response-rubric",
        description="Rate the response on factual accuracy",
        model_id=model.id,
        prompt="Rate the response on a 1-5 scale based on factual accuracy.",
    )
except layerlens.ConflictError:
    print("A scorer with this name already exists")
```

#### `EnvironmentConflictError` (409)

A `ConflictError` from `client.environments.create`: the name is taken by an environment
the call cannot reuse. `differences` names the inputs that differ (with
`if_not_exists=True`), and `project_id` the project that holds the name, when it is another.

#### `EnvironmentBuildError`

Raised by `client.environments.wait_until_ready` when the environment's build fails.
`refused_because` holds the generator's reason. See [Environments](environments.md).

#### `UnprocessableEntityError` (422)

Request parameters are valid but cannot be processed.

```python
import layerlens

try:
    client = layerlens.LayerLens()
    evaluation = client.evaluations.create(model=model, benchmark=benchmark)
except layerlens.UnprocessableEntityError as e:
    print(f"Cannot process request: {e}")
    print("Parameters are valid but operation cannot be completed")
```

#### `RateLimitError` (429)

Too many requests sent in a given time period. The client has already retried the request
(twice by default; see [Retry Configuration](client.md#retry-configuration)) by the time it
raises this.

The reply's `Retry-After` header says how long to wait, as a number of seconds (`120`) or as
an HTTP date (`Wed, 21 Oct 2026 07:28:00 GMT`); RFC 9110 allows both, so read it with a
helper rather than `int()`, which fails on a date:

```python
import math
import time
from email.utils import mktime_tz, parsedate_tz

import layerlens


def retry_after_seconds(error: layerlens.APIStatusError, default: float) -> float:
    """The seconds a 429 or 503 asks you to wait, or `default` without a usable header."""
    value = error.response.headers.get("retry-after", "")
    try:
        seconds = float(value)
    except ValueError:  # not a number, so perhaps a date
        date = parsedate_tz(value)
        try:
            seconds = max(mktime_tz(date) - time.time(), 0.0) if date else default
        except (ValueError, OverflowError):  # a date no calendar holds
            seconds = default
    else:
        if not math.isfinite(seconds) or seconds < 0:  # such as -1 or inf
            seconds = default
    return seconds


deadline = time.monotonic() + 600  # the longest this job waits for LayerLens
try:
    client = layerlens.LayerLens()
    evaluation = client.evaluations.create(model=model, benchmark=benchmark)
except layerlens.RateLimitError as e:
    wait = retry_after_seconds(e, default=60)
    if time.monotonic() + wait > deadline:
        raise  # LayerLens asks for longer than this job can wait: stop now, with the 429
    print(f"Rate limit exceeded; waiting {wait:.0f} seconds before retrying")
    time.sleep(wait)
```

Never retry sooner than `Retry-After` asks: LayerLens answers such a retry with another 429.
A deadline of your own also keeps a far-off date from parking your program (and
`time.sleep()` raises `OverflowError` past about 292 years).

`wait_for_completion` and the environments `wait` methods do this themselves: they keep
polling through a 429 until their timeout. A wait never polls sooner than a `Retry-After` asks. When the `Retry-After`
reaches past the wait's deadline, the wait sleeps until the deadline and then raises, with the
429 as the cause. A wait with no deadline (`timeout_seconds=None`, or `0` on an evaluation
wait) reads a `Retry-After` longer than 5 minutes as 5 minutes.

#### `InternalServerError` (500+)

Server-side error occurred.

```python
import layerlens

try:
    client = layerlens.LayerLens()
    evaluation = client.evaluations.create(model=model, benchmark=benchmark)
except layerlens.InternalServerError as e:
    print(f"Server error: {e.status_code}")
    print("This is a server-side issue - try again later")
    print(f"Request ID: {e.request_id}")  # For support tickets
```

## Best Practices

### 1. Handle Specific Exceptions

```python
import layerlens
import time
from layerlens import LayerLens
from layerlens.models import Benchmark, Model

def robust_create_evaluation(model: Model, benchmark: Benchmark, max_retries: int = 3):
    client = LayerLens()

    for attempt in range(max_retries):
        try:
            evaluation = client.evaluations.create(model=model, benchmark=benchmark)
            return evaluation

        except layerlens.AuthenticationError:
            print("❌ Authentication failed - check your API key")
            break  # Don't retry auth errors

        except layerlens.PermissionDeniedError:
            print("❌ Permission denied - contact your administrator")
            break  # Don't retry permission errors

        except layerlens.NotFoundError:
            print(f"❌ Model '{model.key}' or benchmark '{benchmark.key}' not found")
            break  # Don't retry not found errors

        except layerlens.RateLimitError as e:
            wait = retry_after_seconds(e, default=60)  # the helper under RateLimitError above
            if wait > 600:
                print("❌ Rate limited for longer than this job waits")
                break
            print(f"⏳ Rate limited - waiting {wait:.0f} seconds...")
            time.sleep(wait)
            continue  # Retry after waiting

        except layerlens.InternalServerError:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff
                print(f"🔄 Server error - retrying in {wait_time}s (attempt {attempt + 1})")
                time.sleep(wait_time)
                continue
            else:
                print("❌ Server error - max retries exceeded")
                break

        except layerlens.APIConnectionError:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                print(f"🔄 Connection error - retrying in {wait_time}s (attempt {attempt + 1})")
                time.sleep(wait_time)
                continue
            else:
                print("❌ Connection failed - check your network")
                break

        except layerlens.APIError as e:
            print(f"❌ Unexpected API error: {e}")
            break

    return None
```

### 2. Graceful Degradation

```python
import layerlens
from layerlens import LayerLens

def get_evaluation_results_with_fallback(evaluation_id: str):
    client = LayerLens()

    try:
        results = client.results.get_by_id(evaluation_id=evaluation_id)

        if results:
            return {"success": True, "data": results, "message": "Results retrieved successfully"}
        else:
            return {"success": False, "data": None, "message": "No results found"}

    except layerlens.NotFoundError:
        return {"success": False, "data": None, "message": "Evaluation not found"}

    except layerlens.AuthenticationError:
        return {"success": False, "data": None, "message": "Authentication required"}

    except layerlens.APIConnectionError:
        return {"success": False, "data": None, "message": "Service temporarily unavailable"}

    except layerlens.APIError as e:
        return {"success": False, "data": None, "message": f"Service error: {e}"}

# Usage
result = get_evaluation_results_with_fallback("eval_123")
if result["success"]:
    process_results(result["data"])
else:
    print(f"Could not get results: {result['message']}")
```

### 3. Logging and Monitoring

```python
import logging
import layerlens
from layerlens import LayerLens

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def monitored_api_call(model, benchmark):
    client = LayerLens()

    try:
        logger.info("Creating evaluation...")
        evaluation = client.evaluations.create(model=model, benchmark=benchmark)
        if evaluation is None:  # the API's answer held no evaluation
            logger.warning("Evaluation creation returned None")
            return None
        logger.info(f"Evaluation created successfully: {evaluation.id}")
        return evaluation

    except layerlens.RateLimitError as e:
        logger.warning(f"Rate limited - request ID: {e.request_id}")
        raise

    except layerlens.AuthenticationError:
        logger.error("Authentication failed - check API key configuration")
        raise

    except layerlens.APIConnectionError:
        logger.error("Network connection failed")
        raise

    except layerlens.InternalServerError as e:
        logger.error(f"Server error: {e.status_code} - request ID: {e.request_id}")
        raise

    except layerlens.APIError as e:
        logger.error(f"Unexpected API error: {e} - request ID: {getattr(e, 'request_id', 'N/A')}")
        raise
```

### 4. Context Managers for Resource Management

```python
import layerlens
from contextlib import contextmanager
from layerlens import LayerLens

@contextmanager
def layerlens_client():
    """Context manager for LayerLens client with error handling"""
    client = None
    try:
        client = LayerLens()
        yield client
    except layerlens.AuthenticationError:
        print("Authentication failed")
        raise
    except layerlens.APIConnectionError:
        print("Connection failed")
        raise
    finally:
        # Cleanup if needed
        pass

# Usage
try:
    with layerlens_client() as client:
        evaluation = client.evaluations.create(model=model, benchmark=benchmark)
        results = client.results.get(evaluation=evaluation)
except layerlens.LayerLensError:
    print("LayerLens operation failed")
```

## Error Response Details

### Status Error Properties

```python
import layerlens
from layerlens import LayerLens

try:
    client = LayerLens()
    evaluation = client.evaluations.get_by_id("000000000000000000000000")
except layerlens.APIStatusError as e:
    print(f"Status Code: {e.status_code}")
    print(f"Request ID: {e.request_id}")
    print(f"Response Headers: {dict(e.response.headers)}")
    print(f"Response Body: {e.body}")
    print(f"Request URL: {e.request.url}")
    print(f"Request Method: {e.request.method}")
```

### Extracting Useful Information

```python
import layerlens
from layerlens import LayerLens

def extract_error_info(error: layerlens.APIError):
    info = {
        "type": type(error).__name__,
        "message": str(error),
        "request_url": error.request.url if hasattr(error, 'request') else None,
        "request_method": error.request.method if hasattr(error, 'request') else None,
    }

    if hasattr(error, 'status_code'):
        info["status_code"] = error.status_code

    if hasattr(error, 'request_id'):
        info["request_id"] = error.request_id

    if hasattr(error, 'response'):
        info["response_headers"] = dict(error.response.headers)

    return info

# Usage
try:
    client = LayerLens()
    evaluation = client.evaluations.create(model=model, benchmark=benchmark)
except layerlens.APIError as e:
    error_info = extract_error_info(e)
    print(f"Error details: {error_info}")
```

## Testing Error Handling

Build the error your code should handle, and raise it from a mock:

```python
from unittest.mock import Mock

import httpx
import layerlens


def create_or_report(create):
    try:
        return create()
    except layerlens.AuthenticationError:
        return "check your API key"


def test_authentication_error_handling():
    """Test that authentication errors are handled properly"""
    request = httpx.Request("GET", "https://api.layerlens.ai/api/v1/organizations")
    error = layerlens.AuthenticationError(
        "Error code: 401 - {'status': 'error', 'error': 'Unknown API key'}",
        response=httpx.Response(401, request=request),
        body="Unknown API key",
    )
    assert create_or_report(Mock(side_effect=error)) == "check your API key"

def test_rate_limit_retry():
    """Test that rate limit errors trigger appropriate retry logic"""
    # Your retry logic test here
    pass
```

## Common Error Scenarios

### Invalid Configuration

```python
import layerlens
from layerlens import LayerLens

# Missing API key (with LAYERLENS_API_KEY unset too)
try:
    client = LayerLens(api_key=None)
except layerlens.LayerLensError as e:
    print(f"Configuration error: {e}")
```

### Network Issues

These need `LAYERLENS_API_KEY` set, to any value (without a key, `LayerLens()` raises
`LayerLensError` before any request), and nothing else, so they run as written without a
network. `APITimeoutError` is a kind of `APIConnectionError`, so catch it first:

```python
import layerlens
from layerlens import LayerLens

# Connection timeout
try:
    client = LayerLens(timeout=0.001)  # A timeout no request can meet
    evaluations = client.evaluations.get_many(page_size=10)
except layerlens.APITimeoutError:
    print("Request timed out")
except layerlens.APIConnectionError:  # no network: the connection fails before it times out
    print("Network connectivity issue")

# Network connectivity (an address nothing answers on stands in for a network outage)
try:
    client = LayerLens(base_url="http://127.0.0.1:9/api/v1")
    evaluations = client.evaluations.get_many(page_size=10)
except layerlens.APIConnectionError:
    print("Network connectivity issue")
```

## Error Recovery Strategies

### Exponential Backoff

```python
import time
import random
import layerlens
from layerlens import LayerLens

def exponential_backoff_retry(func, max_retries=3, base_delay=1):
    """Retry function with exponential backoff"""
    for attempt in range(max_retries):
        try:
            return func()
        except (layerlens.InternalServerError, layerlens.APIConnectionError) as e:
            if attempt == max_retries - 1:
                raise

            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            print(f"Attempt {attempt + 1} failed, retrying in {delay:.2f}s...")
            time.sleep(delay)

# Usage
def create_evaluation():
    client = LayerLens()
    return client.evaluations.create(model=model, benchmark=benchmark)

evaluation = exponential_backoff_retry(create_evaluation)
```

### Circuit Breaker Pattern

```python
import time
from enum import Enum
from layerlens import LayerLens
import layerlens

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    def __init__(self, failure_threshold=5, timeout=60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED

    def call(self, func, *args, **kwargs):
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time < self.timeout:
                raise RuntimeError("Circuit breaker is OPEN")
            else:
                self.state = CircuitState.HALF_OPEN

        try:
            result = func(*args, **kwargs)
            self.on_success()
            return result
        except (layerlens.InternalServerError, layerlens.APIConnectionError) as e:
            self.on_failure()
            raise

    def on_success(self):
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def on_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN

# Usage
breaker = CircuitBreaker()
client = LayerLens()

try:
    evaluation = breaker.call(
        client.evaluations.create,
        model=model,
        benchmark=benchmark,
    )
except (layerlens.APIError, RuntimeError) as e:
    print(f"Circuit breaker prevented call or operation failed: {e}")
```
