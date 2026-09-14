import "@testing-library/jest-dom/vitest";

// `lib/dynamodb.ts` reads these at import time. The values are never used
// against AWS: every test mocks the DynamoDB document client's `send`.
process.env.INCIDENT_TABLE_NAME ??= "incidents-test";
process.env.SERVICE_REGISTRY_TABLE_NAME ??= "service-registry-test";
process.env.AWS_ROLE_ARN ??= "arn:aws:iam::000000000000:role/test-only";
