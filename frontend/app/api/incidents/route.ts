import { NextRequest, NextResponse } from "next/server";
import { QueryCommand } from "@aws-sdk/lib-dynamodb";
import { dynamo, TABLE } from "@/lib/dynamodb";

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const status = searchParams.get("status");
  const service = searchParams.get("service");

  try {
    let command: QueryCommand;

    if (service) {
      command = new QueryCommand({
        TableName: TABLE,
        IndexName: "service-created-index",
        KeyConditionExpression: "affected_service = :s",
        ExpressionAttributeValues: { ":s": service },
        ScanIndexForward: false,
      });
    } else {
      const statusValue = status ?? "open";
      command = new QueryCommand({
        TableName: TABLE,
        IndexName: "status-created-index",
        KeyConditionExpression: "#st = :s",
        ExpressionAttributeNames: { "#st": "status" },
        ExpressionAttributeValues: { ":s": statusValue },
        ScanIndexForward: false,
      });
    }

    const result = await dynamo.send(command);
    return NextResponse.json(result.Items ?? []);
  } catch (err) {
    console.error("DynamoDB query failed:", err);
    return NextResponse.json({ error: "Failed to fetch incidents" }, { status: 500 });
  }
}
