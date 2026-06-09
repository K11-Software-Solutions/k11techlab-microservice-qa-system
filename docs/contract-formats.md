# Contract Formats

The K11tech Microservice QA System supports three API contract formats for cross-repository impact analysis.

## OpenAPI 3.x / Swagger 2.0

The primary and most feature-complete format. The extractor detects files named `openapi.yaml`, `openapi.json`, `swagger.yaml`, or `swagger.json` in a PR diff.

**Detected breaking changes:**
- Endpoint removed (`DELETE /api/v2/users/{id}` disappears from paths)
- Required field added to request body
- Field type changed in request/response schema
- Required parameter added (path or query)
- Parameter removed
- Success response code removed (2xx codes only)

**Example contract file:**
```yaml
openapi: 3.0.0
info:
  title: User Service
  version: 2.1.0
paths:
  /api/v2/users/{id}:
    get:
      summary: Get user by ID
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: OK
        "404":
          description: Not Found
```

## gRPC / Protocol Buffers

Detected from `.proto` files. The extractor parses service and rpc definitions using regex — it does not require the full protobuf compiler.

**Detected breaking changes:**
- RPC method removed from a service
- Request/response message type changes (via proto diff)

**Example:**
```proto
syntax = "proto3";
package users.v2;

service UserService {
  rpc GetUser (GetUserRequest) returns (UserResponse);
  rpc CreateUser (CreateUserRequest) returns (UserResponse);
}
```

## GraphQL

Detected from `.graphql` files. The extractor parses `Query`, `Mutation`, and `Subscription` type blocks.

**Detected breaking changes:**
- Field removed from Query/Mutation type
- Field type changed

**Example:**
```graphql
type Query {
  user(id: ID!): User
  users(page: Int): [User]
}

type Mutation {
  createUser(email: String!, name: String!): User
  updateUser(id: ID!, name: String): User
}
```

## AsyncAPI 2.x

Parsed identically to OpenAPI. AsyncAPI files (`asyncapi.yaml`) are treated as message contract definitions. Breaking changes are detected the same way — removed channels, changed message schemas.

---

## Registration

Register your service and its contract path:

```bash
curl -X POST http://localhost:9003/services/register \
  -H "Content-Type: application/json" \
  -d '{
    "name":          "user-service",
    "repo":          "org/user-service",
    "contract_path": "openapi.yaml",
    "team":          "platform",
    "slack_channel": "#team-platform"
  }'
```

The pipeline will automatically detect changes to this file in future PRs.
