import { NextRequest, NextResponse } from "next/server";
import { QueryCommand } from "@aws-sdk/lib-dynamodb";
import { dynamo, TABLE } from "@/lib/dynamodb";
import { Incident } from "@/lib/types";

const STATUSES = ["open", "acknowledged", "resolved"];
const ALL_STATUSES = "all";

function byCreatedAtDesc(a: Incident, b: Incident) {
  return b.created_at.localeCompare(a.created_at);
}

/** All incidents for one service, optionally narrowed to a single status. */
function queryByService(service: string, status: string) {
  const narrowed = status !== ALL_STATUSES;
  return new QueryCommand({
    TableName: TABLE,
    IndexName: "service-created-index",
    KeyConditionExpression: "affected_service = :s",
    // Status is not part of this index's key, so it has to be a filter. The
    // partition is one service (~9 incidents), so filtering after the read is
    // cheap here — unlike filtering across the whole table.
    ...(narrowed && {
      FilterExpression: "#st = :st",
      ExpressionAttributeNames: { "#st": "status" },
    }),
    ExpressionAttributeValues: narrowed ? { ":s": service, ":st": status } : { ":s": service },
    ScanIndexForward: false,
  });
}

function queryByStatus(status: string) {
  return new QueryCommand({
    TableName: TABLE,
    IndexName: "status-created-index",
    KeyConditionExpression: "#st = :s",
    ExpressionAttributeNames: { "#st": "status" },
    ExpressionAttributeValues: { ":s": status },
    ScanIndexForward: false,
  });
}

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const service = searchParams.get("service");
  const status = searchParams.get("status") ?? "open";

  if (status !== ALL_STATUSES && !STATUSES.includes(status)) {
    return NextResponse.json({ error: `Unknown status: ${status}` }, { status: 400 });
  }

  try {
    // Service and status compose. Previously a service filter silently ignored
    // status, so the UI could show resolved incidents while claiming "open".
    if (service) {
      const result = await dynamo.send(queryByService(service, status));
      return NextResponse.json(result.Items ?? []);
    }

    // "All" has no single partition on the status index. Query the known
    // statuses in parallel and merge rather than scanning the table.
    if (status === ALL_STATUSES) {
      const results = await Promise.all(STATUSES.map((s) => dynamo.send(queryByStatus(s))));
      const items = results.flatMap((r) => (r.Items ?? []) as Incident[]);
      return NextResponse.json(items.sort(byCreatedAtDesc));
    }

    const result = await dynamo.send(queryByStatus(status));
    return NextResponse.json(result.Items ?? []);
  } catch (err) {
    console.error("DynamoDB query failed:", err);
    return NextResponse.json({ error: "Failed to fetch incidents" }, { status: 500 });
  }
}
