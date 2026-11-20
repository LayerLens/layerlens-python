# Authentication Problems

This guide covers common authentication-related issues when using the Layerlens python sdk.

## Common Authentication Errors

### Invalid or Missing API Key

**Error**: `AuthenticationError` (401) or `PermissionDeniedError: Error code: 403 - {'message': 'Forbidden'}`

**Symptoms**:

- The first call fails, or reading `client.organization_id` does. Building the client makes no request, so it doesn't fail there.
- A bare `403 Forbidden`, with no sentence, comes from API Gateway, which refused the key before LayerLens saw the request.

**Cause**: the key is wrong, disabled or deleted. Check that it is enabled in **Manage organization → API keys**.

### Missing Required Configuration

**Error**: `LayerLensError: The api_key client option must be set either by passing api_key to the client or by setting the LAYERLENS_API_KEY environment variable`