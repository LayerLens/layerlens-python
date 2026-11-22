#!/usr/bin/env python3
"""Example: working with integrations via the LayerLens SDK.

Requires:
    uv pip install --extra-index-url https://sdk.layerlens.ai/package layerlens
    export LAYERLENS_API_KEY="your-api-key"
"""

import sys

from layerlens import LayerLens, BadRequestError


def main() -> None:
    try:
        client = LayerLens()
        client.organization_id  # checks the key and URL now: building the client makes no request
    except Exception as exc:
        print(f"ERROR: Failed to initialize LayerLens client: {exc}")
        sys.exit(1)

    # --- List all integrations
    response = client.integrations.get_many()

    if response is None or not response.integrations:
        print("No integrations found.")
    else:
        print(f"Found {response.total_count} integration(s):\n")
        for integration in response.integrations:
            print(f"  [{integration.id}] {integration.name}")
            print(f"    Type:    {integration.type}")
            print(f"    Status:  {integration.status}")
            print(f"    Created: {integration.created_at}")
            print()

    # --- List with pagination
    page1 = client.integrations.get_many(page=1, page_size=5)
    if page1:
        print(f"Page 1: showing {page1.count} of {page1.total_count}")

    # --- Get a single integration by ID
    if response and response.integrations:
        integration_id = response.integrations[0].id

        integration = client.integrations.get(integration_id)
        if integration:
            print(f"\nIntegration detail:")
            print(f"  ID:     {integration.id}")
            print(f"  Name:   {integration.name}")
            print(f"  Type:   {integration.type}")
            print(f"  Status: {integration.status}")
            print(f"  Config: {integration.config}")

        # --- Test an integration. The API tests Langfuse integrations only, and refuses
        # another kind with a 400 that says so.
        try:
            result = client.integrations.test(integration_id)
        except BadRequestError as exc:
            print(f"\nTest skipped: {exc}")
            result = None
        if result:
            status = "OK" if result.success else "FAILED"
            print(f"\nTest result: {status}")
            if result.message:
                print(f"  Message: {result.message}")


if __name__ == "__main__":
    main()
