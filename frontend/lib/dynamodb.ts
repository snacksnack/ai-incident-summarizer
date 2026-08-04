import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient } from "@aws-sdk/lib-dynamodb";

const client = new DynamoDBClient({
  region: process.env.AWS_REGION ?? "us-east-1",
  credentials: {
    accessKeyId: process.env.AWS_ACCESS_KEY_ID!,
    secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY!,
  },
});

export const dynamo = DynamoDBDocumentClient.from(client);
export const TABLE = process.env.INCIDENT_TABLE_NAME!;
export const SERVICE_REGISTRY_TABLE = process.env.SERVICE_REGISTRY_TABLE_NAME!;
