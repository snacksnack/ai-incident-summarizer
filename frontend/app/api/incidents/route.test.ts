// @vitest-environment node
import { DynamoDBDocumentClient, QueryCommand } from "@aws-sdk/lib-dynamodb";
import { mockClient } from "aws-sdk-client-mock";
import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Incident } from "@/lib/types";
import { GET } from "./route";

// Filter composition (RC1-217): service and status compose, "all" fans out
// across the known statuses, and an unknown status is rejected. The document
// client's `send` is mocked at the prototype, so the real client in
// `lib/dynamodb.ts` is exercised without credentials or a table.
const ddb = mockClient(DynamoDBDocumentClient);

function incident(overrides: Partial<Incident>): Incident {
  return {
    incident_id: "INC-0",
    affected_service: "payments-service",
    severity: "high",
    status: "open",
    created_at: "2026-09-14T10:00:00+00:00",
    source_alerts: [],
    ...overrides,
  };
}

function get(query: string) {
  return GET(new NextRequest(`http://localhost/api/incidents${query}`));
}

function queryInputs() {
  return ddb.commandCalls(QueryCommand).map((call) => call.args[0].input);
}

beforeEach(() => {
  ddb.reset();
  vi.spyOn(console, "error").mockImplementation(() => {});
});

describe("GET /api/incidents", () => {
  it("applies the status filter when a service and a specific status are given", async () => {
    ddb.on(QueryCommand).resolves({ Items: [incident({ status: "resolved" })] });

    const res = await get("?service=payments-service&status=resolved");

    expect(res.status).toBe(200);
    expect(await res.json()).toHaveLength(1);
    const [input] = queryInputs();
    expect(queryInputs()).toHaveLength(1);
    expect(input.IndexName).toBe("service-created-index");
    expect(input.FilterExpression).toBe("#st = :st");
    expect(input.ExpressionAttributeValues).toEqual({ ":s": "payments-service", ":st": "resolved" });
  });

  it("omits the status filter for a service with status=all", async () => {
    ddb.on(QueryCommand).resolves({ Items: [] });

    await get("?service=payments-service&status=all");

    const [input] = queryInputs();
    expect(input.IndexName).toBe("service-created-index");
    expect(input.FilterExpression).toBeUndefined();
    expect(input.ExpressionAttributeNames).toBeUndefined();
    expect(input.ExpressionAttributeValues).toEqual({ ":s": "payments-service" });
  });

  it("fans status=all out across the known statuses and merges newest first", async () => {
    ddb.on(QueryCommand).callsFake((input: { ExpressionAttributeValues: { ":s": string } }) => {
      const status = input.ExpressionAttributeValues[":s"];
      const byStatus: Record<string, Incident[]> = {
        open: [incident({ incident_id: "INC-open", status, created_at: "2026-09-14T09:00:00+00:00" })],
        acknowledged: [
          incident({ incident_id: "INC-ack", status, created_at: "2026-09-14T11:00:00+00:00" }),
        ],
        resolved: [
          incident({ incident_id: "INC-res-1", status, created_at: "2026-09-14T10:00:00+00:00" }),
          incident({ incident_id: "INC-res-2", status, created_at: "2026-09-13T10:00:00+00:00" }),
        ],
      };
      return { Items: byStatus[status] };
    });

    const res = await get("?status=all");

    expect(res.status).toBe(200);
    const ids = ((await res.json()) as Incident[]).map((i) => i.incident_id);
    expect(ids).toEqual(["INC-ack", "INC-res-1", "INC-open", "INC-res-2"]);
    expect(queryInputs().map((i) => i.ExpressionAttributeValues?.[":s"])).toEqual([
      "open",
      "acknowledged",
      "resolved",
    ]);
    expect(new Set(queryInputs().map((i) => i.IndexName))).toEqual(new Set(["status-created-index"]));
  });

  it("defaults to open on the status index when nothing is given", async () => {
    ddb.on(QueryCommand).resolves({ Items: [] });

    await get("");

    expect(queryInputs()).toHaveLength(1);
    expect(queryInputs()[0].IndexName).toBe("status-created-index");
    expect(queryInputs()[0].ExpressionAttributeValues).toEqual({ ":s": "open" });
  });

  it("rejects an unrecognised status with 400 before touching DynamoDB", async () => {
    const res = await get("?status=closed");

    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "Unknown status: closed" });
    expect(queryInputs()).toHaveLength(0);
  });

  it("answers 500 when the query fails, without leaking the error", async () => {
    ddb.on(QueryCommand).rejects(new Error("ProvisionedThroughputExceededException"));

    const res = await get("?status=open");

    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "Failed to fetch incidents" });
  });
});
