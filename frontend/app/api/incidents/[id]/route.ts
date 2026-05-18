import { NextRequest, NextResponse } from "next/server";
import { GetCommand } from "@aws-sdk/lib-dynamodb";
import { dynamo, TABLE } from "@/lib/dynamodb";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;

  try {
    const result = await dynamo.send(
      new GetCommand({ TableName: TABLE, Key: { incident_id: id } })
    );

    if (!result.Item) {
      return NextResponse.json({ error: "Incident not found" }, { status: 404 });
    }

    return NextResponse.json(result.Item);
  } catch (err) {
    console.error("DynamoDB get failed:", err);
    return NextResponse.json({ error: "Failed to fetch incident" }, { status: 500 });
  }
}
