import { NextRequest, NextResponse } from "next/server";
import { QueryCommand, QueryCommandInput } from "@aws-sdk/lib-dynamodb";
import { dynamo, TABLE } from "@/lib/dynamodb";
import { Incident } from "@/lib/types";

const STATUSES = ["open", "acknowledged", "resolved"];
const ALL_STATUSES = "all";

function byCreatedAtDesc(a: Incident, b: Incident) {
  return b.created_at.localeCompare(a.created_at);
}

// Every read runs a Query to completion. DynamoDB pages at 1 MB whatever the
// index, so stopping after the first response would silently truncate the
// moment a partition outgrows a page (RC1-466).
async function queryAll(input: QueryCommandInput): Promise<Incident[]> {
  const items: Incident[] = [];
  let startKey: QueryCommandInput["ExclusiveStartKey"];
  do {
    const page = await dynamo.send(new QueryCommand({ ...input, ExclusiveStartKey: startKey }));
    items.push(...((page.Items ?? []) as Incident[]));
    startKey = page.LastEvaluatedKey;
  } while (startKey);
  return items;
}

/** All incidents for one service, optionally narrowed to a single status. */
function queryByService(service: string, status: string): QueryCommandInput {
  const narrowed = status !== ALL_STATUSES;
  return {
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
  };
}

function queryByStatus(status: string): QueryCommandInput {
  return {
    TableName: TABLE,
    IndexName: "status-created-index",
    KeyConditionExpression: "#st = :s",
    ExpressionAttributeNames: { "#st": "status" },
    ExpressionAttributeValues: { ":s": status },
    ScanIndexForward: false,
  };
}

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const service = searchParams.get("service");
  // "all" is the default. This dashboard is a history surface first: an
  // open-only default made a healthy system look stale, showing nothing but
  // never-closed seed data while real incidents resolved out of view (RC1-466).
  const status = searchParams.get("status") ?? ALL_STATUSES;

  if (status !== ALL_STATUSES && !STATUSES.includes(status)) {
    return NextResponse.json({ error: `Unknown status: ${status}` }, { status: 400 });
  }

  try {
    // Service and status compose. Previously a service filter silently ignored
    // status, so the UI could show resolved incidents while claiming "open".
    if (service) {
      return NextResponse.json(await queryAll(queryByService(service, status)));
    }

    // "All" has no single partition on the status index. Query the known
    // statuses in parallel and merge rather than scanning the table.
    if (status === ALL_STATUSES) {
      const results = await Promise.all(STATUSES.map((s) => queryAll(queryByStatus(s))));
      return NextResponse.json(results.flat().sort(byCreatedAtDesc));
    }

    return NextResponse.json(await queryAll(queryByStatus(status)));
  } catch (err) {
    console.error("DynamoDB query failed:", err);
    return NextResponse.json({ error: "Failed to fetch incidents" }, { status: 500 });
  }
}
